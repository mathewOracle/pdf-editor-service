// Minimal client-side logic for the PDF editor canvas.
// Everything else (loading overlays, saving positions, filling fields,
// exporting) goes through HTMX round-trips to the server, which stays
// the single source of truth.

let placingMode = null; // "image" | null (text placement: text_editor.js)
let pendingImageAssetId = null;
let dragState = null;

function setPlacingMode(mode, assetId) {
  placingMode = mode;
  pendingImageAssetId = assetId || null;
  document.querySelectorAll(".asset-thumb").forEach((el) => el.classList.remove("selected"));
  if (mode === "image" && assetId) {
    const thumb = document.querySelector(`.asset-thumb[data-asset-id="${assetId}"]`);
    if (thumb) thumb.classList.add("selected");
  }
  const stage = document.getElementById("page-stage");
  if (stage) stage.style.cursor = placingMode ? "crosshair" : "default";
}

function onStageClick(evt) {
  if (handleTextStageClick(evt)) return; // text_editor.js
  if (!placingMode) return;
  // Ignore clicks on existing overlay elements (they handle their own drag).
  if (evt.target.closest(".overlay-el")) return;

  const stage = evt.currentTarget;
  const rect = stage.getBoundingClientRect();
  const xFrac = (evt.clientX - rect.left) / rect.width;
  const yFrac = (evt.clientY - rect.top) / rect.height;
  const pageNumber = stage.dataset.pageNumber;
  const documentId = stage.dataset.documentId;

  if (placingMode === "image" && pendingImageAssetId) {
    const widthFrac = document.getElementById("image-width-input").value || 0.25;
    const body = new URLSearchParams({
      page_number: pageNumber,
      x_frac: xFrac,
      y_frac: yFrac,
      asset_id: pendingImageAssetId,
      width_frac: widthFrac,
    });
    fetch(`/documents/${documentId}/elements/image`, { method: "POST", body }).then(() =>
      refreshOverlay(documentId, pageNumber)
    );
  }
}

function refreshOverlay(documentId, pageNumber) {
  htmx.ajax("GET", `/documents/${documentId}/elements?page=${pageNumber}`, {
    target: "#overlay-layer",
    swap: "innerHTML",
  });
  refreshHistoryButtons(); // undo.js
}

function startDrag(evt, elementId) {
  evt.stopPropagation();
  // Don't hijack mouse events while the user is typing in an inline edit.
  if (evt.target.isContentEditable) return;
  const stage = document.getElementById("page-stage");
  const el = document.getElementById(`el-${elementId}`);
  dragState = { elementId, stage, el };
  document.addEventListener("mousemove", onDragMove);
  document.addEventListener("mouseup", onDragEnd);
}

function onDragMove(evt) {
  if (!dragState) return;
  const rect = dragState.stage.getBoundingClientRect();
  let xFrac = (evt.clientX - rect.left) / rect.width;
  let yFrac = (evt.clientY - rect.top) / rect.height;
  xFrac = Math.min(Math.max(xFrac, 0), 1);
  yFrac = Math.min(Math.max(yFrac, 0), 1);
  dragState.el.style.left = `${xFrac * 100}%`;
  dragState.el.style.top = `${yFrac * 100}%`;
  dragState.lastX = xFrac;
  dragState.lastY = yFrac;
}

function onDragEnd() {
  if (!dragState) return;
  const { elementId, lastX, lastY, stage } = dragState;
  document.removeEventListener("mousemove", onDragMove);
  document.removeEventListener("mouseup", onDragEnd);
  if (lastX !== undefined) {
    const documentId = stage.dataset.documentId;
    const body = new URLSearchParams({ x_frac: lastX, y_frac: lastY });
    fetch(`/documents/${documentId}/elements/${elementId}`, { method: "PATCH", body }).then(refreshHistoryButtons);
  }
  dragState = null;
}

function removeElement(evt, documentId, elementId, pageNumber) {
  evt.stopPropagation();
  fetch(`/documents/${documentId}/elements/${elementId}`, { method: "DELETE" }).then(() =>
    refreshOverlay(documentId, pageNumber)
  );
}

// ---------------------------------------------------- background color --
// Saved on the server; the preview re-renders with the new paper color.

// Shared by the background and watermark cards: POST a setting, report
// the result in that card's status line, re-render the page.
function savePageSetting(path, body, statusId, okText) {
  const stage = document.getElementById("page-stage");
  const status = document.getElementById(statusId);
  status.textContent = "Saving...";
  fetch(`/documents/${stage.dataset.documentId}/${path}`, { method: "POST", body }).then((r) => {
    status.textContent = r.ok ? okText : "Couldn't save that. Please try again.";
    if (r.ok) showPage(currentPageNumber());
  });
}

function applyBackground(evt) {
  evt.preventDefault();
  const body = new URLSearchParams(new FormData(evt.target));
  savePageSetting("background", body, "bg-status", `Background set to ${body.get("bg_color")}.`);
}

function resetBackground() {
  savePageSetting("background", new URLSearchParams({ bg_color: "" }), "bg-status", "Background back to the original.");
}

// ------------------------------------------------------------ watermarks --

function applyWatermarks(evt) {
  evt.preventDefault();
  const body = new URLSearchParams(new FormData(evt.target));
  const count = body.getAll("remove").length + (body.has("faint_marks") ? 1 : 0);
  const text = count ? "Watermark removal applied." : "Watermarks restored.";
  savePageSetting("watermarks", body, "wm-status", text);
}
