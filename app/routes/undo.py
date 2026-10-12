"""Undo / redo endpoints. The state itself lives in history.py."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from .. import history
from ..history import Stack
from ..web import SessionDep, get_document_or_404

router = APIRouter()


def _state_json(session, document_id: int, applied=None) -> dict:
    state = history.state(session, document_id)
    return {
        "undo_label": state.undo_label,
        "redo_label": state.redo_label,
        "applied_label": applied.label if applied else None,
        "page_number": applied.page_number if applied else None,
    }


@router.get("/documents/{document_id}/history")
def history_state(document_id: int, session: SessionDep):
    get_document_or_404(session, document_id)
    return _state_json(session, document_id)


def _apply_step(document_id: int, session: SessionDep, direction: Stack) -> JSONResponse:
    """Shared by /undo and /redo: same logic, opposite stack. 409 when
    there's nothing to do, so a double-press can't silently do nothing
    and look like it worked.
    """
    doc = get_document_or_404(session, document_id)
    applied = history.step(session, doc, direction)
    if applied is None:
        session.rollback()
        raise HTTPException(409, f"Nothing to {direction}")
    session.commit()
    return JSONResponse(_state_json(session, document_id, applied))


@router.post("/documents/{document_id}/undo")
def undo(document_id: int, session: SessionDep):
    return _apply_step(document_id, session, "undo")


@router.post("/documents/{document_id}/redo")
def redo(document_id: int, session: SessionDep):
    return _apply_step(document_id, session, "redo")
