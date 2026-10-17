"""Offline report tests: synthetic artifacts only, no model or phone access."""

import json
from pathlib import Path

import build_public_data as report
import render_report


def write_trial(base, ident="final-r1-jev", **fields):
    folder = base / ident
    folder.mkdir(parents=True, exist_ok=True)
    raw = {
        "status": "success",
        "provider": "jev",
        "piElapsedMs": 1000,
        "modelCalls": [],
        "operations": [],
        **fields,
    }
    (folder / "result.json").write_text(json.dumps(raw))
    (base / f"{ident}-launch.json").write_text("{}")
    return raw


def test_pending_has_no_fabricated_measurements_and_exact_six_ids(tmp_path):
    data = report.build(tmp_path)
    assert data["status"] == "pending"
    assert data["trials"] == []
    assert data["pendingFormalIds"] == list(report.FORMAL_IDS)
    assert len(report.FORMAL_IDS) == 6
    assert data["summary"]["jev"]["successfulPi"]["medianMs"] is None
    assert data["summary"]["jev"]["successRate"] is None
    assert data["historical"]["status"] == "unavailable"
    assert data["experimentEvents"] == []
    assert data["experimentDate"] == "2026-10-17"
    assert data["date"] is None
    assert data["generatedAt"]


def test_unlisted_trials_are_never_promoted_to_formal_results(tmp_path):
    write_trial(tmp_path)
    write_trial(tmp_path, "some-extra-jev")
    write_trial(tmp_path, "video-jev")
    data = report.build(tmp_path)
    assert [t["trialId"] for t in data["trials"]] == ["final-r1-jev"]
    assert len(data["pendingFormalIds"]) == 5


def test_selected_action_counts_exclude_stale_records_and_unknown_phases_stay_null(tmp_path):
    write_trial(
        tmp_path,
        operations=[
            {"name": "selected_action", "choice": "CLICK:a", "stage": "search", "label": "Search"},
            {"name": "stale_reference", "choice": "CLICK:a", "stage": "search", "label": "Search"},
            {
                "name": "direct_reference_rebind",
                "choice": "CLICK:a",
                "stage": "search",
                "label": "Search",
            },
        ],
    )
    trial = report.build(tmp_path)["trials"][0]
    assert trial["counts"]["actionAttempts"] == 1
    assert trial["retries"]["modelDirectedRepeatedActions"] == 0
    assert trial["retries"]["staleReferenceRecoveries"] == 1
    assert trial["retries"]["directReferenceRebindings"] == 1
    assert all(p["ms"] is None for p in trial["phases"])


def test_missing_network_measurements_are_null_not_zero(tmp_path):
    write_trial(
        tmp_path,
        modelCalls=[
            {
                "servedModel": "fake-model",
                "latencyMs": 300,
                "network": {"httpVersion": "HTTP/2", "newConnection": False},
            }
        ],
    )
    group = report.build(tmp_path)["network"]["groups"][0]
    assert group["samples"][0]["connectMs"] is None
    assert group["samples"][0]["tlsMs"] is None
    assert group["stages"]["connectMs"]["medianMs"] is None
    assert group["stages"]["connectMs"]["missingN"] == 1


def test_unrelated_pi_paths_are_not_read_even_when_inside_base(tmp_path):
    write_trial(tmp_path)
    # Invalid JSON would raise if the report attempted to read this unrelated log.
    other = tmp_path / "unrelated-pi-session.jsonl"
    other.write_text("DO NOT READ THIS")
    events = tmp_path / "unrelated-pi-events.jsonl"
    events.write_text("DO NOT READ THIS")
    (tmp_path / "final-r1-jev-launch.json").write_text(
        json.dumps({"piSession": str(other), "piEvents": str(events)})
    )
    ev = report.build(tmp_path)["trials"][0]["piEvidence"]
    assert ev["toolCalls"] == []
    assert ev["agentStarts"] is None
    assert ev["sessionFile"] is None


def test_render_escapes_script_breakout_and_preserves_decoded_text(tmp_path):
    attack = '</script><script>alert("unsafe")</script>'
    write_trial(tmp_path, error=attack)
    out = render_report.render(tmp_path, tmp_path / "report")
    html = out.read_text()
    marker = '<script id="report-data" type="application/json">'
    encoded = html.split(marker, 1)[1].split("</script>", 1)[0]
    assert "<" not in encoded
    assert json.loads(encoded)["trials"][0]["error"] == attack
    assert attack not in html
    assert (out.parent / "public-data.json").is_file()


def test_no_credentials_raw_screen_config_or_personal_paths_in_public_output(tmp_path):
    write_trial(
        tmp_path,
        modelCalls=[
            {
                "servedModel": "fake-model",
                "latencyMs": 4,
                "rawAnswer": {"token": "NEVER-PUBLISH"},
                "usage": {
                    "input_tokens": 3,
                    "headers": {"Authorization": "SECRET-HEADER"},
                    "config": {"token": "SECRET-CONFIG"},
                },
                "network": {"totalMs": 4, "rawscreen": "SECRET-SCREEN"},
            }
        ],
        operations=[
            {
                "name": "selected_action",
                "arguments": {"choice": "WAIT", "headers": "SECRET-HEADER"},
                "rawscreen": "SECRET-SCREEN",
            }
        ],
        rawscreen="SECRET-SCREEN",
        config={"token": "SECRET-CONFIG"},
    )
    data = report.build(tmp_path)
    encoded = json.dumps(data)
    for forbidden in (
        "NEVER-PUBLISH",
        "SECRET-HEADER",
        "SECRET-CONFIG",
        "SECRET-SCREEN",
        "Authorization",
        "rawAnswer",
        "rawscreen",
        str(tmp_path),
    ):
        assert forbidden not in encoded


def test_template_is_pending_and_has_no_embedded_results_or_personal_paths():
    template = Path(render_report.TEMPLATE).read_text()
    assert "/Users/" not in template
    assert "sk-or-" not in template
    assert "当我闯进" not in template
    assert "19511.211" not in template
    embedded = template.split(render_report.MARKER, 1)[1].split("</script>", 1)[0]
    assert json.loads(embedded)["status"] == "pending"
    assert json.loads(embedded)["trials"] == []


def test_default_output_configuration_and_missing_history_are_offline(tmp_path, monkeypatch):
    import config

    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        config,
        "load_openrouter_key",
        lambda: (_ for _ in ()).throw(AssertionError("must not read keys")),
    )
    monkeypatch.setattr(
        config,
        "mcp_settings",
        lambda: (_ for _ in ()).throw(AssertionError("must not read MCP auth")),
    )
    data = report.build(historical=tmp_path / "does-not-exist.json")
    assert data["historical"]["status"] == "unavailable"
    path = render_report.render()
    assert path == tmp_path / "report" / "phone-use-optimized.html"
    assert json.loads((path.parent / "public-data.json").read_text())["trials"] == []


def test_first_observation_failure_with_no_final_state_remains_reportable(tmp_path):
    write_trial(
        tmp_path,
        status="failed",
        piElapsedMs=700,
        finalState=None,
        error="Initial phone observation failed",
        operations=[{"name": "android_get_screen_state", "status": "error", "ms": 650}],
    )
    write_trial(
        tmp_path,
        "replay-cold-r1",
        status="failed",
        piElapsedMs=200,
        finalState=None,
        error="Initial replay observation failed",
    )
    data = report.build(tmp_path)
    trial = data["trials"][0]
    assert trial["status"] == "failed"
    assert trial["piElapsedMs"] == 700
    assert trial["finalState"] == {}
    assert trial["counts"]["mcpErrors"] == 1
    assert data["summary"]["jev"]["attempts"] == 1
    assert data["summary"]["jev"]["successes"] == 0
    assert data["summary"]["jev"]["successfulPi"]["medianMs"] is None
    probe = data["diagnostics"][0]
    assert probe["status"] == "failed"
    assert probe["piElapsedMs"] == 200
    assert probe["finalState"] == {}
    assert render_report.render(tmp_path).is_file()


def test_public_state_ignores_non_object_observations():
    for missing_state in (None, [], "unavailable", False, 0):
        assert report.public_state(missing_state) == {}
