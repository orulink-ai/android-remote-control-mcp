"""Real PI -> extension -> worker -> policy, with device/network-free boundaries."""

import json
import os
from pathlib import Path
import shutil
import sys

import pytest

import run_pi_trial


pytestmark = pytest.mark.skipif(
    shutil.which(os.environ.get("PI_BIN", "pi")) is None,
    reason="PI is not installed",
)


def launch_fixture(tmp_path, monkeypatch, scenario):
    """Only model and phone boundaries are doubles; the IPC/policy path is real."""
    wrapper = tmp_path / "offline-policy-worker.py"
    wrapper.write_text(
        "import argparse,asyncio,json,sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parent)!r})\n"
        "import controller,httpx,own_mcp,worker\n"
        "from replay_worker import private_json\n"
        "from test_worker_policy import PhoneDouble,ModelDouble,api_failure\n"
        "def forbidden(*args,**kwargs):\n"
        " raise AssertionError('Offline integration test reached a real I/O boundary')\n"
        "httpx.Client=httpx.AsyncClient=forbidden\n"
        "controller.OwnMcp=own_mcp.OwnMcp=forbidden\n"
        f"base=Path({str(tmp_path)!r})\n"
        f"scenario={scenario!r}\n"
        "events=[]\n"
        "class FakeModel(ModelDouble):\n"
        " async def decide(self,backend,state,options):\n"
        "  events.append({'kind':'model','backend':backend,'generation':state['generation']})\n"
        "  return await super().decide(backend,state,options)\n"
        "class FakePhone(PhoneDouble):\n"
        " def __init__(self,trial_id):\n"
        "  super().__init__(trial_id)\n"
        "  (base/trial_id).mkdir(mode=0o700)\n"
        " def observe(self):\n"
        "  events.append({'kind':'observe','generation':self.index+1})\n"
        "  return super().observe()\n"
        " def act(self,choice):\n"
        "  events.append({'kind':'action','choice':choice})\n"
        "  return super().act(choice)\n"
        " def finish(self,extra=None):\n"
        "  result=super().finish(extra)\n"
        "  result.update(elapsedMs=1,milestones=[],fixtureTrace={\n"
        "   'events':events,'modelCalls':FakeModel.instances[0].calls,\n"
        "   'observeCount':self.index,'actions':self.actions})\n"
        "  private_json(base/args.trial_id/'result.json',result)\n"
        "  return result\n"
        "if scenario=='fallback': FakeModel.outcomes=[api_failure(),None]\n"
        "elif scenario=='both-fail': FakeModel.outcomes=[api_failure(),api_failure()]\n"
        "elif scenario=='action-timeout': FakePhone.fail_action=True\n"
        "parser=argparse.ArgumentParser()\n"
        "parser.add_argument('--provider',required=True)\n"
        "parser.add_argument('--trial-id',required=True)\n"
        "args=parser.parse_args()\n"
        "asyncio.run(worker.serve(args.provider,args.trial_id,base=base,\n"
        " runtime_factory=FakePhone,client_factory=FakeModel,\n"
        " key_reader=lambda:'fixture-secret-never-save'))\n"
    )
    monkeypatch.setenv("PHONE_BENCH_WORKER", str(wrapper))
    monkeypatch.setenv("PHONE_BENCH_PYTHON", sys.executable)
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-secret-never-save")
    monkeypatch.delenv("PHONE_BENCH_JEV_TIMEOUT", raising=False)
    monkeypatch.delenv("PHONE_BENCH_DEEPSEEK_TIMEOUT", raising=False)
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda serial: 0)
    metadata = run_pi_trial.launch("jev-auto", scenario, base=tmp_path, timeout=25)
    report = json.loads((tmp_path / scenario / "result.json").read_text())
    events = [json.loads(line) for line in Path(metadata["piEvents"]).read_text().splitlines()]
    tools = [event["toolName"] for event in events if event["type"] == "tool_execution_start"]
    for path in [
        tmp_path / f"{scenario}-launch.json",
        tmp_path / scenario / "result.json",
        Path(metadata["piEvents"]),
        Path(metadata["piSession"]),
    ]:
        assert "fixture-secret-never-save" not in path.read_text()
        assert "fixture-private-value" not in path.read_text()
    return metadata, report, tools


def test_real_pi_refreshes_after_jev_failure_before_deepseek_uses_new_node(tmp_path, monkeypatch):
    metadata, report, tools = launch_fixture(tmp_path, monkeypatch, "fallback")
    assert metadata["exitCode"] == 0 and metadata["status"] == "success"
    assert tools == ["phone_observe", "phone_observe", "phone_action"]
    assert report["fixtureTrace"] == {
        "events": [
            {"kind": "observe", "generation": 1},
            {"kind": "model", "backend": "jev", "generation": 1},
            {"kind": "observe", "generation": 2},
            {"kind": "model", "backend": "deepseek", "generation": 2},
            {"kind": "action", "choice": "CLICK:node-2"},
            {"kind": "observe", "generation": 3},
        ],
        "modelCalls": [["jev", 1], ["deepseek", 2]],
        "observeCount": 3,
        "actions": ["CLICK:node-2"],
    }
    assert report["decisionPolicy"]["fallbackSuccesses"] == 1
    assert report["piAgentLoop"] is True and report["piElapsedMs"] > 0


def test_real_pi_primary_success_has_one_explicit_observe_and_no_fallback(tmp_path, monkeypatch):
    metadata, report, tools = launch_fixture(tmp_path, monkeypatch, "primary")
    assert metadata["exitCode"] == 0 and metadata["status"] == "success"
    assert tools == ["phone_observe", "phone_action"]
    trace = report["fixtureTrace"]
    assert trace["modelCalls"] == [["jev", 1]]
    assert trace["actions"] == ["CLICK:node-1"]
    assert trace["observeCount"] == 2  # initial PI read plus the action's readback
    assert report["decisionPolicy"]["fallbackRequests"] == 0


def test_real_pi_uncertain_action_reads_once_then_fails_without_fallback_or_replay(
    tmp_path, monkeypatch
):
    metadata, report, tools = launch_fixture(tmp_path, monkeypatch, "action-timeout")
    assert metadata["status"] == report["status"] == "failed"
    assert tools == ["phone_observe", "phone_action"]
    trace = report["fixtureTrace"]
    assert trace["modelCalls"] == [["jev", 1]]
    assert trace["actions"] == ["CLICK:node-1"]
    assert trace["observeCount"] == 2
    assert report["decisionPolicy"]["fallbackRequests"] == 0
    recovery = [op for op in report["operations"] if op["name"] == "action_reconciliation"]
    assert len(recovery) == 1 and recovery[0]["replayed"] is False


def test_real_pi_two_model_failures_are_bounded_and_dispatch_no_action(tmp_path, monkeypatch):
    metadata, report, tools = launch_fixture(tmp_path, monkeypatch, "both-fail")
    assert metadata["status"] == report["status"] == "failed"
    assert tools == ["phone_observe", "phone_observe"]
    trace = report["fixtureTrace"]
    assert trace["modelCalls"] == [["jev", 1], ["deepseek", 2]]
    assert trace["actions"] == [] and trace["observeCount"] == 2
    assert len(report["decisionPolicy"]["attempts"]) == 2
    assert report["decisionPolicy"]["fallbackRequests"] == 1
