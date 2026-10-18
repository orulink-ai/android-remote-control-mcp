"""Model-selected XHS task family using live AX and shared screenshot evidence.

Experience supplies page/toolbar expectations, never an automatic action macro.
"""

from __future__ import annotations

import base64
from datetime import datetime
import io
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

from PIL import Image

from config import output_dir
from controller import (
    PACKAGE,
    PhoneRuntime,
    contained,
    icon_state,
    identify_toolbar_like,
    normalized_controls,
    private_write,
    screen_dimensions,
    search_value,
)
from experience_tasks import validate_experience_task
from own_mcp import McpError, OwnMcp, content_text, own_nodes
from terminal_runtime import _controls, _query_bars, _search_fields, screen_locked
from window_state import focused_package


def _spaced(value):
    return " ".join(value.split())


def _card_matches_title(card_title, requested):
    requested = caption_title(requested)
    if not requested or not card_title:
        return False
    if card_title == requested:
        return True
    # Navigation candidate only. Detail must independently prove the exact
    # requested title before any interaction becomes available.
    prefix = _spaced(requested) + " "
    merged = _spaced(card_title)
    return merged.startswith(prefix) and len(merged[len(prefix):]) >= 32


def _photo_authors(nodes, size):
    headers = [node for node in nodes if node["package"] == PACKAGE
               and "on" in node["flags"] and "ena" in node["flags"]
               and node["class"] == "TextView"
               and node["resource_id"] == PACKAGE + ":id/nickNameTV"
               and node["bounds"][1] < size[1] * .2 and node["text"].strip()]
    if len(headers) > 1:
        return None
    return {headers[0]["text"].strip()} if headers else set()


def _photo_identity(nodes, size, raw_caption):
    """Prove a selected merged card by exact title + adjacent long body prefix."""
    card = _spaced(caption_title(raw_caption) or "")
    width, height = size
    texts = [node for node in nodes if node["package"] == PACKAGE
             and "on" in node["flags"] and node["class"] == "TextView"
             and node["text"] and node["bounds"][2]-node["bounds"][0] >= .7*width]
    matches = []
    for title_node in texts:
        title = caption_title(title_node["text"])
        if not title or len(title) < 4 or title_node["bounds"][3]-title_node["bounds"][1] > height*.12:
            continue
        prefix = _spaced(title) + " "
        if not card.startswith(prefix):
            continue
        summary = card[len(prefix):]
        if len(summary) < 32:
            continue
        for body in texts:
            if (body is title_node
                    or not 0 <= body["bounds"][1]-title_node["bounds"][3] <= height*.04
                    or abs(body["bounds"][0]-title_node["bounds"][0]) > width*.02
                    or abs(body["bounds"][2]-title_node["bounds"][2]) > width*.05
                    or not _spaced(body["text"]).startswith(summary)):
                continue
            matches.append({
                "source": "xhs-adjacent-title-body-v1", "title": title,
                "titleLabel": title_node["text"], "bodyLabel": body["text"],
                "cardLabel": raw_caption, "matchedBodyPrefix": summary,
                "titleBounds": title_node["bounds"], "bodyBounds": body["bounds"],
            })
    return matches[0] if len(matches) == 1 else None


class ExperienceRuntimeError(RuntimeError):
    """Stable diagnostics without transport secrets or untrusted server prose."""

    MESSAGES = {
        "LOCKED": "手机已锁屏，请解锁后开启新任务。",
        "UNKNOWN_FOCUS": "无法确认唯一前台窗口，已停止。",
        "BLOCKED": "没有可安全执行的候选操作，已停止。",
        "NO_PROGRESS": "同一界面连续三次未取得进展，已停止。",
        "BUDGET": "任务动作或时间预算已用完，已停止。",
        "DELIVERY_UNCERTAIN": "动作投递结果不确定，已只读回查并停止，请检查当前状态后新建任务。",
        "TOGGLE_UNVERIFIED": "动作后未获得同帖截图配对验证，已停止，不重复点击。",
        "TARGET_CHANGED": "目标帖子或未授权修改的状态发生变化，已停止。",
        "STOPPED": "任务已经停止，不能继续派发操作。",
    }

    def __init__(self, code):
        self.code = code
        super().__init__(self.MESSAGES[code])


# Public compatibility alias; task authority has a single canonical validator.
parse_experience_task = validate_experience_task


def _labels(nodes):
    return [
        node["description"] or node["text"]
        for node in nodes
        if node["package"] == PACKAGE
        and "on" in node["flags"]
        and (node["description"] or node["text"])
    ]


def caption_title(label):
    """Separate caption metadata at whitespace+#, never by fuzzy substring."""
    if not isinstance(label, str):
        return None
    title = re.split(r"\s+#", label.strip(), maxsplit=1)[0].strip()
    return title if title and not title.startswith("#") else None


def _public_label(label):
    label = caption_title(label)
    return (
        label is not None
        and not re.fullmatch(
            r"[\d\s.,:万亿千百/\-]+|(?:今天|昨天|前天)(?:\s.*)?|(?:点赞|收藏|已点赞|已收藏)\d*",
            label,
        )
        and label not in {"关注", "已关注", "搜索", "综合", "全部", "用户", "返回", "首页"}
    )


def result_cards(nodes, size):
    """Bounded carousel/masonry adapter; ambiguous/missing cards fail closed."""
    width, height = size
    visible = [
        node
        for node in nodes
        if node["package"] == PACKAGE
        and "on" in node["flags"]
        and "ena" in node["flags"]
        and 0 <= node["bounds"][0] < node["bounds"][2] <= width
        and 0 <= node["bounds"][1] < node["bounds"][3] <= height
    ]
    containers = [
        node
        for node in visible
        if "clk" in node["flags"]
        and node["class"] in {"ViewGroup", "FrameLayout", "LinearLayout", "RelativeLayout"}
        and 0.2 * width <= node["bounds"][2] - node["bounds"][0] <= 0.8 * width
        and node["bounds"][3] - node["bounds"][1] >= 0.12 * height
        and node["bounds"][1] >= 0.15 * height
    ]
    # Keep unreadable outer cards as barriers: "first" cannot silently mean
    # the second card merely because only its title was exposed by AX.
    outer = [
        parent
        for parent in containers
        if not any(
            other is not parent
            and other["bounds"] != parent["bounds"]
            and contained(other["bounds"], parent["bounds"])
            for other in containers
        )
    ]
    cards = []
    for parent in outer:
        children = [
            node
            for node in visible
            if node is not parent and contained(parent["bounds"], node["bounds"])
        ]
        titles = sorted(
            [
                node
                for node in children
                if node["class"] == "TextView" and _public_label(node["text"])
            ],
            key=lambda node: (node["bounds"][1], node["bounds"][0]),
        )
        first_y = titles[0]["bounds"][1] if titles else None
        raw_label = (
            titles[0]["text"]
            if titles and sum(abs(node["bounds"][1] - first_y) < 2 for node in titles) == 1
            else None
        )
        title = caption_title(raw_label)
        cards.append(
            {
                "id": parent["id"],
                "bounds": parent["bounds"],
                "method": "click",
                "role": "open_target",
                "targetTitle": title,
                "rawLabel": raw_label,
                "memberLabels": [node["description"] or node["text"] for node in children],
                "label": "打开本次搜索结果帖子：" + title
                if title
                else "首卡标题缺少明确证据，禁止打开",
            }
        )
    return sorted(cards, key=lambda card: (card["bounds"][1], card["bounds"][0]))


def _title_labels(nodes, title):
    title = caption_title(title)
    if title is None:
        return []
    return list(
        dict.fromkeys(
            label
            for node in nodes
            if node["package"] == PACKAGE and "on" in node["flags"]
            for label in (node["text"], node["description"])
            if label and caption_title(label) == title
        )
    )[:4]


def _title_visible(nodes, title):
    return bool(_title_labels(nodes, title))


class ExperienceRuntime(PhoneRuntime):
    """Goal-derived stages with exactly one model-selected action per act()."""

    def __init__(self, trial_id, task, *, base=None):
        self.task = parse_experience_task(task)
        if not isinstance(trial_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", trial_id
        ):
            raise ValueError("Invalid terminal trial id")
        self.started = time.perf_counter()
        self.started_at = datetime.now().astimezone().isoformat()
        self.trial_id = trial_id
        self.directory = Path(base or output_dir()) / trial_id
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.phases, self.operations, self.model_calls = [], [], []
        self.progress = SimpleNamespace(phase="search", milestones=[])
        self.actions, self.snapshot, self.index = {}, None, 0
        self.error = None
        self.stale_recoveries = self.direct_rebindings = 0
        self.bound_target = self.initial_state = None
        self.desired_verified = False
        self._query_submitted = False
        self.no_progress = {}
        self.pending_toggle = None
        self.stopped = False
        start = time.perf_counter()
        self.mcp = OwnMcp()
        self.server_info = self.mcp.server_info
        self.setup_ms = (time.perf_counter() - start) * 1000
        self.phases.append({"kind": "mcp", "name": "MCP 初始化", "ms": self.setup_ms})

    def _stop(self, code):
        self.error = ExperienceRuntimeError.MESSAGES[code]
        self.stopped = True
        self.progress.phase = "failed"
        self.actions = {}
        if self.snapshot:
            self.snapshot["done"] = False
            self.snapshot["options"] = {}
            self.snapshot["state"]["actionDescriptors"] = {}
            self.snapshot["state"]["stopCode"] = code
            self.snapshot["state"]["stage"] = "failed"
            self._save()
        self.operations.append({"name": "experience_stop", "code": code})
        raise ExperienceRuntimeError(code)

    def _save(self):
        private_write(
            self.directory / f"state-{self.index}.json",
            json.dumps(self.snapshot, ensure_ascii=False, indent=2),
        )

    def _retained_first_results(self, observation):
        return (
            self.task["target"]["kind"] == "first"
            and not self._query_submitted
            and observation.get("kind") == "search_results"
            and observation.get("queryMatches") is True
        )

    def _budget(self):
        if self.stopped:
            raise ExperienceRuntimeError("STOPPED")
        if (
            sum(item.get("name") == "selected_action" for item in self.operations) >= 32
            or time.perf_counter() - self.started > 180
        ):
            self._stop("BUDGET")

    def _capture_frame(self, focus_attempt):
        """Keep AX, focus and image from one response under one observation id."""
        result = self.call("android_get_screen_state", {"include_screenshot": True})
        text = content_text(result)
        self.index += 1
        private_write(self.directory / f"observation-{self.index}.txt", text)
        nodes = own_nodes(text)
        package = focused_package(text)
        image = None
        images = [item for item in result.get("content", []) if item.get("type") == "image"]
        if len(images) == 1:
            try:
                image_bytes = base64.b64decode(images[0]["data"], validate=True)
                image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                private_write(self.directory / f"observation-{self.index}.jpg", image_bytes)
            except (KeyError, ValueError, OSError):
                image = None
        self.operations.append(
            {
                "name": "focus_observation",
                "observation": self.index,
                "focusAttempt": focus_attempt,
                "focusedPackage": package,
                "locked": screen_locked(nodes),
                "pairedScreenshot": image is not None,
                "readOnly": True,
            }
        )
        return text, nodes, package, image

    def observe(self):
        if self.stopped:
            raise ExperienceRuntimeError("STOPPED")
        for attempt in range(4):
            text, nodes, package, image = self._capture_frame(attempt)
            # Never wait through a lock screen, reuse an older package, or pair
            # a newly focused tree with an earlier transient frame's screenshot.
            if package is not None or screen_locked(nodes) or attempt == 3:
                break
            self.operations.append(
                {
                    "name": "focus_stabilization",
                    "attempt": attempt + 1,
                    "observation": self.index,
                    "readOnly": True,
                }
            )
            start = time.perf_counter()
            time.sleep(0.25)
            self.phases.append(
                {
                    "kind": "wait",
                    "name": "前台焦点稳定只读等待",
                    "ms": (time.perf_counter() - start) * 1000,
                }
            )
        labels = _labels(nodes)
        observation = {
            "kind": "screen",
            "focusedPackage": package,
            "locked": screen_locked(nodes),
            "liked": None,
            "collected": None,
            "evidence": {},
            "visibleTexts": labels[:120],
            "queryMatches": False,
            "targetVerified": False,
        }
        self.snapshot = {
            "state": {
                "goal": self.task["goal"],
                "stage": self.progress.phase,
                "observation": observation,
            },
            "options": {},
            "done": False,
        }
        if observation["locked"]:
            self._stop("LOCKED")
        if package is None:
            self._stop("UNKNOWN_FOCUS")
        size = screen_dimensions(text)
        controls = normalized_controls(nodes) if package == PACKAGE else []
        actions = {
            "WAIT": {
                "method": "wait",
                "role": "wait",
                "label": "等待当前界面稳定后重新观察（最多连续三次无进展）",
            },
            "BLOCKED": {
                "method": "blocked",
                "role": "blocked",
                "label": "无法安全继续，停止并保留证据",
            },
        }
        if package != PACKAGE:
            if self.bound_target:
                self._stop("TARGET_CHANGED")
            actions["OPEN_APP:" + PACKAGE] = {
                "method": "open",
                "role": "navigation",
                "label": "打开小红书进行当前任务",
                "package": PACKAGE,
            }
        elif self.bound_target:
            self._detail(nodes, controls, size, image, observation, actions)
        else:
            self._search(nodes, controls, text, labels, observation, actions)
        observation["firstTitleVisible"] = observation["targetVerified"]
        self.actions = {} if self.progress.phase == "done" else actions
        next_goal = {
            "search": f"从当前页面正常导航，搜索“{self.task['query']}”，打开符合本次 target 的帖子；不要修改互动状态。",
            "desired": f"仅将明确授权的状态设置为 {json.dumps(self.task['desired'], ensure_ascii=False)}；已满足则不点击。",
            "cancel": "正向目标已验证；仅取消本次 desired 中指定的项目，逐项回读验证为 false。",
            "done": "本次显式目标均已回读验证。",
        }[self.progress.phase]
        goal = self.task["goal"]
        if self._retained_first_results(observation):
            next_goal = (
                "当前是保留结果，需打开查询输入框，核对当前值后重新提交本次查询"
                f"“{self.task['query']}”，以定位全局首篇。下一步选择编辑查询候选；等待旧结果不会完成这次查询提交。"
            )
            goal += "\n当前阶段：" + next_goal
        self.snapshot = {
            "state": {
                "goal": goal,
                "nextGoal": next_goal,
                "taskKind": "experience",
                "stage": self.progress.phase,
                "query": self.task["query"],
                "appName": "小红书",
                "target": self.task["target"],
                "desired": self.task["desired"],
                "restoreAfter": self.task["restoreAfter"],
                "querySubmittedThisRun": self._query_submitted,
                "boundTarget": self.bound_target,
                "initialState": self.initial_state,
                "focusedPackage": package,
                "locked": False,
                "observation": observation,
                "screenText": labels[:120],
                "experience": {
                    "source": "live-xhs-semantic-family-v1",
                    "execution": "model-selected-every-step",
                    "usesRecordedCoordinates": False,
                },
                "actionDescriptors": {
                    key: {name: action.get(name) for name in ("method", "role", "label")}
                    for key, action in self.actions.items()
                },
                "visibleControls": [
                    {
                        "id": action.get("id"),
                        "label": action["label"],
                        "center": action.get("center"),
                    }
                    for action in self.actions.values()
                    if action.get("id")
                ],
            },
            "options": {key: action["label"] for key, action in self.actions.items()},
            "done": self.progress.phase == "done",
        }
        self._save()
        return self.snapshot

    def _search(self, nodes, controls, text, labels, observed, actions):
        fields = _search_fields(_controls(nodes, PACKAGE), text)
        query = search_value(nodes, screen_dimensions(text))
        if len(fields) == 1:
            raw_field = next(node for node in nodes if node["id"] == fields[0]["id"])
            query = re.sub(r"^搜索[,，]\s*", "", raw_field["text"].strip()).strip()
        observed.update(observedQuery=query, queryMatches=query == self.task["query"])
        is_results = "综合" in labels and query == self.task["query"] and not fields
        observed["kind"] = (
            "search_results" if is_results else "search_input" if fields else "screen"
        )
        if is_results:
            cards = result_cards(nodes, screen_dimensions(text))
            target = self.task["target"]
            if target["kind"] == "first":
                cards = cards[:1] if self._query_submitted else []
            else:
                cards = [
                    card
                    for card in cards
                    if _card_matches_title(card["targetTitle"], target["title"])
                    and (
                        "author" not in target
                        or any(
                            label in {target["author"], "作者" + target["author"]}
                            for label in card["memberLabels"]
                        )
                    )
                ]
            if len(cards) == 1 and cards[0]["targetTitle"] is not None:
                card = cards[0]
                actions["CLICK:" + card["id"]] = card
            observed["resultCandidates"] = [
                {
                    "title": card["targetTitle"],
                    "rawLabel": card["rawLabel"],
                    "bounds": card["bounds"],
                }
                for card in cards
            ]
        for control in controls:
            if control["label"] == "搜索" and (not fields or query == self.task["query"]):
                if (
                    is_results
                    and self.task["target"]["kind"] == "first"
                    and not self._query_submitted
                ):
                    continue
                actions["CLICK:" + control["id"]] = {
                    **control,
                    "role": "search_submit" if fields else "search_entry",
                }
        if len(fields) == 1 and query != self.task["query"]:
            field = fields[0]
            actions["TYPE:" + field["id"]] = {
                **field,
                "role": "search_field",
                "label": "输入本次搜索词：" + self.task["query"],
            }
        if len(fields) == 1 and query == self.task["query"]:
            actions["CLICK:experience-enter"] = {
                "method": "key",
                "role": "search_submit",
                "label": "提交当前正确搜索词（ENTER）",
            }
        if not fields:
            for bar in _query_bars(_controls(nodes, PACKAGE), nodes, text, query):
                if self._retained_first_results(observed):
                    bar["label"] = (
                        f"打开查询输入框，重新提交“{self.task['query']}”以定位本次全局首篇"
                    )
                actions["CLICK:" + bar["id"]] = bar
        actions["CLICK:experience-back"] = {
            "method": "back",
            "role": "back",
            "label": "返回上一级，寻找当前任务的搜索入口",
        }

    def _detail(self, nodes, controls, size, image, observed, actions):
        title = self.bound_target["title"]
        labels = _labels(nodes)
        author = self.bound_target.get("author")
        authors = {label[2:] for label in labels if label.startswith("作者") and len(label) > 2}
        photo_authors = _photo_authors(nodes, size)
        if photo_authors is None:
            authors = set()
        else:
            authors |= photo_authors
        visible_author = authors == {author} if author else len(authors) == 1
        identity = None
        title_matches = _title_visible(nodes, title)
        if not title_matches and visible_author and self.initial_state is None:
            identity = _photo_identity(nodes, size, self.bound_target["rawLabel"])
            title_matches = identity is not None and (
                self.task["target"]["kind"] == "first"
                or identity["title"] == caption_title(self.task["target"]["title"])
            )
        favorites = [
            control
            for control in controls
            if re.fullmatch(r"(?:已)?收藏(?:\s*[\d.万千亿,]+)?", control["label"].strip())
            and control["bounds"][1] > size[1] * 0.8
        ]
        if not (
            title_matches
            and visible_author
            and len(favorites) == 1
            and image is not None
        ):
            observed["kind"] = "target_pending_verification"
            return
        favorite = favorites[0]
        like = identify_toolbar_like(controls, favorite, size)
        if like is None:
            observed["kind"] = "ambiguous_toolbar"
            return
        if identity and title_matches:
            self.bound_target["cardTitle"] = self.bound_target["title"]
            self.bound_target["title"] = title = identity["title"]
            self.bound_target["photoIdentity"] = identity
        like_evidence = icon_state(image, like["bounds"], "like", size)
        collect_evidence = icon_state(image, favorite["bounds"], "collect", size)
        observed.update(
            kind="detail",
            targetVerified=True,
            liked=like_evidence["active"],
            collected=collect_evidence["active"],
        )
        observed["evidence"] = {
            "liked": like_evidence,
            "collected": collect_evidence,
            "targetTitle": title,
            "targetLabels": _title_labels(nodes, title),
            "photoIdentity": self.bound_target.get("photoIdentity"),
            "author": author,
            "observation": self.index,
        }
        if self.initial_state is None:
            self.bound_target["author"] = next(iter(authors))
            self.initial_state = {key: observed[key] for key in ("liked", "collected")}
            self.operations.append(
                {
                    "name": "initial_target_verification",
                    "observation": self.index,
                    "target": dict(self.bound_target),
                    "state": dict(self.initial_state),
                    "evidence": observed["evidence"],
                }
            )
        for key in {"liked", "collected"} - self.task["desired"].keys():
            if observed[key] != self.initial_state[key]:
                self._stop("TARGET_CHANGED")
        if self.pending_toggle:
            pending = self.pending_toggle
            if observed[pending["field"]] != pending["expected"]:
                # ACK can precede a rendered frame. No choices or milestones
                # advance until bounded, read-only settling sees the target.
                observed["kind"] = "toggle_settling"
                return
            self.operations.append(
                {
                    "name": "toggle_verification",
                    **pending,
                    "after": observed[pending["field"]],
                    "afterObservation": self.index,
                    "evidence": observed["evidence"],
                }
            )
            self.pending_toggle = None
        desired = self.task["desired"]
        if not self.desired_verified and all(
            observed[key] == value for key, value in desired.items()
        ):
            self.desired_verified = True
            self.progress.milestones.append(
                {
                    "stage": "desired",
                    "observation": self.index,
                    "state": {key: observed[key] for key in desired},
                    "evidence": observed["evidence"],
                }
            )
        final = (
            {key: False for key in desired}
            if self.desired_verified and self.task["restoreAfter"]
            else desired
        )
        self.progress.phase = (
            "cancel" if self.desired_verified and self.task["restoreAfter"] else "desired"
        )
        if self.desired_verified and all(observed[key] == value for key, value in final.items()):
            if self.task["restoreAfter"]:
                self.progress.milestones.append(
                    {
                        "stage": "cancel",
                        "observation": self.index,
                        "state": {key: observed[key] for key in final},
                        "evidence": observed["evidence"],
                    }
                )
            self.progress.phase = "done"
            return
        for field, control, role in (("liked", like, "like"), ("collected", favorite, "collect")):
            if field in final and observed[field] != final[field]:
                actions["CLICK:" + control["id"]] = {
                    **control,
                    "role": role,
                    "field": field,
                    "expected": final[field],
                    "before": observed[field],
                    "targetTitle": title,
                    "label": ("点赞" if field == "liked" else "收藏")
                    + f"：当前 {observed[field]} → 目标 {final[field]}（同帖截图验证）",
                }

    def _signature(self):
        state = self.snapshot["state"]
        observed = state["observation"]
        return json.dumps(
            {
                "package": state.get("focusedPackage"),
                "stage": state["stage"],
                "kind": observed["kind"],
                "query": observed.get("observedQuery"),
                "target": self.bound_target,
                "liked": observed["liked"],
                "collected": observed["collected"],
                "actions": sorted(
                    (action["method"], action["role"], action["label"])
                    for action in self.actions.values()
                ),
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    def act(self, choice):
        self._budget()
        if choice not in self.actions:
            raise ValueError("Choice unavailable in current observation")
        selected = dict(self.actions[choice])
        before_signature = self._signature()
        self.operations.append(
            {
                "name": "selected_action",
                "choice": choice,
                "stage": self.progress.phase,
                "label": selected["label"],
            }
        )
        if selected["method"] == "blocked":
            self._stop("BLOCKED")
        # Even a still-valid opaque reference must be authorized by fresh state.
        fresh = self.observe()
        if fresh["done"]:
            return fresh
        matches = [
            action
            for action in self.actions.values()
            if all(
                action.get(key) == selected.get(key)
                for key in (
                    "method",
                    "role",
                    "label",
                    "bounds",
                    "targetTitle",
                    "expected",
                    "before",
                )
            )
        ]
        if len(matches) != 1:
            fresh["execution"] = {"status": "not_dispatched", "reason": "fresh_state_changed"}
            self.operations.append({"name": "execution", **fresh["execution"]})
            self._save()
            return fresh
        action = matches[0]
        method = action["method"]
        try:
            if method == "open":
                self.call("android_open_app", {"package_id": PACKAGE})
            elif method == "back":
                self.call("android_press_back", {})
            elif method == "key":
                self.call("android_press_key", {"key": "ENTER"})
                self._query_submitted = True
            elif method in {"click", "tap"}:
                if action["role"] == "open_target":
                    self.bound_target = {
                        "title": action["targetTitle"],
                        "rawLabel": action["rawLabel"],
                        "source": "current-search-result",
                        "selectedObservation": self.index,
                    }
                    if "author" in self.task["target"]:
                        self.bound_target["author"] = self.task["target"]["author"]
                if action["role"] in {"like", "collect"}:
                    self.pending_toggle = {
                        "field": action["field"],
                        "before": action["before"],
                        "expected": action["expected"],
                        "beforeObservation": self.index,
                        "stage": self.progress.phase,
                        "target": dict(self.bound_target),
                    }
                if method == "click":
                    self.call("android_click_node", {"node_id": action["id"]})
                    if action["role"] == "search_submit":
                        self._query_submitted = True
                else:
                    # Center belongs to the fresh live node, never the recording.
                    self.call("android_tap", {"x": action["center"][0], "y": action["center"][1]})
            elif method == "type":
                self.call("android_type_clear_text", {"node_id": action["id"]})
                after_clear = content_text(
                    self.call("android_get_screen_state", {"include_screenshot": False})
                )
                cleared_nodes = own_nodes(after_clear)
                if focused_package(after_clear) != PACKAGE or screen_locked(cleared_nodes):
                    self._stop("TARGET_CHANGED")
                fields = _search_fields(_controls(cleared_nodes, PACKAGE), after_clear)
                if len(fields) != 1:
                    self._stop("TARGET_CHANGED")
                self.call(
                    "android_type_append_text",
                    {
                        "node_id": fields[0]["id"],
                        "text": self.task["query"],
                        "typing_speed": 10,
                        "typing_speed_variance": 0,
                    },
                )
            elif method != "wait":
                raise ValueError("Unsupported experience action")
        except McpError as error:
            canonical = "Node not found in accessibility tree (ToolError; stage=tool_call)"
            if method in {"click", "tap"} and str(error) == canonical:
                self.pending_toggle = None
                if action["role"] == "open_target":
                    self.bound_target = None
                self.stale_recoveries += 1
                observed = self.observe()
                observed["execution"] = {"status": "not_dispatched", "reason": "stale_reference"}
                self._save()
                return observed
            self.pending_toggle = None
            self.operations.append({"name": "uncertain_delivery_readback", "readOnly": True})
            try:
                self.observe()
            except Exception:
                pass
            self._stop("DELIVERY_UNCERTAIN")
        time.sleep(0.25)
        result = self.observe()
        for attempt in range(2):
            if not self.pending_toggle:
                break
            self.operations.append(
                {"name": "toggle_settle", "attempt": attempt + 1, "readOnly": True}
            )
            start = time.perf_counter()
            time.sleep(0.2)
            self.phases.append(
                {
                    "kind": "wait",
                    "name": "互动状态只读稳定等待",
                    "ms": (time.perf_counter() - start) * 1000,
                }
            )
            result = self.observe()
        if self.pending_toggle:
            self._stop("TOGGLE_UNVERIFIED")
        if not result["done"] and before_signature == self._signature():
            key = (before_signature, action["role"], action["label"])
            self.no_progress[key] = self.no_progress.get(key, 0) + 1
            if self.no_progress[key] >= 3:
                self._stop("NO_PROGRESS")
        else:
            self.no_progress.clear()
        return result
