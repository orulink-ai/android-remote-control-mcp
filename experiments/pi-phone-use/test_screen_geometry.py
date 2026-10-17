"""Offline fixtures: device dimensions affect verification, never dispatched bounds."""

import base64
import io

from PIL import Image, ImageDraw
import pytest

import controller
import run_pi_trial


SIZES = [(1080, 2340), (1440, 3200)]


def scaled(bounds, size):
    return [round(v * size[index % 2] / (1080, 2340)[index % 2]) for index, v in enumerate(bounds)]


def node(key, text, bounds, size, *, cls="TextView", clickable=True):
    return {
        "id": key,
        "class": cls,
        "text": text,
        "description": "",
        "resource_id": "",
        "bounds": scaled(bounds, size),
        "flags": ["on", "ena"] + (["clk"] if clickable else []),
        "package": controller.PACKAGE,
    }


def screenshot(size, active=False):
    # MCP's image is resized independently of its physical-coordinate tree.
    image = Image.new("RGB", (540, round(size[1] * 540 / size[0])), "white")
    b = scaled([447, 2163, 665, 2295], size)
    x1, y1, x2, y2 = b
    roi = [
        int((x1 + (x2 - x1) * 0.08) * image.width / size[0]),
        int((y1 + (y2 - y1) * 0.15) * image.height / size[1]),
        int((x1 + (x2 - x1) * 0.45) * image.width / size[0]),
        int((y1 + (y2 - y1) * 0.85) * image.height / size[1]),
    ]
    if active:
        ImageDraw.Draw(image).rectangle(roi, fill=(230, 20, 30))
    return image, roi


def runtime_fixture(monkeypatch, tmp_path, size, nodes, *, active=False, window_header=None):
    image, _ = screenshot(size, active)
    data = io.BytesIO()
    image.save(data, "PNG")
    result = {
        "content": [
            {
                "type": "text",
                "text": f"screen:{size[0]}x{size[1]} density:480 orientation:portrait\n"
                + (
                    window_header
                    if window_header is not None
                    else "--- window:1 type:APPLICATION pkg:com.xingin.xhs title:小红书 layer:0 focused:true ---\n"
                ),
            },
            {"type": "image", "data": base64.b64encode(data.getvalue()).decode()},
        ]
    }
    runtime = controller.PhoneRuntime.__new__(controller.PhoneRuntime)
    runtime.call = lambda *args: result
    runtime.index = 0
    runtime.directory = tmp_path
    runtime.operations = []
    runtime.progress = controller.Progress()
    monkeypatch.setattr(controller, "own_nodes", lambda _text: nodes)
    return runtime


@pytest.mark.parametrize("size", SIZES)
def test_query_field_uses_physical_screen_dimensions(monkeypatch, tmp_path, size):
    nodes = [
        node("field", "搜索, 小狮日记", [180, 114, 740, 246], size),
        node("submit", "搜索", [864, 114, 1032, 246], size),
    ]
    runtime = runtime_fixture(monkeypatch, tmp_path, size, nodes)
    snapshot = runtime.observe()
    observation = snapshot["state"]["observation"]
    assert observation["kind"] == "search_results_other"
    assert observation["observedQuery"] == "小狮日记"
    assert observation["queryMatches"] is True
    action = runtime.actions["CLICK:field"]
    assert action["role"] == "search_field"
    assert action["bounds"] == scaled([180, 114, 740, 246], size)
    assert snapshot["state"]["actionDescriptors"]["CLICK:field"]["role"] == "search_field"


@pytest.mark.parametrize("size", SIZES)
def test_detail_verification_and_toolbar_actions_keep_actual_bounds(monkeypatch, tmp_path, size):
    nodes = [
        node("title", controller.TITLE, [40, 1500, 1000, 1700], size, clickable=False),
        node("author", "作者小狮日记", [40, 1700, 1000, 1800], size, clickable=False),
        node("like", "22", [447, 2163, 665, 2295], size),
        node("favorite", "收藏", [665, 2163, 890, 2295], size),
    ]
    runtime = runtime_fixture(monkeypatch, tmp_path, size, nodes, active=True)
    snapshot = runtime.observe()
    observation = snapshot["state"]["observation"]
    assert observation["kind"] == "detail"
    assert observation["liked"] is True and observation["collected"] is False
    action = runtime.actions["CLICK:like"]
    assert action["bounds"] == scaled([447, 2163, 665, 2295], size)
    assert action["center"] == [
        (action["bounds"][0] + action["bounds"][2]) // 2,
        (action["bounds"][1] + action["bounds"][3]) // 2,
    ]
    assert snapshot["state"]["actionDescriptors"]["CLICK:like"]["role"] == "like"


@pytest.mark.parametrize("size", SIZES)
def test_input_action_filter_scales_header_threshold(monkeypatch, tmp_path, size):
    nodes = [
        node("field", "小狮日记", [180, 240, 740, 290], size, cls="EditText"),
        node("submit", "搜索", [864, 240, 1032, 290], size),
        node("suggestion", "小狮日记别的", [180, 600, 740, 730], size),
    ]
    runtime = runtime_fixture(monkeypatch, tmp_path, size, nodes)
    snapshot = runtime.observe()
    assert snapshot["state"]["observation"]["kind"] == "search_input"
    assert "TYPE:field" in runtime.actions
    assert "CLICK:submit" in runtime.actions
    assert "CLICK:suggestion" not in runtime.actions


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("missing", ["author", "title"])
def test_screen_scaling_does_not_weaken_post_identity(monkeypatch, tmp_path, size, missing):
    nodes = [
        node(
            "title",
            controller.TITLE if missing != "title" else "其它帖子",
            [40, 1500, 1000, 1700],
            size,
            clickable=False,
        ),
        node(
            "author",
            "作者小狮日记" if missing != "author" else "作者其他人",
            [40, 1700, 1000, 1800],
            size,
            clickable=False,
        ),
        node("like", "22", [447, 2163, 665, 2295], size),
        node("favorite", "收藏", [665, 2163, 890, 2295], size),
    ]
    runtime = runtime_fixture(monkeypatch, tmp_path, size, nodes, active=True)
    observation = runtime.observe()["state"]["observation"]
    assert observation["kind"] != "detail"
    assert observation["liked"] is None and observation["collected"] is None


@pytest.mark.parametrize("package", ["com.sec.android.app.launcher", "com.miui.home"])
def test_launcher_reset_accepts_only_supported_focused_launcher(monkeypatch, package):
    import own_mcp

    state = f"--- window:1 type:APPLICATION pkg:{package} title:Home layer:0 focused:true ---\n"
    calls = []

    class FakeMcp:
        def call(self, name, args):
            calls.append(name)
            return {"content": [{"type": "text", "text": state}]}

        def close(self):
            calls.append("close")

    monkeypatch.setattr(own_mcp, "OwnMcp", FakeMcp)
    monkeypatch.setattr(run_pi_trial.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(run_pi_trial.time, "sleep", lambda *a: None)
    assert run_pi_trial.reset_phone("offline-fixture") >= 0
    assert calls[-1] == "close"


@pytest.mark.parametrize(
    "state",
    [
        "--- window:1 type:APPLICATION pkg:com.unknown.home focused:true ---",
        "--- window:1 type:APPLICATION pkg:com.sec.android.app.launcher.evil focused:true ---",
        "--- window:1 type:APPLICATION pkg:com.miui.home focused:false ---\n--- window:2 type:SYSTEM pkg:com.android.systemui title:Keyguard focused:true ---",
        "--- window:1 type:APPLICATION pkg:com.sec.android.app.launcher focused:false ---\n--- window:2 type:SYSTEM pkg:com.android.systemui title:Keyguard focused:true ---",
        "some node text pkg:com.sec.android.app.launcher",
    ],
)
def test_launcher_reset_rejects_unknown_locked_background_or_unverified_state(monkeypatch, state):
    import own_mcp

    class FakeMcp:
        def call(self, *_args):
            return {"content": [{"type": "text", "text": state}]}

        def close(self):
            pass

    monkeypatch.setattr(own_mcp, "OwnMcp", FakeMcp)
    monkeypatch.setattr(run_pi_trial.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(run_pi_trial.time, "sleep", lambda *a: None)
    with pytest.raises(RuntimeError, match="launcher not verified"):
        run_pi_trial.reset_phone("offline-fixture")


@pytest.mark.parametrize(
    "text", ["", "screen:0x3200", "screen:3200x1440", "screen:1440x3200\nscreen:1080x2340"]
)
def test_missing_ambiguous_or_nonportrait_dimensions_are_rejected(text):
    with pytest.raises(RuntimeError, match="screen dimensions"):
        controller.screen_dimensions(text)


@pytest.mark.parametrize("size", SIZES)
def test_image_verifier_maps_physical_bounds_to_resized_mcp_image(size):
    image, expected_roi = screenshot(size, active=True)
    evidence = controller.icon_state(
        image, scaled([447, 2163, 665, 2295], size), "like", screen_size=size
    )
    assert evidence["active"] is True
    assert evidence["roi"] == expected_roi


def test_legacy_icon_verifier_default_geometry_is_preserved():
    image, roi = screenshot((1080, 2340), active=True)
    evidence = controller.icon_state(image, [447, 2163, 665, 2295], "like")
    assert evidence["active"] is True
    assert evidence["roi"] == roi


def test_final_stage_does_not_finish_with_unknown_or_active_toggle():
    progress = controller.Progress()
    progress.phase = "uncollect"
    for liked, collected in [(False, None), (None, False), (True, False), (False, True)]:
        progress.update({"kind": "detail", "liked": liked, "collected": collected})
        assert progress.phase == "uncollect"
    progress.update({"kind": "detail", "liked": False, "collected": False})
    assert progress.phase == "done"


@pytest.mark.parametrize("success_at", [1, 2, 3])
def test_reset_waits_bounded_observations_for_home_transition_without_repeating_home(
    monkeypatch, success_at
):
    import own_mcp

    launcher = "--- window:5272 type:APPLICATION pkg:com.miui.home title:系统桌面 activity:com.miui.home.launcher.Launcher layer:0 focused:true ---"
    transient = "--- window:1 type:APPLICATION pkg:com.xingin.xhs focused:true ---"
    states = iter([transient] * (success_at - 1) + [launcher])
    calls, sleeps = [], []

    class FakeMcp:
        def call(self, name, arguments):
            calls.append((name, arguments))
            if name == "android_get_screen_state":
                return {"content": [{"type": "text", "text": next(states)}]}
            return {}

        def close(self):
            calls.append(("close", {}))

    monkeypatch.setattr(own_mcp, "OwnMcp", FakeMcp)
    monkeypatch.setattr(run_pi_trial.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(run_pi_trial.time, "sleep", sleeps.append)
    assert run_pi_trial.reset_phone("offline-fixture") >= 0
    assert [name for name, _ in calls] == ["android_press_key"] + [
        "android_get_screen_state"
    ] * success_at + ["close"]
    assert calls[0][1] == {"key": "HOME"}
    assert sleeps == [0.3] * success_at


@pytest.mark.parametrize("package", ["com.android.systemui", "com.unknown.home"])
def test_reset_exhausts_three_observations_and_closes_session_without_accepting_lock_or_unknown(
    monkeypatch, package
):
    import own_mcp

    state = f"--- window:1 type:SYSTEM pkg:{package} title:Keyguard focused:true ---"
    calls, sleeps = [], []

    class FakeMcp:
        def call(self, name, arguments):
            calls.append(name)
            return {"content": [{"type": "text", "text": state}]}

        def close(self):
            calls.append("close")

    monkeypatch.setattr(own_mcp, "OwnMcp", FakeMcp)
    monkeypatch.setattr(run_pi_trial.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(run_pi_trial.time, "sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="launcher not verified"):
        run_pi_trial.reset_phone("offline-fixture")
    assert calls == ["android_press_key"] + ["android_get_screen_state"] * 3 + ["close"]
    assert sleeps == [0.3] * 3


def toolbar_control(key, label, bounds):
    return {
        "id": key,
        "label": label,
        "bounds": bounds,
        "method": "click",
        "role": "navigation",
        "center": [(bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2],
    }


@pytest.mark.parametrize(
    "size,like_bounds,favorite_bounds",
    [
        ((1080, 2340), [447, 2163, 665, 2295], [665, 2163, 890, 2295]),
        ((1440, 3200), [699, 2990, 956, 3144], [956, 2990, 1218, 3144]),
    ],
)
def test_like_is_unique_numeric_control_immediately_left_of_verified_favorite(
    size, like_bounds, favorite_bounds
):
    like = toolbar_control("like", "1547", like_bounds)
    favorite = toolbar_control("favorite", "收藏176", favorite_bounds)
    input_control = toolbar_control(
        "input", "说点什么...", [0, like_bounds[1], like_bounds[0], like_bounds[3]]
    )
    comment = toolbar_control(
        "comment", "评论 16", [favorite_bounds[2], favorite_bounds[1], size[0], favorite_bounds[3]]
    )
    assert (
        controller.identify_toolbar_like([input_control, like, favorite, comment], favorite, size)
        == like
    )


@pytest.mark.parametrize("label", ["1547", "1.5万", "点赞", "已点赞", "点赞 1547"])
def test_toolbar_like_accepts_only_numeric_or_like_semantics(label):
    like = toolbar_control("like", label, [699, 2990, 956, 3144])
    favorite = toolbar_control("favorite", "收藏176", [956, 2990, 1218, 3144])
    assert controller.identify_toolbar_like([like, favorite], favorite, (1440, 3200)) == like


@pytest.mark.parametrize(
    "label", ["评论 16", "说点什么...", "分享", "收藏 176", "关注", "1547条评论"]
)
def test_adjacent_toolbar_control_without_like_semantics_is_rejected(label):
    wrong = toolbar_control("wrong", label, [699, 2990, 956, 3144])
    favorite = toolbar_control("favorite", "收藏176", [956, 2990, 1218, 3144])
    assert controller.identify_toolbar_like([wrong, favorite], favorite, (1440, 3200)) is None


def test_like_ambiguity_is_rejected_instead_of_guessing_nearest_candidate():
    first = toolbar_control("a", "1547", [699, 2990, 956, 3144])
    second = toolbar_control("b", "点赞", [699, 2990, 956, 3144])
    favorite = toolbar_control("favorite", "收藏176", [956, 2990, 1218, 3144])
    assert (
        controller.identify_toolbar_like([first, second, favorite], favorite, (1440, 3200)) is None
    )


@pytest.mark.parametrize(
    "bounds", [[699, 2500, 956, 2654], [300, 2990, 550, 3144], [1218, 2990, 1440, 3144]]
)
def test_numeric_control_on_different_row_or_not_adjacent_left_is_rejected(bounds):
    wrong = toolbar_control("wrong", "1547", bounds)
    favorite = toolbar_control("favorite", "收藏176", [956, 2990, 1218, 3144])
    assert controller.identify_toolbar_like([wrong, favorite], favorite, (1440, 3200)) is None


def test_redmi_real_toolbar_bounds_pass_complete_observation_verification(monkeypatch, tmp_path):
    size = (1440, 3200)
    nodes = [
        node("title", controller.TITLE, [40, 1500, 1000, 1700], size, clickable=False),
        node("author", "作者小狮日记", [40, 1700, 1000, 1800], size, clickable=False),
    ]
    for key, label, bounds in [
        ("like", "1547", [699, 2990, 956, 3144]),
        ("favorite", "收藏176", [956, 2990, 1218, 3144]),
        ("comment", "评论 16", [1218, 2990, 1440, 3144]),
        ("input", "说点什么...", [0, 2990, 699, 3144]),
    ]:
        n = node(key, label, [1, 1, 2, 2], size)
        n["bounds"] = bounds
        nodes.append(n)
    runtime = runtime_fixture(monkeypatch, tmp_path, size, nodes)
    observation = runtime.observe()["state"]["observation"]
    assert observation["kind"] == "detail"
    assert runtime.actions["CLICK:like"]["bounds"] == [699, 2990, 956, 3144]
    assert runtime.actions["CLICK:like"]["role"] == "like"
    assert "CLICK:comment" not in runtime.actions
    assert "CLICK:input" not in runtime.actions


@pytest.mark.parametrize(
    "header",
    [
        "--- window:5272 type:APPLICATION pkg:com.miui.home title:系统桌面 layer:0 focused:false ---\n--- window:2 type:SYSTEM pkg:com.android.systemui layer:1 focused:false ---\n",
        "--- window:2 type:SYSTEM pkg:com.android.systemui title:ControlCenter layer:1 focused:true ---\n",
        "--- window:1 type:APPLICATION pkg:com.other.app focused:true ---\n",
        "",
    ],
)
def test_nonfocused_launcher_system_only_and_other_apps_are_transition_without_open_app(
    monkeypatch, tmp_path, header
):
    runtime = runtime_fixture(monkeypatch, tmp_path, (1440, 3200), [], window_header=header)
    snapshot = runtime.observe()
    assert snapshot["state"]["observation"]["kind"] == "transition"
    assert set(runtime.actions) == {"WAIT", "BLOCKED"}
    assert set(snapshot["options"]) == {"WAIT", "BLOCKED"}
    assert snapshot["state"]["stage"] == "search"


@pytest.mark.parametrize("package", ["com.sec.android.app.launcher", "com.miui.home"])
def test_only_explicitly_focused_supported_launcher_offers_open_app(monkeypatch, tmp_path, package):
    header = f"--- window:1 type:APPLICATION pkg:{package} focused:true ---\n"
    runtime = runtime_fixture(monkeypatch, tmp_path, (1440, 3200), [], window_header=header)
    snapshot = runtime.observe()
    assert snapshot["state"]["observation"]["kind"] == "launcher"
    assert set(runtime.actions) == {"WAIT", "BLOCKED", "OPEN_APP"}


def test_control_center_over_background_xhs_nodes_cannot_offer_background_actions(
    monkeypatch, tmp_path
):
    nodes = [node("search", "搜索", [864, 114, 1032, 246], (1440, 3200))]
    header = "--- window:1 type:APPLICATION pkg:com.xingin.xhs focused:false ---\n--- window:2 type:SYSTEM pkg:com.android.systemui title:ControlCenter focused:true ---\n"
    runtime = runtime_fixture(monkeypatch, tmp_path, (1440, 3200), nodes, window_header=header)
    snapshot = runtime.observe()
    assert snapshot["state"]["observation"]["kind"] == "transition"
    assert set(runtime.actions) == {"WAIT", "BLOCKED"}
    assert snapshot["state"]["visibleControls"] == []


def test_conflicting_focused_windows_are_transition_even_when_xhs_nodes_are_present(
    monkeypatch, tmp_path
):
    nodes = [node("search", "搜索", [864, 114, 1032, 246], (1440, 3200))]
    header = "--- window:1 type:APPLICATION pkg:com.xingin.xhs focused:true ---\n--- window:2 type:SYSTEM pkg:com.android.systemui focused:true ---\n"
    runtime = runtime_fixture(monkeypatch, tmp_path, (1440, 3200), nodes, window_header=header)
    assert runtime.observe()["state"]["observation"]["kind"] == "transition"
    assert set(runtime.actions) == {"WAIT", "BLOCKED"}
