"""FastAPI entrypoint: routes for uploading PDFs, the editor UI, placing
elements/assets, filling AcroForm fields, and exporting a fresh PDF.
"""
import json
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from . import pdf_utils
from .db import document_dir, exports_dir, get_session, init_db
from .fonts import FontResolver, warm_font_index
from .image_utils import image_pixel_size, make_background_transparent
from .models import Asset, Document, Element, FieldValue

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Offline PDF Editor")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

SessionDep = Annotated[Session, Depends(get_session)]

HEX_COLOR = r"^#[0-9a-fA-F]{6}$"
# Font names end up inside a CSS style attribute, so no quotes, semicolons
# or brackets: letters, digits, spaces and a little punctuation only.
FONT_FAMILY = r"^[\w .&+-]*$"


@dataclass(frozen=True)
class TextStyle:
    font_size: float
    color: str
    font_family: Optional[str]
    bold: bool
    italic: bool


def text_style_form(
    font_size: float = Form(14.0, gt=0, le=400),
    color: str = Form("#000000", pattern=HEX_COLOR),
    font_family: Optional[str] = Form(None, max_length=120, pattern=FONT_FAMILY),
    bold: bool = Form(False),
    italic: bool = Form(False),
) -> TextStyle:
    """The style fields shared by every route that creates text."""
    return TextStyle(font_size, color, font_family or None, bold, italic)


TextStyleDep = Annotated[TextStyle, Depends(text_style_form)]


class ElementPatch(BaseModel):
    x_frac: Optional[float] = None
    y_frac: Optional[float] = None
    text: Optional[str] = None
    font_size: Optional[float] = Field(None, gt=0, le=400)
    color: Optional[str] = Field(None, pattern=HEX_COLOR)
    font_family: Optional[str] = Field(None, max_length=120, pattern=FONT_FAMILY)
    bold: Optional[bool] = None
    italic: Optional[bool] = None


def top_from_baseline(doc: Document, page_number: int, baseline_frac: float,
                      style: TextStyle, text: str) -> float:
    """y_frac (top of the 1em box) that puts the letters on ``baseline_frac``.
    That's how replacement text lands exactly where the old line sat.
    """
    with FontResolver(Path(doc.stored_path)) as fonts:
        font = fonts.resolve(style.font_family, style.bold, style.italic, text)
    _, page_height = pdf_utils.get_page_size(Path(doc.stored_path), page_number)
    return baseline_frac - font.baseline_ratio * style.font_size / page_height


def document_edits(session: Session, doc: Document) -> pdf_utils.Edits:
    """Every edit to *original* content for this document. Shared by the
    preview, the text picker and export so all three always agree.
    """
    return pdf_utils.Edits(
        redactions=tuple(_document_redactions(session, doc.id)),
        watermarks=frozenset(removed_watermarks(doc)),
    )


def removed_watermarks(doc: Document) -> list[str]:
    return json.loads(doc.removed_watermarks) if doc.removed_watermarks else []


def _document_redactions(session: Session, document_id: int) -> list[pdf_utils.Redaction]:
    elements = session.exec(
        select(Element)
        .where(Element.document_id == document_id)
        .where(Element.redact_x0_frac.is_not(None))
    ).all()
    return [
        pdf_utils.Redaction(
            page_number=el.page_number,
            x0_frac=el.redact_x0_frac,
            y0_frac=el.redact_y0_frac,
            x1_frac=el.redact_x1_frac,
            y1_frac=el.redact_y1_frac,
            mode=el.redact_mode or "area",
        )
        for el in elements
    ]


DEFAULT_BG_TOLERANCE = 40


DEFAULT_FAINT_MARK_STRENGTH = 50


def document_cleanup(doc: Document) -> pdf_utils.PixelCleanup:
    background = (
        pdf_utils.Background.from_hex(doc.bg_color, doc.bg_tolerance or DEFAULT_BG_TOLERANCE)
        if doc.bg_color
        else None
    )
    return pdf_utils.PixelCleanup(background=background, faint_mark_strength=doc.faint_mark_strength)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    warm_font_index()


def get_document_or_404(session: Session, document_id: int) -> Document:
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


# ---------------------------------------------------------------- pages --


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: SessionDep):
    documents = session.exec(select(Document).order_by(Document.created_at.desc())).all()
    return templates.TemplateResponse(request, "index.html", {"documents": documents})


@app.post("/documents")
async def upload_document(session: SessionDep, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file")

    doc = Document(filename=file.filename, stored_path="", page_count=0)
    session.add(doc)
    session.commit()
    session.refresh(doc)

    stored_path = document_dir(doc.id) / "original.pdf"
    with stored_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    doc.stored_path = str(stored_path)
    doc.page_count = pdf_utils.get_page_count(stored_path)
    session.add(doc)
    session.commit()

    return RedirectResponse(f"/documents/{doc.id}/edit", status_code=303)


@app.get("/documents/{document_id}/edit", response_class=HTMLResponse)
def edit_document(request: Request, document_id: int, session: SessionDep):
    doc = get_document_or_404(session, document_id)
    fields = pdf_utils.get_form_fields(Path(doc.stored_path))
    assets = session.exec(select(Asset).where(Asset.document_id == document_id)).all()
    with FontResolver(Path(doc.stored_path)) as fonts:
        font_families = fonts.dropdown_families()
    return templates.TemplateResponse(
        request,
        "editor.html",
        {
            "doc": doc,
            "page_numbers": list(range(doc.page_count)),
            "page_heights_pt": pdf_utils.get_page_heights(Path(doc.stored_path)),
            "fields": fields,
            "assets": assets,
            "detected_bg": pdf_utils.detect_page_background(Path(doc.stored_path), 0),
            "default_bg_tolerance": DEFAULT_BG_TOLERANCE,
            "watermarks": pdf_utils.detect_watermarks(Path(doc.stored_path)),
            "removed_watermarks": set(removed_watermarks(doc)),
            "default_faint_strength": DEFAULT_FAINT_MARK_STRENGTH,
            "font_families": font_families,
        },
    )


@app.post("/documents/{document_id}/watermarks", response_class=HTMLResponse)
def set_watermarks(
    document_id: int,
    session: SessionDep,
    remove: Annotated[list[str], Form()] = [],  # keys from detect_watermarks
    faint_marks: bool = Form(False),
    faint_strength: int = Form(DEFAULT_FAINT_MARK_STRENGTH, ge=0, le=100),
):
    """Save which watermarks to remove. Unticking everything = undo."""
    doc = get_document_or_404(session, document_id)
    known = {w.key for w in pdf_utils.detect_watermarks(Path(doc.stored_path))}
    unknown = set(remove) - known
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown watermark(s): {sorted(unknown)}")
    doc.removed_watermarks = json.dumps(sorted(remove)) if remove else None
    doc.faint_mark_strength = faint_strength if faint_marks else None
    session.add(doc)
    session.commit()
    return HTMLResponse("")


@app.post("/documents/{document_id}/background", response_class=HTMLResponse)
def set_background(
    document_id: int,
    session: SessionDep,
    bg_color: str = Form(""),  # empty = reset to the original paper
    bg_tolerance: int = Form(DEFAULT_BG_TOLERANCE, ge=0, le=255),
):
    """Save (or clear) the paper recolor. Applies to every page, both in
    the preview and on export.
    """
    doc = get_document_or_404(session, document_id)
    if bg_color and not re.fullmatch(r"#[0-9a-fA-F]{6}", bg_color):
        raise HTTPException(status_code=422, detail="bg_color must look like #rrggbb")
    doc.bg_color = bg_color or None
    doc.bg_tolerance = bg_tolerance if bg_color else None
    session.add(doc)
    session.commit()
    return HTMLResponse("")


@app.get("/documents/{document_id}/page/{page_number}.png")
def page_png(document_id: int, page_number: int, session: SessionDep, dpi: int = 150):
    doc = get_document_or_404(session, document_id)
    png_bytes = pdf_utils.render_page_png(
        Path(doc.stored_path),
        page_number,
        dpi=dpi,
        edits=document_edits(session, doc),
        cleanup=document_cleanup(doc),
    )
    return Response(content=png_bytes, media_type="image/png")


@app.get("/documents/{document_id}/text-spans")
def text_spans(document_id: int, page: int, session: SessionDep):
    """Line-level text already printed in the original PDF, for the
    "click existing text to edit it" feature. Plain JSON (not an HTMX
    partial) since the client needs the raw numbers to position its own
    clickable highlight boxes, not pre-rendered HTML.
    """
    doc = get_document_or_404(session, document_id)
    spans = pdf_utils.extract_text_spans(
        Path(doc.stored_path), page, edits=document_edits(session, doc)
    )
    return [asdict(s) for s in spans]


# ------------------------------------------------------------- elements --


@app.get("/documents/{document_id}/elements", response_class=HTMLResponse)
def list_elements(request: Request, document_id: int, page: int, session: SessionDep):
    doc = get_document_or_404(session, document_id)
    elements = session.exec(
        select(Element)
        .where(Element.document_id == document_id)
        .where(Element.page_number == page)
    ).all()
    assets_by_id = {a.id: a for a in session.exec(select(Asset)).all()}
    page_width_pt, _ = pdf_utils.get_page_size(Path(doc.stored_path), page)
    with FontResolver(Path(doc.stored_path)) as fonts:
        fonts_by_id = {
            el.id: fonts.resolve(el.font_family, el.bold, el.italic, el.text or "")
            for el in elements
            if el.kind == "text"
        }
    return templates.TemplateResponse(
        request,
        "_elements_overlay.html",
        {
            "elements": elements,
            "assets_by_id": assets_by_id,
            "fonts_by_id": fonts_by_id,
            "web_fonts": {f.font_id for f in fonts_by_id.values() if f.web_loadable},
            "page_width_pt": page_width_pt,
        },
    )


@app.get("/documents/{document_id}/assets/{asset_id}/file")
def asset_file(document_id: int, asset_id: int, session: SessionDep):
    asset = session.get(Asset, asset_id)
    if asset is None or asset.document_id != document_id:
        raise HTTPException(404, "Asset not found")
    return FileResponse(asset.stored_path)


@app.get("/documents/{document_id}/fonts/{font_id}")
def font_file(document_id: int, font_id: str, session: SessionDep):
    """A font file for the preview's @font-face: one embedded in this PDF or
    an installed one the resolver picked. Only ids the resolver hands out
    work, so this can't be used to read arbitrary files.
    """
    doc = get_document_or_404(session, document_id)
    with FontResolver(Path(doc.stored_path)) as fonts:
        found = fonts.font_file(font_id)
    if found is None:
        raise HTTPException(404, "Font not found")
    data, ext = found
    return Response(
        content=data,
        media_type=f"font/{ext}",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/documents/{document_id}/fonts-resolve")
def resolve_font(
    document_id: int,
    session: SessionDep,
    family: str = Query("", max_length=120, pattern=FONT_FAMILY),
    bold: bool = False,
    italic: bool = False,
    text: str = Query("", max_length=2000),
):
    """Which font new text would really be drawn in, so the panel can say
    "exact match" or "Calibri isn't installed, using Helvetica" up front.
    """
    doc = get_document_or_404(session, document_id)
    with FontResolver(Path(doc.stored_path)) as fonts:
        font = fonts.resolve(family or None, bold, italic, text)
    return {"label": font.label, "quality": font.quality, "note": font.note}


@app.post("/documents/{document_id}/elements/text", response_class=HTMLResponse)
def add_text_element(
    document_id: int,
    session: SessionDep,
    style: TextStyleDep,
    page_number: int = Form(...),
    x_frac: float = Form(...),
    y_frac: float = Form(...),
    text: str = Form(..., min_length=1),
):
    """Place new text. ``y_frac`` is the top of the text."""
    get_document_or_404(session, document_id)
    session.add(Element(
        document_id=document_id, page_number=page_number, kind="text",
        x_frac=x_frac, y_frac=y_frac, text=text, **asdict(style),
    ))
    session.commit()
    return HTMLResponse("")


@app.post("/documents/{document_id}/elements/image", response_class=HTMLResponse)
def add_image_element(
    document_id: int,
    session: SessionDep,
    page_number: int = Form(...),
    x_frac: float = Form(...),
    y_frac: float = Form(...),
    asset_id: int = Form(...),
    width_frac: float = Form(0.25),
):
    get_document_or_404(session, document_id)
    el = Element(
        document_id=document_id,
        page_number=page_number,
        kind="image",
        x_frac=x_frac,
        y_frac=y_frac,
        asset_id=asset_id,
        width_frac=width_frac,
    )
    session.add(el)
    session.commit()
    return HTMLResponse("")


@app.post("/documents/{document_id}/elements/replace-text", response_class=HTMLResponse)
def replace_text_element(
    document_id: int,
    session: SessionDep,
    style: TextStyleDep,
    page_number: int = Form(...),
    x_frac: float = Form(...),
    y_frac: float = Form(...),
    text: str = Form(""),  # empty = "just erase the original", a valid edit
    redact_x0_frac: float = Form(...),
    redact_y0_frac: float = Form(...),
    redact_x1_frac: float = Form(...),
    redact_y1_frac: float = Form(...),
    redact_mode: pdf_utils.RedactMode = Form("area"),
    baseline_frac: Optional[float] = Form(None),
):
    """Edit existing content: remember the original rectangle to redact and
    draw ``text`` in its place. With ``baseline_frac`` (sent when a real
    text line was picked) the new letters sit on the old line's baseline,
    whatever font was chosen.
    """
    doc = get_document_or_404(session, document_id)
    if baseline_frac is not None:
        y_frac = top_from_baseline(doc, page_number, baseline_frac, style, text)
    session.add(Element(
        document_id=document_id, page_number=page_number, kind="text",
        x_frac=x_frac, y_frac=y_frac, text=text, **asdict(style),
        redact_x0_frac=redact_x0_frac, redact_y0_frac=redact_y0_frac,
        redact_x1_frac=redact_x1_frac, redact_y1_frac=redact_y1_frac,
        redact_mode=redact_mode,
    ))
    session.commit()
    return HTMLResponse("")


@app.patch("/documents/{document_id}/elements/{element_id}", response_class=HTMLResponse)
async def update_element(document_id: int, element_id: int, request: Request, session: SessionDep):
    """Move an element (drag sends x_frac/y_frac) or restyle/reword text
    (the edit panel sends text and style fields). Only fields actually sent
    are changed. Checkboxes are explicit "true"/"false" here, because an
    unticked box would otherwise look like "not sent".
    """
    el = session.get(Element, element_id)
    if el is None or el.document_id != document_id:
        raise HTTPException(404, "Element not found")
    try:
        changes = ElementPatch.model_validate(dict(await request.form()))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    for name, value in changes.model_dump(exclude_unset=True).items():
        if name == "font_family":
            value = value or None  # "" from the dropdown = default font
        setattr(el, name, value)
    session.add(el)
    session.commit()
    return HTMLResponse("")


@app.delete("/documents/{document_id}/elements/{element_id}", response_class=HTMLResponse)
def delete_element(document_id: int, element_id: int, session: SessionDep):
    el = session.get(Element, element_id)
    if el is None or el.document_id != document_id:
        raise HTTPException(404, "Element not found")
    session.delete(el)
    session.commit()
    return HTMLResponse("")


# ---------------------------------------------------------------- assets --


@app.post("/documents/{document_id}/assets", response_class=HTMLResponse)
async def upload_asset(
    document_id: int,
    session: SessionDep,
    file: UploadFile = File(...),
    transparent_bg: bool = Form(True),
):
    get_document_or_404(session, document_id)
    assets_dir = document_dir(document_id) / "assets"
    raw_path = assets_dir / f"{uuid.uuid4().hex}_{file.filename}"
    with raw_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    final_path = raw_path
    if transparent_bg:
        # Always a distinct filename from raw_path, even when the upload
        # was already a .png (with_suffix(".png") would otherwise collide
        # with raw_path and we'd delete the file we just wrote).
        final_path = assets_dir / f"{raw_path.stem}_transparent.png"
        make_background_transparent(raw_path, final_path)
        raw_path.unlink(missing_ok=True)

    width_px, height_px = image_pixel_size(final_path)
    asset = Asset(
        document_id=document_id,
        stored_path=str(final_path),
        width_px=width_px,
        height_px=height_px,
    )
    session.add(asset)
    session.commit()

    return RedirectResponse(f"/documents/{document_id}/edit", status_code=303)


# ---------------------------------------------------------------- fields --


@app.post("/documents/{document_id}/fields", response_class=HTMLResponse)
async def fill_fields(document_id: int, request: Request, session: SessionDep):
    get_document_or_404(session, document_id)
    form = await request.form()

    for key, value in form.items():
        if not key.startswith("field__"):
            continue
        field_name = key[len("field__") :]
        existing = session.exec(
            select(FieldValue)
            .where(FieldValue.document_id == document_id)
            .where(FieldValue.field_name == field_name)
        ).first()
        if existing:
            existing.value = value
        else:
            existing = FieldValue(document_id=document_id, field_name=field_name, value=value)
        session.add(existing)
    session.commit()
    return HTMLResponse('<p class="hint">Saved — values will apply on export.</p>')


# ---------------------------------------------------------------- export --


@app.post("/documents/{document_id}/export")
def export_document(document_id: int, session: SessionDep, flatten: bool = Form(False)):
    doc = get_document_or_404(session, document_id)
    elements = session.exec(select(Element).where(Element.document_id == document_id)).all()
    assets_by_id = {a.id: a for a in session.exec(select(Asset)).all()}
    field_values = {
        fv.field_name: fv.value
        for fv in session.exec(select(FieldValue).where(FieldValue.document_id == document_id)).all()
    }

    text_elements = [
        {
            "page_number": el.page_number,
            "x_frac": el.x_frac,
            "y_frac": el.y_frac,
            "text": el.text,
            "font_size": el.font_size,
            "color": el.color,
            "font_family": el.font_family,
            "bold": el.bold,
            "italic": el.italic,
        }
        for el in elements
        if el.kind == "text"
    ]
    image_elements = [
        {
            "page_number": el.page_number,
            "x_frac": el.x_frac,
            "y_frac": el.y_frac,
            "width_frac": el.width_frac or 0.25,
            "aspect_ratio": assets_by_id[el.asset_id].height_px / assets_by_id[el.asset_id].width_px,
            "asset_path": assets_by_id[el.asset_id].stored_path,
        }
        for el in elements
        if el.kind == "image"
    ]

    output_name = f"{Path(doc.filename).stem}_edited.pdf"
    output_path = exports_dir() / f"{document_id}_{uuid.uuid4().hex}_{output_name}"
    pdf_utils.bake_pdf(
        Path(doc.stored_path),
        output_path,
        field_values=field_values,
        text_elements=text_elements,
        image_elements=image_elements,
        edits=document_edits(session, doc),
        cleanup=document_cleanup(doc),
        flatten=flatten,
    )
    return FileResponse(output_path, filename=output_name, media_type="application/pdf")
