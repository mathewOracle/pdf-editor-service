"""End-to-end check of text editing with matched fonts, in a real browser.

Run with a server up (default http://127.0.0.1:8420, or pass a URL):
    python tests/e2e_text_style.py http://127.0.0.1:8424

Builds its own test PDF (Calibri + Times New Roman Bold, subset-embedded,
like a Word export) so it doesn't depend on files in ~/Downloads, then:
  1. clicks an existing line: the panel is prefilled with its font/size/color
  2. replaces it: the export has the new text in the same font, on the same
     baseline, and the old text is gone
  3. adds new text next to existing text: it picks up the nearby style
  4. double-clicks the added text and makes it bold
  5. "Copy style" takes the title's font
"""
import sys
from pathlib import Path

import httpx
import pymupdf as fitz
from playwright.sync_api import Page, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8420"
OFFICE_FONTS = Path("/Applications/Microsoft Word.app/Contents/Resources/DFonts")
TEST_PDF = Path("/tmp/e2e_text_style.pdf")
SHOTS = Path("/tmp/e2e_shots")
SHOTS.mkdir(exist_ok=True)


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail else ""))
    if not ok:
        sys.exit(1)


def make_pdf() -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font("Fcal", fontfile=str(OFFICE_FONTS / "Calibri.ttf"))
    page.insert_font("Ftnrb", fontfile=str(OFFICE_FONTS / "timesbd.ttf"))
    page.insert_text((72, 80), "EMPLOYMENT AGREEMENT", fontsize=20, fontname="Ftnrb", color=(0.1, 0.2, 0.5))
    page.insert_text((72, 130), "John Smith", fontsize=11, fontname="Fcal")
    page.insert_text((72, 160), "Start date: 01 March 2026", fontsize=11, fontname="Fcal", color=(0.3, 0.3, 0.3))
    doc.subset_fonts()
    doc.save(TEST_PDF, garbage=4, deflate=True)


def upload() -> int:
    with TEST_PDF.open("rb") as f:
        r = httpx.post(f"{BASE}/documents", files={"file": (TEST_PDF.name, f, "application/pdf")})
    return int(r.headers["location"].split("/")[2])


def wait_for_stage(page: Page) -> dict:
    page.wait_for_load_state("networkidle")
    page.wait_for_function("document.getElementById('page-stage').getBoundingClientRect().width > 100")
    return page.locator("#page-stage").bounding_box()


def spans(pdf_bytes: bytes) -> list[dict]:
    page = fitz.open(stream=pdf_bytes, filetype="pdf")[0]
    return [
        {**s, "text": s["text"].replace("\xa0", " ")}
        for b in page.get_text("dict")["blocks"] for line in b.get("lines", []) for s in line["spans"]
    ]


def main() -> None:
    make_pdf()
    doc_id = upload()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{BASE}/documents/{doc_id}/edit")
        stage = wait_for_stage(page)

        # ---- 1. click an existing line -----------------------------------
        page.click("#edit-existing-toggle")
        page.locator(".text-span-hit[title^='John Smith']").click()
        check("panel opens with the line's text", page.input_value("#tp-text") == "John Smith")
        check("font prefilled from the PDF", page.input_value("#tp-font") == "Calibri",
              page.input_value("#tp-font"))
        check("size prefilled", float(page.input_value("#tp-size")) == 11.0)
        check("bold off for regular text", not page.is_checked("#tp-bold"))
        page.wait_for_function("document.getElementById('tp-font-note').textContent.includes('Calibri')")
        check("font note explains the match", True, page.text_content("#tp-font-note"))

        # ---- 2. replace it ----------------------------------------------
        page.fill("#tp-text", "Mathew Kadambatt")
        page.click("#tp-submit")
        page.wait_for_selector(".overlay-el.text >> text=Mathew Kadambatt")
        family = page.eval_on_selector(".overlay-el.text", "el => getComputedStyle(el).fontFamily")
        first = family.split(",")[0].strip().strip('"')
        check("preview uses the real font file", first.startswith("pdf-"), family)
        page.wait_for_function(f"document.fonts.check('11px \"{first}\"')")
        check("browser loaded that font", True, first)
        page.click("#edit-existing-toggle")  # done editing

        # ---- 3. add text under "Start date" -------------------------------
        page.click("#add-text-toggle")
        page.locator("#page-stage").click(position={"x": stage["width"] * 0.12, "y": stage["height"] * 0.225})
        page.wait_for_selector("#text-panel:not([hidden])")
        check("new text picks up the nearby font", page.input_value("#tp-font") == "Calibri")
        check("and the nearby color", page.input_value("#tp-color") == "#4d4d4d", page.input_value("#tp-color"))
        page.fill("#tp-text", "Notice period: 30 days")
        page.click("#tp-submit")
        page.wait_for_selector(".overlay-el.text >> text=Notice period: 30 days")
        page.click("#add-text-toggle")  # leave add mode

        # ---- 4. double-click it and make it bold -------------------------
        page.locator(".overlay-el.text", has_text="Notice period").dblclick()
        page.wait_for_selector("#text-panel:not([hidden])")
        check("double-click reopens the panel with its text", page.input_value("#tp-text") == "Notice period: 30 days")
        page.check("#tp-bold")
        page.click("#tp-submit")
        page.wait_for_function(
            "[...document.querySelectorAll('.overlay-el.text')].some(e => e.textContent.includes('Notice') && e.dataset.bold === 'true')"
        )
        check("restyled to bold", True)

        # ---- 5. copy style from the title ---------------------------------
        page.locator(".overlay-el.text", has_text="Notice period").dblclick()
        page.click("#tp-copy-style")
        page.locator(".text-span-hit[title^='EMPLOYMENT']").click()
        check("copy style took the title font", page.input_value("#tp-font") == "Times New Roman")
        check("and its size", float(page.input_value("#tp-size")) == 20.0)
        page.click("text=Cancel")

        page.screenshot(path=SHOTS / "text_style_editor.png", full_page=True)
        browser.close()

    # ---- export -----------------------------------------------------------
    out = httpx.post(f"{BASE}/documents/{doc_id}/export").content
    exported = spans(out)
    original = {s["text"]: s for s in spans(TEST_PDF.read_bytes())}
    by_text = {s["text"]: s for s in exported}
    texts = " | ".join(by_text)
    check("old text removed from export", "John Smith" not in texts, texts)
    new = by_text.get("Mathew Kadambatt")
    check("replacement exported", new is not None, texts)
    check("replacement in Calibri", "Calibri" in new["font"], new["font"])
    drift = abs(new["origin"][1] - original["John Smith"]["origin"][1])
    check("replacement on the original baseline", drift < 0.2, f"{drift:.2f}pt")
    added = by_text["Notice period: 30 days"]
    check("added text exported bold Calibri", added["font"] == "Calibri Bold", added["font"])
    fitz.open(stream=out, filetype="pdf")[0].get_pixmap(dpi=110).save(SHOTS / "text_style_export.png")
    print(f"\nAll checks passed. Screenshots in {SHOTS}")


if __name__ == "__main__":
    main()
