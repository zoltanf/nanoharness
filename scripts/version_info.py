#!/usr/bin/env python3
"""Generate and persist NanoHarness build version metadata."""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nanoharness.buildmeta import default_display_version, parse_display_version, write_version_file


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Explicit display version (YYYY.MM.DD.HHMM).")
    parser.add_argument(
        "--format",
        choices=("plain", "shell"),
        default="plain",
        help="Output format.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the selected display version into nanoharness/_version.py.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    display_version = args.version or default_display_version()
    parsed = parse_display_version(display_version)

    if args.write:
        write_version_file(parsed.display)

    if args.format == "shell":
        pairs = {
            "NANOHARNESS_BUILD_VERSION": parsed.display,
            "NANOHARNESS_PACKAGE_VERSION": parsed.package,
            "NANOHARNESS_BUNDLE_SHORT_VERSION": parsed.bundle_short,
            "NANOHARNESS_BUNDLE_BUILD_VERSION": parsed.bundle_build,
        }
        for key, value in pairs.items():
            print(f"{key}={shlex.quote(value)}")
    else:
        print(parsed.display)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
