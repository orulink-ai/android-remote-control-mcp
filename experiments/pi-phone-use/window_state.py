"""Read observed window focus without importing a launcher or phone runtime."""

import re

SUPPORTED_LAUNCHERS = frozenset({"com.sec.android.app.launcher", "com.miui.home"})


def focused_package(state):
    """Return only the sole explicitly focused window's package, else unknown.

    Background windows are not foreground evidence. Focus is read from the
    formatter's final field so title text cannot masquerade as the focus flag.
    """
    packages = []
    for header in state.splitlines():
        if not header.startswith("--- window:"):
            continue
        focus = re.search(r"\sfocused:(true|false)\s+---\s*$", header)
        if focus is None or focus.group(1) != "true":
            continue
        package = re.search(r"(?:^|\s)pkg:([^\s]+)", header)
        if package is None:
            return None
        packages.append(package.group(1))
    return packages[0] if len(packages) == 1 else None


def verified_launcher(state):
    package = focused_package(state)
    return package if package in SUPPORTED_LAUNCHERS else None
