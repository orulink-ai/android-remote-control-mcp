"""Bounded terminal phone tasks over this project's MCP, with no shell transport.

Read, launch an installed matching app, and search Xiaohongshu are explicit
adapters, not an arbitrary-task agent. Recorded replay retains the common task
verifier and prepares HOME through MCP; returning from a retained app stack is
an observed PI/Jev action rather than an implicit force-stop.
"""

from __future__ import annotations

from datetime import datetime
import copy
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

from config import output_dir
from controller import (
    PACKAGE, PhoneRuntime, private_write, prune_controls, screen_dimensions, search_value,
)
from own_mcp import McpError, OwnMcp, content_text, own_nodes
from window_state import focused_package

BACK = "CLICK:terminal-back"


class TerminalRuntimeError(RuntimeError):
    MESSAGES = {
        "LOCKED": "手机当前处于锁屏状态，请手动解锁后重试；未尝试解锁。",
        "UNKNOWN_FOCUS": "无法确认手机当前前台窗口，已停止。",
        "OPEN_TIMEOUT": "应用启动后的等待次数已达 8 次，未确认目标前台，已停止。",
        "BLOCKED": "手机任务无法安全继续，已停止；未继续派发动作。",
        "NO_PROGRESS": "相同界面上的同一动作连续 3 次未取得进展，已停止。",
    }

    def __init__(self, code):
        self.code = code
        super().__init__(self.MESSAGES[code])


def screen_locked(nodes):
    markers = {"keyguard_root_view", "keyguard_panel_view", "keyguard_long_press",
               "keyguard_bouncer", "keyguard_security_container"}
    return any(
        node["package"] == "com.android.systemui" and "on" in node["flags"]
        and node["resource_id"].startswith("com.android.systemui:id/")
        and (node["resource_id"].split("/", 1)[1] in markers
             or "bouncer" in node["resource_id"].split("/", 1)[1])
        for node in nodes
    )


def stop_on_screen(runtime, task, package, locked, labels, code):
    runtime.actions = {}
    observation = {"kind": "locked_screen" if locked else "unverified_screen",
                   "focusedPackage": package, "locked": locked, "visibleTexts": labels[:120]}
    runtime.snapshot = {
        "state": {"goal": task["goal"], "stage": runtime.progress.phase,
                  "focusedPackage": package, "locked": locked, "observation": observation,
                  "actionDescriptors": {}, "visibleControls": []},
        "options": {}, "done": False,
    }
    runtime.operations.append({"name": "terminal_stop", "code": code})
    private_write(runtime.directory / f"state-{runtime.index}.json", json.dumps(runtime.snapshot, ensure_ascii=False))
    raise TerminalRuntimeError(code)


def focus_wait(runtime, attempt, raw):
    runtime.operations.append({"name": "focus_stabilization", "attempt": attempt,
                               "readOnly": True, "status": "retry"})
    private_write(runtime.directory / f"focus-stabilization-{len(runtime.operations)}.txt", raw)
    start = time.perf_counter()
    time.sleep(0.25)
    runtime.phases.append({"kind": "wait", "name": "前台焦点稳定只读等待",
                           "ms": (time.perf_counter() - start) * 1000})


def stable_focus_text(runtime, initial):
    """A transient missing focus permits three reads, never a phone mutation."""
    raw = initial
    for attempt in range(1, 4):
        if focused_package(raw) is not None or screen_locked(own_nodes(raw)):
            return raw
        focus_wait(runtime, attempt, raw)
        raw = content_text(runtime.call("android_get_screen_state", {"include_screenshot": False}))
    return raw


def parse_terminal_task(raw):
    task = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if not isinstance(task, dict) or task.get("kind") not in {"read", "open", "search", "recorded"}:
        raise ValueError("Unsupported terminal phone task")
    if not isinstance(task.get("goal"), str) or not 1 <= len(task["goal"].strip()) <= 4000:
        raise ValueError("A bounded terminal goal is required")
    if task["kind"] == "open" and (
        not isinstance(task.get("appName"), str) or not 1 <= len(task["appName"].strip()) <= 128
    ):
        raise ValueError("Opening an app requires its name")
    if task["kind"] == "search":
        if not isinstance(task.get("query"), str) or not 1 <= len(task["query"].strip()) <= 256:
            raise ValueError("Search requires a bounded query")
        app_name = task.get("appName", "小红书")
        if not isinstance(app_name, str) or app_name.casefold() not in {
            "小红书", "xiaohongshu", PACKAGE,
        }:
            raise ValueError("Terminal search currently supports Xiaohongshu only")
    return task


def _controls(nodes, package):
    controls = []
    for item in nodes:
        label = item["description"] or item["text"]
        if item["package"] != package or not {"on", "ena"}.issubset(item["flags"]):
            continue
        if not label:
            if item["class"] != "EditText":
                continue
            label = "空搜索输入框"
        if not any(flag in item["flags"] for flag in {"clk", "edt"}):
            continue
        controls.append({
            "id": item["id"], "label": label[:300], "bounds": item["bounds"],
            "method": "type" if item["class"] == "EditText" else "click",
            "role": "search_field" if item["class"] == "EditText" else "navigation",
        })
    return prune_controls(controls)


def _apps(result, name):
    raw = content_text(result)
    try:
        # Current MCP prepends an untrusted-content warning before the JSON array.
        value = json.loads(raw[raw.index("["):])
    except (ValueError, TypeError):
        raise RuntimeError("Installed app list is not valid JSON") from None
    if not isinstance(value, list) or len(value) > 3000:
        raise RuntimeError("Installed app list is invalid")
    matching = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label, package = item.get("name"), item.get("package_id")
        if not isinstance(label, str) or not isinstance(package, str):
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
            continue
        if name.casefold() in label.casefold() or name.casefold() in package.casefold():
            matching.append({"name": label, "package_id": package})
    exact = [item for item in matching if name.casefold() in {
        item["name"].casefold(), item["package_id"].casefold(),
    }]
    result = exact or matching
    if len(result) > 20 or len({item["package_id"] for item in result}) != len(result):
        raise RuntimeError("Installed app candidates are ambiguous")
    return result


def _search_fields(controls, state_text):
    width, height = screen_dimensions(state_text)
    return [control for control in controls if control["method"] == "type"
            and control["bounds"][1] / height < 300 / 2340
            and (control["bounds"][2] - control["bounds"][0]) / width >= 0.3]


def _query_bars(controls, nodes, state_text, observed_query):
    width, height = screen_dimensions(state_text)
    by_id = {node["id"]: node for node in nodes}
    result = []
    for control in controls:
        node = by_id[control["id"]]
        bounds = control["bounds"]
        value = re.sub(r"^搜索[,，]\s*", "", node["text"]).strip()
        if (
            control["method"] == "click" and node["class"] == "TextView" and value
            and 0.1 <= bounds[0] / width <= 0.25 and bounds[1] / height < 300 / 2340
            and (bounds[2] - bounds[0]) / width >= 0.3
            and observed_query is not None and value == observed_query
        ):
            control["role"] = "search_field"
            control["label"] = "编辑当前搜索词：" + value
            result.append(control)
    return result


def _explicit_app(name):
    name = name.strip()
    if name.casefold() in {"小红书", "rednote", "xiaohongshu"}:
        return {"name": name, "package_id": PACKAGE}, "known-alias"
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", name):
        return {"name": name, "package_id": name}, "explicit-package"
    return None, None


class GenericTerminalRuntime(PhoneRuntime):
    """Shared audit methods, with task-specific semantic candidates and completion."""

    def __init__(self, trial_id, task, *, base=None, mcp_factory=None):
        self.task = parse_terminal_task(task)
        if self.task["kind"] == "recorded":
            raise ValueError("Recorded tasks require the common replay runtime")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", trial_id):
            raise ValueError("Trial ID must be a simple filename")
        self.started = time.perf_counter()
        self.started_at = datetime.now().astimezone().isoformat()
        self.trial_id = trial_id
        self.directory = Path(base or output_dir()) / trial_id
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.phases, self.operations, self.model_calls = [], [], []
        self.progress = SimpleNamespace(phase=self.task["kind"], milestones=[])
        self.actions, self.snapshot = {}, None
        self.index = 0
        self.error = None
        self.stale_recoveries = self.direct_rebindings = 0
        self.selected_package = None
        self.pending_open = None
        self.open_waits = 0
        self.apps = None
        self.app_discovery = None
        self.no_progress = {}
        start = time.perf_counter()
        self.mcp = (mcp_factory or OwnMcp)()
        self.server_info = self.mcp.server_info
        self.setup_ms = (time.perf_counter() - start) * 1000
        self.phases.append({"kind": "mcp", "name": "MCP初始化与握手", "ms": self.setup_ms})

    def observe(self):
        raw = self.call("android_get_screen_state", {"include_screenshot": False})
        text = stable_focus_text(self, content_text(raw))
        package = focused_package(text)
        nodes = own_nodes(text)
        controls = _controls(nodes, package)
        self.index += 1
        private_write(self.directory / f"observation-{self.index}.txt", text)
        labels = [item["description"] or item["text"] for item in nodes
                  if item["package"] == package and "on" in item["flags"]
                  and (item["description"] or item["text"])]
        locked = screen_locked(nodes)
        if self.task["kind"] != "read" and (locked or package is None):
            stop_on_screen(self, self.task, package, locked, labels, "LOCKED" if locked else "UNKNOWN_FOCUS")
        actions = {
            "WAIT": {"method": "wait", "role": "wait", "label": "等待界面稳定后重新观察"},
            "BLOCKED": {"method": "blocked", "role": "blocked", "label": "无法安全继续，停止"},
        }
        kind, done = "locked_screen" if locked else "screen", self.task["kind"] == "read"
        observed_query = None
        if self.task["kind"] == "open":
            if self.apps is None:
                self.app_discovery = {"status": "available", "candidateSource": "visible-list"}
                try:
                    self.apps = _apps(self.call("android_list_apps", {
                        "filter": "all", "name_query": self.task["appName"],
                    }), self.task["appName"])
                except McpError:
                    self.apps = []
                    self.app_discovery["status"] = "unavailable"
                if not self.apps:
                    candidate, source = _explicit_app(self.task["appName"])
                    if candidate is not None:
                        self.apps = [candidate]
                    self.app_discovery.update(
                        candidateSource=source or "none", installationKnown=False,
                        note="App list visibility is limited; only actual launch and focused-package readback verify completion",
                    )
                self.operations.append({"name": "app_candidate_resolution", **self.app_discovery})
            for app in self.apps:
                actions["OPEN_APP:" + app["package_id"]] = {
                    "method": "open", "role": "navigation",
                    "label": f"按明确应用目标通过 MCP 打开 {app['name']} ({app['package_id']})，然后回读前台包名验证",
                    "package": app["package_id"],
                }
            if not self.apps:
                actions = {"BLOCKED": {"method": "blocked", "role": "blocked",
                                       "label": "应用列表可见性不足，且没有明确包名或已知别名；停止，不猜测应用包名"}}
            target = self.selected_package
            if target is None and len(self.apps) == 1:
                target = self.apps[0]["package_id"]
            done = target is not None and package == target
            kind = "app" if done else "screen"
        elif self.task["kind"] == "search":
            if package != PACKAGE:
                actions["OPEN_APP:" + PACKAGE] = {
                    "method": "open", "role": "navigation", "label": "打开小红书进行搜索",
                    "package": PACKAGE,
                }
            else:
                observed_query = search_value(nodes, screen_dimensions(text))
                fields = _search_fields(controls, text)
                if len(fields) == 1:
                    field = next(item for item in nodes if item["id"] == fields[0]["id"])
                    observed_query = re.sub(r"^搜索[,，]\s*", "", field["text"].strip()).strip()
                query_bars = _query_bars(controls, nodes, text, observed_query)
                search_form = bool(fields or query_bars or "综合" in labels or "全部删除" in labels)
                done = observed_query == self.task["query"] and "综合" in labels
                kind = "search_results" if done else "search_input" if fields else "screen"
                for control in controls:
                    if control in fields or control in query_bars or (
                        control["method"] != "type" and control["label"] in {"搜索", "返回", "首页"}
                    ):
                        if control["label"] == "搜索" and search_form and observed_query != self.task["query"]:
                            continue
                        prefix = "TYPE:" if control["method"] == "type" else "CLICK:"
                        actions[prefix + control["id"]] = control
                actions[BACK] = {"method": "back", "role": "navigation", "label": "返回上一级，寻找搜索入口"}
                if len(fields) == 1 and observed_query == self.task["query"]:
                    actions["CLICK:terminal-enter"] = {
                        "method": "key", "role": "navigation", "label": "提交当前正确搜索词（ENTER）",
                    }
        if self.pending_open is not None:
            if package == self.pending_open:
                self.pending_open = None
                self.open_waits = 0
            else:
                if self.open_waits >= 8:
                    stop_on_screen(self, self.task, package, locked, labels, "OPEN_TIMEOUT")
                actions = {key: actions[key] for key in ("WAIT", "BLOCKED")}
                kind = "app_launch_pending"
        if done and self.progress.phase != "done":
            self.progress.milestones.append({"stage": self.progress.phase, "next": "done"})
            self.progress.phase = "done"
        self.actions = actions
        observation = {
            "kind": kind, "focusedPackage": package, "observedQuery": observed_query,
            "queryMatches": observed_query == self.task.get("query") if observed_query is not None else False,
            "visibleTexts": labels[:120],
            "locked": locked,
            "alreadyForeground": bool(self.task["kind"] == "open" and done and self.selected_package is None),
        }
        self.snapshot = {
            "state": {
                "goal": self.task["goal"], "stage": self.progress.phase,
                "taskKind": self.task["kind"], "query": self.task.get("query"),
                "appName": self.task.get("appName"), "focusedPackage": package,
                "locked": locked, "pendingOpenPackage": self.pending_open,
                "openWaits": self.open_waits,
                "appDiscovery": self.app_discovery,
                "observation": observation, "visibleControls": controls,
                "screenText": labels[:120],
                "actionDescriptors": {key: {name: action.get(name) for name in ("method", "role", "label")}
                                      for key, action in actions.items()},
            },
            "options": {key: action["label"] for key, action in actions.items()}, "done": done,
        }
        private_write(self.directory / f"state-{self.index}.json", json.dumps(self.snapshot, ensure_ascii=False))
        return self.snapshot

    def act(self, choice):
        if choice not in self.actions:
            raise RuntimeError("Choice is unavailable on the current screen")
        if len([item for item in self.operations if item.get("name") == "selected_action"]) >= 24:
            raise RuntimeError("Terminal action budget exhausted")
        if time.perf_counter() - self.started > 150:
            raise RuntimeError("Terminal task time budget exhausted")
        action = self.actions[choice]
        before = self._progress_signature()
        semantic_action = tuple(action.get(key) for key in ("method", "role", "label"))
        self.operations.append({"name": "selected_action", "choice": choice,
                                "label": action["label"], "stage": self.progress.phase})
        method = action["method"]
        if method == "blocked":
            raise TerminalRuntimeError("BLOCKED")
        if method == "open":
            self.call("android_open_app", {"package_id": action["package"]})
            self.selected_package = action["package"]
            self.pending_open = action["package"]
            self.open_waits = 0
        elif method == "click":
            self.call("android_click_node", {"node_id": action["id"]})
        elif method == "back":
            self.call("android_press_back", {})
        elif method == "key":
            self.call("android_press_key", {"key": "ENTER"})
        elif method == "type":
            self.call("android_type_clear_text", {"node_id": action["id"]})
            fresh = stable_focus_text(self, content_text(self.call(
                "android_get_screen_state", {"include_screenshot": False})))
            if focused_package(fresh) != PACKAGE:
                raise RuntimeError("Input focus changed after clearing")
            fields = _search_fields(_controls(own_nodes(fresh), PACKAGE), fresh)
            if len(fields) != 1:
                raise RuntimeError("Search field became ambiguous after clearing")
            self.call("android_type_append_text", {
                "node_id": fields[0]["id"], "text": self.task["query"],
                "typing_speed": 10, "typing_speed_variance": 0,
            })
        elif method == "wait":
            if self.pending_open is not None:
                self.open_waits += 1
        else:
            raise RuntimeError("Unsupported terminal action")
        start = time.perf_counter()
        time.sleep(0.25)
        self.phases.append({"kind": "wait", "name": "终端任务界面稳定等待", "ms": (time.perf_counter() - start) * 1000})
        result = self.observe()
        if not result["done"] and self.pending_open is None and before == self._progress_signature():
            key = (before, semantic_action)
            self.no_progress[key] = self.no_progress.get(key, 0) + 1
            self.operations.append({"name": "terminal_no_progress", "count": self.no_progress[key],
                                    "choice": choice, "observation": self.index})
            if self.no_progress[key] >= 3:
                raise TerminalRuntimeError("NO_PROGRESS")
        else:
            self.no_progress.clear()
        return result

    def _progress_signature(self):
        state = self.snapshot["state"]
        observed = state["observation"]
        return json.dumps({
            "focusedPackage": state["focusedPackage"], "kind": observed["kind"],
            "observedQuery": observed.get("observedQuery"),
            "actions": sorted((item.get("method"), item.get("role"), item.get("label"))
                              for item in state["actionDescriptors"].values()),
        }, ensure_ascii=False, sort_keys=True)


class TerminalRecordedRuntime(PhoneRuntime):
    """Prepare a reusable start using MCP HOME and expose semantic stack recovery."""

    def __init__(self, trial_id, task):
        self.task = parse_terminal_task(task)
        if self.task["kind"] != "recorded":
            raise ValueError("A recorded task is required")
        self._prepared = False
        super().__init__(trial_id)

    def observe(self):
        if not self._prepared:
            initial = stable_focus_text(self, content_text(self.call(
                "android_get_screen_state", {"include_screenshot": False})))
            package = focused_package(initial)
            locked = screen_locked(own_nodes(initial))
            if locked or package is None:
                stop_on_screen(self, self.task, package, locked, [], "LOCKED" if locked else "UNKNOWN_FOCUS")
            self._prepared = True
            self.call("android_press_key", {"key": "HOME"})
        for attempt in range(4):
            previous_progress = copy.deepcopy(getattr(self, "progress", None))
            snapshot = super().observe()
            raw = (self.directory / f"observation-{self.index}.txt").read_text()
            package, locked = focused_package(raw), screen_locked(own_nodes(raw))
            if package is not None and not locked:
                break
            # Never let an unfocused/transient tree become completion evidence.
            if previous_progress is not None:
                self.progress = previous_progress
                snapshot["state"]["stage"] = previous_progress.phase
                snapshot["done"] = previous_progress.phase == "done"
                private_write(self.directory / f"state-{self.index}.json", json.dumps(snapshot, ensure_ascii=False))
            if locked or attempt == 3:
                stop_on_screen(self, self.task, package, locked, [], "LOCKED" if locked else "UNKNOWN_FOCUS")
            focus_wait(self, attempt + 1, raw)
        state = snapshot["state"]
        state["workflowGoal"], state["goal"] = state["goal"], self.task["goal"]
        raw = (self.directory / f"observation-{self.index}.txt").read_text()
        state["focusedPackage"] = focused_package(raw)
        state["locked"] = screen_locked(own_nodes(raw))
        state["observation"]["focusedPackage"] = state["focusedPackage"]
        state["observation"]["locked"] = state["locked"]
        if state["locked"] or state["focusedPackage"] is None:
            stop_on_screen(self, self.task, state["focusedPackage"], state["locked"], [],
                           "LOCKED" if state["locked"] else "UNKNOWN_FOCUS")
        if state["stage"] == "search" and state["focusedPackage"] == PACKAGE:
            has_search = any(action.get("label") == "搜索" for action in self.actions.values())
            if state["observation"]["kind"] != "feed" or not has_search:
                self.actions[BACK] = {"method": "back", "role": "navigation", "label": "返回上一级，寻找搜索入口"}
                snapshot["options"][BACK] = self.actions[BACK]["label"]
                state["actionDescriptors"][BACK] = dict(self.actions[BACK])
        private_write(self.directory / f"state-{self.index}.json", json.dumps(snapshot, ensure_ascii=False))
        return snapshot

    def act(self, choice):
        if choice == "BLOCKED" and self.actions.get(choice, {}).get("method") == "blocked":
            self.operations.append({"name": "selected_action", "choice": choice,
                                    "label": self.actions[choice]["label"], "stage": self.progress.phase})
            raise TerminalRuntimeError("BLOCKED")
        if choice != BACK:
            return super().act(choice)
        if BACK not in self.actions or self.snapshot["state"]["stage"] != "search":
            raise RuntimeError("Back action is not authorized for this phase")
        if sum(item.get("name") == "selected_action" for item in self.operations) >= 24:
            raise RuntimeError("Action step budget exceeded")
        if time.perf_counter() - self.started > 150:
            raise RuntimeError("Trial time budget exceeded")
        self.operations.append({"name": "selected_action", "choice": choice,
                                "label": self.actions[BACK]["label"], "stage": "search"})
        self.call("android_press_back", {})
        start = time.perf_counter()
        time.sleep(0.25)
        self.phases.append({"kind": "wait", "name": "返回搜索入口后等待", "ms": (time.perf_counter() - start) * 1000})
        return self.observe()
