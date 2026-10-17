"""Record one additional real PI phone trial without changing its runtime.

This launcher starts exactly its own screenrecord process and remembers the PID
printed by that launch's $!. It never uses pidof/pkill or stops other recordings.
The original MP4 is pulled unchanged: playback 1x, no edits, cuts or transcoding.
MCP session cleanup remains the caller's responsibility between trials.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import time
from uuid import uuid4

from config import output_dir, python_executable
from run_pi_trial import require_device, validate_new_trial

RUNTIME_DIR = Path(__file__).resolve().parent
BASE = output_dir()


@dataclass
class Recording:
    pid: int
    remote_path: str
    process: subprocess.Popen
    started: float


def adb(serial, *arguments, **kwargs):
    return subprocess.run(
        [os.environ.get("ADB", "adb"), "-s", serial, *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=kwargs.pop("timeout", 20),
        **kwargs,
    )


def recording_script(remote_path):
    return (
        f"screenrecord --size 720x1560 --bit-rate 2500000 --time-limit 180 {shlex.quote(remote_path)} &\n"
        "phone_record_pid=$!\n"
        "printf 'PHONE_RECORD_PID=%s\\n' \"$phone_record_pid\"\n"
        'wait "$phone_record_pid"\n'
    )


def _read_pid(process, timeout=10):
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            line = process.stdout.readline()
            if not line:
                break
            found = re.fullmatch(r"PHONE_RECORD_PID=([1-9][0-9]*)", line.strip())
            if found:
                return int(found.group(1))
    raise RuntimeError("The new recording did not report its PID")


def _identity_script(recording, stop):
    if type(recording.pid) is not int or recording.pid <= 1:
        raise ValueError("Unsafe recording PID")
    path = shlex.quote(recording.remote_path)
    action = (
        f"kill -2 {recording.pid} && printf 'PHONE_RECORD_STOP=sent_int\\n'"
        if stop
        else "printf 'PHONE_RECORD_OWNER=owned\\n'"
    )
    return (
        f"if [ ! -r /proc/{recording.pid}/cmdline ]; then\n"
        " printf 'PHONE_RECORD_STOP=already_exited\\n'\n"
        " exit 0\nfi\n"
        f"phone_record_cmd=$(tr '\\000' ' ' < /proc/{recording.pid}/cmdline)\n"
        f'case "$phone_record_cmd" in\n *screenrecord*{path}*) {action} ;;\n'
        " *) exit 42 ;;\nesac\n"
    )


def start_recording(serial, remote_path):
    process = subprocess.Popen(
        [os.environ.get("ADB", "adb"), "-s", serial, "shell", recording_script(remote_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    owned = None
    try:
        owned = Recording(
            pid=_read_pid(process),
            remote_path=remote_path,
            process=process,
            started=time.perf_counter(),
        )
        time.sleep(0.2)
        owner = adb(serial, "shell", _identity_script(owned, False))
        if "PHONE_RECORD_OWNER=owned" not in owner.stdout or process.poll() is not None:
            raise RuntimeError("The new screen recording is not running")
        return owned
    except BaseException:
        if owned is not None:
            try:
                stop_recording(owned, serial)
            except Exception:
                pass
        elif process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        raise


def stop_recording(recording, serial):
    result = {"status": "not_stopped", "pid": recording.pid}
    try:
        stopped = adb(serial, "shell", _identity_script(recording, True))
        marker = re.search(r"PHONE_RECORD_STOP=(sent_int|already_exited)", stopped.stdout)
        if marker is None:
            raise RuntimeError("Recording stop acknowledgment missing")
        result["status"] = marker.group(1)
    finally:
        # These handles belong exclusively to our ADB launcher. Never signal a
        # different Android process if PID ownership verification failed.
        try:
            stdout, stderr = recording.process.communicate(timeout=15)
            result["adbStderr"] = stderr[-4000:]
            result["launcherExitCode"] = recording.process.returncode
        except subprocess.TimeoutExpired:
            recording.process.terminate()
            try:
                recording.process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                recording.process.kill()
                recording.process.communicate(timeout=3)
            result["launcherForcedClosed"] = True
    return result


def pull_recording(serial, remote_path, target):
    adb(serial, "pull", remote_path, str(target), timeout=60)
    target.chmod(0o600)


def validate_probe(data):
    videos = [stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"]
    if len(videos) != 1:
        raise ValueError("Recording must contain one video stream")
    stream = videos[0]
    duration = float(data.get("format", {}).get("duration", stream.get("duration", 0)))
    if (
        not math.isfinite(duration)
        or duration <= 0
        or stream.get("width") != 720
        or stream.get("height") != 1560
    ):
        raise ValueError("Recording duration or dimensions are invalid")
    if not stream.get("codec_name"):
        raise ValueError("Recording codec is missing")
    return {
        "valid": True,
        "durationSeconds": duration,
        "width": 720,
        "height": 1560,
        "codec": stream["codec_name"],
        "averageFrameRate": stream.get("avg_frame_rate"),
        "ffprobe": data,
    }


def probe_video(path, ffprobe="ffprobe"):
    binary = shutil.which(ffprobe)
    if binary is None:
        raise FileNotFoundError("ffprobe was not found")
    result = subprocess.run(
        [binary, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return validate_probe(json.loads(result.stdout))


def run_trial(model, trial_id, output_directory, timeout=260, *, serial=None):
    command = [python_executable(), str(RUNTIME_DIR / "run_pi_trial.py"), model, trial_id]
    started = time.perf_counter()
    stdout = output_directory / f"{model}-trial-stdout.jsonl"
    stderr = output_directory / f"{model}-trial-stderr.log"
    exit_code = None
    with (
        os.fdopen(os.open(stdout, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out,
        os.fdopen(os.open(stderr, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as err,
    ):
        process = subprocess.Popen(
            command,
            cwd=RUNTIME_DIR,
            env={
                **os.environ,
                "PHONE_BENCH_OUTPUT_DIR": str(BASE),
                "PHONE_BENCH_DEVICE": require_device(serial),
            },
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
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            raise
    result_path = BASE / trial_id / "result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    return {
        "command": command,
        "pid": process.pid,
        "exitCode": exit_code,
        "wallMs": (time.perf_counter() - started) * 1000,
        "status": result.get("status", "missing_result"),
        "resultPath": str(result_path),
        "launchMetadataPath": str(BASE / f"{trial_id}-launch.json"),
        "stdoutPath": str(stdout),
        "stderrPath": str(stderr),
    }


def record_trial(model, trial_id, *, report_dir, serial=None, ffprobe="ffprobe"):
    if model not in {"jev", "deepseek"} or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", trial_id
    ):
        raise ValueError("Invalid model or trial ID")
    validate_new_trial(trial_id, BASE)
    serial = require_device(serial)
    output = Path(report_dir).resolve() / "phone-use-videos"
    output.mkdir(parents=True, exist_ok=True)
    video = output / f"{model}-full-flow.mp4"
    recording_json = output / f"{model}-recording.json"
    collisions = [
        video,
        recording_json,
        output / f"{model}-trial-stdout.jsonl",
        output / f"{model}-trial-stderr.log",
    ]
    trial_collisions = [BASE / trial_id] + [
        BASE / f"{trial_id}{suffix}"
        for suffix in ["-launch.json", "-pi-session.jsonl", "-pi-events.jsonl", "-pi-stderr.log"]
    ]
    if any(path.exists() for path in collisions + trial_collisions):
        raise FileExistsError(
            "Recording or trial artifacts already exist; use a new destination/trial"
        )
    remote = f"/sdcard/phone-use-{model}-{trial_id}-{uuid4().hex}.mp4"
    result = {
        "schemaVersion": 1,
        "model": model,
        "trialId": trial_id,
        "serial": serial,
        "status": "failed",
        "videoPath": str(video),
        "recordingJson": str(recording_json),
        "remotePath": remote,
        "remotePid": None,
        "playbackRate": 1,
        "edited": False,
        "trimmed": False,
        "requested": {"width": 720, "height": 1560, "bitrate": 2500000, "timeLimitSeconds": 180},
        "trial": None,
        "stop": None,
        "validation": None,
        "errors": [],
    }
    owned = None
    alive_after_trial = False
    try:
        owned = start_recording(serial, remote)
        result["remotePid"] = owned.pid
        result["trial"] = run_trial(model, trial_id, output, serial=serial)
        alive_after_trial = owned.process.poll() is None
    except Exception as error:
        result["errors"].append({"stage": "trial_or_start", "type": type(error).__name__})
    finally:
        if owned is not None:
            try:
                result["stop"] = stop_recording(owned, serial)
            except Exception as error:
                result["errors"].append(
                    {"stage": "stop_own_recording", "type": type(error).__name__}
                )
            # Preserve a useful unedited recording even if the trial failed.
            try:
                pull_recording(serial, remote, video)
                result["validation"] = probe_video(video, ffprobe)
                # Only remove our unique remote artifact once a valid local copy exists.
                adb(serial, "shell", "rm", "--", remote)
                result["remoteRemovedAfterVerifiedPull"] = True
            except Exception as error:
                result["errors"].append({"stage": "pull_or_ffprobe", "type": type(error).__name__})
        validation = result["validation"] or {}
        trial = result["trial"] or {}
        result["fullFlowCovered"] = bool(
            alive_after_trial
            and result["stop"]
            and result["stop"].get("status") == "sent_int"
            and validation.get("valid")
            and validation.get("durationSeconds", 0) + 0.5
            >= trial.get("wallMs", float("inf")) / 1000
        )
        result["durationSeconds"] = validation.get("durationSeconds")
        result["width"], result["height"], result["codec"] = (
            validation.get("width"),
            validation.get("height"),
            validation.get("codec"),
        )
        result["trialStatus"] = trial.get("status")
        if (
            not result["errors"]
            and trial.get("exitCode") == 0
            and trial.get("status") == "success"
            and result["fullFlowCovered"]
        ):
            result["status"] = "recorded"
        fd = os.open(recording_json, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as document:
            json.dump(result, document, ensure_ascii=False, indent=2, allow_nan=False)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=["jev", "deepseek"])
    parser.add_argument("trial_id")
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--serial", default=os.environ.get("PHONE_BENCH_DEVICE"))
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args(argv)
    result = record_trial(
        args.model,
        args.trial_id,
        report_dir=args.report_dir,
        serial=args.serial,
        ffprobe=args.ffprobe,
    )
    print(
        json.dumps(
            {
                key: result[key]
                for key in [
                    "schemaVersion",
                    "model",
                    "trialId",
                    "status",
                    "videoPath",
                    "recordingJson",
                    "durationSeconds",
                    "width",
                    "height",
                    "codec",
                    "playbackRate",
                    "edited",
                    "trimmed",
                    "fullFlowCovered",
                    "trialStatus",
                    "remotePid",
                    "errors",
                ]
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if result["status"] == "recorded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
