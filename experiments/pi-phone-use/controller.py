"""Shared MCP runtime and deterministic evidence verifier for PI's AgentLoop.

No model is selected here and no action sequence is executed automatically.
PI receives observed state and candidate actions; its custom provider chooses.
"""

from __future__ import annotations
import base64
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import time
from PIL import Image

from own_mcp import McpError, OwnMcp, own_nodes, content_text

from config import output_dir
from window_state import focused_package, verified_launcher

BASE = output_dir()
PACKAGE = "com.xingin.xhs"
TITLE = "当我闯进一个限时48小时的创造营"
QUERY = "小狮日记"
PHASE_GOALS = {
    "search": "打开小红书，搜索“小狮日记”，提交搜索并看到搜索结果。若输入框已是正确词，直接点搜索，不要反复输入。",
    "open_first": "打开搜索结果中最靠前的第一篇帖子（用户卡片下左侧首篇），标题应为“"
    + TITLE
    + "”。不要点用户资料或其它帖子。",
    "like": "给当前目标帖子点赞。liked必须变成true，已经满足则等待验证，不要反复点。",
    "collect": "收藏当前目标帖子。collected必须变成true，已经满足则等待验证，不要点点赞按钮。",
    "unlike": "取消当前目标帖子的点赞。liked必须变成false；保留收藏。",
    "uncollect": "取消当前目标帖子的收藏。collected必须变成false；保留未点赞。",
    "done": "全流程完成并已验证。",
}


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data) if isinstance(data, bytes) else path.write_text(data)
    os.chmod(path, 0o600)


def contained(a, b):
    return a[0] <= b[0] and a[1] <= b[1] and a[2] >= b[2] and a[3] >= b[3]


def prune_controls(controls):
    """Keep actionable leaves instead of an aggregate parent competing with them."""

    def area(b):
        return max(0, b[2] - b[0]) * max(0, b[3] - b[1])

    return [
        parent
        for parent in controls
        if not any(
            child is not parent
            and child["label"]
            and contained(parent["bounds"], child["bounds"])
            and 0 < area(child["bounds"]) < area(parent["bounds"]) * 0.98
            for child in controls
        )
    ]


def rebind_action(old, controls, before, after):
    """Resolve an unchanged intention only after a definite lookup-before-dispatch failure."""
    if before.get("kind") != after.get("kind"):
        return None
    if old.get("role") in ["like", "collect"]:
        if not before.get("firstTitleVisible") or not after.get("firstTitleVisible"):
            return None
        if any(before.get(k) != after.get(k) for k in ["liked", "collected"]):
            return None
    matches = [
        c
        for c in controls
        if c["role"] == old["role"]
        and c["method"] == old["method"]
        and c["label"] == old["label"]
        and c["bounds"] == old["bounds"]
    ]
    return matches[0] if len(matches) == 1 else None


REFERENCE_SCREEN_SIZE = (1080, 2340)


def screen_dimensions(text):
    """Read physical tree coordinates, independently of resized MCP image pixels."""
    matches = re.findall(r"^screen:(\d+)x(\d+)\b", text, flags=re.MULTILINE)
    if len(matches) != 1:
        raise RuntimeError("Physical screen dimensions missing or ambiguous")
    width, height = map(int, matches[0])
    if width <= 0 or height <= width:
        raise RuntimeError("Portrait physical screen dimensions required")
    return width, height


def reference_bounds(bounds, screen_size):
    """Normalize observation thresholds only; never replace actionable bounds."""
    width, height = screen_size
    if width <= 0 or height <= 0:
        raise ValueError("Screen dimensions must be positive")
    return [
        value * REFERENCE_SCREEN_SIZE[index % 2] / screen_size[index % 2]
        for index, value in enumerate(bounds)
    ]


def search_value(nodes, screen_size=REFERENCE_SCREEN_SIZE):
    for n in nodes:
        bounds = reference_bounds(n["bounds"], screen_size)
        if (
            n["package"] == PACKAGE
            and "on" in n["flags"]
            and 150 <= bounds[0] <= 200
            and bounds[1] < 300
            and n["class"] in ["EditText", "TextView"]
        ):
            text = n["text"].strip()
            return re.sub(r"^搜索[,，]\s*", "", text).strip()
    return None


def normalized_controls(nodes):
    out = []
    for n in nodes:
        if (
            n["package"] != PACKAGE
            or "on" not in n["flags"]
            or "ena" not in n["flags"]
            or not any(f in n["flags"] for f in ["clk", "lclk", "edt"])
        ):
            continue
        label = n["description"] or n["text"]
        if not label:
            values = []
            for child in nodes:
                value = child["description"] or child["text"]
                if (
                    child["package"] == PACKAGE
                    and value
                    and "on" in child["flags"]
                    and contained(n["bounds"], child["bounds"])
                    and value not in values
                ):
                    values.append(value)
            label = " | ".join(values)
        b = n["bounds"]
        out.append(
            {
                "id": n["id"],
                "label": label[:200],
                "bounds": b,
                "center": [(b[0] + b[2]) // 2, (b[1] + b[3]) // 2],
                "method": "type"
                if "EditText" in n["class"]
                else "click"
                if "clk" in n["flags"]
                else "tap",
                "role": "navigation",
            }
        )
    return prune_controls(out)


def identify_toolbar_like(controls, favorite, screen_size):
    """Resolve a unique like button adjacent to the verified favorite toolbar.

    Numeric counts or explicit like semantics are required. Absolute x positions
    are deliberately irrelevant; missing or competing evidence returns no target.
    """
    fx1, fy1, fx2, fy2 = favorite["bounds"]
    favorite_height = fy2 - fy1
    if favorite_height <= 0 or fx2 <= fx1 or screen_size[0] <= 0:
        return None
    label_pattern = re.compile(
        r"(?:\d+(?:\.\d+)?(?:万|千|[kKwW])?|"
        r"(?:已点赞|点赞|赞)(?:\s*\d+(?:\.\d+)?(?:万|千|[kKwW])?)?)"
    )
    candidates = []
    for control in controls:
        if control is favorite or control.get("method") not in {"click", "tap"}:
            continue
        if not label_pattern.fullmatch(control.get("label", "").strip()):
            continue
        x1, y1, x2, y2 = control["bounds"]
        height = y2 - y1
        if height <= 0 or x2 <= x1 or not 0.75 <= height / favorite_height <= 1.33:
            continue
        vertical_overlap = max(0, min(y2, fy2) - max(y1, fy1))
        same_row = vertical_overlap / min(height, favorite_height) >= 0.8
        adjacent_left = (x1 + x2) / 2 < fx1 and abs(fx1 - x2) <= screen_size[0] * 0.015
        if same_row and adjacent_left:
            candidates.append(control)
    return candidates[0] if len(candidates) == 1 else None


def icon_state(image, bounds, kind, screen_size=REFERENCE_SCREEN_SIZE):
    # Inner-left part of each toolbar button contains its icon. Excludes red node
    # rectangle borders and right-side numeric child nodes in annotated MCP images.
    if screen_size[0] <= 0 or screen_size[1] <= 0:
        raise ValueError("Screen dimensions must be positive")
    sx, sy = image.width / screen_size[0], image.height / screen_size[1]
    x1, y1, x2, y2 = bounds
    w, h = x2 - x1, y2 - y1
    roi = (
        int((x1 + 0.08 * w) * sx),
        int((y1 + 0.15 * h) * sy),
        int((x1 + 0.45 * w) * sx),
        int((y1 + 0.85 * h) * sy),
    )
    crop = image.convert("RGB").crop(roi)
    pixels = list(crop.get_flattened_data())
    if kind == "like":
        count = sum(r > 160 and r > g * 1.5 and r > b * 1.25 for r, g, b in pixels)
    else:
        count = sum(
            r > 155 and g > 100 and r > b * 1.5 and g > b * 1.4 and r < g * 1.65
            for r, g, b in pixels
        )
    ratio = count / max(1, len(pixels))
    return {
        "active": ratio > 0.12,
        "coloredFraction": round(ratio, 4),
        "roi": list(roi),
        "source": "MCP screenshot fixed-color toolbar verifier; no model vision call",
    }


class Progress:
    def __init__(self):
        self.phase = "search"
        self.milestones = []

    def update(self, v):
        previous = self.phase
        if self.phase == "search" and v.get("kind") == "results" and v.get("firstTitleVisible"):
            self.phase = "open_first"
        elif self.phase == "open_first" and v.get("kind") == "detail":
            self.phase = "like"
        elif self.phase == "like" and v.get("kind") == "detail" and v.get("liked") is True:
            self.phase = "collect"
        elif (
            self.phase == "collect"
            and v.get("kind") == "detail"
            and v.get("collected") is True
            and v.get("liked") is True
        ):
            self.phase = "unlike"
        elif (
            self.phase == "unlike"
            and v.get("kind") == "detail"
            and v.get("liked") is False
            and v.get("collected") is True
        ):
            self.phase = "uncollect"
        elif (
            self.phase == "uncollect"
            and v.get("kind") == "detail"
            and v.get("liked") is False
            and v.get("collected") is False
        ):
            self.phase = "done"
        if previous != self.phase:
            self.milestones.append(
                {
                    "stage": previous,
                    "next": self.phase,
                    "liked": v.get("liked"),
                    "collected": v.get("collected"),
                    "at": datetime.now().astimezone().isoformat(),
                }
            )


def allowed_choice(choice, actions, phase, observation):
    if choice not in actions:
        raise ValueError("Unknown action")
    a = actions[choice]
    label = a.get("label", "")
    if label in ["关注", "分享", "发布", "立即安装"] or "评论" in label or "说点什么" in label:
        raise ValueError("Out-of-scope action")
    role = a.get("role", "navigation")
    if role == "like" and phase not in ["like", "unlike"]:
        raise ValueError("Like toggle outside its stage")
    if role == "collect" and phase not in ["collect", "uncollect"]:
        raise ValueError("Favorite toggle outside its stage")
    if phase in ["like", "unlike", "collect", "uncollect"] and role not in [
        "like",
        "collect",
        "wait",
        "blocked",
    ]:
        raise ValueError("Navigation outside toggle stages")
    return a


def toggle_stage_candidates(actions, phase, observation):
    """Expose only the unsatisfied current toggle with its counterpart verified.

    A transient screenshot may satisfy one flag while the other is inconsistent.
    Keep that observation and its milestone unchanged; PI can wait for a new read.
    """
    stages = {
        "like": ("like", False, False),
        "collect": ("collect", True, False),
        "unlike": ("like", True, True),
        "uncollect": ("collect", False, True),
    }
    if phase not in stages:
        return actions
    role, liked, collected = stages[phase]
    ready = observation.get("liked") is liked and observation.get("collected") is collected
    return {
        key: action
        for key, action in actions.items()
        if action.get("role") in {"wait", "blocked"}
        or (ready and action.get("role") == role)
    }


class PhoneRuntime:
    def __init__(self, trial_id):
        if not isinstance(trial_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", trial_id
        ):
            raise ValueError("Trial ID must be a simple filename")
        self.started = time.perf_counter()
        self.trial_id = trial_id
        self.directory = BASE / trial_id
        if self.directory.exists():
            raise RuntimeError("Trial ID exists; refusing replay")
        self.directory.mkdir(parents=True, mode=0o700)
        self.phases = []
        self.operations = []
        self.model_calls = []
        self.progress = Progress()
        self.actions = {}
        self.snapshot = None
        self.index = 0
        self.error = None
        self.stale_recoveries = 0
        self.direct_rebindings = 0
        self.started_at = datetime.now().astimezone().isoformat()
        start = time.perf_counter()
        self.mcp = OwnMcp()
        self.setup_ms = (time.perf_counter() - start) * 1000
        self.phases.append({"kind": "mcp", "name": "MCP初始化与握手", "ms": self.setup_ms})
        self.server_info = self.mcp.server_info

    def call(self, name, args):
        start = time.perf_counter()
        status = "success"
        try:
            return self.mcp.call(name, args)
        except Exception:
            status = "error"
            raise
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            self.phases.append({"kind": "mcp", "name": name, "ms": elapsed})
            self.operations.append(
                {
                    "index": len(self.operations) + 1,
                    "name": name,
                    "arguments": args,
                    "ms": elapsed,
                    "status": status,
                }
            )

    def observe(self):
        result = self.call("android_get_screen_state", {"include_screenshot": True})
        text = content_text(result)
        physical_size = screen_dimensions(text)
        nodes = own_nodes(text)
        focused_xhs = focused_package(text) == PACKAGE
        controls = normalized_controls(nodes) if focused_xhs else []

        def observed_bounds(control):
            return reference_bounds(control["bounds"], physical_size)

        self.index += 1
        private_write(self.directory / f"observation-{self.index}.txt", text)
        image = None
        for item in result.get("content", []):
            if item.get("type") == "image":
                data = base64.b64decode(item["data"])
                image = Image.open(io.BytesIO(data))
                private_write(self.directory / f"observation-{self.index}.jpg", data)
        texts = [
            n["description"] or n["text"]
            for n in nodes
            if focused_xhs
            and n["package"] == PACKAGE
            and "on" in n["flags"]
            and (n["description"] or n["text"])
        ]
        in_xhs = focused_xhs and any(n["package"] == PACKAGE and "on" in n["flags"] for n in nodes)
        detail = (
            any("作者小狮日记" in t for t in texts)
            and any(TITLE in t for t in texts)
            and any(
                observed_bounds(c)[1] > 2100 and c["label"].startswith(("收藏", "已收藏"))
                for c in controls
            )
        )
        results = not detail and any(TITLE in t for t in texts) and any(t == "综合" for t in texts)
        fields = [c for c in controls if c["method"] == "type"]
        query_fields = [
            c
            for c in controls
            if observed_bounds(c)[1] < 300
            and 150 <= observed_bounds(c)[0] <= 200
            and 650 <= observed_bounds(c)[2] <= 800
            and c["label"]
        ]
        for c in query_fields:
            c["role"] = "search_field"
            c["label"] = "编辑搜索框；当前显示：" + c["label"]
        kind = (
            "detail"
            if detail
            else "results"
            if results
            else "search_input"
            if fields
            else "search_results_other"
            if query_fields
            else "feed"
            if in_xhs
            else "launcher"
            if verified_launcher(text) is not None
            else "transition"
        )
        state = {
            "kind": kind,
            "observedQuery": search_value(nodes, physical_size) if in_xhs else None,
            "queryMatches": in_xhs and search_value(nodes, physical_size) == QUERY,
            "firstTitleVisible": any(TITLE in t for t in texts),
            "liked": None,
            "collected": None,
            "evidence": {},
        }
        if detail:
            favorite = next(
                c
                for c in controls
                if observed_bounds(c)[1] > 2100 and c["label"].startswith(("收藏", "已收藏"))
            )
            like = identify_toolbar_like(controls, favorite, physical_size)
            if not like or image is None:
                raise RuntimeError("Toolbar nodes or MCP screenshot missing")
            for control, role in [(like, "like"), (favorite, "collect")]:
                ev = icon_state(image, control["bounds"], role, screen_size=physical_size)
                state["evidence"][role] = ev
                active = ev["active"]
                state["liked" if role == "like" else "collected"] = active
                control["role"] = role
                control["label"] = (
                    ("点赞" if role == "like" else "收藏")
                    + "按钮；"
                    + ("已激活" if active else "未激活")
                    + "；共同图标验证器"
                )
        self.progress.update(state)
        actions = {
            "WAIT": {"method": "wait", "role": "wait", "label": "等待界面稳定再观察"},
            "BLOCKED": {
                "method": "blocked",
                "role": "blocked",
                "label": "无法继续；停止并记录原因",
            },
        }
        if kind == "search_results_other" and state["observedQuery"] != QUERY:
            actions.pop("WAIT", None)
        if kind == "launcher":
            actions["OPEN_APP"] = {
                "method": "open",
                "role": "navigation",
                "label": "打开小红书 com.xingin.xhs",
            }
        elif in_xhs:
            for c in controls:
                label = c["label"]
                if (
                    not label
                    or any(x in label for x in ["说点什么", "评论", "分享", "立即安装"])
                    or label == "关注"
                ):
                    continue
                if kind == "search_input" and observed_bounds(c)[1] > 300:
                    continue
                if detail and c["role"] not in ["like", "collect"]:
                    continue
                if kind in ["results", "search_results_other"] and not (
                    TITLE in label or label in ["搜索", "返回"] or c["role"] == "search_field"
                ):
                    continue
                if (
                    kind == "feed"
                    and label not in ["搜索", "首页", "稍后再说", "返回"]
                    and c["role"] != "search_field"
                ):
                    continue
                prefix = "TYPE" if c["method"] == "type" else "CLICK"
                actions[prefix + ":" + c["id"]] = c
        actions = toggle_stage_candidates(actions, self.progress.phase, state)
        self.actions = actions
        self.snapshot = {
            "state": {
                "goal": "从启动小红书开始，搜索小狮日记，打开第一篇帖子，依次点赞、收藏、取消点赞、取消收藏，验证闭环。",
                "stage": self.progress.phase,
                "nextGoal": PHASE_GOALS[self.progress.phase],
                "observation": state,
                "query": QUERY,
                "expectedFirstTitle": TITLE,
                "actionDescriptors": {
                    choice: {key: action.get(key) for key in ("method", "role", "label")}
                    for choice, action in actions.items()
                },
                "visibleControls": [
                    {"id": c.get("id"), "label": c["label"], "center": c.get("center")}
                    for c in actions.values()
                    if c.get("id")
                ],
                "recentActions": [
                    {"stage": x["stage"], "action": x["label"]}
                    for x in self.operations
                    if x.get("name") == "selected_action"
                ][-4:],
            },
            "options": {
                k: (
                    "在搜索框清空后填入固定搜索词小狮日记；当前值：" + v["label"]
                    if v["method"] == "type"
                    else "点击：" + v["label"]
                    if v["method"] in ["click", "tap"]
                    else v["label"]
                )
                for k, v in actions.items()
            },
            "done": self.progress.phase == "done",
        }
        private_write(
            self.directory / f"state-{self.index}.json",
            json.dumps(self.snapshot, ensure_ascii=False, indent=2),
        )
        return self.snapshot

    def act(self, choice):
        action = allowed_choice(choice, self.actions, self.progress.phase, self.snapshot)
        if sum(o.get("name") == "selected_action" for o in self.operations) >= 24:
            raise RuntimeError("Action step budget exceeded")
        if time.perf_counter() - self.started > 150:
            raise RuntimeError("Trial time budget exceeded")
        self.operations.append(
            {
                "name": "selected_action",
                "choice": choice,
                "label": action["label"],
                "stage": self.progress.phase,
            }
        )
        before = dict(self.snapshot["state"]["observation"])
        method = action["method"]
        if method == "blocked":
            raise RuntimeError("Model selected BLOCKED")
        successful_mutations = 0

        def mutate(name, arguments):
            nonlocal successful_mutations
            result = self.call(name, arguments)
            successful_mutations += 1
            return result

        try:
            if method == "open":
                mutate("android_open_app", {"package_id": PACKAGE})
            elif method == "click":
                mutate("android_click_node", {"node_id": action["id"]})
            elif method == "tap":
                mutate("android_tap", {"x": action["center"][0], "y": action["center"][1]})
            elif method == "type":
                mutate("android_type_clear_text", {"node_id": action["id"]})
                # Clearing may change the opaque reference; resolve the current search field.
                fresh = self.call("android_get_screen_state", {"include_screenshot": False})
                field = next(
                    n
                    for n in own_nodes(content_text(fresh))
                    if n["package"] == PACKAGE and "on" in n["flags"] and n["class"] == "EditText"
                )
                mutate(
                    "android_type_append_text",
                    {
                        "node_id": field["id"],
                        "text": QUERY,
                        "typing_speed": 10,
                        "typing_speed_variance": 0,
                    },
                )
            elif method != "wait":
                raise RuntimeError("Unsupported action")
        except McpError as error:
            # This MCP error guarantees target lookup failed before an action dispatch.
            # Re-observe and let PI request a new model decision; never repeat a toggle
            # automatically after a timeout or an ambiguous delivery error.
            canonical_stale = "Node not found in accessibility tree (ToolError; stage=tool_call)"
            if (
                error.stage != "tool_call"
                or error.exception_type != "ToolError"
                or str(error) != canonical_stale
                or successful_mutations
            ):
                # A clear followed by a failed append is a partially applied group.
                # Never label it as undelivered or retry it automatically.
                raise
            self.stale_recoveries += 1
            self.operations.append(
                {
                    "name": "stale_reference",
                    "stage": self.progress.phase,
                    "choice": choice,
                    "recovery": "Fresh observation; PI must request another model decision",
                }
            )
            fresh = self.observe()
            rebound = rebind_action(
                action, list(self.actions.values()), before, fresh["state"]["observation"]
            )
            if rebound and method == "click":
                # The first request failed during lookup, so it performed no toggle. Rebind
                # once only when role, geometry, label, target identity and state agree.
                self.direct_rebindings += 1
                self.operations.append(
                    {
                        "name": "direct_reference_rebind",
                        "stage": self.progress.phase,
                        "oldId": action["id"],
                        "newId": rebound["id"],
                        "label": action["label"],
                    }
                )
                mutate("android_click_node", {"node_id": rebound["id"]})
                wait_start = time.perf_counter()
                time.sleep(0.4)
                self.phases.append(
                    {
                        "kind": "wait",
                        "name": "重定位动作后界面等待",
                        "ms": (time.perf_counter() - wait_start) * 1000,
                    }
                )
                return self.observe()
            fresh["state"]["lastActionError"] = (
                "旧节点引用已失效，动作没有执行。请选择新观察中的节点重新完成当前阶段。"
            )
            execution = {
                "status": "not_dispatched",
                "reason": "stale_reference",
                "choice": choice,
                "intent": {key: action.get(key) for key in ("method", "role", "label")},
            }
            fresh["execution"] = execution
            self.snapshot = fresh
            self.operations.append({"name": "execution", "stage": self.progress.phase, **execution})
            private_write(
                self.directory / f"state-{self.index}.json",
                json.dumps(fresh, ensure_ascii=False, indent=2),
            )
            return fresh
        wait_start = time.perf_counter()
        time.sleep(0.4 if method not in ["open", "type"] else 0.6)
        self.phases.append(
            {
                "kind": "wait",
                "name": "界面稳定等待",
                "ms": (time.perf_counter() - wait_start) * 1000,
            }
        )
        return self.observe()

    def record_model(self, result):
        self.phases.append({"kind": "model", "name": "模型决策请求", "ms": result["latencyMs"]})
        public = {k: v for k, v in result.items() if k != "rawAnswer"}
        public["stage"] = self.progress.phase
        public["observation"] = self.index
        self.model_calls.append(public)
        private_write(
            self.directory / f"decision-{len(self.model_calls)}.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )

    def finish(self, extra=None):
        elapsed = (time.perf_counter() - self.started) * 1000
        other = max(0, elapsed - sum(p["ms"] for p in self.phases))
        phases = self.phases + [
            {"kind": "other", "name": "共同验证器、PI往返与控制器开销", "ms": other}
        ]
        selected_actions = [o for o in self.operations if o.get("name") == "selected_action"]
        result = {
            "trialId": self.trial_id,
            "startedAt": self.started_at,
            "completedAt": datetime.now().astimezone().isoformat(),
            "status": "success" if self.progress.phase == "done" else "failed",
            "elapsedMs": elapsed,
            "phases": phases,
            "operations": self.operations,
            "modelCalls": self.model_calls,
            "milestones": self.progress.milestones,
            "finalState": self.snapshot["state"]["observation"] if self.snapshot else None,
            "serverInfo": self.server_info,
            "error": self.error,
            "retries": {
                "directReferenceRebindings": self.direct_rebindings,
                "staleReferenceRecoveries": self.stale_recoveries,
                "automaticModelRetries": 0,
                "automaticActionRetries": 0,
                "modelDirectedRepeatedActions": len(selected_actions)
                - len({(o.get("stage"), o.get("label")) for o in selected_actions}),
                "waitChoices": sum(o.get("choice") == "WAIT" for o in selected_actions),
            },
            **(extra or {}),
        }
        try:
            private_write(
                self.directory / "result.json", json.dumps(result, ensure_ascii=False, indent=2)
            )
        finally:
            self.mcp.close()
        return result
