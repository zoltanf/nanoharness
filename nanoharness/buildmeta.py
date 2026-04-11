"""Build metadata helpers shared by release scripts and tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

DISPLAY_VERSION_RE = re.compile(r"^(?P<year>\d{4})\.(?P<month>\d{2})\.(?P<day>\d{2})\.(?P<hour_min>\d{4})$")
VERSION_FILE = Path(__file__).with_name("_version.py")


@dataclass(frozen=True)
class BuildVersion:
    """Version strings for UI display and macOS packaging metadata."""

    display: str
    package: str
    bundle_short: str
    bundle_build: str


def default_display_version(now: datetime | None = None) -> str:
    """Return the default timestamp-based display version in local time."""
    when = now.astimezone() if now is not None else datetime.now().astimezone()
    return when.strftime("%Y.%m.%d.%H%M")


def parse_display_version(version: str) -> BuildVersion:
    """Validate and split a display version into packaging-friendly forms."""
    match = DISPLAY_VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(
            "Version must look like YYYY.MM.DD.HHMM, for example 2026.04.11.1229."
        )
    try:
        datetime.strptime(version, "%Y.%m.%d.%H%M")
    except ValueError as exc:
        raise ValueError(
            "Version must be a real timestamp like 2026.04.11.1229."
        ) from exc

    year = match.group("year")
    month = match.group("month")
    day = match.group("day")
    hour_min = match.group("hour_min")

    # macOS bundle metadata prefers normalized numeric segments.
    package = f"{year}.{int(month)}.{int(day)}.{int(hour_min)}"
    bundle_short = f"{year}.{int(month)}.{int(day)}"
    bundle_build = f"{year}{month}{day}{hour_min}"
    return BuildVersion(
        display=version,
        package=package,
        bundle_short=bundle_short,
        bundle_build=bundle_build,
    )


def render_version_file(version: str) -> str:
    """Render the generated version module content."""
    parsed = parse_display_version(version)
    return (
        '"""Build-time version stamp for NanoHarness."""\n\n'
        f'__version__ = "{parsed.display}"\n'
    )


def write_version_file(version: str, target: Path = VERSION_FILE) -> Path:
    """Write the display version to the generated version module."""
    target.write_text(render_version_file(version))
    return target
