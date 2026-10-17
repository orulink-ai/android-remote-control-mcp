"""PI must explicitly re-decide after a guaranteed pre-dispatch reference miss."""

import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from controller import QUERY, TITLE
from decision_policy import DecisionPolicyError
import run_pi_trial
from test_skill_bridge import SkillDouble
from test_worker_policy import PhoneDouble
import worker

INTENT = {"method": "click", "role": "navigation", "label": "like the target"}


class RefreshPhone(PhoneDouble):
    scenario = "not-dispatched"

    def __init__(self, trial_id):
        super().__init__(trial_id)
        self.attempted = []

    def act(self, choice):
        self.attempted.append(choice)
        if len(self.attempted) != 1:
            return super().act(choice)
        fresh = self.observe()
        marker = {
            "status": "not_dispatched",
            "reason": "stale_reference",
            "choice": choice,
            "intent": dict(INTENT),
        }
        if self.scenario == "partial":
            marker["status"] = "partial"
        elif self.scenario == "unknown":
            marker["status"] = "unknown"
        elif self.scenario == "wrong-choice":
            marker["choice"] = "CLICK:unapproved"
        elif self.scenario == "partial-forged":
            marker["partial"] = True
        fresh["execution"] = marker
        return fresh


class RefreshPolicy(SkillDouble):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reference_refreshes = []

    def not_dispatched(self, snapshot, choice):
        marker = snapshot.get("execution", {})
        if marker != {
            "status": "not_dispatched",
            "reason": "stale_reference",
            "choice": choice,
            "intent": INTENT,
        }:
            raise DecisionPolicyError("Not a verified pre-dispatch miss")
        self.reference_refreshes.append(choice)

    def verify(self, snapshot):
        if snapshot.get("done") is not True:
            raise DecisionPolicyError("Postcondition not met")
        super().verify(snapshot)

    def summary(self):
        return {**super().summary(), "referenceRefreshes": len(self.reference_refreshes)}


@pytest.mark.parametrize("status", ["partial", "unknown", "timeout", "maybe", None])
def test_worker_rejects_uncertain_delivery_even_when_postcondition_verifier_accepts(
    tmp_path, monkeypatch, refresh_module, status
):
    # Deliberately permissive verifier: only the worker's protocol guard can
    # prevent a coincidentally correct final state from masking partial delivery.
    sys.modules["workflow"].SkillPolicy = SkillDouble
    SkillDouble.instances = []

    class UncertainPhone(PhoneDouble):
        def act(self, choice):
            value = super().act(choice)
            value["execution"] = {"status": status}
            return value

    UncertainPhone.instances = []
    output = io.StringIO()
    requests = [
        {"command": "observe"},
        {"command": "decide"},
        {"command": "act", "choice": "CLICK:node-1"},
        {"command": "finish"},
    ]
    asyncio.run(
        worker.serve(
            "skill",
            "uncertain-state-correct",
            base=tmp_path,
            input_stream=io.StringIO("\n".join(json.dumps(r) for r in requests)),
            output_stream=output,
            runtime_factory=UncertainPhone,
        )
    )
    phone, policy = UncertainPhone.instances[0], SkillDouble.instances[0]
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert phone.completed is True and phone.actions == ["CLICK:node-1"]
    assert "error" in replies[2]
    assert phone.result["status"] == "failed" and phone.result["modelCalls"] == []
    assert policy.verified == []


def test_explicit_applied_delivery_still_uses_normal_verification(tmp_path, refresh_module):
    class AppliedPhone(PhoneDouble):
        def act(self, choice):
            value = super().act(choice)
            value["execution"] = {"status": "applied"}
            return value

    AppliedPhone.instances = []
    output = io.StringIO()
    requests = [
        {"command": "observe"},
        {"command": "decide"},
        {"command": "act", "choice": "CLICK:node-1"},
        {"command": "finish"},
    ]
    asyncio.run(
        worker.serve(
            "skill",
            "applied-normal",
            base=tmp_path,
            input_stream=io.StringIO("\n".join(json.dumps(r) for r in requests)),
            output_stream=output,
            runtime_factory=AppliedPhone,
        )
    )
    assert AppliedPhone.instances[0].result["status"] == "success"
    assert len(RefreshPolicy.instances[0].verified) == 1


@pytest.fixture
def refresh_module(monkeypatch):
    RefreshPhone.instances = []
    RefreshPolicy.instances = []
    for key in ("fail_action", "fail_readback", "parse_after_action"):
        monkeypatch.setattr(RefreshPhone, key, False)
    for key in ("reject_verification", "reject_completion", "reject_decision"):
        monkeypatch.setattr(RefreshPolicy, key, False)
    module = SimpleNamespace(
        SkillPolicy=RefreshPolicy,
        load_recipe=lambda path: {"task": {"query": QUERY, "expectedFirstTitle": TITLE}},
    )
    monkeypatch.setitem(sys.modules, "workflow", module)
    monkeypatch.setenv("PHONE_BENCH_RECIPE", "/fixture/recipe.json")


REQUESTS = [
    {"command": "observe"},
    {"command": "decide"},
    {"command": "act", "choice": "CLICK:node-1"},
    {"command": "decide"},
    {"command": "act", "choice": "CLICK:node-2"},
    {"command": "finish", "piElapsedMs": 20},
]


def drive(tmp_path):
    output = io.StringIO()
    asyncio.run(
        worker.serve(
            "skill",
            "fixture-refresh",
            base=tmp_path,
            input_stream=io.StringIO("\n".join(json.dumps(r) for r in REQUESTS)),
            output_stream=output,
            runtime_factory=RefreshPhone,
            key_reader=lambda: pytest.fail("local skill must not read model credentials"),
            client_factory=lambda *a, **kw: pytest.fail(
                "local skill must not create model clients"
            ),
        )
    )
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_not_dispatched_releases_policy_for_next_pi_decision_without_internal_action(
    tmp_path, monkeypatch, refresh_module
):
    monkeypatch.setattr(RefreshPhone, "scenario", "not-dispatched")
    replies = drive(tmp_path)
    phone, policy = RefreshPhone.instances[0], RefreshPolicy.instances[0]
    assert replies[2]["result"]["execution"]["status"] == "not_dispatched"
    assert replies[3]["result"]["choice"] == "CLICK:node-2"
    assert phone.attempted == ["CLICK:node-1", "CLICK:node-2"]
    assert phone.actions == ["CLICK:node-2"]
    assert phone.index == 3  # Initial PI observe and the two action-owned fresh reads.
    assert policy.reference_refreshes == ["CLICK:node-1"]
    assert len(policy.verified) == 1
    assert phone.result["status"] == "success" and phone.result["modelCalls"] == []
    assert phone.result["recipeExecution"]["referenceRefreshes"] == 1


@pytest.mark.parametrize("scenario", ["partial", "unknown", "wrong-choice", "partial-forged"])
def test_partial_or_unverified_reference_failure_cannot_authorize_replay(
    tmp_path, monkeypatch, refresh_module, scenario
):
    monkeypatch.setattr(RefreshPhone, "scenario", scenario)
    replies = drive(tmp_path)
    phone = RefreshPhone.instances[0]
    assert "error" in replies[2] and "error" in replies[3]
    assert phone.attempted == ["CLICK:node-1"] and phone.actions == []
    assert phone.index == 2
    assert phone.result["status"] == "failed" and phone.result["modelCalls"] == []
    assert not any(op["name"] == "action_reconciliation" for op in phone.operations)


@pytest.mark.skipif(
    shutil.which(os.environ.get("PI_BIN", "pi")) is None, reason="PI is not installed"
)
@pytest.mark.parametrize("scenario", ["not-dispatched", "partial", "unknown", "partial-forged"])
def test_actual_pi_uses_a_new_tool_action_only_for_verified_pre_dispatch_refresh(
    tmp_path, monkeypatch, refresh_module, scenario
):
    wrapper = tmp_path / "offline-reference-worker.py"
    wrapper.write_text(
        "import argparse,asyncio,json,sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parent)!r})\n"
        "import controller,httpx,own_mcp,worker\n"
        "from replay_worker import private_json\n"
        "from test_skill_not_dispatched_bridge import RefreshPhone,RefreshPolicy\n"
        "def forbidden(*args,**kwargs):\n"
        " raise AssertionError('Offline reference test reached real I/O')\n"
        "httpx.Client=httpx.AsyncClient=forbidden\n"
        "controller.OwnMcp=own_mcp.OwnMcp=forbidden\n"
        f"base=Path({str(tmp_path)!r})\n"
        f"RefreshPhone.scenario={scenario!r}\n"
        "sys.modules['workflow']=SimpleNamespace(SkillPolicy=RefreshPolicy,load_recipe=lambda path:{})\n"
        "class FakePhone(RefreshPhone):\n"
        " def __init__(self,trial_id):\n"
        "  super().__init__(trial_id)\n"
        "  (base/trial_id).mkdir(mode=0o700)\n"
        " def finish(self,extra=None):\n"
        "  result=super().finish(extra)\n"
        "  result.update(elapsedMs=1,milestones=[],actions=self.actions,attempted=self.attempted,observations=self.index)\n"
        "  private_json(base/args.trial_id/'result.json',result)\n"
        "  return result\n"
        "parser=argparse.ArgumentParser()\n"
        "parser.add_argument('--provider',required=True)\n"
        "parser.add_argument('--trial-id',required=True)\n"
        "args=parser.parse_args()\n"
        "asyncio.run(worker.serve(args.provider,args.trial_id,base=base,runtime_factory=FakePhone,\n"
        " key_reader=forbidden,client_factory=forbidden))\n"
    )
    monkeypatch.setenv("PHONE_BENCH_WORKER", str(wrapper))
    monkeypatch.setenv("PHONE_BENCH_PYTHON", sys.executable)
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")
    monkeypatch.setattr(run_pi_trial, "reset_phone", lambda *a: 0)
    metadata = run_pi_trial.launch("skill", scenario, base=tmp_path, timeout=25)
    result = json.loads((tmp_path / scenario / "result.json").read_text())
    events = [json.loads(line) for line in Path(metadata["piEvents"]).read_text().splitlines()]
    tools = [event["toolName"] for event in events if event["type"] == "tool_execution_start"]
    assert tools == ["phone_observe", "phone_action"] + (
        ["phone_action"] if scenario == "not-dispatched" else []
    )
    assert result["actions"] == (["CLICK:node-2"] if scenario == "not-dispatched" else [])
    assert result["observations"] == (3 if scenario == "not-dispatched" else 2)
    assert result["status"] == ("success" if scenario == "not-dispatched" else "failed")
    assert result["modelCalls"] == [] and result["piAgentLoop"] is True
