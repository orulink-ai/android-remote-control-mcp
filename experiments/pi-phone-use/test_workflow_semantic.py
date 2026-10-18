"""Semantic raw provenance remains distinct from verified runtime navigation."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from test_workflow_intents import supported_raw
from workflow_intents import IntentError, derive_plan, digest, validation_binding

FIXTURE = Path(__file__).parent / "fixtures/xhs-semantic-recording-v2.json"


def semantic_raw():
    return json.loads(FIXTURE.read_text())


def test_semantic_plan_accounts_for_all_thirteen_events_without_inventing_submit():
    raw = semantic_raw()
    before = copy.deepcopy(raw)
    plan = derive_plan(raw)
    assert raw == before and plan["schemaVersion"] == 2
    assert [item["eventSequence"] for item in plan["intents"]] == [1, 4, 7, 10, 11, 12, 13]
    assert "search_submit" not in [item["kind"] for item in plan["intents"]]
    assert [item["eventSequence"] for item in plan["rawEventDispositions"]] == list(range(1, 14))
    assert [item["eventSha256"] for item in plan["rawEventDispositions"]] == [
        digest(event) for event in raw["events"]
    ]
    assert plan["queryFocusEventSequences"] == [2, 3]
    assert [item["eventSequence"] for item in plan["observationNotifications"]] == [5, 6, 8, 9]
    binding = validation_binding(plan)
    assert binding["rawIntentCount"] == 7 and binding["rawEventCount"] == 13
    assert binding["allRawEventsAccountedFor"] is True and binding["allRawActionsReplayed"] is False
    assert "allConsumed" not in binding
    assert derive_plan(supported_raw())["schemaVersion"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "extra-click",
        "missing-like",
        "focus-class",
        "focus-field",
        "focus-text",
        "notification-class",
        "notification-label",
        "scroll-outside",
        "wrong-query",
        "wrong-order",
    ],
)
def test_semantic_normalization_rejects_unknown_actions_or_unproven_noise(change):
    raw = semantic_raw()
    if change == "extra-click":
        raw["events"][4]["type"] = "click"
    elif change == "missing-like":
        raw["events"][9]["selector"]["semanticRole"] = None
    elif change == "focus-class":
        raw["events"][1]["selector"]["className"] = "android.widget.Button"
    elif change == "focus-field":
        raw["events"][2]["selector"]["resourceId"] = "other"
    elif change == "focus-text":
        raw["events"][2]["selector"]["text"] = "另一个查询"
    elif change == "notification-class":
        raw["events"][4]["selector"]["className"] = "android.widget.EditText"
    elif change == "notification-label":
        raw["events"][4]["selector"]["contentDescription"] = "发布"
    elif change == "scroll-outside":
        raw["events"][7], raw["events"][9] = raw["events"][9], raw["events"][7]
    elif change == "wrong-query":
        raw["events"][3]["value"] = "other"
    else:
        raw["events"][10], raw["events"][11] = raw["events"][11], raw["events"][10]
    for sequence, event in enumerate(raw["events"], 1):
        event["sequence"], event["elapsedMs"] = sequence, sequence * 100
    with pytest.raises(IntentError):
        derive_plan(raw)


def test_actual_android_raw_is_only_read_and_hash_binding_is_to_that_raw():
    path = (
        Path(__file__).parents[2]
        / "artifacts/pi-phone-use/app-workflow-20261018/actual-android-recording.json"
    )
    if not path.is_file():
        pytest.skip("Private real-device evidence is not in this checkout")
    before = path.read_bytes()
    plan = derive_plan(json.loads(before))
    assert plan["rawSha256"] == digest(json.loads(before)) and len(plan["intents"]) == 7
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(before).digest()


@pytest.mark.parametrize("automatic", [False, True])
def test_runtime_seven_raw_intents_and_only_actual_required_transition(
    tmp_path, monkeypatch, automatic
):
    from terminal_runtime import TerminalRecordedRuntime
    from test_workflow_verification import install_fake
    from workflow import compile_recording
    from workflow_intents import evidence_matches_plan
    from workflow_verification import DraftIntentRuntime

    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    original = TerminalRecordedRuntime.act

    def act(self, choice):
        if automatic and self.position == 1 and choice == "NEXT":
            self.position = 3
            return self.observe()
        return original(self, choice)

    monkeypatch.setattr(TerminalRecordedRuntime, "act", act)
    plan = derive_plan(semantic_raw())
    runtime = DraftIntentRuntime("verify", plan)
    runtime.observe()
    for _ in range(7 if automatic else 8):
        assert "NEXT" in runtime.actions
        runtime.act("NEXT")
    result = runtime.finish()
    assert result["status"] == "success"
    evidence = json.loads((tmp_path / "verify/recording.json").read_text())
    assert evidence_matches_plan(evidence, plan)
    assert [
        step["rawEventSequence"]
        for step in evidence["steps"]
        if step.get("rawEventSequence") is not None
    ] == [1, 4, 7, 10, 11, 12, 13]
    transitions = [
        step for step in evidence["steps"] if step.get("origin") == "runtime-required-transition"
    ]
    assert len(transitions) == (0 if automatic else 1)
    if transitions:
        assert transitions[0]["rawEventSequence"] is None and transitions[0][
            "betweenRawEvents"
        ] == [4, 7]
    recipe = compile_recording(evidence, tmp_path / "bundle", entry_strategy="prepare-feed-v1")
    generation = json.loads((tmp_path / "bundle/generation.json").read_text())
    assert recipe["recordingValidation"]["rawIntentCount"] == generation["rawIntentCount"] == 7
    assert (
        recipe["runtimeTransitionCount"]
        == generation["runtimeTransitionCount"]
        == (0 if automatic else 1)
    )
    assert len(recipe["steps"]) == (7 if automatic else 8)
    assert "7 项原录制意图" in (tmp_path / "bundle/SKILL.md").read_text()


def test_semantic_transition_never_chooses_between_duplicate_search_targets(tmp_path, monkeypatch):
    from test_workflow_verification import install_fake
    from workflow_verification import DraftIntentRuntime, IntentVerificationError

    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    runtime = DraftIntentRuntime("verify", derive_plan(semantic_raw()))
    runtime.observe()
    runtime.act("NEXT")
    runtime.act("NEXT")
    runtime.offered["DUP"] = copy.deepcopy(runtime.offered["NEXT"])
    with pytest.raises(IntentVerificationError):
        runtime._refresh_options(runtime.snapshot)


def test_semantic_stale_query_is_not_consumed_and_can_resolve_fresh_reference(
    tmp_path, monkeypatch
):
    from terminal_runtime import TerminalRecordedRuntime
    from test_workflow_verification import install_fake
    from workflow_intents import evidence_matches_plan
    from workflow_verification import DraftIntentRuntime

    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    original = TerminalRecordedRuntime.act
    stale = [True]

    def act(self, choice):
        if self.position == 1 and stale[0]:
            stale[0] = False
            before = copy.deepcopy(self.snapshot)
            after = self.observe()
            after["execution"] = {
                "status": "not_dispatched",
                "reason": "stale_reference",
                "choice": choice,
                "intent": before["state"]["actionDescriptors"][choice],
            }
            return after
        return original(self, choice)

    monkeypatch.setattr(TerminalRecordedRuntime, "act", act)
    plan = derive_plan(semantic_raw())
    runtime = DraftIntentRuntime("verify", plan)
    runtime.observe()
    runtime.act("NEXT")
    runtime.act("NEXT")
    assert runtime.cursor == 1
    for _ in range(7):
        runtime.act("NEXT")
    assert runtime.finish()["status"] == "success"
    evidence = json.loads((tmp_path / "verify/recording.json").read_text())
    assert evidence_matches_plan(evidence, plan)


@pytest.mark.parametrize("remove", [[1], [2], [4], [5], [7], [8], [1, 2, 4, 5, 7, 8]])
def test_optional_focus_and_notifications_are_bounded_not_fixed_event_counts(remove):
    raw = semantic_raw()
    original = [event for index, event in enumerate(raw["events"]) if index not in remove]
    raw["events"] = copy.deepcopy(original)
    for sequence, event in enumerate(raw["events"], 1):
        event["sequence"] = sequence
    plan = derive_plan(raw)
    assert len(plan["intents"]) == 7 and len(plan["rawEventDispositions"]) == 13 - len(remove)
    assert plan["runtimeTransitionPolicy"]["betweenRawEvents"] == [
        plan["intents"][1]["eventSequence"],
        plan["intents"][2]["eventSequence"],
    ]


def with_recorded_submit():
    raw = semantic_raw()
    submit = copy.deepcopy(raw["events"][0])
    submit["elapsedMs"] = raw["events"][3]["elapsedMs"]
    raw["events"].insert(4, submit)
    for sequence, event in enumerate(raw["events"], 1):
        event["sequence"] = sequence
    return raw


def test_semantic_explicit_submit_remains_a_real_eighth_raw_intent():
    plan = derive_plan(with_recorded_submit())
    assert plan["schemaVersion"] == 2 and len(plan["intents"]) == 8
    assert (
        plan["intents"][2]["kind"] == "search_submit" and plan["intents"][2]["eventSequence"] == 5
    )
    assert plan["runtimeTransitionPolicy"] is None
    assert (
        validation_binding(plan)["rawIntentCount"] == 8 and len(plan["rawEventDispositions"]) == 14
    )


@pytest.mark.parametrize("change", ["duplicate", "before-query", "after-open"])
def test_recorded_submit_must_be_unique_and_between_query_and_target(change):
    raw = with_recorded_submit()
    if change == "duplicate":
        raw["events"].insert(5, copy.deepcopy(raw["events"][4]))
    else:
        submit = raw["events"].pop(4)
        raw["events"].insert(3 if change == "before-query" else 7, submit)
    for sequence, event in enumerate(raw["events"], 1):
        event["sequence"], event["elapsedMs"] = sequence, sequence * 100
    with pytest.raises(IntentError):
        derive_plan(raw)


def test_applied_search_transition_is_not_repeated_while_results_are_unverified(
    tmp_path, monkeypatch
):
    from terminal_runtime import TerminalRecordedRuntime
    from test_workflow_verification import install_fake
    from workflow_verification import DraftIntentRuntime, IntentVerificationError

    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    original = TerminalRecordedRuntime.act
    dispatches = []

    def act(self, choice):
        if self.position == 2 and choice == "NEXT":
            dispatches.append(True)
            return self.observe()
        return original(self, choice)

    monkeypatch.setattr(TerminalRecordedRuntime, "act", act)
    runtime = DraftIntentRuntime("verify", derive_plan(semantic_raw()))
    runtime.observe()
    runtime.act("NEXT")
    runtime.act("NEXT")
    runtime.act("NEXT")
    assert runtime.transition_pending and runtime.transition_dispatches == 1
    for _ in range(7):
        assert "NEXT" not in runtime.actions
        runtime.act("WAIT")
    with pytest.raises(IntentVerificationError):
        runtime.act("WAIT")
    assert dispatches == [True] and runtime.finish()["status"] == "failed"


@pytest.mark.parametrize("change", ["raw-hash", "origin", "between", "notification", "plan"])
def test_semantic_paired_evidence_cannot_fake_or_rebind_navigation_source(
    tmp_path, monkeypatch, change
):
    from test_workflow_verification import install_fake
    from workflow import compile_recording
    from workflow_intents import evidence_matches_plan
    from workflow_verification import DraftIntentRuntime

    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    plan = derive_plan(semantic_raw())
    runtime = DraftIntentRuntime("verify", plan)
    runtime.observe()
    for _ in range(8):
        runtime.act("NEXT")
    runtime.finish()
    evidence = json.loads((tmp_path / "verify/recording.json").read_text())
    if change == "raw-hash":
        evidence["steps"][0]["rawEventSha256"] = "0" * 64
    elif change == "origin":
        evidence["steps"][2]["origin"] = "raw-intent"
    elif change == "between":
        evidence["steps"][2]["betweenRawEvents"] = [1, 7]
    elif change == "notification":
        evidence["observationNotifications"][0]["gestureReplayed"] = True
    else:
        evidence["intentPlan"]["planSha256"] = "0" * 64
    if change != "plan":
        assert not evidence_matches_plan(evidence, plan)
    with pytest.raises(ValueError):
        compile_recording(evidence, tmp_path / "must-not-publish")
    assert not (tmp_path / "must-not-publish").exists()


@pytest.mark.parametrize(
    "change", ["third-focus", "third-window-notification", "notification-after-like"]
)
def test_semantic_auxiliary_counts_and_navigation_windows_remain_strict(change):
    raw = semantic_raw()
    if change == "third-focus":
        raw["events"].insert(2, copy.deepcopy(raw["events"][1]))
    elif change == "third-window-notification":
        event = raw["events"].pop(7)
        raw["events"].insert(5, event)
    else:
        event = raw["events"].pop(7)
        raw["events"].insert(9, event)
    for sequence, event in enumerate(raw["events"], 1):
        event["sequence"], event["elapsedMs"] = sequence, sequence * 100
    with pytest.raises(IntentError):
        derive_plan(raw)
