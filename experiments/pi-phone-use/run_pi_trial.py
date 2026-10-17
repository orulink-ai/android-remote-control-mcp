"""Launch real PI; only cold-process/launcher setup occurs outside its timed loop."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from config import output_dir, python_executable
from replay_worker import private_json, validate_trial_id

RUNTIME_DIR = Path(__file__).resolve().parent
BASE = output_dir()
PROMPT = "请实际执行手机闭环测试：从Android桌面打开小红书，搜索小狮日记，打开第一篇帖子，依次点赞、收藏、取消点赞、取消收藏，回读验证最终未点赞、未收藏。只使用phone_observe和phone_action。"


def require_device(serial=None):
    value = serial if serial is not None else os.environ.get("PHONE_BENCH_DEVICE", "")
    if not value or not value.strip() or value.startswith("-"):
        raise ValueError("PHONE_BENCH_DEVICE must explicitly identify the test phone")
    return value


def validate_new_trial(trial_id, base):
    validate_trial_id(trial_id)
    paths = [base / trial_id] + [
        base / f"{trial_id}{suffix}"
        for suffix in ("-launch.json", "-pi-session.jsonl", "-pi-events.jsonl", "-pi-stderr.log")
    ]
    if any(path.exists() for path in paths):
        raise FileExistsError("Trial ID already has artifacts; use a new ID")


def reset_phone(serial):
    # Imported lazily so offline launch validation never initializes phone tools.
    from own_mcp import OwnMcp, content_text

    started = time.perf_counter()
    subprocess.run(
        [os.environ.get("ADB", "adb"), "-s", serial, "shell", "am", "force-stop", "com.xingin.xhs"],
        check=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    client = OwnMcp()
    try:
        client.call("android_press_key", {"key": "HOME"})
        time.sleep(0.3)
        state = content_text(client.call("android_get_screen_state", {"include_screenshot": False}))
        if "pkg:com.sec.android.app.launcher" not in state:
            raise RuntimeError("Android launcher not verified; trial not started")
    finally:
        client.close()
    return (time.perf_counter() - started) * 1000


def launch(provider, trial_id, *, base=None, timeout=230):
    if provider not in {"jev", "deepseek"}:
        raise ValueError("Invalid provider")
    base = Path(base if base is not None else output_dir()).resolve()
    validate_new_trial(trial_id, base)
    serial = require_device()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    setup_ms = reset_phone(serial)
    session = base / f"{trial_id}-pi-session.jsonl"
    stdout = base / f"{trial_id}-pi-events.jsonl"
    stderr = base / f"{trial_id}-pi-stderr.log"
    metadata_path = base / f"{trial_id}-launch.json"
    env = {
        **os.environ,
        "PHONE_BENCH_TRIAL": trial_id,
        "PHONE_BENCH_OUTPUT_DIR": str(base),
        "PHONE_BENCH_MODE": os.environ.get("PHONE_BENCH_MODE", "warm"),
        "PHONE_BENCH_PYTHON": python_executable(),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
    }
    command = [
        os.environ.get("PI_BIN", "pi"),
        "--no-extensions",
        "-e",
        str(RUNTIME_DIR / "pi-extension.ts"),
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
        provider,
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
        "provider": provider,
        "excludedResetMs": setup_ms,
        "piSession": str(session),
        "piEvents": str(stdout),
    }
    started = time.perf_counter()
    process, exit_code = None, None
    try:
        with (
            os.fdopen(os.open(stdout, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out,
            os.fdopen(os.open(stderr, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as err,
        ):
            process = subprocess.Popen(
                command,
                cwd=RUNTIME_DIR,
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
            cliWallMs=(time.perf_counter() - started) * 1000,
            exitCode=exit_code,
            pid=process.pid if process is not None else None,
        )
        private_json(metadata_path, metadata)
        for path in [stdout, stderr, session]:
            if path.exists():
                path.chmod(0o600)
    result_path = base / trial_id / "result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text())
        metadata.update(
            status=result["status"],
            piElapsedMs=result.get("piElapsedMs"),
            elapsedMs=result["elapsedMs"],
            modelCalls=len(result["modelCalls"]),
            milestones=result["milestones"],
            error=result.get("error"),
        )
    else:
        metadata["status"] = "missing_result"
    private_json(metadata_path, metadata)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=["jev", "deepseek"])
    parser.add_argument("trial_id")
    args = parser.parse_args(argv)
    result = launch(args.provider, args.trial_id)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("exitCode") == 0 and result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
