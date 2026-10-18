"""A completed App job must publish an immutable recipe without clobbering PI config."""

import json
from pathlib import Path
import stat

import pytest

from controller import QUERY, TITLE
from test_workflow import demo
from workflow import compile_recording
from workflow_skill_install import PublicationError, SkillPublisher


def setup(tmp_path):
    raw = demo()
    raw["task"] = {"query": QUERY, "expectedFirstTitle": TITLE}
    for step in raw["steps"]:
        for side in ("before", "after"):
            step[side]["state"]["query"] = QUERY
            step[side]["state"]["expectedFirstTitle"] = TITLE
    bundle = tmp_path / "bundle"
    compile_recording(raw, bundle)
    profile = tmp_path / "terminal.json"
    profile.write_text(
        json.dumps(
            {"recipe": "/old/recipe.json", "python": "/venv/python", "custom": {"preserve": True}},
            ensure_ascii=False,
        )
    )
    return bundle, profile, tmp_path / "skills"


def test_verified_bundle_is_copied_and_next_pi_entry_uses_new_recipe(tmp_path):
    bundle, profile, root = setup(tmp_path)
    original = profile.read_bytes()
    publish = SkillPublisher(profile, skills_directory=root)
    receipt = publish(bundle, artifact_id="job-001")
    config = json.loads(profile.read_text())
    new = Path(config["recipe"])
    assert new.parent.parent == root and new.parent.name == "generated-job-001"
    for name in ("recipe.json", "SKILL.md", "generation.json"):
        assert (new.parent / name).read_bytes() == (bundle / name).read_bytes()
        assert stat.S_IMODE((new.parent / name).stat().st_mode) == 0o600
    assert config["custom"] == {"preserve": True}
    assert next(profile.parent.glob("terminal.json.before-job-001*")).read_bytes() == original
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert receipt["artifactId"] == "job-001" and receipt["status"] == "installed"
    assert str(tmp_path) not in json.dumps(receipt)


def test_user_profile_edits_during_job_are_never_overwritten(tmp_path):
    bundle, profile, root = setup(tmp_path)
    publish = SkillPublisher(profile, skills_directory=root)
    changed = b'{"recipe":"/user/new.json","custom":"user edit"}'
    profile.write_bytes(changed)
    with pytest.raises(PublicationError):
        publish(bundle, artifact_id="job-001")
    assert profile.read_bytes() == changed
    assert not root.exists()


def test_publication_is_idempotent_without_overwriting_an_existing_bundle(tmp_path):
    bundle, profile, root = setup(tmp_path)
    publish = SkillPublisher(profile, skills_directory=root)
    first = publish(bundle, artifact_id="job-001")
    before = profile.read_bytes()
    second = publish(bundle, artifact_id="job-001")
    assert second["recipeSha256"] == first["recipeSha256"]
    assert profile.read_bytes() == before
    assert len(list(root.glob("generated-*"))) == 1


@pytest.mark.parametrize("change", ["digest", "generation", "missing_skill", "traversal"])
def test_invalid_bundle_or_identifier_cannot_change_profile(tmp_path, change):
    bundle, profile, root = setup(tmp_path)
    before = profile.read_bytes()
    artifact = "job-001"
    if change == "digest":
        r = json.loads((bundle / "recipe.json").read_text())
        r["task"]["query"] = "other"
        (bundle / "recipe.json").write_text(json.dumps(r))
    elif change == "generation":
        r = json.loads((bundle / "generation.json").read_text())
        r["sourceSha256"] = "0" * 64
        (bundle / "generation.json").write_text(json.dumps(r))
    elif change == "missing_skill":
        (bundle / "SKILL.md").unlink()
    else:
        artifact = "../escape"
    with pytest.raises(PublicationError):
        SkillPublisher(profile, skills_directory=root)(bundle, artifact_id=artifact)
    assert profile.read_bytes() == before
    assert not root.exists()


def test_existing_different_artifact_is_not_overwritten(tmp_path):
    bundle, profile, root = setup(tmp_path)
    target = root / "generated-job-001"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("previous artifact")
    before = profile.read_bytes()
    with pytest.raises(PublicationError):
        SkillPublisher(profile, skills_directory=root)(bundle, artifact_id="job-001")
    assert (target / "SKILL.md").read_text() == "previous artifact"
    assert profile.read_bytes() == before


def test_profile_replacement_failure_keeps_original_entry_usable(tmp_path, monkeypatch):
    import workflow_skill_install as module

    bundle, profile, root = setup(tmp_path)
    before = profile.read_bytes()
    publish = SkillPublisher(profile, skills_directory=root)

    def fail(*args):
        raise OSError("private error must not leak")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(PublicationError) as caught:
        publish(bundle, artifact_id="job-001")
    assert profile.read_bytes() == before
    assert "private error" not in str(caught.value)
    assert not list(profile.parent.glob(".phone-profile-*"))


def test_profile_change_while_copying_does_not_lose_user_edit(tmp_path, monkeypatch):
    import workflow_skill_install as module

    bundle, profile, root = setup(tmp_path)
    publish = SkillPublisher(profile, skills_directory=root)
    original = module.os.rename
    changed = b'{"recipe":"/another/user-recipe.json"}'

    def racing_rename(*args):
        original(*args)
        profile.write_bytes(changed)

    monkeypatch.setattr(module.os, "rename", racing_rename)
    with pytest.raises(PublicationError):
        publish(bundle, artifact_id="job-001")
    assert profile.read_bytes() == changed
