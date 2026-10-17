"""Real PI terminal extension + real worker/runtime; only MCP/model are doubles."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

DIRECTORY = Path(__file__).resolve().parent

WRAPPER = '''import asyncio,json,os
from pathlib import Path
import terminal_runtime,worker
from providers import ProviderError
base=Path(os.environ["PHONE_BENCH_OUTPUT_DIR"])
log=base/"offline-terminal.jsonl"
def event(name,**fields):
    with log.open("a") as f: f.write(json.dumps({"name":name,**fields})+"\\n")
def result(text): return {"content":[{"type":"text","text":text}]}
class Mcp:
    server_info={"name":"offline-mcp-only"}
    def __init__(self): self.package="com.sec.android.app.launcher"; self.page="feed"; self.query=""
    def call(self,name,args):
        event("tool",tool=name,args=args)
        if name=="android_list_apps": return result(json.dumps([{"name":"小红书","package_id":"com.xingin.xhs"}]))
        if name=="android_open_app": self.package=args["package_id"]
        if name=="android_click_node":
            if args["node_id"]=="node_aa": self.page="input"
            else: self.page="results"
        if name=="android_type_clear_text": self.query=""
        if name=="android_type_append_text": self.query=args["text"]
        if name=="android_get_screen_state":
            text="screen:1080x2340\\n--- window:1 pkg:"+self.package+" focused:true ---\\n"
            if self.package=="com.xingin.xhs":
                if self.page=="feed": text+="node_aa\\tTextView\\t搜索\\t-\\t-\\t100,100,250,200\\ton,ena,clk"
                else:
                    text+="node_bb\\tEditText\\t"+self.query+"\\t-\\t-\\t180,80,760,190\\ton,ena,clk\\n"
                    text+="node_cc\\tTextView\\t搜索\\t-\\t-\\t800,80,950,190\\ton,ena,clk"
                    if self.page=="results": text+="\\nnode_dd\\tTextView\\t综合\\t-\\t-\\t100,250,200,300\\ton,ena"
            return result(text)
        return result("success")
    def close(self): event("mcp_close")
terminal_runtime.OwnMcp=Mcp
class Model:
    def __init__(self,*args,**kwargs): self.mode="warm"; self.decisionStyle="heads"; self.count=0
    async def decide(self,backend,state,options):
        self.count+=1; event("model",backend=backend,options=list(options))
        if os.environ["OFFLINE_FAILURE"]=="first" and self.count==1:
            raise ProviderError("offline",network={},latency_ms=0,exception_type="ConnectError",stage="tcp_connect")
        if any(key.startswith("OPEN_APP:") for key in options): choice=next(key for key in options if key.startswith("OPEN_APP:"))
        elif state["observation"].get("queryMatches"):
            choice="CLICK:node_cc"
        elif any(key.startswith("TYPE:") for key in options): choice=next(key for key in options if key.startswith("TYPE:"))
        else: choice="CLICK:node_aa"
        return {"choice":choice,"latencyMs":0,"usage":{},"cost":0}
    async def close(self): event("model_close")
def key(): event("key_read"); return "offline-not-a-real-key"
raise SystemExit(asyncio.run(worker.main(client_factory=Model,key_reader=key)))
'''


@pytest.mark.parametrize("goal,expected_backends", [
    ("读取当前屏幕", []),
    ("打开小红书", ["jev", "deepseek"]),
    ("在小红书搜索“猫咪”", ["jev"] * 4),
])
def test_terminal_actual_pi_worker_runtime_mcp_only(tmp_path, goal, expected_backends):
    if shutil.which("pi") is None:
        pytest.skip("PI not installed")
    extension = DIRECTORY / "terminal-extension.ts"
    assert extension.exists(), "Terminal extension must exist before its integration contract can pass"
    wrapper = tmp_path / "offline-terminal-worker.py"
    wrapper.write_text(WRAPPER)
    env = {
        **os.environ, "PHONE_BENCH_PYTHON": sys.executable, "PHONE_BENCH_WORKER": str(wrapper),
        "PHONE_BENCH_OUTPUT_DIR": str(tmp_path), "PYTHONPATH": str(DIRECTORY),
        "OFFLINE_FAILURE": "first" if goal == "打开小红书" else "none",
        "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
    }
    process = subprocess.run([
        "pi", "--no-extensions", "-e", str(extension), "--no-mcp", "--no-skills",
        "--no-context-files", "--no-prompt-templates", "--no-themes", "--offline",
        "--no-builtin-tools", "--tools", "phone_observe,phone_action", "--provider",
        "phone-control", "--model", "auto", "--thinking", "off", "--session",
        str(tmp_path / "session.jsonl"), "--mode", "json", "--print", goal,
    ], cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=25)
    assert process.returncode == 0, process.stderr
    logs = [json.loads(line) for line in (tmp_path / "offline-terminal.jsonl").read_text().splitlines()]
    assert [item["backend"] for item in logs if item["name"] == "model"] == expected_backends
    assert sum(item["name"] == "key_read" for item in logs) == bool(expected_backends)
    results = list(tmp_path.glob("*/result.json"))
    assert len(results) == 1
    result = json.loads(results[0].read_text())
    assert result["status"] == "success" and result["provider"] == "jev-auto"
    assert all(item["tool"].startswith("android_") for item in logs if item["name"] == "tool")
    assert result["terminalTask"]["goal"] == goal
