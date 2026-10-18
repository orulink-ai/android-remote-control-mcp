"""Draft verification consumes actual recorded intent, never just a matching query."""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from controller import Progress
from test_workflow_intents import supported_raw
from workflow import compile_recording
from workflow_intents import TASK, derive_plan, evidence_matches_plan
from workflow_verification import DraftIntentRuntime, IntentVerificationError
from terminal_runtime import TerminalRecordedRuntime

DIRECTORY = Path(__file__).resolve().parent
ACTIONS = [
    {"method": "click", "role": "navigation", "label": "搜索"},
    {"method": "type", "role": "search_field", "label": "输入小狮日记"},
    {"method": "click", "role": "navigation", "label": "搜索"},
    {"method": "click", "role": "navigation", "label": TASK["expectedFirstTitle"]},
    {"method": "click", "role": "like", "label": "点赞"},
    {"method": "click", "role": "collect", "label": "收藏"},
    {"method": "click", "role": "like", "label": "取消点赞"},
    {"method": "click", "role": "collect", "label": "取消收藏"},
]


def screen(position):
    kinds = ["feed", "search_input", "search_input", "results"] + ["detail"] * 5
    stages = ["search"] * 3 + ["open_first", "like", "collect", "unlike", "uncollect", "done"]
    flags = [(None, None)] * 4 + [
        (False, False),
        (True, False),
        (True, True),
        (False, True),
        (False, False),
    ]
    actions = {
        "WAIT": {"method": "wait", "role": "wait", "label": "等待"},
        "BLOCKED": {"method": "blocked", "role": "blocked", "label": "停止"},
        "WRONG": {"method": "click", "role": "navigation", "label": "发布"},
    }
    if position < 8:
        actions["NEXT"] = copy.deepcopy(ACTIONS[position])
    liked, collected = flags[position]
    return {
        "state": {
            **TASK,
            "focusedPackage": "com.xingin.xhs",
            "stage": stages[position],
            "observation": {
                "kind": kinds[position],
                "queryMatches": position >= 2,
                "firstTitleVisible": position >= 3,
                "liked": liked,
                "collected": collected,
            },
            "actionDescriptors": actions,
        },
        "done": position == 8,
        "options": {key: value["label"] for key, value in actions.items()},
    }


def install_fake(monkeypatch=None):
    """Replace only MCP-facing base runtime, retaining real draft policy/worker."""

    def bind(name, value):
        if monkeypatch:
            monkeypatch.setattr(TerminalRecordedRuntime, name, value)
        else:
            setattr(TerminalRecordedRuntime, name, value)

    def init(self, trial, task, **_options):
        self.trial_id, self.position = trial, 0
        self.directory = Path(os.environ["PHONE_BENCH_OUTPUT_DIR"]) / trial
        self.directory.mkdir(parents=True, exist_ok=True)
        self.index, self.progress, self.error = 0, Progress(), None
        self.operations, self.phases, self.model_calls = [], [], []
        self.snapshot = None

    def observe(self):
        self.index += 1
        self.snapshot = screen(self.position)
        self.actions = copy.deepcopy(self.snapshot["state"]["actionDescriptors"])
        self.progress.phase = self.snapshot["state"]["stage"]
        if self.position >= 3:
            self.progress.milestones = [
                {"stage": stage}
                for stage in ["search", "open_first", "like", "collect", "unlike", "uncollect"][
                    : self.position - 2
                ]
            ]
        return self.snapshot

    def act(self, choice):
        if choice == "NEXT":
            self.position += 1
        elif choice != "WAIT":
            raise RuntimeError("Unexpected mutation")
        return self.observe()

    def finish(self, extra=None):
        value = {
            "status": "success" if self.progress.phase == "done" else "failed",
            "modelCalls": self.model_calls,
            "milestones": self.progress.milestones,
            "finalState": self.snapshot["state"]["observation"],
            **(extra or {}),
        }
        (self.directory / "result.json").write_text(json.dumps(value))
        return value

    bind("__init__", init)
    bind("observe", observe)
    bind("act", act)
    bind("finish", finish)
    bind("record_model", lambda self, value: self.model_calls.append(value))


def test_each_consumed_intent_immediately_exposes_next_live_action(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    plan = derive_plan(supported_raw())
    runtime = DraftIntentRuntime("verify", plan)
    runtime.observe()
    for index in range(8):
        assert "WRONG" not in runtime.actions
        assert "NEXT" in runtime.actions, (index, runtime.snapshot)
        runtime.act("NEXT")
        assert runtime.cursor == index + 1
    result = runtime.finish({"piAgentLoop": True})
    evidence = json.loads((tmp_path / "verify/recording.json").read_text())
    assert result["status"] == "success" and evidence_matches_plan(evidence, plan)
    assert len(compile_recording(evidence, tmp_path / "bundle")["steps"]) == 8


def test_unrecorded_action_and_unverified_terminal_state_never_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    runtime = DraftIntentRuntime("verify", derive_plan(supported_raw()))
    runtime.observe()
    with pytest.raises(IntentVerificationError):
        runtime.act("WRONG")
    runtime.progress.phase = "done"
    assert runtime.finish()["status"] == "failed"


@pytest.mark.parametrize("failure", ["none", "primary", "both"])
def test_real_pi_worker_consumes_draft_intents_with_mcp_and_models_doubled(tmp_path, failure):
    if not shutil.which("pi"):
        pytest.skip("PI not installed")
    plan = derive_plan(supported_raw())
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    wrapper = tmp_path / "offline-worker.py"
    wrapper.write_text("""import argparse,asyncio,json,os
from test_workflow_verification import install_fake
from workflow_verification import DraftIntentRuntime,load_plan
from providers import ProviderError
import worker
install_fake()
class Model:
    mode='warm'; decisionStyle='heads'
    def __init__(self,*a,**kw): self.calls=0
    async def decide(self,backend,state,options):
        self.calls+=1
        if os.environ['FAIL']=='both' and self.calls<=2 or os.environ['FAIL']=='primary' and self.calls==1:
            raise ProviderError('offline',network={},latency_ms=0,exception_type='ConnectError',stage='tcp_connect')
        return {'choice':'NEXT' if 'NEXT' in options else 'WAIT','latencyMs':0,'usage':{},'cost':0}
    async def close(self): pass
p=argparse.ArgumentParser();p.add_argument('--provider');p.add_argument('--trial-id');a=p.parse_args()
plan=load_plan(os.environ['PLAN'])
asyncio.run(worker.serve('jev-auto',a.trial_id,runtime_factory=lambda t:DraftIntentRuntime(t,plan),client_factory=Model,key_reader=lambda:'offline'))
""")
    env = {
        **os.environ,
        "PHONE_BENCH_OUTPUT_DIR": str(tmp_path),
        "PHONE_BENCH_PYTHON": sys.executable,
        "PHONE_BENCH_WORKER": str(wrapper),
        "PLAN": str(tmp_path / "plan.json"),
        "FAIL": failure,
        "PYTHONPATH": str(DIRECTORY),
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
    }
    env.pop("PHONE_TERMINAL_TASK", None)
    command = [
        "pi",
        "--no-extensions",
        "-e",
        str(DIRECTORY / "terminal-extension.ts"),
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
        "phone-control",
        "--model",
        "auto",
        "--thinking",
        "off",
        "--session",
        str(tmp_path / "session.jsonl"),
        "--mode",
        "json",
        "--print",
        "执行录制的小红书完整闭环",
    ]
    process = subprocess.run(
        command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25
    )
    assert process.returncode == 0, process.stderr
    paths = list(tmp_path.glob("terminal-*/result.json"))
    assert len(paths) == 1
    result = json.loads(paths[0].read_text())
    evidence = json.loads((paths[0].parent / "recording.json").read_text())
    assert result["piAgentLoop"] is True
    assert result["status"] == ("failed" if failure == "both" else "success")
    if failure == "both":
        assert evidence["steps"] == [] and result["decisionPolicy"]["fallbackRequests"] == 1
    else:
        assert evidence_matches_plan(evidence, plan)
        assert result["decisionPolicy"]["fallbackSuccesses"] == (failure == "primary")
        assert len(compile_recording(evidence, tmp_path / "bundle")["steps"]) == 8


def test_ambiguous_applied_delivery_cannot_become_paired_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    runtime = DraftIntentRuntime("verify", derive_plan(supported_raw()))
    runtime.observe()
    original = TerminalRecordedRuntime.act

    def ambiguous(self, choice):
        after = original(self, choice)
        after["execution"] = {"status": "applied", "uncertain": True}
        return after

    monkeypatch.setattr(TerminalRecordedRuntime, "act", ambiguous)
    with pytest.raises(IntentVerificationError):
        runtime.act("NEXT")
    assert runtime.cursor == 0 and runtime.recorded_steps == []
    assert runtime.finish()["status"] == "failed"


@pytest.mark.parametrize("back_count", [4, 5])
def test_observed_back_transitions_prepare_feed_without_consuming_intent(
    tmp_path, monkeypatch, back_count
):
    """Redmi retained detail/results/input/IME stack needs more than three BACKs."""
    from terminal_runtime import BACK

    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    install_fake(monkeypatch)
    original_observe = TerminalRecordedRuntime.observe
    original_act = TerminalRecordedRuntime.act

    def observe(self):
        snapshot = original_observe(self)
        if self.remaining_backs:
            snapshot["state"]["observation"]["kind"] = [
                "detail",
                "results",
                "search_input",
                "search_input",
                "transition",
            ][5 - self.remaining_backs]
        return snapshot

    def act(self, choice):
        if choice == BACK:
            self.remaining_backs -= 1
            return self.observe()
        return original_act(self, choice)

    monkeypatch.setattr(TerminalRecordedRuntime, "observe", observe)
    monkeypatch.setattr(TerminalRecordedRuntime, "act", act)
    runtime = DraftIntentRuntime("verify", derive_plan(supported_raw()))
    runtime.remaining_backs = back_count
    runtime.observe()
    for _ in range(back_count):
        assert BACK in runtime.actions and "NEXT" not in runtime.actions
        runtime.act(BACK)
        assert runtime.cursor == 0
    assert len(runtime.preparation_steps) == back_count and runtime.recorded_steps == []
    assert "NEXT" in runtime.actions
    for _ in range(8):
        runtime.act("NEXT")
    assert runtime.finish()["status"] == "success"
