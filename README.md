# Offline PDF Editor Service

A small, self-contained web app for editing PDFs entirely on your own
machine: fill existing form fields, type free-form text anywhere on a
page, and drop in images (signatures, stamps, logos) -- then export a
fresh PDF. No cloud service, no telemetry, no API keys. Works on a plane.

## What it does

- **Upload any PDF** -- scanned forms, fillable AcroForms, plain documents.
- **Detects existing form fields** automatically and lets you fill them
  through a normal form; values are baked into the PDF on export.
- **Click-to-place free text** anywhere on a page, with font size and
  color control. Useful for PDFs that *aren't* fillable forms.
- **Click-to-place images**, e.g. a signature. Upload once, reuse across
  pages. White backgrounds are stripped to transparent automatically so a
  photographed/scanned signature doesn't show an ugly white box.
- **Drag to reposition**, click the little `x` to remove, anything you've
  placed, before you export.
- **Export** either keeps the PDF's original text/vector content intact
  (fast, small, still searchable) or, with "Flatten", rasterizes every
  page so the result is a fully non-editable, flat document.

## Running it

```bash
uv venv
uv pip install -r requirements.txt \

source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8420
```

Then open http://127.0.0.1:8420/ in a browser.

## Architecture

```
app/
  main.py          FastAPI routes
  models.py        SQLModel tables: Document, Asset, Element, FieldValue
  db.py            SQLite engine + storage path helpers
  pdf_utils.py     All PyMuPDF logic: render previews, read AcroForm
                   fields, bake elements + field values into a fresh PDF
  image_utils.py   Signature background-transparency helper
  templates/       Jinja2 + HTMX templates (no JS framework)
  static/          Vendored htmx.min.js (offline!), hand-rolled CSS,
                   app.js (click-to-place + drag coordinate capture)
```

Positions of placed elements are stored as **fractions of page width/height
(0..1)**, not pixels -- so they stay correct no matter what DPI a preview
was rendered at, and the math to convert back to PDF points at export time
is trivial.

## Deliberate deviations from the usual default stack

- **No Tailwind CDN.** The whole point of this app is to work completely
  offline. The Tailwind Play CDN fetches and JIT-compiles from the
  internet on every page load, which defeats that. `static/styles.css` is
  a small hand-written stylesheet covering exactly what this UI needs.
- **Vendored htmx**, not loaded from unpkg/jsdelivr at runtime, for the
  same reason -- `static/htmx.min.js` is a plain local file.
- **SQLite database lives outside the project directory**, under
  `~/.local/share/pdf-editor-service/`. If your project folder happens to
  be inside a cloud-sync folder (OneDrive, iCloud Drive, Dropbox...),
  those use placeholder files that can make SQLite see a read-only
  filesystem and fail. Runtime data deliberately avoids that.

## Notes

- Form field filling calls PyMuPDF's `widget.update()`, which regenerates
  the field's appearance stream, so the typed value is visible even if a
  viewer doesn't re-run its own form rendering.
- "Flatten" rasterizes at 300 DPI and re-encodes as JPEG (quality 88)
  rather than raw PNG -- raw PNG at that resolution bloats a 2-page PDF to
  tens of megabytes for no visible quality gain.
