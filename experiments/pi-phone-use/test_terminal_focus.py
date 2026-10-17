"""Window transitions permit bounded reads, never speculative phone actions."""

import pytest

from controller import PhoneRuntime, Progress
from terminal_runtime import TerminalRecordedRuntime, TerminalRuntimeError
from test_terminal_runtime import Mcp, node, runtime, screen, text
from window_state import verified_launcher


def transient():
    return screen().replace("focused:true", "focused:false")


def test_generic_unknown_focus_stabilizes_with_only_three_reads(tmp_path, monkeypatch):
    monkeypatch.setattr("terminal_runtime.time.sleep", lambda _: None)
    mcp = Mcp([transient(), transient(), screen()])
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开小红书", "appName": "小红书"}, mcp)
    observed = phone.observe()
    assert "OPEN_APP:com.xingin.xhs" in observed["options"]
    assert [name for name, _ in mcp.calls][:3] == ["android_get_screen_state"] * 3
    assert sum(item.get("name") == "focus_stabilization" for item in phone.operations) == 2


def test_persistent_unknown_focus_stops_after_three_read_only_retries(tmp_path, monkeypatch):
    monkeypatch.setattr("terminal_runtime.time.sleep", lambda _: None)
    mcp = Mcp([transient()] * 4)
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开小红书", "appName": "小红书"}, mcp)
    with pytest.raises(TerminalRuntimeError) as caught:
        phone.observe()
    assert caught.value.code == "UNKNOWN_FOCUS"
    assert [name for name, _ in mcp.calls] == ["android_get_screen_state"] * 4


def test_search_open_transition_reads_until_real_focused_results(tmp_path, monkeypatch):
    monkeypatch.setattr("terminal_runtime.time.sleep", lambda _: None)
    result_nodes = node("node_aa", "猫咪", klass="EditText", bounds="180,80,760,190")
    result_nodes += "\n" + node("node_bb", "综合", flags="on,ena")
    mcp = Mcp([screen(), transient(), screen("com.xingin.xhs", result_nodes)])
    phone = runtime(tmp_path, {"kind": "search", "goal": "搜索猫咪", "query": "猫咪"}, mcp)
    phone.observe()
    assert phone.act("OPEN_APP:com.xingin.xhs")["done"] is True
    assert sum(name == "android_open_app" for name, _ in mcp.calls) == 1


def test_recorded_transient_observations_cannot_advance_common_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("terminal_runtime.time.sleep", lambda _: None)
    calls = []
    initial = iter([transient(), screen()])

    def initialize(self, trial):
        self.directory, self.index = tmp_path, 0
        self.progress = Progress()
        self.actions = {}
        self.operations, self.phases = [], []

    def call(self, name, arguments):
        calls.append(name)
        return text(next(initial)) if name == "android_get_screen_state" else text("ok")

    def observe(self):
        self.index += 1
        unknown = self.index == 1
        # Deliberately buggy/misleading common observation: recovery must roll
        # this back when no window is actually focused.
        if unknown:
            self.progress.phase = "done"
            self.progress.milestones.append({"stage": "search", "next": "done"})
        raw = transient() if unknown else screen("com.miui.personalassistant")
        (tmp_path / f"observation-{self.index}.txt").write_text(raw)
        self.snapshot = {"state": {"goal": "fixed", "stage": self.progress.phase,
            "observation": {"kind": "launcher"}, "actionDescriptors": {}},
            "options": {}, "done": self.progress.phase == "done"}
        return self.snapshot

    monkeypatch.setattr(PhoneRuntime, "__init__", initialize)
    monkeypatch.setattr(PhoneRuntime, "call", call)
    monkeypatch.setattr(PhoneRuntime, "observe", observe)
    phone = TerminalRecordedRuntime("recorded", {"kind": "recorded", "goal": "复用录制任务"})
    observed = phone.observe()
    assert not observed["done"] and phone.progress.phase == "search"
    assert phone.progress.milestones == []
    assert calls[:3] == ["android_get_screen_state", "android_get_screen_state", "android_press_key"]
    assert phone.index == 2


def test_redmi_personalassistant_is_launcher_only_when_sole_focused():
    observed = screen("com.miui.personalassistant")
    assert verified_launcher(observed) == "com.miui.personalassistant"
    assert verified_launcher(observed.replace("focused:true", "focused:false")) is None
    ambiguous = observed + "\n--- window:2 pkg:com.xingin.xhs focused:true ---"
    assert verified_launcher(ambiguous) is None
