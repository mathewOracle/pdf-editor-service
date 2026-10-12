"""Find and remove watermarks that live in a PDF's text layer.

Watermarks in the *pixels* of a scan are a different problem; that's
handled by image_utils.recolor_background (light marks -> paper color).

A text watermark is just text, so "remove it" means redacting its glyphs.
The catch: a diagonal watermark's bounding box covers half the page, and
redacting that box would delete the real text under it. So each watermark
glyph gets a tiny probe rectangle placed somewhere inside the glyph that
doesn't touch any other text, and only the probes are redacted.
"""
import itertools
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

import pymupdf as fitz

ANNOT_KEY = "annot"
_TEXT_KEY_PREFIX = "text:"

_MIN_ROTATED_SIZE = 18.0  # rotated text this big is almost never body copy
_MIN_FADED_SIZE = 24.0  # big *and* pale/see-through text
_MIN_SEE_THROUGH_SIZE = 14.0
_LIGHT_LUMINANCE = 0.55  # 0 = black, 1 = white
_MAX_TEXT_LEN = 60
_PROBE_HALF = 0.3  # points; the probe is a 0.6pt square
_PROBE_GRID = [i / 8 for i in range(1, 8)]  # where inside a glyph to try


@dataclass
class WatermarkCandidate:
    key: str  # stable id the UI sends back, e.g. "text:CONFIDENTIAL"
    label: str
    reason: str
    pages: set[int] = field(default_factory=set)


def _line_text(line: dict) -> str:
    return "".join(c["c"] for s in line["spans"] for c in s["chars"]).strip()


def _luminance(srgb: int) -> float:
    r, g, b = (srgb >> 16) & 255, (srgb >> 8) & 255, srgb & 255
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def watermark_reason(line: dict) -> Optional[str]:
    """Why this text line looks like a watermark, or None if it doesn't."""
    spans = line["spans"]
    text = _line_text(line)
    if not spans or not text or len(text) > _MAX_TEXT_LEN:
        return None
    size = max(s["size"] for s in spans)
    rotated = abs(line["dir"][1]) > 0.1
    light = _luminance(spans[0]["color"]) >= _LIGHT_LUMINANCE
    see_through = spans[0].get("alpha", 255) < 255

    if rotated and size >= _MIN_ROTATED_SIZE:
        return "large, rotated"
    if see_through and size >= _MIN_SEE_THROUGH_SIZE:
        return "see-through"
    if light and size >= _MIN_FADED_SIZE:
        return "large, pale"
    return None


def _lines(page: fitz.Page) -> Iterator[dict]:
    for block in page.get_text("rawdict")["blocks"]:
        yield from block.get("lines", [])


def _watermark_annots(page: fitz.Page) -> list:
    return [a for a in page.annots() or [] if a.type[0] == fitz.PDF_ANNOT_WATERMARK]


def find_watermarks(doc: fitz.Document) -> list[WatermarkCandidate]:
    found: dict[str, WatermarkCandidate] = {}
    for page in doc:
        for line in _lines(page):
            reason = watermark_reason(line)
            if reason:
                text = _line_text(line)
                key = _TEXT_KEY_PREFIX + text
                found.setdefault(key, WatermarkCandidate(key, text, reason)).pages.add(page.number)
        if _watermark_annots(page):
            found.setdefault(
                ANNOT_KEY, WatermarkCandidate(ANNOT_KEY, "Watermark annotation", "marked as watermark")
            ).pages.add(page.number)
    return sorted(found.values(), key=lambda c: (-len(c.pages), c.label))


def _probe(quad: fitz.Quad, obstacles: list[fitz.Rect]) -> Optional[fitz.Rect]:
    """A tiny rect inside ``quad`` that touches none of ``obstacles``."""
    for u, v in itertools.product(_PROBE_GRID, repeat=2):
        top = quad.ul + (quad.ur - quad.ul) * u
        bottom = quad.ll + (quad.lr - quad.ll) * u
        pt = top + (bottom - top) * v
        rect = fitz.Rect(pt.x - _PROBE_HALF, pt.y - _PROBE_HALF, pt.x + _PROBE_HALF, pt.y + _PROBE_HALF)
        if not any(rect.intersects(o) for o in obstacles):
            return rect
    return None


def remove_watermarks(doc: fitz.Document, keys: Iterable[str]) -> int:
    """Remove the chosen watermarks in place. Returns how many glyphs had
    to be left alone because every spot on them overlapped real text.
    """
    keys = set(keys)
    if not keys:
        return 0
    texts = {k[len(_TEXT_KEY_PREFIX):] for k in keys if k.startswith(_TEXT_KEY_PREFIX)}
    skipped = 0
    for page in doc:
        if ANNOT_KEY in keys:
            for annot in _watermark_annots(page):
                page.delete_annot(annot)
        if not texts:
            continue

        targets, keep = [], []
        for line in _lines(page):
            is_target = _line_text(line) in texts and watermark_reason(line) is not None
            (targets if is_target else keep).append(line)
        if not targets:
            continue

        keep_rects = [fitz.Rect(c["bbox"]) for line in keep for s in line["spans"] for c in s["chars"]]
        for line in targets:
            nearby = [r for r in keep_rects if r.intersects(line["bbox"])]
            for span in line["spans"]:
                for char in span["chars"]:
                    if not char["c"].strip():
                        continue
                    probe = _probe(fitz.recover_char_quad(line["dir"], span, char), nearby)
                    if probe is None:
                        skipped += 1
                    else:
                        page.add_redact_annot(probe)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
    return skipped
