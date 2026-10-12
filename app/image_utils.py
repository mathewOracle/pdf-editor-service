"""Image helpers for signature/stamp assets."""
from pathlib import Path

import numpy as np
from PIL import Image


def make_background_transparent(src_path: Path, dst_path: Path, threshold: int = 235) -> None:
    """Turn near-white pixels into transparent ones.

    Signatures are almost always photographed/scanned on a white or
    off-white background. Without this, placing them on a form shows an
    ugly white rectangle instead of blending with the page. Pixels whose
    R, G and B are all above ``threshold`` become fully transparent; the
    rest keep their original color with full opacity (no soft fade, which
    would make ink strokes look faded).
    """
    img = Image.open(src_path).convert("RGBA")
    arr = np.asarray(img).copy()

    rgb = arr[:, :, :3]
    is_background = np.all(rgb >= threshold, axis=-1)
    arr[is_background, 3] = 0

    Image.fromarray(arr, mode="RGBA").save(dst_path, format="PNG")


RGB = tuple[int, int, int]

# Pixels a bit *beyond* the tolerance get partially shifted too, so the
# anti-aliased edges of letters don't keep a ring of the old paper color.
_SOFT_EDGE = 0.5


def detect_background_rgb(arr: np.ndarray) -> RGB:
    """The paper color: the most common color on the page.

    Colors are bucketed (32 levels per channel) first, because scan noise
    means almost no two "paper" pixels are exactly equal. The answer is
    the real average of the pixels in the winning bucket, not the bucket.
    """
    rgb = arr[:, :, :3].reshape(-1, 3)
    buckets = (rgb >> 3).astype(np.int32)
    keys = (buckets[:, 0] << 10) | (buckets[:, 1] << 5) | buckets[:, 2]
    winner = np.bincount(keys).argmax()
    mean = rgb[keys == winner].mean(axis=0)
    return tuple(int(round(c)) for c in mean)


def recolor_background(arr: np.ndarray, target: RGB, tolerance: int) -> np.ndarray:
    """Swap the detected paper color for ``target``.

    ``tolerance`` (0-255) is how far a pixel can be from the paper color,
    on its most-different channel, and still count as paper. Paper pixels
    become exactly ``target``. Pixels in a soft band past that are moved
    part of the way, and ink stays as it is.
    """
    rgb = arr[:, :, :3].astype(np.int16)
    background = np.array(detect_background_rgb(arr), dtype=np.int16)
    shift = np.array(target, dtype=np.int16) - background

    distance = np.abs(rgb - background).max(axis=-1)
    band = max(tolerance * _SOFT_EDGE, 1.0)
    weight = np.clip((tolerance + band - distance) / band, 0.0, 1.0)

    out = rgb + (weight[..., None] * shift).round().astype(np.int16)
    out[distance <= tolerance] = target
    result = arr.copy()
    result[:, :, :3] = np.clip(out, 0, 255).astype(np.uint8)
    return result


def image_pixel_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size
