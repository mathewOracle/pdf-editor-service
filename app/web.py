"""Shared web plumbing: templates, the DB session dependency, and helpers
that turn a Document's saved settings into what pdf_utils needs.
"""
import json
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from . import pdf_utils
from .db import get_session
from .models import Document, Element

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

SessionDep = Annotated[Session, Depends(get_session)]

HEX_COLOR = r"^#[0-9a-fA-F]{6}$"
# Font names end up inside a CSS style attribute, so no quotes, semicolons
# or brackets: letters, digits, spaces and a little punctuation only.
FONT_FAMILY = r"^[\w .&+-]*$"

DEFAULT_BG_TOLERANCE = 40
DEFAULT_FAINT_MARK_STRENGTH = 50


def get_document_or_404(session: Session, document_id: int) -> Document:
    doc = session.get(Document, document_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


def removed_watermarks(doc: Document) -> list[str]:
    return json.loads(doc.removed_watermarks) if doc.removed_watermarks else []


def document_redactions(session: Session, document_id: int) -> list[pdf_utils.Redaction]:
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


def document_edits(session: Session, doc: Document) -> pdf_utils.Edits:
    """Every edit to *original* content for this document. Shared by the
    preview, the text picker and export so all three always agree.
    """
    return pdf_utils.Edits(
        redactions=tuple(document_redactions(session, doc.id)),
        watermarks=frozenset(removed_watermarks(doc)),
    )


def document_cleanup(doc: Document) -> pdf_utils.PixelCleanup:
    background = (
        pdf_utils.Background.from_hex(doc.bg_color, doc.bg_tolerance or DEFAULT_BG_TOLERANCE)
        if doc.bg_color
        else None
    )
    return pdf_utils.PixelCleanup(background=background, faint_mark_strength=doc.faint_mark_strength)
