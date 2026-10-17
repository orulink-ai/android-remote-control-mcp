"""Offline safety/transition contract: no phone, model API, or credentials."""

import asyncio
import json
import math

import pytest

from decision_policy import DecisionPolicy, DecisionPolicyError, ObservationRefreshRequired
from providers import ProviderError


def snapshot(options=None):
    return {"state": {"screen": "current"}, "options": options or {"CLICK:n1": "Like"}}


def decision(choice="CLICK:n1"):
    return {
        "choice": choice,
        "latencyMs": 99999,
        "network": {"httpVersion": "HTTP/2"},
        "cost": 0.002,
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "rawAnswer": {"private": "not for attempt audit"},
    }


def failure():
    return ProviderError(
        "secret-request-header",
        network={"connectMs": 2.0},
        latency_ms=99999,
        exception_type="ConnectError",
        stage="tcp_connect",
    )


class Client:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def decide(self, backend, state, options):
        self.calls.append((backend, state, dict(options)))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return outcome


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        self.value += 0.025
        return self.value


def run(coroutine):
    return asyncio.run(coroutine)


def test_primary_success_and_consistent_measured_latency():
    client = Client(decision())
    policy = DecisionPolicy(client, "jev-auto", clock=Clock())
    result = run(policy.decide(snapshot(), 1))
    assert result["kind"] == "decision"
    assert result["selectedBackend"] == "jev"
    assert result["fallbackUsed"] is False
    assert result["latencyMs"] == pytest.approx(25)
    attempt = policy.attempts[0]
    assert attempt["latencyMs"] == result["latencyMs"]
    assert attempt["status"] == "success"
    assert attempt["attemptId"] == 1
    assert result["attemptId"] == attempt["attemptId"]
    assert attempt["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert attempt["cost"] == 0.002
    assert "rawAnswer" not in attempt
    assert policy.summary()["totalAttemptMs"] == result["latencyMs"]


def test_failure_requires_fresh_observation_then_fallback_once_and_returns_to_primary():
    client = Client(failure(), decision(), decision())
    policy = DecisionPolicy(client, "jev-auto", clock=Clock())

    async def scenario():
        result = await policy.decide(snapshot(), 4)
        assert result == {"kind": "refresh_required", "fallbackPending": True}
        assert [item[0] for item in client.calls] == ["jev"]
        assert policy.summary()["pendingRefresh"] is True
        for stale in (4, 3):
            with pytest.raises(ObservationRefreshRequired):
                await policy.decide(snapshot(), stale)
        assert len(client.calls) == 1
        fresh = snapshot({"CLICK:n2": "fresh Like"})
        client.outcomes[0] = decision("CLICK:n2")
        fallback = await policy.decide(fresh, 5)
        assert fallback["selectedBackend"] == "deepseek"
        assert fallback["fallbackUsed"] is True
        assert fallback["choice"] == "CLICK:n2"
        assert fallback["attemptId"] == 2
        assert client.calls[-1][2] == fresh["options"]
        restored = await policy.decide(snapshot(), 6)
        assert restored["selectedBackend"] == "jev"
        assert restored["fallbackUsed"] is False

    run(scenario())
    summary = policy.summary()
    assert summary["primaryFailures"] == 1
    assert summary["fallbackRequests"] == 1
    assert summary["fallbackSuccesses"] == 1
    assert summary["pendingRefresh"] is False
    assert summary["totalAttemptMs"] == pytest.approx(75)


@pytest.mark.parametrize("bad", [None, {}, {"choice": "UNKNOWN"}, {"choice": 2}])
def test_invalid_primary_choice_requests_refresh(bad):
    policy = DecisionPolicy(Client(bad), "jev-auto")
    assert run(policy.decide(snapshot(), 1))["kind"] == "refresh_required"
    assert policy.attempts[0]["status"] == "invalid_choice"


@pytest.mark.parametrize("choice", ["WAIT", "BLOCKED"])
def test_valid_wait_or_blocked_does_not_fallback(choice):
    client = Client(decision(choice))
    policy = DecisionPolicy(client, "jev-auto")
    assert run(policy.decide(snapshot({choice: choice}), 1))["choice"] == choice
    assert policy.summary()["primaryFailures"] == 0
    assert len(client.calls) == 1


async def hang():
    await asyncio.sleep(1)
    return decision()


def test_primary_whole_call_deadline_requests_refresh():
    policy = DecisionPolicy(Client(hang), "jev-auto", primary_timeout=0.001)
    assert run(policy.decide(snapshot(), 1))["kind"] == "refresh_required"
    assert policy.attempts[0]["status"] == "timeout"
    assert policy.attempts[0]["stage"] == "decision_deadline"
    assert policy.attempts[0]["exceptionType"] == "TimeoutError"


@pytest.mark.parametrize("fallback", [failure(), {"choice": "stale"}, hang])
def test_failed_fallback_is_terminal_without_retry(fallback):
    client = Client(failure(), fallback, decision())
    policy = DecisionPolicy(client, "jev-auto", fallback_timeout=0.001)

    async def scenario():
        await policy.decide(snapshot(), 1)
        with pytest.raises(DecisionPolicyError):
            await policy.decide(snapshot(), 2)
        with pytest.raises(DecisionPolicyError):
            await policy.decide(snapshot(), 3)

    run(scenario())
    assert [item[0] for item in client.calls] == ["jev", "deepseek"]
    assert policy.summary()["fallbackSuccesses"] == 0
    assert policy.summary()["pendingRefresh"] is False


@pytest.mark.parametrize("mode", ["jev", "deepseek"])
def test_single_modes_never_fallback_or_apply_auto_deadline(mode):
    client = Client(failure())
    policy = DecisionPolicy(client, mode, primary_timeout=0.000001)
    with pytest.raises(ProviderError):
        run(policy.decide(snapshot(), 1))
    assert [item[0] for item in client.calls] == [mode]
    assert policy.summary()["fallbackRequests"] == 0
    assert not policy.summary()["pendingRefresh"]

    async def slow_success():
        await asyncio.sleep(0.003)
        return decision()

    assert (
        run(
            DecisionPolicy(Client(slow_success), mode, primary_timeout=0.000001).decide(
                snapshot(), 1
            )
        )["selectedBackend"]
        == mode
    )


def test_cancelled_primary_propagates_and_never_calls_fallback():
    client = Client(asyncio.CancelledError(), decision())
    policy = DecisionPolicy(client, "jev-auto")
    with pytest.raises(asyncio.CancelledError):
        run(policy.decide(snapshot(), 1))
    assert len(client.calls) == 1
    assert not policy.summary()["pendingRefresh"]
    assert policy.attempts[0]["status"] == "cancelled"


def test_cancelled_fallback_propagates_without_another_attempt():
    client = Client(failure(), asyncio.CancelledError(), decision())
    policy = DecisionPolicy(client, "jev-auto")

    async def scenario():
        await policy.decide(snapshot(), 1)
        with pytest.raises(asyncio.CancelledError):
            await policy.decide(snapshot(), 2)
        with pytest.raises(DecisionPolicyError):
            await policy.decide(snapshot(), 3)

    run(scenario())
    assert len(client.calls) == 2


def test_unknown_exception_is_not_hidden_or_used_as_fallback_signal():
    error = RuntimeError("internal bug with secret text")
    client = Client(error)
    policy = DecisionPolicy(client, "jev-auto")
    with pytest.raises(RuntimeError) as caught:
        run(policy.decide(snapshot(), 1))
    assert caught.value is error
    assert not policy.summary()["pendingRefresh"]
    assert policy.attempts[0]["status"] == "internal_error"


def test_internal_timeout_is_not_mistaken_for_policy_deadline():
    error = TimeoutError("internal timeout does not imply our deadline expired")
    policy = DecisionPolicy(Client(error), "jev-auto")
    with pytest.raises(TimeoutError) as caught:
        run(policy.decide(snapshot(), 1))
    assert caught.value is error
    assert not policy.summary()["pendingRefresh"]
    assert policy.attempts[0]["status"] == "internal_error"


def test_external_task_cancellation_is_audited_and_not_fallback():
    entered = asyncio.Event()

    async def waiting():
        entered.set()
        await asyncio.sleep(10)

    client = Client(waiting)
    policy = DecisionPolicy(client, "jev-auto")

    async def scenario():
        task = asyncio.create_task(policy.decide(snapshot(), 1))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(DecisionPolicyError):
            await policy.decide(snapshot(), 2)

    run(scenario())
    assert len(client.calls) == 1
    assert len(policy.attempts) == 1
    assert policy.attempts[0]["status"] == "cancelled"
    assert policy.summary()["primaryFailures"] == 0


@pytest.mark.parametrize("mode", ["jev", "deepseek"])
def test_invalid_choice_in_single_mode_terminates_without_fallback(mode):
    client = Client({"choice": "not offered"}, decision())
    policy = DecisionPolicy(client, mode)
    with pytest.raises(DecisionPolicyError):
        run(policy.decide(snapshot(), 1))
    with pytest.raises(DecisionPolicyError):
        run(policy.decide(snapshot(), 2))
    assert len(client.calls) == 1
    assert not policy.summary()["pendingRefresh"]


@pytest.mark.parametrize("budget", [0, -1, math.inf, math.nan, True, "5", None])
@pytest.mark.parametrize("field", ["primary_timeout", "fallback_timeout"])
def test_invalid_budget_rejected_before_any_request(field, budget):
    client = Client()
    with pytest.raises(ValueError):
        DecisionPolicy(client, "jev-auto", **{field: budget})
    assert client.calls == []


def test_mode_and_observation_id_validated_before_request():
    client = Client()
    with pytest.raises(ValueError):
        DecisionPolicy(client, "other")
    policy = DecisionPolicy(client, "jev-auto")
    for invalid in (None, True, -1, "2", 1.5):
        with pytest.raises(ValueError):
            run(policy.decide(snapshot(), invalid))
    assert client.calls == []


def test_attempts_and_summary_do_not_leak_error_request_or_headers():
    secret = "sk-or-sensitive-test-value"
    error = ProviderError(
        secret,
        network={
            "connectMs": 2,
            "headers": {"Authorization": secret},
            "httpVersion": secret,
            "requestToHeadersMs": secret,
        },
        latency_ms=10,
        exception_type=secret,
        stage=secret,
    )
    policy = DecisionPolicy(Client(error), "jev-auto")
    run(policy.decide(snapshot(), 1))
    serialized = json.dumps(policy.summary())
    assert secret not in serialized
    assert "headers" not in serialized
    assert policy.attempts[0]["network"] == {"connectMs": 2}
    assert policy.attempts[0]["exceptionType"] == "ProviderError"
    assert policy.attempts[0]["stage"] == "provider_request"


def test_summary_is_a_copy_and_does_not_mutate_policy_audit():
    policy = DecisionPolicy(Client(decision()), "jev-auto")
    run(policy.decide(snapshot(), 1))
    summary = policy.summary()
    summary["attempts"][0]["network"]["httpVersion"] = "changed"
    assert policy.attempts[0]["network"]["httpVersion"] == "HTTP/2"
