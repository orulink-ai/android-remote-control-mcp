"""Independent review regressions; all evidence is synthetic and device-free."""

import asyncio
import copy

import pytest

from decision_policy import DecisionPolicyError
from test_workflow import demo, snapshot
from workflow import SkillPolicy, compile_recording


def test_final_completion_rechecks_last_postcondition_after_later_observation(tmp_path):
    recording = demo()
    policy = SkillPolicy(None, "skill", recipe=compile_recording(recording, tmp_path / "bundle"))
    asyncio.run(policy.decide(recording["steps"][0]["before"], 1))
    policy.verify(recording["steps"][0]["after"])
    changed = snapshot("detail", "done", liked=True, collected=False, done=True)
    with pytest.raises(DecisionPolicyError):
        policy.verify_complete(changed)


def test_step_postcondition_rejects_task_identity_change(tmp_path):
    recording = demo()
    policy = SkillPolicy(None, "skill", recipe=compile_recording(recording, tmp_path / "bundle"))
    asyncio.run(policy.decide(recording["steps"][0]["before"], 1))
    changed = copy.deepcopy(recording["steps"][0]["after"])
    changed["state"]["expectedFirstTitle"] = "另一个任务目标"
    with pytest.raises(DecisionPolicyError):
        policy.verify(changed)
    assert policy.summary()["verifiedSteps"] == 0


def test_compiler_rejects_selected_action_absent_from_its_before_observation(tmp_path):
    recording = demo()
    recording["steps"][0]["action"] = copy.deepcopy(recording["steps"][0]["action"])
    recording["steps"][0]["action"]["label"] = "从未出现过的控件"
    with pytest.raises(ValueError):
        compile_recording(recording, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_compiler_rejects_recording_with_changed_task_identity_midway(tmp_path):
    recording = demo()
    recording["steps"][0]["after"]["state"]["query"] = "不属于这次示教的搜索词"
    with pytest.raises(ValueError):
        compile_recording(recording, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_settling_does_not_authorize_wait_on_a_later_unseen_observation(tmp_path):
    from test_workflow import loading_demo

    recording = loading_demo()
    policy = SkillPolicy(None, "skill", recipe=compile_recording(recording, tmp_path / "bundle"))
    asyncio.run(policy.decide(recording["steps"][0]["before"], 1))
    policy.verify(recording["steps"][0]["after"])
    # PI may explicitly refresh again before deciding; prior known loading
    # evidence must not authorize an action on this newer unexpected screen.
    unseen = snapshot("feed", options={"WAIT": {"method": "wait", "role": "wait", "label": "等待"}})
    with pytest.raises(DecisionPolicyError):
        asyncio.run(policy.decide(unseen, 2))


def test_explicit_partial_delivery_cannot_be_hidden_by_not_dispatched_status(tmp_path):
    recording = demo()
    before = recording["steps"][0]["before"]
    policy = SkillPolicy(None, "skill", recipe=compile_recording(recording, tmp_path / "bundle"))
    decision = asyncio.run(policy.decide(before, 1))
    fresh = copy.deepcopy(before)
    fresh["execution"] = {
        "status": "not_dispatched",
        "reason": "stale_reference",
        "choice": decision["choice"],
        "intent": copy.deepcopy(recording["steps"][0]["action"]),
        "partial": True,
    }
    with pytest.raises(DecisionPolicyError):
        policy.not_dispatched(fresh, decision["choice"])
