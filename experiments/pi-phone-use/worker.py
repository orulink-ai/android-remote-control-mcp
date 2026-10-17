"""NDJSON worker for the dedicated PI phone benchmark extension."""

import argparse
import asyncio
import json
import os
import sys
import time

from config import load_openrouter_key, output_dir
from controller import PhoneRuntime, private_write
from own_mcp import McpError
from providers import DecisionClient, ProviderError


def safe_error(error, command):
    """Only known safe messages and scalar diagnostic identifiers cross IPC."""
    if isinstance(error, (ProviderError, McpError)):
        return str(error), {"exceptionType": error.exception_type, "stage": error.stage}
    return "Phone worker command failed", {"exceptionType": type(error).__name__, "stage": command}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["jev", "deepseek"], required=True)
    parser.add_argument("--trial-id", required=True)
    args = parser.parse_args()
    runtime = None
    finished = False
    error_details = None
    client = DecisionClient(
        load_openrouter_key(),
        mode=os.environ.get("PHONE_BENCH_MODE", "warm"),
        compact=True,
        decisionStyle="heads",
    )
    try:
        for line in sys.stdin:
            request = json.loads(line)
            response = {"id": request["id"]}
            command = request.get("command", "unknown")
            try:
                if runtime is None:
                    runtime = PhoneRuntime(args.trial_id)
                if command == "observe":
                    value = runtime.observe()
                elif command == "act":
                    value = runtime.act(request["choice"])
                elif command == "decide":
                    if runtime.snapshot is None:
                        raise RuntimeError("Observe first")
                    started = time.perf_counter()
                    try:
                        value = await client.decide(
                            args.provider, runtime.snapshot["state"], runtime.snapshot["options"]
                        )
                    except Exception as error:
                        if isinstance(error, ProviderError):
                            runtime.operations.append(
                                {
                                    "name": "model_request_failure",
                                    "network": error.network,
                                    "ms": error.latencyMs,
                                    "exceptionType": error.exception_type,
                                    "stage": error.stage,
                                }
                            )
                        runtime.phases.append(
                            {
                                "kind": "model",
                                "name": "模型请求失败",
                                "ms": (time.perf_counter() - started) * 1000,
                            }
                        )
                        raise
                    runtime.record_model(value)
                elif command == "finish":
                    value = runtime.finish(
                        {
                            "provider": args.provider,
                            "piElapsedMs": request.get("piElapsedMs"),
                            "piAgentLoop": True,
                            "clientMode": client.mode,
                            "decisionStyle": client.decisionStyle,
                            "errorDetails": error_details,
                        }
                    )
                    finished = True
                else:
                    raise ValueError("Unknown worker command")
                if (
                    command in ["observe", "act"]
                    and value.get("state", {}).get("observation", {}).get("kind") == "detail"
                ):
                    original = output_dir() / "original-post-state.json"
                    if not original.exists():
                        private_write(
                            original,
                            json.dumps(value["state"]["observation"], ensure_ascii=False, indent=2),
                        )
                response["result"] = value
            except Exception as error:
                message, error_details = safe_error(error, command)
                if runtime:
                    runtime.error = message
                response["error"] = message
                response["errorDetails"] = error_details
            print(json.dumps(response, ensure_ascii=False), flush=True)
            if finished:
                break
    finally:
        try:
            if runtime and not finished:
                runtime.error = runtime.error or "PI process disconnected before finish"
                runtime.finish(
                    {"provider": args.provider, "piAgentLoop": True, "errorDetails": error_details}
                )
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
