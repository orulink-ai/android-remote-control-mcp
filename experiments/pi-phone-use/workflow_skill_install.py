"""Publish a successfully executed workflow for subsequent pi-phone sessions.

The companion supplies execution/provenance verification before invoking this
publisher. This layer checks the bundle, preserves it immutably and changes the
profile pointer atomically; it never runs phone actions or a model.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from controller import QUERY, TITLE
from workflow import load_recipe

FILES = ("recipe.json", "SKILL.md", "generation.json")


class PublicationError(ValueError):
    """A static publication failure; never expose profile contents or paths."""


def read_bundle(directory):
    try:
        directory = Path(directory)
        payload = {}
        for name in FILES:
            path = directory / name
            if path.is_symlink() or not path.is_file() or not 1 <= path.stat().st_size <= 2_000_000:
                raise ValueError
            payload[name] = path.read_bytes()
        recipe = load_recipe(directory / "recipe.json")
        generation = json.loads(payload["generation.json"])
        if (
            recipe["task"] != {"query": QUERY, "expectedFirstTitle": TITLE}
            or generation.get("status") != "success"
            or generation.get("reviewState") != "verified-demonstration"
            or generation.get("sourceSha256") != recipe["sourceSha256"]
            or generation.get("stepCount") != len(recipe["steps"])
        ):
            raise ValueError
        return payload, recipe
    except (OSError, ValueError, KeyError, TypeError):
        raise PublicationError("Verified workflow bundle is invalid") from None


class SkillPublisher:
    def __init__(self, profile_path, *, skills_directory=None):
        self.profile = Path(profile_path).expanduser().resolve()
        try:
            self.expected = self.profile.read_bytes()
            self.configuration = json.loads(self.expected)
            if not isinstance(self.configuration.get("recipe"), str):
                raise ValueError
        except (OSError, ValueError, AttributeError):
            raise PublicationError("Phone profile is unavailable") from None
        self.root = (
            Path(skills_directory or "~/.local/share/orulink-phone/skills").expanduser().resolve()
        )

    def _unchanged(self):
        if self.profile.read_bytes() != self.expected:
            raise PublicationError("Phone profile changed during workflow execution")

    def __call__(self, bundle_path, *, artifact_id):
        if not isinstance(artifact_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", artifact_id
        ):
            raise PublicationError("Invalid workflow artifact identifier")
        payload, recipe = read_bundle(bundle_path)
        temporary = None
        try:
            lock_path = self.profile.with_name(self.profile.name + ".lock")
            with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._unchanged()
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination = self.root / ("generated-" + artifact_id)
                if destination.exists():
                    if destination.is_symlink() or any(
                        not (destination / name).is_file()
                        or (destination / name).is_symlink()
                        or (destination / name).read_bytes() != value
                        for name, value in payload.items()
                    ):
                        raise PublicationError(
                            "Workflow artifact already exists with different contents"
                        )
                else:
                    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=self.root))
                    for name, value in payload.items():
                        with os.fdopen(
                            os.open(temporary / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                            "wb",
                        ) as stream:
                            stream.write(value)
                    os.rename(temporary, destination)
                    temporary = None
                pointer = str(destination / "recipe.json")
                if self.configuration["recipe"] != pointer:
                    self._install_pointer(pointer, artifact_id)
                return {
                    "artifactId": artifact_id,
                    "status": "installed",
                    "recipeSha256": recipe["recipeSha256"],
                    "sourceSha256": recipe["sourceSha256"],
                    "requiresNewSession": True,
                }
        except PublicationError:
            raise
        except (OSError, ValueError, TypeError):
            raise PublicationError(
                "Workflow publication failed; inspect the retained job"
            ) from None
        finally:
            if temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)

    def _install_pointer(self, pointer, artifact_id):
        backup = self.profile.with_name(self.profile.name + ".before-" + artifact_id)
        if backup.exists():
            if backup.read_bytes() != self.expected:
                raise PublicationError("Workflow profile backup conflicts with existing data")
        else:
            with os.fdopen(
                os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
            ) as stream:
                stream.write(self.expected)
        configuration = {**self.configuration, "recipe": pointer}
        updated = json.dumps(configuration, ensure_ascii=False, indent=2).encode()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".phone-profile-", dir=self.profile.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            self._unchanged()
            os.replace(temporary, self.profile)
            temporary = None
            self.expected, self.configuration = updated, configuration
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
