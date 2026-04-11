"""Tests for NanoHarness build metadata helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from nanoharness.buildmeta import default_display_version, parse_display_version, write_version_file


def test_default_display_version_uses_timestamp_format():
    when = datetime(2026, 4, 11, 12, 29, tzinfo=timezone(timedelta(hours=2)))
    assert default_display_version(when) == "2026.04.11.1229"


def test_parse_display_version_derives_packaging_variants():
    parsed = parse_display_version("2026.04.11.1229")
    assert parsed.display == "2026.04.11.1229"
    assert parsed.package == "2026.4.11.1229"
    assert parsed.bundle_short == "2026.4.11"
    assert parsed.bundle_build == "202604111229"


def test_parse_display_version_rejects_invalid_values():
    with pytest.raises(ValueError, match="YYYY.MM.DD.HHMM"):
        parse_display_version("2026-04-11")

    with pytest.raises(ValueError, match="real timestamp"):
        parse_display_version("2026.13.99.2561")


def test_write_version_file_uses_display_version(tmp_path: Path):
    target = tmp_path / "_version.py"
    write_version_file("2026.04.11.1229", target)
    assert target.read_text() == '"""Build-time version stamp for NanoHarness."""\n\n__version__ = "2026.04.11.1229"\n'


def test_version_info_script_outputs_shell_exports():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/version_info.py",
            "--version",
            "2026.04.11.1229",
            "--format",
            "shell",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "NANOHARNESS_BUILD_VERSION=2026.04.11.1229" in result.stdout
    assert "NANOHARNESS_BUNDLE_SHORT_VERSION=2026.4.11" in result.stdout
