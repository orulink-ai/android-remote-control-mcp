"""PI-owned phone protocol with explicit observation before model fallback.

The decision policy cannot execute phone tools. Only PI's observe/act requests
enter the phone runtime, and every action must match a decision for its current
observation. Uncertain action delivery is read back once and then halted.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
import json
import os
from pathlib import Path
import sys

from config import decision_timeouts, load_openrouter_key, output_dir
from controller import PhoneRuntime, private_write
from decision_policy import DecisionPolicy, DecisionPolicyError
from own_mcp import McpError
from providers import DecisionClient, ProviderError
from worker_io import cancellation_scope, protocol_lines


def safe_error(error, command):
    """No response bodies or arbitrary exception messages cross the IPC boundary."""
    if isinstance(error, ProviderError):
        return "Model request failed; no action selected", {
            "exceptionType": error.exception_type,
            "stage": error.stage,
        }
    if isinstance(error, McpError):
        return "MCP request failed; execution stopped", {
            "exceptionType": error.exception_type,
            "stage": error.stage,
        }
    if isinstance(error, DecisionPolicyError):
        return "Phone decision policy stopped", {
            "exceptionType": "DecisionPolicyError",
            "stage": "decision_policy",
        }
    return "Phone worker command failed", {"exceptionType": "InternalError", "stage": command}


def record_failed_attempts(runtime, attempts):
    for attempt in attempts:
        if attempt["status"] == "success":
            continue
        runtime.operations.append(
            {
                "name": "model_request_failure",
                **attempt,
                "ms": attempt["latencyMs"],
            }
        )
        runtime.phases.append(
            {
                "kind": "model",
                "name": "模型请求失败",
                "ms": attempt["latencyMs"],
            }
        )


def reconcile_action(runtime):
    """Observe once after uncertain dispatch, without authorizing another action."""
    recovery = {"name": "action_reconciliation", "status": "failed", "replayed": False}
    try:
        runtime.observe()
        recovery.update(status="observed", observation=runtime.index)
    except Exception as error:
        _, recovery["errorDetails"] = safe_error(error, "reconciliation_observe")
    runtime.operations.append(recovery)


def remember_original(snapshot, base):
    if snapshot.get("state", {}).get("observation", {}).get("kind") != "detail":
        return
    original = base / "original-post-state.json"
    if not original.exists():
        private_write(
            original, json.dumps(snapshot["state"]["observation"], ensure_ascii=False, indent=2)
        )


async def serve(
    provider,
    trial_id,
    *,
    input_stream=None,
    output_stream=None,
    runtime_factory=PhoneRuntime,
    client_factory=DecisionClient,
    key_reader=load_openrouter_key,
    base=None,
):
    """Run the actual protocol; injectable dependencies keep tests device-free."""
    if provider not in {"jev", "deepseek", "jev-auto"}:
        raise ValueError("Unsupported phone decision mode")
    budgets = decision_timeouts() if provider == "jev-auto" else (5.0, 15.0)
    base = Path(base) if base is not None else output_dir()
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    client = client_factory(
        key_reader(),
        mode=os.environ.get("PHONE_BENCH_MODE", "warm"),
        compact=True,
        decisionStyle="heads",
    )
    policy = DecisionPolicy(
        client, provider, primary_timeout=budgets[0], fallback_timeout=budgets[1]
    )
    runtime, error_details = None, None
    finished = failed = False
    approved_choice = approved_observation = None

    def finish(pi_elapsed_ms=None, *, disconnected=False):
        if runtime is None:
            raise RuntimeError("No phone observation to finalize")
        if disconnected:
            runtime.error = runtime.error or "PI disconnected before finish"
        extra = {
            "provider": provider,
            "piElapsedMs": pi_elapsed_ms,
            "piAgentLoop": True,
            "clientMode": client.mode,
            "decisionStyle": client.decisionStyle,
            "decisionPolicy": policy.summary(),
            "errorDetails": error_details,
        }
        # Even a successful readback cannot turn a failed PI tool into a success.
        if failed or disconnected:
            extra["status"] = "failed"
        return runtime.finish(extra)

    try:
        async with aclosing(protocol_lines(input_stream)) as lines:
            async for line in lines:
                request, command = {}, "unknown"
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("Expected protocol object")
                    command = request.get("command", "unknown")
                    if failed and command != "finish":
                        raise RuntimeError("Worker stopped after an earlier failure")
                    if command == "observe":
                        approved_choice = approved_observation = None
                        if runtime is None:
                            runtime = runtime_factory(trial_id)
                        value = runtime.observe()
                        remember_original(value, base)
                    elif command == "decide":
                        if runtime is None or runtime.snapshot is None:
                            raise RuntimeError("Observe first")
                        if approved_choice is not None:
                            raise RuntimeError("Previous decision has not been consumed")
                        start = len(policy.attempts)
                        try:
                            value = await policy.decide(runtime.snapshot, runtime.index)
                        finally:
                            record_failed_attempts(runtime, policy.attempts[start:])
                        if value.get("kind") == "decision":
                            runtime.record_model(value)
                            approved_choice, approved_observation = value["choice"], runtime.index
                        # refresh_required is a reply to PI, never an internal phone call.
                    elif command == "act":
                        if (
                            runtime is None
                            or approved_choice is None
                            or request.get("choice") != approved_choice
                            or runtime.index != approved_observation
                            or policy.summary()["pendingRefresh"]
                        ):
                            raise RuntimeError("No decision authorized for this observation")
                        choice = approved_choice
                        approved_choice = approved_observation = None
                        try:
                            value = runtime.act(choice)
                        except Exception:
                            reconcile_action(runtime)
                            raise
                        remember_original(value, base)
                    elif command == "finish":
                        value = finish(request.get("piElapsedMs"))
                        finished = True
                    else:
                        raise ValueError("Unknown worker command")
                    response = {"id": request.get("id"), "result": value}
                except Exception as error:
                    failed = True
                    message, error_details = safe_error(error, command)
                    if runtime is not None:
                        runtime.error = message
                    response = {
                        "id": request.get("id") if isinstance(request, dict) else None,
                        "error": message,
                        "errorDetails": error_details,
                    }
                print(json.dumps(response, ensure_ascii=False), file=output_stream, flush=True)
                if finished:
                    break
    finally:
        try:
            if runtime is not None and not finished:
                finish(disconnected=True)
        finally:
            await client.close()


async def main(argv=None, **serve_kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["jev", "deepseek", "jev-auto"], required=True)
    parser.add_argument("--trial-id", required=True)
    args = parser.parse_args(argv)
    try:
        async with cancellation_scope():
            await serve(args.provider, args.trial_id, **serve_kwargs)
    except asyncio.CancelledError:
        return 143
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
