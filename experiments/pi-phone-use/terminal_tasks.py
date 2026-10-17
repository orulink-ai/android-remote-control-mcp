"""Bounded intent routing; these decisions select a controller, never a phone action."""

from __future__ import annotations

import re

QUERY = "小狮日记"
TITLE = "当我闯进一个限时48小时的创造营"


class TerminalTaskError(ValueError):
    """Unsupported terminal intent: do not initialize or operate the phone."""


def _authorized_recorded_task(text):
    """Authorize only whole positive commands for the fixed verified scope.

    Substring/keyword matches are insufficient for a mutation workflow: they
    also match explanations, negation, another post, and appended side effects.
    Unsupported phrasing fails closed rather than broadening the recipe.
    """
    polite = r"(?:请帮我|帮我|请)?"
    ending = r"[。！!]*"
    command = r"(?:执行|运行|重放|复用)录制的小红书完整闭环(?:一次|一遍)?"
    if re.fullmatch(polite + command + ending, text):
        return True
    separator = r"[，,、；;。]*"
    query = re.escape(QUERY)
    search = rf"搜索(?:“{query}”|\"{query}\"|「{query}」|{query}){separator}"
    recorded_prefix = rf"(?:执行|运行|重放|复用)录制(?:的)?(?:Skill|skill|技能|流程){separator}"
    app_prefix = rf"(?:(?:从(?:Android|安卓|手机)?桌面)?(?:打开|启动)小红书{separator}|在小红书)"
    target = rf"(?:打开|进入|给)(?:搜索结果(?:中|里|的))?(?:第一篇|首篇)(?:帖子)?{separator}"
    connector = r"(?:然后|再|接着|随后|并|和)?"
    actions = (
        rf"(?:依次|先)?点赞{separator}{connector}收藏{separator}"
        rf"{connector}取消点赞{separator}{connector}取消收藏"
    )
    verification = rf"(?:{separator}(?:回读)?(?:验证|确认)(?:最终)?未点赞{separator}未收藏)?"
    # Naming the recorded skill selects its known first post; an ordinary full
    # task must explicitly name the first post as well as the exact search word.
    scope = rf"(?:{recorded_prefix}{search}(?:{target})?|{app_prefix}{search}{target})"
    return re.fullmatch(polite + scope + actions + verification + ending, text) is not None


def resolve_task(goal, recipe_path=None):
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 4000:
        raise TerminalTaskError("Unsupported phone task")
    original = goal.strip()
    text = re.sub(r"\s+", "", original)
    task = {"goal": original}
    if _authorized_recorded_task(text):
        return {**task, "kind": "recorded"}
    if re.search(r"录制|完整闭环", text) or all(
        action in text for action in ("点赞", "收藏", "取消点赞", "取消收藏")
    ):
        raise TerminalTaskError(
            "Use an explicit positive recorded-workflow command without changed targets or extra actions"
        )
    query_match = re.search(r'搜索\s*[“"「]([^”"」]+)[”"」]', original)
    if query_match is None:
        query_match = re.search(r"搜索\s*([^，。；\n]+)", original)
    if "小红书" in text and query_match:
        query = query_match[1].strip()
        outside = original.replace(query, "")
        if 1 <= len(query) <= 100 and not re.search(r"点赞|收藏|发送|评论|发布|删除|转账", outside):
            return {**task, "kind": "search", "appName": "小红书", "query": query}
    cleaned = re.sub(r"[，,。].*(?:不要|不做|不进行).*$", "", text)
    if re.search(r"读|查看|看看|获取", cleaned) and re.search(r"屏幕|界面|页面", cleaned):
        if not re.search(r"打开|启动|搜索|点击|点赞|收藏|输入|滑动", cleaned):
            return {**task, "kind": "read"}
    opened = re.fullmatch(r"(?:请|帮我|请帮我)?(?:打开|启动)([^，。；,\n]{1,40})[。！!]?", text)
    if opened:
        name = opened[1].strip("。！!")
        if not re.search(r"并|然后|之后|点赞|收藏|发送|搜索|点击", name):
            return {**task, "kind": "open", "appName": name}
    raise TerminalTaskError(
        "Use read-screen, open-app, Xiaohongshu search, or the explicit recorded workflow"
    )
