"""Short optional hints extracted from a verified demonstration, not a recipe runner.

No historical targets, node references, coordinates, action sequence or success
predicates are exposed. Current task authorization, choices and verification
remain the controller's responsibility; experience cannot expand them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from workflow import load_recipe


class ExperienceSkillError(ValueError):
    """The requested experience could not be verified; never silently disable it."""


_BOUNDARY = "示教仅供参考；页面不同可以偏离。以当前目标、当前候选和验证为准，不增加授权。"
_NAVIGATION = "先观察当前页面再定位入口，不依赖示教的位置；缺少安全候选时停止。"
_READ = "当前仅请求观察时，只读取实时状态，不因示教中有点击而扩大任务。"
_SEARCH_ENTRY = "按当前目标寻找当前页面的搜索入口，不机械重演历史返回或打开次数。"
_SEARCH_INPUT = "输入当前请求的搜索词，核对实际输入和结果状态；示教词不是本次目标。"
_RESULT = "按当前目标核对搜索结果身份；示教标题只是历史例子，不默认选择同一帖子。"
_TOGGLE = "先确认当前状态和本次目标，只在当前授权需要改变时操作，再读屏验证，避免重复切换。"


_SCREEN_KINDS = frozenset(
    {
        "launcher",
        "feed",
        "search_input",
        "search_results",
        "search_results_other",
        "detail",
        "transition",
        "screen",
        "app",
    }
)
_ROLES = frozenset({"navigation", "search_field", "like", "collect", "wait"})


@dataclass(frozen=True)
class DemonstratedTransition:
    before_kind: str
    method: str
    role: str
    after_kind: str
    example_field: str | None = None
    example_before: bool | None = None
    example_after: bool | None = None

    def context(self):
        result = {
            "beforeKind": self.before_kind,
            "method": self.method,
            "role": self.role,
            "afterKind": self.after_kind,
            "exampleOnly": True,
        }
        if self.example_field is not None:
            result["exampleStateChange"] = {
                "field": self.example_field,
                "before": self.example_before,
                "after": self.example_after,
            }
        return result


def _transition(step):
    before, after, selector = (
        step["before"]["observation"],
        step["after"]["observation"],
        step["selector"],
    )
    if (
        before["kind"] not in _SCREEN_KINDS
        or after["kind"] not in _SCREEN_KINDS
        or selector["role"] not in _ROLES
    ):
        return None
    field = {"like": "liked", "collect": "collected"}.get(selector["role"])
    known = field is not None and type(before.get(field)) is bool and type(after.get(field)) is bool
    return DemonstratedTransition(
        before["kind"],
        selector["method"],
        selector["role"],
        after["kind"],
        field if known else None,
        before[field] if known else None,
        after[field] if known else None,
    )


def _desired_roles(state):
    task = state.get("task")
    task = task if isinstance(task, dict) else {}
    desired = task.get("desired", state.get("desired"))
    fields = {"liked": "like", "collected": "collect"}
    if (
        not isinstance(desired, dict)
        or not desired
        or not set(desired) <= fields.keys()
        or any(type(value) is not bool for value in desired.values())
    ):
        return None
    return frozenset(fields[key] for key in desired)


@dataclass(frozen=True)
class ExperienceSkill:
    source_sha256: str
    recipe_sha256: str
    capabilities: frozenset[str]
    demonstrated_transitions: tuple[DemonstratedTransition, ...] = ()

    def context(self, state: dict[str, Any]) -> dict[str, Any]:
        """Return 1–2 advisory hints for the current screen, never control data."""
        observation = state.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        task = state.get("task")
        task_kind = task.get("kind") if isinstance(task, dict) else None
        phase = state.get("stage") or state.get("taskKind") or task_kind
        kind = observation.get("kind")
        goal = state.get("goal")
        goal = goal if isinstance(goal, str) else ""
        if phase == "read" or task_kind == "read":
            hint = _READ
        elif kind in {"locked_screen", "unverified_screen"}:
            hint = _NAVIGATION
        elif (
            phase in {"like", "collect", "unlike", "uncollect", "desired", "cancel"}
            and kind == "detail"
            and "toggle" in self.capabilities
        ):
            hint = _TOGGLE
        elif kind == "search_input" and "search_input" in self.capabilities:
            hint = _SEARCH_INPUT
        elif phase == "open_first" and "result" in self.capabilities:
            hint = _RESULT
        elif (
            kind == "search_results"
            and "result" in self.capabilities
            and task_kind != "search"
            and phase != "search"
        ):
            hint = _RESULT
        elif "search_entry" in self.capabilities and (
            phase == "search" or task_kind == "search" or "搜索" in goal
        ):
            hint = _SEARCH_ENTRY
        else:
            hint = _NAVIGATION
        return {
            "advisoryOnly": True,
            "currentGoalOnly": True,
            "sourceSha256": self.source_sha256,
            "hints": [hint, _BOUNDARY],
            "demonstratedTransitions": self._relevant_transitions(
                state, observation, phase, task_kind, goal
            ),
        }

    def _relevant_transitions(self, state, observation, phase, task_kind, goal):
        """Rank historical examples, never filter current actions or test completion."""
        kind = observation.get("kind")
        if phase == "read" or task_kind == "read" or kind not in _SCREEN_KINDS:
            return []
        goal_scope = re.sub(r"(?:不要|不用|不必|禁止|不)(?:点赞|收藏)", "", goal).casefold()
        collect_only = "收藏" in goal_scope or "collect" in goal_scope or "favorite" in goal_scope
        like_only = "点赞" in goal_scope or re.search(r"\blike\b", goal_scope) is not None
        desired_roles = _desired_roles(state)
        if kind == "detail" and desired_roles is not None:
            role = desired_roles
        elif kind == "detail" and collect_only != like_only:
            role = "collect" if collect_only else "like"
        elif phase in {"collect", "uncollect"} or task_kind in {"collect", "uncollect"}:
            role = "collect"
        elif phase in {"like", "unlike"} or task_kind in {"like", "unlike"}:
            role = "like"
        elif kind == "search_input":
            role = "search_field"
        elif kind == "search_results" and (phase == "open_first" or task_kind == "open_first"):
            role = "navigation"
        elif kind in {"feed", "launcher", "transition", "screen", "app"}:
            role = "navigation"
        else:
            return []
        roles = role if isinstance(role, frozenset) else {role}
        examples = [
            item
            for item in self.demonstrated_transitions
            if item.before_kind == kind and item.role in roles
        ]
        # Matching an example's *before* value improves relevance only. It does
        # not assert current predicates, choose an action or establish success.
        examples.sort(
            key=lambda item: (
                not (
                    item.example_field is not None
                    and observation.get(item.example_field) is item.example_before
                ),
                item.method,
                item.after_kind,
                str(item.example_before),
                str(item.example_after),
            )
        )
        return [item.context() for item in examples[:2]]


def load_experience_skill(path: str | Path) -> ExperienceSkill:
    """Load via the shared source/hash validator; only abstract capabilities leave."""
    try:
        recipe = load_recipe(Path(path).expanduser())
        source_sha = recipe.get("sourceSha256")
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
            raise ValueError("Missing verified source digest")
        capabilities = set()
        for step in recipe["steps"]:
            selector = step["selector"]
            before = step["before"]
            if selector["role"] == "search_field" and selector["method"] == "type":
                capabilities.add("search_input")
            if before["stage"] == "search" and before["observation"]["kind"] == "feed":
                capabilities.add("search_entry")
            if before["stage"] == "open_first" or before["observation"]["kind"] == "search_results":
                capabilities.add("result")
            if selector["role"] in {"like", "collect"}:
                capabilities.add("toggle")
        transitions = tuple(
            dict.fromkeys(
                item for step in recipe["steps"] if (item := _transition(step)) is not None
            )
        )
        return ExperienceSkill(
            source_sha, recipe["recipeSha256"], frozenset(capabilities), transitions
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Paths and malformed artifact contents may be private. Expose only the
        # failure category; callers must fix the requested skill or opt out.
        raise ExperienceSkillError(
            "Requested experience skill is not a valid verified demonstration"
        ) from None
