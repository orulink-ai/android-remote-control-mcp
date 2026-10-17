"""Auditable Jev-first decisions with an explicit read-before-fallback barrier.

This module never observes or operates a phone. A refresh marker means the caller
must obtain a new successful observation before calling decide again. A provider
failure cannot itself dispatch a fallback action against stale screen controls.
"""

from __future__ import annotations

import asyncio
import copy
import math
import time

from providers import ProviderError


class DecisionPolicyError(RuntimeError):
    """The policy cannot safely produce another decision for this session."""


class ObservationRefreshRequired(DecisionPolicyError):
    """A successful newer observation is required before fallback is permitted."""


_SAFE_EXCEPTION_TYPES = {
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "HTTPStatusError",
    "JSONDecodeError",
    "ValueError",
    "TimeoutError",
    "ImportError",
    "InvalidURL",
    "ReadError",
    "WriteError",
    "CloseError",
    "LocalProtocolError",
    "DecodingError",
    "UnsupportedProtocol",
}
_SAFE_STAGES = {
    "create_client",
    "create_request",
    "send_request",
    "read_response",
    "http_status",
    "validate_response",
    "tls_handshake",
    "tcp_connect",
}
_NETWORK_TIMINGS = {
    "connectMs",
    "tlsMs",
    "requestToHeadersMs",
    "downloadMs",
    "totalMs",
}
_USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "cost",
}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _network(value):
    """Only known scalar metrics cross the audit boundary, never HTTP payloads."""
    if not isinstance(value, dict):
        return {}
    result = {
        key: item
        for key, item in value.items()
        if key in _NETWORK_TIMINGS and (item is None or _number(item))
    }
    version = value.get("httpVersion")
    if version is None or (
        isinstance(version, str) and version in {"HTTP/1.0", "HTTP/1.1", "HTTP/2", "HTTP/3"}
    ):
        if "httpVersion" in value:
            result["httpVersion"] = value["httpVersion"]
    if value.get("newConnection") is None or type(value.get("newConnection")) is bool:
        if "newConnection" in value:
            result["newConnection"] = value["newConnection"]
    return result


class DecisionPolicy:
    """Single-model control, or one bounded DeepSeek fallback per failed Jev step.

    ``latencyMs`` measures the whole awaited provider call using the injected
    monotonic clock. Returned successful decisions and audit records use exactly
    the same duration, so callers can account for successes and failures once.
    Only auto mode applies request deadlines. Cancellation and internal errors
    propagate directly and terminate the policy without selecting another model.
    """

    def __init__(
        self,
        client,
        mode,
        *,
        primary_timeout=5.0,
        fallback_timeout=15.0,
        clock=time.perf_counter,
    ):
        if mode not in {"jev", "deepseek", "jev-auto"}:
            raise ValueError("Unsupported decision policy mode")
        for budget in (primary_timeout, fallback_timeout):
            if type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0:
                raise ValueError("Decision deadlines must be finite positive seconds")
        self.client = client
        self.mode = mode
        self.primary_timeout = primary_timeout
        self.fallback_timeout = fallback_timeout
        self.clock = clock
        self.attempts = []
        self._pending_id = None
        self._terminal = False
        self._lock = asyncio.Lock()

    async def decide(self, snapshot, observation_id):
        if type(observation_id) is not int or observation_id < 0:
            raise ValueError("Observation ID must be a nonnegative integer")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("state"), dict):
            raise ValueError("A current state snapshot is required")
        options = snapshot.get("options")
        if (
            not isinstance(options, dict)
            or not options
            or any(
                not isinstance(key, str) or not key or not isinstance(label, str)
                for key, label in options.items()
            )
        ):
            raise ValueError("A nonempty choice mapping is required")
        # The offered choices cannot be altered by the caller while awaiting IO.
        state, options = copy.deepcopy(snapshot["state"]), dict(options)
        async with self._lock:
            if self._terminal:
                raise DecisionPolicyError("Decision session terminated; start a new session")
            fallback = self._pending_id is not None
            if fallback and observation_id <= self._pending_id:
                raise ObservationRefreshRequired("A newer successful observation is required")
            backend = "deepseek" if fallback else "jev" if self.mode == "jev-auto" else self.mode
            result, failure = await self._attempt(backend, state, options)
            if failure is None:
                self._pending_id = None
                return {
                    **result,
                    "kind": "decision",
                    "selectedBackend": backend,
                    "fallbackUsed": fallback,
                }
            if self.mode == "jev-auto" and backend == "jev":
                self._pending_id = observation_id
                return {"kind": "refresh_required", "fallbackPending": True}
            self._pending_id = None
            self._terminal = True
            if self.mode != "jev-auto" and isinstance(failure, ProviderError):
                raise failure
            raise DecisionPolicyError("Decision request failed; no action selected") from None

    async def _attempt(self, backend, state, options):
        started = self.clock()
        deadline = None
        result, failure = None, None
        audit = {
            "attemptId": len(self.attempts) + 1,
            "backend": backend,
            "status": "success",
            "network": {},
        }
        try:
            if self.mode == "jev-auto":
                budget = self.primary_timeout if backend == "jev" else self.fallback_timeout
                deadline = asyncio.timeout(budget)
                async with deadline:
                    result = await self.client.decide(backend, state, options)
            else:
                result = await self.client.decide(backend, state, options)
            if isinstance(result, dict):
                audit["network"] = _network(result.get("network"))
            if (
                not isinstance(result, dict)
                or not isinstance(result.get("choice"), str)
                or result["choice"] not in options
            ):
                audit.update(
                    status="invalid_choice", exceptionType="ValueError", stage="validate_choice"
                )
                failure = DecisionPolicyError("Unavailable model choice")
            else:
                usage = result.get("usage")
                if isinstance(usage, dict):
                    audit["usage"] = {
                        key: value
                        for key, value in usage.items()
                        if key in _USAGE_KEYS and _number(value)
                    }
                if _number(result.get("cost")):
                    audit["cost"] = result["cost"]
        except asyncio.CancelledError:
            self._terminal, self._pending_id = True, None
            audit.update(status="cancelled", exceptionType="CancelledError", stage="decision")
            raise
        except ProviderError as error:
            failure = error
            audit.update(
                status="provider_error",
                network=_network(error.network),
                exceptionType=error.exception_type
                if error.exception_type in _SAFE_EXCEPTION_TYPES
                else "ProviderError",
                stage=error.stage if error.stage in _SAFE_STAGES else "provider_request",
            )
        except Exception as error:
            if isinstance(error, TimeoutError) and deadline is not None and deadline.expired():
                failure = error
                audit.update(
                    status="timeout", exceptionType="TimeoutError", stage="decision_deadline"
                )
            else:
                self._terminal, self._pending_id = True, None
                # Class names and exception messages can be application-defined.
                audit.update(
                    status="internal_error", exceptionType="InternalError", stage="decision"
                )
                raise
        finally:
            audit["latencyMs"] = max(0.0, (self.clock() - started) * 1000)
            self.attempts.append(audit)
        if failure is None:
            result = {**result, "latencyMs": audit["latencyMs"], "attemptId": audit["attemptId"]}
        return result, failure

    def summary(self):
        failures = {"provider_error", "timeout", "invalid_choice"}
        fallback_attempts = self.attempts if self.mode == "jev-auto" else []
        return {
            "mode": self.mode,
            "primaryFailures": sum(
                item["backend"] == "jev" and item["status"] in failures for item in self.attempts
            ),
            "fallbackRequests": sum(item["backend"] == "deepseek" for item in fallback_attempts),
            "fallbackSuccesses": sum(
                item["backend"] == "deepseek" and item["status"] == "success"
                for item in fallback_attempts
            ),
            "pendingRefresh": self._pending_id is not None,
            "totalAttemptMs": sum(item["latencyMs"] for item in self.attempts),
            "attempts": copy.deepcopy(self.attempts),
        }
