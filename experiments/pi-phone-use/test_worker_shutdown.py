"""Real subprocess cancellation with fake phone/models only; never device/API IO."""

import asyncio
from contextlib import aclosing
import io
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

import pytest

from worker_io import cancellation_scope, protocol_lines


DIRECTORY = Path(__file__).resolve().parent


def test_injected_string_stream_preserves_protocol_lines():
    async def scenario():
        async with aclosing(protocol_lines(io.StringIO("first\n\nlast"))) as lines:
            return [line async for line in lines]

    assert asyncio.run(scenario()) == ["first\n", "\n", "last"]


def test_cancellation_scope_restores_normal_signal_handler():
    original = signal.getsignal(signal.SIGTERM)

    def previous(_number, _frame):
        pass

    signal.signal(signal.SIGTERM, previous)
    try:

        async def scenario():
            async with cancellation_scope():
                assert signal.getsignal(signal.SIGTERM) is not previous

        asyncio.run(scenario())
        assert signal.getsignal(signal.SIGTERM) is previous
    finally:
        signal.signal(signal.SIGTERM, original)


WRAPPER = """import asyncio,json,sys
from pathlib import Path
import worker
base=Path(sys.argv[1])
class FakeRuntime:
    def __init__(self,trial):
        self.index=0; self.snapshot=None; self.operations=[]; self.phases=[]
        self.error=None
    def observe(self):
        self.index+=1
        self.snapshot={"state":{"observation":{"kind":"feed"}},"options":{"WAIT":"wait"},"done":False}
        return self.snapshot
    def finish(self,extra=None):
        result={"status":"failed","error":self.error,**(extra or {})}
        (base/"result.json").write_text(json.dumps(result))
        (base/"phone-closed").write_text("closed")
        return result
class FakeClient:
    def __init__(self,*args,**kwargs): self.mode="warm"; self.decisionStyle="heads"
    async def decide(self,*args):
        print("MODEL_ENTERED",flush=True)
        await asyncio.sleep(60)
    async def close(self): (base/"client-closed").write_text("closed")
async def run():
    code=await worker.main(["--provider","jev-auto","--trial-id","shutdown-fixture"],runtime_factory=FakeRuntime,client_factory=FakeClient,key_reader=lambda:"offline-fixture",base=base)
    return code
raise SystemExit(asyncio.run(run()))
"""


def read_line(process, timeout=4):
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    assert ready, "Offline fixture did not respond before cancellation test timeout"
    line = process.stdout.readline().strip()
    if not line:
        assert process.poll() is None, process.stderr.read()
        pytest.fail("Offline fixture closed stdout without a protocol reply")
    return line


@pytest.mark.parametrize("stage", ["idle_stdin", "active_model"])
def test_real_worker_sigterm_finalizes_and_closes_resources(tmp_path, stage):
    wrapper = tmp_path / "offline_worker.py"
    wrapper.write_text(WRAPPER)
    env = {**os.environ, "PYTHONPATH": str(DIRECTORY)}
    process = subprocess.Popen(
        [sys.executable, "-u", str(wrapper), str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        process.stdin.write('{"id":"observe","command":"observe"}\n')
        process.stdin.flush()
        observed = json.loads(read_line(process))
        assert observed["result"]["done"] is False
        if stage == "active_model":
            process.stdin.write('{"id":"decide","command":"decide"}\n')
            process.stdin.flush()
            assert read_line(process) == "MODEL_ENTERED"
        process.send_signal(signal.SIGTERM)
        # Keep stdin open: this must test cancellation, not an EOF-driven exit.
        process.wait(timeout=4)
        stdout, stderr = process.communicate(timeout=4)
        assert process.returncode == 143, (stdout, stderr)
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "failed"
        assert (tmp_path / "phone-closed").read_text() == "closed"
        assert (tmp_path / "client-closed").read_text() == "closed"
        attempts = result["decisionPolicy"]["attempts"]
        assert len(attempts) == (1 if stage == "active_model" else 0)
        if attempts:
            assert attempts[0]["status"] == "cancelled"
        assert result["decisionPolicy"]["fallbackRequests"] == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
