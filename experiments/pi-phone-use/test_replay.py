"""Read-only replay tests with injected model clients and a real PI offline smoke."""

import asyncio
import hashlib
import io
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

import replay_worker
import run_replay_pi


def snapshots():
    return [
        {
            "id": f"phone-{i}",
            "state": {"goal": "snapshot only", "index": i},
            "options": {"CLICK:n1": "a control", "WAIT": "wait"},
        }
        for i in range(6)
    ]


class FakeClient:
    instances = []

    def __init__(self, key, **kwargs):
        assert key == "offline-only-key"
        self.kwargs, self.calls, self.closed = kwargs, [], False
        self.instances.append(self)

    async def decide(self, provider, state, options):
        self.calls.append((provider, state, options))
        return {
            "choice": "WAIT",
            "latencyMs": 1,
            "servedModel": "offline-fixture",
            "usage": {},
            "cost": None,
            "network": {"newConnection": len(self.calls) == 1},
        }

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("mode", ["cold", "warm"])
def test_protocol_runs_six_identical_contexts_without_a_phone_and_preserves_first_sample(
    tmp_path, mode
):
    data = tmp_path / "snapshots.json"
    data.write_text(json.dumps(snapshots()))
    requests = [{"id": 0, "command": "observe"}]
    for i in range(6):
        requests += [
            {"id": i * 2 + 1, "command": "decide"},
            {"id": i * 2 + 2, "command": "act", "choice": "WAIT"},
        ]
    requests.append({"id": "finish", "command": "finish", "piElapsedMs": 123})
    output = io.StringIO()
    asyncio.run(
        replay_worker.serve(
            "jev",
            "trial",
            data,
            mode=mode,
            base=tmp_path,
            input_stream=io.StringIO("\n".join(map(json.dumps, requests))),
            output_stream=output,
            client_factory=FakeClient,
            key_reader=lambda: "offline-only-key",
        )
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == len(requests)
    assert responses[-2]["result"]["done"] is True
    result = json.loads((tmp_path / "trial/result.json").read_text())
    assert result["status"] == "replay_completed"
    assert result["replayOnly"] is True and result["phoneTaskSuccess"] is None
    assert result["piElapsedMs"] == 123
    assert len(result["modelCalls"]) == len(result["operations"]) == 6
    assert result["modelCalls"][0]["network"]["newConnection"] is True
    assert all(op["dispatchedToPhone"] is False for op in result["operations"])
    client = FakeClient.instances[-1]
    assert client.kwargs == {"mode": mode, "compact": False, "decisionStyle": "flat"}
    assert client.closed
    assert [call[1] for call in client.calls] == [
        {**s["state"], "replayOnly": True, "replayNote": replay_worker.REPLAY_NOTE}
        for s in snapshots()
    ]
    assert json.loads(data.read_text()) == snapshots()  # original recording never changed
    assert (tmp_path / "trial").stat().st_mode & 0o077 == 0
    assert (tmp_path / "trial/result.json").stat().st_mode & 0o077 == 0


def test_invalid_action_does_not_advance_or_dispatch_and_failure_is_saved(tmp_path):
    data = tmp_path / "snapshots.json"
    data.write_text(json.dumps(snapshots()))
    requests = [
        {"id": 1, "command": "observe"},
        {"id": 2, "command": "act", "choice": "CLICK:n1"},
        {"id": 3, "command": "finish"},
    ]
    output = io.StringIO()
    asyncio.run(
        replay_worker.serve(
            "jev",
            "failed",
            data,
            mode="cold",
            base=tmp_path,
            input_stream=io.StringIO("\n".join(map(json.dumps, requests))),
            output_stream=output,
            client_factory=FakeClient,
            key_reader=lambda: "offline-only-key",
        )
    )
    assert "error" in json.loads(output.getvalue().splitlines()[1])
    result = json.loads((tmp_path / "failed/result.json").read_text())
    assert result["status"] == "replay_failed" and result["operations"] == []


def test_replay_data_and_trial_ids_are_validated_before_credentials(tmp_path):
    invalid = tmp_path / "bad.json"
    invalid.write_text(json.dumps(snapshots()[:5]))
    with pytest.raises(ValueError):
        replay_worker.load_snapshots(invalid)
    with pytest.raises(ValueError):
        replay_worker.validate_trial_id("../outside")
    with pytest.raises(ValueError):
        replay_worker.validate_trial_id("..")


def test_key_reader_rejects_public_files_without_reading_them(tmp_path):
    key = tmp_path / "fake.key"
    key.write_text("fixture-only")
    key.chmod(0o644)
    with pytest.raises(PermissionError):
        replay_worker.read_key(key)
    key.chmod(0o600)
    assert replay_worker.read_key(key) == "fixture-only"


@pytest.mark.skipif(
    shutil.which(os.environ.get("PI_BIN", "pi")) is None, reason="PI is not installed"
)
def test_real_pi_replay_loop_uses_only_fake_decisions_and_advances_six_snapshots(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PHONE_BENCH_PYTHON", sys.executable)
    data = tmp_path / "snapshots.json"
    data.write_text(json.dumps(snapshots()))
    wrapper = tmp_path / "offline-worker.py"
    wrapper.write_text(
        "import asyncio,sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(Path(__file__).parent)!r})\n"
        "import replay_worker\n"
        "class Fake:\n"
        " def __init__(self,*args,**kwargs): pass\n"
        " async def decide(self,provider,state,options):\n"
        "  return {'choice':'WAIT','latencyMs':0,'servedModel':'offline','usage':{},'cost':0}\n"
        " async def close(self): pass\n"
        f"asyncio.run(replay_worker.cli(base=Path({str(tmp_path)!r}),client_factory=Fake,key_reader=lambda:'fixture-key'))\n"
    )
    result = run_replay_pi.launch(
        "warm",
        "offline-pi",
        data,
        base=tmp_path,
        worker_path=wrapper,
        extension_path=Path(__file__).parent / "pi-replay-extension.ts",
        timeout=20,
    )
    assert result["exitCode"] == 0
    assert result["replayOnly"] is True
    report = json.loads((tmp_path / "offline-pi/result.json").read_text())
    assert report["status"] == "replay_completed"
    assert len(report["modelCalls"]) == 6
    events = [json.loads(line) for line in Path(result["piEvents"]).read_text().splitlines()]
    tools = [event["toolName"] for event in events if event["type"] == "tool_execution_start"]
    assert tools == ["phone_observe"] + ["phone_action"] * 6
    assert "只读手机状态回放已完成；没有派发手机动作。" in Path(result["piEvents"]).read_text()
    assert (
        "手机控制任务已完成，最终状态已由控制器确认。" not in Path(result["piEvents"]).read_text()
    )
    meta = json.loads((tmp_path / "offline-pi-launch.json").read_text())
    assert meta["terminalAssistantTextIsSynthetic"] is True
    assert "adb" not in " ".join(meta["command"])
    assert "openrouter.key" not in " ".join(meta["command"])
    assert meta["replayDatasetSha256"] == hashlib.sha256(data.read_bytes()).hexdigest()


def test_replay_extension_is_exactly_the_frozen_extension_except_terminal_text():
    base = Path(__file__).parent
    original = (base / "pi-extension.ts").read_text()
    expected = original.replace(
        "手机控制任务已完成，最终状态已由控制器确认。", "只读手机状态回放已完成；没有派发手机动作。"
    )
    assert (base / "pi-replay-extension.ts").read_text() == expected


def test_cold_and_warm_payload_hashes_match_and_model_errors_are_saved_safely(tmp_path):
    data = tmp_path / "snapshots.json"
    data.write_text(json.dumps(snapshots()))

    async def scenario():
        hashes = []
        for mode in ["cold", "warm"]:
            runtime = replay_worker.ReplayRuntime(
                "jev",
                mode,
                snapshots(),
                mode=mode,
                base=tmp_path,
                client_factory=FakeClient,
                key_reader=lambda: "offline-only-key",
            )
            try:
                runtime.observe()
                for _ in range(6):
                    decision = await runtime.decide()
                    runtime.act(decision["choice"])
                result = runtime.finish(10)
                hashes.append([call["requestPayloadSha256"] for call in result["modelCalls"]])
            finally:
                await runtime.client.close()
        assert hashes[0] == hashes[1]

        class FailingClient(FakeClient):
            async def decide(self, *args):
                raise RuntimeError("offline-only-key should not appear")

        output = io.StringIO()
        await replay_worker.serve(
            "jev",
            "failed-model",
            data,
            mode="warm",
            base=tmp_path,
            input_stream=io.StringIO(
                "\n".join(
                    map(
                        json.dumps,
                        [
                            {"id": 1, "command": "observe"},
                            {"id": 2, "command": "decide"},
                            {"id": 3, "command": "finish"},
                        ],
                    )
                )
            ),
            output_stream=output,
            client_factory=FailingClient,
            key_reader=lambda: "offline-only-key",
        )
        assert "offline-only-key" not in output.getvalue()
        result = json.loads((tmp_path / "failed-model/result.json").read_text())
        assert result["status"] == "replay_failed" and len(result["modelCalls"]) == 1
        assert FakeClient.instances[-1].closed

    asyncio.run(scenario())


def test_replay_inherits_private_env_and_uses_separate_output_directory(tmp_path, monkeypatch):
    data = tmp_path / "snapshots.json"
    data.write_text(json.dumps(snapshots()))
    destination = tmp_path / "run-output"
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-secret-never-save")
    monkeypatch.setenv("PI_BIN", "fixture-pi")
    captured = {}

    class FakeProcess:
        pid = 123

        def __init__(self, command, **kwargs):
            captured.update(command=command, **kwargs)

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(run_replay_pi.subprocess, "Popen", FakeProcess)
    result = run_replay_pi.launch("cold", "fixture", data, base=destination)
    assert captured["command"][0] == "fixture-pi"
    assert captured["env"]["OPENROUTER_API_KEY"] == "fixture-secret-never-save"
    assert captured["env"]["PHONE_BENCH_OUTPUT_DIR"] == str(destination)
    assert "fixture-secret-never-save" not in json.dumps(result)
    assert "fixture-secret-never-save" not in (destination / "fixture-launch.json").read_text()


@pytest.mark.parametrize(
    "status,process_exit,expected",
    [
        ("replay_completed", 0, 0),
        ("replay_failed", 0, 1),
        ("replay_incomplete", 0, 1),
        ("replay_result_missing", 0, 1),
        ("replay_completed", 1, 1),
        (None, 0, 1),
    ],
)
def test_replay_cli_exit_requires_six_collected_responses(
    monkeypatch, status, process_exit, expected
):
    monkeypatch.setattr(
        run_replay_pi,
        "launch",
        lambda *a, **kw: {
            "exitCode": process_exit,
            "status": status,
        },
    )
    assert run_replay_pi.main(["warm", "offline-status", "--data", "fixture.json"]) == expected
