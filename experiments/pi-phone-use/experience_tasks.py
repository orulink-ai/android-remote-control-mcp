"""Bounded Xiaohongshu user-task parsing; recording text grants no authority.

Supported requests explicitly search one query, identify the first result or an
exact title (optionally author), and name like/collect state changes. Unspecified
fields grant no write permission. ``restoreAfter`` means explicitly cancel the
requested positive fields afterwards, regardless of their starting state.

This module consumes the current user's goal only, never a Skill's instructions.
It is intentionally not a general natural-language or arbitrary-app planner.
"""

from __future__ import annotations

import re

_KEYS = {"kind", "goal", "appName", "query", "target", "desired", "restoreAfter"}
_QUOTED = r"""(?:“([^”]*)”|"([^"]*)"|「([^」]*)」|'([^']*)')"""
_ACTION = re.compile(r"取消点赞和收藏|取消收藏和点赞|取消点赞|取消收藏|点赞|收藏")
_NEGATIVE = re.compile(
    r"(?:不要|不需要|不必|不用|无需|别|不)(?:再)?(?:取消)?(?:点赞|收藏)"
    r"(?:[和与及或、](?:取消)?(?:点赞|收藏))?"
)
_UNSUPPORTED = re.compile(
    r"评论|私信|消息|发送|发给|发到|留言|回复|删除|发布|关注|转发|分享|购买|下单|举报|屏蔽|拉黑|下载|上传|付款"
)
_OTHER_APP = re.compile(r"微信|微博|抖音|快手|淘宝|京东|哔哩|B站|YouTube|Instagram|TikTok", re.I)
_FIRST = re.compile(r"第(?:一|1)(?:篇|条|个)(?:帖子|笔记|结果)?")


def _text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError("Experience task has an invalid text parameter")
    return value.strip()


def validate_experience_task(task):
    """Return a detached canonical task, rejecting extra authority or ambiguity."""
    if not isinstance(task, dict) or set(task) not in (_KEYS, _KEYS - {"restoreAfter"}):
        raise ValueError("Invalid experience task schema")
    if task["kind"] != "experience" or task["appName"] != "小红书":
        raise ValueError("Experience tasks currently support Xiaohongshu only")
    _text(task["goal"], 4000)
    if len(task["goal"]) > 4000:
        raise ValueError("User goal exceeds the bounded task limit")
    restore = task.get("restoreAfter", False)
    target, desired = task["target"], task["desired"]
    if not isinstance(target, dict):
        raise ValueError("Invalid experience target")
    if target.get("kind") == "first" and set(target) == {"kind"}:
        canonical_target = {"kind": "first"}
    elif target.get("kind") == "title" and set(target) in (
        {"kind", "title"},
        {"kind", "title", "author"},
    ):
        canonical_target = {"kind": "title", "title": _text(target["title"], 200)}
        if "author" in target:
            canonical_target["author"] = _text(target["author"], 100)
    else:
        raise ValueError("Experience target must be first result or exact title")
    if (
        not isinstance(desired, dict)
        or not desired
        or not set(desired) <= {"liked", "collected"}
        or any(type(value) is not bool for value in desired.values())
        or type(restore) is not bool
        or (restore and not all(desired.values()))
    ):
        raise ValueError("Invalid explicit experience state changes")
    return {
        "kind": "experience",
        "goal": task["goal"],
        "appName": "小红书",
        "query": _text(task["query"], 256),
        "target": canonical_target,
        "desired": {key: desired[key] for key in ("liked", "collected") if key in desired},
        "restoreAfter": restore,
    }


def _extract(body, prefix, *, plain=False):
    pattern = prefix + r"\s*[:：]?\s*" + _QUOTED
    if plain:
        pattern += r"|搜索(?!结果|栏|框|入口)\s*[:：]?\s*([^，,。；;\n“\"「']+?)(?=打开|进入|，|,|。|；|;|\n|$)"
    matches = list(re.finditer(pattern, body))
    values = [
        next(group for group in match.groups() if group is not None).strip() for match in matches
    ]
    for match in reversed(matches):
        body = body[: match.start()] + " " * (match.end() - match.start()) + body[match.end() :]
    return values, body


def _changes(body):
    negative_roles = set()

    def negative(match):
        negative_roles.update(
            role
            for name, role in (("点赞", "liked"), ("收藏", "collected"))
            if name in match.group()
        )
        return " " * len(match.group())

    body = _NEGATIVE.sub(negative, body)
    events = []
    for match in _ACTION.finditer(body):
        label = match.group()
        names = [name for name in ("点赞", "收藏") if name in label]
        for name in names:
            events.append(
                (
                    "liked" if name == "点赞" else "collected",
                    not label.startswith("取消"),
                    match.start(),
                )
            )
    if not events:
        raise ValueError("No explicit like or collect state was authorized")
    roles = {role for role, _, _ in events}
    if roles & negative_roles:
        raise ValueError("Conflicting negative and positive instructions")
    for only, excluded in (("只收藏", "liked"), ("只点赞", "collected")):
        if only in body and excluded in roles:
            raise ValueError("Only-one-field request conflicts with another change")
    by_role = {role: [value for current, value, _ in events if current == role] for role in roles}
    generic_cancel = re.search(r"(?:之后|然后|后|再)\s*(?:再)?取消(?!点赞|收藏)", body)
    if re.search(r"取消(?!点赞|收藏)", body) and generic_cancel is None:
        raise ValueError("Cancellation must have an explicit scope and sequence")
    restoring = (
        any(len(set(values)) > 1 for values in by_role.values()) or generic_cancel is not None
    )
    if restoring:
        if not re.search(r"先|然后|之后|后|再", body):
            raise ValueError("Opposite changes need an explicit before/after order")
        # The compact schema cannot encode partially restored/mixed operations.
        explicit = all(values == [True, False] for values in by_role.values())
        generic = generic_cancel is not None and all(
            values == [True] for values in by_role.values()
        )
        if not (explicit or generic):
            raise ValueError("Restoration must cancel exactly all requested positive fields")
        if generic and generic_cancel.start() < max(position for _, _, position in events):
            raise ValueError("Cancellation must follow the requested positive operations")
        return {role: True for role in roles}, True
    if any(len(values) != 1 for values in by_role.values()):
        raise ValueError("Repeated state changes are outside the bounded task")
    return {role: values[0] for role, values in by_role.items()}, False


def parse_experience_task(goal: str) -> dict | None:
    """Recognize the bounded user syntax; return None for unrelated tasks.

    Quoted query/title/author strings are data. Extra mutations, multiple targets,
    unclear cancellation, and unsupported apps fail closed with ValueError.
    """
    if not isinstance(goal, str):
        raise ValueError("A user goal string is required")
    if not goal.strip():
        return None
    if len(goal) > 4000:
        raise ValueError("User goal exceeds the bounded task limit")
    if "搜索" not in goal:
        return None
    queries, body = _extract(goal, r"搜索(?!结果|栏|框|入口)", plain=True)
    titles, body = _extract(body, r"(?:标题(?:为|是)?|名为)")
    authors, body = _extract(body, r"作者(?:为|是)?")
    if not (
        _ACTION.search(body)
        or _UNSUPPORTED.search(body)
        or "录制经验" in body
        or "使用经验" in body
    ):
        return None
    prefix = re.sub(r"(?:使用|体验|用|通过)?录制经验", "", goal[: goal.index("搜索")])
    app = re.search(r"(?:在|用|通过|去|打开)([^，,。；;\s]{1,32}?)(?:上|里)?$", prefix)
    if app and app.group(1).casefold() not in {"小红书", "rednote", "xiaohongshu"}:
        raise ValueError("Experience tasks currently support Xiaohongshu only")
    if _OTHER_APP.search(body) or _UNSUPPORTED.search(body):
        raise ValueError("The request includes unsupported app or extra actions")
    if len(queries) != 1 or not queries[0]:
        raise ValueError("Exactly one nonempty search query is required")
    first = list(_FIRST.finditer(body))
    if re.search(
        r"第[二三四五六七八九十2-9]|所有|每一|全部帖子|随机|前[二三四五六七八九十2-9]", body
    ):
        raise ValueError("Only one explicit result target is supported")
    if len(titles) == 1 and not first and len(authors) <= 1:
        target = {"kind": "title", "title": titles[0]}
        if authors:
            target["author"] = authors[0]
    elif len(first) == 1 and not titles and not authors:
        target = {"kind": "first"}
    else:
        raise ValueError("One unambiguous first-result or exact-title target is required")
    desired, restore = _changes(body)
    return validate_experience_task(
        {
            "kind": "experience",
            "goal": goal,
            "appName": "小红书",
            "query": queries[0],
            "target": target,
            "desired": desired,
            "restoreAfter": restore,
        }
    )
