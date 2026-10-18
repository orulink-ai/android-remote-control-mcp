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
import re
import sys
import time

from config import decision_timeouts, load_openrouter_key, output_dir
from controller import PhoneRuntime, private_write
from decision_policy import DecisionPolicy, DecisionPolicyError
from own_mcp import McpError
from providers import DecisionClient, ProviderError, safe_http_status
from worker_io import cancellation_scope, protocol_lines


class LocalRecipeClient:
    """Skill drift stops locally; this mode has no network model dependency."""

    mode = "local"
    decisionStyle = "recipe"

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def safe_error(error, command):
    """No response bodies or arbitrary exception messages cross the IPC boundary."""
    from terminal_runtime import TerminalRuntimeError
    from experience_runtime import ExperienceRuntimeError
    from terminal_tasks import TerminalTaskError

    if isinstance(error, TerminalTaskError):
        return "手机任务未识别或超出当前支持范围；请描述具体操作。未执行手机操作。", {
            "exceptionType": "TerminalTaskError", "stage": "terminal_task", "code": "UNSUPPORTED_TASK",
        }
    if isinstance(error, ExperienceRuntimeError):
        return ExperienceRuntimeError.MESSAGES[error.code], {
            "exceptionType": "ExperienceRuntimeError", "stage": "experience_observation", "code": error.code,
        }
    if isinstance(error, TerminalRuntimeError):
        return TerminalRuntimeError.MESSAGES[error.code], {
            "exceptionType": "TerminalRuntimeError", "stage": "terminal_observation", "code": error.code,
        }
    if isinstance(error, ProviderError):
        details = {
            "exceptionType": error.exception_type,
            "stage": error.stage,
        }
        status = safe_http_status(getattr(error, "httpStatus", None))
        if status is not None:
            details["httpStatus"] = status
        return "Model request failed; no action selected", details
    if isinstance(error, McpError):
        return "MCP request failed; execution stopped", {
            "exceptionType": error.exception_type,
            "stage": error.stage,
        }
    if isinstance(error, DecisionPolicyError):
        details = {
            "exceptionType": "DecisionPolicyError",
            "stage": "decision_policy",
        }
        status = safe_http_status(getattr(error, "httpStatus", None))
        if status is not None:
            details["httpStatus"] = status
        return "Phone decision policy stopped", details
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


async def serve_terminal_rejection(trial_id, error, *, input_stream, output_stream, base, started):
    """Keep rejected task setup inside the IPC protocol, before any dependencies start."""
    message, details = safe_error(error, "terminal_task")
    result = {
        "trialId": trial_id, "status": "failed", "provider": "terminal-auto",
        "elapsedMs": 0, "piElapsedMs": None, "piAgentLoop": True,
        "modelCalls": [], "operations": [], "phases": [], "milestones": [],
        "finalState": None, "error": message, "errorDetails": details,
        "terminalSummary": {"kind": "unsupported", "backend": "terminal-auto"},
    }

    def persist(pi_elapsed_ms=None):
        result.update(elapsedMs=(time.perf_counter() - started) * 1000, piElapsedMs=pi_elapsed_ms)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", trial_id):
            try:
                directory = base / trial_id
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                private_write(directory / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
            except OSError:
                # A full disk must not turn a known no-dispatch rejection into
                # an unexplained worker exit or uncertain action delivery.
                pass

    persist()
    async with aclosing(protocol_lines(input_stream)) as lines:
        async for line in lines:
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    request = {}
            except ValueError:
                request = {}
            finished = request.get("command") == "finish"
            if finished:
                persist(request.get("piElapsedMs"))
                response = {"id": request.get("id"), "result": result}
            else:
                response = {"id": request.get("id"), "error": message, "errorDetails": details}
            print(json.dumps(response, ensure_ascii=False), file=output_stream, flush=True)
            if finished:
                break


async def serve(
    provider,
    trial_id,
    *,
    input_stream=None,
    output_stream=None,
    runtime_factory=None,
    client_factory=DecisionClient,
    key_reader=load_openrouter_key,
    base=None,
):
    """Run the actual protocol; injectable dependencies keep tests device-free."""
    serve_started = time.perf_counter()
    terminal_task = None
    if provider == "terminal-auto" or os.environ.get("PHONE_TERMINAL_TASK"):
        from terminal_runtime import parse_terminal_task
        from terminal_tasks import TerminalTaskError, resolve_task

        supplied = os.environ.get("PHONE_TERMINAL_TASK")
        try:
            if supplied:
                terminal_task = parse_terminal_task(supplied)
            else:
                terminal_task = parse_terminal_task(resolve_task(
                    os.environ.get("PHONE_TERMINAL_GOAL"), os.environ.get("PHONE_BENCH_RECIPE"),
                ))
        except (ValueError, TypeError):
            await serve_terminal_rejection(
                trial_id, TerminalTaskError("Unsupported terminal task"),
                input_stream=sys.stdin if input_stream is None else input_stream,
                output_stream=sys.stdout if output_stream is None else output_stream,
                base=Path(base) if base is not None else output_dir(), started=serve_started,
            )
            return
        provider = (
            "experience-auto" if terminal_task["kind"] == "experience"
            else "skill-auto" if terminal_task["kind"] == "recorded" else "jev-auto"
        )
    if provider not in {"jev", "deepseek", "jev-auto", "skill", "skill-auto", "experience-auto"}:
        raise ValueError("Unsupported phone decision mode")
    budgets = decision_timeouts() if provider in {"jev-auto", "skill-auto", "experience-auto"} else (5.0, 15.0)
    recipe = None
    if provider in {"skill", "skill-auto"}:
        from workflow import SkillPolicy, load_recipe

        recipe_path = os.environ.get("PHONE_BENCH_RECIPE", "")
        if not recipe_path.strip():
            raise ValueError("PHONE_BENCH_RECIPE is required for skill mode")
        recipe = load_recipe(Path(recipe_path).expanduser())
        if terminal_task is not None:
            from terminal_tasks import QUERY, TITLE

            if recipe["task"] != {"query": QUERY, "expectedFirstTitle": TITLE}:
                raise ValueError("Legacy replay requires its exact demonstrated task")
    if runtime_factory is None:
        if terminal_task is not None:
            from terminal_runtime import GenericTerminalRuntime, TerminalRecordedRuntime

            def terminal_factory(trial):
                if terminal_task["kind"] == "experience":
                    from experience_runtime import ExperienceRuntime

                    return ExperienceRuntime(trial, terminal_task, base=base)
                if terminal_task["kind"] == "recorded":
                    if recipe is not None and recipe.get("entryStrategy") is not None:
                        return TerminalRecordedRuntime(trial, terminal_task, entry_strategy=recipe["entryStrategy"])
                    return TerminalRecordedRuntime(trial, terminal_task)
                return GenericTerminalRuntime(trial, terminal_task, base=base)

            runtime_factory = terminal_factory
        elif os.environ.get("PHONE_BENCH_RECORDING") == "1":
            from workflow import RecordingPhoneRuntime

            runtime_factory = RecordingPhoneRuntime
        else:
            runtime_factory = PhoneRuntime
    base = Path(base) if base is not None else output_dir()
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    client_options = {
        "mode": os.environ.get("PHONE_BENCH_MODE", "warm"),
        "compact": True, "decisionStyle": "heads",
    }
    if terminal_task is not None:
        client_options["providerFallbacks"] = True
    if provider == "skill":
        client = LocalRecipeClient()
    elif provider in {"skill-auto", "experience-auto"} or terminal_task is not None:
        from skill_auto import LazyDecisionClient
        client = LazyDecisionClient(client_factory, key_reader, **client_options)
    else:
        client = client_factory(key_reader(), **client_options)
    policy_options = {"primary_timeout": budgets[0], "fallback_timeout": budgets[1]}
    if provider == "skill":
        policy = SkillPolicy(client, provider, recipe=recipe, **policy_options)
    elif provider == "skill-auto":
        from skill_auto import SkillAutoPolicy
        policy = SkillAutoPolicy(client, provider, recipe=recipe, **policy_options)
    elif provider == "experience-auto":
        from experience_policy import ExperiencePolicy

        source = os.environ.get("PHONE_BENCH_RECIPE") or None
        if os.environ.get("PHONE_EXPERIENCE_ENABLED", "1") == "0":
            source = None
        policy = ExperiencePolicy(client, "jev-auto", skill_path=source, **policy_options)
    else:
        policy = DecisionPolicy(client, provider, **policy_options)
    runtime, error_details = None, None
    early_failure_written = False
    finished = failed = False
    approved_choice = approved_observation = None

    def finish(pi_elapsed_ms=None, *, disconnected=False):
        nonlocal early_failure_written
        if runtime is None:
            if terminal_task is not None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", trial_id):
                result = {
                    "trialId": trial_id, "status": "failed", "provider": provider,
                    "elapsedMs": (time.perf_counter() - serve_started) * 1000,
                    "piElapsedMs": pi_elapsed_ms, "piAgentLoop": True,
                    "modelCalls": [], "operations": [], "phases": [], "milestones": [],
                    "finalState": None, "error": "手机 MCP 初始化或任务准备失败；未开始手机操作。",
                    "errorDetails": error_details, "decisionPolicy": policy.summary(),
                    "terminalTask": terminal_task,
                    "terminalSummary": {"kind": terminal_task["kind"], "backend": provider},
                }
                directory = base / trial_id
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = directory / "result.json"
                if early_failure_written or not path.exists():
                    private_write(path, json.dumps(result, ensure_ascii=False, indent=2))
                    early_failure_written = True
                return result
            raise RuntimeError("No phone observation to finalize")
        if disconnected:
            runtime.error = runtime.error or "PI disconnected before finish"
        if provider in {"skill", "skill-auto"} and not failed and not disconnected:
            if provider == "skill-auto":
                policy.verify_complete(runtime.snapshot, progress=runtime.progress)
            else:
                policy.verify_complete(runtime.snapshot)
        extra = {
            "provider": provider,
            "piElapsedMs": pi_elapsed_ms,
            "piAgentLoop": True,
            "clientMode": client.mode,
            "decisionStyle": client.decisionStyle,
            "decisionPolicy": policy.summary(),
            "errorDetails": error_details,
        }
        if provider in {"skill", "skill-auto"}:
            extra["recipeExecution"] = policy.summary()
        if terminal_task is not None:
            extra["terminalTask"] = terminal_task
            extra["terminalSummary"] = {"kind": terminal_task["kind"], "backend": provider}
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
                            if value.get("modelRequest") is not False:
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
                        if provider in {"skill", "skill-auto"}:
                            # The final action can make PI stop without another
                            # decide request; verify every readback immediately.
                            delivery = value.get("execution", {"status": "applied"})
                            if not isinstance(delivery, dict) or delivery.get("status") not in {
                                "applied",
                                "not_dispatched",
                            }:
                                raise DecisionPolicyError("Phone action delivery is uncertain")
                            if delivery["status"] == "applied" and set(delivery) != {"status"}:
                                raise DecisionPolicyError("Phone action delivery evidence is ambiguous")
                            if delivery["status"] == "not_dispatched":
                                # This releases only the pending reference. PI
                                # must choose and dispatch again on the returned
                                # observation; partial delivery never enters here.
                                policy.not_dispatched(value, choice)
                            else:
                                policy.verify(value)
                        remember_original(value, base)
                    elif command == "finish":
                        try:
                            value = finish(request.get("piElapsedMs"))
                        except DecisionPolicyError as error:
                            # Persist the failed final verification before the
                            # bridge receives a rejection and terminates us.
                            failed = True
                            message, error_details = safe_error(error, command)
                            runtime.error = message
                            finish(request.get("piElapsedMs"))
                            finished = True
                            raise
                        finished = True
                    else:
                        raise ValueError("Unknown worker command")
                    response = {"id": request.get("id"), "result": value}
                except Exception as error:
                    failed = True
                    message, error_details = safe_error(error, command)
                    if runtime is not None:
                        runtime.error = message
                    elif terminal_task is not None:
                        # Initialization failures must survive an immediate PI
                        # disconnect, rather than depending on a later finish.
                        finish()
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
            if (runtime is not None or (terminal_task is not None and failed)) and not finished:
                finish(disconnected=True)
        finally:
            await client.close()


async def main(argv=None, **serve_kwargs):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider", choices=["jev", "deepseek", "jev-auto", "skill", "skill-auto", "terminal-auto"], required=True
    )
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
