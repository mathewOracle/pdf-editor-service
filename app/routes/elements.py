"""Things placed on a page (text, images), edits to the original text,
uploaded images, and the font plumbing behind matched-font text.
"""
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import select

from .. import history, pdf_utils
from ..db import document_dir
from ..fonts import FontResolver
from ..image_utils import image_pixel_size, make_background_transparent
from ..models import Asset, Document, Element
from ..web import FONT_FAMILY, HEX_COLOR, SessionDep, document_edits, get_document_or_404, templates

router = APIRouter()


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


def _element_or_404(session, document_id: int, element_id: int) -> Element:
    el = session.get(Element, element_id)
    if el is None or el.document_id != document_id:
        raise HTTPException(404, "Element not found")
    return el


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


@router.get("/documents/{document_id}/text-spans")
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


@router.get("/documents/{document_id}/elements", response_class=HTMLResponse)
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


@router.get("/documents/{document_id}/assets/{asset_id}/file")
def asset_file(document_id: int, asset_id: int, session: SessionDep):
    asset = session.get(Asset, asset_id)
    if asset is None or asset.document_id != document_id:
        raise HTTPException(404, "Asset not found")
    return FileResponse(asset.stored_path)


@router.get("/documents/{document_id}/fonts/{font_id}")
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


@router.get("/documents/{document_id}/fonts-resolve")
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


@router.post("/documents/{document_id}/elements/text", response_class=HTMLResponse)
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
    doc = get_document_or_404(session, document_id)
    history.record(session, doc, "Add text", page_number)
    session.add(Element(
        document_id=document_id, page_number=page_number, kind="text",
        x_frac=x_frac, y_frac=y_frac, text=text, **asdict(style),
    ))
    session.commit()
    return HTMLResponse("")


@router.post("/documents/{document_id}/elements/image", response_class=HTMLResponse)
def add_image_element(
    document_id: int,
    session: SessionDep,
    page_number: int = Form(...),
    x_frac: float = Form(...),
    y_frac: float = Form(...),
    asset_id: int = Form(...),
    width_frac: float = Form(0.25),
):
    doc = get_document_or_404(session, document_id)
    history.record(session, doc, "Place image", page_number)
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


@router.post("/documents/{document_id}/elements/replace-text", response_class=HTMLResponse)
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
    history.record(session, doc, "Replace text" if text else "Erase", page_number)
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


@router.patch("/documents/{document_id}/elements/{element_id}", response_class=HTMLResponse)
async def update_element(document_id: int, element_id: int, request: Request, session: SessionDep):
    """Move an element (drag sends x_frac/y_frac) or restyle/reword text
    (the edit panel sends text and style fields). Only fields actually sent
    are changed. Checkboxes are explicit "true"/"false" here, because an
    unticked box would otherwise look like "not sent".
    """
    doc = get_document_or_404(session, document_id)
    el = _element_or_404(session, document_id, element_id)
    try:
        changes = ElementPatch.model_validate(dict(await request.form()))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    sent = changes.model_fields_set
    history.record(session, doc, "Move" if sent <= {"x_frac", "y_frac"} else "Edit text", el.page_number)
    for name, value in changes.model_dump(exclude_unset=True).items():
        if name == "font_family":
            value = value or None  # "" from the dropdown = default font
        setattr(el, name, value)
    session.add(el)
    session.commit()
    return HTMLResponse("")


@router.delete("/documents/{document_id}/elements/{element_id}", response_class=HTMLResponse)
def delete_element(document_id: int, element_id: int, session: SessionDep):
    doc = get_document_or_404(session, document_id)
    el = _element_or_404(session, document_id, element_id)
    history.record(session, doc, "Remove", el.page_number)
    session.delete(el)
    session.commit()
    return HTMLResponse("")


@router.post("/documents/{document_id}/assets", response_class=HTMLResponse)
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

