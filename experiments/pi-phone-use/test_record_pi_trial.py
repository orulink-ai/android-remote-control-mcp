"""Recorder contract tests use process doubles; no ADB/device/API calls."""

import json
from pathlib import Path
import subprocess

import pytest

import record_pi_trial as recorder


@pytest.fixture(autouse=True)
def isolated_runtime_base(tmp_path, monkeypatch):
    runtime_base = tmp_path / "runtime"
    runtime_base.mkdir()
    monkeypatch.setattr(recorder, "BASE", runtime_base)
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "fixture-device")


class FakeRecordingProcess:
    pid = 9001
    returncode = 0

    def poll(self):
        return None

    def communicate(self, timeout=None):
        return ("", "")

    def terminate(self):
        pass

    def kill(self):
        pass


def recording():
    return recorder.Recording(
        pid=12345,
        remote_path="/sdcard/phone-use-owned-token.mp4",
        process=FakeRecordingProcess(),
        started=1.0,
    )


def test_remote_launch_reports_its_own_new_pid_with_required_recording_settings():
    script = recorder.recording_script("/sdcard/phone-use-owned-token.mp4")
    assert "--size 720x1560" in script
    assert "--bit-rate 2500000" in script
    assert "--time-limit 180" in script
    assert "$!" in script and "PHONE_RECORD_PID=" in script
    assert "pidof" not in script and "pkill" not in script


def test_stop_signals_only_captured_pid_after_checking_unique_remote_file(monkeypatch):
    calls = []

    def adb(serial, *arguments, **kwargs):
        calls.append((serial, arguments))
        return subprocess.CompletedProcess(
            arguments, 0, stdout="PHONE_RECORD_STOP=sent_int\n", stderr=""
        )

    monkeypatch.setattr(recorder, "adb", adb)
    result = recorder.stop_recording(recording(), "fixture-device")
    script = calls[0][1][-1]
    assert "/proc/12345/cmdline" in script
    assert "kill -2 12345" in script
    assert "phone-use-owned-token.mp4" in script
    assert "pidof" not in script and "pkill" not in script
    assert result["status"] == "sent_int"


def test_ffprobe_requires_valid_portrait_video():
    data = {
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1560}],
        "format": {"duration": "40.2"},
    }
    assert recorder.validate_probe(data)["durationSeconds"] == 40.2
    data["streams"][0]["width"] = 1080
    with pytest.raises(ValueError):
        recorder.validate_probe(data)
    with pytest.raises(ValueError):
        recorder.validate_probe({"streams": [], "format": {"duration": "0"}})


@pytest.mark.parametrize("trial_failure", [False, True])
def test_owned_recording_is_stopped_and_pulled_even_when_trial_fails(
    tmp_path, monkeypatch, trial_failure
):
    calls = []
    monkeypatch.setattr(
        recorder, "start_recording", lambda *args: calls.append("start") or recording()
    )

    def trial(*args, **kwargs):
        calls.append("trial")
        if trial_failure:
            raise RuntimeError("fixture trial failure")
        return {"exitCode": 0, "wallMs": 10, "status": "success", "resultPath": "fixture-result"}

    monkeypatch.setattr(recorder, "run_trial", trial)
    monkeypatch.setattr(
        recorder, "stop_recording", lambda *args: calls.append("stop") or {"status": "sent_int"}
    )

    def pull(_serial, _remote, target):
        calls.append("pull")
        target.write_bytes(b"offline-video-fixture")

    monkeypatch.setattr(recorder, "pull_recording", pull)
    monkeypatch.setattr(
        recorder,
        "probe_video",
        lambda *args: {
            "durationSeconds": 1.0,
            "width": 720,
            "height": 1560,
            "codec": "h264",
            "valid": True,
        },
    )
    monkeypatch.setattr(recorder, "adb", lambda *a, **kw: calls.append("remove-verified-remote"))
    result = recorder.record_trial("jev", "video-jev", report_dir=tmp_path)
    assert calls == ["start", "trial", "stop", "pull", "remove-verified-remote"]
    assert result["status"] == ("failed" if trial_failure else "recorded")
    assert result["playbackRate"] == 1 and result["edited"] is False and result["trimmed"] is False
    assert Path(result["videoPath"]).name == "jev-full-flow.mp4"
    metadata = json.loads(Path(result["recordingJson"]).read_text())
    assert metadata["remotePid"] == 12345
    assert metadata["serial"] == "fixture-device"


def test_existing_video_refuses_to_start_recording_or_trial(tmp_path, monkeypatch):
    output = tmp_path / "phone-use-videos"
    output.mkdir()
    (output / "deepseek-full-flow.mp4").write_bytes(b"keep original")
    monkeypatch.setattr(
        recorder, "start_recording", lambda *args: pytest.fail("must not touch phone")
    )
    with pytest.raises(FileExistsError):
        recorder.record_trial("deepseek", "video-deepseek", report_dir=tmp_path)


def test_pid_ownership_refusal_never_attempts_another_signal(monkeypatch):
    calls = []

    def reject(serial, *arguments, **kwargs):
        calls.append(arguments)
        raise subprocess.CalledProcessError(42, arguments)

    monkeypatch.setattr(recorder, "adb", reject)
    with pytest.raises(subprocess.CalledProcessError):
        recorder.stop_recording(recording(), "fixture")
    assert len(calls) == 1
    assert "kill -2 12345" in calls[0][-1]
    # The conditional shell owns the only signal; no second kill/pid search runs.
    assert "case" in calls[0][-1] and "exit 42" in calls[0][-1]


def test_missing_device_never_starts_recording(tmp_path, monkeypatch):
    monkeypatch.delenv("PHONE_BENCH_DEVICE", raising=False)
    monkeypatch.setattr(recorder, "start_recording", lambda *a: pytest.fail("phone touched"))
    with pytest.raises(ValueError, match="PHONE_BENCH_DEVICE"):
        recorder.record_trial("jev", "new", report_dir=tmp_path)


def test_invalid_pull_is_never_deleted_remotely(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "start_recording", lambda *a: recording())
    monkeypatch.setattr(
        recorder, "run_trial", lambda *a, **kw: {"exitCode": 0, "wallMs": 10, "status": "success"}
    )
    monkeypatch.setattr(recorder, "stop_recording", lambda *a: {"status": "sent_int"})
    monkeypatch.setattr(recorder, "pull_recording", lambda *a: None)

    def invalid(*a):
        raise ValueError("invalid recording")

    monkeypatch.setattr(recorder, "probe_video", invalid)
    monkeypatch.setattr(recorder, "adb", lambda *a: pytest.fail("must retain remote copy"))
    result = recorder.record_trial("jev", "bad-pull", report_dir=tmp_path)
    assert result["status"] == "failed"
    assert "remoteRemovedAfterVerifiedPull" not in result


@pytest.mark.parametrize("trial_status", ["failed", "missing_result", None])
def test_zero_exit_failed_task_preserves_video_without_claiming_completed_flow(
    tmp_path, monkeypatch, trial_status
):
    calls = []
    monkeypatch.setattr(recorder, "start_recording", lambda *a: recording())
    monkeypatch.setattr(
        recorder,
        "run_trial",
        lambda *a, **kw: {
            "exitCode": 0,
            "wallMs": 10,
            "status": trial_status,
        },
    )
    monkeypatch.setattr(recorder, "stop_recording", lambda *a: {"status": "sent_int"})

    def pull(_serial, _remote, target):
        calls.append("pull")
        target.write_bytes(b"offline-video-fixture")

    monkeypatch.setattr(recorder, "pull_recording", pull)

    def probe(*a):
        calls.append("validate")
        return {"durationSeconds": 1, "width": 720, "height": 1560, "codec": "h264", "valid": True}

    monkeypatch.setattr(recorder, "probe_video", probe)
    monkeypatch.setattr(recorder, "adb", lambda *a, **kw: calls.append("remove-verified-remote"))
    result = recorder.record_trial("jev", "failed-zero", report_dir=tmp_path)
    assert calls == ["pull", "validate", "remove-verified-remote"]
    assert result["status"] == "failed"
    assert result["trialStatus"] == trial_status
    assert Path(result["videoPath"]).exists()
