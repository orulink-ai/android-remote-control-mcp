"""Model chooses every step; optional experience only augments current state.

The existing DecisionPolicy remains authoritative for deadlines, validation,
current candidate choices, cancellation and PI's refresh-before-fallback barrier.
There is no recipe cursor, cached action, phone operation or speed guarantee.
"""

from __future__ import annotations

import copy

from decision_policy import DecisionPolicy
from experience_skill import load_experience_skill


class ExperiencePolicy(DecisionPolicy):
    def __init__(self, client, mode="jev-auto", *, skill_path=None, **policy_options):
        super().__init__(client, mode, **policy_options)
        self.experience = load_experience_skill(skill_path) if skill_path is not None else None
        self.hint_decisions = 0

    async def decide(self, snapshot, observation_id):
        if (
            self.experience is not None
            and isinstance(snapshot, dict)
            and isinstance(snapshot.get("state"), dict)
        ):
            snapshot = copy.deepcopy(snapshot)
            snapshot["state"]["skillExperience"] = self.experience.context(snapshot["state"])
        result = await super().decide(snapshot, observation_id)
        if self.experience is not None and result.get("kind") == "decision":
            # Counts validated model choices, not task completion or recipe hits.
            # Failed attempts remain visible in the parent's attempts/modelRequests.
            self.hint_decisions += 1
        return result

    def summary(self):
        summary = super().summary()
        return {
            **summary,
            "mode": "experience-assisted" if self.experience is not None else self.mode,
            "decisionMode": self.mode,
            "experienceLoaded": self.experience is not None,
            "sourceSha256": self.experience.source_sha256 if self.experience is not None else None,
            "recipeSha256": self.experience.recipe_sha256 if self.experience is not None else None,
            "hintDecisions": self.hint_decisions,
            "modelRequests": len(self.attempts),
            "recipeHits": 0,
            "everyStepModelDecided": True,
        }
