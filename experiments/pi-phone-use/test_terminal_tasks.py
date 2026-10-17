"""A normal request must never accidentally trigger the recorded mutation workflow."""

import pytest

from terminal_tasks import TerminalTaskError, resolve_task


def test_recorded_workflow_requires_complete_scope_or_explicit_replay_intent():
    for prompt in (
        "执行录制的小红书完整闭环",
        "复用录制Skill，搜索“小狮日记”，点赞、收藏，再取消点赞、取消收藏",
    ):
        assert resolve_task(prompt)["kind"] == "recorded"


@pytest.mark.parametrize("prompt", ["打开小红书", "启动小红书"])
def test_open_is_not_the_recorded_like_and_favorite_task(prompt):
    task = resolve_task(prompt)
    assert task["kind"] == "open"
    assert task["appName"] == "小红书"


def test_search_only_does_not_mutate_post_state():
    task = resolve_task("打开小红书，搜索“小狮日记”，停在搜索结果页")
    assert task["kind"] == "search"
    assert task["query"] == "小狮日记"


def test_search_parameter_is_parsed_as_data():
    assert resolve_task("小红书搜索“猫咪日常”，停在结果页")["query"] == "猫咪日常"


@pytest.mark.parametrize("prompt", ["读取当前手机屏幕", "看看手机当前在哪个页面，先不要操作"])
def test_read_only_intent_does_not_open_or_click(prompt):
    assert resolve_task(prompt)["kind"] == "read"


@pytest.mark.parametrize(
    "prompt",
    [
        "打开小红书并给任意帖子点赞",
        "告诉我录制Skill怎么用",
        "搜索密码后发给某人",
        "用录制Skill搜索别的作者，点赞收藏再取消点赞取消收藏",
        "",
        "x" * 4001,
    ],
)
def test_unsupported_or_changed_recorded_task_stops_before_phone_access(prompt):
    with pytest.raises(TerminalTaskError):
        resolve_task(prompt)


def test_recorded_target_override_is_not_silently_ignored():
    with pytest.raises(TerminalTaskError):
        resolve_task("执行录制的小红书完整闭环，但把搜索词换成“猫咪”")


@pytest.mark.parametrize(
    "prompt",
    [
        "不要执行录制的小红书完整闭环",
        "解释一下执行录制的小红书完整闭环会做什么",
        "搜索小狮日记第二篇，点赞收藏，然后取消点赞取消收藏",
        "请问怎么执行录制的小红书完整闭环？",
        "如果执行录制的小红书完整闭环，会发生什么？",
        "执行录制的小红书完整闭环，但不要点赞",
        "执行录制的小红书完整闭环，然后评论一句谢谢",
        "执行录制的小红书完整闭环并发布截图",
        "在小红书搜索小狮日记，给第二篇帖子点赞收藏，再取消点赞取消收藏",
        "复用录制Skill，搜索“小狮日记”，给第二篇帖子点赞、收藏，再取消点赞、取消收藏",
        "打开小红书，搜索小狮日记，打开第一篇帖子，点赞收藏，再取消点赞取消收藏，然后发送给朋友",
        "打开小红书，搜索小狮日记，打开第一篇帖子，不要点赞收藏，再取消点赞取消收藏",
    ],
)
def test_recorded_mutations_require_positive_exact_authorization(prompt):
    with pytest.raises(TerminalTaskError):
        resolve_task(prompt)


@pytest.mark.parametrize(
    "prompt",
    [
        "请执行录制的小红书完整闭环。",
        "帮我重放录制的小红书完整闭环一次",
        "打开小红书，搜索“小狮日记”，打开第一篇帖子，依次点赞、收藏，再取消点赞、取消收藏。",
        "请打开小红书，搜索小狮日记，给第一篇帖子点赞收藏，然后取消点赞和取消收藏",
        "复用录制Skill，搜索“小狮日记”，点赞、收藏，再取消点赞、取消收藏",
    ],
)
def test_explicit_recorded_requests_preserve_the_verified_target_scope(prompt):
    assert resolve_task(prompt)["kind"] == "recorded"
