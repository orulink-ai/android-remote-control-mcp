"""Same-model routing is opt-in for terminal; no local retries or real APIs."""

import asyncio
import io
import json

import httpx
import pytest

from decision_policy import DecisionPolicy, DecisionPolicyError
from providers import DecisionClient, ProviderError, _chat_payload
from test_providers import KEY, OPTIONS, STATE, reply
from test_worker_policy import PhoneDouble
import worker


def test_payload_provider_fallback_default_is_frozen_but_can_be_enabled():
    assert _chat_payload(STATE, OPTIONS)["provider"]["allow_fallbacks"] is False
    assert _chat_payload(STATE, OPTIONS, providerFallbacks=True)["provider"]["allow_fallbacks"] is True


@pytest.mark.parametrize("value", [None, "true", 0, 1])
def test_provider_fallback_configuration_rejects_non_boolean(value):
    with pytest.raises(ValueError):
        DecisionClient(KEY, providerFallbacks=value)
    with pytest.raises(ValueError):
        _chat_payload(STATE, OPTIONS, providerFallbacks=value)


@pytest.mark.parametrize("configured", [False, True])
def test_decision_client_sets_same_model_routing_in_exactly_one_request(configured):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply(request))
    async def scenario():
        async with DecisionClient(KEY, mode="cold", providerFallbacks=configured,
                                  transport=httpx.MockTransport(respond)) as client:
            await client.decide("deepseek", STATE, OPTIONS)
    asyncio.run(scenario())
    assert len(requests) == 1
    assert requests[0]["provider"]["allow_fallbacks"] is configured
    assert requests[0]["model"] == "deepseek/deepseek-v4.1-flash"


def test_http429_only_safe_numeric_status_reaches_provider_policy_and_worker():
    calls = []
    def reject(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"message": "private-secret-provider-body"}})
    async def scenario():
        async with DecisionClient(KEY, mode="cold", transport=httpx.MockTransport(reject)) as client:
            policy = DecisionPolicy(client, "deepseek")
            with pytest.raises(ProviderError) as caught:
                await policy.decide({"state": STATE, "options": OPTIONS}, 1)
            assert caught.value.httpStatus == 429
            message, details = worker.safe_error(caught.value, "decide")
            assert details["httpStatus"] == 429
            assert policy.attempts[0]["httpStatus"] == 429
            assert "private-secret" not in json.dumps([message, details, policy.summary()])
    asyncio.run(scenario())
    assert len(calls) == 1


def test_http429_survives_terminal_policy_failure_without_raw_exception():
    class Client:
        async def decide(self, *args):
            raise ProviderError("private-secret", network={}, latency_ms=1,
                                exception_type="HTTPStatusError", stage="http_status", http_status=429)
    async def scenario():
        policy = DecisionPolicy(Client(), "jev-auto")
        state = {"state": STATE, "options": OPTIONS}
        await policy.decide(state, 1)
        with pytest.raises(DecisionPolicyError) as caught:
            await policy.decide(state, 2)
        message, details = worker.safe_error(caught.value, "decide")
        assert details["httpStatus"] == 429
        assert [attempt["httpStatus"] for attempt in policy.attempts] == [429, 429]
        assert "private-secret" not in json.dumps([message, details, policy.summary()])
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["429 secret", True, 429.0, -1, 999])
def test_unsafe_status_metadata_is_not_saved_or_forwarded(bad):
    error = ProviderError("private-secret", network={}, latency_ms=1,
                          exception_type="HTTPStatusError", stage="http_status")
    error.httpStatus = bad
    class Client:
        async def decide(self, *args):
            raise error
    async def scenario():
        policy = DecisionPolicy(Client(), "deepseek")
        with pytest.raises(ProviderError):
            await policy.decide({"state": STATE, "options": OPTIONS}, 1)
        assert "httpStatus" not in policy.attempts[0]
        assert "httpStatus" not in worker.safe_error(error, "decide")[1]
    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", [False, True])
def test_worker_enables_provider_routing_only_for_terminal_tasks(tmp_path, monkeypatch, terminal):
    observed = []
    if terminal:
        monkeypatch.setenv("PHONE_TERMINAL_TASK", json.dumps({"kind": "open", "goal": "打开小红书", "appName": "小红书"}))
    else:
        monkeypatch.delenv("PHONE_TERMINAL_TASK", raising=False)
    for field in ("fail_action", "fail_readback", "parse_after_action"):
        monkeypatch.setattr(PhoneDouble, field, False)
    class Client:
        def __init__(self, key, **options):
            observed.append(options)
            self.mode, self.decisionStyle = "warm", "heads"
        async def decide(self, backend, state, options):
            return {"choice": next(iter(options)), "latencyMs": 0, "usage": {}}
        async def close(self):
            pass
    requests = [{"command": "observe"}, {"command": "decide"},
                {"command": "act", "choice": "CLICK:node-1"}, {"command": "finish"}]
    asyncio.run(worker.serve("terminal-auto" if terminal else "jev-auto", "routing", base=tmp_path,
        input_stream=io.StringIO("\n".join(json.dumps(request) for request in requests)),
        output_stream=io.StringIO(), runtime_factory=PhoneDouble, client_factory=Client,
        key_reader=lambda: "offline-key"))
    assert observed[0].get("providerFallbacks", False) is terminal
