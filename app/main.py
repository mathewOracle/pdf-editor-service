"""FastAPI entrypoint: routes for uploading PDFs, the editor UI, placing
elements/assets, filling AcroForm fields, and exporting a fresh PDF.
"""
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from . import pdf_utils
from .db import document_dir, exports_dir, get_session, init_db
from .image_utils import image_pixel_size, make_background_transparent
from .models import Asset, Document, Element, FieldValue

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Offline PDF Editor")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

SessionDep = Annotated[Session, Depends(get_session)]


def document_redactions(session: Session, document_id: int) -> list[pdf_utils.Redaction]:
    """Every edit to *original* content for this document. Shared by the
    preview, the text picker and export so all three always agree.
    """
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


@app.on_event("startup")
def on_startup() -> None:
    init_db()


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
    return templates.TemplateResponse(
        request,
        "editor.html",
        {
            "doc": doc,
            "page_numbers": list(range(doc.page_count)),
            "page_heights_pt": pdf_utils.get_page_heights(Path(doc.stored_path)),
            "fields": fields,
            "assets": assets,
        },
    )


@app.get("/documents/{document_id}/page/{page_number}.png")
def page_png(document_id: int, page_number: int, session: SessionDep, dpi: int = 150):
    doc = get_document_or_404(session, document_id)
    png_bytes = pdf_utils.render_page_png(
        Path(doc.stored_path),
        page_number,
        dpi=dpi,
        redactions=document_redactions(session, document_id),
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
        Path(doc.stored_path), page, redactions=document_redactions(session, document_id)
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
    return templates.TemplateResponse(
        request,
        "_elements_overlay.html",
        {
            "elements": elements,
            "assets_by_id": assets_by_id,
            "page_width_pt": page_width_pt,
        },
    )


@app.get("/documents/{document_id}/assets/{asset_id}/file")
def asset_file(document_id: int, asset_id: int, session: SessionDep):
    asset = session.get(Asset, asset_id)
    if asset is None or asset.document_id != document_id:
        raise HTTPException(404, "Asset not found")
    return FileResponse(asset.stored_path)


@app.post("/documents/{document_id}/elements/text", response_class=HTMLResponse)
def add_text_element(
    document_id: int,
    session: SessionDep,
    page_number: int = Form(...),
    x_frac: float = Form(...),
    y_frac: float = Form(...),
    text: str = Form(...),
    font_size: float = Form(14.0),
    color: str = Form("#000000"),
):
    get_document_or_404(session, document_id)
    el = Element(
        document_id=document_id,
        page_number=page_number,
        kind="text",
        x_frac=x_frac,
        y_frac=y_frac,
        text=text,
        font_size=font_size,
        color=color,
    )
    session.add(el)
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
    page_number: int = Form(...),
    x_frac: float = Form(...),
    y_frac: float = Form(...),
    text: str = Form(""),  # empty = "just erase the original", a valid edit
    font_size: float = Form(14.0),
    color: str = Form("#000000"),
    redact_x0_frac: float = Form(...),
    redact_y0_frac: float = Form(...),
    redact_x1_frac: float = Form(...),
    redact_y1_frac: float = Form(...),
    redact_mode: pdf_utils.RedactMode = Form("area"),
):
    """Like add_text_element, but also remembers the original-PDF
    rectangle to redact at export time -- this is "edit existing text",
    whereas add_text_element is "place new text on a blank area".
    """
    get_document_or_404(session, document_id)
    el = Element(
        document_id=document_id,
        page_number=page_number,
        kind="text",
        x_frac=x_frac,
        y_frac=y_frac,
        text=text,
        font_size=font_size,
        color=color,
        redact_x0_frac=redact_x0_frac,
        redact_y0_frac=redact_y0_frac,
        redact_x1_frac=redact_x1_frac,
        redact_y1_frac=redact_y1_frac,
        redact_mode=redact_mode,
    )
    session.add(el)
    session.commit()
    return HTMLResponse("")


@app.patch("/documents/{document_id}/elements/{element_id}", response_class=HTMLResponse)
def update_element(
    document_id: int,
    element_id: int,
    session: SessionDep,
    x_frac: Optional[float] = Form(None),
    y_frac: Optional[float] = Form(None),
    text: Optional[str] = Form(None),
    font_size: Optional[float] = Form(None),
    color: Optional[str] = Form(None),
):
    """Single endpoint for both moving an element (drag-and-drop sends
    x_frac/y_frac) and editing its content (double-click-to-edit sends
    text/font_size/color) -- same row, no reason to split the API.
    Only fields actually present in the request get touched.
    """
    el = session.get(Element, element_id)
    if el is None or el.document_id != document_id:
        raise HTTPException(404, "Element not found")
    if x_frac is not None:
        el.x_frac = x_frac
    if y_frac is not None:
        el.y_frac = y_frac
    if text is not None:
        el.text = text
    if font_size is not None:
        el.font_size = font_size
    if color is not None:
        el.color = color
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
        redactions=document_redactions(session, document_id),
        flatten=flatten,
    )
    return FileResponse(output_path, filename=output_name, media_type="application/pdf")
