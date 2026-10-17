"""Runtime delivery evidence: a whole action group is not one native tool call."""

import json
import time

import pytest

import controller
from own_mcp import McpError


def stale():
    return McpError(
        "Node not found in accessibility tree", stage="tool_call", exception_type="ToolError"
    )


def action(key="old", method="type", *, bounds=None):
    return {
        "id": key,
        "method": method,
        "role": "navigation",
        "label": "搜索",
        "bounds": bounds or [180, 114, 740, 246],
        "center": [460, 180],
    }


def state(generation=1):
    return {
        "state": {
            "observation": {"kind": "search_input", "liked": None, "collected": None},
            "generation": generation,
        },
        "options": {"TYPE:old": "搜索"},
        "done": False,
    }


def runtime(monkeypatch, tmp_path, outcomes, *, method="type", rebind=False):
    value = controller.PhoneRuntime.__new__(controller.PhoneRuntime)
    value.started = time.perf_counter()
    value.directory = tmp_path
    value.operations = []
    value.phases = []
    value.progress = controller.Progress()
    value.stale_recoveries = value.direct_rebindings = 0
    value.index = 1
    value.snapshot = state()
    key = ("TYPE:" if method == "type" else "CLICK:") + "old"
    value.actions = {key: action(method=method)}
    calls = []
    results = iter(outcomes)

    class FakeMcp:
        def call(self, name, args):
            calls.append((name, args))
            outcome = next(results)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    value.mcp = FakeMcp()
    observed = []

    def observe():
        value.index += 1
        result = state(value.index)
        value.snapshot = result
        observed.append(result)
        new = action("new", method, bounds=[180, 114, 740, 246] if rebind else [180, 200, 740, 332])
        value.actions = {("TYPE:" if method == "type" else "CLICK:") + "new": new}
        return result

    value.observe = observe
    monkeypatch.setattr(controller.time, "sleep", lambda *args: None)
    return value, key, calls, observed


@pytest.mark.parametrize("method", ["type", "click"])
def test_safe_first_lookup_failure_marks_whole_group_not_dispatched_and_persists_snapshot(
    monkeypatch, tmp_path, method
):
    value, choice, calls, observed = runtime(monkeypatch, tmp_path, [stale()], method=method)
    result = value.act(choice)
    marker = {
        "status": "not_dispatched",
        "reason": "stale_reference",
        "choice": choice,
        "intent": {"method": method, "role": "navigation", "label": "搜索"},
    }
    assert result["execution"] == marker
    assert value.snapshot is result and observed[-1] is result
    assert json.loads((tmp_path / "state-2.json").read_text())["execution"] == marker
    audit = [item for item in value.operations if item.get("name") == "execution"]
    assert audit == [{"name": "execution", "stage": "search", **marker}]
    assert len(calls) == 1
    assert value.direct_rebindings == 0


@pytest.mark.parametrize("failing_tool", ["android_get_screen_state", "android_type_append_text"])
def test_clear_succeeded_then_later_stale_is_partial_and_stops_without_not_dispatched_marker(
    monkeypatch, tmp_path, failing_tool
):
    lookup = {
        "content": [
            {
                "type": "text",
                "text": "--- window:1 type:APPLICATION pkg:com.xingin.xhs focused:true ---\nnode_ab\tEditText\t-\t-\tsearch\t180,114,740,246\ton,ena,edt\n",
            }
        ]
    }
    error = stale()
    outcomes = [{}, error] if failing_tool == "android_get_screen_state" else [{}, lookup, error]
    value, choice, calls, observed = runtime(monkeypatch, tmp_path, outcomes)
    with pytest.raises(McpError) as failed:
        value.act(choice)
    assert failed.value is error
    assert calls[-1][0] == failing_tool
    assert not observed
    assert "execution" not in value.snapshot
    assert not any(item.get("name") == "execution" for item in value.operations)
    assert any(
        item.get("name") == "android_type_clear_text" and item.get("status") == "success"
        for item in value.operations
    )


@pytest.mark.parametrize(
    "error",
    [
        McpError("uncertain delivery", stage="tool_call", exception_type="ReadTimeout"),
        McpError(
            "Node not found in accessibility tree", stage="tool_call", exception_type="ReadTimeout"
        ),
        McpError(
            "Node not found in accessibility tree extra unsafe detail",
            stage="tool_call",
            exception_type="ToolError",
        ),
        RuntimeError("Node not found in accessibility tree"),
    ],
)
def test_uncertain_or_noncanonical_failure_never_claims_not_dispatched(
    monkeypatch, tmp_path, error
):
    value, choice, calls, observed = runtime(monkeypatch, tmp_path, [error])
    with pytest.raises(type(error)) as failed:
        value.act(choice)
    assert failed.value is error
    assert not observed
    assert "execution" not in value.snapshot
    assert not any(item.get("name") == "execution" for item in value.operations)
    assert len(calls) == 1


def test_direct_reference_rebind_applies_action_and_does_not_claim_not_dispatched(
    monkeypatch, tmp_path
):
    value, choice, calls, observed = runtime(
        monkeypatch, tmp_path, [stale(), {}], method="click", rebind=True
    )
    result = value.act(choice)
    assert [name for name, _ in calls] == ["android_click_node", "android_click_node"]
    assert value.direct_rebindings == 1
    assert len(observed) == 2
    assert "execution" not in result
    assert not any(item.get("name") == "execution" for item in value.operations)
