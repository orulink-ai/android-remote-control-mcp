from controller import prune_controls, rebind_action
import pytest


def c(key, label, bounds, method="click", role="navigation"):
    return {
        "id": key,
        "label": label,
        "bounds": bounds,
        "method": method,
        "role": role,
        "center": [(bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2],
    }


def test_search_parent_removed_actual_query_and_submit_preserved():
    controls = [
        c("parent", "搜索 小狮日记 全部删除 搜索", [114, 102, 1050, 258]),
        c("field", "搜索,小狮日记", [180, 114, 670, 246], "type"),
        c("clear", "全部删除", [778, 153, 832, 207]),
        c("submit", "搜索", [864, 114, 1032, 246]),
    ]
    assert [x["id"] for x in prune_controls(controls)] == ["field", "clear", "submit"]


def test_toolbar_leaf_buttons_preserved():
    controls = [
        c("like", "点赞", [447, 2163, 665, 2295], role="like"),
        c("collect", "收藏", [665, 2163, 890, 2295], role="collect"),
    ]
    assert prune_controls(controls) == controls


def test_rebinding_requires_same_intent_bounds_and_state():
    old = c("old", "收藏按钮；未激活", [665, 2163, 890, 2295], role="collect")
    new = c("new", "收藏按钮；未激活", [665, 2163, 890, 2295], role="collect")
    before = {"kind": "detail", "firstTitleVisible": True, "liked": True, "collected": False}
    assert rebind_action(old, [new], before, before)["id"] == "new"
    for changed in [
        {**before, "collected": True},
        {**before, "firstTitleVisible": False},
        {**before, "kind": "feed"},
    ]:
        assert rebind_action(old, [new], before, changed) is None
    assert rebind_action(old, [new, new], before, before) is None


def test_navigation_rebinding_does_not_guess_moved_or_renamed_controls():
    old = c("old", "搜索", [864, 114, 1032, 246])
    assert (
        rebind_action(
            old, [{**old, "id": "new"}], {"kind": "search_input"}, {"kind": "search_input"}
        )["id"]
        == "new"
    )
    assert (
        rebind_action(
            old,
            [{**old, "bounds": [864, 200, 1032, 320]}],
            {"kind": "search_input"},
            {"kind": "search_input"},
        )
        is None
    )
    assert (
        rebind_action(
            old, [{**old, "label": "立即安装"}], {"kind": "search_input"}, {"kind": "search_input"}
        )
        is None
    )


def test_actual_search_value_distinguishes_wrong_query_from_loading():
    from controller import search_value

    n = {
        "package": "com.xingin.xhs",
        "flags": ["on"],
        "class": "EditText",
        "text": "搜索, 小狮日记",
        "bounds": [180, 114, 670, 246],
    }
    assert search_value([n]) == "小狮日记"
    assert search_value([{**n, "text": "搜索, 初高衔接"}]) == "初高衔接"
    assert search_value([{**n, "text": "搜索,"}]) == ""


@pytest.mark.parametrize("trial_id", ["../outside", "/absolute", "a/b", ".", ""])
def test_trial_id_cannot_escape_artifact_directory(trial_id, monkeypatch):
    from pathlib import Path

    def unexpected_write(*args, **kwargs):
        raise AssertionError("Invalid ID must fail before filesystem access")

    monkeypatch.setattr(Path, "mkdir", unexpected_write)
    from controller import PhoneRuntime

    with pytest.raises(ValueError, match="Trial ID"):
        PhoneRuntime(trial_id)


def test_finish_closes_session_when_result_cannot_be_saved(monkeypatch):
    import controller

    runtime = controller.PhoneRuntime.__new__(controller.PhoneRuntime)
    runtime.started = controller.time.perf_counter()
    runtime.trial_id = "offline"
    runtime.started_at = "test"
    runtime.phases = []
    runtime.operations = []
    runtime.model_calls = []
    runtime.progress = controller.Progress()
    runtime.snapshot = None
    runtime.server_info = {}
    runtime.error = None
    runtime.direct_rebindings = 0
    runtime.stale_recoveries = 0
    from pathlib import Path

    runtime.directory = Path("/unused")

    class Mcp:
        closed = False

        def close(self):
            self.closed = True

    runtime.mcp = Mcp()

    def fail_save(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(controller, "private_write", fail_save)
    with pytest.raises(OSError):
        runtime.finish()
    assert runtime.mcp.closed


def test_retry_counters_count_selected_actions_and_compare_labels(monkeypatch, tmp_path):
    import controller

    runtime = controller.PhoneRuntime.__new__(controller.PhoneRuntime)
    runtime.started = controller.time.perf_counter()
    runtime.trial_id = "offline"
    runtime.started_at = "test"
    runtime.phases = []
    runtime.model_calls = []
    runtime.progress = controller.Progress()
    runtime.snapshot = None
    runtime.server_info = {}
    runtime.error = None
    runtime.direct_rebindings = 0
    runtime.stale_recoveries = 1
    runtime.directory = tmp_path

    class Mcp:
        def close(self):
            pass

    runtime.mcp = Mcp()
    runtime.operations = [
        {"name": "selected_action", "stage": "search", "choice": "CLICK:old", "label": "搜索"},
        {"name": "stale_reference", "stage": "search", "choice": "CLICK:old"},
        {"name": "selected_action", "stage": "search", "choice": "CLICK:new", "label": "搜索"},
        {"name": "selected_action", "stage": "search", "choice": "WAIT", "label": "等待"},
        {"name": "stale_reference", "stage": "search", "choice": "WAIT"},
        {"name": "selected_action", "stage": "open_first", "choice": "CLICK:new", "label": "搜索"},
    ]
    result = runtime.finish()
    assert result["retries"]["modelDirectedRepeatedActions"] == 1
    assert result["retries"]["waitChoices"] == 1
