"""Launch PI for recorded-state network diagnostics; never import phone tools.

Each invocation is one independent six-snapshot session. Run cold and warm with
the same dataset to isolate connection strategy: compact=False, flat choices.
The first warm connection/sample is retained. No app reset or device call occurs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from config import python_executable
from replay_worker import BASE, RUNTIME_DIR, load_snapshots, private_json, validate_trial_id

PROMPT = "执行只读手机状态回放诊断：依次评价六个已记录快照；phone_action仅推进回放，不向手机派发任何动作。完成仅表示回放响应收集结束，不代表手机任务成功。"


def launch(
    mode, trial_id, data_path, *, base=BASE, worker_path=None, extension_path=None, timeout=180
):
    validate_trial_id(trial_id)
    if mode not in {"cold", "warm"}:
        raise ValueError("Replay connection mode must be cold or warm")
    data_path, base = Path(data_path).resolve(), Path(base).resolve()
    snapshots = load_snapshots(data_path)
    session = base / f"{trial_id}-pi-session.jsonl"
    stdout = base / f"{trial_id}-pi-events.jsonl"
    stderr = base / f"{trial_id}-pi-stderr.log"
    metadata_path = base / f"{trial_id}-launch.json"
    if any(path.exists() for path in [base / trial_id, session, stdout, stderr, metadata_path]):
        raise FileExistsError("Replay trial ID already has artifacts")
    worker_path = Path(worker_path or RUNTIME_DIR / "replay_worker.py").resolve()
    extension_path = Path(extension_path or RUNTIME_DIR / "pi-replay-extension.ts").resolve()
    env = {
        **os.environ,
        "PHONE_BENCH_REPLAY_DATA": str(data_path),
        "PHONE_BENCH_WORKER": str(worker_path),
        "PHONE_BENCH_MODE": mode,
        "PHONE_BENCH_TRIAL": trial_id,
        "PHONE_BENCH_PYTHON": python_executable(),
        "PHONE_BENCH_OUTPUT_DIR": str(base),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
    }
    command = [
        os.environ.get("PI_BIN", "pi"),
        "--no-extensions",
        "-e",
        str(extension_path),
        "--no-mcp",
        "--no-skills",
        "--no-context-files",
        "--no-prompt-templates",
        "--no-themes",
        "--offline",
        "--no-builtin-tools",
        "--tools",
        "phone_observe,phone_action",
        "--provider",
        "phone-benchmark",
        "--model",
        "jev",
        "--thinking",
        "off",
        "--session",
        str(session),
        "--mode",
        "json",
        "--print",
        PROMPT,
    ]
    metadata = {
        "trialId": trial_id,
        "provider": "jev",
        "replayOnly": True,
        "phoneTaskSuccess": None,
        "clientMode": mode,
        "compact": False,
        "decisionStyle": "flat",
        "snapshotCount": 6,
        "snapshotIds": [item["id"] for item in snapshots],
        "firstWarmSampleIncluded": True,
        "replayDataset": str(data_path),
        "replayDatasetSha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
        "piSession": str(session),
        "piEvents": str(stdout),
        "piStderr": str(stderr),
        "terminalAssistantTextIsSynthetic": True,
        "command": command,
        "worker": str(worker_path),
        "stdin": "DEVNULL",
    }
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = time.perf_counter()
    process = None
    exit_code = None
    try:
        with (
            os.fdopen(os.open(stdout, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out,
            os.fdopen(os.open(stderr, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as err,
        ):
            process = subprocess.Popen(
                command,
                cwd=base,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
                umask=0o077,
            )
            try:
                exit_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                exit_code = 124
    finally:
        metadata.update(
            exitCode=exit_code,
            cliWallMs=(time.perf_counter() - started) * 1000,
            pid=process.pid if process is not None else None,
        )
        private_json(metadata_path, metadata)
        for path in [session, stdout, stderr]:
            if path.exists():
                path.chmod(0o600)
    report = base / trial_id / "result.json"
    if report.exists():
        result = json.loads(report.read_text())
        metadata.update(
            status=result["status"],
            piElapsedMs=result.get("piElapsedMs"),
            modelCallCount=len(result["modelCalls"]),
        )
    else:
        metadata.update(status="replay_result_missing")
    private_json(metadata_path, metadata)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["cold", "warm"])
    parser.add_argument("trial_id")
    parser.add_argument("--data", default=os.environ.get("PHONE_BENCH_REPLAY_DATA"))
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    if not args.data:
        parser.error("--data or PHONE_BENCH_REPLAY_DATA is required")
    result = launch(args.mode, args.trial_id, args.data, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("exitCode") == 0 and result.get("status") == "replay_completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
