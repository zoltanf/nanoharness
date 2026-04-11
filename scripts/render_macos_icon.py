#!/usr/bin/env python3
"""Render a simple NanoHarness icon and convert it into a macOS .icns bundle."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw


ICON_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _rounded_rect(draw: ImageDraw.ImageDraw, size: int, fill: str) -> None:
    margin = max(1, size // 14)
    radius = size // 5
    draw.rounded_rectangle(
        (margin, margin, size - margin, size - margin),
        radius=radius,
        fill=fill,
    )


def _make_base_image(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), "#0b1220")
    draw = ImageDraw.Draw(image)
    _rounded_rect(draw, size, "#15233d")

    inner_margin = size // 6
    draw.rounded_rectangle(
        (inner_margin, inner_margin, size - inner_margin, size - inner_margin),
        radius=size // 8,
        outline="#f97316",
        width=max(2, size // 28),
    )

    line_w = max(2, size // 24)
    left = inner_margin + size // 12
    right = size - left
    top = inner_margin + size // 8
    bottom = size - top

    draw.line((left, top, left, bottom), fill="#38bdf8", width=line_w)
    draw.line((left, top, size // 2, size // 2), fill="#38bdf8", width=line_w)
    draw.line((size // 2, size // 2, right, top), fill="#22c55e", width=line_w)
    draw.line((right, top, right, bottom), fill="#22c55e", width=line_w)
    draw.line((left, bottom, right, bottom), fill="#e2e8f0", width=line_w)
    return image


def build_iconset(iconset_dir: Path) -> None:
    iconset_dir.mkdir(parents=True, exist_ok=True)
    for size in ICON_SIZES:
        image = _make_base_image(size)
        image.save(iconset_dir / f"icon_{size}x{size}.png")
        if size <= 512:
            image.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(
                iconset_dir / f"icon_{size}x{size}@2x.png"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Destination .icns path.")
    parser.add_argument(
        "--workdir",
        help="Temporary iconset directory. Defaults to <output>.iconset.",
    )
    args = parser.parse_args()

    output_path = Path(args.output).resolve()
    workdir = Path(args.workdir).resolve() if args.workdir else output_path.with_suffix(".iconset")

    if shutil.which("iconutil") is None:
        raise SystemExit("iconutil is required to build a macOS .icns icon.")

    if workdir.exists():
        shutil.rmtree(workdir)

    build_iconset(workdir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["iconutil", "-c", "icns", str(workdir), "-o", str(output_path)],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
