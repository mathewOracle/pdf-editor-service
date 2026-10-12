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
  if (!placingMode || editExistingActive) return;
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

// ------------------------------------------------ inline edit (dblclick) --
// Placed text isn't "type once and it's frozen": double-click it, change
// the wording, press Enter (or click away) to save, Escape to cancel.

function startInlineEdit(evt, documentId, elementId, pageNumber) {
  evt.stopPropagation();
  const span = document.querySelector(`#el-${elementId} .el-text`);
  if (!span || span.isContentEditable) return;
  const original = span.textContent;
  span.contentEditable = "true";
  span.focus();
  document.getSelection().selectAllChildren(span);

  let cancelled = false;
  const onKey = (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      span.blur();
    } else if (e.key === "Escape") {
      cancelled = true;
      span.textContent = original;
      span.blur();
    }
  };
  const onBlur = () => {
    span.removeEventListener("keydown", onKey);
    span.contentEditable = "false";
    const updated = span.textContent.trim();
    if (cancelled || updated === original.trim()) return;
    const body = new URLSearchParams({ text: updated });
    fetch(`/documents/${documentId}/elements/${elementId}`, { method: "PATCH", body }).then(() =>
      refreshOverlay(documentId, pageNumber)
    );
  };
  span.addEventListener("keydown", onKey);
  span.addEventListener("blur", onBlur, { once: true });
}

// ------------------------------------------------- edit existing content --
// Two ways to target something already in the PDF:
//   1. click a highlighted line (PDFs with a real text layer), or
//   2. drag a box over any area (scanned pages, handwriting, logos/images).
// Either way we end up with a rectangle to redact plus optional new text.

let editExistingActive = false;
let pendingReplace = null; // {pageNumber, box: [x0,y0,x1,y1] fractions, mode}
let rubberband = null;

function toggleEditExistingMode() {
  editExistingActive = !editExistingActive;
  setPlacingMode(null);
  const stage = document.getElementById("page-stage");
  const layer = document.getElementById("text-spans-layer");
  const toggle = document.getElementById("edit-existing-toggle");
  layer.classList.toggle("active", editExistingActive);
  toggle.textContent = editExistingActive ? "Done editing" : "Pick text to edit";
  toggle.classList.toggle("secondary", !editExistingActive);
  if (editExistingActive) {
    stage.style.cursor = "crosshair";
    loadTextSpans(stage.dataset.documentId, stage.dataset.pageNumber);
    stage.addEventListener("mousedown", onAreaDragStart);
  } else {
    stage.style.cursor = "default";
    layer.innerHTML = "";
    stage.removeEventListener("mousedown", onAreaDragStart);
    cancelReplaceText();
  }
}

function loadTextSpans(documentId, pageNumber) {
  const layer = document.getElementById("text-spans-layer");
  fetch(`/documents/${documentId}/text-spans?page=${pageNumber}`)
    .then((r) => r.json())
    .then((spans) => {
      layer.innerHTML = "";
      if (spans.length === 0) {
        layer.insertAdjacentHTML(
          "beforeend",
          '<div class="hint" style="position:absolute;top:8px;left:8px;background:#fff;padding:4px 8px;border-radius:6px;">' +
            "No selectable text on this page (probably a scan). Drag a box over what you want to erase or replace.</div>"
        );
      }
      spans.forEach((s) => {
        const hit = document.createElement("div");
        hit.className = "text-span-hit";
        hit.title = s.text;
        Object.assign(hit.style, {
          left: `${s.x0_frac * 100}%`,
          top: `${s.y0_frac * 100}%`,
          width: `${(s.x1_frac - s.x0_frac) * 100}%`,
          height: `${(s.y1_frac - s.y0_frac) * 100}%`,
        });
        hit.addEventListener("mousedown", (e) => e.stopPropagation()); // not an area drag
        hit.addEventListener("click", (e) => {
          e.stopPropagation();
          layer.querySelectorAll(".selected").forEach((el) => el.classList.remove("selected"));
          hit.classList.add("selected");
          openReplacePanel(
            [s.x0_frac, s.y0_frac, s.x1_frac, s.y1_frac],
            s.text,
            s.text,
            Math.round(s.font_size * 2) / 2,
            "text" // only remove the glyphs; keep form lines/borders
          );
        });
        layer.appendChild(hit);
      });
    });
}

function stageFrac(evt) {
  const rect = document.getElementById("page-stage").getBoundingClientRect();
  return [
    Math.min(Math.max((evt.clientX - rect.left) / rect.width, 0), 1),
    Math.min(Math.max((evt.clientY - rect.top) / rect.height, 0), 1),
  ];
}

function onAreaDragStart(evt) {
  if (evt.target.closest(".overlay-el") || evt.target.closest(".text-span-hit")) return;
  evt.preventDefault();
  const [x, y] = stageFrac(evt);
  const box = document.createElement("div");
  box.className = "erase-rubberband";
  document.getElementById("text-spans-layer").appendChild(box);
  rubberband = { x0: x, y0: y, x1: x, y1: y, box };
  document.addEventListener("mousemove", onAreaDragMove);
  document.addEventListener("mouseup", onAreaDragEnd);
}

function onAreaDragMove(evt) {
  if (!rubberband) return;
  [rubberband.x1, rubberband.y1] = stageFrac(evt);
  const [x0, x1] = [rubberband.x0, rubberband.x1].sort((a, b) => a - b);
  const [y0, y1] = [rubberband.y0, rubberband.y1].sort((a, b) => a - b);
  Object.assign(rubberband.box.style, {
    left: `${x0 * 100}%`,
    top: `${y0 * 100}%`,
    width: `${(x1 - x0) * 100}%`,
    height: `${(y1 - y0) * 100}%`,
  });
}

function onAreaDragEnd() {
  document.removeEventListener("mousemove", onAreaDragMove);
  document.removeEventListener("mouseup", onAreaDragEnd);
  if (!rubberband) return;
  const [x0, x1] = [rubberband.x0, rubberband.x1].sort((a, b) => a - b);
  const [y0, y1] = [rubberband.y0, rubberband.y1].sort((a, b) => a - b);
  const tooSmall = x1 - x0 < 0.005 || y1 - y0 < 0.003; // a click, not a drag
  if (tooSmall) {
    rubberband.box.remove();
    rubberband = null;
    return;
  }
  // Default the replacement font size to roughly fit the box height.
  const stage = document.getElementById("page-stage");
  const pageHeightPt = parseFloat(stage.dataset.pageHeightPt || "842");
  const guessSize = Math.max(6, Math.min(36, Math.round((y1 - y0) * pageHeightPt * 0.75)));
  openReplacePanel([x0, y0, x1, y1], "(selected area)", "", guessSize, "area");
}

function openReplacePanel(box, originalLabel, prefill, fontSize, mode) {
  const stage = document.getElementById("page-stage");
  pendingReplace = { pageNumber: stage.dataset.pageNumber, box, mode };
  document.getElementById("edit-existing-original").textContent = originalLabel;
  document.getElementById("edit-existing-text").value = prefill;
  document.getElementById("edit-existing-size").value = fontSize;
  document.getElementById("edit-existing-panel").style.display = "block";
  const input = document.getElementById("edit-existing-text");
  input.focus();
  input.select();
}

function cancelReplaceText() {
  pendingReplace = null;
  if (rubberband) {
    rubberband.box.remove();
    rubberband = null;
  }
  document.querySelectorAll(".text-span-hit.selected").forEach((el) => el.classList.remove("selected"));
  const panel = document.getElementById("edit-existing-panel");
  if (panel) panel.style.display = "none";
}

function confirmReplaceText() {
  if (!pendingReplace) return;
  const stage = document.getElementById("page-stage");
  const documentId = stage.dataset.documentId;
  const { pageNumber, box, mode } = pendingReplace;
  const [x0, y0, x1, y1] = box;
  const body = new URLSearchParams({
    page_number: pageNumber,
    x_frac: x0,
    y_frac: y0,
    text: document.getElementById("edit-existing-text").value.trim(),
    font_size: document.getElementById("edit-existing-size").value || 12,
    color: document.getElementById("edit-existing-color").value || "#000000",
    redact_x0_frac: x0,
    redact_y0_frac: y0,
    redact_x1_frac: x1,
    redact_y1_frac: y1,
    redact_mode: mode,
  });
  fetch(`/documents/${documentId}/elements/replace-text`, { method: "POST", body }).then(() => {
    cancelReplaceText();
    refreshOverlay(documentId, pageNumber);
  });
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && document.activeElement?.id === "edit-existing-text") confirmReplaceText();
  if (e.key === "Escape" && pendingReplace) cancelReplaceText();
});

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
