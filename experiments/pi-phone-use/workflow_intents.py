"""Conservative fixed-scope intent extraction from unverified Android events."""

from __future__ import annotations

import hashlib
import json
import re

from controller import PACKAGE, QUERY, TITLE

KINDS = (
    "search_entry",
    "query",
    "search_submit",
    "open_target",
    "like",
    "collect",
    "unlike",
    "uncollect",
)
TASK = {"query": QUERY, "expectedFirstTitle": TITLE}


class IntentError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _labels(selector):
    return [
        value.strip()
        for key in ("text", "contentDescription")
        if isinstance(value := selector.get(key), str) and value.strip()
    ]


def _box(evidence, key):
    box = evidence[key]
    if set(box) != {"left", "top", "right", "bottom"} or any(
        type(v) is not int for v in box.values()
    ):
        raise ValueError
    width, height = evidence["screenWidth"], evidence["screenHeight"]
    if type(width) is not int or type(height) is not int or not 0 < width < height <= 20000:
        raise ValueError
    if not (0 <= box["left"] < box["right"] <= width and 0 <= box["top"] < box["bottom"] <= height):
        raise ValueError
    return box


def _toolbar(event, expected_role):
    selector, evidence = event["selector"], event.get("evidence", {})
    if (
        selector.get("semanticRole") != expected_role
        or selector.get("contextTitle") != TITLE
        or evidence.get("source") != "xhs-toolbar-candidate"
        or evidence.get("contextAuthor") != "小狮日记"
    ):
        return False
    source, anchor = _box(evidence, "sourceBounds"), _box(evidence, "favoriteAnchorBounds")
    labels = evidence.get("favoriteAnchorLabels", [])
    if (
        not isinstance(labels, list)
        or not 1 <= len(labels) <= 12
        or not all(isinstance(label, str) and len(label) <= 2000 for label in labels)
        or not any(label.startswith(("收藏", "已收藏")) for label in labels)
        or min(source["top"], anchor["top"]) < evidence["screenHeight"] * 0.8
    ):
        return False
    if expected_role == "collect_toggle":
        return source == anchor
    height = min(source["bottom"] - source["top"], anchor["bottom"] - anchor["top"])
    overlap = min(source["bottom"], anchor["bottom"]) - max(source["top"], anchor["top"])
    gap = anchor["left"] - source["right"]
    return overlap >= height * 0.5 and 0 <= gap <= height * 3


def _derive_strict(raw):
    try:
        events = raw["events"]
        if (
            raw.get("schemaVersion") != 1
            or raw.get("source") != "android-accessibility-events"
            or raw.get("targetPackage") != PACKAGE
            or raw.get("needsReview") is not True
            or raw.get("hasPairedVerification") is not False
            or len(events) != len(KINDS)
        ):
            raise ValueError
        previous = -1
        intents = []
        for index, (event, kind) in enumerate(zip(events, KINDS), 1):
            selector = event["selector"]
            if (
                event.get("sequence") != index
                or type(event.get("elapsedMs")) is not int
                or event["elapsedMs"] < previous
                or selector.get("packageName") != PACKAGE
            ):
                raise ValueError
            previous = event["elapsedMs"]
            labels = _labels(selector)
            if kind == "query":
                valid = (
                    event["type"] == "text"
                    and isinstance(event.get("value"), str)
                    and re.sub(r"^搜索[,，]\s*", "", event["value"].strip()).strip() == QUERY
                )
            elif event["type"] != "click":
                valid = False
            elif kind in {"search_entry", "search_submit"}:
                valid = labels == ["搜索"] or (
                    bool(labels) and all(label == "搜索" for label in labels)
                )
            elif kind == "open_target":
                valid = any(TITLE in label for label in labels)
                if not valid:
                    evidence = event.get("evidence", {})
                    valid = (
                        selector.get("semanticRole") == "open_target"
                        and selector.get("contextTitle") == TITLE
                        and evidence.get("source") in {"source-label-v1", "source-descendants-v1"}
                        and any(
                            TITLE in label
                            for label in evidence.get("sourceLabels", [])
                            if isinstance(label, str)
                        )
                    )
            else:
                role = "like_toggle" if kind in {"like", "unlike"} else "collect_toggle"
                if selector.get("semanticRole") is not None:
                    valid = _toolbar(event, role)
                else:
                    allowed = {
                        "like": {"点赞"},
                        "collect": {"收藏"},
                        "unlike": {"取消点赞"},
                        "uncollect": {"取消收藏", "已收藏"},
                    }[kind]
                    valid = bool(labels) and all(label in allowed for label in labels)
            if not valid:
                raise ValueError
            intents.append({"kind": kind, "eventSequence": index, "eventSha256": digest(event)})
        plan = {"schemaVersion": 1, "task": TASK, "rawSha256": digest(raw), "intents": intents}
        plan["planSha256"] = digest(plan)
        return plan
    except (KeyError, TypeError, ValueError, AttributeError):
        raise IntentError(
            "Recorded events do not establish the complete supported intent sequence"
        ) from None


SEMANTIC_KINDS = tuple(kind for kind in KINDS if kind != "search_submit")


def transition_policy(query_sequence, open_sequence):
    return {
        "kind": "search_submit",
        "origin": "runtime-required-transition",
        "betweenRawEvents": [query_sequence, open_sequence],
        "maxDispatches": 1,
        "maxWaits": 8,
    }


def query_text(value):
    return re.sub(r"^搜索[,，]\s*", "", value.strip()).strip() if isinstance(value, str) else None


def _notification(event):
    selector = event["selector"]
    start, end = event.get("scrollFromIndex"), event.get("scrollToIndex")
    valid_range = (start is None and end is None) or (
        type(start) is int and start == 0 and type(end) is int and 0 <= end <= 12
    )
    if (
        event["type"] != "scroll"
        or selector.get("className") != "androidx.recyclerview.widget.RecyclerView"
        or not isinstance(selector.get("resourceId"), str)
        or not selector["resourceId"]
        or _labels(selector)
        or selector.get("semanticRole") is not None
        or selector.get("contextTitle") is not None
        or event.get("value") is not None
        or event.get("evidence") is not None
        or not valid_range
    ):
        raise ValueError
    return {
        "eventSequence": event["sequence"],
        "eventSha256": digest(event),
        "notification": "android-view-scrolled",
        "gestureReplayed": False,
        "scrollFromIndex": start,
        "scrollToIndex": end,
    }


def _semantic_intent(event, kind):
    selector = event["selector"]
    if kind == "search_entry":
        labels = _labels(selector)
        valid = (
            event["type"] == "click" and bool(labels) and all(label == "搜索" for label in labels)
        )
    elif kind == "search_submit":
        labels = _labels(selector)
        valid = (
            event["type"] == "click"
            and bool(labels)
            and all(label == "搜索" for label in labels)
            and selector.get("className") in {"android.widget.Button", "android.widget.TextView"}
        )
    elif kind == "query":
        valid = (
            event["type"] == "text"
            and query_text(event.get("value")) == QUERY
            and selector.get("className") == "android.widget.EditText"
            and isinstance(selector.get("resourceId"), str)
            and bool(selector["resourceId"])
        )
    elif kind == "open_target":
        evidence = event.get("evidence") or {}
        valid = (
            event["type"] == "click"
            and selector.get("semanticRole") == "open_target"
            and selector.get("contextTitle") == TITLE
            and evidence.get("source") in {"source-label-v1", "source-descendants-v1"}
            and any(
                isinstance(label, str) and TITLE in label
                for label in evidence.get("sourceLabels", [])
            )
        )
    else:
        valid = event["type"] == "click" and _toolbar(
            event, "like_toggle" if kind in {"like", "unlike"} else "collect_toggle"
        )
    if not valid:
        raise ValueError
    return {"kind": kind, "eventSequence": event["sequence"], "eventSha256": digest(event)}


def _derive_semantic(raw):
    events = raw["events"]
    if (
        raw.get("schemaVersion") != 1
        or raw.get("source") != "android-accessibility-events"
        or raw.get("targetPackage") != PACKAGE
        or raw.get("needsReview") is not True
        or raw.get("hasPairedVerification") is not False
        or not 7 <= len(events) <= 14
    ):
        raise ValueError
    previous = -1
    for sequence, event in enumerate(events, 1):
        if (
            type(event.get("sequence")) is not int
            or event["sequence"] != sequence
            or type(event.get("elapsedMs")) is not int
            or event["elapsedMs"] < previous
            or event["selector"].get("packageName") != PACKAGE
        ):
            raise ValueError
        previous = event["elapsedMs"]
    intents, focus, notifications, cursor = [], [], [], 0
    for kind in SEMANTIC_KINDS:
        if kind == "query":
            while (
                cursor < len(events)
                and events[cursor]["type"] == "click"
                and events[cursor]["selector"].get("className") == "android.widget.EditText"
            ):
                focus.append(events[cursor])
                cursor += 1
                if len(focus) > 2:
                    raise ValueError
        if kind in {"open_target", "like"}:
            window = []
            while cursor < len(events) and events[cursor]["type"] == "scroll":
                window.append(_notification(events[cursor]))
                cursor += 1
                if len(window) > 2:
                    raise ValueError
            if (
                kind == "open_target"
                and events[cursor]["type"] == "click"
                and _labels(events[cursor]["selector"])
                and all(label == "搜索" for label in _labels(events[cursor]["selector"]))
            ):
                intents.append(_semantic_intent(events[cursor], "search_submit"))
                cursor += 1
                while cursor < len(events) and events[cursor]["type"] == "scroll":
                    window.append(_notification(events[cursor]))
                    cursor += 1
                    if len(window) > 2:
                        raise ValueError
            notifications.extend(window)
        intents.append(_semantic_intent(events[cursor], kind))
        cursor += 1
    if cursor != len(events):
        raise ValueError
    field = events[intents[1]["eventSequence"] - 1]["selector"]
    previous_value = ""
    for event in focus:
        selector, labels = event["selector"], _labels(event["selector"])
        values = [query_text(label) for label in labels]
        if (
            selector.get("resourceId") != field["resourceId"]
            or event.get("value") is not None
            or selector.get("semanticRole") is not None
            or selector.get("contextTitle") is not None
            or event.get("evidence") is not None
            or not labels
            or len(set(values)) != 1
            or values[0] not in {"", QUERY}
            or (previous_value == QUERY and values[0] == "")
            or any(not label.startswith(("搜索,", "搜索，")) for label in labels)
        ):
            raise ValueError
        previous_value = values[0]
    intent_sequences = {item["eventSequence"] for item in intents}
    focus_sequences = [item["sequence"] for item in focus]
    dispositions = [
        {
            "eventSequence": event["sequence"],
            "eventSha256": digest(event),
            "disposition": "raw-intent"
            if event["sequence"] in intent_sequences
            else "query-focus-evidence"
            if event["sequence"] in focus_sequences
            else "observation-notification",
        }
        for event in events
    ]
    plan = {
        "schemaVersion": 2,
        "normalization": "xhs-query-focus-scroll-v1",
        "task": TASK,
        "rawSha256": digest(raw),
        "intents": intents,
        "rawEventDispositions": dispositions,
        "queryFocusEventSequences": focus_sequences,
        "observationNotifications": notifications,
        "runtimeTransitionPolicy": (
            transition_policy(intents[1]["eventSequence"], intents[2]["eventSequence"])
            if len(intents) == 7
            else None
        ),
    }
    plan["planSha256"] = digest(plan)
    return plan


def derive_plan(raw):
    try:
        if len(raw["events"]) == 8:
            try:
                return _derive_strict(raw)
            except IntentError:
                pass
        return _derive_semantic(raw)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise IntentError(
            "Recorded events do not establish the supported bounded intent sequence"
        ) from None


def validate_plan(plan):
    try:
        version = plan["schemaVersion"]
        kinds = KINDS if version == 1 or len(plan["intents"]) == 8 else SEMANTIC_KINDS
        sequences = [item["eventSequence"] for item in plan["intents"]]
        if (
            version not in (1, 2)
            or plan.get("task") != TASK
            or plan.get("planSha256")
            != digest({key: value for key, value in plan.items() if key != "planSha256"})
            or [item["kind"] for item in plan["intents"]] != list(kinds)
            or any(type(item) is not int for item in sequences)
            or sequences != sorted(set(sequences))
        ):
            raise ValueError
        if version == 1 and sequences != list(range(1, 9)):
            raise ValueError
        if version == 2:
            coverage, notifications, focus = (
                plan["rawEventDispositions"],
                plan["observationNotifications"],
                plan["queryFocusEventSequences"],
            )
            notification_sequences = [item["eventSequence"] for item in notifications]
            open_sequence = sequences[2] if len(sequences) == 7 else sequences[3]
            first_like = sequences[3] if len(sequences) == 7 else sequences[4]
            if (
                plan.get("normalization") != "xhs-query-focus-scroll-v1"
                or not 7 <= len(coverage) <= 14
                or [item["eventSequence"] for item in coverage] != list(range(1, len(coverage) + 1))
                or not 0 <= len(focus) <= 2
                or focus != sorted(set(focus))
                or any(not sequences[0] < item < sequences[1] for item in focus)
                or notification_sequences != sorted(set(notification_sequences))
                or any(
                    not (sequences[1] < item < open_sequence or open_sequence < item < first_like)
                    for item in notification_sequences
                )
                or sum(sequences[1] < item < open_sequence for item in notification_sequences) > 2
                or sum(open_sequence < item < first_like for item in notification_sequences) > 2
                or set(sequences + focus + notification_sequences)
                != set(range(1, len(coverage) + 1))
                or len(sequences + focus + notification_sequences) != len(coverage)
                or [item["disposition"] for item in coverage]
                != [
                    "raw-intent"
                    if i in sequences
                    else "query-focus-evidence"
                    if i in focus
                    else "observation-notification"
                    for i in range(1, len(coverage) + 1)
                ]
                or plan.get("runtimeTransitionPolicy")
                != (transition_policy(sequences[1], open_sequence) if len(sequences) == 7 else None)
                or any(
                    item["eventSha256"] != coverage[item["eventSequence"] - 1]["eventSha256"]
                    for item in plan["intents"] + notifications
                )
                or any(
                    item.get("gestureReplayed") is not False
                    or item.get("notification") != "android-view-scrolled"
                    for item in notifications
                )
            ):
                raise ValueError
        return plan
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise IntentError("Invalid bounded intent plan") from None


def validation_binding(plan):
    binding = {"androidRecordingSha256": plan["rawSha256"], "intentPlanSha256": plan["planSha256"]}
    if plan["schemaVersion"] == 1:
        return {
            **binding,
            "consumedEventSequences": [step["eventSequence"] for step in plan["intents"]],
            "intentKinds": [step["kind"] for step in plan["intents"]],
            "allConsumed": True,
        }
    return {
        **binding,
        "schemaVersion": 2,
        "rawIntentCount": len(plan["intents"]),
        "rawEventCount": len(plan["rawEventDispositions"]),
        "rawIntentEventSequences": [step["eventSequence"] for step in plan["intents"]],
        "accountedEventSequences": [item["eventSequence"] for item in plan["rawEventDispositions"]],
        "rawIntentKinds": [step["kind"] for step in plan["intents"]],
        "eventDispositionsSha256": digest(plan["rawEventDispositions"]),
        "queryFocusEventSequences": list(plan["queryFocusEventSequences"]),
        "observationNotificationEventSequences": [
            item["eventSequence"] for item in plan["observationNotifications"]
        ],
        "allRawIntentsVerified": True,
        "allRawEventsAccountedFor": True,
        "allRawActionsReplayed": False,
    }


def matches_intent(kind, snapshot, action):
    state, observation = snapshot["state"], snapshot["state"]["observation"]
    if any(state.get(key) != value for key, value in TASK.items()):
        return False
    method, role, label = (action.get(key) for key in ("method", "role", "label"))
    if kind == "search_entry":
        return (
            method == "click"
            and observation["kind"] == "feed"
            and (label == "搜索" or role == "search_field")
        )
    if kind == "query":
        return method == "type" and role == "search_field" and state["stage"] == "search"
    if kind == "search_submit":
        return method == "click" and label == "搜索" and observation.get("queryMatches") is True
    if kind == "open_target":
        return method == "click" and TITLE in label and state["stage"] == "open_first"
    flags = {
        "like": ("like", False, False),
        "collect": ("collect", True, False),
        "unlike": ("like", True, True),
        "uncollect": ("collect", False, True),
    }
    expected_role, liked, collected = flags[kind]
    return (
        method in {"click", "tap"}
        and role == expected_role
        and state["stage"] == kind
        and observation.get("kind") == "detail"
        and observation.get("firstTitleVisible") is True
        and observation.get("liked") is liked
        and observation.get("collected") is collected
    )


def intent_completed(kind, snapshot):
    state, observed = snapshot["state"], snapshot["state"]["observation"]
    if any(state.get(key) != value for key, value in TASK.items()):
        return False
    if kind == "search_entry":
        return observed["kind"] == "search_input"
    if kind == "query":
        return observed.get("queryMatches") is True and state["stage"] == "search"
    if kind == "search_submit":
        return (
            observed["kind"] == "results"
            and observed.get("queryMatches") is True
            and state["stage"] == "open_first"
        )
    stages = {
        "open_target": ("like", False, False),
        "like": ("collect", True, False),
        "collect": ("unlike", True, True),
        "unlike": ("uncollect", False, True),
        "uncollect": ("done", False, False),
    }
    stage, liked, collected = stages[kind]
    return (
        state["stage"] == stage
        and observed["kind"] == "detail"
        and observed.get("firstTitleVisible") is True
        and observed.get("liked") is liked
        and observed.get("collected") is collected
    )


def transition_matches(snapshot, action):
    return (
        matches_intent("search_submit", snapshot, action)
        and snapshot["state"]["stage"] == "search"
        and snapshot["state"]["observation"]["kind"] == "search_input"
    )


def raw_intent_completed(kind, snapshot, plan):
    return intent_completed(kind, snapshot) or (
        plan["schemaVersion"] == 2
        and kind == "query"
        and intent_completed("search_submit", snapshot)
    )


def evidence_matches_plan(evidence, plan):
    """Validate source-bound intents, distinct navigation, and paired outcomes."""
    try:
        steps = evidence["steps"]
        annotated = [
            (index, step)
            for index, step in enumerate(steps)
            if step.get("rawEventSequence") is not None
        ]
        semantic = plan["schemaVersion"] == 2
        if evidence.get("recordingValidation") != validation_binding(plan) or len(annotated) != len(
            plan["intents"]
        ):
            return False
        if semantic and (
            evidence.get("rawEventDispositions") != plan["rawEventDispositions"]
            or evidence.get("observationNotifications") != plan["observationNotifications"]
            or evidence.get("runtimeTransitionPolicy") != plan["runtimeTransitionPolicy"]
        ):
            return False
        for position, ((index, step), intent) in enumerate(zip(annotated, plan["intents"])):
            end = annotated[position + 1][0] if position + 1 < len(annotated) else len(steps)
            if (
                step.get("rawEventSequence") != intent["eventSequence"]
                or step.get("intentKind") != intent["kind"]
                or step.get("delivery", "applied") != "applied"
                or not matches_intent(intent["kind"], step["before"], step["action"])
                or not any(
                    raw_intent_completed(intent["kind"], item["after"], plan)
                    for item in steps[index:end]
                )
            ):
                return False
            if semantic and (
                step.get("origin") != "raw-intent"
                or step.get("rawEventSha256") != intent["eventSha256"]
            ):
                return False
            if (
                semantic
                and intent["kind"] == "open_target"
                and not intent_completed("search_submit", step["before"])
            ):
                return False
            if (
                semantic
                and intent["kind"] == "search_submit"
                and not transition_matches(step["before"], step["action"])
            ):
                return False
        cursor, transition_count = 0, 0
        for step in steps:
            if step.get("rawEventSequence") is not None:
                cursor += 1
                continue
            if step["action"].get("method") == "wait":
                continue
            if (
                step.get("origin") == "raw-intent-undelivered"
                and step.get("delivery") == "not_dispatched"
            ):
                intent = plan["intents"][cursor]
                if (
                    step.get("intendedRawEventSequence") != intent["eventSequence"]
                    or step.get("intentKind") != intent["kind"]
                    or not matches_intent(intent["kind"], step["before"], step["action"])
                ):
                    return False
                continue
            if (
                not semantic
                or plan.get("runtimeTransitionPolicy") is None
                or cursor != 2
                or step.get("origin") != "runtime-required-transition"
                or step.get("rawEventSequence") is not None
                or step.get("betweenRawEvents")
                != plan["runtimeTransitionPolicy"]["betweenRawEvents"]
                or not transition_matches(step["before"], step["action"])
            ):
                return False
            if step.get("delivery") == "applied":
                transition_count += 1
            elif step.get("delivery") != "not_dispatched":
                return False
        return transition_count <= 1
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return False
