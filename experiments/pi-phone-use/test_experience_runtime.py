"""Offline safety tests for goal-driven, model-selected XHS experience tasks."""

import base64
import io
from copy import deepcopy

from PIL import Image, ImageDraw
import pytest

from own_mcp import McpError
from experience_runtime import ExperienceRuntime, ExperienceRuntimeError, parse_experience_task

PACKAGE = "com.xingin.xhs"
TITLE = "任意新标题"


def task(**changes):
    value = {
        "kind": "experience",
        "goal": "搜索参数并收藏首帖",
        "appName": "小红书",
        "query": "动态搜索词",
        "target": {"kind": "first"},
        "desired": {"collected": True},
    }
    value.update(changes)
    return value


def node(
    identifier,
    label="",
    *,
    description="",
    klass="TextView",
    bounds=(100, 100, 250, 200),
    flags="on,ena,clk",
    resource="",
):
    return "\t".join(
        (
            "node_" + identifier,
            klass,
            label or "-",
            description or "-",
            resource or "-",
            ",".join(map(str, bounds)),
            flags,
        )
    )


def screen(*nodes, package=PACKAGE, liked=False, collected=False):
    raw = f"screen:1080x2340\n--- window:1 pkg:{package} focused:true ---\n" + "\n".join(nodes)
    image = Image.new("RGB", (1080, 2340), "white")
    draw = ImageDraw.Draw(image)
    if liked:
        draw.rectangle((570, 2140, 630, 2250), fill=(240, 0, 0))
    if collected:
        draw.rectangle((770, 2140, 830, 2250), fill=(240, 180, 0))
    data = io.BytesIO()
    image.save(data, "PNG")
    return {
        "content": [
            {"type": "text", "text": raw},
            {
                "type": "image",
                "data": base64.b64encode(data.getvalue()).decode(),
                "mimeType": "image/png",
            },
        ]
    }


def results(query="动态搜索词", titles=(TITLE, "第二篇"), author=None):
    nodes = [
        node("1", query, bounds=(160, 100, 800, 220)),
        node("2", "综合", bounds=(40, 350, 140, 410)),
    ]
    for index, title in enumerate(titles):
        left = 20 + index * 530
        nodes += [
            node(f"{index + 10:x}", klass="ViewGroup", bounds=(left, 500, left + 500, 1200)),
            node(
                f"{index + 20:x}", title, flags="on,ena", bounds=(left + 10, 1000, left + 480, 1090)
            ),
        ]
        if author:
            nodes += [
                node(
                    f"{index + 30:x}",
                    author,
                    flags="on,ena",
                    bounds=(left + 10, 1100, left + 480, 1150),
                )
            ]
    return screen(*nodes)


def detail(*, liked=False, collected=False, title=TITLE, author="任意作者", missing_image=False):
    value = screen(
        node(
            "1",
            title + " #任意话题",
            flags="on,ena",
            resource=PACKAGE + ":id/noteContentText",
            bounds=(10, 1800, 1000, 2000),
        ),
        node("2", description="作者" + author, bounds=(10, 1650, 400, 1770)),
        node("3", "31", klass="LinearLayout", bounds=(550, 2100, 750, 2300)),
        node("4", description="收藏12", klass="Button", bounds=(750, 2100, 950, 2300)),
        liked=liked,
        collected=collected,
    )
    if missing_image:
        value["content"] = value["content"][:1]
    return value


class FakeMcp:
    def __init__(self, states, error=None):
        self.states = iter(states)
        self.calls = []
        self.server_info = {"name": "offline-own-mcp"}
        self.error = error
        self.closed = False

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "android_get_screen_state":
            return deepcopy(next(self.states))
        if self.error:
            raise self.error
        return {"content": [{"type": "text", "text": "OK"}]}

    def close(self):
        self.closed = True


def runtime(tmp_path, monkeypatch, states, spec=None, error=None, *, search_submitted=True):
    mcp = FakeMcp(states, error)
    monkeypatch.setattr("experience_runtime.OwnMcp", lambda: mcp)
    monkeypatch.setattr("experience_runtime.time.sleep", lambda _: None)
    phone = ExperienceRuntime("offline", spec or task(), base=tmp_path)
    # Most cases isolate post-search safety from navigation. Separate integration
    # tests below drive the required real observe/act search-submission protocol.
    phone._query_submitted = search_submitted
    return phone, mcp


def choose(phone, role):
    return next(key for key, value in phone.actions.items() if value["role"] == role)


def bind(phone):
    phone.observe()
    return phone.act(choose(phone, "open_target"))


def test_schema_is_parameterized_and_does_not_restore_by_default():
    parsed = parse_experience_task(
        task(query="另一个词", target={"kind": "title", "title": "另一个标题", "author": "作者甲"})
    )
    assert parsed["query"] == "另一个词"
    assert parsed["restoreAfter"] is False
    assert parsed["desired"] == {"collected": True}


@pytest.mark.parametrize(
    "change",
    [
        {"desired": {}},
        {"desired": {"liked": 1}},
        {"restoreAfter": 1},
        {"restoreAfter": True, "desired": {"liked": False}},
        {"target": {"kind": "title", "title": ""}},
        {"target": {"kind": "last"}},
        {"appName": "微信"},
        {"query": ""},
    ],
)
def test_invalid_scope_rejected(change):
    with pytest.raises(ValueError):
        parse_experience_task(task(**change))


def test_first_binds_actual_result_and_only_collect_never_exposes_like(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [
            results(),
            results(),
            detail(liked=True),
            detail(liked=True),
            detail(liked=True, collected=True),
        ],
    )
    observed = bind(phone)
    assert observed["state"]["boundTarget"]["title"] == TITLE
    assert not observed["done"]
    assert [
        value["role"] for value in phone.actions.values() if value["role"] in {"like", "collect"}
    ] == ["collect"]
    done = phone.act(choose(phone, "collect"))
    assert done["done"]
    assert done["state"]["observation"]["liked"] is True
    assert not any(name == "android_press_key" for name, _ in mcp.calls)
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 2
    assert phone.finish()["status"] == "success"
    assert mcp.closed


def test_already_collected_finishes_without_toggle(tmp_path, monkeypatch):
    phone, mcp = runtime(tmp_path, monkeypatch, [results(), results(), detail(collected=True)])
    assert bind(phone)["done"]
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 1


def test_explicit_title_author_must_match_same_result_card(tmp_path, monkeypatch):
    spec = task(target={"kind": "title", "title": TITLE, "author": "正确作者"})
    phone, _ = runtime(tmp_path, monkeypatch, [results(author="错误作者")], spec)
    phone.observe()
    assert not any(value["role"] == "open_target" for value in phone.actions.values())


def test_title_is_exact_not_substring_and_first_is_not_hardcoded(tmp_path, monkeypatch):
    phone, _ = runtime(tmp_path, monkeypatch, [results(titles=("新首帖", "新次帖"))])
    phone.observe()
    assert phone.actions[choose(phone, "open_target")]["targetTitle"] == "新首帖"


def test_restore_after_cancels_true_desired_even_when_initially_active(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [results(), results(), detail(collected=True), detail(collected=True), detail()],
        task(restoreAfter=True),
    )
    observed = bind(phone)
    assert observed["state"]["stage"] == "cancel"
    assert observed["state"]["initialState"]["collected"] is True
    assert phone.act(choose(phone, "collect"))["done"]
    assert [item["stage"] for item in phone.progress.milestones] == ["desired", "cancel"]
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 2


def test_true_then_cancel_has_separate_paired_evidence(tmp_path, monkeypatch):
    phone, _ = runtime(
        tmp_path,
        monkeypatch,
        [
            results(),
            results(),
            detail(),
            detail(),
            detail(collected=True),
            detail(collected=True),
            detail(),
        ],
        task(restoreAfter=True),
    )
    bind(phone)
    assert phone.act(choose(phone, "collect"))["state"]["stage"] == "cancel"
    assert phone.act(choose(phone, "collect"))["done"]
    pairs = [item for item in phone.operations if item["name"] == "toggle_verification"]
    assert [(item["before"], item["after"]) for item in pairs] == [(False, True), (True, False)]


def test_fresh_changed_state_never_replays_stale_toggle(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path, monkeypatch, [results(), results(), detail(), detail(collected=True)]
    )
    bind(phone)
    assert phone.act(choose(phone, "collect"))["done"]
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 1


def test_ambiguous_delivery_reads_back_and_stops_even_if_goal_now_true(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path, monkeypatch, [results(), results(), detail(), detail(), detail(collected=True)]
    )
    bind(phone)
    mcp.error = McpError("MCP request failed", stage="tool_call", exception_type="ReadTimeout")
    with pytest.raises(ExperienceRuntimeError) as caught:
        phone.act(choose(phone, "collect"))
    assert caught.value.code == "DELIVERY_UNCERTAIN"
    assert phone.finish()["status"] == "failed"
    assert mcp.calls[-1][0] == "android_get_screen_state"


def test_missing_screenshot_cannot_authorize_toggle(tmp_path, monkeypatch):
    phone, _ = runtime(tmp_path, monkeypatch, [results(), results(), detail(missing_image=True)])
    observed = bind(phone)
    assert not observed["done"]
    assert not any(value["role"] in {"like", "collect"} for value in phone.actions.values())


def test_current_detail_search_navigation_does_not_force_home(tmp_path, monkeypatch):
    phone, mcp = runtime(tmp_path, monkeypatch, [detail(), detail(), screen(node("a", "搜索"))])
    observed = phone.observe()
    assert not observed["done"]
    phone.act(choose(phone, "back"))
    assert ("android_press_back", {}) in mcp.calls
    assert not any(
        name == "android_press_key" and args.get("key") == "HOME" for name, args in mcp.calls
    )


def test_locked_screen_has_no_mutations(tmp_path, monkeypatch):
    locked = screen(
        node("a", resource="com.android.systemui:id/keyguard_root_view"),
        package="com.android.systemui",
    )
    phone, mcp = runtime(tmp_path, monkeypatch, [locked])
    with pytest.raises(ExperienceRuntimeError) as caught:
        phone.observe()
    assert caught.value.code == "LOCKED"
    assert [name for name, _ in mcp.calls] == ["android_get_screen_state"]


def test_wait_is_bounded_without_dispatching_mutations(tmp_path, monkeypatch):
    empty = screen()
    phone, mcp = runtime(tmp_path, monkeypatch, [empty] * 9)
    phone.observe()
    with pytest.raises(ExperienceRuntimeError) as caught:
        for _ in range(4):
            phone.act("WAIT")
    assert caught.value.code == "NO_PROGRESS"
    assert all(name == "android_get_screen_state" for name, _ in mcp.calls)


def test_explicit_title_exact_match_rejects_longer_title(tmp_path, monkeypatch):
    phone, _ = runtime(
        tmp_path,
        monkeypatch,
        [results(titles=(TITLE + "附加内容", "另一篇"))],
        task(target={"kind": "title", "title": TITLE}),
    )
    phone.observe()
    assert not any(value["role"] == "open_target" for value in phone.actions.values())


def test_both_flags_follow_goals_and_finish_without_implicit_cancellation(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [
            results(),
            results(),
            detail(),
            detail(),
            detail(liked=True),
            detail(liked=True),
            detail(liked=True, collected=True),
        ],
        task(desired={"liked": True, "collected": True}),
    )
    bind(phone)
    assert phone.act(choose(phone, "like"))["state"]["stage"] == "desired"
    assert phone.act(choose(phone, "collect"))["done"]
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 3
    assert [item["stage"] for item in phone.progress.milestones] == ["desired"]


def test_explicit_false_goal_is_supported_without_positive_toggle(tmp_path, monkeypatch):
    phone, _ = runtime(
        tmp_path,
        monkeypatch,
        [
            results(),
            results(),
            detail(liked=True, collected=True),
            detail(liked=True, collected=True),
            detail(collected=True),
        ],
        task(desired={"liked": False}),
    )
    bind(phone)
    assert phone.act(choose(phone, "like"))["done"]


def test_untouched_flag_changed_externally_stops(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path, monkeypatch, [results(), results(), detail(), detail(liked=True)]
    )
    bind(phone)
    with pytest.raises(ExperienceRuntimeError) as caught:
        phone.act(choose(phone, "collect"))
    assert caught.value.code == "TARGET_CHANGED"
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 1


def test_toggle_ack_without_changed_state_stops_without_retry(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [results(), results(), detail(), detail(), detail(), detail(), detail()],
    )
    bind(phone)
    with pytest.raises(ExperienceRuntimeError) as caught:
        phone.act(choose(phone, "collect"))
    assert caught.value.code == "TOGGLE_UNVERIFIED"
    with pytest.raises(ExperienceRuntimeError):
        phone.act("WAIT")
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 2


def test_pre_dispatch_missing_node_reobserves_without_automatic_retry(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path, monkeypatch, [results(), results(), detail(), detail(), detail()]
    )
    bind(phone)
    mcp.error = McpError(
        "Node not found in accessibility tree", stage="tool_call", exception_type="ToolError"
    )
    observed = phone.act(choose(phone, "collect"))
    assert observed["execution"] == {"status": "not_dispatched", "reason": "stale_reference"}
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 2
    assert not phone.pending_toggle


def test_stale_first_result_does_not_bind_new_first_without_model(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path, monkeypatch, [results(), results(titles=("替换后的新首帖", "第二篇"))]
    )
    phone.observe()
    observed = phone.act(choose(phone, "open_target"))
    assert observed["execution"]["status"] == "not_dispatched"
    assert phone.bound_target is None
    assert all(name == "android_get_screen_state" for name, _ in mcp.calls)


def test_search_input_uses_dynamic_parameter_and_relocates_after_clear(tmp_path, monkeypatch):
    def input_screen(value, identifier="a"):
        return screen(
            node(
                identifier,
                value,
                klass="EditText",
                bounds=(160, 100, 800, 220),
                flags="on,ena,clk,edt",
            )
        )

    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [
            input_screen("旧词"),
            input_screen("旧词"),
            input_screen("", identifier="b"),
            input_screen("动态搜索词", identifier="c"),
        ],
    )
    phone.observe()
    observed = phone.act(choose(phone, "search_field"))
    assert observed["state"]["observation"]["queryMatches"]
    assert (
        "android_type_append_text",
        {"node_id": "node_b", "text": "动态搜索词", "typing_speed": 10, "typing_speed_variance": 0},
    ) in mcp.calls


def test_exact_target_author_is_bound_after_detail_and_rechecked(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path, monkeypatch, [results(), results(), detail(author="甲"), detail(author="乙")]
    )
    bind(phone)
    observed = phone.act(choose(phone, "collect"))
    assert not observed["done"]
    assert not any(value["role"] in {"like", "collect"} for value in phone.actions.values())
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 1


def test_runtime_time_budget_prevents_mutation(tmp_path, monkeypatch):
    phone, mcp = runtime(tmp_path, monkeypatch, [screen(node("a", "搜索"))])
    phone.observe()
    phone.started -= 181
    with pytest.raises(ExperienceRuntimeError) as caught:
        phone.act(choose(phone, "search_entry"))
    assert caught.value.code == "BUDGET"
    assert all(name == "android_get_screen_state" for name, _ in mcp.calls)


def test_real_1440_result_geometry_includes_carousel_first_not_profile_or_masonry():
    from experience_runtime import result_cards, _title_visible
    from own_mcp import own_nodes

    raw = "\n".join(
        (
            "screen:1440x3200",
            "--- window:1 pkg:com.xingin.xhs focused:true ---",
            node("a", klass="LinearLayout", bounds=(18, 646, 1422, 926)),
            node("b", "小狮日记", bounds=(291, 708, 580, 793), flags="on,ena"),
            node("c", klass="ViewGroup", bounds=(53, 926, 488, 1672)),
            node("d", "昨天 12:38", bounds=(83, 1435, 320, 1475), flags="on,ena"),
            node("e", "1674", bounds=(334, 1435, 458, 1475), flags="on,ena"),
            node(
                "f", "当我闯进一个限时48小时的创造营", bounds=(62, 1524, 479, 1670), flags="on,ena"
            ),
            node("10", klass="ViewGroup", bounds=(502, 926, 936, 1672)),
            node("11", "小狮日记招聘！", bounds=(511, 1524, 927, 1595), flags="on,ena"),
            node("12", klass="FrameLayout", bounds=(17, 1724, 712, 2994)),
            node("13", "后面的瀑布流帖子", bounds=(45, 2678, 684, 2834), flags="on,ena"),
        )
    )
    assert [card["targetTitle"] for card in result_cards(own_nodes(raw), (1440, 3200))][:2] == [
        "当我闯进一个限时48小时的创造营",
        "小狮日记招聘！",
    ]
    caption = own_nodes(
        "screen:1440x3200\n--- window:1 pkg:com.xingin.xhs focused:true ---\n"
        + node(
            "a",
            description="任意标题 #话题 展开",
            klass="FrameLayout",
            resource="",
            bounds=(0, 2766, 1440, 2948),
        )
    )
    assert _title_visible(caption, "任意标题")
    assert not _title_visible(caption, "任意")


def test_first_unreadable_post_does_not_fall_through_to_second(tmp_path, monkeypatch):
    phone, _ = runtime(tmp_path, monkeypatch, [results(titles=("1234", "有标题的第二篇"))])
    phone.observe()
    assert not any(value["role"] == "open_target" for value in phone.actions.values())


def test_first_requires_this_run_search_submit_and_supports_retained_results(tmp_path, monkeypatch):
    input_screen = screen(
        node(
            "a", "动态搜索词", klass="EditText", bounds=(160, 100, 800, 220), flags="on,ena,clk,edt"
        )
    )
    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [
            results(),
            results(),
            input_screen,
            input_screen,
            results(),
            results(),
            detail(collected=True),
        ],
        search_submitted=False,
    )
    phone.observe()
    assert not any(action["role"] == "open_target" for action in phone.actions.values())
    phone.act(choose(phone, "search_field"))
    observed = phone.act("CLICK:experience-enter")
    assert observed["state"]["querySubmittedThisRun"] is True
    assert phone.act(choose(phone, "open_target"))["done"]
    assert ("android_press_key", {"key": "ENTER"}) in mcp.calls
    assert ("android_press_key", {"key": "HOME"}) not in mcp.calls


def test_exact_visible_title_does_not_require_forced_new_query(tmp_path, monkeypatch):
    phone, _ = runtime(
        tmp_path,
        monkeypatch,
        [results()],
        task(target={"kind": "title", "title": TITLE}),
        search_submitted=False,
    )
    phone.observe()
    assert phone.actions[choose(phone, "open_target")]["targetTitle"] == TITLE


def test_transient_toggle_frame_gets_read_only_settling_then_paired_success(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [
            results(),
            results(),
            detail(),
            detail(),
            detail(),
            detail(collected=True),
        ],
    )
    bind(phone)
    assert phone.act(choose(phone, "collect"))["done"]
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 2
    assert len([op for op in phone.operations if op["name"] == "toggle_settle"]) == 1
    assert len([op for op in phone.operations if op["name"] == "toggle_verification"]) == 1


def test_missing_image_during_post_toggle_settles_without_redispatch(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [
            results(),
            results(),
            detail(),
            detail(),
            detail(missing_image=True),
            detail(collected=True),
        ],
    )
    bind(phone)
    assert phone.act(choose(phone, "collect"))["done"]
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 2


def unfocused(value):
    copy = deepcopy(value)
    copy["content"][0]["text"] = copy["content"][0]["text"].replace("focused:true", "focused:false")
    return copy


def test_after_open_transient_focus_uses_read_only_reobserve_and_preserves_frames(
    tmp_path, monkeypatch
):
    launcher = screen(package="com.miui.home")
    phone, mcp = runtime(
        tmp_path, monkeypatch, [launcher, launcher, unfocused(screen()), screen(node("a", "搜索"))]
    )
    waits = []
    monkeypatch.setattr("experience_runtime.time.sleep", waits.append)
    phone.observe()
    observed = phone.act("OPEN_APP:" + PACKAGE)
    assert observed["state"]["focusedPackage"] == PACKAGE
    assert [name for name, _ in mcp.calls].count("android_open_app") == 1
    assert all(name in {"android_get_screen_state", "android_open_app"} for name, _ in mcp.calls)
    assert phone.index == 4
    assert all((phone.directory / f"observation-{index}.txt").exists() for index in range(1, 5))
    assert all((phone.directory / f"observation-{index}.jpg").exists() for index in range(1, 5))
    frames = [item for item in phone.operations if item["name"] == "focus_observation"]
    assert [(item["observation"], item["focusedPackage"]) for item in frames][-2:] == [
        (3, None),
        (4, PACKAGE),
    ]
    assert waits.count(0.25) == 2  # one post-action wait, one read-only focus wait


def test_missing_focus_exhausts_three_retries_without_using_previous_focus(tmp_path, monkeypatch):
    phone, mcp = runtime(tmp_path, monkeypatch, [screen(), *[unfocused(screen())] * 4])
    phone.observe()
    waits = []
    monkeypatch.setattr("experience_runtime.time.sleep", waits.append)
    with pytest.raises(ExperienceRuntimeError) as caught:
        phone.observe()
    assert caught.value.code == "UNKNOWN_FOCUS"
    assert len(mcp.calls) == 5
    assert waits == [0.25, 0.25, 0.25]
    assert phone.snapshot["state"]["observation"]["focusedPackage"] is None
    assert phone.finish()["status"] == "failed"


def test_focus_retry_stops_immediately_on_locked_frame(tmp_path, monkeypatch):
    locked = screen(
        node("a", resource="com.android.systemui:id/keyguard_root_view"),
        package="com.android.systemui",
    )
    phone, mcp = runtime(tmp_path, monkeypatch, [unfocused(screen()), locked, screen()])
    with pytest.raises(ExperienceRuntimeError) as caught:
        phone.observe()
    assert caught.value.code == "LOCKED"
    assert len(mcp.calls) == 2
    assert phone.index == 2


def test_focus_recovery_uses_screenshot_from_same_new_response_as_ax(tmp_path, monkeypatch):
    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [
            results(),
            results(),
            unfocused(detail(collected=True)),
            detail(collected=False),
        ],
    )
    observed = bind(phone)
    assert not observed["done"]
    assert observed["state"]["observation"]["collected"] is False
    assert choose(phone, "collect")
    assert phone.index == 4
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 1


@pytest.mark.parametrize(
    "label,expected",
    [
        ("露营省钱新手必备干货→之气罐篇 #露营装备  #露营必备", "露营省钱新手必备干货→之气罐篇"),
        (
            "露营省钱新手必备干货→之气罐篇 #露营装备[话题]# #经验分享...",
            "露营省钱新手必备干货→之气罐篇",
        ),
        ("没有话题的原始标题", "没有话题的原始标题"),
        ("", None),
        ("   ", None),
        ("#只有话题 #露营", None),
        (" #只有话题[话题]#", None),
    ],
)
def test_caption_title_normalization_is_exact_and_rejects_tag_only(label, expected):
    from experience_runtime import caption_title

    assert caption_title(label) == expected


def test_first_card_caption_and_detail_topic_formats_share_exact_title_and_keep_raw_evidence(
    tmp_path, monkeypatch
):
    title = "露营省钱新手必备干货→之气罐篇"
    raw = title + " #露营装备  #露营必备 #经验分享"
    detail_title = title + " #露营装备[话题]# #露营必备[话题]#"
    phone, mcp = runtime(
        tmp_path,
        monkeypatch,
        [
            results(titles=(raw, "第二篇")),
            results(titles=(raw, "第二篇")),
            detail(title=detail_title, collected=True),
        ],
    )
    observed = bind(phone)
    assert observed["done"]
    assert observed["state"]["boundTarget"]["title"] == title
    assert observed["state"]["boundTarget"]["rawLabel"] == raw
    assert observed["state"]["observation"]["evidence"]["targetLabels"][0].startswith(title + " #")
    assert len([name for name, _ in mcp.calls if name == "android_click_node"]) == 1


def test_specified_caption_title_is_canonicalized_without_substring_match(tmp_path, monkeypatch):
    phone, _ = runtime(
        tmp_path,
        monkeypatch,
        [results(titles=("精确标题 #卡片话题", "精确标题附加 #话题"))],
        task(target={"kind": "title", "title": "精确标题 #用户带的话题"}),
        search_submitted=False,
    )
    phone.observe()
    assert phone.actions[choose(phone, "open_target")]["targetTitle"] == "精确标题"


def test_caption_title_collision_keeps_both_candidates_and_refuses_ambiguous_specified_target(
    tmp_path, monkeypatch
):
    phone, _ = runtime(
        tmp_path,
        monkeypatch,
        [results(titles=("相同标题 #话题甲", "相同标题 #话题乙"))],
        task(target={"kind": "title", "title": "相同标题"}),
        search_submitted=False,
    )
    observed = phone.observe()
    assert [item["title"] for item in observed["state"]["observation"]["resultCandidates"]] == [
        "相同标题",
        "相同标题",
    ]
    assert not any(action["role"] == "open_target" for action in phone.actions.values())


def test_tag_only_first_card_remains_barrier_instead_of_selecting_second(tmp_path, monkeypatch):
    phone, _ = runtime(tmp_path, monkeypatch, [results(titles=("#纯话题 #露营", "第二篇正常标题"))])
    phone.observe()
    assert not any(action["role"] == "open_target" for action in phone.actions.values())


def test_retained_same_query_first_explains_research_goal_and_existing_edit_choice(
    tmp_path, monkeypatch
):
    phone, _ = runtime(tmp_path, monkeypatch, [results()], search_submitted=False)
    observed = phone.observe()
    state = observed["state"]
    for field in ("goal", "nextGoal"):
        assert "保留结果" in state[field]
        assert "打开查询输入框" in state[field]
        assert "重新提交" in state[field]
    assert "重新提交" in phone.actions[choose(phone, "search_field")]["label"]
    assert set(observed["options"]) == {"WAIT", "BLOCKED", "CLICK:node_1", "CLICK:experience-back"}


@pytest.mark.parametrize(
    "query,target",
    [
        ("其它查询", {"kind": "first"}),
        ("动态搜索词", {"kind": "title", "title": TITLE}),
    ],
)
def test_normal_query_or_exact_title_keeps_original_goal_without_research_constraint(
    tmp_path, monkeypatch, query, target
):
    spec = task(target=target)
    phone, _ = runtime(tmp_path, monkeypatch, [results(query=query)], spec, search_submitted=False)
    observed = phone.observe()
    assert observed["state"]["goal"] == spec["goal"]
    assert "保留结果" not in observed["state"]["nextGoal"]
    assert "重新提交" not in phone.actions[choose(phone, "search_field")]["label"]


# Necessary XHS-only text/geometry extracted from terminal-17682c38 AX frames
# 8/9. System notifications and unrelated posts are deliberately not fixtures.
PHOTO_TITLE="新手露营装备必备清单✅照着带就够⛺️"
PHOTO_SUMMARY="✅ 按类别收拾不遗漏： 🏕️ 住宿睡眠： 帐篷 + 防潮垫 + 睡垫 + 睡袋 + 枕头 过夜一定要准备好，晚上睡不好"
PHOTO_BODY="✅ 按类别收拾不遗漏：   🏕️ 住宿睡眠： 帐篷 + 防潮垫 + 睡垫 + 睡袋 + 枕头 过夜一定要准备好，晚上睡不好真的会崩溃🥲💤   🪑 桌椅营地： 蛋卷桌 + 克米特椅 + 月亮椅 ...truncated"


def photo_detail(title=PHOTO_TITLE,body=PHOTO_BODY,*,extra_author=False,body_top=2492):
    nodes=[
        node("a","野游家",resource=PACKAGE+":id/nickNameTV",bounds=(314,206,470,266),flags="on,ena"),
        node("b",title,bounds=(53,2387,1387,2457),flags="on,foc,ena"),
        node("c",body,bounds=(53,body_top,1387,3200),flags="on,foc,ena"),
        node("d",description="点赞 99",klass="Button",bounds=(723,2948,933,3144)),
        node("e",description="收藏 103",klass="Button",bounds=(933,2948,1171,3144)),
    ]
    if extra_author:
        nodes.append(node("f","另一作者",resource=PACKAGE+":id/nickNameTV",bounds=(500,206,670,266),flags="on,ena"))
    value=screen(*nodes)
    value["content"][0]["text"]=value["content"][0]["text"].replace("screen:1080x2340","screen:1440x3200")
    return value


def test_real_photo_title_and_adjacent_long_body_align_selected_card_then_bind_exact_title(tmp_path,monkeypatch):
    merged=PHOTO_TITLE+" "+PHOTO_SUMMARY
    phone,_=runtime(tmp_path,monkeypatch,[results(titles=(merged,"第二篇")),results(titles=(merged,"第二篇")),photo_detail()],
                    task(desired={"collected":False}))
    observed=bind(phone)
    assert observed["done"]
    assert observed["state"]["boundTarget"]["title"]==PHOTO_TITLE
    assert observed["state"]["boundTarget"]["rawLabel"]==merged
    assert observed["state"]["boundTarget"]["author"]=="野游家"
    evidence=observed["state"]["observation"]["evidence"]["photoIdentity"]
    assert evidence["titleLabel"]==PHOTO_TITLE
    assert evidence["bodyLabel"]==PHOTO_BODY
    assert evidence["source"]=="xhs-adjacent-title-body-v1"


@pytest.mark.parametrize("body,title,extra_author,body_top",[
    ("不相干的另一个长正文"*10,PHOTO_TITLE,False,2492),
    (PHOTO_BODY,PHOTO_TITLE,True,2492),
    (PHOTO_BODY,PHOTO_TITLE,False,2900),
    (PHOTO_BODY,"新手露营装备必备清单",False,2492),
])
def test_merged_photo_identity_rejects_wrong_body_author_gap_or_short_title(tmp_path,monkeypatch,body,title,extra_author,body_top):
    merged=PHOTO_TITLE+" "+PHOTO_SUMMARY
    phone,_=runtime(tmp_path,monkeypatch,[results(titles=(merged,"第二篇")),results(titles=(merged,"第二篇")),
                                        photo_detail(title,body,extra_author=extra_author,body_top=body_top)])
    observed=bind(phone)
    assert not observed["done"]
    assert not any(action["role"] in {"like","collect"} for action in phone.actions.values())


def test_specified_exact_title_can_open_merged_caption_but_requires_exact_detail_title(tmp_path,monkeypatch):
    merged=PHOTO_TITLE+" "+PHOTO_SUMMARY
    phone,_=runtime(tmp_path,monkeypatch,[results(titles=(merged,"第二篇")),results(titles=(merged,"第二篇")),photo_detail()],
                    task(target={"kind":"title","title":PHOTO_TITLE},desired={"collected":False}))
    assert bind(phone)["done"]


def test_specified_title_prefix_cannot_authorize_photo_writes_even_with_body_evidence(tmp_path,monkeypatch):
    requested="相近标题"
    actual=requested+" 其他内容"
    merged=actual+" "+PHOTO_SUMMARY
    phone,_=runtime(tmp_path,monkeypatch,[results(titles=(merged,"第二篇")),results(titles=(merged,"第二篇")),
                                        photo_detail(actual)],
                    task(target={"kind":"title","title":requested}))
    observed=bind(phone)
    assert not observed["done"]
    assert not any(action["role"] in {"like","collect"} for action in phone.actions.values())


def test_two_merged_card_matches_do_not_guess_specified_target(tmp_path,monkeypatch):
    merged=PHOTO_TITLE+" "+PHOTO_SUMMARY
    phone,_=runtime(tmp_path,monkeypatch,[results(titles=(merged,merged+"附加"))],
                    task(target={"kind":"title","title":PHOTO_TITLE}))
    phone.observe()
    assert not any(action["role"]=="open_target" for action in phone.actions.values())
