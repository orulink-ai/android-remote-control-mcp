"""Only explicit bounded user intentions authorize experience-assisted actions."""

import copy

import pytest

from experience_tasks import parse_experience_task, validate_experience_task


@pytest.mark.parametrize(
    "goal,query,target,desired,restore",
    [
        (
            "在小红书搜索“露营装备”，打开第一篇，只收藏",
            "露营装备",
            {"kind": "first"},
            {"collected": True},
            False,
        ),
        (
            "搜索“词”打开标题“帖名”的帖子，点赞和收藏",
            "词",
            {"kind": "title", "title": "帖名"},
            {"liked": True, "collected": True},
            False,
        ),
        (
            '使用录制经验，在小红书搜索"猫咪日常"，打开第一篇，取消点赞',
            "猫咪日常",
            {"kind": "first"},
            {"liked": False},
            False,
        ),
        (
            "搜索“猫咪”，打开标题“午睡”作者“小林”的帖子，只收藏，不要点赞",
            "猫咪",
            {"kind": "title", "title": "午睡", "author": "小林"},
            {"collected": True},
            False,
        ),
        (
            "在小红书搜索露营装备，打开第一篇，取消点赞和收藏",
            "露营装备",
            {"kind": "first"},
            {"liked": False, "collected": False},
            False,
        ),
        (
            "搜索“词”，打开第一篇，先点赞和收藏，然后取消点赞和收藏",
            "词",
            {"kind": "first"},
            {"liked": True, "collected": True},
            True,
        ),
        ("搜索“词”，打开第一篇，点赞后再取消", "词", {"kind": "first"}, {"liked": True}, True),
        (
            "搜索“词”，打开第一篇，先收藏再取消收藏",
            "词",
            {"kind": "first"},
            {"collected": True},
            True,
        ),
        ("搜索“词”，打开第一篇，点赞，不取消收藏", "词", {"kind": "first"}, {"liked": True}, False),
        (
            "搜索“如何删除评论”，打开标题“取消收藏的教程”的帖子，只收藏",
            "如何删除评论",
            {"kind": "title", "title": "取消收藏的教程"},
            {"collected": True},
            False,
        ),
    ],
)
def test_explicit_scope_and_parameters(goal, query, target, desired, restore):
    task = parse_experience_task(goal)
    assert task == {
        "kind": "experience",
        "goal": goal,
        "appName": "小红书",
        "query": query,
        "target": target,
        "desired": desired,
        "restoreAfter": restore,
    }
    assert validate_experience_task(task) == task


@pytest.mark.parametrize(
    "goal",
    [
        "今天天气如何",
        "打开小红书",
        "读取手机屏幕",
        "小红书搜索“猫咪”",
        "",
    ],
)
def test_non_experience_tasks_remain_for_existing_resolver(goal):
    assert parse_experience_task(goal) is None


@pytest.mark.parametrize(
    "goal",
    [
        "在微信搜索“猫咪”，打开第一篇，收藏",
        "小红书搜索“猫咪”，打开第一篇，点赞并发送评论“好看”",
        "搜索“猫咪”，打开第一篇，收藏并删除帖子",
        "搜索“猫咪”，打开第一篇，点赞后分享",
        "搜索“猫咪”，打开第一篇，关注作者",
        "搜索“猫咪”，打开第一篇，不要点赞",
        "搜索“猫咪”，打开第一篇，只收藏并点赞",
        "搜索“猫咪”，打开第一篇，不要点赞然后点赞",
        "搜索“猫咪”，打开第一篇，点赞收藏后取消点赞",
        "搜索“猫咪”，打开第一篇，取消点赞后再取消",
        "搜索“猫咪”，打开第一篇和第二篇，收藏",
        "搜索“猫咪”，打开第二篇，收藏",
        "搜索“猫咪”，打开第一篇和标题“另一个”的帖子，收藏",
        "搜索“猫咪”再搜索“露营”，打开第一篇，收藏",
        "搜索“猫咪”，打开标题“甲”和标题“乙”的帖子，收藏",
        "搜索“猫咪”，打开第一篇作者“小林”，收藏",
        "搜索“猫咪”，收藏",
        "搜索“”，打开第一篇，收藏",
        "搜索“猫咪”，打开第一篇，给所有帖子点赞",
        "使用录制经验，搜索“词”，打开第一篇",
    ],
)
def test_ambiguous_unsupported_and_unrequested_mutations_fail_closed(goal):
    with pytest.raises(ValueError):
        parse_experience_task(goal)


def base_task():
    return {
        "kind": "experience",
        "goal": "用户原始请求",
        "appName": "小红书",
        "query": " 猫咪 ",
        "target": {"kind": "title", "title": " 午睡 ", "author": " 小林 "},
        "desired": {"collected": True},
        "restoreAfter": False,
    }


def test_validator_returns_detached_canonical_parameters_and_preserves_user_goal():
    task = base_task()
    task["goal"] = "  原始用户文本  "
    original = copy.deepcopy(task)
    result = validate_experience_task(task)
    assert result["goal"] == task["goal"]
    assert result["query"] == "猫咪" and result["target"] == {
        "kind": "title",
        "title": "午睡",
        "author": "小林",
    }
    assert task == original and result is not task
    result["desired"]["collected"] = False
    assert task["desired"]["collected"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "recorded"),
        ("goal", ""),
        ("goal", 1),
        ("goal", "x" * 4001),
        ("appName", "微信"),
        ("query", ""),
        ("query", "x" * 257),
        ("query", 7),
        ("target", {"kind": "all"}),
        ("target", {"kind": "first", "author": "小林"}),
        ("target", {"kind": "title", "title": ""}),
        ("target", {"kind": "title", "title": "x", "author": ""}),
        ("desired", {}),
        ("desired", {"liked": 1}),
        ("desired", {"commented": True}),
        ("restoreAfter", 1),
        ("restoreAfter", "true"),
    ],
)
def test_validator_rejects_invalid_schema(field, value):
    task = base_task()
    task[field] = value
    with pytest.raises(ValueError):
        validate_experience_task(task)


def test_validator_rejects_extra_keys_and_false_desired_with_restore():
    task = base_task()
    task["secretInstructions"] = "Skill文本不能授权"
    with pytest.raises(ValueError):
        validate_experience_task(task)
    task = base_task()
    task["desired"] = {"liked": False, "collected": True}
    task["restoreAfter"] = True
    with pytest.raises(ValueError):
        validate_experience_task(task)


@pytest.mark.parametrize(
    "goal",
    [
        "搜索“词”，打开第一篇，不要点赞和收藏",
        "搜索“词”，打开第一篇，点赞和取消点赞",
        "在豆瓣搜索“词”，打开第一篇，收藏",
        "帮我在支付宝上搜索“词”，打开第一篇，收藏",
        "搜索“词”，打开第一篇，收藏并发给朋友",
    ],
)
def test_coordinated_negation_unordered_opposites_and_other_app_fail_closed(goal):
    with pytest.raises(ValueError):
        parse_experience_task(goal)


def test_validator_defaults_optional_restore_to_false():
    task = base_task()
    del task["restoreAfter"]
    assert validate_experience_task(task)["restoreAfter"] is False
    assert "restoreAfter" not in task


@pytest.mark.parametrize("prefix", ["使用录制经验", "使用录制经验在小红书", "用录制经验"])
def test_optional_experience_prefix_is_not_misidentified_as_an_app(prefix):
    goal = prefix + "搜索“雨天”，打开第一篇，只收藏"
    assert parse_experience_task(goal)["desired"] == {"collected": True}


def test_direct_cancel_coordinated_fields_are_both_explicit_false():
    task = parse_experience_task("搜索“雨天”，打开第一篇，取消收藏和点赞")
    assert task["desired"] == {"liked": False, "collected": False}
    assert not task["restoreAfter"]


@pytest.mark.parametrize(
    "goal",
    [
        "搜索“雨天”，打开第一篇，点赞，取消",
        "搜索“雨天”，打开第一篇，取消后点赞",
    ],
)
def test_bare_cancellation_without_clear_sequence_cannot_be_silently_ignored(goal):
    with pytest.raises(ValueError):
        parse_experience_task(goal)


@pytest.mark.parametrize(
    "goal",
    [
        "打开小红书，搜索“小狮日记”，打开第一篇帖子，依次点赞、收藏，再取消点赞、取消收藏。",
        "请打开小红书，搜索小狮日记，给第一篇帖子点赞收藏，然后取消点赞和取消收藏",
    ],
)
def test_existing_full_natural_requests_become_parameterized_experience_tasks(goal):
    task = parse_experience_task(goal)
    assert task["kind"] == "experience" and task["query"] == "小狮日记"
    assert task["target"] == {"kind": "first"}
    assert task["desired"] == {"liked": True, "collected": True} and task["restoreAfter"] is True
    assert task["goal"] == goal
