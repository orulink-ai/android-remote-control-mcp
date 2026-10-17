"""All requests are local MockTransport exchanges; no model/device/key lookup."""

import asyncio
import importlib.util
import json
import sys
import types

import httpx
import pytest

import providers

KEY = "sk-or-offline-only-secret"
OPTIONS = {"CLICK:n1": "点击搜索", "TYPE:n2": "输入小狮日记", "WAIT": "等待", "BLOCKED": "无法继续"}
STATE = {
    "goal": "搜索",
    "visibleControls": [{"id": "n1", "label": "搜索", "checked": False}],
    "options": OPTIONS,
}


@pytest.fixture(autouse=True)
def mock_h2_import_only(monkeypatch):
    # HTTPX checks h2 availability even with MockTransport. This sentinel is
    # exclusively for offline mocks; no HTTP/2 implementation/network is invoked.
    if importlib.util.find_spec("h2") is None:
        monkeypatch.setitem(sys.modules, "h2", types.ModuleType("h2"))


def answer(options, choice):
    return {
        "type": "choice",
        "choice": choice,
        "confidence": 1.0,
        "probabilities": {key: float(key == choice) for key in options},
    }


def reply(request):
    body = json.loads(request.content)
    if "questions" in body:
        answers = {}
        for name, question in body["questions"].items():
            keys = list(question["criteria"])
            answers[name] = answer(keys, keys[0])
        return {
            "model": providers.JEV_MODEL,
            "answers": answers,
            "usage": {"input_tokens": 37, "output_tokens": 4, "cost": 0.001, "extra": 5},
        }
    return {
        "model": providers.DEEPSEEK_MODEL,
        "choices": [{"finish_reason": "stop", "message": {"content": '{"choice":"CLICK:n1"}'}}],
        "usage": {"prompt_tokens": 39, "completion_tokens": 5, "cost": 0.002},
    }


class TrackingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []
        self.closes = 0

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json=reply(request), extensions={"http_version": b"HTTP/2"})

    async def aclose(self):
        self.closes += 1


@pytest.mark.parametrize("mode,expected_count,http2", [("warm", 1, True), ("cold", 2, False)])
def test_session_reuses_warm_client_closes_owned_clients_but_not_borrowed_transport(
    monkeypatch, mode, expected_count, http2
):
    clients, flags = [], []
    real_client = httpx.AsyncClient

    def create(**kwargs):
        flags.append(kwargs["http2"])
        client = real_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(providers.httpx, "AsyncClient", create)
    transport = TrackingTransport()

    async def scenario():
        client = providers.DecisionClient(KEY, mode=mode, transport=transport)
        await client.decide("jev", STATE, OPTIONS)
        await client.decide("deepseek", STATE, OPTIONS)
        if mode == "warm":
            assert not clients[0].is_closed
        await client.close()
        await client.close()
        with pytest.raises(ValueError, match="closed"):
            await client.decide("jev", STATE, OPTIONS)

    asyncio.run(scenario())
    assert len(clients) == expected_count
    assert flags == [http2] * expected_count
    assert all(client.is_closed for client in clients)
    assert transport.closes == 0  # injected transport remains caller-owned


def test_missing_trace_phases_stay_unknown_and_real_usage_is_preserved():
    transport = TrackingTransport()

    async def scenario():
        client = providers.DecisionClient(KEY, transport=transport)
        try:
            result = await client.decide("jev", STATE, OPTIONS)
            assert result["network"] == {
                "connectMs": None,
                "tlsMs": None,
                "requestToHeadersMs": None,
                "downloadMs": None,
                "httpVersion": "HTTP/2",
                "newConnection": None,
                "totalMs": result["network"]["totalMs"],
            }
            assert result["network"]["totalMs"] >= 0
            assert result["usage"] == {
                "input_tokens": 37,
                "output_tokens": 4,
                "cost": 0.001,
                "extra": 5,
            }
            assert result["cost"] == 0.001
            assert result["servedModel"] == providers.JEV_MODEL
        finally:
            await client.close()

    asyncio.run(scenario())


def test_network_trace_uses_phase_boundaries_not_inference_or_dns_claims():
    ticks = iter([1, 1.010, 1.020, 1.040, 1.050, 1.060, 1.100, 1.110, 1.130])
    trace = providers.NetworkTrace(clock=lambda: next(ticks))

    async def scenario():
        for name in [
            "connection.connect_tcp.started",
            "connection.connect_tcp.complete",
            "connection.start_tls.started",
            "connection.start_tls.complete",
            "http2.send_request_headers.started",
            "http2.receive_response_headers.started",
            "http2.receive_response_headers.complete",
            "http2.receive_response_body.started",
            "http2.receive_response_body.complete",
        ]:
            await trace(name, {"secret": KEY})

    asyncio.run(scenario())
    metrics = trace.metrics(total_ms=145, http_version="HTTP/2")
    assert metrics["connectMs"] == pytest.approx(10)
    assert metrics["tlsMs"] == pytest.approx(20)
    assert metrics["requestToHeadersMs"] == pytest.approx(50)
    assert metrics["downloadMs"] == pytest.approx(20)
    assert metrics["newConnection"] is True
    assert KEY not in repr(trace.__dict__)


def test_warm_trace_reports_reuse_only_with_request_events_and_no_connect():
    ticks = iter([2, 2.01, 2.02])
    trace = providers.NetworkTrace(clock=lambda: next(ticks))

    async def scenario():
        await trace("http11.send_request_headers.started", {})
        await trace("http11.receive_response_headers.started", {})
        await trace("http11.receive_response_headers.complete", {})

    asyncio.run(scenario())
    result = trace.metrics(total_ms=21, http_version="HTTP/1.1")
    assert result["newConnection"] is False
    assert result["connectMs"] is None
    assert result["tlsMs"] is None
    assert result["downloadMs"] is None


@pytest.mark.parametrize(
    "operation,choice",
    [
        ("CLICK", "CLICK:n1"),
        ("TYPE", "TYPE:n2"),
        ("WAIT", "WAIT"),
        ("BLOCKED", "BLOCKED"),
        ("OPEN_APP", "OPEN_APP"),
    ],
)
def test_typed_heads_map_only_the_selected_operation_back_to_original_choice(operation, choice):
    options = {**OPTIONS, "OPEN_APP": "打开小红书"}
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        questions = payload["questions"]
        answers = {"operation": answer(questions["operation"]["criteria"], operation)}
        name = operation.lower() + "_target"
        if name in questions:
            answers[name] = answer(questions[name]["criteria"], choice)
        return httpx.Response(200, json={"answers": answers, "model": providers.JEV_MODEL})

    async def scenario():
        async with providers.DecisionClient(
            KEY, transport=httpx.MockTransport(respond), decisionStyle="heads"
        ) as client:
            return await client.decide("jev", STATE, options)

    result = asyncio.run(scenario())
    assert result["choice"] == choice
    assert "click_target" in requests[0]["questions"]
    assert "type_target" in requests[0]["questions"]
    assert "wait_target" not in requests[0]["questions"]


def test_compact_context_is_shared_and_does_not_repeat_option_labels_in_state():
    transport = TrackingTransport()

    async def scenario():
        async with providers.DecisionClient(
            KEY, compact=True, transport=transport, decisionStyle="flat"
        ) as client:
            await client.decide("jev", STATE, OPTIONS)
            await client.decide("deepseek", STATE, OPTIONS)

    asyncio.run(scenario())
    j, d = [json.loads(request.content) for request in transport.requests]
    envelope = json.loads(d["messages"][1]["content"])
    assert j["state"] == envelope["state"]
    assert "options" not in j["state"]
    assert "label" not in j["state"]["visibleControls"][0]
    assert j["state"]["visibleControls"][0]["checked"] is False
    assert j["questions"]["action"]["criteria"] == envelope["options"] == OPTIONS
    assert j["questions"]["action"]["instructions"] == d["messages"][0]["content"]
    assert d["reasoning"] == {"enabled": False}
    assert d["provider"]["allow_fallbacks"] is False
    assert d["response_format"]["json_schema"]["schema"]["properties"]["choice"]["enum"] == list(
        OPTIONS
    )
    assert STATE["visibleControls"][0]["label"] == "搜索"


@pytest.mark.parametrize("provider", ["jev", "deepseek"])
def test_unsafe_error_is_sanitized_without_retry_or_credential_leak(provider):
    calls = []

    def reject(request):
        calls.append(request)
        return httpx.Response(429, text=KEY + " secret response")

    async def scenario():
        async with providers.DecisionClient(KEY, transport=httpx.MockTransport(reject)) as client:
            with pytest.raises(providers.ProviderError) as failure:
                await client.decide(provider, STATE, OPTIONS)
            assert KEY not in str(failure.value)
            assert "secret response" not in str(failure.value)
            assert failure.value.network["connectMs"] is None

    asyncio.run(scenario())
    assert len(calls) == 1


def test_bad_choice_and_credentials_in_response_cannot_escape():
    calls = []

    def respond(request):
        calls.append(request)
        body = reply(request)
        body["headers"] = {"Authorization": KEY}
        body["api_key"] = KEY
        body["diagnostic"] = KEY
        return httpx.Response(200, json=body)

    async def scenario():
        async with providers.DecisionClient(KEY, transport=httpx.MockTransport(respond)) as client:
            good = await client.decide("jev", STATE, OPTIONS)
            assert KEY not in json.dumps(good)
            assert "headers" not in good["rawAnswer"]

        def invented(request):
            body = reply(request)
            body["answers"]["action"]["choice"] = "invented"
            return httpx.Response(200, json=body)

        async with providers.DecisionClient(KEY, transport=httpx.MockTransport(invented)) as client:
            with pytest.raises(providers.ProviderError):
                await client.decide("jev", STATE, OPTIONS)

    asyncio.run(scenario())


def test_incomplete_trace_pair_is_unknown_not_zero():
    trace = providers.NetworkTrace(clock=lambda: 1)
    asyncio.run(trace("connection.connect_tcp.started", {}))
    result = trace.metrics(total_ms=1, http_version=None)
    assert result["connectMs"] is None
    assert result["newConnection"] is True


def test_trace_is_attached_to_actual_requests_and_warm_reuse_is_not_assumed():
    calls = []

    async def respond(request):
        trace = request.extensions["trace"]
        calls.append(request)
        if len(calls) == 1:
            for name in [
                "connection.connect_tcp.started",
                "connection.connect_tcp.complete",
                "connection.start_tls.started",
                "connection.start_tls.complete",
            ]:
                await trace(name, {})
        for name in [
            "http2.send_request_headers.started",
            "http2.receive_response_headers.started",
            "http2.receive_response_headers.complete",
            "http2.receive_response_body.started",
            "http2.receive_response_body.complete",
        ]:
            await trace(name, {})
        return httpx.Response(200, json=reply(request), extensions={"http_version": b"HTTP/2"})

    async def scenario():
        async with providers.DecisionClient(KEY, transport=httpx.MockTransport(respond)) as client:
            first = await client.decide("jev", STATE, OPTIONS)
            second = await client.decide("deepseek", STATE, OPTIONS)
        assert first["network"]["newConnection"] is True
        assert second["network"]["newConnection"] is False
        assert first["network"]["tlsMs"] >= 0
        assert second["network"]["tlsMs"] is None
        assert second["network"]["requestToHeadersMs"] >= 0
        assert second["network"]["httpVersion"] == "HTTP/2"

    asyncio.run(scenario())


def test_cold_client_is_closed_on_failure_and_cancellation(monkeypatch):
    created = []
    real_client = httpx.AsyncClient

    def create(**kwargs):
        client = real_client(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(providers.httpx, "AsyncClient", create)

    async def reject(request):
        raise httpx.ReadTimeout("unsafe " + KEY, request=request)

    async def pending(request):
        raise asyncio.CancelledError()

    async def scenario():
        async with providers.DecisionClient(
            KEY, mode="cold", transport=httpx.MockTransport(reject)
        ) as client:
            with pytest.raises(providers.ProviderError):
                await client.decide("jev", STATE, OPTIONS)
        async with providers.DecisionClient(
            KEY, mode="cold", transport=httpx.MockTransport(pending)
        ) as client:
            with pytest.raises(asyncio.CancelledError):
                await client.decide("deepseek", STATE, OPTIONS)

    asyncio.run(scenario())
    assert len(created) == 2
    assert all(client.is_closed for client in created)


def test_noncompact_flat_mode_retains_the_frozen_baseline_envelope():
    transport = TrackingTransport()

    async def scenario():
        async with providers.DecisionClient(
            KEY, compact=False, decisionStyle="flat", transport=transport
        ) as client:
            await client.decide("jev", STATE, OPTIONS)

    asyncio.run(scenario())
    payload = json.loads(transport.requests[0].content)
    assert payload["state"] == {"state": STATE, "options": OPTIONS}
    assert list(payload["questions"]) == ["action"]


def test_heads_rejects_cross_operation_target_and_never_falls_back():
    calls = []

    def wrong_target(request):
        payload = json.loads(request.content)
        calls.append(request)
        questions = payload["questions"]
        answers = {
            "operation": answer(questions["operation"]["criteria"], "TYPE"),
            "type_target": answer(["CLICK:n1"], "CLICK:n1"),
        }
        return httpx.Response(200, json={"answers": answers})

    async def scenario():
        async with providers.DecisionClient(
            KEY, decisionStyle="heads", transport=httpx.MockTransport(wrong_target)
        ) as client:
            with pytest.raises(providers.ProviderError):
                await client.decide("jev", STATE, OPTIONS)

    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "kwargs", [{"mode": "invalid"}, {"decisionStyle": "invalid"}, {"compact": "yes"}]
)
def test_invalid_session_configuration_never_creates_a_client(kwargs):
    with pytest.raises(ValueError):
        providers.DecisionClient(KEY, **kwargs)


def test_tls_failure_preserves_safe_exception_type_and_phase():
    async def reject(request):
        await request.extensions["trace"]("connection.start_tls.started", {})
        raise httpx.ConnectError(
            "unsafe " + KEY + " https://example/?token=secret", request=request
        )

    async def scenario():
        async with providers.DecisionClient(KEY, transport=httpx.MockTransport(reject)) as client:
            with pytest.raises(providers.ProviderError) as failed:
                await client.decide("jev", STATE, OPTIONS)
            assert failed.value.exception_type == "ConnectError"
            assert failed.value.stage == "tls_handshake"
            assert "ConnectError" in str(failed.value)
            assert "secret" not in str(failed.value)
            assert KEY not in str(failed.value)

    asyncio.run(scenario())


def test_invalid_response_preserves_validation_phase_without_raw_body():
    async def scenario():
        async with providers.DecisionClient(
            KEY, transport=httpx.MockTransport(lambda request: httpx.Response(200, text=KEY))
        ) as client:
            with pytest.raises(providers.ProviderError) as failed:
                await client.decide("jev", STATE, OPTIONS)
            assert failed.value.exception_type == "JSONDecodeError"
            assert failed.value.stage == "validate_response"
            assert KEY not in str(failed.value)

    asyncio.run(scenario())
