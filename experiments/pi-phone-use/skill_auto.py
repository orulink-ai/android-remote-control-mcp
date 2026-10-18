"""Verified local recipe first; typed screen drift hands remaining work to models.

Models never repair or resume the recipe cursor. A drift switches this trial's
remaining PI decisions to Jev, with the existing fresh-observation barrier before
a single DeepSeek fallback. Neither policy dispatches phone operations.
"""

from __future__ import annotations

import copy

from controller import PACKAGE
from decision_policy import DecisionPolicy, DecisionPolicyError
from workflow import RecipeDrift, SELECTOR_KEYS, SkillPolicy, matches, semantic_selector


class LazyDecisionClient:
    """No credentials or HTTP client on cache hits; construct once on first drift."""

    def __init__(self, factory, key_reader, **options):
        self.factory, self.key_reader, self.options = factory, key_reader, options
        self.client = None
        self.closed = False

    @property
    def mode(self):
        return self.options.get("mode", "warm") if self.client is not None else "local"

    @property
    def decisionStyle(self):
        return self.options.get("decisionStyle", "heads") if self.client is not None else "recipe"

    async def decide(self, *args):
        if self.closed:
            raise DecisionPolicyError("Model client is closed")
        if self.client is None:
            self.client = self.factory(self.key_reader(), **self.options)
        return await self.client.decide(*args)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.client is not None:
            await self.client.close()


class SkillAutoPolicy:
    """Recover only RecipeDrift; task/delivery/ambiguity/budget errors stop."""

    def __init__(self, client, mode, *, recipe, **budgets):
        if mode != "skill-auto":
            raise ValueError("Unsupported skill recovery mode")
        self.skill = SkillPolicy(None, "skill", recipe=recipe)
        self.models = DecisionPolicy(client, "jev-auto", **budgets)
        self.attempts = self.models.attempts
        self.recovery_active = False
        self.drifts = []
        self.last_observation = -1
        self._approved = None
        self._reference_refreshes = {}
        self.preparation_enabled = recipe.get("entryStrategy") == "prepare-feed-v1"
        self.preparing = self.preparation_enabled
        self._preparation_decision = False

    def _guard(self, snapshot, *, preparing=False):
        execution = snapshot.get("execution")
        if execution is not None:
            if not isinstance(execution, dict):
                raise DecisionPolicyError("Phone action delivery evidence is invalid")
            if execution.get("status") == "applied":
                valid = set(execution) == {"status"}
            else:
                valid = (
                    set(execution) == {"status", "reason", "choice", "intent"}
                    and execution.get("status") == "not_dispatched"
                    and execution.get("reason") == "stale_reference"
                    and isinstance(execution.get("intent"), dict)
                    and set(execution["intent"]) == SELECTOR_KEYS
                )
            if not valid:
                raise DecisionPolicyError("Uncertain delivery cannot enter model recovery")
        state = snapshot.get("state", {})
        if any(state.get(key) != value for key, value in self.skill.recipe["task"].items()):
            raise DecisionPolicyError("Recipe task identity changed")
        observation = state.get("observation", {})
        if not observation.get("kind") or not state.get("stage"):
            raise DecisionPolicyError("A verified common screen observation is required")
        if not preparing and observation.get("kind") == "detail" and observation.get("firstTitleVisible") is not True:
            raise DecisionPolicyError("Current detail target is not verified")
        # No model may choose between semantically indistinguishable offered targets.
        seen = set()
        for choice, descriptor in state.get("actionDescriptors", {}).items():
            if choice not in snapshot.get("options", {}):
                continue
            selector = semantic_selector(descriptor, self.skill.recipe["task"])
            token = tuple(selector[key] for key in sorted(SELECTOR_KEYS))
            if token in seen:
                raise DecisionPolicyError("Ambiguous current semantic target")
            seen.add(token)

    def _recover(self, drift, snapshot, observation_id):
        self.recovery_active = True
        self.drifts.append({
            "reason": drift.reason, "observation": observation_id,
            "stage": snapshot["state"]["stage"], "recipeCursor": self.skill.cursor,
        })
        self._approved = None

    async def decide(self, snapshot, observation_id):
        self._guard(snapshot, preparing=self.preparing)
        self.last_observation = observation_id
        if self.preparing:
            state = snapshot["state"]
            ready = (state["observation"]["kind"] == "feed" and state.get("focusedPackage") == PACKAGE
                     and state["stage"] == "search" and snapshot.get("done") is False)
            if ready and not self.models.summary()["pendingRefresh"]:
                self.preparing = False
            else:
                # A late feed read cannot skip an already pending fallback. Only
                # navigation/wait choices are exposed to the preparation model.
                safe = {key: value for key, value in state.get("actionDescriptors", {}).items()
                        if key in snapshot["options"] and value.get("method") in {"open", "back", "wait", "blocked"}}
                prepared = copy.deepcopy(snapshot)
                prepared["options"] = {key: snapshot["options"][key] for key in safe}
                prepared["state"]["actionDescriptors"] = safe
                start = len(self.attempts)
                try:
                    result = await self.models.decide(prepared, observation_id)
                finally:
                    for attempt in self.attempts[start:]:
                        attempt["workflowPhase"] = "preparation"
                if result.get("kind") == "decision":
                    self._approved = {"choice": result["choice"], "descriptor": copy.deepcopy(safe[result["choice"]]),
                                      "snapshot": copy.deepcopy(snapshot)}
                    self._preparation_decision = True
                    result = {**result, "preparation": True, "modelRequest": True}
                return result
        if not self.recovery_active:
            try:
                result = await self.skill.decide(snapshot, observation_id)
            except RecipeDrift as drift:
                self._recover(drift, snapshot, observation_id)
            else:
                return result
        start = len(self.attempts)
        try:
            result = await self.models.decide(snapshot, observation_id)
        finally:
            for attempt in self.attempts[start:]:
                attempt["workflowPhase"] = "recovery"
        if result.get("kind") == "decision":
            descriptor = snapshot["state"].get("actionDescriptors", {}).get(result["choice"])
            if not isinstance(descriptor, dict):
                raise DecisionPolicyError("Selected action lacks a current semantic descriptor")
            self._approved = {
                "choice": result["choice"], "descriptor": copy.deepcopy(descriptor),
                "snapshot": copy.deepcopy(snapshot),
            }
            result = {**result, "recipeRecovery": True, "modelRequest": True}
        return result

    def verify(self, snapshot):
        self._guard(snapshot, preparing=self.preparing)
        if self._preparation_decision:
            self._preparation_decision = False
            self._approved = None
            return
        if self.recovery_active:
            self._approved = None
            return
        try:
            self.skill.verify(snapshot)
        except RecipeDrift as drift:
            self._recover(drift, snapshot, self.last_observation + 1)

    def not_dispatched(self, snapshot, choice):
        self._guard(snapshot, preparing=self.preparing)
        if not self.recovery_active and not self.preparing:
            return self.skill.not_dispatched(snapshot, choice)
        marker = snapshot.get("execution", {})
        approved = self._approved
        if (
            approved is None or choice == "WAIT" or choice != approved["choice"]
            or set(marker) != {"status", "reason", "choice", "intent"}
            or marker.get("status") != "not_dispatched"
            or marker.get("reason") != "stale_reference" or marker.get("choice") != choice
            or marker.get("intent") != approved["descriptor"]
            or not matches(snapshot, self._predicate(approved["snapshot"]))
        ):
            raise DecisionPolicyError("Unverified action delivery cannot authorize recovery")
        token = (snapshot["state"]["stage"], choice.split(":", 1)[0],
                 tuple(sorted(approved["descriptor"].items())))
        count = self._reference_refreshes.get(token, 0)
        if count >= 3:
            raise DecisionPolicyError("Reference refresh budget exhausted")
        self._reference_refreshes[token] = count + 1
        self._approved = None
        self._preparation_decision = False

    @staticmethod
    def _predicate(snapshot):
        from workflow import predicate
        return predicate(snapshot)

    def verify_complete(self, snapshot, *, progress=None):
        if self.preparing:
            raise DecisionPolicyError("Recorded recipe entry has not been prepared")
        self._guard(snapshot)
        state, observed = snapshot["state"], snapshot["state"]["observation"]
        if (
            snapshot.get("done") is not True or state.get("stage") != "done"
            or observed.get("kind") != "detail" or observed.get("firstTitleVisible") is not True
            or observed.get("liked") is not False or observed.get("collected") is not False
        ):
            raise DecisionPolicyError("Original target final state is not verified")
        if not self.recovery_active:
            return self.skill.verify_complete(snapshot)
        stages = ["search", "open_first", "like", "collect", "unlike", "uncollect", "done"]
        expected = list(zip(stages, stages[1:]))
        if (
            progress is None or progress.phase != "done"
            or [(item.get("stage"), item.get("next")) for item in progress.milestones] != expected
            or self.models.summary()["pendingRefresh"]
        ):
            raise DecisionPolicyError("Common full-workflow completion evidence is required")

    def summary(self):
        local, models = self.skill.summary(), self.models.summary()
        preparation_requests = sum(item.get("workflowPhase") == "preparation" for item in self.attempts)
        recovery_requests = len(self.attempts) - preparation_requests
        return {
            **local, **models, "mode": "skill-auto", "recoveryActive": self.recovery_active,
            "drifts": copy.deepcopy(self.drifts), "modelFallbacks": recovery_requests,
            "preparationEnabled": self.preparation_enabled, "preparationComplete": self.preparation_enabled and not self.preparing,
            "preparationRequests": preparation_requests, "recoveryRequests": recovery_requests,
            "recipeToModelRecoveries": len(self.drifts),
            "deepseekFallbackRequests": models["fallbackRequests"],
            "driftPolicy": "jev-for-rest-of-trial-then-deepseek-once-after-observe",
            "referenceRefreshes": local["referenceRefreshes"] + sum(self._reference_refreshes.values()),
        }
