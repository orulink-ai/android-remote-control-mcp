"""Dedicated PI worker for consuming a bounded Android-recorded intent plan.

This is an explicit companion-only entry, leaving the ordinary terminal worker
unchanged. Plans constrain every task mutation; preparation and settling are
recorded separately and can never count as consumed Android events.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from functools import partial
from pathlib import Path

from controller import Progress, private_write
from terminal_runtime import BACK, PREPARATION_GOAL, TerminalRecordedRuntime
from workflow import SELECTOR_KEYS
from workflow_intents import (
    TASK,
    intent_completed,
    matches_intent,
    raw_intent_completed,
    transition_matches,
    validate_plan,
    validation_binding,
)
from worker_io import cancellation_scope
from providers import DecisionClient
import worker


class IntentVerificationError(RuntimeError):
    pass


def load_plan(path):
    value = json.loads(Path(path).read_text())
    try:
        validate_plan(value)
    except ValueError:
        raise IntentVerificationError("Invalid verified-scope intent plan") from None
    return value


class DraftIntentRuntime(TerminalRecordedRuntime):
    def __init__(self, trial_id, plan):
        self.plan = plan
        self.cursor = 0
        self.pending_intent = None
        self.settle_waits = self.prepare_backs = 0
        self.recorded_steps = []
        self.preparation_steps = []
        self.offered = {}
        self.transition_pending = False
        self.transition_dispatches = self.transition_waits = 0
        super().__init__(
            trial_id,
            {
                "kind": "recorded",
                "goal": "先经允许的导航准备到小红书首页，再逐项验证手机录制意图；仅允许有证据的必要搜索过渡。",
            },
            entry_strategy="prepare-feed-v1",
        )

    def observe(self):
        snapshot = super().observe()
        if self.cursor == 0 and self.pending_intent is None:
            # A retained previous results page is preparation, not evidence that
            # the newly recorded search intent was actually performed.
            self.progress = Progress()
            snapshot["state"]["stage"], snapshot["done"] = "search", False
            if (
                snapshot["state"]["focusedPackage"] == "com.xingin.xhs"
                and snapshot["state"]["observation"]["kind"] != "feed"
            ):
                self.actions[BACK] = {
                    "method": "back",
                    "role": "navigation",
                    "label": "返回录制任务的搜索入口",
                }
        self.offered = copy.deepcopy(self.actions)
        return self._filter_options(snapshot)

    def _filter_options(self, snapshot):
        offered = self.offered
        allowed = {key: action for key, action in offered.items() if key in {"WAIT", "BLOCKED"}}
        if (
            self.pending_intent is None
            and not self.transition_pending
            and self.cursor < len(self.plan["intents"])
        ):
            kind = self.plan["intents"][self.cursor]["kind"]
            require_results = self.plan["schemaVersion"] == 2 and kind == "open_target"
            matches = {
                key: action
                for key, action in offered.items()
                if matches_intent(kind, snapshot, action)
                and (not require_results or intent_completed("search_submit", snapshot))
                and (
                    self.plan["schemaVersion"] != 2
                    or kind != "search_submit"
                    or transition_matches(snapshot, action)
                )
            }
            if (
                require_results
                and self.plan.get("runtimeTransitionPolicy") is not None
                and not matches
                and self.transition_dispatches == 0
            ):
                matches = {
                    key: action
                    for key, action in offered.items()
                    if transition_matches(snapshot, action)
                }
            if len(matches) > 1:
                raise IntentVerificationError("Recorded intent has ambiguous live targets")
            allowed.update(matches)
            if self.cursor == 0:
                allowed.update(
                    {
                        key: action
                        for key, action in offered.items()
                        if key == "OPEN_APP" or key == BACK
                    }
                )
        self.actions = allowed
        snapshot["options"] = {key: action["label"] for key, action in allowed.items()}
        snapshot["state"]["actionDescriptors"] = {
            key: {name: action.get(name) for name in SELECTOR_KEYS}
            for key, action in allowed.items()
        }
        snapshot["state"]["recordedIntent"] = (
            self.plan["intents"][self.cursor] if self.cursor < len(self.plan["intents"]) else None
        )
        snapshot["state"]["runtimeTransition"] = (
            self.plan.get("runtimeTransitionPolicy")
            if self.plan["schemaVersion"] == 2 and self.cursor == 2
            else None
        )
        snapshot["state"]["androidRecordingSha256"] = self.plan["rawSha256"]
        snapshot["state"]["goal"] = snapshot["state"]["nextGoal"] = self._current_goal(snapshot)
        private_write(
            self.directory / f"state-{self.index}.json", json.dumps(snapshot, ensure_ascii=False)
        )
        self.snapshot = snapshot
        return snapshot

    def _current_goal(self, snapshot):
        """Describe only the current allowed phase without changing action gates."""
        if self.transition_pending:
            return "必要搜索过渡已派发；只等待并回读核实正确查询的搜索结果，不重复提交搜索或输入。"
        if self.pending_intent is not None:
            return "当前录制意图已派发；只等待并回读验证其结果，不重复动作，不提前执行下一项。"
        if self.cursor >= len(self.plan["intents"]):
            return "录制意图已逐项验证；只回读核实目标帖子最终未点赞、未收藏，不再派发修改。"
        kind = self.plan["intents"][self.cursor]["kind"]
        if self.cursor == 0 and snapshot["state"]["observation"]["kind"] != "feed":
            return PREPARATION_GOAL
        if (
            kind == "open_target"
            and self.plan.get("runtimeTransitionPolicy") is not None
            and not intent_completed("search_submit", snapshot)
        ):
            if self.transition_dispatches:
                return "必要搜索过渡已派发；只等待并回读正确查询的搜索结果，不重复提交搜索或输入。"
            return (
                f"必要搜索过渡：核实当前输入为“{self.plan['task']['query']}”后，"
                "只选择当前提供的唯一搜索提交一次，再回读搜索结果；这是允许的必要导航，"
                "不改查询、不点赞、不收藏。若结果已自动出现，则直接验证，不补造提交。"
            )
        goals = {
            "search_entry": "执行当前录制意图：点击小红书首页搜索入口，进入搜索输入页。",
            "query": f"执行当前录制意图：在搜索框填入“{self.plan['task']['query']}”，并回读核实；不重复追加输入。",
            "search_submit": f"执行当前录制意图：核实查询为“{self.plan['task']['query']}”，点击唯一搜索提交一次并回读结果。",
            "open_target": f"执行当前录制意图：打开搜索结果中的目标帖子“{self.plan['task']['expectedFirstTitle']}”，回读核实目标身份。",
            "like": "执行当前录制意图：给已核实的目标帖子点赞，回读确认 liked=true；不要反复点击。",
            "collect": "执行当前录制意图：收藏已核实的目标帖子，回读确认 collected=true；不要重复点赞。",
            "unlike": "执行当前录制意图：对目标帖子取消点赞，回读确认 liked=false，保留收藏。",
            "uncollect": "执行当前录制意图：对目标帖子取消收藏，回读确认 collected=false、liked=false。",
        }
        return goals[kind]

    def act(self, choice):
        if choice not in self.actions:
            raise IntentVerificationError("Action is outside the current recorded intent")
        before, action = copy.deepcopy(self.snapshot), copy.deepcopy(self.actions[choice])
        kind = (
            self.plan["intents"][self.cursor]["kind"]
            if self.cursor < len(self.plan["intents"])
            else None
        )
        recorded = (
            self.pending_intent is None
            and kind is not None
            and matches_intent(kind, before, action)
        )
        transition = (
            self.plan["schemaVersion"] == 2
            and self.plan.get("runtimeTransitionPolicy") is not None
            and kind == "open_target"
            and not recorded
            and not self.transition_pending
            and self.transition_dispatches == 0
            and transition_matches(before, action)
        )
        if not recorded and not transition and choice not in {"WAIT", "BLOCKED", "OPEN_APP", BACK}:
            raise IntentVerificationError("Unrecorded task mutation is forbidden")
        if choice in {"OPEN_APP", BACK}:
            if self.cursor != 0 or self.pending_intent is not None:
                raise IntentVerificationError("Preparation cannot replace a recorded action")
            if choice == BACK:
                self.prepare_backs += 1
                if self.prepare_backs > 8:
                    raise IntentVerificationError("Preparation budget exceeded")
        if recorded:
            self.pending_intent = self.cursor
        if transition:
            self.transition_pending = True
        after = super().act(choice)
        marker = after.get("execution", {"status": "applied"})
        delivery = marker.get("status") if isinstance(marker, dict) else None
        if delivery not in {"applied", "not_dispatched"} or (
            delivery == "applied" and set(marker) != {"status"}
        ):
            raise IntentVerificationError("Uncertain delivery cannot become demonstration evidence")
        step = {
            "before": before,
            "action": {key: action.get(key) for key in SELECTOR_KEYS},
            "after": copy.deepcopy(after),
            "delivery": delivery,
        }
        if recorded and delivery == "applied":
            step.update(
                rawEventSequence=self.plan["intents"][self.cursor]["eventSequence"],
                intentKind=kind,
                rawEventSha256=self.plan["intents"][self.cursor]["eventSha256"],
                origin="raw-intent",
            )
        elif recorded:
            self.pending_intent = None
            step.update(
                origin="raw-intent-undelivered",
                intentKind=kind,
                intendedRawEventSequence=self.plan["intents"][self.cursor]["eventSequence"],
            )
        if transition:
            step.update(
                origin="runtime-required-transition",
                intentKind="search_submit",
                rawEventSequence=None,
                betweenRawEvents=list(self.plan["runtimeTransitionPolicy"]["betweenRawEvents"]),
            )
            if delivery == "applied":
                self.transition_dispatches += 1
            else:
                self.transition_pending = False
        if recorded or self.cursor > 0 or self.pending_intent is not None:
            self.recorded_steps.append(step)
        else:
            self.preparation_steps.append(step)
        if self.pending_intent is not None:
            if raw_intent_completed(kind, after, self.plan):
                self.cursor += 1
                self.pending_intent = None
                self.settle_waits = 0
            elif choice == "WAIT":
                self.settle_waits += 1
                if self.settle_waits >= 8:
                    raise IntentVerificationError("Recorded postcondition did not verify")
        if self.transition_pending:
            if intent_completed("search_submit", after):
                self.transition_pending = False
            elif choice == "WAIT":
                self.transition_waits += 1
                if self.transition_waits >= 8:
                    raise IntentVerificationError("Required search transition did not verify")
        # The parent observed while the intent was pending, exposing only WAIT.
        # Rebind candidates from that same snapshot without another phone read.
        if self.pending_intent is None:
            self._refresh_options(after)
        return self.snapshot

    def _refresh_options(self, snapshot):
        # Refilter only the controls already captured by the paired readback.
        # Advancing the intent never performs a hidden phone operation.
        self._filter_options(snapshot)

    def finish(self, extra=None):
        binding = validation_binding(self.plan)
        complete = (
            self.cursor == len(self.plan["intents"])
            and self.pending_intent is None
            and not self.transition_pending
            and self.progress.phase == "done"
            and intent_completed("uncollect", self.snapshot)
            and [item.get("stage") for item in self.progress.milestones]
            == ["search", "open_first", "like", "collect", "unlike", "uncollect"]
        )
        values = {
            **(extra or {}),
            "recordingValidation": binding
            if complete
            else (
                {**binding, "allRawIntentsVerified": False}
                if self.plan["schemaVersion"] == 2
                else {"allConsumed": False}
            ),
        }
        if not complete:
            values["status"] = "failed"
        result = super().finish(values)
        recording = {
            "schemaVersion": 1,
            "source": "pi-agent-demonstration",
            "status": result["status"],
            "task": TASK,
            "steps": self.recorded_steps,
            "preparationSteps": self.preparation_steps,
            "recordingValidation": result["recordingValidation"],
        }
        if self.plan["schemaVersion"] == 2:
            recording.update(
                intentPlan=copy.deepcopy(self.plan),
                rawEventDispositions=copy.deepcopy(self.plan["rawEventDispositions"]),
                observationNotifications=copy.deepcopy(self.plan["observationNotifications"]),
                runtimeTransitionPolicy=copy.deepcopy(self.plan["runtimeTransitionPolicy"]),
            )
        private_write(
            self.directory / "recording.json", json.dumps(recording, ensure_ascii=False, indent=2)
        )
        return result


async def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--trial-id", required=True)
    args = parser.parse_args(argv)
    plan = load_plan(os.environ["PHONE_WORKFLOW_PLAN"])
    try:
        async with cancellation_scope():
            await worker.serve(
                "jev-auto",
                args.trial_id,
                runtime_factory=lambda trial: DraftIntentRuntime(trial, plan),
                client_factory=partial(DecisionClient, providerFallbacks=True),
            )
    except asyncio.CancelledError:
        return 143
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
