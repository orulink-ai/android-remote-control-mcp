"""Exercise the user's installed-entry contract through real PI, not a different launcher."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from terminal_launcher import build_launch
from test_workflow import demo
from workflow import compile_recording


def profile(tmp_path):
    recording = demo()
    title = "当我闯进一个限时48小时的创造营"
    recording["task"]["expectedFirstTitle"] = title
    for s in recording["steps"]:
        for key in ("before", "after"):
            s[key]["state"]["expectedFirstTitle"] = title
    compile_recording(recording, tmp_path / "bundle")
    mcp = tmp_path / "mcp.json"
    mcp.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "phone": {
                        "url": "http://192.168.1.2:8080/mcp",
                        "headers": {"Authorization": "Bearer do-not-copy"},
                    }
                }
            }
        )
    )
    return {
        "recipe": str(tmp_path / "bundle/recipe.json"),
        "mcpConfig": str(mcp),
        "mcpServer": "phone",
        "python": "/fixture/python",
    }


def test_launcher_selects_registered_phone_provider_and_only_phone_tools(tmp_path):
    command, env = build_launch(profile(tmp_path), ["--session", "user-session"], {})
    assert command[command.index("--provider") + 1] == "phone-control"
    assert command[command.index("--model") + 1] == "auto"
    assert command[command.index("--tools") + 1] == "phone_observe,phone_action"
    assert "--no-builtin-tools" in command and "--no-mcp" in command
    assert "user-session" in command
    assert env["ANDROID_MCP_URL"] == "http://192.168.1.2:8080/mcp"
    assert "do-not-copy" not in json.dumps(command) + json.dumps(env)
    assert "adb" not in " ".join(command).lower()


def test_launcher_rejects_model_or_tool_override_in_phone_only_entry(tmp_path):
    with pytest.raises(ValueError):
        build_launch(profile(tmp_path), ["--tools", "bash"], {})


def test_missing_recipe_does_not_silently_fall_back_to_native_deepseek(tmp_path):
    value = profile(tmp_path)
    value["recipe"] = str(tmp_path / "missing.json")
    with pytest.raises((ValueError, OSError)):
        build_launch(value, [], {})


@pytest.mark.skipif(shutil.which("pi") is None, reason="PI CLI not installed")
def test_actual_terminal_extension_gets_latest_goal_and_registered_provider(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text("""import json,os,sys
from pathlib import Path
p=Path(os.environ["TERMINAL_TEST_LOG"])
with p.open("a") as f:f.write(json.dumps({"argv":sys.argv,"goal":os.environ.get("PHONE_TERMINAL_GOAL"),"trial":os.environ["PHONE_BENCH_TRIAL"]})+"\\n")
for line in sys.stdin:
 r=json.loads(line);command=r["command"]
 if command=="observe":v={"state":{"goal":os.environ["PHONE_TERMINAL_GOAL"],"observation":{"kind":"screen"}},"options":{"WAIT":"等待"},"done":True}
 elif command=="finish":v={"status":"success","modelCalls":[],"operations":[],"milestones":[],"decisionPolicy":{"attempts":[]},"terminalTask":{"kind":"read"},"finalState":{"focusedPackage":"test.app"}}
 else:raise RuntimeError("Unexpected action")
 print(json.dumps({"id":r["id"],"result":v}),flush=True)
 if command=="finish":break
""")
    env = {
        **os.environ,
        "PHONE_BENCH_PYTHON": os.sys.executable,
        "PHONE_BENCH_WORKER": str(worker),
        "TERMINAL_TEST_LOG": str(tmp_path / "calls.jsonl"),
        "PI_OFFLINE": "1",
    }
    extension = Path(__file__).with_name("terminal-extension.ts")
    session = tmp_path / "session.jsonl"
    command = [
        "pi",
        "--no-extensions",
        "-e",
        str(extension),
        "--no-mcp",
        "--no-skills",
        "--no-context-files",
        "--no-builtin-tools",
        "--tools",
        "phone_observe,phone_action",
        "--provider",
        "phone-control",
        "--model",
        "auto",
        "--offline",
        "--thinking",
        "off",
        "--session",
        str(session),
        "--mode",
        "json",
        "-p",
    ]
    for goal in ("读取当前手机屏幕", "查看当前手机页面", "/skill:xhs-recorded-roundtrip"):
        result = subprocess.run(
            command + [goal], env=env, capture_output=True, text=True, timeout=35
        )
        assert result.returncode == 0, result.stderr[-1000:]
        assert '"provider":"phone-control"' in result.stdout.replace(" ", "")
        assert "未使用 ADB" in result.stdout
    calls = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [x["goal"] for x in calls] == [
        "读取当前手机屏幕",
        "查看当前手机页面",
        "执行录制的小红书完整闭环",
    ]
    assert all("terminal-auto" in x["argv"] for x in calls)
    assert len({x["trial"] for x in calls}) == 3


@pytest.mark.skipif(shutil.which("pi") is None, reason="PI CLI not installed")
@pytest.mark.parametrize("locked_read", [False, True])
def test_terminal_reports_locked_device_without_exposing_worker_text(tmp_path, locked_read):
    worker = tmp_path / "worker.py"
    worker.write_text(
        """import json,sys
for line in sys.stdin:
 r=json.loads(line)
 if r["command"]=="finish":
  print(json.dumps({"id":r["id"],"result":{"status":"failed"}}),flush=True);break
 if LOCKED_READ:
  value={"state":{"locked":True,"observation":{"kind":"locked_screen"}},"options":{},"done":True}
  if r["command"]=="observe":reply={"id":r["id"],"result":value}
  else:raise RuntimeError("Unexpected command")
 else:
  reply={"id":r["id"],"error":"SECRET-RAW-ERROR","errorDetails":{"code":"LOCKED","exceptionType":"TerminalRuntimeError"}}
 print(json.dumps(reply),flush=True)
""".replace("LOCKED_READ", repr(locked_read)).replace(
            '{"status":"failed"}',
            '{"status":"success","finalState":{"locked":True},"terminalTask":{"kind":"read"}}'
            if locked_read
            else '{"status":"failed"}',
        )
    )
    env = {
        **os.environ,
        "PHONE_BENCH_PYTHON": os.sys.executable,
        "PHONE_BENCH_WORKER": str(worker),
        "PI_OFFLINE": "1",
    }
    command = [
        "pi",
        "--no-extensions",
        "-e",
        str(Path(__file__).with_name("terminal-extension.ts")),
        "--no-mcp",
        "--no-skills",
        "--no-context-files",
        "--no-builtin-tools",
        "--tools",
        "phone_observe,phone_action",
        "--provider",
        "phone-control",
        "--model",
        "auto",
        "--offline",
        "--thinking",
        "off",
        "--mode",
        "json",
        "-p",
        "读取当前手机屏幕",
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=35)
    assert "锁屏" in result.stdout
    assert "SECRET-RAW-ERROR" not in result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("pi") is None, reason="PI CLI not installed")
def test_replay_command_rejects_arguments_before_sending_workflow(tmp_path):
    extension = tmp_path / "probe.ts"
    audit = tmp_path / "audit.json"
    source = json.dumps(str(Path(__file__).with_name("phone-extension.ts")))
    extension.write_text(f"""import {{writeFileSync}} from 'node:fs';
import {{registerPhoneBenchmark}} from {source};
export default async function(pi) {{
 const commands=new Map(); const sent=[];
 const fake={{on(){{}},registerTool(){{}},registerProvider(){{}},
  registerCommand(name,value){{commands.set(name,value)}},
  sendUserMessage(text){{sent.push(text)}}}};
 registerPhoneBenchmark(fake,{{terminal:true,terminalText:'test'}});
 const handler=commands.get('phone-replay').handler;
 const ctx={{ui:{{notify(){{}}}}}};
 await handler('不要点赞',ctx);
 const invalid=[...sent]; sent.length=0;
 await handler('',ctx);
 writeFileSync({json.dumps(str(audit))},JSON.stringify({{invalid,valid:sent}}));
 pi.registerCommand('probe',{{description:'offline command probe',handler:async()=>{{}}}});
}}
""")
    result = subprocess.run(
        [
            "pi",
            "--no-extensions",
            "-e",
            str(extension),
            "--no-mcp",
            "--no-skills",
            "--no-context-files",
            "--offline",
            "--mode",
            "json",
            "-p",
            "/probe",
        ],
        env={**os.environ, "PI_OFFLINE": "1"},
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert audit.exists(), result.stderr
    assert json.loads(audit.read_text()) == {"invalid": [], "valid": ["执行录制的小红书完整闭环"]}


@pytest.mark.skipif(shutil.which("pi") is None, reason="PI CLI not installed")
def test_terminal_reports_real_fallback_rate_limit_from_safe_audit(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text("""import json,sys,os
from pathlib import Path
for line in sys.stdin:
 r=json.loads(line)
 with Path(os.environ["FALLBACK_TEST_LOG"]).open("a") as f:f.write(r["command"]+"\\n")
 if r["command"]=="observe":
  reply={"id":r["id"],"result":{"state":{},"options":{"WAIT":"wait"},"done":False}}
 elif r["command"]=="decide":
  reply={"id":r["id"],"error":"SECRET-API-ERROR"}
 elif r["command"]=="finish":
  reply={"id":r["id"],"result":{"status":"failed","decisionPolicy":{"attempts":[{"backend":"deepseek","status":"provider_error","httpStatus":429}]}}}
 else:raise RuntimeError("Unexpected command")
 print(json.dumps(reply),flush=True)
 if r["command"]=="finish":break
""")
    env = {
        **os.environ,
        "PHONE_BENCH_PYTHON": os.sys.executable,
        "PHONE_BENCH_WORKER": str(worker),
        "PI_OFFLINE": "1",
        "FALLBACK_TEST_LOG": str(tmp_path / "commands.log"),
    }
    result = subprocess.run(
        [
            "pi",
            "--no-extensions",
            "-e",
            str(Path(__file__).with_name("terminal-extension.ts")),
            "--no-mcp",
            "--no-skills",
            "--no-context-files",
            "--no-builtin-tools",
            "--tools",
            "phone_observe,phone_action",
            "--provider",
            "phone-control",
            "--model",
            "auto",
            "--offline",
            "--thinking",
            "off",
            "--mode",
            "json",
            "-p",
            "打开小红书",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert "DeepSeek 兜底请求受到限流（HTTP 429）" in result.stdout
    assert (tmp_path / "commands.log").read_text().splitlines() == ["observe", "decide", "finish"]
    assert "SECRET-API-ERROR" not in result.stdout + result.stderr
