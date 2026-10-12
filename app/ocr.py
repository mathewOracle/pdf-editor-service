"""Read text out of scanned pages (OCR), so it can be edited like real text.

Uses Apple's Vision framework: built into macOS, runs fully offline, and
reads printed and handwritten text well. On other systems ``available()``
is False and the editor falls back to "drag a box to replace".

Results are cached per page next to the uploaded PDF. The scan never
changes, so each page is only read once.

Scans have no font information, so the style is estimated from the pixels:
size from the line height, color from the darkest ink, bold from how thick
the strokes are compared with the rest of the page.
"""
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pymupdf as fitz

log = logging.getLogger(__name__)

OCR_DPI = 200
# Measured on rendered test pages: a Vision line box is ~0.92-1.04x the
# font size, and the baseline sits ~25% of the box height above its bottom.
_BOX_HEIGHT_PER_PT = 0.96
_BASELINE_FROM_BOTTOM = 0.25
# Replacing scanned text paints over its pixels, so the box is grown a bit
# to catch anti-aliased edges and descenders the OCR box cuts close.
_PAD_X = 0.15  # of the line height, each side
_PAD_Y = 0.18
_BOLD_INK_RATIO = 1.35  # stroke density vs the page's typical line
_INK_LEVEL = 140  # 0-255 luminance; darker than this counts as ink
_MIN_CONFIDENCE = 0.3
_CACHE_VERSION = 1

try:
    import Quartz
    import Vision
    from Foundation import NSData

    _VISION = True
except ImportError:  # not macOS, or pyobjc-framework-Vision not installed
    _VISION = False


def available() -> bool:
    return _VISION


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    # Tight box from OCR, as fractions of the page (top-left origin).
    x0: float
    y0: float
    x1: float
    y1: float
    font_size: float
    color: str
    bold: bool
    baseline: float  # fraction of page height


def page_lines(pdf_path: Path, page_number: int, cache_dir: Path) -> list[OcrLine]:
    """OCR one page (cached). Empty when OCR isn't available."""
    if not _VISION:
        return []
    cache = cache_dir / f"ocr_page_{page_number}.json"
    if cache.exists():
        data = json.loads(cache.read_text())
        if data.get("version") == _CACHE_VERSION:
            return [OcrLine(**line) for line in data["lines"]]
    with fitz.open(pdf_path) as doc:
        page = doc[page_number]
        pix = page.get_pixmap(dpi=OCR_DPI, alpha=False)
        page_height_pt = page.rect.height
    lines = _read_lines(pix, page_height_pt)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"version": _CACHE_VERSION, "lines": [asdict(x) for x in lines]}))
    return lines


def edit_box(line: OcrLine) -> tuple[float, float, float, float]:
    """The OCR box padded out, for painting the old text away. Fractions."""
    h = line.y1 - line.y0
    # Horizontal padding is in page-height units scaled to width. Close
    # enough at A4/Letter ratios and much simpler than carrying page sizes.
    return (
        max(line.x0 - h * _PAD_X, 0.0),
        max(line.y0 - h * _PAD_Y, 0.0),
        min(line.x1 + h * _PAD_X, 1.0),
        min(line.y1 + h * _PAD_Y, 1.0),
    )


def _read_lines(pix: fitz.Pixmap, page_height_pt: float) -> list[OcrLine]:
    observations = _vision_lines(pix.tobytes("png"))
    rgb = _rgb(pix)
    raw = []
    for text, confidence, (x0, y0, x1, y1) in observations:
        if confidence < _MIN_CONFIDENCE or not text.strip():
            continue
        ink = _ink_stats(rgb, x0, y0, x1, y1)
        raw.append((text.strip(), confidence, (x0, y0, x1, y1), ink))
    densities = [ink[1] for *_, ink in raw if ink[1] > 0]
    typical = float(np.median(densities)) if densities else 0.0
    lines = []
    for text, confidence, (x0, y0, x1, y1), (color, density) in raw:
        box_h = y1 - y0
        lines.append(OcrLine(
            text=text,
            confidence=round(confidence, 2),
            x0=x0, y0=y0, x1=x1, y1=y1,
            font_size=_round_half(box_h * page_height_pt / _BOX_HEIGHT_PER_PT),
            color=color,
            bold=typical > 0 and density > typical * _BOLD_INK_RATIO,
            baseline=y1 - box_h * _BASELINE_FROM_BOTTOM,
        ))
    return lines


def _vision_lines(png: bytes) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    """(text, confidence, (x0, y0, x1, y1)) per line, top-left origin."""
    data = NSData.dataWithBytes_length_(png, len(png))
    source = Quartz.CGImageSourceCreateWithData(data, None)
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, error = handler.performRequests_error_([request], None)
    if not ok:
        log.warning("OCR failed: %s", error)
        return []
    lines = []
    for result in request.results() or []:
        best = result.topCandidates_(1)[0]
        box = result.boundingBox()  # normalized, origin bottom-left
        x0, w, h = box.origin.x, box.size.width, box.size.height
        y0 = 1.0 - box.origin.y - h
        lines.append((str(best.string()), float(best.confidence()), (x0, y0, x0 + w, y0 + h)))
    return lines


def _rgb(pix: fitz.Pixmap) -> np.ndarray:
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]


def _ink_stats(rgb: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> tuple[str, float]:
    """(ink color as #rrggbb, share of the box that's ink)."""
    h, w = rgb.shape[:2]
    crop = rgb[int(y0 * h):max(int(y1 * h), int(y0 * h) + 1), int(x0 * w):max(int(x1 * w), int(x0 * w) + 1)]
    if crop.size == 0:
        return "#000000", 0.0
    pixels = crop.reshape(-1, 3).astype(np.int32)
    luminance = pixels @ np.array([299, 587, 114]) // 1000
    ink = pixels[luminance < _INK_LEVEL]
    if len(ink) == 0:
        return "#000000", 0.0
    # The darkest third of the ink is the stroke core, free of the paper
    # color that blends into anti-aliased edges.
    core = ink[np.argsort(ink.sum(axis=1))[: max(len(ink) // 3, 1)]]
    r, g, b = (int(c) for c in np.median(core, axis=0))
    return f"#{r:02x}{g:02x}{b:02x}", len(ink) / len(pixels)


def _round_half(value: float) -> float:
    return max(round(value * 2) / 2, 4.0)


def is_covered(line: OcrLine, box: tuple[float, float, float, float], share: float = 0.5) -> bool:
    """Whether at least ``share`` of the line is inside ``box``, i.e. it has
    already been replaced or erased and shouldn't be offered again.
    """
    x0, y0, x1, y1 = box
    ix = max(0.0, min(line.x1, x1) - max(line.x0, x0))
    iy = max(0.0, min(line.y1, y1) - max(line.y0, y0))
    area = (line.x1 - line.x0) * (line.y1 - line.y0)
    return area > 0 and ix * iy >= share * area
