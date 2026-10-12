// Text editing: change text already in the PDF, add new text, restyle
// either one. One panel serves all three; `panelTarget` says what Apply does.
//
// Matching the original formatting is the point, so whenever the panel opens
// it's prefilled from real text: the line you clicked, or the line nearest
// to where you're adding text. The server decides which font file is really
// used (fonts.py) and the note under the font picker says how exact it is.

let textMode = null; // null | "edit" | "add" | "copy"
let modeBeforeCopy = null;
let pageSpans = []; // text lines on the current page, from /text-spans
let panelTarget = null;
// panelTarget is one of:
//   {kind: "new", pageNumber, x, y}
//   {kind: "replace", pageNumber, box: [x0, y0, x1, y1], mode, baseline}
//   {kind: "element", id, pageNumber}
let rubberband = null;
let fontCheckTimer = null;

const MODE_HINTS = {
  edit: "Click a highlighted line to change it. On scans, drag a box over what you want to replace.",
  add: "Click on the page where the new text should start.",
  copy: "Click any highlighted text to copy its font, size and color.",
};

const stageEl = () => document.getElementById("page-stage");
const docId = () => stageEl().dataset.documentId;
const pageNo = () => stageEl().dataset.pageNumber;

// --------------------------------------------------------------- modes --

function setTextMode(mode) {
  textMode = mode;
  setPlacingMode(null); // app.js: stop placing images
  const layer = document.getElementById("text-spans-layer");
  const showSpans = mode === "edit" || mode === "copy";
  layer.classList.toggle("active", showSpans);
  layer.classList.toggle("copying", mode === "copy");
  stageEl().style.cursor = mode ? "crosshair" : "default";
  document.getElementById("text-mode-hint").textContent = MODE_HINTS[mode] || "";

  for (const [id, m] of [["edit-existing-toggle", "edit"], ["add-text-toggle", "add"]]) {
    const button = document.getElementById(id);
    const on = mode === m || (mode === "copy" && modeBeforeCopy === m);
    button.setAttribute("aria-pressed", String(on));
    button.classList.toggle("secondary", !on);
  }
  document.getElementById("edit-existing-toggle").textContent =
    mode === "edit" ? "Done editing" : "Edit existing text";

  stageEl().removeEventListener("mousedown", onAreaDragStart);
  if (mode === "edit") stageEl().addEventListener("mousedown", onAreaDragStart);
  if (mode) loadTextSpans();
  else document.getElementById("text-spans-layer").innerHTML = "";
}

function toggleEditExistingMode() {
  closeTextPanel();
  setTextMode(textMode === "edit" ? null : "edit");
}

function startAddText() {
  closeTextPanel();
  setTextMode(textMode === "add" ? null : "add");
}

function startCopyStyle() {
  modeBeforeCopy = textMode === "copy" ? modeBeforeCopy : textMode;
  setTextMode("copy");
}

// Called by showPage() in editor.html.
function textEditorPageChanged() {
  closeTextPanel();
  if (textMode) loadTextSpans();
}

// --------------------------------------------------- existing text lines --

function loadTextSpans() {
  const layer = document.getElementById("text-spans-layer");
  return fetch(`/documents/${docId()}/text-spans?page=${pageNo()}`)
    .then((r) => r.json())
    .then((spans) => {
      pageSpans = spans;
      layer.innerHTML = "";
      if (textMode === "edit" && spans.length === 0) {
        const note = document.createElement("div");
        note.className = "hint spans-empty";
        note.textContent =
          "No selectable text on this page (probably a scan). Drag a box over what you want to erase or replace.";
        layer.appendChild(note);
      }
      spans.forEach((span) => layer.appendChild(spanHitBox(span)));
    });
}

function spanHitBox(span) {
  const hit = document.createElement("div");
  hit.className = "text-span-hit";
  hit.title = `${span.text}\n${span.font_family} ${span.font_size}pt`;
  Object.assign(hit.style, {
    left: `${span.x0_frac * 100}%`,
    top: `${span.y0_frac * 100}%`,
    width: `${(span.x1_frac - span.x0_frac) * 100}%`,
    height: `${(span.y1_frac - span.y0_frac) * 100}%`,
  });
  hit.addEventListener("mousedown", (e) => e.stopPropagation()); // not an area drag
  hit.addEventListener("click", (e) => {
    e.stopPropagation();
    if (textMode === "copy") return copyStyleFrom(span);
    markSelected(hit);
    openTextPanel(
      { kind: "replace", pageNumber: pageNo(), mode: "text", baseline: span.baseline_frac,
        box: [span.x0_frac, span.y0_frac, span.x1_frac, span.y1_frac] },
      { ...styleOf(span), text: span.text },
      `Original: “${span.text}”`
    );
  });
  return hit;
}

function markSelected(hit) {
  document.querySelectorAll(".text-span-hit.selected").forEach((el) => el.classList.remove("selected"));
  if (hit) hit.classList.add("selected");
}

function styleOf(span) {
  return {
    font_family: span.font_family,
    font_size: Math.round(span.font_size * 2) / 2,
    color: span.color,
    bold: span.bold,
    italic: span.italic,
  };
}

// The line closest to a point, so new text matches what's around it.
function nearestSpan(x, y) {
  let best = null;
  let bestDistance = Infinity;
  for (const s of pageSpans) {
    const dx = Math.max(s.x0_frac - x, 0, x - s.x1_frac);
    const dy = Math.max(s.y0_frac - y, 0, y - s.y1_frac);
    const distance = dx * dx + dy * dy * 4; // same-row text matters most
    if (distance < bestDistance) [best, bestDistance] = [s, distance];
  }
  return best;
}

function copyStyleFrom(span) {
  fillStyle(styleOf(span));
  setTextMode(modeBeforeCopy);
  document.getElementById("text-mode-hint").textContent =
    `Copied ${span.font_family} ${span.font_size}pt from “${span.text.slice(0, 40)}”.`;
  document.getElementById("tp-text").focus();
}

// ------------------------------------------------------------ stage clicks --

// Returns true when the click was handled here, so app.js skips it.
function handleTextStageClick(evt) {
  if (textMode !== "add" || evt.target.closest(".overlay-el")) return textMode !== null;
  const [x, y] = stageFrac(evt);
  const span = nearestSpan(x, y);
  openTextPanel(
    { kind: "new", pageNumber: pageNo(), x, y },
    { ...(span ? styleOf(span) : {}), text: "" },
    span ? `Matching nearby text: “${span.text.slice(0, 40)}”` : ""
  );
  return true;
}

function stageFrac(evt) {
  const rect = stageEl().getBoundingClientRect();
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

function rubberbandBox() {
  const [x0, x1] = [rubberband.x0, rubberband.x1].sort((a, b) => a - b);
  const [y0, y1] = [rubberband.y0, rubberband.y1].sort((a, b) => a - b);
  return [x0, y0, x1, y1];
}

function onAreaDragMove(evt) {
  if (!rubberband) return;
  [rubberband.x1, rubberband.y1] = stageFrac(evt);
  const [x0, y0, x1, y1] = rubberbandBox();
  Object.assign(rubberband.box.style, {
    left: `${x0 * 100}%`, top: `${y0 * 100}%`,
    width: `${(x1 - x0) * 100}%`, height: `${(y1 - y0) * 100}%`,
  });
}

function onAreaDragEnd() {
  document.removeEventListener("mousemove", onAreaDragMove);
  document.removeEventListener("mouseup", onAreaDragEnd);
  if (!rubberband) return;
  const box = rubberbandBox();
  const [x0, y0, x1, y1] = box;
  if (x1 - x0 < 0.005 || y1 - y0 < 0.003) return clearRubberband(); // a click, not a drag
  const span = nearestSpan((x0 + x1) / 2, (y0 + y1) / 2);
  const pageHeightPt = parseFloat(stageEl().dataset.pageHeightPt || "842");
  const fitSize = Math.max(6, Math.min(36, Math.round((y1 - y0) * pageHeightPt * 0.75)));
  openTextPanel(
    { kind: "replace", pageNumber: pageNo(), mode: "area", box },
    { font_size: fitSize, ...(span ? styleOf(span) : {}), text: "" },
    "Replacing the selected area. Leave the text empty to just erase it."
  );
}

function clearRubberband() {
  if (rubberband) rubberband.box.remove();
  rubberband = null;
}

// ------------------------------------------------------------------ panel --

function openTextPanel(target, values, originalNote) {
  panelTarget = target;
  const panel = document.getElementById("text-panel");
  const original = document.getElementById("text-panel-original");
  original.textContent = originalNote || "";
  original.hidden = !originalNote;
  document.getElementById("tp-text").value = values.text || "";
  fillStyle(values);
  document.getElementById("tp-text-label").textContent =
    target.kind === "replace" ? "Replace with (leave empty to erase)" : "Text";
  document.getElementById("tp-submit").textContent = target.kind === "new" ? "Add text" : "Apply";
  panel.hidden = false;
  const input = document.getElementById("tp-text");
  input.focus();
  input.select();
}

function fillStyle(style) {
  const fontSelect = document.getElementById("tp-font");
  if (style.font_family) {
    if (![...fontSelect.options].some((o) => o.value === style.font_family)) {
      fontSelect.add(new Option(style.font_family, style.font_family), 0);
    }
    fontSelect.value = style.font_family;
  }
  if (style.font_size) document.getElementById("tp-size").value = style.font_size;
  if (style.color) document.getElementById("tp-color").value = style.color;
  if (style.bold !== undefined) document.getElementById("tp-bold").checked = style.bold;
  if (style.italic !== undefined) document.getElementById("tp-italic").checked = style.italic;
  scheduleFontCheck();
}

function openTextPanelForElement(evt, el) {
  evt.stopPropagation();
  const d = el.dataset;
  openTextPanel(
    { kind: "element", id: d.elementId, pageNumber: d.pageNumber },
    {
      text: d.text,
      font_family: d.fontFamily || "Helvetica",
      font_size: parseFloat(d.fontSize),
      color: d.color,
      bold: d.bold === "true",
      italic: d.italic === "true",
    },
    "Editing text you added."
  );
}

function closeTextPanel() {
  panelTarget = null;
  clearRubberband();
  markSelected(null);
  document.getElementById("text-panel").hidden = true;
}

function panelValues() {
  return {
    text: document.getElementById("tp-text").value.trim(),
    font_family: document.getElementById("tp-font").value,
    font_size: document.getElementById("tp-size").value || 12,
    color: document.getElementById("tp-color").value,
    // Always explicit, so the server can tell "unticked" from "not sent".
    bold: String(document.getElementById("tp-bold").checked),
    italic: String(document.getElementById("tp-italic").checked),
  };
}

function submitTextPanel(evt) {
  evt.preventDefault();
  if (!panelTarget) return;
  const values = panelValues();
  const target = panelTarget;
  if (target.kind === "new" && !values.text) {
    document.getElementById("tp-font-note").textContent = "Type some text first.";
    return;
  }
  let request;
  if (target.kind === "element") {
    request = fetch(`/documents/${docId()}/elements/${target.id}`, {
      method: "PATCH", body: new URLSearchParams(values),
    });
  } else if (target.kind === "new") {
    request = fetch(`/documents/${docId()}/elements/text`, {
      method: "POST",
      body: new URLSearchParams({ ...values, page_number: target.pageNumber, x_frac: target.x, y_frac: target.y }),
    });
  } else {
    const [x0, y0, x1, y1] = target.box;
    const body = new URLSearchParams({
      ...values, page_number: target.pageNumber, x_frac: x0, y_frac: y0,
      redact_x0_frac: x0, redact_y0_frac: y0, redact_x1_frac: x1, redact_y1_frac: y1,
      redact_mode: target.mode,
    });
    if (target.baseline !== undefined) body.set("baseline_frac", target.baseline);
    request = fetch(`/documents/${docId()}/elements/replace-text`, { method: "POST", body });
  }
  request.then((r) => {
    if (!r.ok) {
      document.getElementById("tp-font-note").textContent = "Couldn't save that. Check the size and try again.";
      return;
    }
    closeTextPanel();
    refreshOverlay(docId(), target.pageNumber);
    if (textMode === "edit") loadTextSpans(); // the replaced line stops being pickable
  });
}

// -------------------------------------------------------------- font note --

function scheduleFontCheck() {
  clearTimeout(fontCheckTimer);
  fontCheckTimer = setTimeout(checkFont, 250);
}

function checkFont() {
  const v = panelValues();
  const params = new URLSearchParams({ family: v.font_family, bold: v.bold, italic: v.italic, text: v.text });
  const note = document.getElementById("tp-font-note");
  fetch(`/documents/${docId()}/fonts-resolve?${params}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((font) => {
      if (!font) return;
      note.textContent = `${font.label}: ${font.note}`;
      note.dataset.quality = font.quality;
    });
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (panelTarget) closeTextPanel();
  else if (textMode === "copy") setTextMode(modeBeforeCopy);
  else if (textMode) setTextMode(null);
});
