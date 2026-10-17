"""Launcher interruption tests operate on owned process doubles, never a phone."""

import signal
import subprocess

import pytest

import run_pi_trial as launcher


@pytest.mark.parametrize("reason", ["interrupt", "timeout"])
def test_launch_always_terminates_only_its_pi_process_group(tmp_path, monkeypatch, reason):
    monkeypatch.setenv("PHONE_BENCH_DEVICE", "offline")
    monkeypatch.setattr(launcher, "reset_phone", lambda *args: 0)
    signals = []

    class Process:
        pid = 424242
        returncode = None
        waits = 0

        def __init__(self, *args, **kwargs):
            assert kwargs["start_new_session"] is True

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                if reason == "interrupt":
                    raise KeyboardInterrupt()
                raise subprocess.TimeoutExpired("fixture-pi", timeout)
            self.returncode = -15
            return self.returncode

        def poll(self):
            return self.returncode

    def killpg(pid, sig):
        assert pid == Process.pid
        signals.append(sig)
        if sig == 0:
            raise ProcessLookupError()

    monkeypatch.setattr(launcher.subprocess, "Popen", Process)
    monkeypatch.setattr(launcher.os, "killpg", killpg)
    if reason == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            launcher.launch("jev", "interrupted", base=tmp_path)
    else:
        result = launcher.launch("jev", "interrupted", base=tmp_path)
        assert result["exitCode"] == 124
    assert signal.SIGTERM in signals


def test_owned_group_hard_kill_is_bounded_even_when_pi_exits_before_worker(monkeypatch):
    signals = []
    clock = iter([0, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5])
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

    class Process:
        pid = 424242
        def poll(self):
            return -15  # Parent already exited; its worker still owns the group.
        def wait(self, timeout):
            return -15

    monkeypatch.setattr(launcher.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    launcher.stop_owned_group(Process())
    assert signals[0] == (424242, signal.SIGTERM)
    assert signals[-1] == (424242, signal.SIGKILL)
    assert all(pid == 424242 for pid, _ in signals)


def test_main_sigterm_runs_launch_finally_and_restores_previous_handler(monkeypatch):
    previous = signal.getsignal(signal.SIGTERM)
    cleaned = []

    def interrupted(*args):
        try:
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
        finally:
            cleaned.append(True)

    monkeypatch.setattr(launcher, "launch", interrupted)
    assert launcher.main(["skill-auto", "offline-sigterm"]) == 143
    assert cleaned == [True]
    assert signal.getsignal(signal.SIGTERM) is previous
