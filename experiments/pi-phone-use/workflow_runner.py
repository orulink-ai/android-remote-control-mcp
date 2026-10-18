"""Workflow jobs reuse the LAN PI terminal entry; never reset via device shell."""

from __future__ import annotations

import json
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import time

import httpx

from controller import private_write
from own_mcp import OwnMcp
from terminal_launcher import build_launch

DIRECTORY = Path(__file__).resolve().parent


class WorkflowRunError(RuntimeError):
    pass


class WorkflowStopped(BaseException):
    """Top-level SIGTERM must unwind all owned PI process groups."""


@contextmanager
def termination_scope():
    """CLI main-thread scope; restore the ordinary previous signal handler."""
    previous = signal.getsignal(signal.SIGTERM)

    def stop(_signum, _frame):
        raise WorkflowStopped()

    signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def load_profile(path=None):
    path = Path(
        path or os.environ.get("PI_PHONE_PROFILE", "~/.config/orulink-phone/terminal.json")
    ).expanduser()
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or not Path(value["python"]).is_absolute():
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise WorkflowRunError("Invalid PI phone profile") from None


def profile_mcp(profile):
    """Read credentials privately from the same MCP configuration as pi-phone."""
    try:
        server = json.loads(Path(profile["mcpConfig"]).expanduser().read_text())["mcpServers"][
            profile["mcpServer"]
        ]
        url, headers = server["url"], server.get("headers", {})
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError
        return OwnMcp(url=url, headers=headers)
    except (OSError, ValueError, TypeError, KeyError):
        raise WorkflowRunError("Invalid configured Android MCP") from None


class FeedbackMcp:
    """Cancellable, short-timeout MCP session restricted to workflow feedback."""

    def __init__(self, url, headers, *, timeout):
        self.url, self.sequence, self.session_id = url, 1, None
        self.client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            headers={**headers, "Accept": "application/json, text/event-stream"},
        )

    async def initialize(self):
        response = await self.client.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "phone-workflow-feedback", "version": "1"},
                },
            },
        )
        response.raise_for_status()
        if "error" in response.json():
            raise WorkflowRunError("Workflow feedback initialization rejected")
        self.session_id = response.headers.get("mcp-session-id")
        if self.session_id:
            self.client.headers["Mcp-Session-Id"] = self.session_id
        response = await self.client.post(
            self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )
        response.raise_for_status()

    async def call(self, name, args):
        if name not in {"android_workflow_update", "android_workflow_status"}:
            raise WorkflowRunError("Feedback session cannot dispatch phone actions")
        self.sequence += 1
        response = await self.client.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": self.sequence,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        )
        response.raise_for_status()
        body = response.json()
        if "error" in body or body.get("result", {}).get("isError"):
            raise WorkflowRunError("Workflow feedback request rejected")
        return body["result"]

    async def abort(self):
        # Cancellation performs no further HTTP request beyond the total budget.
        await self.client.aclose()

    async def close(self):
        try:
            if self.session_id:
                await self.client.delete(self.url)
        finally:
            await self.abort()


async def feedback_mcp(profile, *, timeout):
    try:
        server = json.loads(Path(profile["mcpConfig"]).expanduser().read_text())["mcpServers"][
            profile["mcpServer"]
        ]
        url, headers = server["url"], server.get("headers", {})
        if (
            not isinstance(url, str)
            or not url.startswith(("http://", "https://"))
            or not isinstance(headers, dict)
        ):
            raise ValueError
        client = FeedbackMcp(url, headers, timeout=timeout)
    except (OSError, ValueError, TypeError, KeyError):
        raise WorkflowRunError("Invalid workflow feedback configuration") from None
    try:
        await client.initialize()
        return client
    except BaseException:
        await client.abort()
        raise


def phase_launch(profile, phase, directory, *, recording, recipe, plan, environment):
    if (phase, recording) not in {("verify", True), ("execute", False)}:
        raise WorkflowRunError("Unsupported workflow phase")
    env = dict(environment)
    for key in (
        "ADB",
        "PHONE_BENCH_DEVICE",
        "PHONE_WORKFLOW_USB_FORWARD",
        "PHONE_TERMINAL_TASK",
        "PHONE_BENCH_WORKER",
        "PHONE_WORKFLOW_PLAN",
    ):
        env.pop(key, None)
    args = [
        "--no-context-files",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--mode",
        "json",
        "--print",
        "执行录制的小红书完整闭环",
    ]
    command, env = build_launch(profile, args, env)
    env["PHONE_BENCH_OUTPUT_DIR"] = str(Path(directory) / "pi")
    if recording:
        if plan is None:
            raise WorkflowRunError("A validated recorded intent plan is required")
        env["PHONE_WORKFLOW_PLAN"] = str(plan)
        env["PHONE_BENCH_WORKER"] = str(DIRECTORY / "workflow_verification.py")
    else:
        if recipe is None:
            raise WorkflowRunError("A generated recipe is required")
        env["PHONE_BENCH_RECIPE"] = str(recipe)
    return command, env


def stop_owned_group(process):
    """Bound the entire PI session, even after its parent has already exited.

    ``process`` must come from our ``start_new_session=True`` Popen. Waiting on
    that parent alone cannot prove its inherited worker process group is empty.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 5
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    while True:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        raise WorkflowRunError("Owned PI process group did not terminate") from None


def run_pi(provider, trial_id, directory, *, recording, recipe, heartbeat, plan=None, profile=None):
    """One owned real PI process group; lease loss stops it before returning."""
    if (provider, trial_id, recording) not in {
        ("jev-auto", "verify", True),
        ("skill-auto", "execute", False),
    }:
        raise WorkflowRunError("Unsupported PI workflow phase")
    phase_dir = Path(directory) / trial_id
    phase_dir.mkdir(mode=0o700, exist_ok=False)
    command, env = phase_launch(
        profile if profile is not None else load_profile(),
        trial_id,
        phase_dir,
        recording=recording,
        recipe=recipe,
        plan=plan,
        environment=os.environ,
    )
    process = None
    heartbeat()
    started = last_heartbeat = time.monotonic()
    try:
        path = phase_dir / "pi.log"
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            process = subprocess.Popen(
                command,
                env=env,
                cwd=DIRECTORY,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
                umask=0o077,
            )
            while process.poll() is None:
                now = time.monotonic()
                if now - started > 270:
                    raise WorkflowRunError("PI workflow deadline exceeded")
                if now - last_heartbeat >= 10:
                    heartbeat()
                    last_heartbeat = now
                time.sleep(0.2)
        results = list((phase_dir / "pi").glob("*/result.json"))
        if len(results) != 1 or not results[0].resolve().is_relative_to(phase_dir.resolve()):
            raise WorkflowRunError("PI did not produce one bounded result")
        result = json.loads(results[0].read_text())
        if process.returncode != 0:
            result["status"] = "failed"
        private_write(phase_dir / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
        if recording and (results[0].parent / "recording.json").is_file():
            private_write(
                phase_dir / "recording.json", (results[0].parent / "recording.json").read_text()
            )
        return result
    finally:
        if process is not None:
            stop_owned_group(process)
