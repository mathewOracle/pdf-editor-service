// Minimal client-side logic for the PDF editor canvas.
// Everything else (loading overlays, saving positions, filling fields,
// exporting) goes through HTMX round-trips to the server, which stays
// the single source of truth.

let placingMode = null; // "text" | "image" | null
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
  if (!placingMode) return;
  // Ignore clicks on existing overlay elements (they handle their own drag).
  if (evt.target.closest(".overlay-el")) return;

  const stage = evt.currentTarget;
  const rect = stage.getBoundingClientRect();
  const xFrac = (evt.clientX - rect.left) / rect.width;
  const yFrac = (evt.clientY - rect.top) / rect.height;
  const pageNumber = stage.dataset.pageNumber;
  const documentId = stage.dataset.documentId;

  if (placingMode === "text") {
    const text = document.getElementById("text-input").value.trim();
    if (!text) {
      alert("Type some text first, then click on the page to place it.");
      return;
    }
    const fontSize = document.getElementById("font-size-input").value || 14;
    const color = document.getElementById("color-input").value || "#000000";

    const body = new URLSearchParams({
      page_number: pageNumber,
      x_frac: xFrac,
      y_frac: yFrac,
      text,
      font_size: fontSize,
      color,
    });
    fetch(`/documents/${documentId}/elements/text`, { method: "POST", body }).then(() =>
      refreshOverlay(documentId, pageNumber)
    );
  } else if (placingMode === "image" && pendingImageAssetId) {
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
}

function startDrag(evt, elementId) {
  evt.stopPropagation();
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
    fetch(`/documents/${documentId}/elements/${elementId}`, { method: "PATCH", body });
  }
  dragState = null;
}

function removeElement(evt, documentId, elementId, pageNumber) {
  evt.stopPropagation();
  fetch(`/documents/${documentId}/elements/${elementId}`, { method: "DELETE" }).then(() =>
    refreshOverlay(documentId, pageNumber)
  );
}
