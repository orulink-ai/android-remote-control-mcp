"""TDD for PI's skill bridge: policy decisions never become invented API calls."""

import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from decision_policy import DecisionPolicyError
from controller import QUERY, TITLE
import record_pi_trial
import run_pi_trial
from test_worker_policy import ModelDouble, PhoneDouble
import worker


class SkillDouble:
    instances = []
    reject_verification = False
    reject_completion = False
    reject_decision = False

    def __init__(self, client, mode, **kwargs):
        self.instances.append(self)
        self.client, self.mode = client, mode
        self.attempts = []
        self.hits = 0
        self.verified, self.completed = [], []

    async def decide(self, snapshot, observation):
        if self.reject_decision:
            raise DecisionPolicyError("recipe mismatch")
        self.hits += 1
        return {
            "kind": "decision",
            "choice": next(iter(snapshot["options"])),
            "selectedBackend": "skill",
            "latencyMs": 0.2,
            "modelRequest": False,
        }

    def summary(self):
        return {
            "mode": "skill",
            "pendingRefresh": False,
            "recipeHits": self.hits,
            "modelFallbacks": 0,
            "attempts": self.attempts,
        }

    def verify(self, snapshot):
        self.verified.append(snapshot)
        if self.reject_verification:
            raise DecisionPolicyError("postcondition failed")

    def verify_complete(self, snapshot):
        self.completed.append(snapshot)
        if self.reject_completion:
            raise DecisionPolicyError("final postcondition failed")


@pytest.fixture
def skill_module(monkeypatch):
    SkillDouble.instances = []
    PhoneDouble.instances = []
    ModelDouble.instances = []
    for field in ("reject_verification", "reject_completion", "reject_decision"):
        monkeypatch.setattr(SkillDouble, field, False)
    for field in ("fail_action", "fail_readback", "parse_after_action"):
        monkeypatch.setattr(PhoneDouble, field, False)
    monkeypatch.setattr(ModelDouble, "outcomes", [])
    loaded = []
    module = SimpleNamespace(
        SkillPolicy=SkillDouble,
        RecordingPhoneRuntime=PhoneDouble,
        load_recipe=lambda path: (
            loaded.append(Path(path)) or {"task": {"query": QUERY, "expectedFirstTitle": TITLE}}
        ),
    )
    monkeypatch.setitem(sys.modules, "workflow", module)
    monkeypatch.setenv("PHONE_BENCH_RECIPE", "/fixture/recipe.json")
    return module, loaded


async def drive(tmp_path, requests, **kwargs):
    output = io.StringIO()
    await worker.serve(
        "skill",
        "fixture-skill",
        base=tmp_path,
        input_stream=io.StringIO(
            "\n".join(json.dumps({"id": i, **r}) for i, r in enumerate(requests))
        ),
        output_stream=output,
        runtime_factory=PhoneDouble,
        client_factory=ModelDouble,
        key_reader=lambda: "fixture-private-key",
        **kwargs,
    )
    return [json.loads(line) for line in output.getvalue().splitlines()]


REQUESTS = [
    {"command": "observe"},
    {"command": "decide"},
    {"command": "act", "choice": "CLICK:node-1"},
    {"command": "finish", "piElapsedMs": 10},
]


def test_skill_hit_stays_in_pi_action_protocol_and_is_not_a_model_request(tmp_path, skill_module):
    replies = asyncio.run(drive(tmp_path, REQUESTS))
    phone, policy = PhoneDouble.instances[0], SkillDouble.instances[0]
    assert replies[1]["result"]["modelRequest"] is False
    assert phone.actions == ["CLICK:node-1"]
    assert ModelDouble.instances == [] and phone.model_calls == []
    assert not any(p["kind"] == "model" for p in phone.phases)
    assert len(policy.verified) == 1 and len(policy.completed) == 1
    assert phone.result["status"] == "success" and phone.result["piAgentLoop"] is True
    assert phone.result["recipeExecution"]["recipeHits"] == 1
    assert phone.result["recipeExecution"]["modelFallbacks"] == 0
    assert policy.client.closed and phone.closed


def test_local_skill_needs_no_api_key_or_http_client(tmp_path, skill_module, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_JEV_TIMEOUT", "unused-in-local-mode")
    monkeypatch.setenv("PHONE_BENCH_DEEPSEEK_TIMEOUT", "unused-in-local-mode")
    output = io.StringIO()
    asyncio.run(
        worker.serve(
            "skill",
            "no-api-key",
            base=tmp_path,
            input_stream=io.StringIO("\n".join(json.dumps(r) for r in REQUESTS)),
            output_stream=output,
            runtime_factory=PhoneDouble,
            key_reader=lambda: pytest.fail("local recipe must not read API credentials"),
            client_factory=lambda *a, **kw: pytest.fail(
                "local recipe must not construct HTTP clients"
            ),
        )
    )
    assert PhoneDouble.instances[0].result["status"] == "success"


@pytest.mark.parametrize("failure", ["reject_verification", "reject_completion"])
def test_final_done_cannot_override_skill_postcondition_failure(
    tmp_path, monkeypatch, skill_module, failure
):
    monkeypatch.setattr(SkillDouble, failure, True)
    replies = asyncio.run(drive(tmp_path, REQUESTS))
    phone = PhoneDouble.instances[0]
    assert phone.completed is True
    assert phone.result["status"] == "failed"
    assert phone.actions == ["CLICK:node-1"]
    assert any("error" in reply for reply in replies)
    assert not any(op["name"] == "action_reconciliation" for op in phone.operations)


def test_recipe_mismatch_stops_without_dispatch_or_model_request(
    tmp_path, monkeypatch, skill_module
):
    monkeypatch.setattr(SkillDouble, "reject_decision", True)
    replies = asyncio.run(drive(tmp_path, REQUESTS))
    phone = PhoneDouble.instances[0]
    assert "error" in replies[1]
    assert phone.actions == [] and phone.model_calls == []
    assert phone.result["status"] == "failed"


def test_invalid_recipe_rejected_before_model_credentials_or_phone(
    tmp_path, monkeypatch, skill_module
):
    module, _ = skill_module
    module.load_recipe = lambda path: (_ for _ in ()).throw(ValueError("invalid recipe"))
    with pytest.raises(ValueError):
        asyncio.run(
            worker.serve(
                "skill",
                "fixture",
                base=tmp_path,
                input_stream=io.StringIO(""),
                key_reader=lambda: pytest.fail("key must not be read"),
            )
        )
    assert PhoneDouble.instances == []


def test_skill_launch_prevalidates_recipe_before_device_reset(tmp_path, monkeypatch, skill_module):
    module, _ = skill_module
    module.load_recipe = lambda path: (_ for _ in ()).throw(ValueError("invalid recipe"))
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: pytest.fail("phone touched"))
    with pytest.raises(ValueError):
        run_pi_trial.launch("skill", "invalid-skill", base=tmp_path)


@pytest.mark.parametrize("field", ["query", "expectedFirstTitle"])
def test_unsupported_recipe_task_never_resets_phone_or_starts_recording(
    tmp_path, monkeypatch, skill_module, field
):
    module, _ = skill_module
    task = {"query": QUERY, "expectedFirstTitle": TITLE, field: "not-supported-by-adapter"}
    module.load_recipe = lambda path: {"task": task}
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: pytest.fail("phone touched"))
    monkeypatch.setattr(record_pi_trial, "BASE", tmp_path)
    monkeypatch.setattr(
        record_pi_trial, "start_recording", lambda *a: pytest.fail("screenrecord started")
    )
    with pytest.raises(ValueError, match="unsupported"):
        run_pi_trial.launch("skill", "invalid-task", base=tmp_path)
    with pytest.raises(ValueError, match="unsupported"):
        record_pi_trial.record_trial("skill", "video-invalid-task", report_dir=tmp_path)


def test_skill_launch_uses_real_pi_provider_and_private_recipe_env(
    tmp_path, monkeypatch, skill_module
):
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: 0)
    observed = {}

    class Process:
        pid = 1

        def __init__(self, command, **kwargs):
            observed.update(command=command, **kwargs)

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(run_pi_trial.subprocess, "Popen", Process)
    result = run_pi_trial.launch("skill", "skill-launch", base=tmp_path)
    assert observed["command"][observed["command"].index("--model") + 1] == "skill"
    assert observed["env"]["PHONE_BENCH_RECIPE"] == "/fixture/recipe.json"
    assert result["decisionPolicyDetails"] == "recorded-skill-stop-on-drift"
    assert "decisionTimeoutsSeconds" not in result
    assert "/fixture/recipe.json" not in json.dumps(result)
    assert skill_module[1]


def test_skill_recording_prevalidates_before_screenrecord(tmp_path, monkeypatch, skill_module):
    module, _ = skill_module
    module.load_recipe = lambda path: (_ for _ in ()).throw(ValueError("invalid recipe"))
    monkeypatch.setattr(record_pi_trial, "BASE", tmp_path)
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setattr(
        record_pi_trial, "start_recording", lambda *a: pytest.fail("recording started")
    )
    with pytest.raises(ValueError):
        record_pi_trial.record_trial("skill", "video-skill", report_dir=tmp_path)


def test_skill_recording_keeps_unedited_full_flow_under_its_own_name(
    tmp_path, monkeypatch, skill_module
):
    from test_record_pi_trial import recording

    monkeypatch.setattr(record_pi_trial, "BASE", tmp_path)
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setattr(record_pi_trial, "start_recording", lambda *a: recording())
    monkeypatch.setattr(
        record_pi_trial,
        "run_trial",
        lambda *a, **kw: {"exitCode": 0, "wallMs": 10, "status": "success"},
    )
    monkeypatch.setattr(record_pi_trial, "stop_recording", lambda *a: {"status": "sent_int"})
    monkeypatch.setattr(
        record_pi_trial,
        "pull_recording",
        lambda serial, remote, target: target.write_bytes(b"offline-fixture"),
    )
    monkeypatch.setattr(
        record_pi_trial,
        "probe_video",
        lambda *a: {
            "valid": True,
            "durationSeconds": 1,
            "width": 720,
            "height": 1560,
            "codec": "h264",
        },
    )
    monkeypatch.setattr(record_pi_trial, "adb", lambda *a, **kw: None)
    result = record_pi_trial.record_trial("skill", "video-skill", report_dir=tmp_path)
    assert Path(result["videoPath"]).name == "skill-full-flow.mp4"
    assert result["status"] == "recorded"
    assert result["edited"] is False and result["trimmed"] is False
    assert result["playbackRate"] == 1 and result["fullFlowCovered"] is True


def test_recording_flag_selects_recording_runtime_only_for_explicit_default(
    tmp_path, monkeypatch, skill_module
):
    monkeypatch.setenv("PHONE_BENCH_RECORDING", "1")
    monkeypatch.setattr(
        worker, "PhoneRuntime", lambda *a: pytest.fail("unexpected real phone runtime")
    )
    output = io.StringIO()
    asyncio.run(
        worker.serve(
            "jev",
            "recorded-demo",
            base=tmp_path,
            input_stream=io.StringIO('{"command":"observe"}\n{"command":"finish"}'),
            output_stream=output,
            client_factory=ModelDouble,
            key_reader=lambda: "fixture-key",
        )
    )
    assert len(PhoneDouble.instances) == 1 and PhoneDouble.instances[0].closed


def test_cli_accepts_skill_without_changing_single_model_modes(monkeypatch):
    selected = []
    monkeypatch.setattr(
        run_pi_trial,
        "launch",
        lambda p, t: selected.append(p) or {"exitCode": 0, "status": "success"},
    )
    assert run_pi_trial.main(["skill", "fixture-skill"]) == 0
    assert selected == ["skill"]


@pytest.mark.skipif(
    shutil.which(os.environ.get("PI_BIN", "pi")) is None, reason="PI is not installed"
)
@pytest.mark.parametrize("reject_completion", [False, True])
def test_real_pi_runs_skill_tools_and_rejects_unverified_final_done(
    tmp_path, monkeypatch, skill_module, reject_completion
):
    """Real PI, shared extension and actual worker; no phone or network I/O."""
    wrapper = tmp_path / "offline-skill-worker.py"
    wrapper.write_text(
        "import argparse,asyncio,json,sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parent)!r})\n"
        "import controller,httpx,own_mcp,worker\n"
        "from replay_worker import private_json\n"
        "from test_skill_bridge import SkillDouble\n"
        "from test_worker_policy import PhoneDouble,ModelDouble\n"
        "def forbidden(*args,**kwargs):\n"
        " raise AssertionError('Offline skill bridge reached real I/O')\n"
        "httpx.Client=httpx.AsyncClient=forbidden\n"
        "controller.OwnMcp=own_mcp.OwnMcp=forbidden\n"
        f"base=Path({str(tmp_path)!r})\n"
        f"SkillDouble.reject_completion={reject_completion!r}\n"
        "sys.modules['workflow']=SimpleNamespace(SkillPolicy=SkillDouble,load_recipe=lambda path:{})\n"
        "class FakePhone(PhoneDouble):\n"
        " def __init__(self,trial_id):\n"
        "  super().__init__(trial_id)\n"
        "  (base/trial_id).mkdir(mode=0o700)\n"
        " def finish(self,extra=None):\n"
        "  result=super().finish(extra)\n"
        "  result.update(elapsedMs=1,milestones=[],actions=self.actions)\n"
        "  private_json(base/args.trial_id/'result.json',result)\n"
        "  return result\n"
        "parser=argparse.ArgumentParser()\n"
        "parser.add_argument('--provider',required=True)\n"
        "parser.add_argument('--trial-id',required=True)\n"
        "args=parser.parse_args()\n"
        "asyncio.run(worker.serve(args.provider,args.trial_id,base=base,\n"
        " runtime_factory=FakePhone,client_factory=ModelDouble,key_reader=lambda:'fixture-secret'))\n"
    )
    monkeypatch.setenv("PHONE_BENCH_WORKER", str(wrapper))
    monkeypatch.setenv("PHONE_BENCH_PYTHON", sys.executable)
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: 0)
    ident = "real-pi-skill-final-failed" if reject_completion else "real-pi-skill"
    metadata = run_pi_trial.launch("skill", ident, base=tmp_path, timeout=25)
    result = json.loads((tmp_path / ident / "result.json").read_text())
    events = [json.loads(line) for line in Path(metadata["piEvents"]).read_text().splitlines()]
    tools = [event["toolName"] for event in events if event["type"] == "tool_execution_start"]
    assert tools == ["phone_observe", "phone_action"]
    assert result["actions"] == ["CLICK:node-1"]
    assert result["modelCalls"] == []
    assert result["recipeExecution"]["recipeHits"] == 1
    assert result["piAgentLoop"] is True
    assert result["status"] == ("failed" if reject_completion else "success")
    for path in [
        tmp_path / ident / "result.json",
        Path(metadata["piSession"]),
        Path(metadata["piEvents"]),
    ]:
        assert "fixture-secret" not in path.read_text()
