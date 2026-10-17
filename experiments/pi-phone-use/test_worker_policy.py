"""Exercise the actual worker protocol with injected phone/model doubles."""

import asyncio
import io
import json
import pytest

from own_mcp import McpError
from providers import ProviderError
import worker


def api_failure():
    return ProviderError(
        "fixture-private-value must not be exposed",
        network={"newConnection": True, "tlsMs": None},
        latency_ms=1,
        exception_type="ConnectError",
        stage="tls_handshake",
    )


class PhoneDouble:
    instances = []
    fail_action = False
    fail_readback = False
    parse_after_action = False

    def __init__(self, trial_id):
        self.instances.append(self)
        self.index = 0
        self.snapshot = None
        self.operations, self.phases, self.model_calls = [], [], []
        self.error = None
        self.actions = []
        self.completed = False
        self.result = None
        self.closed = False

    def observe(self):
        self.index += 1
        self.operations.append({"name": "android_get_screen_state", "status": "success"})
        if self.fail_readback and self.index > 1:
            raise McpError("read failed", stage="tool_call", exception_type="ReadTimeout")
        self.snapshot = {
            "state": {"observation": {"kind": "feed"}, "generation": self.index},
            "options": {f"CLICK:node-{self.index}": "like the target"},
            "done": self.completed,
        }
        return self.snapshot

    def act(self, choice):
        self.actions.append(choice)
        self.completed = True  # A delivered toggle may succeed before the timeout.
        if self.parse_after_action:
            raise ValueError("post-dispatch parse failed")
        if self.fail_action:
            raise McpError("uncertain delivery", stage="tool_call", exception_type="ReadTimeout")
        return self.observe()

    def record_model(self, value):
        self.model_calls.append(value)
        self.phases.append({"kind": "model", "name": "decision", "ms": value["latencyMs"]})

    def finish(self, extra=None):
        self.result = {
            "status": "success" if self.completed else "failed",
            "operations": self.operations,
            "phases": self.phases,
            "modelCalls": self.model_calls,
            "error": self.error,
            **(extra or {}),
        }
        self.closed = True
        return self.result


class ModelDouble:
    instances = []
    outcomes = []

    def __init__(self, key, **kwargs):
        self.instances.append(self)
        self.mode = kwargs["mode"]
        self.decisionStyle = kwargs["decisionStyle"]
        self.calls = []
        self.closed = False
        self.responses = iter(self.outcomes)

    async def decide(self, backend, state, options):
        self.calls.append((backend, state["generation"]))
        response = next(self.responses, None)
        if isinstance(response, BaseException):
            raise response
        return {
            "choice": response or next(iter(options)),
            "latencyMs": 1,
            "servedModel": backend + "-fixture",
            "usage": {},
            "cost": None,
        }

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_doubles(monkeypatch):
    PhoneDouble.instances = []
    ModelDouble.instances = []
    monkeypatch.setattr(PhoneDouble, "fail_action", False)
    monkeypatch.setattr(PhoneDouble, "fail_readback", False)
    monkeypatch.setattr(PhoneDouble, "parse_after_action", False)
    monkeypatch.setattr(ModelDouble, "outcomes", [])


async def drive(requests, tmp_path, provider="jev-auto"):
    output = io.StringIO()
    await worker.serve(
        provider,
        "fixture-run",
        input_stream=io.StringIO(
            "\n".join(json.dumps({"id": i, **r}) for i, r in enumerate(requests))
        ),
        output_stream=output,
        runtime_factory=PhoneDouble,
        client_factory=ModelDouble,
        key_reader=lambda: "fixture-key",
        base=tmp_path,
    )
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_fallback_requires_pi_observation_and_executes_only_new_target(tmp_path, monkeypatch):
    monkeypatch.setattr(ModelDouble, "outcomes", [api_failure(), None])
    replies = asyncio.run(
        drive(
            [
                {"command": "observe"},
                {"command": "decide"},
                {"command": "observe"},
                {"command": "decide"},
                {"command": "act", "choice": "CLICK:node-2"},
                {"command": "finish", "piElapsedMs": 17},
            ],
            tmp_path,
        )
    )
    assert replies[1]["result"]["kind"] == "refresh_required"
    assert replies[3]["result"]["selectedBackend"] == "deepseek"
    phone, model = PhoneDouble.instances[0], ModelDouble.instances[0]
    assert model.calls == [("jev", 1), ("deepseek", 2)]
    assert phone.actions == ["CLICK:node-2"]
    assert phone.result["status"] == "success"
    assert phone.result["decisionPolicy"]["fallbackSuccesses"] == 1
    assert len([p for p in phone.phases if p["kind"] == "model"]) == 2
    assert "fixture-private-value" not in json.dumps(phone.result)
    assert phone.closed and model.closed


@pytest.mark.parametrize(
    "next_request",
    [
        {"command": "decide"},
        {"command": "act", "choice": "CLICK:node-1"},
    ],
)
def test_pending_fallback_cannot_dispatch_or_decide_without_fresh_observe(
    tmp_path, monkeypatch, next_request
):
    monkeypatch.setattr(ModelDouble, "outcomes", [api_failure()])
    replies = asyncio.run(
        drive(
            [
                {"command": "observe"},
                {"command": "decide"},
                next_request,
                {"command": "finish"},
            ],
            tmp_path,
        )
    )
    assert "error" in replies[2]
    assert ModelDouble.instances[0].calls == [("jev", 1)]
    assert PhoneDouble.instances[0].actions == []
    assert PhoneDouble.instances[0].result["status"] == "failed"


@pytest.mark.parametrize("readback_fails", [False, True])
def test_uncertain_action_reads_once_then_stops_without_model_switch_or_replay(
    tmp_path, monkeypatch, readback_fails
):
    monkeypatch.setattr(PhoneDouble, "fail_action", True)
    monkeypatch.setattr(PhoneDouble, "fail_readback", readback_fails)
    replies = asyncio.run(
        drive(
            [
                {"command": "observe"},
                {"command": "decide"},
                {"command": "act", "choice": "CLICK:node-1"},
                {"command": "decide"},
                {"command": "finish"},
            ],
            tmp_path,
        )
    )
    phone, model = PhoneDouble.instances[0], ModelDouble.instances[0]
    assert "error" in replies[2] and "error" in replies[3]
    assert phone.index == 2
    assert phone.actions == ["CLICK:node-1"]
    assert model.calls == [("jev", 1)]
    assert phone.result["status"] == "failed"
    assert phone.result["decisionPolicy"]["fallbackRequests"] == 0
    recovery = next(op for op in phone.operations if op["name"] == "action_reconciliation")
    assert recovery["status"] == ("failed" if readback_fails else "observed")


def test_two_api_failures_stop_and_count_both_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(ModelDouble, "outcomes", [api_failure(), api_failure()])
    replies = asyncio.run(
        drive(
            [
                {"command": "observe"},
                {"command": "decide"},
                {"command": "observe"},
                {"command": "decide"},
                {"command": "decide"},
                {"command": "finish"},
            ],
            tmp_path,
        )
    )
    phone, model = PhoneDouble.instances[0], ModelDouble.instances[0]
    assert "error" in replies[3] and "error" in replies[4]
    assert model.calls == [("jev", 1), ("deepseek", 2)]
    assert phone.actions == [] and phone.result["status"] == "failed"
    policy = phone.result["decisionPolicy"]
    assert len(policy["attempts"]) == 2 and policy["fallbackSuccesses"] == 0
    assert sum(p["ms"] for p in phone.phases) == pytest.approx(policy["totalAttemptMs"])


def test_cancellation_closes_resources_without_starting_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(ModelDouble, "outcomes", [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(drive([{"command": "observe"}, {"command": "decide"}], tmp_path))
    assert ModelDouble.instances[0].calls == [("jev", 1)]
    assert ModelDouble.instances[0].closed and PhoneDouble.instances[0].closed
    assert PhoneDouble.instances[0].actions == []


def test_observe_after_decision_invalidates_the_old_action(tmp_path):
    replies = asyncio.run(
        drive(
            [
                {"command": "observe"},
                {"command": "decide"},
                {"command": "observe"},
                {"command": "act", "choice": "CLICK:node-1"},
                {"command": "finish"},
            ],
            tmp_path,
        )
    )
    assert "error" in replies[3]
    assert PhoneDouble.instances[0].actions == []


def test_non_mcp_exception_after_dispatch_also_reads_once_without_replaying(tmp_path, monkeypatch):
    monkeypatch.setattr(PhoneDouble, "parse_after_action", True)
    replies = asyncio.run(
        drive(
            [
                {"command": "observe"},
                {"command": "decide"},
                {"command": "act", "choice": "CLICK:node-1"},
                {"command": "finish"},
            ],
            tmp_path,
        )
    )
    phone = PhoneDouble.instances[0]
    assert "error" in replies[2]
    assert phone.actions == ["CLICK:node-1"] and phone.index == 2
    assert phone.result["status"] == "failed"
    assert phone.result["decisionPolicy"]["fallbackRequests"] == 0
