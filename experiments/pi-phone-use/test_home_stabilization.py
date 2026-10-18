"""Recorded entry waits read-only for this service's post-HOME transition."""

import json

import pytest

import controller
import terminal_runtime
from providers import _context, _jev_payload
from terminal_runtime import TerminalRecordedRuntime, TerminalRuntimeError
from test_terminal_runtime import Mcp, screen

SERVICE = "com.danielealbano.androidremotecontrolmcp.foss.debug"


def service_frame():
    # Sanitized shape of terminal-ccc4522c observation-1: the desktop exists
    # in the background while the MCP application still has sole focus.
    return screen(SERVICE) + "\n--- window:2 pkg:com.miui.home focused:false ---"


def make_runtime(tmp_path, monkeypatch, states, *, strategy="prepare-feed-v1"):
    mcp = Mcp(states)
    monkeypatch.setattr(controller, "BASE", tmp_path)
    monkeypatch.setattr(controller, "OwnMcp", lambda: mcp)
    monkeypatch.setattr(terminal_runtime.time, "sleep", lambda _: None)
    runtime = TerminalRecordedRuntime(
        "entry", {"kind": "recorded", "goal": "复用录制任务"}, entry_strategy=strategy
    )
    return runtime, mcp


def test_post_home_service_focus_waits_before_model_and_returns_latest_snapshot(
    tmp_path, monkeypatch
):
    runtime, mcp = make_runtime(
        tmp_path, monkeypatch, [service_frame(), service_frame(), screen("com.miui.home")]
    )
    observed = runtime.observe()
    assert observed is runtime.snapshot
    assert observed["state"]["focusedPackage"] == "com.miui.home"
    assert "OPEN_APP" in observed["options"]
    assert (
        set(observed["options"])
        == set(runtime.actions)
        == set(observed["state"]["actionDescriptors"])
    )
    body = _jev_payload(
        _context(observed["state"], observed["options"], True), observed["options"], True, "heads"
    )
    assert "OPEN_APP" in body["questions"]["operation"]["criteria"]
    assert runtime.index == 2 and runtime.progress.milestones == []
    assert [name for name, _ in mcp.calls] == [
        "android_get_screen_state",
        "android_press_key",
        "android_get_screen_state",
        "android_get_screen_state",
    ]
    assert mcp.calls[1][1] == {"key": "HOME"}
    assert (
        len([item for item in runtime.operations if item.get("name") == "focus_stabilization"]) == 1
    )
    assert len([item for item in runtime.phases if item.get("kind") == "wait"]) == 1
    assert json.loads((runtime.directory / "state-2.json").read_text()) == observed


def test_post_home_service_focus_budget_stops_without_model_or_more_mutations(
    tmp_path, monkeypatch
):
    runtime, mcp = make_runtime(tmp_path, monkeypatch, [service_frame()] * 5)
    with pytest.raises(TerminalRuntimeError) as caught:
        runtime.observe()
    assert caught.value.code == "HOME_TIMEOUT"
    assert runtime.actions == {} and runtime.snapshot["options"] == {}
    assert runtime.model_calls == [] and runtime.progress.milestones == []
    assert (
        len([item for item in runtime.operations if item.get("name") == "focus_stabilization"]) == 3
    )
    assert [name for name, _ in mcp.calls].count("android_get_screen_state") == 5
    assert [name for name, _ in mcp.calls if name != "android_get_screen_state"] == [
        "android_press_key"
    ]


@pytest.mark.parametrize(
    "strategy,package", [(None, SERVICE), ("prepare-feed-v1", "example.other")]
)
def test_other_foreground_and_legacy_entry_keep_existing_observation_behavior(
    tmp_path, monkeypatch, strategy, package
):
    runtime, mcp = make_runtime(tmp_path, monkeypatch, [screen(package)] * 2, strategy=strategy)
    observed = runtime.observe()
    assert observed is runtime.snapshot and observed["state"]["focusedPackage"] == package
    assert set(observed["options"]) == {"WAIT", "BLOCKED"}
    assert len(mcp.calls) == 3
    assert not any(item.get("name") == "focus_stabilization" for item in runtime.operations)


def test_real_worker_policy_receives_only_final_stable_preparation_candidates(
    tmp_path, monkeypatch
):
    import asyncio
    import io

    import worker
    from test_workflow_companion import paired
    from test_workflow_intents import supported_raw
    from workflow import compile_recording
    from workflow_intents import derive_plan

    compile_recording(
        paired(derive_plan(supported_raw())), tmp_path / "bundle", entry_strategy="prepare-feed-v1"
    )
    monkeypatch.setenv("PHONE_BENCH_RECIPE", str(tmp_path / "bundle/recipe.json"))
    monkeypatch.delenv("PHONE_TERMINAL_TASK", raising=False)
    runtime, mcp = make_runtime(
        tmp_path, monkeypatch, [service_frame(), service_frame(), screen("com.miui.home")]
    )
    calls = []

    class Client:
        mode, decisionStyle = "warm", "heads"

        async def decide(self, backend, state, options):
            calls.append((backend, state, options))
            assert state["focusedPackage"] == "com.miui.home"
            assert "OPEN_APP" in options
            return {"choice": "OPEN_APP", "latencyMs": 0, "usage": {}, "cost": 0}

        async def close(self):
            pass

    output = io.StringIO()
    asyncio.run(
        worker.serve(
            "skill-auto",
            "entry",
            base=tmp_path,
            runtime_factory=lambda _: runtime,
            client_factory=lambda *args, **kwargs: Client(),
            key_reader=lambda: "offline-not-a-secret",
            input_stream=io.StringIO('{"command":"observe"}\n{"command":"decide"}\n'),
            output_stream=output,
        )
    )
    replies = [json.loads(line)["result"] for line in output.getvalue().splitlines()]
    assert replies[0]["state"]["focusedPackage"] == calls[0][1]["focusedPackage"]
    assert set(replies[0]["options"]) == set(calls[0][2])
    assert replies[1]["choice"] == "OPEN_APP" and replies[1]["preparation"] is True
    assert len(calls) == 1 and calls[0][0] == "jev"
    assert not any(name == "android_open_app" for name, _ in mcp.calls)
    assert mcp.closed
