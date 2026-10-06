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


def image_pixel_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size
