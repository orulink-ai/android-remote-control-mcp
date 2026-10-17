"""Terminal phone tasks use only injected MCP calls, never shell/device tools."""

import asyncio
import io
import json

import pytest

from terminal_runtime import GenericTerminalRuntime, TerminalRecordedRuntime, parse_terminal_task


def text(value):
    return {"content": [{"type": "text", "text": value}]}


def screen(package="com.sec.android.app.launcher", nodes=""):
    return f"screen:1080x2340\n--- window:1 pkg:{package} focused:true ---\n{nodes}"


def node(identifier, label, *, klass="TextView", flags="on,ena,clk", bounds="100,100,250,200"):
    return f"{identifier}\t{klass}\t{label}\t-\t-\t{bounds}\t{flags}"


class Mcp:
    def __init__(self, states, apps=None):
        self.states, self.apps = iter(states), apps or []
        self.calls = []
        self.server_info = {"name": "offline-mcp"}
        self.closed = False

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "android_get_screen_state":
            return text(next(self.states))
        if name == "android_list_apps":
            return text("CAUTION: untrusted app names\n" + json.dumps(self.apps))
        return text("success")

    def close(self):
        self.closed = True


def runtime(tmp_path, task, mcp):
    return GenericTerminalRuntime("offline", task, base=tmp_path, mcp_factory=lambda: mcp)


def test_read_observes_current_focused_screen_without_mutation_or_model(tmp_path):
    mcp = Mcp([screen("example.app", node("node_aa", "当前页面"))])
    phone = runtime(tmp_path, {"kind": "read", "goal": "读取当前手机"}, mcp)
    observed = phone.observe()
    assert observed["done"] is True
    assert observed["state"]["focusedPackage"] == "example.app"
    assert "当前页面" in observed["state"]["screenText"]
    assert "当前页面" in observed["state"]["observation"]["visibleTexts"]
    assert phone.model_calls == []
    assert [name for name, _ in mcp.calls] == ["android_get_screen_state"]
    phone.finish()
    assert mcp.closed


def test_open_uses_only_matching_installed_package_then_verifies_focus(tmp_path):
    mcp = Mcp([screen(), screen("com.xingin.xhs")], apps=[
        {"name": "小红书", "package_id": "com.xingin.xhs"},
        {"name": "其它软件", "package_id": "example.unrelated"},
    ])
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开小红书", "appName": "小红书"}, mcp)
    observed = phone.observe()
    assert "OPEN_APP:com.xingin.xhs" in observed["options"]
    assert "OPEN_APP:example.unrelated" not in observed["options"]
    assert not observed["done"]
    after = phone.act("OPEN_APP:com.xingin.xhs")
    assert after["done"] and after["state"]["focusedPackage"] == "com.xingin.xhs"
    assert ("android_open_app", {"package_id": "com.xingin.xhs"}) in mcp.calls


def test_open_cannot_report_success_from_unfocused_background_window(tmp_path):
    background = screen() + "\n--- window:2 pkg:com.xingin.xhs focused:false ---"
    mcp = Mcp([screen(), background], apps=[{"name": "小红书", "package_id": "com.xingin.xhs"}])
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开小红书", "appName": "小红书"}, mcp)
    phone.observe()
    assert phone.act("OPEN_APP:com.xingin.xhs")["done"] is False


def test_open_already_in_unique_resolved_target_finishes_without_model_or_reopen(tmp_path):
    mcp = Mcp([screen("com.xingin.xhs")], apps=[
        {"name": "Android MCP", "package_id": "com.example.self"}])
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开小红书", "appName": "小红书"}, mcp)
    observed = phone.observe()
    assert observed["done"] and phone.progress.phase == "done"
    assert observed["state"]["observation"]["alreadyForeground"] is True
    assert not any(name == "android_open_app" for name, _ in mcp.calls)
    assert phone.model_calls == []


def test_open_unknown_target_cannot_infer_success_from_any_foreground_package(tmp_path):
    mcp = Mcp([screen("com.xingin.xhs")], apps=[])
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开神秘应用", "appName": "神秘应用"}, mcp)
    assert not phone.observe()["done"]


def test_explicit_blocked_decision_has_safe_typed_termination(tmp_path):
    from terminal_runtime import TerminalRuntimeError

    mcp = Mcp([screen()], apps=[])
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开神秘应用", "appName": "神秘应用"}, mcp)
    phone.observe()
    with pytest.raises(TerminalRuntimeError) as caught:
        phone.act("BLOCKED")
    assert caught.value.code == "BLOCKED"
    assert "停止" in str(caught.value)


@pytest.mark.parametrize("name,package", [
    ("小红书", "com.xingin.xhs"), ("REDnote", "com.xingin.xhs"),
    ("xiaohongshu", "com.xingin.xhs"), ("com.xingin.xhs", "com.xingin.xhs"),
    ("org.example.explicit", "org.example.explicit"),
])
def test_open_known_alias_or_explicit_package_when_android_list_is_self_only(tmp_path, name, package):
    mcp = Mcp([screen(), screen(package)], apps=[
        {"name": "Android MCP", "package_id": "com.example.self"}])
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开" + name, "appName": name}, mcp)
    observed = phone.observe()
    assert not observed["done"]  # The requested target is not yet foreground.
    assert "OPEN_APP:" + package in observed["options"]
    assert phone.act("OPEN_APP:" + package)["done"] is True
    assert ("android_open_app", {"package_id": package}) in mcp.calls
    assert observed["state"]["appDiscovery"]["candidateSource"] in {"known-alias", "explicit-package"}


def test_open_unknown_app_does_not_guess_package_from_limited_list(tmp_path):
    mcp = Mcp([screen()], apps=[{"name": "Android MCP", "package_id": "com.example.self"}])
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开神秘应用", "appName": "神秘应用"}, mcp)
    observed = phone.observe()
    assert set(observed["options"]) == {"BLOCKED"}
    assert "未安装" not in observed["options"]["BLOCKED"]
    assert "没有找到匹配的已安装应用" not in observed["options"]["BLOCKED"]


def test_open_known_alias_when_list_tool_is_unavailable(tmp_path):
    from own_mcp import McpError

    class LimitedMcp(Mcp):
        def call(self, name, arguments):
            if name == "android_list_apps":
                self.calls.append((name, arguments))
                raise McpError("Unavailable", stage="tool_call", exception_type="ToolError")
            return super().call(name, arguments)

    mcp = LimitedMcp([screen(), screen("com.xingin.xhs")])
    phone = runtime(tmp_path, {"kind": "open", "goal": "打开小红书", "appName": "小红书"}, mcp)
    observed = phone.observe()
    assert observed["state"]["appDiscovery"]["status"] == "unavailable"
    assert phone.act("OPEN_APP:com.xingin.xhs")["done"]


def locked_screen(marker="keyguard_root_view"):
    return screen("com.android.systemui", "node_aa\tFrameLayout\t锁屏\t-\t"
                  f"com.android.systemui:id/{marker}\t0,0,1080,2340\ton,ena")


@pytest.mark.parametrize("kind", ["open", "search"])
@pytest.mark.parametrize("marker", ["keyguard_root_view", "keyguard_panel_view", "keyguard_long_press"])
def test_locked_non_read_task_stops_before_any_launch_or_model(tmp_path, kind, marker):
    mcp = Mcp([locked_screen(marker)])
    task = {"kind": kind, "goal": "打开小红书", "appName": "小红书", "query": "猫咪"}
    phone = runtime(tmp_path, task, mcp)
    with pytest.raises(RuntimeError, match="锁屏"):
        phone.observe()
    assert [name for name, _ in mcp.calls] == ["android_get_screen_state"]
    assert phone.snapshot["state"]["locked"] is True


def test_read_can_report_locked_screen_without_unlocking(tmp_path):
    mcp = Mcp([locked_screen()])
    phone = runtime(tmp_path, {"kind": "read", "goal": "读取当前屏幕"}, mcp)
    observed = phone.observe()
    assert observed["done"] and observed["state"]["locked"] is True
    assert observed["state"]["observation"]["locked"] is True
    assert [name for name, _ in mcp.calls] == ["android_get_screen_state"]


@pytest.mark.parametrize("kind", ["open", "search"])
def test_launch_is_not_repeated_and_wait_budget_is_bounded(tmp_path, monkeypatch, kind):
    import terminal_runtime

    monkeypatch.setattr(terminal_runtime.time, "sleep", lambda _: None)
    mcp = Mcp([screen()] * 12)
    phone = runtime(tmp_path, {"kind": kind, "goal": "打开小红书", "appName": "小红书", "query": "猫咪"}, mcp)
    phone.observe()
    pending = phone.act("OPEN_APP:com.xingin.xhs")
    assert set(pending["options"]) == {"WAIT", "BLOCKED"}
    for _ in range(7):
        pending = phone.act("WAIT")
        assert set(pending["options"]) == {"WAIT", "BLOCKED"}
    with pytest.raises(RuntimeError, match="启动.*等待|等待.*启动"):
        phone.act("WAIT")
    assert sum(name == "android_open_app" for name, _ in mcp.calls) == 1


def test_worker_factory_failure_still_persists_failed_result(tmp_path, monkeypatch):
    import worker
    from own_mcp import McpError

    monkeypatch.setenv("PHONE_TERMINAL_TASK", json.dumps({"kind": "read", "goal": "读取当前屏幕"}))
    def unavailable(_trial):
        raise McpError("private connection details", stage="initialize", exception_type="ConnectError")
    output = io.StringIO()
    asyncio.run(worker.serve(
        "terminal-auto", "factory-error", base=tmp_path, runtime_factory=unavailable,
        input_stream=io.StringIO('{"command":"observe"}\n{"command":"finish"}\n'),
        output_stream=output, key_reader=lambda: pytest.fail("must not read model key"),
    ))
    result = json.loads((tmp_path / "factory-error/result.json").read_text())
    assert result["status"] == "failed" and result["terminalTask"]["kind"] == "read"
    assert result["errorDetails"]["stage"] == "initialize"
    assert "private connection" not in json.dumps(result)


def test_worker_locked_error_persists_clear_safe_result_without_model(tmp_path, monkeypatch):
    import terminal_runtime
    import worker

    mcp = Mcp([locked_screen()])
    monkeypatch.setattr(terminal_runtime, "OwnMcp", lambda: mcp)
    monkeypatch.setenv("PHONE_TERMINAL_TASK", json.dumps({"kind": "open", "goal": "打开小红书", "appName": "小红书"}))
    asyncio.run(worker.serve(
        "terminal-auto", "locked-worker", base=tmp_path,
        input_stream=io.StringIO('{"command":"observe"}\n'), output_stream=io.StringIO(),
        key_reader=lambda: pytest.fail("locked phone must not read model key"),
    ))
    result = json.loads((tmp_path / "locked-worker/result.json").read_text())
    assert result["status"] == "failed"
    assert result["errorDetails"]["code"] == "LOCKED" and "锁屏" in result["error"]
    assert result["finalState"]["locked"] is True
    assert mcp.closed and [name for name, _ in mcp.calls] == ["android_get_screen_state"]


def test_recorded_lock_preflight_stops_before_home(tmp_path, monkeypatch):
    from controller import PhoneRuntime
    from types import SimpleNamespace

    calls = []
    def initialize(self, trial):
        self.directory, self.index = tmp_path, 0
        self.actions, self.snapshot = {}, None
        self.progress = SimpleNamespace(phase="search")
        self.operations, self.phases = [], []
    def call(self, name, arguments):
        calls.append(name)
        return text(locked_screen())
    monkeypatch.setattr(PhoneRuntime, "__init__", initialize)
    monkeypatch.setattr(PhoneRuntime, "call", call)
    phone = TerminalRecordedRuntime("locked-recorded", {"kind": "recorded", "goal": "复用录制任务"})
    with pytest.raises(RuntimeError, match="锁屏"):
        phone.observe()
    assert calls == ["android_get_screen_state"]


def test_search_only_completes_with_requested_query_and_result_evidence(tmp_path):
    nodes = node("node_aa", "猫咪", klass="EditText", bounds="180,80,760,190")
    nodes += "\n" + node("node_bb", "综合", flags="on,ena")
    mcp = Mcp([screen("com.xingin.xhs", nodes)])
    phone = runtime(tmp_path, {"kind": "search", "goal": "搜索猫咪", "query": "猫咪"}, mcp)
    observed = phone.observe()
    assert observed["done"] and observed["state"]["observation"]["kind"] == "search_results"
    wrong = nodes.replace("猫咪", "狗狗")
    mcp2 = Mcp([screen("com.xingin.xhs", wrong)])
    other = GenericTerminalRuntime("wrong", phone.task, base=tmp_path, mcp_factory=lambda: mcp2)
    assert not other.observe()["done"]


def test_search_typing_rebinds_after_clear_and_uses_fixed_requested_query(tmp_path):
    field = node("node_aa", "旧词", klass="EditText", bounds="180,80,760,190")
    new = node("node_bb", "", klass="EditText", bounds="180,80,760,190")
    typed = node("node_cc", "猫咪", klass="EditText", bounds="180,80,760,190")
    mcp = Mcp([screen("com.xingin.xhs", field), screen("com.xingin.xhs", new),
               screen("com.xingin.xhs", typed)])
    phone = runtime(tmp_path, {"kind": "search", "goal": "搜索猫咪", "query": "猫咪"}, mcp)
    observed = phone.observe()
    assert "TYPE:node_aa" in observed["options"]
    phone.act("TYPE:node_aa")
    append = [arguments for name, arguments in mcp.calls if name == "android_type_append_text"]
    assert append[0]["node_id"] == "node_bb" and append[0]["text"] == "猫咪"


def test_search_can_return_from_existing_detail_via_explicit_pi_back_action(tmp_path):
    mcp = Mcp([screen("com.xingin.xhs", node("node_aa", "某篇详情")), screen("com.xingin.xhs")])
    phone = runtime(tmp_path, {"kind": "search", "goal": "搜索猫咪", "query": "猫咪"}, mcp)
    observed = phone.observe()
    assert "CLICK:terminal-back" in observed["options"]
    assert not any(name == "android_press_back" for name, _ in mcp.calls)
    phone.act("CLICK:terminal-back")
    assert ("android_press_back", {}) in mcp.calls


def test_search_never_offers_comment_field_or_submit_key_on_detail(tmp_path):
    comment = node("node_aa", "猫咪", klass="EditText", bounds="180,2140,760,2250")
    mcp = Mcp([screen("com.xingin.xhs", comment)])
    phone = runtime(tmp_path, {"kind": "search", "goal": "搜索猫咪", "query": "猫咪"}, mcp)
    observed = phone.observe()
    assert "TYPE:node_aa" not in observed["options"]
    assert "CLICK:terminal-enter" not in observed["options"]
    assert "CLICK:terminal-back" in observed["options"]


def test_search_result_query_bar_can_be_edited_but_wrong_query_cannot_submit(tmp_path):
    query = node("node_aa", "小狮日记", bounds="210,152,1065,306")
    query += "\n" + node("node_bb", "搜索", bounds="1187,152,1384,306")
    query += "\n" + node("node_cc", "综合", flags="on,ena", bounds="100,350,250,450")
    raw = screen("com.xingin.xhs", query).replace("1080x2340", "1440x3200")
    mcp = Mcp([raw])
    phone = runtime(tmp_path, {"kind": "search", "goal": "搜索猫咪", "query": "猫咪"}, mcp)
    observed = phone.observe()
    assert "CLICK:node_aa" in observed["options"]
    assert observed["state"]["actionDescriptors"]["CLICK:node_aa"]["role"] == "search_field"
    assert "CLICK:node_bb" not in observed["options"]
    assert "CLICK:terminal-enter" not in observed["options"]


@pytest.mark.parametrize("value", ["旧词", ""])
def test_search_form_submit_is_not_offered_for_wrong_or_unknown_query(tmp_path, value):
    form = node("node_aa", value, klass="EditText", bounds="180,80,760,190")
    form += "\n" + node("node_bb", "搜索", bounds="800,80,950,190")
    phone = runtime(tmp_path, {"kind": "search", "goal": "搜索猫咪", "query": "猫咪"},
                    Mcp([screen("com.xingin.xhs", form)]))
    observed = phone.observe()
    assert "CLICK:node_bb" not in observed["options"]
    assert "TYPE:node_aa" in observed["options"]


@pytest.mark.parametrize("prefix", ["搜索, ", "搜索，"])
def test_search_edit_field_fallback_normalizes_android_search_prefix(tmp_path, prefix):
    form = node("node_aa", prefix + "猫咪日常", klass="EditText", bounds="50,80,760,190")
    form += "\n" + node("node_bb", "搜索", bounds="800,80,950,190")
    phone = runtime(tmp_path, {"kind": "search", "goal": "搜索猫咪日常", "query": "猫咪日常"},
                    Mcp([screen("com.xingin.xhs", form)]))
    observed = phone.observe()
    assert observed["state"]["observation"]["observedQuery"] == "猫咪日常"
    assert observed["state"]["observation"]["queryMatches"] is True
    assert "CLICK:node_bb" in observed["options"]
    assert "CLICK:terminal-enter" in observed["options"]


def test_three_semantically_identical_actions_without_progress_stop_before_fourth(tmp_path, monkeypatch):
    from terminal_runtime import TerminalRuntimeError

    monkeypatch.setattr("terminal_runtime.time.sleep", lambda _: None)
    states = [screen("com.xingin.xhs", node(f"node_a{i}", "搜索")) for i in range(4)]
    mcp = Mcp(states)
    phone = runtime(tmp_path, {"kind": "search", "goal": "搜索猫咪", "query": "猫咪"}, mcp)
    phone.observe()
    for i in range(2):
        phone.act("CLICK:node_a" + str(i))
    with pytest.raises(TerminalRuntimeError) as caught:
        phone.act("CLICK:node_a2")
    assert caught.value.code == "NO_PROGRESS"
    assert sum(name == "android_click_node" for name, _ in mcp.calls) == 3


@pytest.mark.parametrize("task", [
    {}, {"kind": "shell", "goal": "x"}, {"kind": "open", "goal": "x"},
    {"kind": "search", "goal": "x", "query": ""},
    {"kind": "search", "goal": "x", "query": "cat", "appName": "其它App"},
])
def test_terminal_task_validation_is_bounded_and_explicit(task):
    with pytest.raises(ValueError):
        parse_terminal_task(json.dumps(task))


def test_recorded_runtime_preparation_is_only_mcp_home_and_back_is_explicit(tmp_path, monkeypatch):
    from controller import PhoneRuntime
    calls = []

    def initialize(self, trial):
        self.directory = tmp_path
        self.index = 0
        self.actions = {}
        self.operations, self.phases = [], []

    def call(self, name, arguments):
        calls.append((name, arguments))
        if name == "android_get_screen_state":
            return text(screen())

    def observe(self):
        self.index += 1
        (tmp_path / f"observation-{self.index}.txt").write_text(screen("com.xingin.xhs"))
        self.snapshot = {"state": {"goal": "fixed", "stage": "search",
            "observation": {"kind": "detail"}, "actionDescriptors": {}},
            "options": {}, "done": False}
        return self.snapshot

    monkeypatch.setattr(PhoneRuntime, "__init__", initialize)
    monkeypatch.setattr(PhoneRuntime, "call", call)
    monkeypatch.setattr(PhoneRuntime, "observe", observe)
    phone = TerminalRecordedRuntime("recorded", {"kind": "recorded", "goal": "复用已录制任务"})
    observed = phone.observe()
    assert calls == [("android_get_screen_state", {"include_screenshot": False}),
                     ("android_press_key", {"key": "HOME"})]
    assert "CLICK:terminal-back" in observed["options"]
    assert observed["state"]["goal"] == "复用已录制任务"


def test_actual_worker_read_selects_terminal_factory_and_needs_no_key(tmp_path, monkeypatch):
    import terminal_runtime
    import worker

    mcp = Mcp([screen("example.app", node("node_aa", "当前页面"))])
    monkeypatch.setattr(terminal_runtime, "OwnMcp", lambda: mcp)
    monkeypatch.setenv("PHONE_TERMINAL_GOAL", "读取当前屏幕")
    monkeypatch.delenv("PHONE_TERMINAL_TASK", raising=False)
    output = io.StringIO()
    asyncio.run(worker.serve(
        "terminal-auto", "read-worker", base=tmp_path,
        input_stream=io.StringIO('{"command":"observe"}\n{"command":"finish"}\n'),
        output_stream=output,
        key_reader=lambda: pytest.fail("read must not read model credentials"),
        client_factory=lambda *args, **kwargs: pytest.fail("read must not construct model client"),
    ))
    result = json.loads((tmp_path / "read-worker/result.json").read_text())
    assert result["status"] == "success" and result["provider"] == "jev-auto"
    assert result["decisionPolicy"]["attempts"] == []
    assert result["terminalSummary"] == {"kind": "read", "backend": "jev-auto"}
    assert mcp.closed


def test_actual_worker_open_jev_failure_refreshes_before_deepseek(tmp_path, monkeypatch):
    import terminal_runtime
    import worker
    from providers import ProviderError

    mcp = Mcp([screen(), screen(), screen("com.xingin.xhs")], apps=[
        {"name": "小红书", "package_id": "com.xingin.xhs"}])
    monkeypatch.setattr(terminal_runtime, "OwnMcp", lambda: mcp)
    monkeypatch.setenv("PHONE_TERMINAL_TASK", json.dumps({
        "kind": "open", "goal": "打开小红书", "appName": "小红书"}))
    backends = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def decide(self, backend, state, options):
            backends.append(backend)
            if backend == "jev":
                raise ProviderError("fixture", network={}, latency_ms=0,
                                    exception_type="ConnectError", stage="tcp_connect")
            return {"choice": "OPEN_APP:com.xingin.xhs", "latencyMs": 0}
        async def close(self):
            pass

    requests = [{"command": command} for command in ("observe", "decide", "observe", "decide")]
    requests += [{"command": "act", "choice": "OPEN_APP:com.xingin.xhs"}, {"command": "finish"}]
    output = io.StringIO()
    asyncio.run(worker.serve(
        "terminal-auto", "open-worker", base=tmp_path,
        input_stream=io.StringIO("\n".join(json.dumps(item) for item in requests)),
        output_stream=output, key_reader=lambda: "offline", client_factory=Client,
    ))
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[1]["result"]["kind"] == "refresh_required"
    result = json.loads((tmp_path / "open-worker/result.json").read_text())
    assert backends == ["jev", "deepseek"] and result["status"] == "success"
    assert result["decisionPolicy"]["fallbackSuccesses"] == 1
    assert sum(name == "android_get_screen_state" for name, _ in mcp.calls) == 3
