"""Evidence-based phone recipes, separate from MCP transport and PI orchestration.

An accessibility event draft teaches intent but cannot establish success. Only a
successful paired demonstration can produce an executable recipe. Recipes use
semantic selectors resolved against the current observation, never cached IDs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import time

from controller import PhoneRuntime, private_write
from decision_policy import DecisionPolicyError

SELECTOR_KEYS = {"method", "role", "label"}
PREDICATE_KEYS = ("kind", "queryMatches", "firstTitleVisible", "liked", "collected")
MAX_STEPS = 24


class RecipeDrift(DecisionPolicyError):
    """A verified screen/selector changed; contains no phone execution error."""

    def __init__(self, reason):
        if reason not in {"precondition", "selector_missing", "postcondition", "settling_screen"}:
            raise ValueError("Unsupported recoverable recipe drift")
        self.reason = reason
        super().__init__("Recorded screen or selector changed")


def semantic_selector(action, task):
    selector = {key: action.get(key) for key in sorted(SELECTOR_KEYS)}
    title = task.get("expectedFirstTitle")
    if title and title in (selector.get("label") or ""):
        selector["label"] = title
    return selector


def recipe_digest(recipe):
    payload = {key: value for key, value in recipe.items() if key != "recipeSha256"}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def predicate(snapshot):
    state = snapshot.get("state", {})
    observation = state.get("observation", {})
    if (
        not state.get("stage")
        or not observation.get("kind")
        or type(snapshot.get("done")) is not bool
    ):
        raise ValueError("Paired screen verification is required")
    return {
        "stage": state["stage"],
        "done": snapshot["done"],
        "observation": {key: observation[key] for key in PREDICATE_KEYS if key in observation},
    }


def matches(snapshot, expected):
    actual = predicate(snapshot)
    return (
        actual["stage"] == expected["stage"]
        and actual["done"] == expected["done"]
        and all(
            actual["observation"].get(key) == value
            for key, value in expected["observation"].items()
        )
    )


def _validate_recipe(recipe):
    if (
        not isinstance(recipe, dict)
        or recipe.get("schemaVersion") != 1
        or recipe.get("reviewState") != "verified-demonstration"
        or recipe.get("source") != "pi-agent-demonstration"
    ):
        raise ValueError("Recipe must come from a verified paired demonstration")
    if recipe.get("recipeSha256") != recipe_digest(recipe):
        raise ValueError("Recipe changed; demonstrate and verify the new version")
    task = recipe.get("task", {})
    if not all(
        isinstance(task.get(key), str) and task[key] for key in ("query", "expectedFirstTitle")
    ):
        raise ValueError("Recipe requires task identity")
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError("Recipe step budget invalid")
    previous = None
    for step in steps:
        selector = step.get("selector", {})
        if set(selector) != SELECTOR_KEYS or not all(
            isinstance(v, str) and v for v in selector.values()
        ):
            raise ValueError("Selectors must contain only method, role, and label")
        if selector["method"] not in {"open", "click", "tap", "type", "wait"}:
            raise ValueError("Unsupported recorded action")
        settling = step.get("settleStates", [])
        if not isinstance(settling, list) or len(settling) > MAX_STEPS:
            raise ValueError("Invalid recorded settling states")
        for value in [step.get("before", {}), step.get("after", {}), *settling]:
            if (
                set(value) != {"stage", "done", "observation"}
                or not isinstance(value["stage"], str)
                or type(value["done"]) is not bool
                or not isinstance(value["observation"], dict)
                or not value["observation"].get("kind")
                or set(value["observation"]) - set(PREDICATE_KEYS)
            ):
                raise ValueError("Invalid verification predicate")
        if previous is not None and step["before"] != previous:
            raise ValueError("Recording observations are not contiguous")
        previous = step["after"]
    if not previous["done"] or previous["stage"] != "done":
        raise ValueError("Demonstration must verify completion")
    return recipe


def load_recipe(path):
    path = Path(path)
    if path.stat().st_size > 2_000_000:
        raise ValueError("Recipe is too large")
    return _validate_recipe(json.loads(path.read_text()))


def _new_bundle(directory, files):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, value in files.items():
        private_write(
            directory / name,
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2),
        )


def compile_recording(recording, directory):
    """Compile actual selected actions and their before/after observations."""
    started = time.perf_counter()
    if (
        recording.get("schemaVersion") != 1
        or recording.get("source") != "pi-agent-demonstration"
        or recording.get("status") != "success"
    ):
        raise ValueError("A successful paired demonstration is required")
    task = recording.get("task", {})
    previous_observation = None
    for step in recording.get("steps", []):
        for name in ("before", "after"):
            state = step.get(name, {}).get("state", {})
            if any(state.get(key) != value for key, value in task.items()):
                raise ValueError("Task identity changed inside the demonstration")
        before = step["before"]
        offered = [
            descriptor
            for choice, descriptor in before.get("state", {}).get("actionDescriptors", {}).items()
            if choice in before.get("options", {})
            and all(descriptor.get(key) == step.get("action", {}).get(key) for key in SELECTOR_KEYS)
        ]
        if len(offered) != 1:
            raise ValueError("Recorded action must uniquely match an offered control")
        before_predicate, after_predicate = predicate(before), predicate(step["after"])
        if previous_observation is not None and before_predicate != previous_observation:
            raise ValueError("Raw demonstration observations are not contiguous")
        previous_observation = after_predicate
        if step.get("delivery") == "not_dispatched":
            marker = step["after"].get("execution", {})
            choice = marker.get("choice")
            descriptor = before.get("state", {}).get("actionDescriptors", {}).get(choice)
            if (
                marker.get("status") != "not_dispatched"
                or marker.get("reason") != "stale_reference"
                or descriptor != step["action"]
                or marker.get("intent") != step["action"]
                or choice not in before.get("options", {})
                or before_predicate != after_predicate
            ):
                raise ValueError("Undelivered attempt lacks matching dispatch evidence")
    recipe = {
        "schemaVersion": 1,
        "source": "pi-agent-demonstration",
        "reviewState": "verified-demonstration",
        "task": copy.deepcopy(recording.get("task", {})),
        "sourceSha256": hashlib.sha256(
            json.dumps(recording, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "steps": [
            {
                "selector": semantic_selector(step["action"], recording.get("task", {})),
                "before": predicate(step["before"]),
                "after": predicate(step["after"]),
            }
            for step in recording.get("steps", [])
            if step.get("delivery") != "not_dispatched"
        ],
        "undeliveredAttempts": sum(
            step.get("delivery") == "not_dispatched" for step in recording.get("steps", [])
        ),
    }
    recipe["recipeSha256"] = recipe_digest(recipe)
    _validate_recipe(recipe)
    # Waiting teaches the observable completion condition. Keep all those
    # observations as evidence, without requiring a fixed count of WAIT actions.
    folded = []
    for step in recipe["steps"]:
        if (
            step["selector"]["method"] == "wait"
            and folded
            and folded[-1]["selector"]["role"] in {"navigation", "search_field"}
            and folded[-1]["selector"]["method"] in {"open", "click", "tap", "type"}
        ):
            previous = folded[-1]
            previous.setdefault("settleStates", []).append(previous["after"])
            previous["after"] = step["after"]
        else:
            folded.append(step)
    recipe["steps"] = folded
    recipe["recipeSha256"] = recipe_digest(recipe)
    _validate_recipe(recipe)
    task = recipe["task"]
    lines = [
        "---",
        "name: recorded-phone-workflow",
        "description: 复用已验证的小红书手机演示；通过 PI 和 Android MCP 执行。",
        "---",
        "",
        "# 适用场景",
        "重复执行同一手机任务。每次重新观察当前界面并验证结果。",
        "",
        "# 输入与范围",
        f"- 搜索词：{task['query']}",
        f"- 预期首篇标题：{task['expectedFirstTitle']}",
        "当前适配器只接受这组已验证输入；更换帖子或搜索词需要新演示。",
        "",
        "# 步骤",
    ]
    for index, step in enumerate(recipe["steps"], 1):
        lines.append(
            f"{index}. {step['selector']['method']}：{step['selector']['label']}；验证阶段 {step['after']['stage']}。"
        )
    lines.extend(
        [
            "",
            "# 执行与验证",
            "配套 recipe.json 是执行步骤和前后置条件。由 PI phone-benchmark 的 skill 模式加载。",
            "从当前候选控件唯一匹配 method、role、label，再使用本次观察的控件引用。禁止缓存节点 ID 或坐标。",
            "每步执行后使用同一 Android MCP 观察器验证后置条件。界面变化、歧义、未知状态或动作投递异常时停止。",
            "skill 模式遇到漂移即停止，不调用模型。",
            "显式选择 skill-auto 时，仅可恢复的界面或唯一控件缺失允许 Jev 接管本轮剩余步骤，不会强行返回原配方。",
            "Jev 请求失败后必须经 PI 重新观察，再由 DeepSeek 兜底一次；任务变化、目标歧义、动作投递不确定或预算耗尽仍停止。",
            "",
        ]
    )
    generation = {
        "status": "success",
        "generationMs": None,
        "stepCount": len(recipe["steps"]),
        "reviewState": recipe["reviewState"],
        "sourceSha256": recipe["sourceSha256"],
        "modelCalls": 0,
    }
    _new_bundle(
        directory,
        {"recipe.json": recipe, "SKILL.md": "\n".join(lines), "generation.json": generation},
    )
    generation["generationMs"] = (time.perf_counter() - started) * 1000
    generation["measurement"] = "compile and bundle writes, excludes final timing-file write"
    private_write(
        Path(directory) / "generation.json", json.dumps(generation, ensure_ascii=False, indent=2)
    )
    return recipe


def draft_android_recording(raw, directory):
    if raw.get("source") != "android-accessibility-events" or raw.get("schemaVersion") != 1:
        raise ValueError("Unsupported Android recording")
    events = raw.get("events", [])
    if not isinstance(events, list) or not 1 <= len(events) <= 2000:
        raise ValueError("No bounded demonstration events")
    draft = {
        "schemaVersion": 1,
        "source": raw["source"],
        "reviewState": "needs-verification",
        "targetPackage": raw.get("targetPackage"),
        "events": events,
    }
    lines = [
        "---",
        "name: phone-recording-draft",
        "description: 手机演示草稿，需要补充配对验证后才能执行。",
        "---",
        "",
        "# 手机录制草稿",
        "状态：待验证。原始无障碍事件无法证明每步操作结果，不能直接快速重放。",
        "",
        "# 观察到的步骤",
    ]
    for index, event in enumerate(events, 1):
        selector = event.get("selector", {})
        label = (
            selector.get("text")
            or selector.get("contentDescription")
            or selector.get("resourceId")
            or "未命名控件"
        )
        lines.append(f"{index}. {event.get('type', 'unknown')}：{label}")
    lines.extend(
        [
            "",
            "# 待补充",
            "明确本次输入与成功条件；用 PI 观察每步执行前后界面，完成配对验证，再生成 recipe.json。",
            "",
        ]
    )
    _new_bundle(directory, {"draft.json": draft, "SKILL.md": "\n".join(lines)})
    return draft


class RecordingPhoneRuntime(PhoneRuntime):
    """Capture evidence from the same PI-owned operations as the baseline."""

    def __init__(self, trial_id):
        super().__init__(trial_id)
        self.recorded_steps = []

    def act(self, choice):
        before = copy.deepcopy(self.snapshot)
        action = copy.deepcopy(self.actions[choice])
        after = super().act(choice)
        self.recorded_steps.append(
            {
                "before": before,
                "action": {key: action.get(key) for key in sorted(SELECTOR_KEYS)},
                "after": copy.deepcopy(after),
                "delivery": after.get("execution", {}).get("status", "applied"),
            }
        )
        return after

    def finish(self, extra=None):
        result = super().finish(extra)
        state = (self.snapshot or {}).get("state", {})
        recording = {
            "schemaVersion": 1,
            "source": "pi-agent-demonstration",
            "status": result["status"],
            "task": {key: state.get(key) for key in ("query", "expectedFirstTitle")},
            "steps": self.recorded_steps,
        }
        private_write(
            self.directory / "recording.json", json.dumps(recording, ensure_ascii=False, indent=2)
        )
        return result


class SkillPolicy:
    """No network decisions on a verified path; drift fails closed in this MVP."""

    def __init__(self, client, mode, *, recipe=None, **_budgets):
        if mode != "skill":
            raise ValueError("Unsupported recipe mode")
        self.recipe = (
            _validate_recipe(copy.deepcopy(recipe))
            if recipe is not None
            else load_recipe(os.environ["PHONE_BENCH_RECIPE"])
        )
        self.attempts = []
        self.cursor = self.hits = 0
        self.pending = None
        self.last_observation = -1
        self.settling = False
        self.settle_waits = self.current_waits = 0
        self.reference_refreshes = self.step_refreshes = 0
        self.selected_choice = None

    async def decide(self, snapshot, observation_id):
        started = time.perf_counter()
        try:
            if observation_id <= self.last_observation or (
                self.pending is not None and not self.settling
            ):
                raise ValueError("Fresh verified observation required")
            self.last_observation = observation_id
            state = snapshot["state"]
            if any(state.get(key) != value for key, value in self.recipe["task"].items()):
                raise ValueError("Task identity changed")
            if self.cursor >= len(self.recipe["steps"]):
                raise ValueError("Recipe exhausted")
            if self.pending is not None:
                if not any(
                    matches(snapshot, state) for state in self.pending.get("settleStates", [])
                ):
                    raise RecipeDrift("settling_screen")
                if self.current_waits >= 8 or "WAIT" not in snapshot["options"]:
                    raise ValueError("Recorded settling budget exhausted")
                candidates = ["WAIT"]
                self.current_waits += 1
                self.settle_waits += 1
                self.settling = False
            else:
                step = self.recipe["steps"][self.cursor]
                candidates = [
                    choice
                    for choice, descriptor in state.get("actionDescriptors", {}).items()
                    if choice in snapshot["options"]
                    and semantic_selector(descriptor, self.recipe["task"]) == step["selector"]
                ]
                if len(candidates) > 1:
                    raise ValueError("Selector must uniquely match a current control")
                if not matches(snapshot, step["before"]):
                    raise RecipeDrift("precondition")
                if not candidates:
                    raise RecipeDrift("selector_missing")
                self.pending = step
                self.current_waits = 0
                self.hits += 1
            self.selected_choice = candidates[0]
            return {
                "kind": "decision",
                "choice": candidates[0],
                "latencyMs": (time.perf_counter() - started) * 1000,
                "selectedBackend": "skill",
                "servedModel": "local-verified-recipe",
                "modelRequest": False,
                "fallbackUsed": False,
                "usage": {},
                "cost": 0,
            }
        except (KeyError, TypeError, ValueError) as error:
            raise DecisionPolicyError("Recipe cannot safely resolve the next step") from error

    def not_dispatched(self, snapshot, choice):
        """Release only a proven pre-dispatch failure for PI's next decision."""
        try:
            marker = snapshot.get("execution", {})
            if (
                self.pending is None
                or self.settling
                or choice == "WAIT"
                or choice != self.selected_choice
                or set(marker) != {"status", "reason", "choice", "intent"}
                or set(marker.get("intent", {})) != {"method", "role", "label"}
                or marker.get("status") != "not_dispatched"
                or marker.get("reason") != "stale_reference"
                or marker.get("choice") != choice
                or semantic_selector(marker.get("intent", {}), self.recipe["task"])
                != self.pending["selector"]
                or not matches(snapshot, self.pending["before"])
                or any(
                    snapshot["state"].get(key) != value
                    for key, value in self.recipe["task"].items()
                )
                or self.step_refreshes >= 3
            ):
                raise ValueError("No safe undelivered step to resolve again")
            self.reference_refreshes += 1
            self.step_refreshes += 1
            self.pending = None
            self.selected_choice = None
        except (KeyError, TypeError, ValueError) as error:
            raise DecisionPolicyError("Recipe reference refresh is not authorized") from error

    def verify(self, snapshot):
        try:
            if self.pending is None or any(
                snapshot["state"].get(key) != value for key, value in self.recipe["task"].items()
            ):
                raise ValueError("Recorded postcondition not met")
            if not matches(snapshot, self.pending["after"]):
                if any(matches(snapshot, state) for state in self.pending.get("settleStates", [])):
                    self.settling = True
                    return
                raise RecipeDrift("postcondition")
            self.cursor += 1
            self.pending = None
            self.settling = False
            self.step_refreshes = 0
            self.selected_choice = None
        except (KeyError, TypeError, ValueError) as error:
            raise DecisionPolicyError("Recipe postcondition verification failed") from error

    def verify_complete(self, snapshot):
        try:
            if (
                self.pending is not None
                or self.cursor != len(self.recipe["steps"])
                or not matches(snapshot, self.recipe["steps"][-1]["after"])
                or any(
                    snapshot["state"].get(key) != value
                    for key, value in self.recipe["task"].items()
                )
            ):
                raise ValueError("Final state changed or steps remain unverified")
        except (KeyError, TypeError, ValueError) as error:
            raise DecisionPolicyError("Recipe did not verify every recorded step") from error

    def summary(self):
        return {
            "mode": "skill",
            "pendingRefresh": False,
            "recipeHits": self.hits,
            "verifiedSteps": self.cursor,
            "settleWaits": self.settle_waits,
            "referenceRefreshes": self.reference_refreshes,
            "stepCount": len(self.recipe["steps"]),
            "modelFallbacks": 0,
            "attempts": [],
            "driftPolicy": "stop",
            "sourceSha256": self.recipe["sourceSha256"],
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["compile", "import-android"])
    parser.add_argument("recording", type=Path)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    raw = json.loads(args.recording.read_text())
    result = (compile_recording if args.command == "compile" else draft_android_recording)(
        raw, args.bundle
    )
    print(
        json.dumps(
            {"reviewState": result["reviewState"], "bundle": str(args.bundle)}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
