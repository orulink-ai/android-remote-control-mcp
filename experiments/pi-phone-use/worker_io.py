"""Cancelable Unix CLI input and SIGTERM handling, with no phone/model access.

The signal scope belongs exclusively to the main thread's top-level CLI. It is
not nested and does not replace another asyncio signal callback. Injected text
streams stay synchronous for deterministic offline protocol tests.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import signal
import sys


async def protocol_lines(stream):
    """Yield protocol lines without blocking cancellation on real CLI stdin.

    Callers should use contextlib.aclosing so an early protocol finish also
    releases the read transport. Closing it closes CLI stdin, which is owned by
    this worker process. StringIO/test streams remain borrowed and open.
    """
    if stream is not sys.stdin:
        for line in stream:
            yield line
        return
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=1024 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, stream)
    try:
        while line := await reader.readline():
            yield line.decode(stream.encoding or "utf-8")
    finally:
        transport.close()


@asynccontextmanager
async def cancellation_scope():
    """Turn SIGTERM into task cancellation so worker finally blocks can run.

    A surrounding CLI catches CancelledError and returns status 143. Blocking
    synchronous MCP operations remain bounded by their HTTP timeout; a parent
    process may still apply its final SIGKILL deadline when shutdown cannot finish.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous_handler = signal.getsignal(signal.SIGTERM)
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        yield
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous_handler)
