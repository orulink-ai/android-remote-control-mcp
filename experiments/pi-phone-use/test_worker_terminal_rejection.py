"""Terminal routing failures remain protocol replies, with no phone/model IO."""

import asyncio
import errno
import io
import json

import pytest

import worker


def run_rejected(tmp_path, monkeypatch, *, supplied=None, finish=True):
    monkeypatch.delenv("PHONE_TERMINAL_TASK", raising=False)
    monkeypatch.setenv("PHONE_TERMINAL_GOAL", "再来一次 private-goal-marker")
    if supplied is not None:
        monkeypatch.setenv("PHONE_TERMINAL_TASK", supplied)

    def forbidden(*args, **kwargs):
        pytest.fail("A rejected goal must not access the phone, model client, or API key")

    commands = [{"id": 1, "command": "observe"}]
    if finish:
        commands.append({"id": 2, "command": "finish", "piElapsedMs": 123})
    output = io.StringIO()
    asyncio.run(worker.serve(
        "terminal-auto", "unsupported", base=tmp_path,
        input_stream=io.StringIO("".join(json.dumps(item) + "\n" for item in commands)),
        output_stream=output, runtime_factory=forbidden,
        client_factory=forbidden, key_reader=forbidden,
    ))
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["id"] == 1
    assert replies[0]["errorDetails"] == {
        "exceptionType": "TerminalTaskError", "stage": "terminal_task", "code": "UNSUPPORTED_TASK",
    }
    assert "未执行手机操作" in replies[0]["error"]
    assert "private-goal-marker" not in output.getvalue()
    return replies


@pytest.mark.parametrize("supplied", [None, "invalid-json private-goal-marker", '{"kind":"unbounded"}'])
def test_invalid_terminal_task_has_error_reply_and_failed_finish(tmp_path, monkeypatch, supplied):
    replies = run_rejected(tmp_path, monkeypatch, supplied=supplied)
    assert replies[1]["id"] == 2
    result = replies[1]["result"]
    assert result["status"] == "failed" and result["piElapsedMs"] == 123
    assert result["elapsedMs"] >= 0
    assert result["finalState"] is None
    assert result["modelCalls"] == result["operations"] == []
    assert result["errorDetails"] == replies[0]["errorDetails"]
    assert json.loads((tmp_path / "unsupported/result.json").read_text()) == result


def test_rejection_persists_without_finish_request(tmp_path, monkeypatch):
    replies = run_rejected(tmp_path, monkeypatch, finish=False)
    result = json.loads((tmp_path / "unsupported/result.json").read_text())
    assert result["status"] == "failed"
    assert result["errorDetails"] == replies[0]["errorDetails"]


def test_rejection_reply_survives_full_disk(tmp_path, monkeypatch):
    def full_disk(*args, **kwargs):
        raise OSError(errno.ENOSPC, "private-storage-message")

    monkeypatch.setattr(worker, "private_write", full_disk)
    replies = run_rejected(tmp_path, monkeypatch)
    assert replies[1]["result"]["errorDetails"]["code"] == "UNSUPPORTED_TASK"
