#!/usr/bin/env python3
"""Render a polished NanoHarness macOS icon from scratch."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil

from PIL import Image, ImageChops, ImageDraw, ImageFilter


MASTER_SIZE = 1024
ICONSET_EXPORTS = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def _hex(color: str) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    if len(color) == 6:
        color += "ff"
    return tuple(int(color[i : i + 2], 16) for i in range(0, 8, 2))


def _vertical_gradient(size: int, top: str, bottom: str) -> Image.Image:
    image = Image.new("RGBA", (size, size))
    top_rgba = _hex(top)
    bottom_rgba = _hex(bottom)
    pixels = image.load()
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(
            round(top_rgba[i] + (bottom_rgba[i] - top_rgba[i]) * t) for i in range(4)
        )
        for x in range(size):
            pixels[x, y] = color
    return image


def _radial_blob(size: int, color: str, radius: float, center: tuple[float, float]) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    cx = int(size * center[0])
    cy = int(size * center[1])
    max_r = int(size * radius)
    base = _hex(color)
    for r in range(max_r, 0, -1):
        alpha = int(base[3] * (r / max_r) ** 2 * 0.12)
        if alpha <= 0:
            continue
        fill = (base[0], base[1], base[2], alpha)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
    return image.filter(ImageFilter.GaussianBlur(size // 28))


def _rounded_mask(size: int, inset: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((inset, inset, size - inset, size - inset), radius=radius, fill=255)
    return mask


def _apply_mask(image: Image.Image, mask: Image.Image) -> Image.Image:
    result = image.copy()
    result.putalpha(ImageChops.multiply(result.getchannel("A"), mask))
    return result


def _draw_surface(base: Image.Image) -> None:
    size = base.size[0]
    tile_inset = size // 18
    tile_radius = size // 5
    tile_mask = _rounded_mask(size, tile_inset, tile_radius)

    tile = _vertical_gradient(size, "#0f1d3dff", "#09111fff")
    tile.alpha_composite(_radial_blob(size, "#2dd4bfcc", 0.44, (0.27, 0.26)))
    tile.alpha_composite(_radial_blob(size, "#38bdf8cc", 0.38, (0.74, 0.30)))
    tile.alpha_composite(_radial_blob(size, "#fb923ccc", 0.34, (0.70, 0.78)))
    tile.alpha_composite(_radial_blob(size, "#f97316aa", 0.22, (0.24, 0.82)))
    tile = _apply_mask(tile, tile_mask)

    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (tile_inset, tile_inset + size // 26, size - tile_inset, size - tile_inset + size // 26),
        radius=tile_radius,
        fill=(4, 9, 18, 255),
    )
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(size // 20)))
    base.alpha_composite(tile)

    stroke = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    stroke_draw = ImageDraw.Draw(stroke)
    stroke_draw.rounded_rectangle(
        (tile_inset, tile_inset, size - tile_inset, size - tile_inset),
        radius=tile_radius,
        outline=(255, 255, 255, 52),
        width=max(4, size // 64),
    )
    base.alpha_composite(stroke)

    gloss = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gloss_draw = ImageDraw.Draw(gloss)
    gloss_draw.rounded_rectangle(
        (tile_inset + size // 22, tile_inset + size // 22, size - tile_inset - size // 22, size // 2),
        radius=size // 10,
        fill=(255, 255, 255, 24),
    )
    base.alpha_composite(gloss.filter(ImageFilter.GaussianBlur(size // 18)))


def _draw_terminal_card(base: Image.Image) -> tuple[int, int, int, int]:
    size = base.size[0]
    card = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    left = size * 0.18
    top = size * 0.18
    right = size * 0.82
    bottom = size * 0.82
    radius = size // 9
    draw.rounded_rectangle((left, top, right, bottom), radius=radius, fill=(7, 13, 25, 228))
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=radius,
        outline=(255, 255, 255, 48),
        width=max(3, size // 90),
    )
    draw.rounded_rectangle(
        (left, top, right, top + size * 0.1),
        radius=radius,
        fill=(255, 255, 255, 18),
    )
    bar_y = int(top + size * 0.05)
    for idx, fill in enumerate(("#fb7185", "#fbbf24", "#34d399")):
        cx = int(left + size * 0.07 + idx * size * 0.045)
        r = max(5, size // 70)
        draw.ellipse((cx - r, bar_y - r, cx + r, bar_y + r), fill=_hex(fill))

    base.alpha_composite(card.filter(ImageFilter.GaussianBlur(size // 40)))
    base.alpha_composite(card)
    return int(left), int(top), int(right), int(bottom)


def _line(draw: ImageDraw.ImageDraw, pts: list[tuple[int, int]], fill: str, width: int) -> None:
    draw.line(pts, fill=_hex(fill), width=width, joint="curve")


def _lerp(a: tuple[int, int], b: tuple[int, int], t: float) -> tuple[int, int]:
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
    )


def _draw_mark(base: Image.Image, bounds: tuple[int, int, int, int]) -> None:
    size = base.size[0]
    left, top, right, bottom = bounds
    width = right - left
    height = bottom - top

    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    glow_draw = ImageDraw.Draw(glow)

    stem_stroke = max(12, size // 15)
    diag_stroke = max(8, stem_stroke // 2)
    glow_stem_stroke = stem_stroke + size // 34
    glow_diag_stroke = diag_stroke + size // 28

    p1 = (int(left + width * 0.22), int(top + height * 0.78))
    p2 = (int(left + width * 0.22), int(top + height * 0.24))
    p3 = (int(left + width * 0.73), int(top + height * 0.78))
    p4 = (int(left + width * 0.73), int(top + height * 0.24))
    dot = (int((p2[0] + p3[0]) / 2), int((p2[1] + p3[1]) / 2))
    diag_start = _lerp(p2, p3, 0.09)
    diag_end = _lerp(p2, p3, 0.91)

    _line(glow_draw, [diag_start, diag_end], "#f8fafc55", glow_diag_stroke)
    _line(glow_draw, [p1, p2], "#38bdf866", glow_stem_stroke)
    _line(glow_draw, [p3, p4], "#fb923c66", glow_stem_stroke)
    _line(draw, [diag_start, diag_end], "#f8fafcff", diag_stroke)
    _line(draw, [p1, p2], "#7dd3fcff", stem_stroke)
    _line(draw, [p3, p4], "#fb923cff", stem_stroke)

    dot_r = max(16, size // 18)
    glow_draw.ellipse(
        (dot[0] - dot_r * 2, dot[1] - dot_r * 2, dot[0] + dot_r * 2, dot[1] + dot_r * 2),
        fill=_hex("#f8fafc22"),
    )
    draw.ellipse(
        (dot[0] - dot_r, dot[1] - dot_r, dot[0] + dot_r, dot[1] + dot_r),
        fill=_hex("#f8fafcff"),
        outline=_hex("#dbe4f1ff"),
        width=max(2, size // 120),
    )
    inner_r = int(dot_r * 0.68)
    draw.ellipse(
        (dot[0] - inner_r, dot[1] - inner_r, dot[0] + inner_r, dot[1] + inner_r),
        fill=_hex("#1f2937ff"),
    )

    prompt_y = int(top + height * 0.8)
    prompt_x = int(left + width * 0.31)
    prompt_w = max(8, size // 36)
    draw.line(
        (
            prompt_x,
            prompt_y,
            prompt_x + size // 34,
            prompt_y - size // 34,
            prompt_x,
            prompt_y - size // 18,
        ),
        fill=_hex("#f59e0b"),
        width=prompt_w,
        joint="curve",
    )
    draw.rounded_rectangle(
        (
            prompt_x + size // 26,
            prompt_y + size // 70,
            prompt_x + size // 7,
            prompt_y + size // 26,
        ),
        radius=max(4, prompt_w // 2),
        fill=_hex("#f8fafc"),
    )

    base.alpha_composite(glow.filter(ImageFilter.GaussianBlur(size // 26)))
    base.alpha_composite(layer)


def _make_master_icon() -> Image.Image:
    base = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    _draw_surface(base)
    card_bounds = _draw_terminal_card(base)
    _draw_mark(base, card_bounds)
    return base


def build_iconset(iconset_dir: Path, preview_png: Path | None = None) -> None:
    iconset_dir.mkdir(parents=True, exist_ok=True)
    master = _make_master_icon()
    if preview_png is not None:
        preview_png.parent.mkdir(parents=True, exist_ok=True)
        master.save(preview_png)
    for filename, size in ICONSET_EXPORTS:
        image = master.resize((size, size), Image.Resampling.LANCZOS)
        image.save(iconset_dir / filename)


def save_icns(output_path: Path, preview_png: Path | None = None) -> None:
    master = _make_master_icon()
    if preview_png is not None:
        preview_png.parent.mkdir(parents=True, exist_ok=True)
        master.save(preview_png)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    master.save(
        output_path,
        format="ICNS",
        sizes=[(16, 16), (32, 32), (128, 128), (256, 256), (512, 512)],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="Destination .icns path.")
    parser.add_argument(
        "--workdir",
        help="Temporary iconset directory. Defaults to <output>.iconset.",
    )
    parser.add_argument(
        "--preview-png",
        help="Optional path to save the full-resolution preview PNG.",
    )
    args = parser.parse_args()

    output_path = Path(args.output).resolve()
    workdir = Path(args.workdir).resolve() if args.workdir else output_path.with_suffix(".iconset")
    preview_png = Path(args.preview_png).resolve() if args.preview_png else None

    if workdir.exists():
        shutil.rmtree(workdir)

    build_iconset(workdir)
    save_icns(output_path, preview_png=preview_png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
