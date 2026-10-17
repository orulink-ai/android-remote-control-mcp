"""PI-owned replay worker: models see recordings; no device code is imported.

An act command advances the fixed recording regardless of the chosen operation.
It does NOT execute that operation. done means all six responses were collected,
not that any phone goal succeeded. Keys are read only here from an owner-private
local file, lazily before the first model request.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

from config import output_dir, load_openrouter_key

from providers import DecisionClient, ProviderError, _chat_payload, _jev_payload

RUNTIME_DIR = Path(__file__).resolve().parent
BASE = output_dir()
REPLAY_NOTE = "Recorded phone state only; selected choices are never dispatched to a phone."


def validate_trial_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError("Invalid replay trial ID")
    return value


def read_key(path=None):
    if path is None:
        return load_openrouter_key()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("Replay key file must be owner-private")
        if info.st_size > 4096:
            raise ValueError("Invalid replay key file")
        key = os.read(fd, 4097).decode("utf-8").strip()
        if not key or len(key) > 4096:
            raise ValueError("Invalid replay key file")
        return key
    finally:
        os.close(fd)


def load_snapshots(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) != 6:
        raise ValueError("Replay requires exactly six phone snapshots")
    seen = set()
    for item in data:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or item["id"] in seen
        ):
            raise ValueError("Snapshot IDs must be unique nonempty strings")
        if (
            not isinstance(item.get("state"), dict)
            or not isinstance(item.get("options"), dict)
            or not item["options"]
        ):
            raise ValueError("Each replay snapshot needs state and options")
        if any(
            not isinstance(k, str) or not k or not isinstance(v, str)
            for k, v in item["options"].items()
        ):
            raise ValueError("Invalid replay options")
        seen.add(item["id"])
    # Also reject nonfinite data before a provider or credential can be accessed.
    json.dumps(data, allow_nan=False)
    return data


def payload_hash(provider, state, options):
    body = (
        _jev_payload(state, options, False, "flat")
        if provider == "jev"
        else _chat_payload(state, options)
    )
    canonical = json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def private_json(path, value):
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    path.chmod(0o600)


class ReplayRuntime:
    def __init__(self, provider, trial_id, snapshots, *, mode, base, client_factory, key_reader):
        validate_trial_id(trial_id)
        if provider not in {"jev", "deepseek"} or mode not in {"cold", "warm"}:
            raise ValueError("Invalid replay model or connection mode")
        self.provider, self.trial_id, self.mode = provider, trial_id, mode
        self.snapshots = json.loads(json.dumps(snapshots))
        for snapshot in self.snapshots:
            snapshot["state"]["replayOnly"] = True
            snapshot["state"]["replayNote"] = REPLAY_NOTE
        self.directory = Path(base) / trial_id
        Path(base).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.client_factory, self.key_reader = client_factory, key_reader
        self.client = None
        self.key = None
        self.index = 0
        self.observed = False
        self.pending_choice = None
        self.calls, self.operations, self.phases = [], [], []
        self.error = None
        self.started = time.perf_counter()

    def observe(self):
        self.observed = True
        if self.index == len(self.snapshots):
            return {
                "state": {
                    "replayOnly": True,
                    "replayNote": REPLAY_NOTE,
                    "collectedResponses": len(self.calls),
                    "snapshotCount": 6,
                },
                "options": {},
                "done": True,
                "replayOnly": True,
            }
        item = self.snapshots[self.index]
        return {
            "state": item["state"],
            "options": item["options"],
            "done": False,
            "replayOnly": True,
            "replaySnapshotId": item["id"],
            "replayIndex": self.index,
        }

    async def decide(self):
        if self.error or not self.observed or self.index >= 6 or self.pending_choice is not None:
            raise ValueError("Replay decision is not available in this protocol state")
        if self.client is None:
            self.key = self.key_reader()
            self.client = self.client_factory(
                self.key, mode=self.mode, compact=False, decisionStyle="flat"
            )
        item = self.snapshots[self.index]
        started = time.perf_counter()
        try:
            result = await self.client.decide(self.provider, item["state"], item["options"])
        except Exception as error:
            failure = {
                "snapshotId": item["id"],
                "index": self.index,
                "failed": True,
                "requestPayloadSha256": payload_hash(self.provider, item["state"], item["options"]),
            }
            if isinstance(error, ProviderError):
                failure.update(network=error.network, latencyMs=error.latencyMs)
            self.calls.append(failure)
            raise
        finally:
            self.phases.append(
                {
                    "kind": "model",
                    "name": "recorded_snapshot_decision",
                    "snapshotId": item["id"],
                    "ms": (time.perf_counter() - started) * 1000,
                }
            )
        choice = result.get("choice")
        if not isinstance(choice, str) or choice not in item["options"]:
            raise ValueError("Replay model selected an unavailable choice")
        self.pending_choice = choice
        record = {
            **result,
            "snapshotId": item["id"],
            "index": self.index,
            "replayOnly": True,
            "requestPayloadSha256": payload_hash(self.provider, item["state"], item["options"]),
        }
        self.calls.append(record)
        return result

    def act(self, choice):
        if self.error or self.pending_choice is None or choice != self.pending_choice:
            raise ValueError("Replay advance must match the latest evaluated choice")
        item = self.snapshots[self.index]
        self.operations.append(
            {
                "name": "replay_advance",
                "snapshotId": item["id"],
                "choice": choice,
                "dispatchedToPhone": False,
                "replayOnly": True,
            }
        )
        self.index += 1
        self.pending_choice = None
        return self.observe()

    def finish(self, pi_elapsed_ms=None):
        completed = self.index == 6 and len(self.calls) == 6 and not self.error
        result = {
            "trialId": self.trial_id,
            "status": "replay_failed"
            if self.error
            else "replay_completed"
            if completed
            else "replay_incomplete",
            "provider": self.provider,
            "replayOnly": True,
            "phoneTaskSuccess": None,
            "piAgentLoop": True,
            "clientMode": self.mode,
            "compact": False,
            "decisionStyle": "flat",
            "elapsedMs": (time.perf_counter() - self.started) * 1000,
            "piElapsedMs": pi_elapsed_ms
            if type(pi_elapsed_ms) in {float, int}
            and math.isfinite(pi_elapsed_ms)
            and pi_elapsed_ms >= 0
            else None,
            "modelCalls": self.calls,
            "phases": self.phases,
            "operations": self.operations,
            "milestones": {
                "responsesCollected": sum(not call.get("failed", False) for call in self.calls),
                "snapshotsAdvanced": self.index,
            },
            "finalState": {
                "source": "recordingReplay",
                "livePhoneVerified": False,
                "replayOnly": True,
            },
            "error": self.error,
            "firstWarmSampleIncluded": True,
            "terminalAssistantTextIsSynthetic": True,
        }
        if self.key:
            result = json.loads(
                json.dumps(result, ensure_ascii=False).replace(self.key, "[REDACTED]")
            )
        private_json(self.directory / "result.json", result)
        return result


async def serve(
    provider,
    trial_id,
    data_path,
    *,
    mode="warm",
    base=BASE,
    input_stream=sys.stdin,
    output_stream=sys.stdout,
    client_factory=DecisionClient,
    key_reader=read_key,
):
    runtime = ReplayRuntime(
        provider,
        trial_id,
        load_snapshots(data_path),
        mode=mode,
        base=base,
        client_factory=client_factory,
        key_reader=key_reader,
    )
    finished = False
    try:
        for line in input_stream:
            request = {}
            try:
                request = json.loads(line)
                command = request["command"]
                started = time.perf_counter()
                if command == "observe":
                    value = runtime.observe()
                elif command == "decide":
                    value = await runtime.decide()
                elif command == "act":
                    value = runtime.act(request["choice"])
                elif command == "finish":
                    value = runtime.finish(request.get("piElapsedMs"))
                    # The extension terminates the process after this reply. Close
                    # the pooled HTTP client before acknowledging finish.
                    if runtime.client is not None:
                        await runtime.client.close()
                        runtime.client = None
                    finished = True
                else:
                    raise ValueError("Unsupported replay command")
                if command in {"observe", "act"}:
                    runtime.phases.append(
                        {
                            "kind": "localReplay",
                            "name": command,
                            "ms": (time.perf_counter() - started) * 1000,
                        }
                    )
                response = {"id": request.get("id"), "result": value}
            except Exception:
                runtime.error = "Replay command failed; no phone action was dispatched"
                response = {
                    "id": request.get("id") if isinstance(request, dict) else None,
                    "error": runtime.error,
                }
            print(
                json.dumps(response, ensure_ascii=False, allow_nan=False),
                file=output_stream,
                flush=True,
            )
            if finished:
                break
    finally:
        if not finished:
            runtime.error = runtime.error or "PI disconnected before replay finish"
            runtime.finish()
        if runtime.client is not None:
            await runtime.client.close()


async def cli(argv=None, *, base=BASE, client_factory=DecisionClient, key_reader=read_key):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["jev", "deepseek"], required=True)
    parser.add_argument("--trial-id", required=True)
    args = parser.parse_args(argv)
    data = os.environ.get("PHONE_BENCH_REPLAY_DATA")
    if not data:
        raise ValueError("PHONE_BENCH_REPLAY_DATA is required")
    await serve(
        args.provider,
        args.trial_id,
        data,
        mode=os.environ.get("PHONE_BENCH_MODE", "warm"),
        base=base,
        client_factory=client_factory,
        key_reader=key_reader,
    )


if __name__ == "__main__":
    asyncio.run(cli())
