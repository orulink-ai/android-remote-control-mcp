#!/usr/bin/env python3
"""Build a credential-free report snapshot from explicitly named experiment artifacts.

No device actions, API requests, or runtime mutations. Incomplete runs stay pending.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import hashlib
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from config import output_dir

FORMAL_IDS = (
    "final-r1-jev",
    "final-r1-deepseek",
    "final-r2-deepseek",
    "final-r2-jev",
    "final-r3-jev",
    "final-r3-deepseek",
)
PILOT_IDS = ("pilot-optimized-jev",)
PROBE_IDS = ("replay-cold-r1", "replay-warm-r1")
VIDEO_IDS = ("video-jev", "video-deepseek")
NETWORK_FIELDS = ("connectMs", "tlsMs", "requestToHeadersMs", "downloadMs", "totalMs")
RESULT_FIELDS = (
    "trialId",
    "startedAt",
    "completedAt",
    "status",
    "operations",
    "modelCalls",
    "milestones",
    "finalState",
    "serverInfo",
    "error",
    "provider",
    "piElapsedMs",
    "piAgentLoop",
    "clientMode",
    "decisionStyle",
    "decisionPolicy",
    "errorDetails",
)
MODEL_CALL_FIELDS = (
    "choice",
    "latencyMs",
    "servedModel",
    "usage",
    "cost",
    "network",
    "mode",
    "compact",
    "decisionStyle",
    "stage",
    "observation",
    "payloadHash",
    "payloadSha256",
    "candidateCount",
    "stateCharacters",
    "inputCharacters",
    "snapshotId",
    "requestPayloadSha256",
    "failed",
    "selectedBackend",
    "fallbackUsed",
    "attemptId",
)
OP_FIELDS = (
    "index",
    "name",
    "arguments",
    "ms",
    "status",
    "choice",
    "label",
    "stage",
    "recovery",
    "oldId",
    "newId",
    "backend",
    "exceptionType",
    "attemptId",
)
SENSITIVE_KEYS = {
    "apikey",
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "config",
    "token",
    "credentials",
    "secret",
    "rawanswer",
    "headers",
    "rawscreen",
    "raw_screen",
    "screenshot",
    "screenshot_base64",
    "base64",
    "prompt",
    "systemprompt",
    "system_prompt",
    "state",
    "payload",
    "screen",
}


def finite(value: Any) -> bool:
    import math

    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def clean(value: Any) -> Any:
    """Do not copy prompts, screen dumps, credentials, or image bytes into reports."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items() if str(k).lower() not in SENSITIVE_KEYS}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str) and value.startswith("data:image/"):
        return "[image omitted]"
    return value


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object: {path.name}")
    return value


def safe_artifact(base: Path, value: str | None, suffix: str) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.is_relative_to(base.resolve()) or not path.name.endswith(suffix):
        return None
    return path


def pi_evidence(base: Path, ident: str, launch: dict[str, Any]) -> dict[str, Any]:
    events = safe_artifact(base, launch.get("piEvents"), "-pi-events.jsonl")
    session = safe_artifact(base, launch.get("piSession"), "-pi-session.jsonl")
    # Launch metadata may name another user's log. Only read this trial's own names.
    events = events if events == (base / f"{ident}-pi-events.jsonl").resolve() else None
    session = session if session == (base / f"{ident}-pi-session.jsonl").resolve() else None
    starts = ends = None
    tools: list[dict[str, Any]] = []
    if events and events.is_file():
        starts = ends = 0
        for line in events.read_text().splitlines():
            event = json.loads(line)
            starts += event.get("type") == "agent_start"
            ends += event.get("type") == "agent_end"
    if session and session.is_file():
        for line in session.read_text().splitlines():
            entry = json.loads(line)
            message = entry.get("message", {})
            if entry.get("type") != "message" or message.get("role") != "assistant":
                continue
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if part.get("type") == "toolCall" and part.get("name") in {
                    "phone_observe",
                    "phone_action",
                }:
                    args = part.get("arguments", {})
                    tools.append(
                        {
                            "name": part["name"],
                            "arguments": {"choice": args["choice"]} if "choice" in args else {},
                        }
                    )
    return {
        "agentStarts": starts,
        "agentEnds": ends,
        "toolCalls": tools,
        "sessionFile": session.name if session else None,
        "eventsFile": events.name if events else None,
    }


def public_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    return {
        key: state[key]
        for key in ("kind", "queryMatches", "firstTitleVisible", "liked", "collected")
        if key in state
    }


def public_model_call(call: dict[str, Any]) -> dict[str, Any]:
    result = {k: clean(call[k]) for k in MODEL_CALL_FIELDS if k in call}
    if "network" in result:
        network = result["network"] or {}
        result["network"] = {
            key: network.get(key) for key in (*NETWORK_FIELDS, "httpVersion", "newConnection")
        }
    if "usage" in result:
        result["usage"] = {
            key: value
            for key, value in result["usage"].items()
            if key
            in {
                "input_tokens",
                "output_tokens",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cost",
            }
        }
    return result


def public_events(base: Path) -> list[dict[str, Any]]:
    record = read_json(base / "experiment-events.json") or {}
    return [
        clean(
            {
                key: event[key]
                for key in ("kind", "description", "trialId", "includedInModelStatistics")
                if key in event
            }
        )
        for event in record.get("events", [])
        if isinstance(event, dict)
    ]


def aggregate_trial(base: Path, ident: str, sequence: int | None = None) -> dict[str, Any] | None:
    raw = read_json(base / ident / "result.json")
    if raw is None:
        return None
    launch = read_json(base / f"{ident}-launch.json") or {}
    result = {k: clean(raw[k]) for k in RESULT_FIELDS if k in raw}
    result["finalState"] = public_state(raw.get("finalState", {}))
    result["trialId"] = ident
    result["modelKey"] = raw.get("provider", "deepseek" if "deepseek" in ident else "jev")
    result["sequence"] = sequence
    result["pair"] = (sequence + 1) // 2 if sequence else None
    result["cliWallMs"] = launch.get("cliWallMs")
    result["excludedResetMs"] = launch.get("excludedResetMs")
    result["exitCode"] = launch.get("exitCode")
    result["workerElapsedMs"] = raw.get("elapsedMs")
    result["modelCalls"] = [public_model_call(call) for call in raw.get("modelCalls", [])]
    operations = [
        {k: clean(op[k]) for k in OP_FIELDS if k in op} for op in raw.get("operations", [])
    ]
    result["operations"] = operations
    raw_phases = [
        {k: phase.get(k) for k in ("kind", "name", "ms")} for phase in raw.get("phases", [])
    ]
    result["workerPhases"] = raw_phases
    totals = {}
    for kind in ("model", "mcp", "wait"):
        samples = [p["ms"] for p in raw_phases if p.get("kind") == kind and finite(p.get("ms"))]
        totals[kind] = sum(samples) if samples else None
    pi_ms = raw.get("piElapsedMs")
    other = (
        pi_ms - sum(totals.values())
        if finite(pi_ms) and all(finite(v) for v in totals.values())
        else None
    )
    result["phases"] = [
        {"kind": "model", "name": "模型请求（含失败）", "ms": totals["model"]},
        {"kind": "mcp", "name": "MCP初始化、截图、读屏与操作", "ms": totals["mcp"]},
        {"kind": "wait", "name": "共同界面等待", "ms": totals["wait"]},
        {"kind": "other", "name": "PI循环、共同验证器及本地开销", "ms": other},
    ]
    selected = [op for op in operations if op.get("name") == "selected_action"]
    repetitions = Counter((op.get("stage"), op.get("label") or op.get("choice")) for op in selected)
    model_requests = sum(p.get("kind") == "model" for p in raw_phases)
    valid = len(result["modelCalls"])
    result["counts"] = {
        "modelRequests": model_requests,
        "validDecisions": valid,
        "failedModelRequests": max(0, model_requests - valid),
        "actionAttempts": len(selected),
        "readCalls": sum(op.get("name") == "android_get_screen_state" for op in operations),
        "mcpErrors": sum(
            op.get("name", "").startswith("android_") and op.get("status") == "error"
            for op in operations
        ),
    }
    result["retries"] = {
        "staleReferenceRecoveries": sum(op.get("name") == "stale_reference" for op in operations),
        "directReferenceRebindings": sum(
            op.get("name") == "direct_reference_rebind" for op in operations
        ),
        "modelDirectedRepeatedActions": sum(n - 1 for n in repetitions.values()),
        "waitChoices": sum(op.get("choice") == "WAIT" for op in selected),
        "automaticModelRetries": raw.get("retries", {}).get("automaticModelRetries"),
        "automaticActionRetries": raw.get("retries", {}).get("automaticActionRetries"),
    }
    result["piEvidence"] = pi_evidence(base, ident, launch)
    result["counts"]["piToolCalls"] = len(result["piEvidence"]["toolCalls"])
    costs = [call.get("cost") for call in result["modelCalls"] if finite(call.get("cost"))]
    result["knownCostUsd"] = sum(costs) if costs else None
    result["integrityWarnings"] = []
    if not finite(pi_ms):
        result["integrityWarnings"].append("PI主计时缺失；不以worker时间代替")
    if finite(other) and other < -0.001:
        result["integrityWarnings"].append("分段已记录时间超过PI跨度，不能作为可加分段解释")
    if model_requests < valid:
        result["integrityWarnings"].append("模型请求计时条数少于有效响应数")
    if not launch:
        result["integrityWarnings"].append("launch记录缺失，CLI/重置时间未知")
    return result


def nullable_stats(values: list[Any]) -> dict[str, Any]:
    observed = [float(v) for v in values if finite(v)]
    return {
        "observedN": len(observed),
        "missingN": len(values) - len(observed),
        "medianMs": median(observed) if observed else None,
        "minMs": min(observed) if observed else None,
        "maxMs": max(observed) if observed else None,
    }


def network_groups(trials: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for trial in trials:
        for index, call in enumerate(trial.get("modelCalls", [])):
            net = call.get("network") or {}
            new = net.get("newConnection")
            group = (
                "newConnection"
                if new is True
                else "reusedConnection"
                if new is False
                else "unknownConnection"
            )
            sample = {
                "trialId": trial["trialId"],
                "call": index + 1,
                "stage": call.get("stage"),
                "choice": call.get("choice"),
                "latencyMs": call.get("latencyMs"),
                **{k: net.get(k) for k in NETWORK_FIELDS},
                "httpVersion": net.get("httpVersion"),
                "newConnection": new,
            }
            grouped.setdefault((trial["modelKey"], group), []).append(sample)
    return {
        "groups": [
            {
                "modelKey": key,
                "connection": connection,
                "n": len(samples),
                "httpVersions": dict(Counter(s["httpVersion"] or "unknown" for s in samples)),
                "stages": {
                    field: nullable_stats([s.get(field) for s in samples])
                    for field in NETWORK_FIELDS
                },
                "samples": samples,
            }
            for (key, connection), samples in sorted(grouped.items())
        ],
        "notes": [
            "connect含DNS，不能单独报告DNS耗时",
            "requestToHeaders包括网络/网关/排队/服务端处理，非纯GPU推理",
            "阶段缺失保留null；各阶段中位数不一定来自同一次请求，不能相加当总中位数",
            "warm为会话持久client策略，不保证首请求已预建连接",
        ],
    }


def task_summary(trials: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for key in ("jev", "deepseek"):
        attempts = [t for t in trials if t["modelKey"] == key]
        done = [
            t["piElapsedMs"]
            for t in attempts
            if t.get("status") == "success" and finite(t.get("piElapsedMs"))
        ]
        successes = sum(t.get("status") == "success" for t in attempts)
        out[key] = {
            "attempts": len(attempts),
            "successes": successes,
            "successRate": successes / len(attempts) if attempts else None,
            "successfulPi": nullable_stats(done),
        }
    return out


def probe_record(base: Path, ident: str) -> dict[str, Any]:
    raw = read_json(base / ident / "result.json")
    launch = read_json(base / f"{ident}-launch.json")
    if raw is None:
        return {"trialId": ident, "status": "pending", "taskSuccessMetric": False}
    # Read-only probes can have a different schema. Keep safe metadata, never raw payloads.
    safe_fields = (
        "trialId",
        "status",
        "startedAt",
        "completedAt",
        "elapsedMs",
        "piElapsedMs",
        "provider",
        "mode",
        "clientMode",
        "snapshotCount",
        "payloadHashes",
        "error",
        "modelCalls",
        "piAgentLoop",
        "phases",
        "operations",
        "milestones",
        "finalState",
        "replayOnly",
        "phoneTaskSuccess",
        "firstWarmSampleIncluded",
        "terminalAssistantTextIsSynthetic",
        "decisionStyle",
        "compact",
    )
    record = {k: clean(raw[k]) for k in safe_fields if k in raw}
    record["modelCalls"] = [public_model_call(call) for call in raw.get("modelCalls", [])]
    record["finalState"] = public_state(raw.get("finalState", {}))
    record.update(
        {
            "trialId": ident,
            "taskSuccessMetric": False,
            "cliWallMs": (launch or {}).get("cliWallMs"),
            "piEvidence": pi_evidence(base, ident, launch or {}),
        }
    )
    return record


def build(
    base: Path | None = None, historical: Path | None = None, report_dir: Path | None = None
) -> dict[str, Any]:
    base = (base or output_dir()).resolve()
    report_dir = (report_dir or base / "report").resolve()
    trials = []
    invalid_modes = []
    for seq, ident in enumerate(FORMAL_IDS, 1):
        trial = aggregate_trial(base, ident, seq)
        if trial is None:
            continue
        expected = ident.rsplit("-", 1)[1]
        # Either explicit declaration can disqualify a mislabeled baseline.
        # Older independent results may lack both fields and retain ID inference.
        modes = [trial.get("provider", expected)]
        policy = trial.get("decisionPolicy")
        if isinstance(policy, dict) and "mode" in policy:
            modes.append(policy["mode"])
        mismatched = [mode for mode in modes if mode != expected]
        if mismatched:
            actual = mismatched[0]
            invalid_modes.append(
                {
                    "ident": ident,
                    "expectedMode": expected,
                    "actualMode": actual
                    if actual in ("jev", "deepseek", "jev-auto")
                    else "invalid",
                }
            )
            continue
        trials.append(trial)
    accepted_ids = {trial["trialId"] for trial in trials}
    missing = [
        ident
        for ident in FORMAL_IDS
        if ident not in accepted_ids or not (base / f"{ident}-launch.json").is_file()
    ]
    pilots = [t for ident in PILOT_IDS if (t := aggregate_trial(base, ident)) is not None]
    for pilot in pilots:
        pilot["exclusionReason"] = (
            "初始优化联调，首帖已点赞，不满足正式未赞未藏基线；不纳入正式排名。"
        )
    videos = []
    for ident in VIDEO_IDS:
        trial = aggregate_trial(base, ident)
        key = "deepseek" if "deepseek" in ident else "jev"
        relative = f"phone-use-videos/{key}-full-flow.mp4"
        raw_recording = read_json(report_dir / "phone-use-videos" / f"{key}-recording.json") or {}
        recording_metadata = {
            k: raw_recording.get(k)
            for k in (
                "durationSeconds",
                "fullFlowCovered",
                "playbackRate",
                "edited",
                "trimmed",
                "width",
                "height",
                "codec",
                "trialStatus",
                "status",
            )
        }
        videos.append(
            {
                "recordingMetadata": recording_metadata,
                "modelKey": key,
                "trialId": ident,
                "src": relative,
                "fileReady": (report_dir / relative).is_file(),
                "status": trial.get("status") if trial else "pending",
                "piElapsedMs": trial.get("piElapsedMs") if trial else None,
                "recordingTrial": trial,
                "includedInFormalStatistics": False,
                "speed": "1x",
            }
        )
    old = read_json(historical) if historical is not None else None
    history = (
        {
            "label": "旧版v2，条件不同，仅作背景",
            "comparableForCausality": False,
            "source": historical.name,
            "summary": old.get("summary"),
            "environment": old.get("environment"),
            "date": old.get("date"),
        }
        if old
        else {"status": "unavailable", "comparableForCausality": False}
    )
    hashes = read_json(base / "frozen-runtime-hashes.json")
    restoration_raw = read_json(base / "restoration.json") or {}
    restoration = {
        key: restoration_raw[key]
        for key in ("verified", "status", "excludedFromBenchmark", "evidenceTrial")
        if key in restoration_raw
    }
    if not restoration:
        restoration = {"verified": False, "status": "pending"}
    for key in ("before", "original", "after"):
        if key in restoration_raw:
            restoration[key] = public_state(restoration_raw[key])
    diagnostics = [probe_record(base, ident) for ident in PROBE_IDS]
    snapshot_file = base / "six-phone-snapshots.json"
    snapshot_metadata = []
    if snapshot_file.is_file():
        snapshots = json.loads(snapshot_file.read_text())
        if isinstance(snapshots, list):
            for item in snapshots:
                state = json.dumps(
                    item.get("state", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                options = json.dumps(
                    item.get("options", {}),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                snapshot_metadata.append(
                    {
                        "id": item.get("id"),
                        "candidateCount": len(item.get("options", {})),
                        "stateCharacters": len(state),
                        "stateSha256": hashlib.sha256(state.encode()).hexdigest(),
                        "optionsSha256": hashlib.sha256(options.encode()).hexdigest(),
                    }
                )
    readiness = {
        "formalComplete": not missing,
        "diagnosticsComplete": all(p.get("status") != "pending" for p in diagnostics),
        "recordingsComplete": all(
            v["fileReady"]
            and v["recordingTrial"]
            and v["recordingMetadata"].get("fullFlowCovered") is True
            for v in videos
        ),
        "restorationVerified": restoration.get("verified") is True,
    }
    return {
        "title": "2026-10-17 PI Phone Use 实验归档与运行记录",
        "experimentDate": "2026-10-17",
        "date": max((t.get("completedAt") or t.get("startedAt") or "" for t in trials), default="")
        or None,
        "generatedAt": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "status": "complete" if all(readiness.values()) else "pending",
        "readiness": readiness,
        "pendingFormalIds": missing,
        "invalidFormalModes": invalid_modes,
        "models": {
            "jev": {
                "label": "Jev 1.14",
                "requestedModel": "typesafe/jev-1.14",
                "protocol": "OpenRouter Decisions API, operation/target heads",
            },
            "deepseek": {
                "label": "DeepSeek V4.1 Flash",
                "requestedModel": "deepseek/deepseek-v4.1-flash",
                "protocol": "OpenRouter chat completions, reasoning disabled, structured choice",
            },
        },
        "environment": {
            "piVersion": "1.3.0",
            "piIntegration": "PI自定义phone-benchmark provider，将真实模型结构化选择转换为PI工具调用；共享阶段目标、动作候选与颜色验证器。非原生PI模型无限制自主操作基准。",
            "diagnosticControls": "同模型、同payload；cold使用HTTP/1策略，warm使用HTTP/2持久client策略，同时改变连接策略与协议，非仅隔离连接复用。",
            "device": "Samsung SM-S9210 · Android 16 · 1080×2340",
            "mcpVersionsObserved": sorted(
                {
                    str(t.get("serverInfo", {}).get("version"))
                    for t in trials
                    if t.get("serverInfo", {}).get("version")
                }
            ),
            "clientModesObserved": sorted(
                {str(t.get("clientMode")) for t in trials if t.get("clientMode")}
            ),
            "decisionStylesObserved": sorted(
                {str(t.get("decisionStyle")) for t in trials if t.get("decisionStyle")}
            ),
            "connection": "同一USB转发HTTP Android MCP；正式任务由PI逐步phone tools完成",
        },
        "trials": trials,
        "summary": task_summary(trials),
        "network": network_groups(trials),
        "diagnostics": diagnostics,
        "diagnosticSnapshots": snapshot_metadata,
        "pilots": pilots,
        "videos": videos,
        "historical": clean(history),
        "frozenHashes": hashes,
        "restoration": clean(restoration),
        # Incidents are evidence, not defaults inherited by future runs.
        "experimentEvents": public_events(base),
        "methodology": [
            "真实PI AgentLoop使用自定义phone-benchmark provider，把真实模型返回的结构化选择转换成phone_observe/phone_action工具调用，经共同worker与Android MCP完成六阶段任务。两模型共享阶段目标、动作候选和颜色验证器；这不是原生PI模型无限制自主操作基准。",
            "每模型正式3轮，顺序Jev,DeepSeek / DeepSeek,Jev / Jev,DeepSeek。各会话使用相同warm持久client策略；首请求是否新建连接由实际trace决定。",
            "优化包括真实手机树候选去重、状态精简与同一次Jev请求operation/target heads；两模型保持共同任务、执行和状态核验。",
            "PI agent_start到agent_end为主计时，含MCP初始化、截图颜色验证、失败恢复与等待。CLI与worker时间嵌套，不可叠加。",
            "selected_action才计所选动作，失效日志不重复计数；重复选择按stage+label统计。失败请求计入模型请求时延和次数。",
            "cold/warm诊断使用同一模型和真实phone use屏幕的同一payload，通过PI只读回放。cold使用HTTP/1策略、warm使用HTTP/2持久client策略，同时改变连接策略与协议，只能解释为两者的联合差异，不能称只隔离连接复用。不向手机派发动作，不算完整任务成功样本；回放按固定记录前进，不依据决策正确性判任务成功。首个warm请求保留；视频另录各1轮，不纳入正式统计。",
        ],
        "limitations": [
            "connect包含DNS与TCP，本trace不能单拆DNS。",
            "headers等待混合网络、网关、排队与服务端处理，不能称纯GPU推理。",
            "缺失网络阶段保留null，不用0补齐；分阶段中位数不能相加当总中位数。",
            "HTTP/2与连接复用按实际响应和newConnection trace报告，而不是只看client配置。warm首请求仍可能包含新连接与TLS。",
            "具体失败以本轮记录为准；历史 API 连接失败与轮间会话耗尽见研究归档，不预填为本轮事件。",
            "cold为HTTP/1连接策略、warm为HTTP/2持久client策略，协议与连接策略同时变化；即使模型与payload相同，也不能把差异只归因于连接复用。",
            "多项优化一起改变，历史条件不同，不支持单项归因；每模型3轮不能推出稳定普遍加速。",
            "本次手机服务为设备已安装版本，非当前源码HEAD重新构建；具体版本见观察值。",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=None)
    parser.add_argument("--historical", type=Path, default=None)
    parser.add_argument("--report-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    data = build(args.base, args.historical, args.report_dir)
    output = args.output or (args.base or output_dir()) / "public-data.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "status": data["status"],
                "formalRuns": len(data["trials"]),
                "pendingFormalIds": data["pendingFormalIds"],
                "output": str(output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
