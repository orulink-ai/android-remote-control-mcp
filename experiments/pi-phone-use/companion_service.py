"""Install/remove the current user's local PI phone companion on macOS."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL = "ai.orulink.phone-workflow"
DIRECTORY = Path(__file__).resolve().parent


def service_definition(state_directory, env):
    profile = Path(
        env.get("PI_PHONE_PROFILE", "~/.config/orulink-phone/terminal.json")
    ).expanduser()
    try:
        if not profile.is_absolute():
            raise ValueError
        configured = json.loads(profile.read_text())
        python = env.get("PHONE_BENCH_PYTHON", configured["python"])
        if not Path(python).is_absolute():
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError("Set a valid PI_PHONE_PROFILE with an absolute Python runtime") from None
    state_directory = Path(state_directory).resolve()
    allowed = (
        "PHONE_BENCH_PYTHON",
        "OPENROUTER_API_KEY_FILE",
    )
    environment = {key: env[key] for key in allowed if key in env}
    environment.update(
        PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        PI_PHONE_PROFILE=str(profile),
        PHONE_BENCH_PYTHON=python,
    )
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python,
            str(DIRECTORY / "workflow_companion.py"),
            "--profile",
            str(profile),
            "--output",
            str(state_directory / "jobs"),
        ],
        "WorkingDirectory": str(DIRECTORY),
        "EnvironmentVariables": environment,
        "KeepAlive": True,
        "RunAtLoad": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(state_directory / "companion.log"),
        "StandardErrorPath": str(state_directory / "companion-error.log"),
        "Umask": 0o077,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall", "status"])
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/share/orulink-phone-workflow"
    )
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        raise SystemExit(
            "This service installer requires macOS; run workflow_companion.py directly on other systems"
        )
    path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    if args.action == "status":
        return subprocess.run(["launchctl", "print", target], check=False).returncode
    if args.action == "uninstall":
        subprocess.run(
            ["launchctl", "bootout", domain, str(path)], check=False, capture_output=True
        )
        path.unlink(missing_ok=True)
        print("Companion service removed; saved workflows retained")
        return 0
    definition = service_definition(args.state_dir, os.environ)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", domain, str(path)], check=False, capture_output=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as stream:
        plistlib.dump(definition, stream)
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True)
    print("Local PI phone companion installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
