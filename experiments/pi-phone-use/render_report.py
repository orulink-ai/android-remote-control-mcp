#!/usr/bin/env python3
"""Render local experiment artifacts into an offline report; no model/device calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_public_data import build
from config import output_dir

TEMPLATE = Path(__file__).with_name("report-template.html")
MARKER = '<script id="report-data" type="application/json">'


def render(
    base: Path | None = None, report_dir: Path | None = None, historical: Path | None = None
) -> Path:
    base = (base or output_dir()).resolve()
    report_dir = (report_dir or base / "report").resolve()
    data = build(base, historical, report_dir)
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    # HTML raw-text elements terminate on </script> even with application/json.
    embedded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = TEMPLATE.read_text()
    if template.count(MARKER) != 1:
        raise ValueError("Expected exactly one report-data element")
    prefix, remainder = template.split(MARKER, 1)
    _, suffix = remainder.split("</script>", 1)
    html = prefix + MARKER + embedded + "</script>" + suffix
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "public-data.json").write_text(encoded + "\n")
    result = report_dir / "phone-use-optimized.html"
    result.write_text(html)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", type=Path, default=None, help="Run artifacts; default: config.output_dir()"
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Report and relative video directory; default: BASE/report",
    )
    parser.add_argument(
        "--historical",
        type=Path,
        default=None,
        help="Optional prior public-data JSON; absent files remain unavailable",
    )
    args = parser.parse_args()
    path = render(args.base, args.report_dir, args.historical)
    print(json.dumps({"report": str(path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
