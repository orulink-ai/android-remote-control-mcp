"""Raw candidates establish a bounded intent trace, never verified playback."""

import copy

import pytest

from controller import QUERY, TITLE
from workflow_intents import IntentError, derive_plan


def supported_raw():
    labels = ["搜索", None, "搜索", TITLE, "点赞", "收藏", "取消点赞", "取消收藏"]
    return {
        "id": "recording-001",
        "schemaVersion": 1,
        "source": "android-accessibility-events",
        "targetPackage": "com.xingin.xhs",
        "needsReview": True,
        "hasPairedVerification": False,
        "events": [
            {
                "sequence": i + 1,
                "elapsedMs": i * 100,
                "type": "text" if i == 1 else "click",
                "selector": {"packageName": "com.xingin.xhs", "text": label},
                **({"value": "搜索, " + QUERY} if i == 1 else {}),
            }
            for i, label in enumerate(labels)
        ],
    }


def candidate(role):
    like = role == "like_toggle"
    return {
        "sequence": 5,
        "elapsedMs": 400,
        "type": "click",
        "selector": {"packageName": "com.xingin.xhs", "semanticRole": role, "contextTitle": TITLE},
        "evidence": {
            "source": "xhs-toolbar-candidate",
            "contextAuthor": "小狮日记",
            "screenWidth": 1080,
            "screenHeight": 2340,
            "sourceBounds": {
                "left": 200 if like else 360,
                "top": 2130,
                "right": 300 if like else 460,
                "bottom": 2250,
            },
            "favoriteAnchorBounds": {"left": 360, "top": 2130, "right": 460, "bottom": 2250},
            "favoriteAnchorLabels": ["收藏177"],
            "sourceLabels": ["1600"] if like else ["收藏177"],
        },
    }


def test_complete_raw_intent_order_is_bound_to_source_hash():
    plan = derive_plan(supported_raw())
    assert [step["kind"] for step in plan["intents"]] == [
        "search_entry",
        "query",
        "search_submit",
        "open_target",
        "like",
        "collect",
        "unlike",
        "uncollect",
    ]
    assert [step["eventSequence"] for step in plan["intents"]] == list(range(1, 9))
    assert plan["rawSha256"] and plan["planSha256"]


@pytest.mark.parametrize(
    "change", ["query-only", "order", "other-title", "numeric", "publish", "missing-package"]
)
def test_incomplete_unrelated_or_ambiguous_raw_is_rejected(change):
    raw = supported_raw()
    if change == "query-only":
        raw["events"] = [raw["events"][1]]
    elif change == "order":
        raw["events"][4], raw["events"][5] = raw["events"][5], raw["events"][4]
    elif change == "other-title":
        raw["events"][3]["selector"]["text"] = "另一篇帖子"
    elif change == "numeric":
        raw["events"][4]["selector"]["text"] = "1600"
    elif change == "publish":
        raw["events"].append({"type": "click", "selector": {"text": "发布"}})
    else:
        raw["events"][4]["selector"].pop("packageName")
    with pytest.raises(IntentError):
        derive_plan(raw)


def test_toolbar_candidate_requires_anchor_context_and_valid_geometry():
    raw = supported_raw()
    for i, role in [
        (4, "like_toggle"),
        (5, "collect_toggle"),
        (6, "like_toggle"),
        (7, "collect_toggle"),
    ]:
        raw["events"][i] = {**candidate(role), "sequence": i + 1, "elapsedMs": i * 100}
    assert len(derive_plan(raw)["intents"]) == 8
    for field in ("favoriteAnchorLabels", "contextAuthor", "favoriteAnchorBounds"):
        bad = copy.deepcopy(raw)
        bad["events"][4]["evidence"].pop(field)
        with pytest.raises(IntentError):
            derive_plan(bad)
    bad = copy.deepcopy(raw)
    bad["events"][4]["evidence"]["sourceBounds"]["top"] = 100
    with pytest.raises(IntentError):
        derive_plan(bad)
