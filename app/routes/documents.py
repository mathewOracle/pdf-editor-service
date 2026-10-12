"""Documents: upload, the editor page, page previews, page-wide settings
(background, watermarks), form fields, and export.
"""
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlmodel import select

from .. import history, pdf_utils
from ..db import document_dir, exports_dir
from ..fonts import FontResolver
from ..models import Asset, Document, Element, FieldValue
from ..web import (
    DEFAULT_BG_TOLERANCE,
    DEFAULT_FAINT_MARK_STRENGTH,
    HEX_COLOR,
    SessionDep,
    document_cleanup,
    document_edits,
    get_document_or_404,
    removed_watermarks,
    templates,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: SessionDep):
    documents = session.exec(select(Document).order_by(Document.created_at.desc())).all()
    return templates.TemplateResponse(request, "index.html", {"documents": documents})


@router.post("/documents")
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


@router.get("/documents/{document_id}/edit", response_class=HTMLResponse)
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


@router.get("/documents/{document_id}/page/{page_number}.png")
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


@router.post("/documents/{document_id}/watermarks", response_class=HTMLResponse)
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
    history.record(session, doc, "Watermark settings")
    doc.removed_watermarks = json.dumps(sorted(remove)) if remove else None
    doc.faint_mark_strength = faint_strength if faint_marks else None
    session.add(doc)
    session.commit()
    return HTMLResponse("")


@router.post("/documents/{document_id}/background", response_class=HTMLResponse)
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
    if bg_color and not re.fullmatch(HEX_COLOR, bg_color):
        raise HTTPException(status_code=422, detail="bg_color must look like #rrggbb")
    history.record(session, doc, "Background color")
    doc.bg_color = bg_color or None
    doc.bg_tolerance = bg_tolerance if bg_color else None
    session.add(doc)
    session.commit()
    return HTMLResponse("")


@router.post("/documents/{document_id}/fields", response_class=HTMLResponse)
async def fill_fields(document_id: int, request: Request, session: SessionDep):
    doc = get_document_or_404(session, document_id)
    form = await request.form()
    history.record(session, doc, "Fill form fields")

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


@router.post("/documents/{document_id}/export")
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

