"""All PyMuPDF logic lives here: rendering previews, reading AcroForm
fields, and baking elements/field values into a final output PDF.
"""
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Optional

import fitz  # PyMuPDF
import numpy as np
from PIL import Image

from .image_utils import RGB, detect_background_rgb, recolor_background, remove_faint_marks
from .watermark import WatermarkCandidate, find_watermarks, remove_watermarks

RedactMode = Literal["text", "area", "image"]

# Raw PNG streams at 300dpi bloat a multi-page PDF to tens of MB. JPEG at
# quality 88 keeps text/ink crisp at a fraction of the size.
_RASTER_DPI = 300
_RASTER_JPEG_QUALITY = 88


@dataclass(frozen=True)
class Background:
    """Replace each page's paper color with ``rgb``. See
    image_utils.recolor_background for what ``tolerance`` means.
    """

    rgb: RGB
    tolerance: int

    @classmethod
    def from_hex(cls, hex_color: str, tolerance: int) -> "Background":
        h = hex_color.lstrip("#")
        return cls(rgb=(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)), tolerance=tolerance)


def rgb_to_hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


@dataclass(frozen=True)
class PixelCleanup:
    """Changes that can only be made to the rendered pixels of the original
    pages. Faint marks go first, so a watermark becomes paper before the
    paper gets recolored.
    """

    background: Optional[Background] = None
    faint_mark_strength: Optional[int] = None  # 0-100, None = off

    def __bool__(self) -> bool:
        return self.background is not None or self.faint_mark_strength is not None

    def apply(self, arr: np.ndarray) -> np.ndarray:
        if self.faint_mark_strength is not None:
            arr = remove_faint_marks(arr, self.faint_mark_strength)
        if self.background:
            arr = recolor_background(arr, self.background.rgb, self.background.tolerance)
        return arr


NO_CLEANUP = PixelCleanup()


def _page_rgb_array(page: fitz.Page, dpi: int) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)


def _page_image(page: fitz.Page, dpi: int, cleanup: PixelCleanup) -> Image.Image:
    return Image.fromarray(cleanup.apply(_page_rgb_array(page, dpi)), mode="RGB")


def _rasterize(doc: fitz.Document, cleanup: PixelCleanup = NO_CLEANUP) -> fitz.Document:
    """Turn every page into a single image (like a fresh scan), optionally
    cleaning up the pixels first. Closes ``doc`` and returns the new one.
    """
    out = fitz.open()
    for page in doc:
        buf = io.BytesIO()
        _page_image(page, _RASTER_DPI, cleanup).save(
            buf, format="JPEG", quality=_RASTER_JPEG_QUALITY
        )
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=buf.getvalue())
    doc.close()
    return out


@dataclass(frozen=True)
class Redaction:
    """A rectangle of *original* PDF content to genuinely remove.

    ``mode`` decides what gets removed inside the box:
      * ``text``  -- only the glyphs (form lines, logos, borders survive).
                     Used when editing a line that exists in the text layer.
      * ``area``  -- everything: text, vector art, and image *pixels*,
                     painted white. Used for scans / handwriting / stamps.
      * ``image`` -- the whole embedded image object touching the box.
                     Used for "remove this logo/picture".
    """

    page_number: int
    x0_frac: float
    y0_frac: float
    x1_frac: float
    y1_frac: float
    mode: RedactMode = "area"


# (images, graphics, text) flags for Page.apply_redactions, per mode.
_REDACT_FLAGS: dict[str, tuple[int, int, int]] = {
    "text": (fitz.PDF_REDACT_IMAGE_NONE, fitz.PDF_REDACT_LINE_ART_NONE, fitz.PDF_REDACT_TEXT_REMOVE),
    "area": (fitz.PDF_REDACT_IMAGE_PIXELS, fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED, fitz.PDF_REDACT_TEXT_REMOVE),
    "image": (fitz.PDF_REDACT_IMAGE_REMOVE, fitz.PDF_REDACT_LINE_ART_NONE, fitz.PDF_REDACT_TEXT_NONE),
}
# Text-mode boxes are shrunk vertically so glyphs of the lines directly
# above/below (whose bboxes often overlap a few points) aren't swept up.
_TEXT_BOX_VERTICAL_INSET = 0.2


def _redaction_rect(page: fitz.Page, r: Redaction) -> fitz.Rect:
    pw, ph = page.rect.width, page.rect.height
    rect = fitz.Rect(r.x0_frac * pw, r.y0_frac * ph, r.x1_frac * pw, r.y1_frac * ph)
    if r.mode == "text":
        inset = rect.height * _TEXT_BOX_VERTICAL_INSET
        rect = fitz.Rect(rect.x0, rect.y0 + inset, rect.x1, rect.y1 - inset)
    return rect


def apply_redactions(doc: fitz.Document, redactions: Iterable[Redaction]) -> None:
    """Apply redactions in place. apply_redactions() takes one set of flags
    for *all* pending annots on a page, so each mode gets its own pass.
    """
    by_page_mode: dict[tuple[int, str], list[Redaction]] = {}
    for r in redactions:
        by_page_mode.setdefault((r.page_number, r.mode), []).append(r)
    for (page_number, mode), items in by_page_mode.items():
        page = doc[page_number]
        fill = (1, 1, 1) if mode == "area" else False
        for r in items:
            page.add_redact_annot(_redaction_rect(page, r), fill=fill)
        images, graphics, text = _REDACT_FLAGS[mode]
        page.apply_redactions(images=images, graphics=graphics, text=text)


@dataclass(frozen=True)
class Edits:
    """Every change to *original* content: removed watermarks, then the
    user's redactions (replaced/erased text, removed images).
    """

    redactions: tuple[Redaction, ...] = ()
    watermarks: frozenset[str] = frozenset()


NO_EDITS = Edits()


def open_edited(pdf_path: Path, edits: Edits = NO_EDITS) -> fitz.Document:
    """Open the original with this document's edits applied in memory.
    Single source of truth for preview, text/image picking, and export, so
    what you see in the editor is exactly what gets exported.
    """
    doc = fitz.open(pdf_path)
    remove_watermarks(doc, edits.watermarks)
    apply_redactions(doc, edits.redactions)
    return doc


@dataclass
class FormField:
    name: str
    field_type: str  # "text", "checkbox", "choice", ...
    value: str
    page_number: int


def get_page_count(pdf_path: Path) -> int:
    with fitz.open(pdf_path) as doc:
        return len(doc)


def get_page_size(pdf_path: Path, page_number: int) -> tuple[float, float]:
    with fitz.open(pdf_path) as doc:
        rect = doc[page_number].rect
        return rect.width, rect.height


def get_page_heights(pdf_path: Path) -> list[float]:
    with fitz.open(pdf_path) as doc:
        return [page.rect.height for page in doc]


def render_page_png(
    pdf_path: Path,
    page_number: int,
    dpi: int = 150,
    edits: Edits = NO_EDITS,
    cleanup: PixelCleanup = NO_CLEANUP,
) -> bytes:
    with open_edited(pdf_path, edits) as doc:
        page = doc[page_number]
        if not cleanup:
            return page.get_pixmap(dpi=dpi).tobytes("png")
        buf = io.BytesIO()
        _page_image(page, dpi, cleanup).save(buf, format="PNG")
        return buf.getvalue()


def detect_page_background(pdf_path: Path, page_number: int) -> str:
    """Hex color of the page's paper, so the UI can show what it found."""
    with fitz.open(pdf_path) as doc:
        return rgb_to_hex(detect_background_rgb(_page_rgb_array(doc[page_number], dpi=72)))


def detect_watermarks(pdf_path: Path) -> list[WatermarkCandidate]:
    with fitz.open(pdf_path) as doc:
        return find_watermarks(doc)


_WIDGET_TYPE_NAMES = {
    fitz.PDF_WIDGET_TYPE_TEXT: "text",
    fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    fitz.PDF_WIDGET_TYPE_LISTBOX: "choice",
    fitz.PDF_WIDGET_TYPE_COMBOBOX: "choice",
}


def get_form_fields(pdf_path: Path) -> list[FormField]:
    fields: list[FormField] = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            for widget in page.widgets() or []:
                fields.append(
                    FormField(
                        name=widget.field_name,
                        field_type=_WIDGET_TYPE_NAMES.get(widget.field_type, "text"),
                        value=widget.field_value or "",
                        page_number=page.number,
                    )
                )
    return fields


@dataclass
class TextSpan:
    """One line of text already printed in the original PDF, as a unit a
    user could plausibly want to replace. Positions are fractions of page
    width/height (0..1), matching the convention Element uses.
    """

    text: str
    x0_frac: float
    y0_frac: float
    x1_frac: float
    y1_frac: float
    font_size: float
    color: str  # "#rrggbb" of the original text, so replacements match


@dataclass
class PageImage:
    """An embedded image (logo, photo, stamp) the user may want to remove."""

    x0_frac: float
    y0_frac: float
    x1_frac: float
    y1_frac: float


# An image covering at least this share of the page is the page itself
# (a scan). Offering "remove image" on it would blank the whole page, so
# scans are left to the drag-a-box area eraser instead.
_FULL_PAGE_IMAGE_SHARE = 0.85


def extract_images(
    pdf_path: Path, page_number: int, edits: Edits = NO_EDITS
) -> list[PageImage]:
    images: list[PageImage] = []
    with open_edited(pdf_path, edits) as doc:
        page = doc[page_number]
        pw, ph = page.rect.width, page.rect.height
        for info in page.get_image_info():
            bbox = fitz.Rect(info["bbox"]) & page.rect
            if bbox.is_empty or bbox.get_area() >= _FULL_PAGE_IMAGE_SHARE * page.rect.get_area():
                continue
            images.append(PageImage(bbox.x0 / pw, bbox.y0 / ph, bbox.x1 / pw, bbox.y1 / ph))
    return images


def extract_text_spans(
    pdf_path: Path, page_number: int, edits: Edits = NO_EDITS
) -> list[TextSpan]:
    """Line-level text extraction for the "edit existing text" feature.

    True PDF text editing (reflowing a run of text in place) isn't really
    a thing PyMuPDF -- or most PDF tooling -- supports, because PDF has no
    concept of a paragraph, just positioned glyphs. The standard approach
    or most "edit my PDF" tools use is what this app does too: find the
    original text's bounding box, redact (truly remove) exactly that box,
    and draw replacement text in its place. Line granularity is the
    practical sweet spot -- fine enough to target one field's answer
    without accidentally grabbing a whole paragraph.
    """
    spans: list[TextSpan] = []
    # Read from the *redacted* doc so lines already replaced/erased stop
    # showing up as pickable -- their overlay element is the handle now.
    with open_edited(pdf_path, edits) as doc:
        page = doc[page_number]
        pw, ph = page.rect.width, page.rect.height
        raw = page.get_text("dict")
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                line_spans = line.get("spans", [])
                text = "".join(s["text"] for s in line_spans).strip()
                if not text:
                    continue
                x0, y0, x1, y1 = line["bbox"]
                font_size = line_spans[0]["size"] if line_spans else 11.0
                color = f"#{line_spans[0]['color']:06x}" if line_spans else "#000000"
                spans.append(
                    TextSpan(
                        text=text,
                        x0_frac=x0 / pw,
                        y0_frac=y0 / ph,
                        x1_frac=x1 / pw,
                        y1_frac=y1 / ph,
                        font_size=font_size,
                        color=color,
                    )
                )
    return spans


def _hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return r / 255, g / 255, b / 255


def bake_pdf(
    original_path: Path,
    output_path: Path,
    *,
    field_values: dict[str, str],
    text_elements: list[dict],
    image_elements: list[dict],
    edits: Edits = NO_EDITS,
    cleanup: PixelCleanup = NO_CLEANUP,
    flatten: bool = False,
) -> None:
    """Write a fresh PDF with form fields filled and elements placed.

    ``edits`` are applied first (they rewrite the content stream, so
    anything drawn before them could get swept up). That's how editing or
    removing content that already existed in the PDF is implemented.
    ``cleanup`` (paper recolor, faint-mark removal) works on the *original*
    pages' pixels, so those pages become images; text/images you placed
    are drawn afterwards and stay crisp, and aren't touched by it.
    ``text_elements``: dicts with page_number, x_frac, y_frac, text,
    font_size, color. Empty text = pure erase, nothing drawn.
    ``image_elements``: dicts with page_number, x_frac, y_frac, width_frac,
    asset_path (already-resolved filesystem path to the image).

    If ``flatten`` is True the result is rasterized page-by-page (like a
    fresh scan) so nothing in it is interactive or editable anymore --
    useful when the destination only accepts a "flat" document.
    """
    doc = open_edited(original_path, edits)

    if field_values:
        for page in doc:
            for widget in page.widgets() or []:
                if widget.field_name in field_values:
                    widget.field_value = field_values[widget.field_name]
                    widget.update()

    if cleanup:
        doc = _rasterize(doc, cleanup)

    for el in text_elements:
        if not el["text"]:
            continue  # "erase only" replacement: redaction above was the edit
        page = doc[el["page_number"]]
        pw, ph = page.rect.width, page.rect.height
        point = fitz.Point(el["x_frac"] * pw, el["y_frac"] * ph + el["font_size"] * 0.8)
        page.insert_text(
            point,
            el["text"],
            fontsize=el["font_size"],
            color=_hex_to_rgb01(el["color"]),
            fontname="helv",
        )

    for el in image_elements:
        page = doc[el["page_number"]]
        pw, ph = page.rect.width, page.rect.height
        x0 = el["x_frac"] * pw
        y0 = el["y_frac"] * ph
        width = el["width_frac"] * pw
        height = width * el["aspect_ratio"]
        rect = fitz.Rect(x0, y0, x0 + width, y0 + height)
        page.insert_image(rect, filename=str(el["asset_path"]))

    if flatten:
        doc = _rasterize(doc)
    doc.save(output_path, garbage=4, deflate=True)
    doc.close()
