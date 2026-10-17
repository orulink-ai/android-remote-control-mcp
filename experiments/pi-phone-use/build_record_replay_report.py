#!/usr/bin/env python3
"""Render an offline Record & Replay report from explicitly named trial artifacts.

No device connection, model request, credential read, or implicit historical data.
Example: python build_record_replay_report.py --base RUN_DIR --report-dir REPORT_DIR
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import re
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from config import output_dir

TEMPLATE = Path(__file__).with_name("record-replay-report.html")
MARKER = '<script id="report-data" type="application/json">'
FORMAL_IDS = tuple(f"rr-r{i}-{mode}" for i in range(1, 4) for mode in ("jev", "skill"))
TERMINAL_STATUSES = {"success", "failed", "stopped", "aborted"}
PHASE_KINDS = ("model", "mcp", "wait", "other")
CONTEXT_FIELDS = ("deviceLabel", "androidVersion", "screenSize", "mcpVersion", "statusNote")


def number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 0 else None
    return None


def count(value: Any) -> int | None:
    value = number(value)
    return int(value) if value is not None and value.is_integer() else None


def mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def read(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def first_number(source: dict[str, Any], *names: str) -> float | None:
    return next((n for name in names if (n := number(source.get(name))) is not None), None)


def median_or_none(values: list[Any]) -> float | None:
    valid = [value for item in values if (value := number(item)) is not None]
    return median(valid) if valid else None


def metric(raw: dict[str, Any], name: str, alias: str) -> int | None:
    for section in (mapping(raw.get("metrics")), mapping(raw.get("recipeExecution"))):
        for key in (name, alias):
            value = count(section.get(key))
            if value is not None:
                return value
    return None


def actual_mode(raw: dict[str, Any]) -> str:
    value = mapping(raw.get("decisionPolicy")).get("mode", raw.get("provider"))
    return value if value in {"jev", "skill"} else "invalid"


def aggregate(base: Path, ident: str) -> dict[str, Any] | None:
    raw = read(base / ident / "result.json")
    if raw is None:
        return None
    calls = raw.get("modelCalls")
    calls = calls if isinstance(calls, list) else None
    operations = raw.get("operations")
    operations = operations if isinstance(operations, list) else []
    phases = [mapping(p) for p in raw.get("phases", []) if isinstance(p, dict)]
    phase_totals = {
        kind: sum(values)
        if (
            values := [
                n
                for p in phases
                if p.get("kind") == kind and (n := number(p.get("ms"))) is not None
            ]
        )
        else None
        for kind in PHASE_KINDS
    }
    # A recorded phase list can establish a real zero for an absent model stage,
    # when an explicit empty model call list confirms this was a model-free run.
    if calls == [] and phases and phase_totals["model"] is None:
        phase_totals["model"] = 0
    pi_ms = number(raw.get("piElapsedMs"))
    milestones = []
    for item in raw.get("milestones", []):
        item = mapping(item)
        milestones.append(
            {
                key: item[key]
                for key in ("completed", "next", "atMs", "phase")
                if key in item and isinstance(item[key], (str, int, float))
            }
        )
    model_phases = [p for p in phases if p.get("kind") == "model"]
    model_requests = len(model_phases) if phases else (len(calls) if calls is not None else None)
    state = mapping(raw.get("finalState"))
    return {
        "trialId": ident,
        "mode": actual_mode(raw),
        "status": raw.get("status") if raw.get("status") in TERMINAL_STATUSES else "pending",
        "piElapsedMs": pi_ms,
        "modelCalls": len(calls) if calls is not None else None,
        "modelRequests": model_requests,
        "recipeHits": metric(raw, "recipeHits", "hits"),
        "modelFallbacks": metric(raw, "modelFallbacks", "fallbacks"),
        "verifiedSteps": metric(raw, "verifiedSteps", "verifiedSteps"),
        "settleWaits": metric(raw, "settleWaits", "settleWaits"),
        "referenceRefreshes": metric(raw, "referenceRefreshes", "referenceRefreshes"),
        "actionAttempts": sum(
            op.get("name") == "selected_action" for op in operations if isinstance(op, dict)
        ),
        "readCalls": sum(
            op.get("name") == "android_get_screen_state"
            for op in operations
            if isinstance(op, dict)
        ),
        "phases": [{"kind": kind, "ms": phase_totals[kind]} for kind in PHASE_KINDS],
        "milestones": milestones,
        "error": raw.get("error") if isinstance(raw.get("error"), str) else None,
        "finalState": {
            key: state[key]
            for key in ("kind", "queryMatches", "firstTitleVisible", "liked", "collected")
            if key in state
        },
        "piAgentLoop": raw.get("piAgentLoop") is True,
    }


def summarize(trials: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    attempts = [t for t in trials if t["mode"] == mode and t["status"] in TERMINAL_STATUSES]
    successful = [t for t in attempts if t["status"] == "success"]
    return {
        "attempts": len(attempts),
        "successes": len(successful),
        "successRate": len(successful) / len(attempts) if attempts else None,
        "timedSuccesses": sum(t["piElapsedMs"] is not None for t in successful),
        "medianPiMs": median_or_none([t["piElapsedMs"] for t in successful]),
        "medianModelCalls": median_or_none([t["modelCalls"] for t in successful]),
        "medianModelRequests": median_or_none([t["modelRequests"] for t in successful]),
        "medianRecipeHits": median_or_none([t["recipeHits"] for t in successful]),
        "medianModelFallbacks": median_or_none([t["modelFallbacks"] for t in successful]),
        "medianVerifiedSteps": median_or_none([t["verifiedSteps"] for t in successful]),
        "medianSettleWaits": median_or_none([t["settleWaits"] for t in successful]),
        "medianReferenceRefreshes": median_or_none([t["referenceRefreshes"] for t in successful]),
    }


def generation(base: Path) -> dict[str, Any]:
    raw = read(base / "generation.json")
    if raw is None:
        return {
            "status": "pending",
            "generationMs": None,
            "demonstrationMs": None,
            "stepCount": None,
            "reviewState": None,
            "sourceDemoTrialId": None,
        }
    source = raw.get("sourceDemoTrialId")
    source = (
        source
        if isinstance(source, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", source)
        else None
    )
    demo = aggregate(base, source) if source else None
    return {
        "status": raw.get("status", "recorded"),
        "generationMs": first_number(raw, "generationMs", "durationMs", "elapsedMs"),
        "demonstrationMs": first_number(raw, "demonstrationMs", "demoMs")
        if first_number(raw, "demonstrationMs", "demoMs") is not None
        else (demo["piElapsedMs"] if demo else None),
        "stepCount": count(raw.get("stepCount")),
        "reviewState": raw.get("reviewState")
        if raw.get("reviewState")
        in {"draft", "reviewed", "verified", "verified-demonstration", "needs-verification"}
        else None,
        "sourceDemoTrialId": source,
    }


def comparison(summary: dict[str, Any], complete: bool, costs: dict[str, Any]) -> dict[str, Any]:
    result = {
        "savedMs": None,
        "speedup": None,
        "reductionPercent": None,
        "breakEvenRuns": None,
        "breakEvenIncludingDemoRuns": None,
        "reason": "formal_runs_incomplete",
    }
    if not complete:
        return result
    # Require at least two measured successes per mode; one lucky run is too weak.
    if any(summary[mode]["timedSuccesses"] < 2 for mode in ("jev", "skill")):
        result["reason"] = "insufficient_successful_samples"
        return result
    old, new = (summary[mode]["medianPiMs"] for mode in ("jev", "skill"))
    if old <= 0 or new <= 0:
        result["reason"] = "invalid_comparison_duration"
        return result
    saved = old - new
    result.update(
        savedMs=saved,
        speedup=old / new,
        reductionPercent=100 * saved / old,
        reason="successful_run_medians",
    )
    if saved > 0 and costs["generationMs"] is not None:
        result["breakEvenRuns"] = math.ceil(costs["generationMs"] / saved)
        if costs["demonstrationMs"] is not None:
            result["breakEvenIncludingDemoRuns"] = math.ceil(
                (costs["generationMs"] + costs["demonstrationMs"]) / saved
            )
    return result


def ui_evidence(base: Path, report_dir: Path) -> list[dict[str, str]]:
    raw = read(base / "ui-evidence.json") or {}
    items = []
    for entry in raw.get("screenshots", []):
        entry = mapping(entry)
        path = entry.get("path", "")
        if not isinstance(path, str):
            continue
        file = (report_dir / path).resolve()
        if not file.is_relative_to((report_dir / "ui-evidence").resolve()):
            continue
        if file.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or not file.is_file():
            continue
        items.append(
            {
                "path": file.relative_to(report_dir).as_posix(),
                "label": str(entry.get("label", "手机端录制界面")),
            }
        )
    return items


def public_context(base: Path) -> dict[str, str]:
    """Only explicit public test conditions; never publish connection configuration."""
    raw = read(base / "context.json") or {}
    return {
        key: raw[key].strip()[:800]
        for key in CONTEXT_FIELDS
        if isinstance(raw.get(key), str) and raw[key].strip()
    }


def selected_video(
    base: Path, report_dir: Path, mode: str, entry: Any, *, allow_history: bool = True
) -> dict[str, Any]:
    """Explicit selection never rewrites or silently replaces an earlier attempt."""
    if not isinstance(entry, dict) or set(entry) - {"trialId", "path", "earlierAttempts"}:
        raise ValueError("Invalid video selection entry")
    ident, path = entry.get("trialId"), entry.get("path")
    if (
        not isinstance(ident, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", ident)
        or not isinstance(path, str)
        or not re.fullmatch(r"phone-use-videos/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mp4", path)
    ):
        raise ValueError("Invalid video trial ID or relative MP4 path")
    target = (report_dir / path).resolve()
    if (
        not target.is_relative_to(report_dir.resolve())
        or target.parent != (report_dir / "phone-use-videos").resolve()
    ):
        raise ValueError("Video selection escaped the report directory")
    prior = entry.get("earlierAttempts", [])
    if (
        not isinstance(prior, list)
        or len(prior) > 20
        or (not allow_history and "earlierAttempts" in entry)
    ):
        raise ValueError("Invalid earlier video attempts")
    item = aggregate(base, ident)
    if item is not None and item["mode"] != mode:
        raise ValueError("Selected video trial mode mismatch")
    return {
        "mode": mode,
        "trialId": ident,
        "path": path,
        "available": target.is_file(),
        "trial": item,
        "earlierAttempts": [
            selected_video(base, report_dir, mode, old, allow_history=False) for old in prior
        ],
    }


def videos(base: Path, report_dir: Path) -> list[dict[str, Any]]:
    selection = read(base / "video-selection.json") or {}
    if set(selection) - {"jev", "skill"}:
        raise ValueError("Invalid video selection mode")
    return [
        selected_video(
            base,
            report_dir,
            mode,
            selection[mode]
            if mode in selection
            else {
                "trialId": f"video-{mode}",
                "path": f"phone-use-videos/{mode}-full-flow.mp4",
            },
        )
        for mode in ("jev", "skill")
    ]


def build(base: Path | None = None, report_dir: Path | None = None) -> dict[str, Any]:
    base = (base or output_dir()).resolve()
    report_dir = (report_dir or base / "report").resolve()
    trials, pending, invalid = [], [], []
    for ident in FORMAL_IDS:
        item = aggregate(base, ident)
        if item is None:
            pending.append(ident)
            continue
        expected = "skill" if ident.endswith("skill") else "jev"
        if item["mode"] != expected:
            invalid.append({"trialId": ident, "expectedMode": expected, "actualMode": item["mode"]})
            pending.append(ident)
            continue
        trials.append(item)
        if item["status"] not in TERMINAL_STATUSES:
            pending.append(ident)
    summaries = {mode: summarize(trials, mode) for mode in ("jev", "skill")}
    costs = generation(base)
    return {
        "schemaVersion": 1,
        "experimentDate": "2026-10-18",
        "generatedAt": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "status": "complete" if not pending else "pending",
        "pendingFormalIds": pending,
        "invalidModes": invalid,
        "trials": trials,
        "summary": summaries,
        "comparison": comparison(summaries, not pending, costs),
        "generation": costs,
        "context": public_context(base),
        "demonstration": aggregate(base, costs["sourceDemoTrialId"])
        if costs["sourceDemoTrialId"]
        else None,
        "videos": videos(base, report_dir),
        "uiEvidence": ui_evidence(base, report_dir),
    }


def render(base: Path | None = None, report_dir: Path | None = None) -> Path:
    base = (base or output_dir()).resolve()
    report_dir = (report_dir or base / "report").resolve()
    data = build(base, report_dir)
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    embedded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = TEMPLATE.read_text()
    if template.count(MARKER) != 1:
        raise ValueError("Expected one report-data marker")
    prefix, remainder = template.split(MARKER, 1)
    _, suffix = remainder.split("</script>", 1)
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "record-replay-data.json").write_text(encoded + "\n")
    destination = report_dir / "record-replay.html"
    destination.write_text(prefix + MARKER + embedded + "</script>" + suffix)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=None)
    parser.add_argument("--report-dir", type=Path, default=None)
    args = parser.parse_args()
    print(json.dumps({"report": str(render(args.base, args.report_dir))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
