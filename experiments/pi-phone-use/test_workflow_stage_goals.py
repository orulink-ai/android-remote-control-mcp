"""Decision payloads describe allowed preparation and current semantic intent."""

import copy
import json
from pathlib import Path

from providers import _context, _jev_payload
from terminal_runtime import BACK, TerminalRecordedRuntime
from test_workflow_semantic import semantic_raw
from test_workflow_verification import install_fake
from workflow_intents import TASK, derive_plan
from workflow_verification import DraftIntentRuntime


def payload(snapshot):
    return _jev_payload(
        _context(snapshot["state"], snapshot["options"], True), snapshot["options"], True, "heads"
    )


def failed_preparation_snapshot():
    root = (
        Path.home()
        / ".local/share/orulink-phone-workflow/jobs/f49dacf0-96b5-4cd8-8454-c8aa9150ccad/verify/pi"
    )
    paths = list(root.glob("*/state-4.json"))
    if paths:
        return json.loads(paths[0].read_text())
    actions = {
        "WAIT": {"method": "wait", "role": "wait", "label": "等待界面稳定再观察"},
        "BLOCKED": {"method": "blocked", "role": "blocked", "label": "无法继续；停止并记录原因"},
        BACK: {"method": "back", "role": "navigation", "label": "返回录制任务的搜索入口"},
    }
    return {
        "state": {
            **TASK,
            "goal": "按手机录制的完整有界意图逐步配对验证，不执行录制之外的任务",
            "stage": "search",
            "focusedPackage": "com.xingin.xhs",
            "observation": {"kind": "detail", "firstTitleVisible": True},
            "actionDescriptors": actions,
        },
        "options": {key: item["label"] for key, item in actions.items()},
        "done": False,
    }


def test_real_failed_detail_payload_now_explicitly_allows_preparation_back(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    runtime = DraftIntentRuntime("verify", derive_plan(semantic_raw()))
    snapshot = failed_preparation_snapshot()
    previous = copy.deepcopy(snapshot)
    runtime.offered = copy.deepcopy(snapshot["state"]["actionDescriptors"])
    snapshot = runtime._filter_options(snapshot)
    body = payload(snapshot)
    assert "允许的准备导航" in body["state"]["goal"]
    assert "返回到小红书首页" in body["state"]["nextGoal"]
    assert "不执行录制之外的任务" not in json.dumps(body, ensure_ascii=False)
    assert body["state"]["goal"] == body["state"]["nextGoal"]
    assert snapshot["state"]["stage"] == previous["state"]["stage"]
    assert snapshot["options"] == previous["options"] and "BLOCKED" in snapshot["options"]
    assert runtime.cursor == 0 and runtime.operations == []


def test_payload_goal_tracks_raw_intent_required_submit_and_verified_target(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    runtime = DraftIntentRuntime("verify", derive_plan(semantic_raw()))
    runtime.observe()
    assert "搜索入口" in runtime.snapshot["state"]["goal"]
    runtime.act("NEXT")
    assert (
        "填入" in runtime.snapshot["state"]["goal"]
        and "小狮日记" in runtime.snapshot["state"]["nextGoal"]
    )
    runtime.act("NEXT")
    body = payload({**runtime.snapshot, "options": {"CLICK:submit": "搜索"}})
    assert "必要搜索过渡" in body["state"]["goal"]
    assert "唯一" in body["state"]["nextGoal"] and "一次" in body["state"]["nextGoal"]
    runtime.act("NEXT")
    assert TASK["expectedFirstTitle"] in runtime.snapshot["state"]["goal"]
    for label in ("点赞", "收藏", "取消点赞", "取消收藏"):
        runtime.act("NEXT")
        if runtime.cursor < len(runtime.plan["intents"]):
            assert label in runtime.snapshot["state"]["goal"]


def test_pending_submit_payload_only_asks_wait_and_readback(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    original = TerminalRecordedRuntime.act

    def loading(self, choice):
        if self.position == 2 and choice == "NEXT":
            return self.observe()
        return original(self, choice)

    monkeypatch.setattr(TerminalRecordedRuntime, "act", loading)
    runtime = DraftIntentRuntime("verify", derive_plan(semantic_raw()))
    runtime.observe()
    runtime.act("NEXT")
    runtime.act("NEXT")
    runtime.act("NEXT")
    body = payload(runtime.snapshot)
    assert "只等待" in body["state"]["goal"] and "不重复" in body["state"]["nextGoal"]
    assert set(runtime.actions) == {"WAIT", "BLOCKED"}


def test_pending_raw_intent_payload_only_requests_paired_readback(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    original = TerminalRecordedRuntime.act

    def loading(self, choice):
        if self.position == 1 and choice == "NEXT":
            return self.observe()
        return original(self, choice)

    monkeypatch.setattr(TerminalRecordedRuntime, "act", loading)
    runtime = DraftIntentRuntime("verify", derive_plan(semantic_raw()))
    runtime.observe()
    runtime.act("NEXT")
    runtime.act("NEXT")
    body = payload(runtime.snapshot)
    assert "只等待" in body["state"]["goal"] and "不重复动作" in body["state"]["nextGoal"]
    assert runtime.pending_intent == 1 and set(runtime.actions) == {"WAIT", "BLOCKED"}
