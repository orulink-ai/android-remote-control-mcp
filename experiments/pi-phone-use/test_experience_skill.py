"""Verified recordings become optional semantic hints, never executable selectors."""

import json

import pytest

from experience_skill import ExperienceSkillError, load_experience_skill
from workflow import recipe_digest


def write_skill(path):
    def state(stage, kind, liked=None, collected=None):
        return {
            "stage": stage,
            "done": stage == "done",
            "observation": {
                "kind": kind,
                "liked": liked,
                "collected": collected,
            },
        }

    observations = [
        state("search", "feed"),
        state("search", "search_input"),
        state("open_first", "search_results"),
        state("like", "detail", False, False),
        state("collect", "detail", True, False),
        state("unlike", "detail", True, True),
        state("uncollect", "detail", False, True),
        state("done", "detail", False, False),
    ]
    selectors = [
        ("click", "navigation", "搜索 node_deadbeef at 100,200"),
        ("type", "search_field", "旧示教词"),
        ("click", "navigation", "旧示教标题"),
        ("click", "like", "点赞"),
        ("click", "collect", "收藏"),
        ("click", "like", "取消点赞"),
        ("click", "collect", "取消收藏"),
    ]
    recipe = {
        "schemaVersion": 1,
        "source": "pi-agent-demonstration",
        "reviewState": "verified-demonstration",
        "sourceSha256": "a" * 64,
        "task": {"query": "旧示教词", "expectedFirstTitle": "旧示教标题"},
        "steps": [
            {
                "selector": dict(zip(("method", "role", "label"), selector)),
                "before": observations[i],
                "after": observations[i + 1],
            }
            for i, selector in enumerate(selectors)
        ],
    }
    recipe["recipeSha256"] = recipe_digest(recipe)
    path.write_text(json.dumps(recipe, ensure_ascii=False))
    return recipe


def test_experience_is_compact_advisory_and_does_not_export_recorded_targets(tmp_path):
    recipe = write_skill(tmp_path / "recipe.json")
    skill = load_experience_skill(tmp_path / "recipe.json")
    context = skill.context(
        {"stage": "search", "goal": "搜索猫咪", "observation": {"kind": "search_input"}}
    )
    assert context["advisoryOnly"] is True
    assert context["currentGoalOnly"] is True
    assert 1 <= len(context["hints"]) <= 2
    assert len(json.dumps(context, ensure_ascii=False)) < 700
    assert skill.source_sha256 == recipe["sourceSha256"]
    assert skill.recipe_sha256 == recipe["recipeSha256"]
    for private in ("旧示教词", "旧示教标题", "node_deadbeef", "100,200", "steps", "selector"):
        assert private not in json.dumps(context, ensure_ascii=False)


@pytest.mark.parametrize(
    "state,expected,excluded",
    [
        ({"stage": "search", "observation": {"kind": "feed"}}, "搜索入口", "点赞"),
        (
            {"stage": "search", "observation": {"kind": "search_input"}},
            "当前请求的搜索词",
            "旧示教词",
        ),
        (
            {"stage": "open_first", "observation": {"kind": "search_results"}},
            "当前目标",
            "旧示教标题",
        ),
        (
            {"stage": "unlike", "observation": {"kind": "detail", "liked": True}},
            "当前状态",
            "搜索入口",
        ),
        ({"task": {"kind": "read"}, "goal": "读取当前屏幕"}, "观察", "点赞"),
        ({"taskKind": "open", "goal": "打开其他App"}, "页面", "搜索入口"),
    ],
)
def test_hints_follow_current_observation_and_task_not_demonstration_cursor(
    tmp_path, state, expected, excluded
):
    write_skill(tmp_path / "recipe.json")
    value = json.dumps(
        load_experience_skill(tmp_path / "recipe.json").context(state), ensure_ascii=False
    )
    assert expected in value
    assert excluded not in value


@pytest.mark.parametrize("change", ["hash", "source", "review", "source_digest"])
def test_invalid_recordings_fail_explicitly_instead_of_becoming_hints(tmp_path, change):
    path = tmp_path / "recipe.json"
    recipe = write_skill(path)
    if change == "hash":
        recipe["task"]["query"] = "tampered"
    else:
        recipe[
            {"source": "source", "review": "reviewState", "source_digest": "sourceSha256"}[change]
        ] = "invalid"
        recipe["recipeSha256"] = recipe_digest(recipe)
    path.write_text(json.dumps(recipe))
    with pytest.raises(ExperienceSkillError):
        load_experience_skill(path)


def test_missing_path_is_an_error_and_does_not_expose_personal_path(tmp_path):
    missing = tmp_path / "private-name-secret.json"
    with pytest.raises(ExperienceSkillError) as error:
        load_experience_skill(missing)
    assert str(missing) not in str(error.value)


def test_demonstrated_transition_uses_verified_before_method_role_and_after(tmp_path):
    path = tmp_path / "recipe.json"
    write_skill(path)
    context = load_experience_skill(path).context(
        {
            "stage": "search",
            "goal": "本次在不同页面搜索猫咪",
            "observation": {"kind": "feed"},
        }
    )
    patterns = context["demonstratedTransitions"]
    assert 1 <= len(patterns) <= 2
    assert patterns[0] == {
        "beforeKind": "feed",
        "method": "click",
        "role": "navigation",
        "afterKind": "search_input",
        "exampleOnly": True,
    }
    for name in (
        "query",
        "expectedFirstTitle",
        "label",
        "selector",
        "sequence",
        "cursor",
        "node_deadbeef",
    ):
        assert name not in json.dumps(patterns, ensure_ascii=False)


def test_collect_only_experience_never_suggests_a_like_transition(tmp_path):
    path = tmp_path / "recipe.json"
    write_skill(path)
    context = load_experience_skill(path).context(
        {
            "stage": "act",
            "goal": "只收藏当前帖子，不要点赞",
            "observation": {"kind": "detail", "collected": False, "liked": False},
        }
    )
    examples = context["demonstratedTransitions"]
    assert 1 <= len(examples) <= 2
    assert all(item["role"] == "collect" for item in examples)
    assert examples[0]["exampleStateChange"] == {
        "field": "collected",
        "before": False,
        "after": True,
    }
    assert "liked" not in json.dumps(examples)
    assert context["advisoryOnly"] and context["currentGoalOnly"]


def test_like_phase_reports_boolean_example_without_current_predicates(tmp_path):
    path = tmp_path / "recipe.json"
    write_skill(path)
    examples = load_experience_skill(path).context(
        {
            "stage": "unlike",
            "goal": "取消点赞",
            "observation": {"kind": "detail", "liked": True},
        }
    )["demonstratedTransitions"]
    assert examples[0]["role"] == "like"
    assert examples[0]["exampleStateChange"] == {"field": "liked", "before": True, "after": False}
    assert all(item["exampleOnly"] for item in examples)
    assert all("requiredState" not in item and "postcondition" not in item for item in examples)


def test_read_only_task_does_not_receive_recorded_mutation_transitions(tmp_path):
    path = tmp_path / "recipe.json"
    write_skill(path)
    context = load_experience_skill(path).context(
        {
            "task": {"kind": "read"},
            "goal": "只看当前页面",
            "observation": {"kind": "detail", "liked": True},
        }
    )
    assert context["demonstratedTransitions"] == []


def test_different_verified_recordings_change_experience_content_not_just_digest(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    recipe = write_skill(first)
    recipe["sourceSha256"] = "b" * 64
    recipe["steps"][0]["selector"]["method"] = "tap"
    recipe["recipeSha256"] = recipe_digest(recipe)
    second.write_text(json.dumps(recipe))
    state = {"stage": "search", "goal": "搜索新的词", "observation": {"kind": "feed"}}
    original = load_experience_skill(first).context(state)
    replacement = load_experience_skill(second).context(state)
    assert original["hints"] == replacement["hints"]
    assert original["demonstratedTransitions"] != replacement["demonstratedTransitions"]
    assert replacement["demonstratedTransitions"][0]["method"] == "tap"


def test_current_input_context_gets_the_demonstrated_loading_transition(tmp_path):
    path = tmp_path / "recipe.json"
    recipe = write_skill(path)
    recipe["steps"][1]["after"]["observation"]["kind"] = "transition"
    recipe["steps"][2]["before"]["observation"]["kind"] = "transition"
    recipe["recipeSha256"] = recipe_digest(recipe)
    path.write_text(json.dumps(recipe))
    context = load_experience_skill(path).context(
        {
            "stage": "search",
            "query": "完全不同的词",
            "observation": {"kind": "search_input"},
        }
    )
    assert context["demonstratedTransitions"] == [
        {
            "beforeKind": "search_input",
            "method": "type",
            "role": "search_field",
            "afterKind": "transition",
            "exampleOnly": True,
        }
    ]


@pytest.mark.parametrize("phase,collected", [("desired", False), ("cancel", True)])
@pytest.mark.parametrize("nested", [True, False])
def test_runtime_desired_scope_controls_detail_hint_even_with_search_in_goal(
    tmp_path, phase, collected, nested
):
    path = tmp_path / "recipe.json"
    write_skill(path)
    state = {
        "stage": phase,
        "taskKind": "experience",
        "goal": "搜索其他帖子，点赞不要，只收藏，再取消收藏",
        "observation": {"kind": "detail", "liked": False, "collected": collected},
    }
    if nested:
        state["task"] = {"kind": "experience", "desired": {"collected": True}}
    else:
        state["desired"] = {"collected": True}
    context = load_experience_skill(path).context(state)
    assert "当前状态" in context["hints"][0]
    assert "搜索入口" not in context["hints"][0]
    assert context["demonstratedTransitions"]
    assert all(item["role"] == "collect" for item in context["demonstratedTransitions"])
