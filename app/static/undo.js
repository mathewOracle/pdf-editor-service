// Undo / redo. The server holds the actual history (history.py); this is
// just the toolbar buttons and the keyboard shortcuts for it.
//
// Every undo/redo reloads the page. Every sidebar panel (background,
// watermarks, form fields) gets its default values straight from the
// Document in editor.html, so a reload is the simplest way to put all of
// them back in sync at once -- patching each one's inputs by hand would
// mean re-deriving the same state twice and keeping both in sync forever.
// The one thing worth preserving across that reload is which page was
// showing, which sessionStorage below takes care of.

function lastPageKey(documentId) {
  return `pdf-editor-last-page-${documentId}`;
}

function doHistoryStep(direction) {
  const documentId = document.getElementById("page-stage").dataset.documentId;
  fetch(`/documents/${documentId}/${direction}`, { method: "POST" })
    .then((r) => (r.ok ? r.json() : Promise.reject(r)))
    .then((data) => {
      if (data.page_number !== null && data.page_number !== undefined) {
        sessionStorage.setItem(lastPageKey(documentId), data.page_number);
      }
      location.reload();
    })
    .catch(() => {}); // 409 = nothing to undo/redo; the button should already be disabled
}

function refreshHistoryButtons() {
  const stage = document.getElementById("page-stage");
  if (!stage) return;
  fetch(`/documents/${stage.dataset.documentId}/history`)
    .then((r) => r.json())
    .then((state) => {
      setHistoryButton("undo-btn", state.undo_label);
      setHistoryButton("redo-btn", state.redo_label);
    });
}

function setHistoryButton(buttonId, label) {
  const button = document.getElementById(buttonId);
  if (!button) return;
  button.disabled = !label;
  button.title = label ? `${button.dataset.verb}: ${label}` : `Nothing to ${button.dataset.verb.toLowerCase()}`;
}

document.addEventListener("DOMContentLoaded", refreshHistoryButtons);
document.body.addEventListener("htmx:afterRequest", refreshHistoryButtons);

function isTypingTarget(el) {
  return el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable;
}

document.addEventListener("keydown", (e) => {
  if (!(e.metaKey || e.ctrlKey) || isTypingTarget(e.target)) return;
  const key = e.key.toLowerCase();
  if (key === "z") {
    e.preventDefault();
    doHistoryStep(e.shiftKey ? "redo" : "undo");
  } else if (key === "y") {
    e.preventDefault();
    doHistoryStep("redo");
  }
});
