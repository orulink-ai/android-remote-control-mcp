"""Exercise the extension's real IPC and turn boundaries with an offline worker."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


DIRECTORY = Path(__file__).resolve().parent
pytestmark = pytest.mark.skipif(shutil.which("pi") is None, reason="PI CLI not installed")

WORKER = '''import json,os,sys
from pathlib import Path
log=Path(os.environ["LIFECYCLE_LOG"])
for line in sys.stdin:
 request=json.loads(line); command=request["command"]
 with log.open("a") as stream:
  stream.write(json.dumps({"goal":os.environ["PHONE_TERMINAL_GOAL"],"trial":os.environ["PHONE_BENCH_TRIAL"],"command":command})+"\\n")
 if os.environ["PHONE_TERMINAL_GOAL"]=="读取手机屏幕但测试退出":
  break
 if command=="observe": value={"state":{},"options":{},"done":True}
 elif command=="finish": value={"status":"success","terminalTask":{"kind":"read"},"finalState":{}}
 else: raise AssertionError("No action or model request authorized by this fixture")
 print(json.dumps({"id":request["id"],"result":value}),flush=True)
 if command=="finish": break
'''


def probe(tmp_path, body):
    worker = tmp_path / "worker.py"
    worker.write_text(WORKER)
    audit = tmp_path / "audit.json"
    extension = tmp_path / "probe.ts"
    extension.write_text(
        "import {writeFileSync} from 'node:fs';\n"
        f"import {{registerPhoneBenchmark}} from {json.dumps(str(DIRECTORY / 'phone-extension.ts'))};\n"
        "export default async function(pi) {\n"
        " const handlers=new Map(), tools=new Map(), entries=[]; let provider;\n"
        " const fake={on(name,handler){handlers.set(name,handler)},\n"
        " registerTool(tool){tools.set(tool.name,tool)},registerCommand(){},\n"
        " registerProvider(_name,value){provider=value},appendEntry(name,value){entries.push({name,...value})}};\n"
        " registerPhoneBenchmark(fake,{terminal:true,terminalText:'complete'});\n"
        " const model={id:'auto',api:'phone-benchmark-stdio',provider:'phone-control'};\n"
        " const user=(text)=>({role:'user',content:text});\n"
        " const complete=(done=true)=>({role:'toolResult',toolName:'phone_observe',isError:false,content:[{type:'text',text:JSON.stringify({state:{},options:{WAIT:'wait'},done})}]});\n"
        " const respond=(messages)=>provider.streamSimple(model,{messages}).result();\n"
        " const start=()=>handlers.get('agent_start')();\n"
        " const end=()=>handlers.get('agent_end')();\n"
        " const read=async()=>tools.get('phone_observe').execute('test',{});\n"
        + body
        + f"\n writeFileSync({json.dumps(str(audit))},JSON.stringify({{results,entries}}));\n"
        " await handlers.get('session_shutdown')();\n"
        " pi.registerCommand('probe',{description:'offline lifecycle probe',handler:async()=>{}});\n"
        "}\n"
    )
    process = subprocess.run(
        [
            "pi", "--no-extensions", "-e", str(extension), "--no-mcp", "--no-skills",
            "--no-context-files", "--offline", "--mode", "json", "-p", "/probe",
        ],
        cwd=tmp_path,
        env={
            **os.environ, "PHONE_BENCH_PYTHON": sys.executable,
            "PHONE_BENCH_WORKER": str(worker), "LIFECYCLE_LOG": str(tmp_path / "worker.jsonl"),
            "PI_OFFLINE": "1", "PI_TELEMETRY": "0",
        },
        capture_output=True, text=True, timeout=25,
    )
    assert audit.exists(), process.stderr
    commands = tmp_path / "worker.jsonl"
    return json.loads(audit.read_text()), (
        [json.loads(line) for line in commands.read_text().splitlines()] if commands.exists() else []
    )


def test_new_agent_turn_starts_fresh_worker_after_completed_task(tmp_path):
    audit, commands = probe(tmp_path, '''
 const results=[];
 const messages=[];
 for (const goal of ['读取当前手机屏幕','查看当前手机页面']) {
  messages.push(user(goal)); await start();
  results.push(await respond(messages));
  await read(); messages.push(complete());
  results.push(await respond(messages)); await end();
 }
''')
    assert [r["stopReason"] for r in audit["results"]] == ["toolUse", "stop"] * 2
    assert [c["command"] for c in commands] == ["observe", "finish"] * 2
    assert [c["goal"] for c in commands] == ["读取当前手机屏幕"] * 2 + ["查看当前手机页面"] * 2
    assert len({c["trial"] for c in commands}) == 2


def test_new_agent_turn_recovers_after_worker_exits(tmp_path):
    audit, commands = probe(tmp_path, '''
 const results=[];
 await start(); await respond([user('读取手机屏幕但测试退出')]);
 try {await read()} catch {}
 results.push(await respond([user('读取手机屏幕但测试退出')])); await end();
 await start(); results.push(await respond([user('读取当前手机屏幕')]));
 await read(); results.push(await respond([user('读取当前手机屏幕'),complete()])); await end();
''')
    assert [r["stopReason"] for r in audit["results"]] == ["stop", "toolUse", "stop"]
    assert [c["command"] for c in commands] == ["observe", "observe", "finish"]
    assert len({c["trial"] for c in commands}) == 2


def test_previous_turn_observation_cannot_drive_new_task_without_new_read(tmp_path):
    audit, commands = probe(tmp_path, '''
 const results=[];
 const messages=[user('读取先前手机屏幕'),complete(false),user('读取当前手机屏幕')];
 await start(); results.push(await respond(messages));
 // A missing current tool result must fail closed, even with an old pending snapshot.
 results.push(await respond(messages)); await end();
''')
    assert audit["results"][0]["stopReason"] == "toolUse"
    text = "\n".join(block.get("text", "") for block in audit["results"][1]["content"])
    assert "已完成并验证" not in text
    assert "停止" in text
    assert commands == []
