"""Configuration tests never inspect the user's credentials or PI settings."""

import json

import pytest

from config import (
    decision_timeouts,
    load_openrouter_key,
    mcp_settings,
    output_dir,
    python_executable,
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for name in [
        "OPENROUTER_API_KEY",
        "OPENROUTER_API_KEY_FILE",
        "ANDROID_MCP_URL",
        "ANDROID_MCP_CONFIG",
        "ANDROID_MCP_SERVER",
        "PHONE_BENCH_OUTPUT_DIR",
        "PHONE_BENCH_PYTHON",
        "PHONE_BENCH_JEV_TIMEOUT",
        "PHONE_BENCH_DEEPSEEK_TIMEOUT",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


def test_explicit_key_overrides_compatible_file(monkeypatch, tmp_path):
    key = tmp_path / "key"
    key.write_text("file-test-key\n")
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(key))
    assert load_openrouter_key() == "file-test-key"
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-test-key")
    assert load_openrouter_key() == "environment-test-key"


def test_missing_or_blank_key_has_safe_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        load_openrouter_key()
    monkeypatch.setenv("OPENROUTER_API_KEY", " ")
    with pytest.raises(ValueError, match="nonblank"):
        load_openrouter_key()


def test_mcp_default_usb_and_configured_auth(monkeypatch, tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "selected": {
                        "url": "http://old-lan-address/mcp",
                        "headers": {"Authorization": "test-token"},
                    }
                }
            }
        )
    )
    monkeypatch.setenv("ANDROID_MCP_CONFIG", str(path))
    monkeypatch.setenv("ANDROID_MCP_SERVER", "selected")
    assert mcp_settings() == ("http://127.0.0.1:18080/mcp", {"Authorization": "test-token"})
    monkeypatch.setenv("ANDROID_MCP_URL", "http://127.0.0.1:19000/mcp")
    assert mcp_settings()[0] == "http://127.0.0.1:19000/mcp"


def test_missing_mcp_config_allows_unauthenticated_local_server():
    assert mcp_settings() == ("http://127.0.0.1:18080/mcp", {})


def test_paths_and_python_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("PHONE_BENCH_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("PHONE_BENCH_PYTHON", "/some/venv/bin/python")
    assert output_dir() == tmp_path / "out"
    assert python_executable() == "/some/venv/bin/python"


def test_output_default_is_repository_artifacts():
    assert output_dir().parts[-2:] == ("artifacts", "pi-phone-use")
    assert python_executable() == "python3"


def test_auto_decision_timeout_defaults():
    assert decision_timeouts() == (5.0, 15.0)


def test_auto_decision_timeout_overrides(monkeypatch):
    monkeypatch.setenv("PHONE_BENCH_JEV_TIMEOUT", "0.1")
    monkeypatch.setenv("PHONE_BENCH_DEEPSEEK_TIMEOUT", "60")
    assert decision_timeouts() == (0.1, 60.0)


@pytest.mark.parametrize("name", ["PHONE_BENCH_JEV_TIMEOUT", "PHONE_BENCH_DEEPSEEK_TIMEOUT"])
@pytest.mark.parametrize(
    "value", ["", " ", "bad-private-value", "nan", "inf", "-inf", "0", "-1", "60.01"]
)
def test_invalid_auto_timeout_error_never_repeats_value(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        decision_timeouts()
    assert str(error.value) == name
