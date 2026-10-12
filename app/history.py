"""Undo / redo.

Every change saves a snapshot of the document's editable state from just
before it: placed/replaced text and images, form field values, and the
background/watermark settings. Undo swaps the current state for the last
snapshot (keeping the current one for redo). Uploaded images (assets) are
not part of it: undoing never deletes a signature you uploaded.

Mutating routes call ``record()`` *before* changing anything, in the same
session, so the snapshot and the change are committed together.
"""
import json
from dataclasses import dataclass
from typing import Literal, Optional

from sqlmodel import Session, delete, select

from .models import Document, Element, FieldValue, HistoryStep

MAX_UNDO_STEPS = 100
Stack = Literal["undo", "redo"]

_DOC_SETTINGS = ("bg_color", "bg_tolerance", "removed_watermarks", "faint_mark_strength")
_ELEMENT_SKIP = {"id", "document_id", "created_at"}


@dataclass(frozen=True)
class HistoryState:
    undo_label: Optional[str]
    redo_label: Optional[str]


def _snapshot(session: Session, doc: Document) -> str:
    elements = session.exec(
        select(Element).where(Element.document_id == doc.id).order_by(Element.id)
    ).all()
    fields = session.exec(select(FieldValue).where(FieldValue.document_id == doc.id)).all()
    return json.dumps({
        "elements": [el.model_dump(exclude=_ELEMENT_SKIP) for el in elements],
        "fields": {f.field_name: f.value for f in fields},
        "settings": {name: getattr(doc, name) for name in _DOC_SETTINGS},
    })


def _restore(session: Session, doc: Document, snapshot: str) -> None:
    """Replace the document's state with a snapshot. Elements get new ids,
    which is fine: the editor reloads the overlay after every undo/redo.
    """
    state = json.loads(snapshot)
    session.exec(delete(Element).where(Element.document_id == doc.id))
    session.exec(delete(FieldValue).where(FieldValue.document_id == doc.id))
    for values in state["elements"]:
        session.add(Element(document_id=doc.id, **values))
    for name, value in state["fields"].items():
        session.add(FieldValue(document_id=doc.id, field_name=name, value=value))
    for name, value in state["settings"].items():
        setattr(doc, name, value)
    session.add(doc)


def _steps(session: Session, document_id: int, stack: Stack):
    return (
        select(HistoryStep)
        .where(HistoryStep.document_id == document_id)
        .where(HistoryStep.stack == stack)
        .order_by(HistoryStep.id.desc())
    )


def record(session: Session, doc: Document, label: str, page_number: Optional[int] = None) -> None:
    """Remember the state before a change. A new change ends the redo trail,
    like in any editor.
    """
    session.add(HistoryStep(
        document_id=doc.id, stack="undo", label=label,
        page_number=page_number, snapshot=_snapshot(session, doc),
    ))
    session.exec(
        delete(HistoryStep)
        .where(HistoryStep.document_id == doc.id)
        .where(HistoryStep.stack == "redo")
    )
    old = session.exec(_steps(session, doc.id, "undo").offset(MAX_UNDO_STEPS - 1)).all()
    for step in old:
        session.delete(step)


def step(session: Session, doc: Document, direction: Stack) -> Optional[HistoryStep]:
    """Undo (``direction="undo"``) or redo the last change. Returns the step
    that was applied, or None when there's nothing to undo/redo.
    """
    other: Stack = "redo" if direction == "undo" else "undo"
    target = session.exec(_steps(session, doc.id, direction)).first()
    if target is None:
        return None
    session.add(HistoryStep(
        document_id=doc.id, stack=other, label=target.label,
        page_number=target.page_number, snapshot=_snapshot(session, doc),
    ))
    _restore(session, doc, target.snapshot)
    session.delete(target)
    return target


def state(session: Session, document_id: int) -> HistoryState:
    def label(stack: Stack) -> Optional[str]:
        top = session.exec(_steps(session, document_id, stack)).first()
        return top.label if top else None

    return HistoryState(undo_label=label("undo"), redo_label=label("redo"))

