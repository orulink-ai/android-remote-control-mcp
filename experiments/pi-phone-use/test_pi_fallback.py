"""Real PI offline protocol tests; the fake worker cannot access a phone or API."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


DIRECTORY = Path(__file__).parent
pytestmark = pytest.mark.skipif(shutil.which("pi") is None, reason="PI is not installed")

FAKE_WORKER = """import json,os,sys
from pathlib import Path
mode=os.environ["FAKE_MODE"]
log=Path(os.environ["FAKE_LOG"])
observes=0
decisions=0
done=False
def observe():
    global observes
    observes+=1
    return snapshot()
def snapshot():
    key="old" if observes==1 else "new"
    return {"state":{"snapshot":key},"options":{"CLICK:"+key:"Tap "+key},"done":done}
for line in sys.stdin:
    req=json.loads(line)
    with log.open("a") as output: output.write(json.dumps(req)+"\\n")
    command=req["command"]
    error=False
    if command=="observe": result=observe()
    elif command=="decide":
        decisions+=1
        if mode=="worker-error": error=True; result=None
        elif mode in ["auto-refresh","malformed-pending","marker-choice","marker-wrong-kind","isolated-refresh","stale-choice"] and decisions==1:
            result={"kind":"refresh_required","fallbackPending":True}
            if mode=="malformed-pending": result["fallbackPending"]="true"
            if mode=="marker-choice": result["choice"]="CLICK:old"
            if mode=="marker-wrong-kind": result["kind"]="anything"
        else:
            key="old" if observes==1 or mode=="stale-choice" else "new"
            result={"kind":"decision","choice":"CLICK:"+key,"selectedBackend":"deepseek" if observes>1 else "jev","fallbackUsed":observes>1,"attemptId":"attempt-2" if observes>1 else "attempt-1","usage":{},"latencyMs":0}
            if mode=="legacy-primary": del result["kind"]
            if mode=="unknown-kind-choice": result["kind"]="unknown"
            if mode=="decision-with-pending": result["fallbackPending"]=True
    elif command=="act": done=True; result=snapshot()
    elif command=="finish": result={"status":"success" if done else "failed","decisionPolicy":{"mode":"jev-auto","fallbackCount":int(observes>1)}}
    if error: print(json.dumps({"id":req["id"],"error":"fixture refusal"}),flush=True)
    else: print(json.dumps({"id":req["id"],"result":result}),flush=True)
    if command=="finish": break
"""


def run_pi(tmp_path, mode, backend="jev-auto"):
    worker = tmp_path / "offline-worker.py"
    worker.write_text(FAKE_WORKER)
    log = tmp_path / "worker-requests.jsonl"
    session = tmp_path / "pi-session.jsonl"
    command = [
        "pi",
        "--no-extensions",
        "-e",
        str(DIRECTORY / "pi-extension.ts"),
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
        backend,
        "--thinking",
        "off",
        "--session",
        str(session),
        "--mode",
        "json",
        "--print",
        "Run the offline phone protocol fixture.",
    ]
    env = {
        **os.environ,
        "PHONE_BENCH_PYTHON": sys.executable,
        "PHONE_BENCH_WORKER": str(worker),
        "PHONE_BENCH_TRIAL": "offline-fallback",
        "FAKE_MODE": mode,
        "FAKE_LOG": str(log),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
    }
    process = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
    )
    events = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
    requests = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    entries = (
        [json.loads(line) for line in session.read_text().splitlines()] if session.exists() else []
    )
    return process, events, requests, entries


def tool_sequence(events):
    return [event["toolName"] for event in events if event["type"] == "tool_execution_start"]


def custom_data(entries, name):
    return [entry["data"] for entry in entries if entry.get("customType") == name]


def test_auto_refresh_is_a_real_observe_tool_before_the_fallback_action(tmp_path):
    process, events, requests, entries = run_pi(tmp_path, "auto-refresh")
    assert process.returncode == 0, process.stderr
    assert tool_sequence(events) == ["phone_observe", "phone_observe", "phone_action"]
    assert [r["command"] for r in requests] == [
        "observe",
        "decide",
        "observe",
        "decide",
        "act",
        "finish",
    ]
    assert [r["choice"] for r in requests if r["command"] == "act"] == ["CLICK:new"]
    (decision,) = custom_data(entries, "phone-benchmark-decision")
    assert decision["selectedBackend"] == "deepseek"
    assert decision["fallbackUsed"] is True
    assert decision["attemptId"] == "attempt-2"
    (finish,) = custom_data(entries, "phone-benchmark-finish")
    assert finish["decisionPolicy"] == {"mode": "jev-auto", "fallbackCount": 1}


@pytest.mark.parametrize("backend", ["jev-auto", "jev", "deepseek"])
def test_valid_primary_decision_has_no_extra_observation(tmp_path, backend):
    process, events, requests, entries = run_pi(tmp_path, "primary", backend)
    assert process.returncode == 0, process.stderr
    assert tool_sequence(events) == ["phone_observe", "phone_action"]
    assert [r["choice"] for r in requests if r["command"] == "act"] == ["CLICK:old"]
    (decision,) = custom_data(entries, "phone-benchmark-decision")
    assert decision["selectedBackend"] == "jev" and decision["fallbackUsed"] is False


def test_legacy_untagged_isolated_decision_remains_supported(tmp_path):
    _, events, requests, _ = run_pi(tmp_path, "legacy-primary", "jev")
    assert tool_sequence(events) == ["phone_observe", "phone_action"]
    assert [request["choice"] for request in requests if request["command"] == "act"] == [
        "CLICK:old"
    ]


@pytest.mark.parametrize(
    "mode,backend",
    [
        ("malformed-pending", "jev-auto"),
        ("marker-choice", "jev-auto"),
        ("marker-wrong-kind", "jev-auto"),
        ("isolated-refresh", "jev"),
        ("isolated-refresh", "deepseek"),
        ("worker-error", "jev-auto"),
        ("unknown-kind-choice", "jev-auto"),
        ("decision-with-pending", "jev-auto"),
    ],
)
def test_invalid_refresh_or_worker_error_stops_without_an_action(tmp_path, mode, backend):
    _, events, requests, entries = run_pi(tmp_path, mode, backend)
    assert tool_sequence(events) == ["phone_observe"]
    assert all(request["command"] != "act" for request in requests)
    assert custom_data(entries, "phone-benchmark-decision") == []
    assert any(
        event.get("type") == "message_end" and event.get("message", {}).get("stopReason") == "error"
        for event in events
    )


def test_fallback_cannot_use_the_old_observations_choice(tmp_path):
    _, events, requests, entries = run_pi(tmp_path, "stale-choice")
    assert tool_sequence(events) == ["phone_observe", "phone_observe"]
    assert all(request["command"] != "act" for request in requests)
    assert custom_data(entries, "phone-benchmark-decision") == []
