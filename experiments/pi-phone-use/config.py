"""Portable experiment configuration; credentials never enter run artifacts."""

import json
import os
from pathlib import Path


def output_dir() -> Path:
    default = Path(__file__).resolve().parents[2] / "artifacts" / "pi-phone-use"
    return Path(os.environ.get("PHONE_BENCH_OUTPUT_DIR", str(default))).expanduser().resolve()


def python_executable() -> str:
    return os.environ.get("PHONE_BENCH_PYTHON", "python3")


def load_openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key is None:
        path = Path(
            os.environ.get("OPENROUTER_API_KEY_FILE", "~/.config/android-jev/openrouter.key")
        ).expanduser()
        try:
            key = path.read_text().strip()
        except OSError:
            raise ValueError(
                "Set OPENROUTER_API_KEY or a readable OPENROUTER_API_KEY_FILE"
            ) from None
    if not key.strip():
        raise ValueError("OPENROUTER_API_KEY must be nonblank")
    return key.strip()


def mcp_settings() -> tuple[str, dict[str, str]]:
    """Default to the USB forward; PI config supplies headers, not stale LAN URL."""
    path = Path(os.environ.get("ANDROID_MCP_CONFIG", "~/.pi/agent/mcp.json")).expanduser()
    server = os.environ.get("ANDROID_MCP_SERVER", "watcher_android")
    headers = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())["mcpServers"][server]
            headers = settings.get("headers", {})
            if not isinstance(headers, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
            ):
                raise ValueError("Invalid MCP headers")
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("Invalid ANDROID_MCP_CONFIG or ANDROID_MCP_SERVER settings") from None
    elif "ANDROID_MCP_CONFIG" in os.environ:
        raise ValueError("ANDROID_MCP_CONFIG file does not exist")
    return os.environ.get("ANDROID_MCP_URL", "http://127.0.0.1:18080/mcp"), headers
