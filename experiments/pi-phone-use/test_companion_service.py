"""The companion service keeps credentials in private config files."""

import plistlib
import json

import pytest

from companion_service import service_definition


def test_launch_agent_invokes_actual_companion_and_only_copies_allowlisted_paths(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"python": "/absolute/venv/python"}))
    env = {
        "PI_PHONE_PROFILE": str(profile),
        "PHONE_BENCH_DEVICE": "test-device",
        "PHONE_BENCH_PYTHON": "/absolute/venv/python",
        "ANDROID_MCP_CONFIG": "/private/config.json",
        "ANDROID_MCP_SERVER": "redmi_android",
        "OPENROUTER_API_KEY_FILE": "/private/key",
        "OPENROUTER_API_KEY": "never-copy-this-secret",
        "OTHER_SECRET": "never-copy-this-secret",
    }
    definition = service_definition(tmp_path, env)
    raw = plistlib.dumps(definition)
    assert b"never-copy-this-secret" not in raw
    assert definition["ProgramArguments"][0] == "/absolute/venv/python"
    assert definition["ProgramArguments"][1].endswith("workflow_companion.py")
    assert definition["KeepAlive"] is True
    assert definition["EnvironmentVariables"]["PI_PHONE_PROFILE"] == str(profile)
    for forbidden in (
        "PHONE_WORKFLOW_USB_FORWARD",
        "PHONE_BENCH_DEVICE",
        "ADB",
        "OPENROUTER_API_KEY",
    ):
        assert forbidden not in definition["EnvironmentVariables"]
    assert "--profile" in definition["ProgramArguments"]


@pytest.mark.parametrize("env", [{}, {"PHONE_BENCH_DEVICE": "x", "PHONE_BENCH_PYTHON": "python"}])
def test_install_rejects_missing_profile_or_absolute_python_path(tmp_path, env):
    env = {"PI_PHONE_PROFILE": str(tmp_path / "missing-profile.json"), **env}
    with pytest.raises(ValueError):
        service_definition(tmp_path, env)


def test_service_needs_profile_python_but_no_device_or_transport_shell(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"python": "/absolute/venv/python"}))
    definition = service_definition(tmp_path, {"PI_PHONE_PROFILE": str(profile)})
    assert definition["ProgramArguments"][0] == "/absolute/venv/python"
