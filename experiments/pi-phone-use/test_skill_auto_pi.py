"""Actual PI -> actual worker/policies; injected phone/model cannot touch devices."""

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from controller import QUERY, TITLE
from workflow import compile_recording

DIRECTORY = Path(__file__).resolve().parent
STAGES = ["search", "open_first", "like", "collect", "unlike", "uncollect", "done"]
OBSERVATIONS = [
    {"kind": "feed", "firstTitleVisible": False},
    {"kind": "results", "firstTitleVisible": True, "queryMatches": True},
    {"kind": "detail", "firstTitleVisible": True, "liked": False, "collected": False},
    {"kind": "detail", "firstTitleVisible": True, "liked": True, "collected": False},
    {"kind": "detail", "firstTitleVisible": True, "liked": True, "collected": True},
    {"kind": "detail", "firstTitleVisible": True, "liked": False, "collected": True},
    {"kind": "detail", "firstTitleVisible": True, "liked": False, "collected": False},
]
DESCRIPTORS = [
    {"method": "click", "role": "navigation", "label": "搜索"},
    {"method": "click", "role": "navigation", "label": TITLE},
    {"method": "click", "role": "like", "label": "点赞"},
    {"method": "click", "role": "collect", "label": "收藏"},
    {"method": "click", "role": "like", "label": "取消点赞"},
    {"method": "click", "role": "collect", "label": "取消收藏"},
    {"method": "wait", "role": "wait", "label": "完成"},
]


def demonstration():
    states = []
    for i in range(7):
        choice = f"CLICK:demo-{i}"
        states.append({
            "state": {
                "stage": STAGES[i], "query": QUERY, "expectedFirstTitle": TITLE,
                "observation": copy.deepcopy(OBSERVATIONS[i]),
                "actionDescriptors": {choice: copy.deepcopy(DESCRIPTORS[i])},
            },
            "options": {choice: DESCRIPTORS[i]["label"]}, "done": i == 6,
        })
    return {
        "schemaVersion": 1, "source": "pi-agent-demonstration", "status": "success",
        "task": {"query": QUERY, "expectedFirstTitle": TITLE},
        "steps": [{"before": states[i], "after": states[i + 1], "action": DESCRIPTORS[i]}
                  for i in range(6)],
    }


WRAPPER = '''import asyncio,copy,json,os,sys
from pathlib import Path
import worker
from controller import Progress
from own_mcp import McpError
from providers import ProviderError
base=Path(os.environ["PHONE_BENCH_OUTPUT_DIR"])
fixture=json.loads(Path(os.environ["OFFLINE_FIXTURE"]).read_text())
scenario=os.environ["OFFLINE_SCENARIO"]
log=base/"offline-protocol.jsonl"
def event(name,**fields):
    with log.open("a") as f: f.write(json.dumps({"name":name,**fields})+"\\n")
class FakePhone:
    def __init__(self,trial):
        self.trial=trial; self.frame=0; self.index=0; self.snapshot=None
        self.operations=[]; self.phases=[]; self.model_calls=[]; self.error=None
        self.progress=Progress()
    def observe(self):
        self.index+=1
        data=fixture["steps"][self.frame]["before"] if self.frame<6 else fixture["steps"][-1]["after"]
        data=copy.deepcopy(data)
        if scenario in {"drift","jev-fails","both-fail","cancelled"} and self.frame==0:
            data["state"]["observation"]["kind"]="launcher"
        if scenario=="task-change": data["state"]["query"]="different target"
        self.progress.update(data["state"]["observation"])
        data["state"]["stage"]=self.progress.phase
        descriptor=next(iter(data["state"]["actionDescriptors"].values()))
        choice="CLICK:live-"+str(self.index)
        data["state"]["actionDescriptors"]={choice:descriptor}
        data["options"]={choice:descriptor["label"]}
        if scenario=="ambiguous":
            data["state"]["actionDescriptors"]["CLICK:duplicate"]=copy.deepcopy(descriptor)
            data["options"]["CLICK:duplicate"]=descriptor["label"]
        data["done"]=self.progress.phase=="done"
        self.snapshot=data
        event("observe",observation=self.index,frame=self.frame)
        return data
    def act(self,choice):
        event("act",choice=choice,observation=self.index)
        if scenario=="budget": raise RuntimeError("Action step budget exceeded")
        assert choice in self.snapshot["options"]
        self.frame+=1
        if scenario=="uncertain":
            raise McpError("uncertain delivery",stage="tool_call",exception_type="ReadTimeout")
        after=self.observe()
        if scenario=="partial": after["execution"]={"status":"partial"}
        return after
    def record_model(self,value):
        self.model_calls.append(value)
        self.phases.append({"kind":"model","ms":value["latencyMs"]})
    def finish(self,extra=None):
        result={"status":"success" if self.progress.phase=="done" else "failed", "modelCalls":self.model_calls,"operations":self.operations,"phases":self.phases,"milestones":self.progress.milestones,"error":self.error,**(extra or {})}
        directory=base/self.trial; directory.mkdir(exist_ok=True)
        (directory/"result.json").write_text(json.dumps(result))
        event("phone_close")
        return result
class FakeModel:
    def __init__(self,*args,**kwargs):
        self.mode="warm"; self.decisionStyle="heads"; self.calls=0
        event("model_init")
    async def decide(self,backend,state,options):
        self.calls+=1; event("model",backend=backend,options=list(options))
        if scenario=="cancelled": raise asyncio.CancelledError()
        if scenario=="both-fail" or scenario=="jev-fails" and self.calls==1:
            raise ProviderError("fixture",network={},latency_ms=0,exception_type="ConnectError",stage="tcp_connect")
        return {"choice":next(iter(options)),"latencyMs":0,"usage":{},"cost":0}
    async def close(self): event("model_close")
def key(): event("key_read"); return "offline-no-api-key"
raise SystemExit(asyncio.run(worker.main(runtime_factory=FakePhone,client_factory=FakeModel,key_reader=key,base=base)))
'''


def actual_pi(tmp_path, scenario):
    if shutil.which("pi") is None:
        pytest.skip("PI not installed")
    data = demonstration()
    compile_recording(data, tmp_path / "bundle")
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(data))
    wrapper = tmp_path / "offline-worker.py"
    wrapper.write_text(WRAPPER)
    env = {
        **os.environ, "PHONE_BENCH_PYTHON": sys.executable,
        "PHONE_BENCH_WORKER": str(wrapper), "PHONE_BENCH_TRIAL": "skill-auto-fixture",
        "PHONE_BENCH_OUTPUT_DIR": str(tmp_path), "PHONE_BENCH_RECIPE": str(tmp_path / "bundle/recipe.json"),
        "PYTHONPATH": str(DIRECTORY), "OFFLINE_FIXTURE": str(fixture),
        "OFFLINE_SCENARIO": scenario, "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
    }
    process = subprocess.run([
        "pi", "--no-extensions", "-e", str(DIRECTORY / "pi-extension.ts"),
        "--no-mcp", "--no-skills", "--no-context-files", "--no-prompt-templates",
        "--no-themes", "--offline", "--no-builtin-tools", "--tools", "phone_observe,phone_action",
        "--provider", "phone-benchmark", "--model", "skill-auto", "--thinking", "off",
        "--session", str(tmp_path / "pi-session.jsonl"), "--mode", "json", "--print",
        "Execute the synthetic offline phone task.",
    ], cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=25)
    assert process.returncode == 0, process.stderr
    events = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
    log = [json.loads(line) for line in (tmp_path / "offline-protocol.jsonl").read_text().splitlines()]
    result = json.loads((tmp_path / "skill-auto-fixture/result.json").read_text())
    return events, log, result


@pytest.mark.parametrize("scenario,models,extra_observe", [
    ("cached", [], 0), ("drift", ["jev"] * 6, 0),
    ("jev-fails", ["jev", "deepseek"] + ["jev"] * 5, 1),
])
def test_actual_pi_worker_policy_success_paths(tmp_path, scenario, models, extra_observe):
    events, log, result = actual_pi(tmp_path, scenario)
    assert result["status"] == "success"
    assert [item["backend"] for item in log if item["name"] == "model"] == models
    tools = [event["toolName"] for event in events if event["type"] == "tool_execution_start"]
    assert tools == ["phone_observe"] * (1 + extra_observe) + ["phone_action"] * 6
    assert len([item for item in log if item["name"] == "act"]) == 6
    assert result["recipeExecution"]["verifiedSteps"] == (6 if scenario == "cached" else 0)
    assert len(result["milestones"]) == 6
    assert sum(item["name"] == "key_read" for item in log) == bool(models)
    if extra_observe:
        first, second = [item for item in log if item["name"] == "model"][:2]
        assert first["options"] != second["options"]


@pytest.mark.parametrize("scenario,models,actions", [
    ("both-fail", ["jev", "deepseek"], 0), ("uncertain", [], 1),
    ("partial", [], 1), ("task-change", [], 0), ("ambiguous", [], 0),
    ("budget", [], 1), ("cancelled", ["jev"], 0),
])
def test_actual_pi_worker_policy_critical_failures_stop(tmp_path, scenario, models, actions):
    events, log, result = actual_pi(tmp_path, scenario)
    assert result["status"] == "failed"
    assert [item["backend"] for item in log if item["name"] == "model"] == models
    assert len([item for item in log if item["name"] == "act"]) == actions
    assert any(event.get("type") == "message_end" and
               event.get("message", {}).get("stopReason") == "error" for event in events)
    assert sum(item["name"] == "key_read" for item in log) == bool(models)
