"""Launch the same phone-only PI entry used interactively; never shell out to ADB."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from workflow import load_recipe

DIRECTORY = Path(__file__).resolve().parent


def build_launch(profile, arguments, environment):
    protected = (
        "--provider",
        "--model",
        "--tools",
        "-t",
        "--extension",
        "-e",
        "--no-extensions",
        "-ne",
        "--no-tools",
        "--no-builtin-tools",
        "--system-prompt",
        "--api-key",
    )
    if any(arg.split("=", 1)[0] in protected for arg in arguments):
        raise ValueError(
            "The phone entry fixes its provider and tools; use ordinary pi for other configurations"
        )
    recipe = None
    if profile.get("recipe") is not None:
        recipe = Path(profile["recipe"]).expanduser().resolve()
        load_recipe(recipe)
    enabled = profile.get("experienceEnabled", True)
    if type(enabled) is not bool:
        raise ValueError("experienceEnabled must be boolean")
    mcp_file = Path(profile["mcpConfig"]).expanduser().resolve()
    server = profile["mcpServer"]
    configured = json.loads(mcp_file.read_text())["mcpServers"][server]
    url = configured["url"]
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("A configured HTTP Android MCP is required")
    python = str(Path(profile["python"]).expanduser())
    if not Path(python).is_absolute():
        raise ValueError("An absolute Python runtime is required")
    env = {
        **environment,
        "PHONE_BENCH_PYTHON": python,
        "PHONE_BENCH_RECIPE": str(recipe) if recipe is not None else "",
        "PHONE_EXPERIENCE_ENABLED": "1" if enabled else "0",
        "PHONE_BENCH_OUTPUT_DIR": str(
            Path(profile.get("output", "~/.local/share/orulink-phone/terminal"))
            .expanduser()
            .resolve()
        ),
        "ANDROID_MCP_CONFIG": str(mcp_file),
        "ANDROID_MCP_SERVER": server,
        "ANDROID_MCP_URL": url,
        "OPENROUTER_API_KEY_FILE": str(
            Path(profile.get("keyFile", "~/.config/android-jev/openrouter.key")).expanduser()
        ),
    }
    # A previous benchmark wrapper must not silently override the new entry.
    env.pop("PHONE_TERMINAL_TASK", None)
    env.pop("PHONE_BENCH_WORKER", None)
    env.pop("PHONE_BENCH_TRIAL", None)
    command = [
        profile.get("pi", "/opt/homebrew/bin/pi"),
        "--no-extensions",
        "-e",
        str(DIRECTORY / "terminal-extension.ts"),
        "--no-mcp",
        "--no-builtin-tools",
        "--tools",
        "phone_observe,phone_action",
        "--provider",
        "phone-control",
        "--model",
        "auto",
        "--thinking",
        "off",
        *arguments,
    ]
    return command, env


def main(argv=None):
    path = Path(
        os.environ.get("PI_PHONE_PROFILE", "~/.config/orulink-phone/terminal.json")
    ).expanduser()
    try:
        command, environment = build_launch(
            json.loads(path.read_text()), list(sys.argv[1:] if argv is None else argv), os.environ
        )
    except (OSError, ValueError, KeyError, TypeError):
        print(
            "pi-phone 配置无效：请检查 MCP、已验证录制配方与运行时；未操作手机。", file=sys.stderr
        )
        return 2
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    raise SystemExit(main())
