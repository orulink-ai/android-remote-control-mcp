"""Report acceptance tests use synthetic local artifacts, never models or devices."""

import json

import build_record_replay_report as report


def trial(base, ident, *, mode=None, **extra):
    mode = mode or ("skill" if ident.endswith("skill") else "jev")
    folder = base / ident
    folder.mkdir(parents=True, exist_ok=True)
    raw = {
        "status": "success",
        "provider": mode,
        "piElapsedMs": 1000,
        "modelCalls": [],
        "operations": [],
        "milestones": [],
        **extra,
    }
    (folder / "result.json").write_text(json.dumps(raw))


def test_missing_runs_are_pending_with_unknown_metrics(tmp_path):
    data = report.build(tmp_path)
    assert data["status"] == "pending"
    assert len(data["pendingFormalIds"]) == 6
    assert data["summary"]["jev"]["successRate"] is None
    assert data["summary"]["skill"]["medianPiMs"] is None
    assert data["comparison"]["savedMs"] is None
    assert data["comparison"]["breakEvenRuns"] is None
    assert data["generation"]["generationMs"] is None
    assert all(not item["available"] for item in data["videos"])


def test_success_only_median_and_failure_denominator(tmp_path):
    trial(tmp_path, "rr-r1-jev", piElapsedMs=1000, modelCalls=[{}, {}])
    trial(tmp_path, "rr-r2-jev", piElapsedMs=3000, modelCalls=[{}])
    trial(tmp_path, "rr-r3-jev", piElapsedMs=100, status="failed")
    data = report.build(tmp_path)
    summary = data["summary"]["jev"]
    assert summary["attempts"] == 3 and summary["successes"] == 2
    assert summary["successRate"] == 2 / 3
    assert summary["medianPiMs"] == 2000
    assert summary["medianModelCalls"] == 1.5
    assert summary["timedSuccesses"] == 2
    assert data["trials"][2]["status"] == "failed"


def test_no_speed_claim_until_all_six_formal_runs_exist(tmp_path):
    trial(tmp_path, "rr-r1-jev", piElapsedMs=2000)
    trial(tmp_path, "rr-r1-skill", piElapsedMs=1000)
    data = report.build(tmp_path)
    assert data["summary"]["jev"]["medianPiMs"] == 2000
    assert data["comparison"]["savedMs"] is None
    assert data["comparison"]["reason"] == "formal_runs_incomplete"


def test_completed_comparison_cost_and_break_even_are_separate(tmp_path):
    for i, (jev, skill) in enumerate(((6000, 3000), (9000, 4000), (7000, 3500)), 1):
        trial(tmp_path, f"rr-r{i}-jev", piElapsedMs=jev)
        trial(
            tmp_path,
            f"rr-r{i}-skill",
            piElapsedMs=skill,
            metrics={"recipeHits": 8, "modelFallbacks": 1},
        )
    (tmp_path / "generation.json").write_text(
        json.dumps({"generationMs": 10000, "demonstrationMs": 5000})
    )
    data = report.build(tmp_path)
    assert data["status"] == "complete"
    assert data["comparison"]["savedMs"] == 3500
    assert data["comparison"]["speedup"] == 2
    assert data["comparison"]["reductionPercent"] == 50
    assert data["comparison"]["breakEvenRuns"] == 3
    assert data["comparison"]["breakEvenIncludingDemoRuns"] == 5
    assert data["summary"]["skill"]["medianRecipeHits"] == 8


def test_unknown_and_zero_values_do_not_get_conflated(tmp_path):
    trial(tmp_path, "rr-r1-jev", piElapsedMs=None)
    trial(tmp_path, "rr-r1-skill", piElapsedMs=0, recipeExecution={"hits": 0, "fallbacks": 0})
    data = report.build(tmp_path)
    assert data["trials"][0]["piElapsedMs"] is None
    assert data["summary"]["jev"]["medianPiMs"] is None
    skill = data["trials"][1]
    assert skill["recipeHits"] == 0 and skill["modelFallbacks"] == 0
    assert data["trials"][0]["recipeHits"] is None


def test_video_and_unlisted_attempts_do_not_enter_formal_statistics(tmp_path):
    trial(tmp_path, "rr-r1-jev", piElapsedMs=2000)
    trial(tmp_path, "video-jev", piElapsedMs=3)
    trial(tmp_path, "secret-extra", piElapsedMs=1)
    data = report.build(tmp_path)
    assert len(data["trials"]) == 1
    assert data["summary"]["jev"]["medianPiMs"] == 2000
    assert data["videos"][0]["trial"]["piElapsedMs"] == 3


def test_wrong_mode_is_quarantined_and_keeps_formal_slot_pending(tmp_path):
    trial(tmp_path, "rr-r1-jev", mode="jev-auto")
    trial(tmp_path, "rr-r1-skill", mode="jev")
    data = report.build(tmp_path)
    assert data["trials"] == []
    assert len(data["invalidModes"]) == 2
    assert len(data["pendingFormalIds"]) == 6


def test_render_script_safe_and_public_snapshot_excludes_private_data(tmp_path):
    attack = '</script><script>alert("unsafe")</script>'
    trial(
        tmp_path,
        "rr-r1-jev",
        error=attack,
        secret="PRIVATE",
        rawscreen="PRIVATE",
        modelCalls=[{"latencyMs": 1, "rawAnswer": "PRIVATE"}],
        operations=[{"name": "selected_action", "arguments": {"token": "PRIVATE"}}],
    )
    out = report.render(tmp_path, tmp_path / "report")
    html = out.read_text()
    encoded = html.split(report.MARKER, 1)[1].split("</script>", 1)[0]
    assert "<" not in encoded
    data = json.loads(encoded)
    assert data["trials"][0]["error"] == attack
    assert "PRIVATE" not in encoded
    assert str(tmp_path) not in encoded
    assert attack not in html
    assert (out.parent / "record-replay-data.json").exists()


def test_template_is_portable_and_does_not_fabricate_results():
    template = report.TEMPLATE.read_text()
    embedded = template.split(report.MARKER, 1)[1].split("</script>", 1)[0]
    assert json.loads(embedded)["status"] == "pending"
    assert "/Users/" not in template and "sk-or-" not in template
    assert "交互原型" in template and "实机证据" in template


def test_default_environment_path_does_not_read_keys_or_connect(tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path))
    import config

    monkeypatch.setattr(
        config, "load_openrouter_key", lambda: (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr(config, "mcp_settings", lambda: (_ for _ in ()).throw(AssertionError()))
    assert report.render() == tmp_path / "report" / "record-replay.html"


def test_video_path_is_fixed_relative_and_missing_media_stays_unavailable(tmp_path):
    folder = tmp_path / "report" / "phone-use-videos"
    folder.mkdir(parents=True)
    (folder / "jev-full-flow.mp4").write_bytes(b"fixture")
    data = report.build(tmp_path, tmp_path / "report")
    assert data["videos"][0]["available"] is True
    assert data["videos"][0]["path"] == "phone-use-videos/jev-full-flow.mp4"
    assert data["videos"][1]["available"] is False


def test_invalid_numbers_are_missing_not_published_as_nan(tmp_path):
    trial(
        tmp_path,
        "rr-r1-jev",
        piElapsedMs=float("nan"),
        metrics={"recipeHits": -1, "modelFallbacks": True},
    )
    data = report.build(tmp_path)
    assert data["trials"][0]["piElapsedMs"] is None
    assert data["trials"][0]["recipeHits"] is None
    json.dumps(data, allow_nan=False)


def test_optional_context_is_explicitly_allowlisted_and_absent_stays_empty(tmp_path):
    assert report.build(tmp_path)["context"] == {}
    (tmp_path / "context.json").write_text(
        json.dumps(
            {
                "deviceLabel": "Redmi K60 Pro",
                "androidVersion": "16",
                "screenSize": "1440 × 3200",
                "mcpVersion": "FossDebug test build",
                "statusNote": "两组统一同机，缺失实测保持待测。",
                "serial": "PRIVATE-SERIAL",
                "headers": {"Authorization": "PRIVATE-TOKEN"},
                "rawscreen": "PRIVATE-SCREEN",
            }
        )
    )
    context = report.build(tmp_path)["context"]
    assert set(context) == {
        "deviceLabel",
        "androidVersion",
        "screenSize",
        "mcpVersion",
        "statusNote",
    }
    assert context["deviceLabel"] == "Redmi K60 Pro"
    assert "PRIVATE" not in json.dumps(context)


def test_context_text_is_script_safe_and_does_not_accept_nested_objects(tmp_path):
    attack = '</script><img src=x onerror="alert(1)">'
    (tmp_path / "context.json").write_text(
        json.dumps(
            {
                "deviceLabel": "Redmi K60 Pro",
                "androidVersion": {"secret": "PRIVATE"},
                "screenSize": [1440, 3200],
                "mcpVersion": None,
                "statusNote": attack,
            }
        )
    )
    path = report.render(tmp_path)
    html = path.read_text()
    encoded = html.split(report.MARKER, 1)[1].split("</script>", 1)[0]
    context = json.loads(encoded)["context"]
    assert context == {"deviceLabel": "Redmi K60 Pro", "statusNote": attack}
    assert attack not in html and "PRIVATE" not in encoded
    assert "textContent" in html


def test_explicit_demonstration_source_prevents_old_demo_from_becoming_recipe_evidence(tmp_path):
    trial(tmp_path, "demo", piElapsedMs=999999)
    trial(tmp_path, "demo-v2", piElapsedMs=24000)
    (tmp_path / "generation.json").write_text(
        json.dumps(
            {
                "generationMs": 10,
                "sourceDemoTrialId": "demo-v2",
                "stepCount": 7,
            }
        )
    )
    data = report.build(tmp_path)
    assert data["generation"]["sourceDemoTrialId"] == "demo-v2"
    assert data["generation"]["demonstrationMs"] == 24000
    assert data["demonstration"]["trialId"] == "demo-v2"


def test_generation_without_source_id_does_not_implicitly_claim_old_demo(tmp_path):
    trial(tmp_path, "demo", piElapsedMs=999999)
    (tmp_path / "generation.json").write_text(json.dumps({"generationMs": 10}))
    data = report.build(tmp_path)
    assert data["generation"]["sourceDemoTrialId"] is None
    assert data["generation"]["demonstrationMs"] is None
    assert data["demonstration"] is None


def test_demonstration_source_is_an_id_not_an_arbitrary_file_path(tmp_path):
    (tmp_path / "generation.json").write_text(
        json.dumps(
            {
                "generationMs": 10,
                "sourceDemoTrialId": "../private",
                "demonstrationMs": 20000,
            }
        )
    )
    data = report.build(tmp_path)
    assert data["generation"]["sourceDemoTrialId"] is None
    assert data["generation"]["demonstrationMs"] == 20000
    assert data["demonstration"] is None


def test_reuse_verification_wait_and_reference_refresh_metrics_are_preserved(tmp_path):
    trial(
        tmp_path,
        "rr-r1-skill",
        recipeExecution={
            "recipeHits": 10,
            "verifiedSteps": 9,
            "settleWaits": 5,
            "referenceRefreshes": 1,
        },
    )
    data = report.build(tmp_path)
    item = data["trials"][0]
    assert item["verifiedSteps"] == 9
    assert item["settleWaits"] == 5
    assert item["referenceRefreshes"] == 1
    assert data["summary"]["skill"]["medianVerifiedSteps"] == 9
    assert data["summary"]["skill"]["medianSettleWaits"] == 5
    assert data["summary"]["skill"]["medianReferenceRefreshes"] == 1


def test_missing_reuse_metrics_remain_unknown_while_recorded_zero_is_preserved(tmp_path):
    trial(tmp_path, "rr-r1-skill")
    trial(
        tmp_path,
        "rr-r2-skill",
        metrics={
            "verifiedSteps": 0,
            "settleWaits": 0,
            "referenceRefreshes": 0,
        },
    )
    data = report.build(tmp_path)
    first, second = data["trials"]
    for field in ("verifiedSteps", "settleWaits", "referenceRefreshes"):
        assert first[field] is None
        assert second[field] == 0


def test_template_exposes_execution_details_without_simulated_counts():
    template = report.TEMPLATE.read_text()
    assert "已验证步骤" in template
    assert "条件等待" in template
    assert "未派发引用刷新" in template


def test_explicit_video_selection_keeps_first_failure_and_uses_new_trial(tmp_path):
    trial(tmp_path, "video-jev", status="failed", piElapsedMs=25000)
    trial(tmp_path, "video-jev-retry-1", piElapsedMs=21000)
    selection = {
        "jev": {
            "trialId": "video-jev-retry-1",
            "path": "phone-use-videos/jev-retry-1-full-flow.mp4",
            "earlierAttempts": [
                {"trialId": "video-jev", "path": "phone-use-videos/jev-full-flow.mp4"}
            ],
        }
    }
    (tmp_path / "video-selection.json").write_text(json.dumps(selection))
    videos = tmp_path / "report" / "phone-use-videos"
    videos.mkdir(parents=True)
    (videos / "jev-full-flow.mp4").write_bytes(b"original-failed-video")
    (videos / "jev-retry-1-full-flow.mp4").write_bytes(b"successful-new-video")
    data = report.build(tmp_path)
    video = data["videos"][0]
    assert video["trialId"] == "video-jev-retry-1"
    assert video["trial"]["piElapsedMs"] == 21000
    assert video["path"] == selection["jev"]["path"] and video["available"] is True
    assert video["earlierAttempts"][0]["trial"]["status"] == "failed"
    assert video["earlierAttempts"][0]["available"] is True
    assert data["trials"] == []
    assert data["summary"]["jev"]["attempts"] == 0


def test_missing_selected_video_stays_selected_instead_of_falling_back(tmp_path):
    trial(tmp_path, "video-jev", piElapsedMs=999)
    (tmp_path / "video-selection.json").write_text(
        json.dumps(
            {
                "jev": {
                    "trialId": "video-jev-retry-1",
                    "path": "phone-use-videos/jev-retry-1-full-flow.mp4",
                }
            }
        )
    )
    item = report.build(tmp_path)["videos"][0]
    assert item["trialId"] == "video-jev-retry-1"
    assert item["trial"] is None and item["available"] is False


def test_invalid_video_selection_never_silently_falls_back_to_original(tmp_path):
    import pytest

    for entry in (
        {"trialId": "../private", "path": "phone-use-videos/jev.mp4"},
        {"trialId": "video-jev", "path": "../private.mp4"},
        {"trialId": "video-jev", "path": "phone-use-videos/nested/jev.mp4"},
        {"trialId": "video-jev", "path": "https://example.com/jev.mp4"},
        {"trialId": "video-jev", "path": "phone-use-videos/jev.mp4", "earlierAttempts": None},
        None,
        {},
    ):
        (tmp_path / "video-selection.json").write_text(json.dumps({"jev": entry}))
        with pytest.raises(ValueError):
            report.build(tmp_path)


def test_selected_video_cannot_resolve_outside_report_directory(tmp_path):
    import pytest

    outside = tmp_path / "private.mp4"
    outside.write_bytes(b"private")
    folder = tmp_path / "report" / "phone-use-videos"
    folder.mkdir(parents=True)
    (folder / "jev.mp4").symlink_to(outside)
    (tmp_path / "video-selection.json").write_text(
        json.dumps(
            {
                "jev": {
                    "trialId": "video-jev",
                    "path": "phone-use-videos/jev.mp4",
                }
            }
        )
    )
    with pytest.raises(ValueError):
        report.build(tmp_path)


def test_selected_video_mode_must_match_actual_trial(tmp_path):
    import pytest

    trial(tmp_path, "video-jev-retry-1", mode="skill")
    (tmp_path / "video-selection.json").write_text(
        json.dumps(
            {
                "jev": {
                    "trialId": "video-jev-retry-1",
                    "path": "phone-use-videos/jev-retry-1-full-flow.mp4",
                }
            }
        )
    )
    with pytest.raises(ValueError):
        report.build(tmp_path)


def test_template_exposes_earlier_failed_video_attempts():
    template = report.TEMPLATE.read_text()
    assert "保留的较早录像尝试" in template
    assert "打开原始录像" in template
