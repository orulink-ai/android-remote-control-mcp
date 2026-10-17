"""TDD for safe recipe drift recovery; all phone/model state is synthetic."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

from decision_policy import DecisionPolicyError
from providers import ProviderError
from skill_auto import LazyDecisionClient, SkillAutoPolicy
from test_workflow import demo, loading_demo, snapshot
from workflow import RecipeDrift, SkillPolicy, compile_recording


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def decide(self, backend, state, options):
        self.calls.append((backend, state, options))
        result = self.responses.pop(0) if self.responses else {"choice": next(iter(options))}
        if isinstance(result, BaseException):
            raise result
        return result


def error():
    return ProviderError("private", network={}, latency_ms=1,
                         exception_type="ConnectError", stage="tcp_connect")


def make_policy(tmp_path, client=None):
    recipe = compile_recording(demo(), tmp_path / "bundle")
    return SkillAutoPolicy(client or Client(), "skill-auto", recipe=recipe)


def run(coroutine):
    return asyncio.run(coroutine)


def complete_progress():
    stages = ["search", "open_first", "like", "collect", "unlike", "uncollect", "done"]
    return SimpleNamespace(phase="done", milestones=[
        {"stage": a, "next": b} for a, b in zip(stages, stages[1:])
    ])


def test_cached_recipe_path_has_no_model_requests(tmp_path):
    client = Client()
    policy = make_policy(tmp_path, client)
    decision = run(policy.decide(snapshot("feed"), 1))
    assert decision["modelRequest"] is False and decision["selectedBackend"] == "skill"
    final = demo()["steps"][0]["after"]
    policy.verify(final)
    policy.verify_complete(final, progress=complete_progress())
    assert client.calls == [] and policy.attempts == []
    assert policy.summary()["recoveryActive"] is False


@pytest.mark.parametrize("kind", ["precondition", "selector_missing", "postcondition"])
def test_only_typed_screen_drift_hands_remaining_trial_to_jev(tmp_path, kind):
    client = Client()
    policy = make_policy(tmp_path, client)
    current = snapshot("feed")
    if kind == "precondition":
        current["state"]["observation"]["kind"] = "launcher"
    elif kind == "selector_missing":
        current["state"]["actionDescriptors"]["CLICK:ephemeral-1"]["label"] = "搜索新版"
    else:
        run(policy.decide(current, 1))
        changed = snapshot("results", "open_first")
        policy.verify(changed)
        current = changed
    decision = run(policy.decide(current, 2 if kind == "postcondition" else 1))
    assert decision["selectedBackend"] == "jev"
    assert decision["recipeRecovery"] is True
    assert policy.summary()["drifts"][0]["reason"] == kind
    # Once recovered, even a recipe-matching screen stays in the model loop.
    run(policy.decide(snapshot("feed"), 3))
    assert [c[0] for c in client.calls] == ["jev", "jev"]
    assert policy.skill.cursor == 0


def test_jev_failure_requires_new_pi_observation_before_deepseek(tmp_path):
    client = Client(error(), {"choice": "CLICK:new"})
    policy = make_policy(tmp_path, client)
    changed = snapshot("launcher")
    assert run(policy.decide(changed, 1)) == {"kind": "refresh_required", "fallbackPending": True}
    with pytest.raises(DecisionPolicyError):
        run(policy.decide(changed, 1))
    assert len(client.calls) == 1
    fresh = snapshot("launcher", options={"CLICK:new": {
        "method": "click", "role": "navigation", "label": "fresh"
    }})
    decision = run(policy.decide(fresh, 2))
    assert decision["selectedBackend"] == "deepseek" and decision["fallbackUsed"]
    assert len(policy.attempts) == 2


@pytest.mark.parametrize("failure", ["identity", "ambiguous", "budget", "cancelled"])
def test_critical_failures_never_start_model_recovery(tmp_path, failure):
    client = Client()
    policy = make_policy(tmp_path, client)
    current = snapshot("feed")
    if failure == "identity":
        current["state"]["query"] = "different"
    elif failure == "ambiguous":
        current["options"]["CLICK:duplicate"] = "搜索"
        current["state"]["actionDescriptors"]["CLICK:duplicate"] = copy.deepcopy(
            current["state"]["actionDescriptors"]["CLICK:ephemeral-1"])
    elif failure == "budget":
        policy.skill.cursor = len(policy.skill.recipe["steps"])
    else:
        async def cancelled(*args):
            raise asyncio.CancelledError()
        policy.skill.decide = cancelled
    with pytest.raises(asyncio.CancelledError if failure == "cancelled" else DecisionPolicyError):
        run(policy.decide(current, 1))
    assert client.calls == [] and not policy.summary()["recoveryActive"]


def test_failed_model_fallback_is_terminal(tmp_path):
    client = Client(error(), error())
    policy = make_policy(tmp_path, client)
    run(policy.decide(snapshot("launcher"), 1))
    with pytest.raises(DecisionPolicyError):
        run(policy.decide(snapshot("launcher"), 2))
    with pytest.raises(DecisionPolicyError):
        run(policy.decide(snapshot("launcher"), 3))
    assert [c[0] for c in client.calls] == ["jev", "deepseek"]


def test_recovery_final_state_requires_common_progress_and_original_target(tmp_path):
    policy = make_policy(tmp_path)
    run(policy.decide(snapshot("launcher"), 1))
    final = demo()["steps"][0]["after"]
    with pytest.raises(DecisionPolicyError):
        policy.verify_complete(final)
    incomplete = complete_progress()
    incomplete.milestones.pop()
    with pytest.raises(DecisionPolicyError):
        policy.verify_complete(final, progress=incomplete)
    policy.verify_complete(final, progress=complete_progress())
    assert policy.skill.cursor == 0  # Never fabricate all recipe steps as verified.
    for field, value in [("liked", True), ("collected", True), ("firstTitleVisible", False)]:
        changed = copy.deepcopy(final)
        changed["state"]["observation"][field] = value
        with pytest.raises(DecisionPolicyError):
            policy.verify_complete(changed, progress=complete_progress())
    changed = copy.deepcopy(final)
    changed["state"]["expectedFirstTitle"] = "different target"
    with pytest.raises(DecisionPolicyError):
        policy.verify_complete(changed, progress=complete_progress())


@pytest.mark.parametrize("execution", [
    {"status": "partial"}, {"status": "unknown"}, {"status": "applied", "partial": True},
    {"status": "not_dispatched", "reason": "stale_reference", "partial": True},
])
def test_uncertain_delivery_snapshot_never_enters_model_recovery(tmp_path, execution):
    client = Client()
    policy = make_policy(tmp_path, client)
    current = snapshot("launcher")
    current["execution"] = execution
    with pytest.raises(DecisionPolicyError):
        run(policy.decide(current, 1))
    assert client.calls == [] and not policy.recovery_active


def test_strict_skill_retains_stop_behavior_with_typed_drift(tmp_path):
    recipe = compile_recording(demo(), tmp_path / "bundle")
    policy = SkillPolicy(None, "skill", recipe=recipe)
    with pytest.raises(RecipeDrift):
        run(policy.decide(snapshot("launcher"), 1))
    assert policy.summary()["driftPolicy"] == "stop"


def test_demonstrated_settling_budget_exhaustion_is_critical(tmp_path):
    recording = loading_demo()
    client = Client()
    policy = SkillAutoPolicy(client, "skill-auto", recipe=compile_recording(recording, tmp_path / "bundle"))
    run(policy.decide(recording["steps"][0]["before"], 1))
    policy.verify(recording["steps"][0]["after"])
    policy.skill.current_waits = 8
    with pytest.raises(DecisionPolicyError):
        run(policy.decide(recording["steps"][1]["before"], 2))
    assert client.calls == [] and not policy.recovery_active


@pytest.mark.parametrize("invalid", [False, "partial", "wrong_intent", "extra_field"])
def test_recovery_not_dispatched_requires_exact_evidence(tmp_path, invalid):
    policy = make_policy(tmp_path)
    current = snapshot("launcher")
    chosen = run(policy.decide(current, 1))["choice"]
    fresh = copy.deepcopy(current)
    fresh["execution"] = {
        "status": "not_dispatched", "reason": "stale_reference", "choice": chosen,
        "intent": copy.deepcopy(current["state"]["actionDescriptors"][chosen]),
    }
    if invalid == "partial":
        fresh["execution"]["status"] = "partial"
    elif invalid == "wrong_intent":
        fresh["execution"]["intent"]["label"] = "different action"
    elif invalid == "extra_field":
        fresh["execution"]["partial"] = True
    if invalid:
        with pytest.raises(DecisionPolicyError):
            policy.not_dispatched(fresh, chosen)
    else:
        policy.not_dispatched(fresh, chosen)
        result = run(policy.decide(fresh, 2))
        assert result["selectedBackend"] == "jev"
        assert policy.summary()["referenceRefreshes"] == 1


def test_lazy_client_reads_key_and_constructs_transport_only_after_drift():
    events = []

    class Model:
        def __init__(self, key, **kwargs):
            events.append(("client", key, kwargs))
        async def decide(self, *args):
            events.append("request")
            return {"choice": "WAIT"}
        async def close(self):
            events.append("close")

    def key():
        events.append("key")
        return "fixture"

    async def scenario():
        client = LazyDecisionClient(Model, key, mode="warm", compact=True, decisionStyle="heads")
        assert events == [] and client.mode == "local"
        await client.decide("jev", {}, {"WAIT": "wait"})
        await client.decide("deepseek", {}, {"WAIT": "wait"})
        await client.close()
        await client.close()

    run(scenario())
    assert [event if isinstance(event, str) else event[0] for event in events] == [
        "key", "client", "request", "request", "close"]
