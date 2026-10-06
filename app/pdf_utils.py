"""All PyMuPDF logic lives here: rendering previews, reading AcroForm
fields, and baking elements/field values into a final output PDF.
"""
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


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


def render_page_png(pdf_path: Path, page_number: int, dpi: int = 150) -> bytes:
    with fitz.open(pdf_path) as doc:
        page = doc[page_number]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")


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


def extract_text_spans(pdf_path: Path, page_number: int) -> list[TextSpan]:
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
    with fitz.open(pdf_path) as doc:
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
                spans.append(
                    TextSpan(
                        text=text,
                        x0_frac=x0 / pw,
                        y0_frac=y0 / ph,
                        x1_frac=x1 / pw,
                        y1_frac=y1 / ph,
                        font_size=font_size,
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
    flatten: bool = False,
) -> None:
    """Write a fresh PDF with form fields filled and elements placed.

    ``text_elements``: dicts with page_number, x_frac, y_frac, text,
    font_size, color, and an optional ``redact_box`` (x0_frac, y0_frac,
    x1_frac, y1_frac) -- when present, that rectangle of the *original*
    PDF content is genuinely redacted (removed, not just painted over)
    before this element's text is drawn in its place. That's how "editing"
    text that was already in the PDF is implemented; see extract_text_spans.
    ``image_elements``: dicts with page_number, x_frac, y_frac, width_frac,
    asset_path (already-resolved filesystem path to the image).

    If ``flatten`` is True the result is rasterized page-by-page (like a
    fresh scan) so nothing in it is interactive or editable anymore --
    useful when the destination only accepts a "flat" document.
    """
    doc = fitz.open(original_path)

    if field_values:
        for page in doc:
            for widget in page.widgets() or []:
                if widget.field_name in field_values:
                    widget.field_value = field_values[widget.field_name]
                    widget.update()

    # Redactions must happen before any new drawing: apply_redactions()
    # rewrites the page's content stream, so anything inserted beforehand
    # risks being swept up or mis-clipped by it. Group by page so we only
    # call apply_redactions() once per page that actually needs it.
    pages_needing_redaction: set[int] = set()
    for el in text_elements:
        if el.get("redact_box"):
            page = doc[el["page_number"]]
            pw, ph = page.rect.width, page.rect.height
            x0f, y0f, x1f, y1f = el["redact_box"]
            rect = fitz.Rect(x0f * pw, y0f * ph, x1f * pw, y1f * ph)
            page.add_redact_annot(rect, fill=(1, 1, 1))
            pages_needing_redaction.add(el["page_number"])
    for page_number in pages_needing_redaction:
        # IMAGE_PIXELS blanks only the covered pixels of any image under the
        # box -- essential for scanned PDFs, where the whole page IS one
        # image and removing it outright would wipe everything.
        doc[page_number].apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

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
        # Raw PNG streams at 300dpi bloat a multi-page PDF to tens of MB.
        # JPEG at quality 88 keeps text/ink crisp at a fraction of the size
        # (learned the hard way whitening the guest house form earlier).
        flattened = fitz.open()
        for page in doc:
            pix = page.get_pixmap(dpi=300)
            jpeg_bytes = pix.pil_tobytes(format="JPEG", quality=88)
            new_page = flattened.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=jpeg_bytes)
        doc.close()
        flattened.save(output_path, garbage=4, deflate=True)
        flattened.close()
    else:
        doc.save(output_path, garbage=4, deflate=True)
        doc.close()
