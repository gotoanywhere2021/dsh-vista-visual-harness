"""Image operations behind ``inspect`` and ``read_pixels``.

The paper's two visual tools are both defined over *archived original frames*:

* ``inspect``      crops ``region`` from the original frame and enlarges it;
* ``read_pixels``  splits ``region`` into ``rows x cols`` cells and reports the
                   exact colour of every cell (palette + indices + RGB).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

RESAMPLE_NEAREST = Image.Resampling.NEAREST
RESAMPLE_BILINEAR = Image.Resampling.BILINEAR
RESAMPLE_LANCZOS = Image.Resampling.LANCZOS


def clamp_region(
    region: tuple[int, int, int, int], size: tuple[int, int]
) -> tuple[tuple[int, int, int, int], bool]:
    """Clamp a region into the image; returns (region, clamped)."""
    x, y, width, height = region
    image_width, image_height = size
    clamped = False
    if x < 0:
        width += x
        x = 0
        clamped = True
    if y < 0:
        height += y
        y = 0
        clamped = True
    if x + width > image_width:
        width = image_width - x
        clamped = True
    if y + height > image_height:
        height = image_height - y
        clamped = True
    if width <= 0 or height <= 0:
        raise ValueError(f"region {region} does not intersect an image of size {size}")
    return (int(x), int(y), int(width), int(height)), clamped


def crop(image: Image.Image, region: tuple[int, int, int, int]) -> Image.Image:
    x, y, width, height = region
    return image.crop((x, y, x + width, y + height))


def zoom(
    image: Image.Image,
    factor: float,
    resample: Image.Resampling = RESAMPLE_NEAREST,
    max_side: int = 2048,
) -> Image.Image:
    """Enlarge by ``factor``. The enlarged view is capped to keep file sizes sane."""
    factor = max(1.0, float(factor))
    width = max(1, int(round(image.width * factor)))
    height = max(1, int(round(image.height * factor)))
    longest = max(width, height)
    if longest > max_side:
        scale = max_side / longest
        width = max(1, int(round(width * scale)))
        height = max(1, int(round(height * scale)))
    return image.resize((width, height), resample)


def fit_observation(image: Image.Image, max_side: int, scale: float | None = None) -> Image.Image:
    """Build the per-turn observation image the model receives.

    The paper feeds a downsampled render (512x512 PNG from an 8x nearest upscale
    of the 64x64 frame) and keeps the original lossless in memory. Here screen
    frames are already high resolution, so the observation is only ever shrunk
    to ``max_side`` unless an explicit ``scale`` is requested.
    """
    if scale is not None:
        return zoom(image, scale, RESAMPLE_NEAREST, max_side=max(max_side, int(image.width * scale), 8192))
    longest = max(image.width, image.height)
    if longest <= max_side:
        return image.copy()
    factor = max_side / longest
    return image.resize(
        (max(1, int(image.width * factor)), max(1, int(image.height * factor))),
        RESAMPLE_LANCZOS,
    )


def save_png(image: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=False, compress_level=6)
    return path


def _font(size: int = 14) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - older Pillow
        return ImageFont.load_default()


def montage(items: list[tuple[str, Image.Image]], gap: int = 8, label_height: int = 24) -> Image.Image:
    """Stack labeled views vertically into one image (one read_image call)."""
    if not items:
        raise ValueError("montage needs at least one view")
    longest = max(image.width for _, image in items)
    total_height = sum(image.height + label_height + gap for _, image in items) + gap
    canvas = Image.new("RGB", (longest + 2 * gap, total_height), (245, 245, 245))
    draw = ImageDraw.Draw(canvas)
    font = _font(14)
    cursor_y = gap
    for label, image in items:
        draw.rectangle((0, cursor_y, canvas.width, cursor_y + label_height), fill=(32, 32, 32))
        draw.text((gap, cursor_y + 4), label, fill=(240, 240, 240), font=font)
        cursor_y += label_height
        canvas.paste(image, (gap, cursor_y))
        cursor_y += image.height + gap
    return canvas


def _cell_color(cell: Image.Image) -> tuple[tuple[int, int, int], float]:
    """Dominant RGB of a cell plus the fraction of pixels sharing it."""
    colors = list(cell.convert("RGB").getdata())
    if not colors:
        return (0, 0, 0), 0.0
    counts = Counter(colors)
    color, count = counts.most_common(1)[0]
    return color, count / len(colors)


def read_pixels(
    image: Image.Image,
    region: tuple[int, int, int, int],
    rows: int,
    cols: int,
) -> dict[str, Any]:
    """Split ``region`` into ``rows x cols`` cells and report exact colours."""
    region, clamped = clamp_region(region, image.size)
    x, y, width, height = region
    if rows <= 0 or cols <= 0:
        raise ValueError("rows and cols must be positive")
    if rows > height or cols > width:
        raise ValueError(
            f"rows x cols = {rows}x{cols} exceeds the {width}x{height} region; "
            "each cell must cover at least one pixel"
        )
    cell_width = width / cols
    cell_height = height / rows
    working = crop(image, region)
    palette: list[str] = []
    palette_index: dict[tuple[int, int, int], int] = {}
    grid: list[list[int]] = []
    rgb_grid: list[list[list[int]]] = []
    mixed: list[dict[str, Any]] = []
    totals: Counter = Counter()

    for row in range(rows):
        top = int(round(row * cell_height))
        bottom = int(round((row + 1) * cell_height))
        bottom = max(bottom, top + 1)
        index_row: list[int] = []
        rgb_row: list[list[int]] = []
        for col in range(cols):
            left = int(round(col * cell_width))
            right = int(round((col + 1) * cell_width))
            right = max(right, left + 1)
            cell = working.crop((left, top, right, bottom))
            color, purity = _cell_color(cell)
            if color not in palette_index:
                palette_index[color] = len(palette)
                palette.append("#%02x%02x%02x" % color)
            index_row.append(palette_index[color])
            rgb_row.append(list(color))
            totals[color] += 1
            if purity < 0.9:
                mixed.append({"cell": [row, col], "purity": round(purity, 3)})
        grid.append(index_row)
        rgb_grid.append(rgb_row)

    return {
        "region": list(region),
        "clamped": clamped,
        "rows": rows,
        "cols": cols,
        "cell_size": [round(cell_width, 3), round(cell_height, 3)],
        "palette": palette,
        "grid": grid,
        "rgb": rgb_grid,
        "mixed_cells": mixed[:64],
        "distinct_colors": len(palette),
        "top_colors": [
            {"hex": "#%02x%02x%02x" % color, "cells": count}
            for color, count in totals.most_common(12)
        ],
    }
