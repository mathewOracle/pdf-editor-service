"""SQLModel tables for the PDF editor.

Kept deliberately small: a Document owns Assets (uploaded images, e.g.
signatures) and Elements (text or image placed on a specific page).
Coordinates are stored as fractions of the page size (0..1) so they stay
correct no matter what DPI a page preview was rendered at.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    stored_path: str
    page_count: int
    # Optional paper recolor ("#rrggbb" + tolerance 0-255). None = untouched.
    bg_color: Optional[str] = None
    bg_tolerance: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


class Asset(SQLModel, table=True):
    """An uploaded image (signature, stamp, logo...) reusable across pages."""

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    stored_path: str
    width_px: int
    height_px: int
    created_at: datetime = Field(default_factory=utcnow)


class Element(SQLModel, table=True):
    """Something placed on top of a page (free text / placed image), or an
    edit to original content (replaced text / removed image)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    page_number: int  # 0-indexed
    kind: str  # "text" | "image" | "erase"

    # Position of the top-left corner, as a fraction of page width/height.
    x_frac: float
    y_frac: float

    # Text-only fields
    text: Optional[str] = None
    font_size: float = 14.0
    color: str = "#000000"

    # Image-only fields
    asset_id: Optional[int] = Field(default=None, foreign_key="asset.id")
    width_frac: Optional[float] = None  # width as a fraction of page width

    # Set only when this element edits/removes content that already existed
    # in the original PDF. That rectangle is genuinely redacted (removed,
    # not painted over) before anything is drawn. Fractions of page size.
    #   kind="text"  + redact box -> replace existing text (empty text = erase)
    #   kind="erase" + redact box -> remove an existing image, nothing drawn
    # redact_mode is one of pdf_utils.RedactMode: "text" | "area" | "image".
    redact_x0_frac: Optional[float] = None
    redact_y0_frac: Optional[float] = None
    redact_x1_frac: Optional[float] = None
    redact_y1_frac: Optional[float] = None
    redact_mode: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)


class FieldValue(SQLModel, table=True):
    """A value typed into a detected AcroForm field, pending export."""

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id")
    field_name: str
    value: str
