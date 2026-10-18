"""Model candidates obey stage guards and never repeat satisfied toggles."""

import time

import pytest

import controller
from test_screen_geometry import node, runtime_fixture


def phone(monkeypatch, tmp_path, phase, liked, collected):
    size = (1440, 3200)
    nodes = [
        node("title", controller.TITLE, [40, 1500, 1000, 1700], size, clickable=False),
        node("author", "作者小狮日记", [40, 1700, 1000, 1800], size, clickable=False),
        node("like", "22", [447, 2163, 665, 2295], size),
        node("favorite", "收藏", [665, 2163, 890, 2295], size),
    ]
    runtime = runtime_fixture(monkeypatch, tmp_path, size, nodes)
    runtime.progress.phase = phase
    runtime.phases, runtime.started = [], time.perf_counter()
    flags = {"like": liked, "collect": collected}
    monkeypatch.setattr(
        controller, "icon_state", lambda image, bounds, kind, **kwargs: {"active": flags[kind]}
    )
    monkeypatch.setattr(controller.time, "sleep", lambda _: None)
    return runtime, flags


def toggle_roles(snapshot):
    return {
        value["role"]
        for value in snapshot["state"]["actionDescriptors"].values()
        if value["role"] in {"like", "collect"}
    }


@pytest.mark.parametrize(
    "phase,liked,collected,role",
    [
        ("like", False, False, "like"),
        ("collect", True, False, "collect"),
        ("unlike", True, True, "like"),
        ("uncollect", False, True, "collect"),
    ],
)
def test_normal_stage_only_offers_its_current_unsatisfied_toggle(
    monkeypatch, tmp_path, phase, liked, collected, role
):
    runtime, _ = phone(monkeypatch, tmp_path, phase, liked, collected)
    snapshot = runtime.observe()
    assert toggle_roles(snapshot) == {role}
    for choice in snapshot["options"]:
        controller.allowed_choice(choice, runtime.actions, runtime.progress.phase, snapshot)
    assert (
        set(snapshot["options"])
        == set(runtime.actions)
        == set(snapshot["state"]["actionDescriptors"])
    )


@pytest.mark.parametrize(
    "phase,liked,collected",
    [
        ("like", False, True),
        ("collect", False, False),
        ("unlike", True, False),
        ("uncollect", True, True),
    ],
)
def test_counterpart_transient_only_offers_wait_or_blocked(
    monkeypatch, tmp_path, phase, liked, collected
):
    runtime, _ = phone(monkeypatch, tmp_path, phase, liked, collected)
    snapshot = runtime.observe()
    assert set(snapshot["options"]) == {"WAIT", "BLOCKED"}
    assert runtime.progress.phase == phase and runtime.progress.milestones == []


@pytest.mark.parametrize(
    "phase,liked,collected",
    [
        ("like", True, True),
        ("collect", False, True),
        ("unlike", False, False),
        ("uncollect", True, False),
    ],
)
def test_satisfied_target_with_unverified_counterpart_never_offers_repeat_toggle(
    monkeypatch, tmp_path, phase, liked, collected
):
    runtime, _ = phone(monkeypatch, tmp_path, phase, liked, collected)
    snapshot = runtime.observe()
    assert set(snapshot["options"]) == {"WAIT", "BLOCKED"}
    assert toggle_roles(snapshot) == set()


def test_actual_failure_shape_waits_then_uncollects_without_skipping_milestone(
    monkeypatch, tmp_path
):
    # Sanitized state16/state17 of terminal-ce9ba41b: the screenshot briefly
    # reported F/F after unlike; the following read restored F/T.
    runtime, flags = phone(monkeypatch, tmp_path, "unlike", False, False)
    initial = runtime.observe()
    assert initial["state"]["stage"] == "unlike"
    assert set(initial["options"]) == {"WAIT", "BLOCKED"}
    flags["collect"] = True
    after = runtime.act("WAIT")
    assert after is runtime.snapshot and runtime.progress.phase == "uncollect"
    assert [(item["stage"], item["next"]) for item in runtime.progress.milestones] == [
        ("unlike", "uncollect")
    ]
    assert toggle_roles(after) == {"collect"}
    flags["collect"] = False
    final = runtime.observe()
    assert final["done"] and runtime.progress.phase == "done"
    assert [(item["stage"], item["next"]) for item in runtime.progress.milestones] == [
        ("unlike", "uncollect"),
        ("uncollect", "done"),
    ]
    assert [
        item["choice"] for item in runtime.operations if item.get("name") == "selected_action"
    ] == ["WAIT"]


def test_recovery_model_waits_on_transient_then_dispatches_only_current_toggle(
    monkeypatch, tmp_path
):
    import asyncio

    from skill_auto import SkillAutoPolicy
    from test_workflow_companion import paired
    from test_workflow_intents import supported_raw
    from workflow import compile_recording
    from workflow_intents import derive_plan

    recipe = compile_recording(paired(derive_plan(supported_raw())), tmp_path / "bundle")
    runtime, flags = phone(monkeypatch, tmp_path, "unlike", False, False)
    call, dispatched, requested = runtime.call, [], []

    def mcp(name, arguments):
        if name == "android_click_node":
            dispatched.append(arguments["node_id"])
            assert arguments["node_id"] == "favorite"
            flags["collect"] = False
        return call(name, arguments)

    runtime.call = mcp

    class Model:
        async def decide(self, backend, state, options):
            requested.append((backend, state["stage"], set(options)))
            choice = "CLICK:favorite" if "CLICK:favorite" in options else "WAIT"
            return {"choice": choice, "latencyMs": 0, "cost": 0, "usage": {}}

    policy = SkillAutoPolicy(Model(), "skill-auto", recipe=recipe)
    before = runtime.observe()
    first = asyncio.run(policy.decide(before, runtime.index))
    assert first["recipeRecovery"] and first["choice"] == "WAIT"
    flags["collect"] = True
    after = runtime.act(first["choice"])
    policy.verify(after)
    assert dispatched == [] and runtime.progress.phase == "uncollect"
    second = asyncio.run(policy.decide(after, runtime.index))
    assert second["choice"] == "CLICK:favorite"
    final = runtime.act(second["choice"])
    policy.verify(final)
    assert dispatched == ["favorite"] and final["done"]
    assert requested == [
        ("jev", "unlike", {"WAIT", "BLOCKED"}),
        ("jev", "uncollect", {"WAIT", "BLOCKED", "CLICK:favorite"}),
    ]
    assert [(item["stage"], item["next"]) for item in runtime.progress.milestones] == [
        ("unlike", "uncollect"),
        ("uncollect", "done"),
    ]
