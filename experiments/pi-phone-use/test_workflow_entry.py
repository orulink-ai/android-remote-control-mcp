"""Generated recipes prepare a real feed through PI before local cursor starts."""

import asyncio
import copy
import json

import pytest

from controller import PACKAGE
from skill_auto import SkillAutoPolicy
from test_workflow_companion import paired
from test_workflow_intents import supported_raw
from test_workflow_verification import screen
from workflow import compile_recording, load_recipe
from workflow_intents import derive_plan


def entry_screen(kind, action):
    state = screen(0)
    state["state"]["goal"] = "录制的固定闭环"
    state["state"]["observation"]["kind"] = kind
    state["state"]["observation"]["firstTitleVisible"] = kind == "detail"
    state["state"]["focusedPackage"] = (
        PACKAGE if kind != "launcher" else "com.miui.personalassistant"
    )
    state["state"]["actionDescriptors"] = {"PREP": action}
    state["options"] = {"PREP": action["label"]}
    return state


class Model:
    def __init__(self):
        self.calls = []

    async def decide(self, backend, state, options):
        self.calls.append((backend, list(options)))
        return {"choice": "PREP", "latencyMs": 0, "cost": 0, "usage": {}}


@pytest.mark.parametrize("back_count", [0, 4, 5])
def test_entry_preparation_then_eight_local_steps_is_distinct_from_drift(tmp_path, back_count):
    recipe = compile_recording(
        paired(derive_plan(supported_raw())), tmp_path / "bundle", entry_strategy="prepare-feed-v1"
    )
    assert load_recipe(tmp_path / "bundle/recipe.json")["entryStrategy"] == "prepare-feed-v1"
    model = Model()
    policy = SkillAutoPolicy(model, "skill-auto", recipe=recipe)
    prepare = [
        entry_screen("launcher", {"method": "open", "role": "navigation", "label": "打开小红书"})
    ]
    prepare += [
        entry_screen(kind, {"method": "back", "role": "navigation", "label": "返回搜索入口"})
        for kind in ["detail", "results", "search_input", "search_input", "transition"][:back_count]
    ]
    observation = 0
    for index, before in enumerate(prepare):
        observation += 1
        result = asyncio.run(policy.decide(before, observation))
        assert result["preparation"] is True
        assert policy.summary()["recipeHits"] == policy.summary()["verifiedSteps"] == 0
        after = prepare[index + 1] if index + 1 < len(prepare) else screen(0)
        policy.verify(after)
    for index in range(8):
        observation += 1
        result = asyncio.run(policy.decide(screen(index), observation))
        assert result["selectedBackend"] == "skill"
        policy.verify(screen(index + 1))
    policy.verify_complete(screen(8))
    summary = policy.summary()
    assert summary["recipeHits"] == summary["verifiedSteps"] == 8
    assert summary["preparationRequests"] == len(prepare)
    assert summary["recoveryRequests"] == summary["recipeToModelRecoveries"] == 0
    assert summary["preparationComplete"] is True and not summary["recoveryActive"]
    assert len(model.calls) == len(prepare) and len(summary["attempts"]) == len(prepare)


def test_already_feed_new_recipe_has_no_preparation_api(tmp_path):
    recipe = compile_recording(
        paired(derive_plan(supported_raw())), tmp_path / "bundle", entry_strategy="prepare-feed-v1"
    )
    model = Model()
    policy = SkillAutoPolicy(model, "skill-auto", recipe=recipe)
    assert asyncio.run(policy.decide(screen(0), 1))["selectedBackend"] == "skill"
    assert model.calls == [] and policy.summary()["preparationRequests"] == 0


def test_runtime_preparation_masks_task_controls_and_never_advances_common_progress(
    tmp_path, monkeypatch
):
    from controller import PhoneRuntime, Progress
    from terminal_runtime import TerminalRecordedRuntime
    from providers import _context, _jev_payload

    frames = ["launcher", "detail", "results", "search_input", "search_input", "transition", "feed"]

    def initialize(self, trial):
        self.directory, self.index = tmp_path, 0
        self.progress, self.operations, self.phases = Progress(), [], []

    def call(self, name, args):
        package = "com.miui.personalassistant" if self.index == 0 else PACKAGE
        return {
            "content": [{"type": "text", "text": f"--- window:1 pkg:{package} focused:true ---"}]
        }

    def observe(self):
        kind = frames[self.index]
        self.index += 1
        package = "com.miui.personalassistant" if kind == "launcher" else PACKAGE
        snapshot = entry_screen(kind, {"method": "click", "role": "like", "label": "点赞"})
        self.progress.update(snapshot["state"]["observation"])
        snapshot["state"]["stage"] = self.progress.phase
        self.actions = copy.deepcopy(snapshot["state"]["actionDescriptors"])
        self.actions["WAIT"] = {"method": "wait", "role": "wait", "label": "等待"}
        self.actions["BLOCKED"] = {"method": "blocked", "role": "blocked", "label": "停止"}
        if kind == "launcher":
            self.actions["OPEN_APP"] = {
                "method": "open",
                "role": "navigation",
                "label": "打开小红书",
            }
        if kind == "feed":
            self.actions = copy.deepcopy(screen(0)["state"]["actionDescriptors"])
        snapshot["options"] = {key: value["label"] for key, value in self.actions.items()}
        snapshot["state"]["actionDescriptors"] = copy.deepcopy(self.actions)
        (tmp_path / f"observation-{self.index}.txt").write_text(
            f"--- window:1 pkg:{package} focused:true ---"
        )
        self.snapshot = snapshot
        return snapshot

    monkeypatch.setattr(PhoneRuntime, "__init__", initialize)
    monkeypatch.setattr(PhoneRuntime, "call", call)
    monkeypatch.setattr(PhoneRuntime, "observe", observe)
    runtime = TerminalRecordedRuntime(
        "entry", {"kind": "recorded", "goal": "验证入口"}, entry_strategy="prepare-feed-v1"
    )
    for kind in frames:
        value = runtime.observe()
        assert value["state"]["stage"] == "search" and runtime.progress.milestones == []
        if kind == "feed":
            assert "NEXT" in runtime.actions and value["state"]["recipeEntryReady"] is True
        else:
            assert all(
                action["method"] in {"open", "back", "wait", "blocked"}
                for action in runtime.actions.values()
            )
            assert value["state"]["recipeEntryReady"] is False
            payload = _jev_payload(
                _context(value["state"], value["options"], True), value["options"], True, "heads"
            )
            assert "允许的准备导航" in payload["state"]["goal"]
            assert payload["state"]["goal"] == payload["state"]["nextGoal"]


def install_transport_double():
    """Keep real TerminalRecordedRuntime; replace only MCP-facing common boundary."""
    import os
    from pathlib import Path
    import time
    import terminal_runtime
    from controller import PhoneRuntime, Progress
    from test_workflow_verification import screen

    back_count = int(os.environ.get("OFFLINE_BACKS", "5"))

    def init(self, trial):
        self.trial_id, self.position, self.index = trial, 0, 0
        self.feed_position = back_count + 1
        self.directory = Path(os.environ["PHONE_BENCH_OUTPUT_DIR"]) / trial
        self.directory.mkdir(parents=True)
        self.progress, self.error = Progress(), None
        self.operations, self.phases, self.model_calls = [], [], []
        self.started, self.snapshot = time.perf_counter(), None

    def kind(self):
        if self.position >= self.feed_position:
            return screen(self.position - self.feed_position)["state"]["observation"]["kind"]
        return (
            "launcher"
            if self.position == 0
            else ["detail", "results", "search_input", "search_input", "transition"][
                self.position - 1
            ]
        )

    def call(self, name, args):
        self.operations.append({"name": name, "arguments": args, "ms": 0, "status": "success"})
        if name == "android_press_key":
            self.position = 0
        if name == "android_open_app":
            self.position = 1
        if name == "android_press_back":
            self.position += 1
        package = "com.miui.personalassistant" if kind(self) == "launcher" else PACKAGE
        return {
            "content": [{"type": "text", "text": f"--- window:1 pkg:{package} focused:true ---"}]
        }

    def observe(self):
        raw = call(self, "android_get_screen_state", {"include_screenshot": True})["content"][0][
            "text"
        ]
        self.index += 1
        if self.position >= self.feed_position:
            snapshot = screen(self.position - self.feed_position)
        else:
            snapshot = entry_screen(
                kind(self), {"method": "click", "role": "navigation", "label": "发布"}
            )
            snapshot["state"]["actionDescriptors"].update(
                WAIT={"method": "wait", "role": "wait", "label": "等待"},
                BLOCKED={"method": "blocked", "role": "blocked", "label": "停止"},
            )
            if self.position == 0:
                snapshot["state"]["actionDescriptors"]["OPEN_APP"] = {
                    "method": "open",
                    "role": "navigation",
                    "label": "打开小红书",
                }
        snapshot["state"]["goal"] = "录制的固定闭环"
        self.progress.update(snapshot["state"]["observation"])
        snapshot["state"]["stage"], snapshot["done"] = (
            self.progress.phase,
            self.progress.phase == "done",
        )
        self.actions = copy.deepcopy(snapshot["state"]["actionDescriptors"])
        snapshot["options"] = {key: value["label"] for key, value in self.actions.items()}
        (self.directory / f"observation-{self.index}.txt").write_text(raw)
        self.snapshot = snapshot
        return snapshot

    def act(self, choice):
        assert choice in self.actions
        action = self.actions[choice]
        self.operations.append(
            {
                "name": "selected_action",
                "stage": self.progress.phase,
                "choice": choice,
                "label": action["label"],
            }
        )
        if action["method"] == "open":
            call(self, "android_open_app", {"package_id": PACKAGE})
        elif choice == "NEXT":
            call(
                self,
                "android_type_append_text" if action["method"] == "type" else "android_click_node",
                {},
            )
            self.position += 1
        elif choice != "WAIT":
            raise RuntimeError("Forbidden task mutation")
        return self.observe()

    def finish(self, extra=None):
        result = {
            "status": "success" if self.progress.phase == "done" else "failed",
            "modelCalls": self.model_calls,
            "milestones": self.progress.milestones,
            "operations": self.operations,
            "phases": self.phases,
            "finalState": self.snapshot["state"]["observation"] if self.snapshot else None,
            **(extra or {}),
        }
        (self.directory / "result.json").write_text(json.dumps(result))
        return result

    PhoneRuntime.__init__, PhoneRuntime.call = init, call
    PhoneRuntime.observe, PhoneRuntime.act, PhoneRuntime.finish = observe, act, finish
    terminal_runtime.time.sleep = lambda _: None


def test_preparation_provider_failure_requires_refresh_before_deepseek(tmp_path):
    from providers import ProviderError

    recipe = compile_recording(
        paired(derive_plan(supported_raw())), tmp_path / "bundle", entry_strategy="prepare-feed-v1"
    )

    class Failing(Model):
        async def decide(self, backend, state, options):
            self.calls.append(backend)
            if len(self.calls) == 1:
                raise ProviderError(
                    "offline",
                    network={},
                    latency_ms=0,
                    exception_type="ConnectError",
                    stage="tcp_connect",
                )
            return {"choice": "PREP", "latencyMs": 0, "cost": 0, "usage": {}}

    model = Failing()
    policy = SkillAutoPolicy(model, "skill-auto", recipe=recipe)
    current = entry_screen("detail", {"method": "back", "role": "navigation", "label": "返回"})
    assert asyncio.run(policy.decide(current, 1))["kind"] == "refresh_required"
    assert model.calls == ["jev"] and policy.summary()["verifiedSteps"] == 0
    result = asyncio.run(policy.decide(current, 2))
    assert result["selectedBackend"] == "deepseek" and result["preparation"] is True
    policy.verify(screen(0))
    assert asyncio.run(policy.decide(screen(0), 3))["selectedBackend"] == "skill"
    assert policy.summary()["preparationRequests"] == 2
    assert policy.summary()["recoveryRequests"] == 0
    assert policy.summary()["fallbackSuccesses"] == 1


def test_unknown_entry_strategy_and_edited_entry_digest_rejected(tmp_path):
    from workflow import recipe_digest

    recipe = compile_recording(
        paired(derive_plan(supported_raw())), tmp_path / "bundle", entry_strategy="prepare-feed-v1"
    )
    recipe["entryStrategy"] = "other"
    recipe["recipeSha256"] = recipe_digest(recipe)
    (tmp_path / "bad.json").write_text(json.dumps(recipe))
    with pytest.raises(ValueError):
        load_recipe(tmp_path / "bad.json")
    recipe["entryStrategy"] = "prepare-feed-v1"
    (tmp_path / "bad.json").write_text(json.dumps(recipe))
    with pytest.raises(ValueError):
        load_recipe(tmp_path / "bad.json")
