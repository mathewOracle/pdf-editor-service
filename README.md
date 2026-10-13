# Offline PDF Editor Service

A small, self-contained web app for editing PDFs entirely on your own
machine: fill existing form fields, edit or add text in the document's own
fonts, redact/replace content (including on scanned pages, via OCR), clean
up backgrounds and watermarks, drop in images, and undo/redo any of it --
then export a fresh PDF. No cloud service, no telemetry, no API keys.
Works on a plane.

## Quick start

```bash
./run.sh      # starts it in the background, opens it in your browser
./status.sh   # is it running, and at what URL?
./stop.sh     # stop it
```

That's it for day-to-day use. First time on this machine, see
[First-time setup](#first-time-setup) below.

### Accessing it

`run.sh` prints the URL it started at and opens it automatically on
macOS, e.g.:

```
Started (PID 20552) at http://127.0.0.1:8421/
```

**The port is not fixed.** `run.sh` starts at `8420` and walks up
(`8421`, `8422`, ...) until it finds one nothing else is using. See
[Why isn't the port fixed?](#why-isnt-the-port-fixed) if that seems
surprising -- it's the fix for the exact problem that likely brought you
to this README.

If you ever lose track of the tab, `./status.sh` always tells you the
real current URL. Don't guess a port and assume it's "down" if that guess
doesn't answer -- check `status.sh` first.

## First-time setup

```bash
uv venv
uv pip install -r requirements.txt \
```

On macOS, also install the OCR extras (lets you edit text on *scanned*
pages -- see [What it does](#what-it-does)):

```bash
uv pip install -r requirements-macos.txt \
```

This step is optional and safe to skip: without it, scanned pages fall
back to drag-a-box erase/replace instead of click-to-edit. Not available
at all on Linux/Windows (it's Apple's Vision framework).

Then `./run.sh` as above.

## What it does

- **Upload any PDF** -- scanned forms, fillable AcroForms, plain documents.
- **Detects existing form fields** automatically and lets you fill them
  through a normal form; values are baked into the PDF on export.
- **Edit text that's already in the PDF.** Click a line, change the
  words; the replacement is drawn in the *same font, size, color, bold
  and italic* as the original (an embedded font if it has the letters you
  need, else the same font installed on your computer, else the closest
  standard font), on the same baseline. Works via true redaction (the old
  glyphs are removed, not just covered).
- **Edit text on *scanned* pages, too**, via on-device OCR (Apple's
  Vision framework, offline, macOS only). Scanned lines are detected,
  shown as clickable boxes, and editable the same way -- just painted
  over (there's no text layer to redact on a scan) instead of having
  glyphs removed. Falls back cleanly to drag-a-box when OCR isn't
  available or doesn't find something.
- **Click-to-place new text**, picking up the font/size/color of nearby
  text automatically, or pick your own. Double-click anything you've
  added to re-edit it.
- **Click-to-place images**, e.g. a signature. Upload once, reuse across
  pages. White backgrounds are stripped to transparent automatically.
- **Drag to reposition**, click the little `x` to remove, anything
  you've placed, before you export.
- **Recolor the page background** (e.g. cream/yellowed scans back to
  white), with a tolerance slider that preserves ink and signatures.
- **Remove watermarks**: diagonal/pale text watermarks detected and
  listed with one-click removal, plus a "clean up faint marks in scans"
  option for watermarks baked into scanned pixels.
- **Undo / redo** everything above. Buttons above the page (with a
  tooltip naming what they'd undo/redo), plus Cmd/Ctrl+Z and Shift+Z (or
  Ctrl+Y) for redo.
- **Export** either keeps the PDF's original text/vector content intact
  (fast, small, still searchable) or, with "Flatten", rasterizes every
  page so the result is a fully non-editable, flat document. Recoloring
  the background or cleaning up scan watermarks also rasterizes those
  pages specifically, since painting over real pixels isn't something a
  vector PDF can represent any other way.

## Architecture

```
app/
  main.py          Builds the FastAPI app, wires up the route modules
  web.py            Shared plumbing: DB session dep, template loader,
                     Document -> pdf_utils.Edits/PixelCleanup helpers
  routes/
    documents.py     Upload, the editor page, page previews, page-wide
                      settings (background/watermarks), form fields, export
    elements.py       Placed/edited text & images, the font-matching
                       endpoints, uploaded assets
    undo.py            Undo/redo endpoints
  models.py          SQLModel tables: Document, Asset, Element,
                      FieldValue, HistoryStep
  db.py              SQLite engine + storage path helpers
  history.py         Undo/redo: snapshot-based, see the module docstring
  pdf_utils.py       Core PyMuPDF logic: render previews, redact/replace
                      content, read AcroForm fields, bake a fresh PDF
  fonts.py           Picks the real font for new/replacement text:
                      embedded -> installed -> closest standard font
  ocr.py             Reads scanned pages via Apple's Vision framework
  watermark.py       Detects/removes text-layer watermarks
  image_utils.py     Background-color detection/recolor, transparent
                      signatures, faint-mark cleanup (scan watermarks)
  templates/         Jinja2 + HTMX templates (no JS framework)
  static/            Vendored htmx.min.js (offline!), hand-rolled CSS,
                      app.js (placing/dragging), text_editor.js (the text
                      edit panel), undo.js (undo/redo UI + shortcuts)
tests/
  e2e_*.py           Playwright end-to-end checks against a live server,
                      e.g.: python tests/e2e_undo_redo.py http://127.0.0.1:8421
run.sh / stop.sh / status.sh   Start/stop/check, see Quick start above
```

Positions of placed elements are stored as **fractions of page width/height
(0..1)**, not pixels -- so they stay correct no matter what DPI a preview
was rendered at, and the math to convert back to PDF points at export time
is trivial.

## Why isn't the port fixed?

Earlier versions of this app (and this README) hardcoded port `8420`.
That broke the first time something *else* on this machine happened to
be listening on `8420` first -- the editor looked "down" when it was
actually just unreachable at the URL being guessed. (This really
happened: an unrelated "Ollama Usage Monitor" from another project ended
up bound to `8420`, and `curl http://127.0.0.1:8420/` returned a
confusingly plausible-looking 200 from the *wrong app*.)

`run.sh` now scans for the first free port starting at `8420` and
`status.sh` always reports the truth, so "which port is it on" is never
something you have to remember or guess.

## Troubleshooting

- **"It's down" / a URL you remember doesn't load.** Run `./status.sh`.
  If it says "Not running", run `./run.sh`. If it says it's running at a
  URL you weren't using, that's almost certainly the actual problem --
  you had the wrong port.
- **A URL loads but shows something that isn't the PDF editor.** You've
  found a different app squatting on that port (see above). Check
  `./status.sh` for the real URL.
- **`run.sh` says no `.venv` found.** Run the [First-time setup](#first-time-setup)
  commands once.
- **Scanned pages aren't clickable for editing.** Either this isn't
  macOS, or the OCR extras weren't installed -- see
  [First-time setup](#first-time-setup). Drag-a-box erase/replace still
  works either way.
- **Something looks wrong and you want the raw logs:**
  `tail -f ~/.local/share/pdf-editor-service/server.log`

## Deliberate deviations from the usual default stack

- **No Tailwind CDN.** The whole point of this app is to work completely
  offline. The Tailwind Play CDN fetches and JIT-compiles from the
  internet on every page load, which defeats that. `static/styles.css` is
  a small hand-written stylesheet covering exactly what this UI needs.
- **Vendored htmx**, not loaded from unpkg/jsdelivr at runtime, for the
  same reason -- `static/htmx.min.js` is a plain local file.
- **SQLite database (and all runtime storage) lives outside the project
  directory**, under `~/.local/share/pdf-editor-service/`. If your
  project folder happens to be inside a cloud-sync folder (OneDrive,
  iCloud Drive, Dropbox...), those use placeholder files that can make
  SQLite see a read-only filesystem and fail. This is also where
  `run.sh`/`stop.sh`/`status.sh` keep the PID, port, and log files, and
  where `ocr.py` and `fonts.py` cache their (otherwise slow) results.

## Notes

- Form field filling calls PyMuPDF's `widget.update()`, which regenerates
  the field's appearance stream, so the typed value is visible even if a
  viewer doesn't re-run its own form rendering.
- "Flatten" (and background recolor / scan watermark cleanup) rasterizes
  at 300 DPI and re-encodes as JPEG (quality 88) rather than raw PNG --
  raw PNG at that resolution bloats a 2-page PDF to tens of megabytes for
  no visible quality gain.
- Undo/redo stores a full snapshot of the document's editable state per
  step (elements, form field values, background/watermark settings) --
  see `history.py`'s docstring for why that's simpler and safer than a
  diff-based approach for a tool this size.
