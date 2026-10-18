"""Production workflow jobs launch the existing PI terminal entry over configured MCP."""

from pathlib import Path
import signal

import pytest

import workflow_runner as runner


def test_pi_command_uses_terminal_profile_without_legacy_reset_or_usb(tmp_path, monkeypatch):
    captured = {}

    def terminal(profile, args, environment):
        captured.update(profile=profile, args=args, env=environment)
        return ["real-pi", "--provider", "phone-control", "--model", "auto", *args], dict(
            environment
        )

    monkeypatch.setattr(runner, "build_launch", terminal)
    profile = {"python": "/absolute/python"}
    plan = tmp_path / "intent-plan.json"
    plan.write_text("{}")
    command, env = runner.phase_launch(
        profile,
        "verify",
        tmp_path,
        recording=True,
        recipe=None,
        plan=plan,
        environment={"ADB": "forbidden", "PHONE_BENCH_DEVICE": "forbidden"},
    )
    assert command[0] == "real-pi" and "phone-control" in command
    assert all("run_pi_trial.py" not in arg for arg in command)
    assert env["PHONE_WORKFLOW_PLAN"] == str(plan)
    assert Path(env["PHONE_BENCH_WORKER"]).name == "workflow_verification.py"
    assert "ADB" not in env and "PHONE_BENCH_DEVICE" not in env
    assert captured["args"][-1] == "执行录制的小红书完整闭环"


def test_replay_uses_generated_recipe_in_same_terminal_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "build_launch", lambda profile, args, env: (["pi", *args], dict(env))
    )
    recipe = tmp_path / "generated.json"
    command, env = runner.phase_launch(
        {"python": "/absolute/python"},
        "execute",
        tmp_path,
        recording=False,
        recipe=recipe,
        plan=None,
        environment={},
    )
    assert env["PHONE_BENCH_RECIPE"] == str(recipe)
    assert "PHONE_WORKFLOW_PLAN" not in env
    assert "PHONE_BENCH_WORKER" not in env


def test_heartbeat_failure_terminates_only_owned_pi_group(tmp_path, monkeypatch):
    class Process:
        pid = 424242
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.returncode = -15
            return -15

    process = Process()
    signals = []
    monkeypatch.setattr(runner, "load_profile", lambda: {"python": "/absolute/python"})
    monkeypatch.setattr(runner, "phase_launch", lambda *a, **kw: (["real-pi"], {}))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: process)

    def killpg(pid, sig):
        if sig == 0:
            raise ProcessLookupError()
        signals.append((pid, sig))

    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    ticks = iter([0, 11, 11])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(ticks))
    beats = []

    def heartbeat():
        beats.append(True)
        if len(beats) == 2:
            raise RuntimeError("Lease expired")

    with pytest.raises(RuntimeError):
        runner.run_pi(
            "skill-auto",
            "execute",
            tmp_path,
            recording=False,
            recipe=tmp_path / "recipe.json",
            heartbeat=heartbeat,
        )
    assert signals == [(process.pid, signal.SIGTERM)]


def test_top_level_sigterm_interrupts_owned_run_and_restores_handler(monkeypatch):
    def previous(*_):
        return None

    handlers = []
    monkeypatch.setattr(runner.signal, "getsignal", lambda _: previous)
    monkeypatch.setattr(
        runner.signal, "signal", lambda sig, handler: handlers.append((sig, handler))
    )
    with pytest.raises(runner.WorkflowStopped):
        with runner.termination_scope():
            handlers[-1][1](signal.SIGTERM, None)
    assert handlers[-1] == (signal.SIGTERM, previous)


def test_explicit_profile_is_used_by_runner_not_silently_defaulted(tmp_path, monkeypatch):
    class Finished:
        pid = 424242
        returncode = 0

        def poll(self):
            return 0

    profile = {"python": "/expected/python"}

    def launch(actual, phase, directory, **kw):
        assert actual is profile
        result = directory / "pi/terminal-001/result.json"
        result.parent.mkdir(parents=True)
        result.write_text('{"status":"success"}')
        return ["real-pi"], {}

    monkeypatch.setattr(runner, "load_profile", lambda: pytest.fail("Default profile was used"))
    monkeypatch.setattr(runner, "phase_launch", launch)
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **kw: Finished())

    def absent_group(*_):
        raise ProcessLookupError()

    monkeypatch.setattr(runner.os, "killpg", absent_group)
    assert (
        runner.run_pi(
            "skill-auto",
            "execute",
            tmp_path,
            recording=False,
            recipe=tmp_path / "generated.json",
            heartbeat=lambda: None,
            profile=profile,
        )["status"]
        == "success"
    )


def test_exited_pi_parent_does_not_leave_its_worker_group_alive(monkeypatch):
    class ExitedParent:
        pid = 424242
        returncode = -15

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            return self.returncode

    signals = []
    live = True

    def killpg(pid, sig):
        nonlocal live
        assert pid == ExitedParent.pid
        if not live:
            raise ProcessLookupError()
        if sig:
            signals.append(sig)
            if sig == signal.SIGKILL:
                live = False

    times = iter([0, 6])
    monkeypatch.setattr(runner.os, "killpg", killpg)
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    runner.stop_owned_group(ExitedParent())
    assert signals == [signal.SIGTERM, signal.SIGKILL] and not live


def test_feedback_mcp_matches_android_jsonrpc_and_forbids_phone_actions():
    import asyncio
    import httpx

    recorded = []

    async def handle(request):
        import json

        body = json.loads(request.content) if request.method != "DELETE" else {}
        recorded.append((request.method, body))
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json={"result": {"serverInfo": {"name": "offline"}}},
                headers={"mcp-session-id": "offline"},
            )
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if body.get("method") == "tools/call":
            assert request.headers["Mcp-Session-Id"] == "offline"
            return httpx.Response(
                200,
                json={
                    "result": {
                        "content": [
                            {"type": "text", "text": '{"id":"job-001","status":"succeeded"}'}
                        ]
                    }
                },
            )
        return httpx.Response(204)

    async def exercise():
        client = runner.FeedbackMcp("http://offline.invalid/mcp", {}, timeout=2)
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=2)
        await client.initialize()
        result = await client.call("android_workflow_update", {"job_id": "job-001"})
        assert result["content"][0]["text"].startswith('{"id":')
        with pytest.raises(runner.WorkflowRunError):
            await client.call("android_click_node", {})
        await client.close()
        assert client.client.is_closed

    asyncio.run(exercise())
    assert [
        body["params"]["name"] for _, body in recorded if body.get("method") == "tools/call"
    ] == ["android_workflow_update"]


@pytest.mark.parametrize(
    "body", [{"error": {"code": -32603}}, {"result": {"isError": True, "content": []}}]
)
def test_feedback_mcp_rejects_real_protocol_and_tool_errors(body):
    import asyncio
    import httpx

    async def exercise():
        client = runner.FeedbackMcp("http://offline.invalid/mcp", {}, timeout=2)
        await client.client.aclose()
        client.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
        )
        with pytest.raises(runner.WorkflowRunError):
            await client.call("android_workflow_update", {"job_id": "job-001"})
        await client.abort()

    asyncio.run(exercise())


def test_feedback_factory_initialization_is_cancellable_and_closes_transport(tmp_path, monkeypatch):
    import asyncio
    import json

    closed = []

    class Initializing:
        async def initialize(self):
            await asyncio.sleep(10)

        async def abort(self):
            closed.append(True)

    monkeypatch.setattr(runner, "FeedbackMcp", lambda *a, **kw: Initializing())
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"phone": {"url": "http://offline.invalid/mcp"}}}))

    async def exercise():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await runner.feedback_mcp(
                    {"mcpConfig": str(config), "mcpServer": "phone"}, timeout=2
                )

    asyncio.run(exercise())
    assert closed == [True]
