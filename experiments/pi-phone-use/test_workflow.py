"""Demonstrations must retain evidence and resolve fresh controls on replay."""

import asyncio
import copy
import json

import pytest

from decision_policy import DecisionPolicyError
from workflow import SkillPolicy, compile_recording, draft_android_recording, load_recipe


def snapshot(kind, stage="search", *, options=None, liked=None, collected=None, done=False):
    controls = options or {
        "CLICK:ephemeral-1": {"method": "click", "role": "navigation", "label": "搜索"}
    }
    return {
        "state": {
            "stage": stage,
            "query": "小狮日记",
            "expectedFirstTitle": "目标帖子",
            "observation": {
                "kind": kind,
                "liked": liked,
                "collected": collected,
                "queryMatches": kind == "results",
                "firstTitleVisible": kind in ("results", "detail"),
            },
            "actionDescriptors": controls,
        },
        "options": {key: value["label"] for key, value in controls.items()},
        "done": done,
    }


def demo():
    before = snapshot("feed")
    after = snapshot("detail", "done", liked=False, collected=False, done=True)
    return {
        "schemaVersion": 1,
        "source": "pi-agent-demonstration",
        "status": "success",
        "task": {"query": "小狮日记", "expectedFirstTitle": "目标帖子"},
        "steps": [
            {
                "before": before,
                "action": before["state"]["actionDescriptors"]["CLICK:ephemeral-1"],
                "after": after,
            }
        ],
    }


def test_compile_has_provenance_and_no_ephemeral_ids(tmp_path):
    bundle = compile_recording(demo(), tmp_path / "bundle")
    encoded = json.dumps(bundle, ensure_ascii=False)
    assert "ephemeral" not in encoded
    assert "center" not in encoded
    assert bundle["sourceSha256"]
    assert bundle["steps"][0]["selector"] == {
        "method": "click",
        "role": "navigation",
        "label": "搜索",
    }
    assert load_recipe(tmp_path / "bundle" / "recipe.json") == bundle
    skill = (tmp_path / "bundle" / "SKILL.md").read_text()
    for value in ("小狮日记", "搜索", "验证", "recipe.json"):
        assert value in skill


@pytest.mark.parametrize("change", ["failed", "broken_chain", "no_verification"])
def test_unverified_recording_cannot_become_executable(tmp_path, change):
    recording = demo()
    if change == "failed":
        recording["status"] = "failed"
    elif change == "broken_chain":
        recording["steps"].append(copy.deepcopy(recording["steps"][0]))
    else:
        recording["steps"][0]["after"] = {}
    with pytest.raises(ValueError):
        compile_recording(recording, tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


def test_fresh_ids_resolved_without_calling_model(tmp_path):
    recipe = compile_recording(demo(), tmp_path / "bundle")
    policy = SkillPolicy(None, "skill", recipe=recipe)
    current = snapshot(
        "feed",
        options={"CLICK:new-live-42": {"method": "click", "role": "navigation", "label": "搜索"}},
    )
    result = asyncio.run(policy.decide(current, 1))
    assert result["choice"] == "CLICK:new-live-42"
    assert result["modelRequest"] is False
    assert policy.summary()["recipeHits"] == 1
    policy.verify(demo()["steps"][0]["after"])
    assert policy.summary()["verifiedSteps"] == 1


@pytest.mark.parametrize(
    "change", ["ambiguous", "wrong_query", "wrong_title", "wrong_screen", "stale"]
)
def test_replay_stops_on_drift_without_dispatch(tmp_path, change):
    policy = SkillPolicy(None, "skill", recipe=compile_recording(demo(), tmp_path / "bundle"))
    current = snapshot("feed")
    if change == "ambiguous":
        current["state"]["actionDescriptors"]["CLICK:another"] = dict(
            current["state"]["actionDescriptors"]["CLICK:ephemeral-1"]
        )
        current["options"]["CLICK:another"] = "搜索"
    elif change == "wrong_query":
        current["state"]["query"] = "另一个查询"
    elif change == "wrong_title":
        current["state"]["expectedFirstTitle"] = "另一篇帖子"
    elif change == "wrong_screen":
        current["state"]["observation"]["kind"] = "launcher"
    else:
        asyncio.run(policy.decide(current, 1))
    with pytest.raises(DecisionPolicyError):
        asyncio.run(policy.decide(current, 1))


def test_final_verification_is_required_even_when_pi_reports_done(tmp_path):
    policy = SkillPolicy(None, "skill", recipe=compile_recording(demo(), tmp_path / "bundle"))
    asyncio.run(policy.decide(snapshot("feed"), 1))
    wrong = snapshot("detail", "done", liked=True, collected=False, done=True)
    with pytest.raises(DecisionPolicyError):
        policy.verify(wrong)
    assert policy.summary()["verifiedSteps"] == 0


def test_android_raw_recording_produces_reviewable_draft_only(tmp_path):
    raw = {
        "schemaVersion": 1,
        "source": "android-accessibility-events",
        "name": "我的演示",
        "targetPackage": "com.xingin.xhs",
        "needsReview": True,
        "hasPairedVerification": False,
        "events": [{"type": "click", "selector": {"text": "搜索", "resourceId": "search"}}],
    }
    draft = draft_android_recording(raw, tmp_path / "draft")
    assert draft["reviewState"] == "needs-verification"
    assert "搜索" in (tmp_path / "draft" / "SKILL.md").read_text()
    with pytest.raises(ValueError):
        load_recipe(tmp_path / "draft" / "draft.json")


def test_tampered_selector_is_rejected(tmp_path):
    bundle = compile_recording(demo(), tmp_path / "bundle")
    bundle["steps"][0]["selector"]["node_id"] = "stale"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(bundle))
    with pytest.raises(ValueError):
        load_recipe(path)


def test_post_selector_ignores_live_counts_but_keeps_unique_title(tmp_path):
    recording = demo()
    recording["steps"][0]["action"]["label"] = "目标帖子 | 12赞 | 昨天"
    recipe = compile_recording(recording, tmp_path / "bundle")
    policy = SkillPolicy(None, "skill", recipe=recipe)
    current = snapshot(
        "feed",
        options={
            "CLICK:live": {
                "method": "click",
                "role": "navigation",
                "label": "目标帖子 | 13赞 | 今天",
            }
        },
    )
    assert asyncio.run(policy.decide(current, 1))["choice"] == "CLICK:live"


def test_recorder_keeps_paired_evidence_and_failed_trials_uncompilable(tmp_path, monkeypatch):
    import workflow
    from controller import PhoneRuntime

    runtime = workflow.RecordingPhoneRuntime.__new__(workflow.RecordingPhoneRuntime)
    runtime.directory = tmp_path
    runtime.snapshot = demo()["steps"][0]["before"]
    runtime.actions = runtime.snapshot["state"]["actionDescriptors"]
    runtime.recorded_steps = []
    after = demo()["steps"][0]["after"]

    def act(_self, choice):
        _self.snapshot = after
        return after

    monkeypatch.setattr(PhoneRuntime, "act", act)
    monkeypatch.setattr(PhoneRuntime, "finish", lambda _self, extra: {"status": "failed"})
    runtime.act("CLICK:ephemeral-1")
    runtime.finish()
    recorded = json.loads((tmp_path / "recording.json").read_text())
    assert recorded["steps"][0]["before"]["state"]["observation"]["kind"] == "feed"
    assert recorded["steps"][0]["after"] == after
    assert "ephemeral" not in json.dumps(recorded["steps"][0]["action"])
    with pytest.raises(ValueError):
        compile_recording(recorded, tmp_path / "must-not-execute")


def test_edited_steps_invalidate_previous_demonstration_validation(tmp_path):
    recipe = compile_recording(demo(), tmp_path / "bundle")
    recipe["steps"][0]["selector"]["label"] = "另一个按钮"
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(recipe))
    with pytest.raises(ValueError):
        load_recipe(path)


def test_finish_requires_current_final_state_and_task_identity(tmp_path):
    recipe = compile_recording(demo(), tmp_path / "bundle")
    policy = SkillPolicy(None, "skill", recipe=recipe)
    asyncio.run(policy.decide(snapshot("feed"), 1))
    final = demo()["steps"][0]["after"]
    policy.verify(final)
    policy.verify_complete(final)
    changed = copy.deepcopy(final)
    changed["state"]["observation"]["liked"] = True
    with pytest.raises(DecisionPolicyError):
        policy.verify_complete(changed)
    changed = copy.deepcopy(final)
    changed["state"]["expectedFirstTitle"] = "不同帖子"
    with pytest.raises(DecisionPolicyError):
        policy.verify_complete(changed)


def loading_demo():
    a = snapshot("search_input")
    b = snapshot(
        "transition", options={"WAIT": {"method": "wait", "role": "wait", "label": "等待"}}
    )
    c = snapshot("results", "open_first")
    d = snapshot("detail", "done", liked=False, collected=False, done=True)
    recording = demo()
    recording["steps"] = [
        {"before": a, "action": a["state"]["actionDescriptors"]["CLICK:ephemeral-1"], "after": b},
        {"before": b, "action": b["state"]["actionDescriptors"]["WAIT"], "after": c},
        {"before": c, "action": c["state"]["actionDescriptors"]["CLICK:ephemeral-1"], "after": d},
    ]
    return recording


def test_demonstrated_waits_become_verified_settling_not_fixed_extra_actions(tmp_path):
    recording = loading_demo()
    recipe = compile_recording(recording, tmp_path / "bundle")
    assert len(recipe["steps"]) == 2
    policy = SkillPolicy(None, "skill", recipe=recipe)
    asyncio.run(policy.decide(recording["steps"][0]["before"], 1))
    policy.verify(recording["steps"][0]["after"])
    assert policy.summary()["verifiedSteps"] == 0
    result = asyncio.run(policy.decide(recording["steps"][1]["before"], 2))
    assert result["choice"] == "WAIT"
    policy.verify(recording["steps"][1]["after"])
    assert policy.summary()["verifiedSteps"] == 1
    assert policy.summary()["settleWaits"] == 1
    assert policy.summary()["recipeHits"] == 1


def test_fast_page_can_satisfy_same_postcondition_without_replaying_recorded_wait(tmp_path):
    recording = loading_demo()
    recipe = compile_recording(recording, tmp_path / "bundle")
    policy = SkillPolicy(None, "skill", recipe=recipe)
    asyncio.run(policy.decide(recording["steps"][0]["before"], 1))
    policy.verify(recording["steps"][1]["after"])
    assert policy.summary()["verifiedSteps"] == 1
    assert policy.summary()["settleWaits"] == 0


def test_unseen_postcondition_and_exhausted_settling_stop_without_reclick(tmp_path):
    recording = loading_demo()
    recipe = compile_recording(recording, tmp_path / "bundle")
    policy = SkillPolicy(None, "skill", recipe=recipe)
    asyncio.run(policy.decide(recording["steps"][0]["before"], 1))
    with pytest.raises(DecisionPolicyError):
        policy.verify(snapshot("detail", liked=True))
    policy = SkillPolicy(None, "skill", recipe=recipe)
    asyncio.run(policy.decide(recording["steps"][0]["before"], 1))
    loading = recording["steps"][1]["before"]
    policy.verify(loading)
    for index in range(2, 10):
        result = asyncio.run(policy.decide(loading, index))
        assert result["choice"] == "WAIT"
        policy.verify(loading)
    with pytest.raises(DecisionPolicyError):
        asyncio.run(policy.decide(loading, 10))


def test_intervening_observation_cannot_wait_on_unseen_page(tmp_path):
    recording = loading_demo()
    policy = SkillPolicy(None, "skill", recipe=compile_recording(recording, tmp_path / "bundle"))
    asyncio.run(policy.decide(recording["steps"][0]["before"], 1))
    policy.verify(recording["steps"][0]["after"])
    changed = copy.deepcopy(recording["steps"][1]["before"])
    changed["state"]["observation"]["kind"] = "unseen-page"
    with pytest.raises(DecisionPolicyError):
        asyncio.run(policy.decide(changed, 3))


def undelivered(snapshot_value, choice="CLICK:ephemeral-1"):
    value = copy.deepcopy(snapshot_value)
    value["execution"] = {
        "status": "not_dispatched",
        "reason": "stale_reference",
        "choice": choice,
        "intent": copy.deepcopy(snapshot_value["state"]["actionDescriptors"][choice]),
    }
    return value


def test_known_undelivered_reference_reselects_fresh_control_without_skipping_step(tmp_path):
    recording = demo()
    policy = SkillPolicy(None, "skill", recipe=compile_recording(recording, tmp_path / "bundle"))
    current = recording["steps"][0]["before"]
    asyncio.run(policy.decide(current, 1))
    policy.not_dispatched(undelivered(current), "CLICK:ephemeral-1")
    assert policy.summary()["verifiedSteps"] == 0
    fresh = snapshot(
        "feed", options={"CLICK:new": {"method": "click", "role": "navigation", "label": "搜索"}}
    )
    assert asyncio.run(policy.decide(fresh, 2))["choice"] == "CLICK:new"
    policy.verify(recording["steps"][0]["after"])
    assert policy.summary()["referenceRefreshes"] == 1
    assert policy.summary()["verifiedSteps"] == 1


@pytest.mark.parametrize(
    "change", ["partial", "different_choice", "wrong_intent", "wrong_state", "wrong_task"]
)
def test_undelivered_refresh_cannot_authorize_unknown_or_partial_actions(tmp_path, change):
    policy = SkillPolicy(None, "skill", recipe=compile_recording(demo(), tmp_path / "bundle"))
    before = snapshot("feed")
    asyncio.run(policy.decide(before, 1))
    value = undelivered(before)
    if change == "partial":
        value["execution"]["status"] = "partial"
    elif change == "different_choice":
        value["execution"]["choice"] = "CLICK:foreign"
    elif change == "wrong_intent":
        value["execution"]["intent"]["label"] = "另一操作"
    elif change == "wrong_state":
        value["state"]["observation"]["kind"] = "detail"
    else:
        value["state"]["query"] = "另一任务"
    with pytest.raises(DecisionPolicyError):
        policy.not_dispatched(value, "CLICK:ephemeral-1")
    assert policy.summary()["verifiedSteps"] == 0


def test_reference_refresh_has_a_per_step_budget(tmp_path):
    policy = SkillPolicy(None, "skill", recipe=compile_recording(demo(), tmp_path / "bundle"))
    before = snapshot("feed")
    for index in range(1, 4):
        asyncio.run(policy.decide(before, index))
        policy.not_dispatched(undelivered(before), "CLICK:ephemeral-1")
    asyncio.run(policy.decide(before, 4))
    with pytest.raises(DecisionPolicyError):
        policy.not_dispatched(undelivered(before), "CLICK:ephemeral-1")


def test_compiler_excludes_proven_undelivered_attempts_from_executable_steps(tmp_path):
    recording = demo()
    before = recording["steps"][0]["before"]
    recording["steps"].insert(
        0,
        {
            "before": before,
            "action": copy.deepcopy(recording["steps"][0]["action"]),
            "after": undelivered(before),
            "delivery": "not_dispatched",
        },
    )
    recipe = compile_recording(recording, tmp_path / "bundle")
    assert len(recipe["steps"]) == 1
