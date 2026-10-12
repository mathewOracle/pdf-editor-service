"""End-to-end check of the editing features in a real (headless) browser.

Run with the server up on :8420:
    python tests/e2e_editing.py

Verifies, against the live UI (not just HTTP status codes -- that gap is how
the blank-page bug slipped through once already):
  1. Clicking an existing PDF text line and replacing it.
  2. Double-click-to-edit on already-placed text (not "type once" only).
  3. Placing and removing an image element.
  4. Drag-a-box erase on a scanned page with no text layer.
  5. Export genuinely removes the original text (redaction, not overlay).
"""
import sys
from pathlib import Path

import fitz
import httpx
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8420"
SIGNATURE = Path("/tmp/fake_signature.png")
SHOTS = Path("/tmp/e2e_shots")
SHOTS.mkdir(exist_ok=True)


def upload(path: Path) -> int:
    with path.open("rb") as f:
        r = httpx.post(f"{BASE}/documents", files={"file": (path.name, f, "application/pdf")})
    return int(r.headers["location"].split("/")[2])


def check(label: str, ok: bool) -> None:
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        sys.exit(1)


def make_signature() -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 150), "white")
    ImageDraw.Draw(img).line([(20, 100), (120, 30), (220, 110), (380, 40)], fill="navy", width=6)
    img.save(SIGNATURE)


def wait_for_page_image(page) -> dict:
    """The stage only gets its real size once the page PNG loads and
    fitPageImage() runs -- clicking before that hits a ~0px box."""
    page.wait_for_load_state("networkidle")
    page.wait_for_function("document.getElementById('page-stage').getBoundingClientRect().width > 100")
    return page.locator("#page-stage").bounding_box()


def main() -> None:
    make_signature()
    text_doc = upload(Path.home() / "Downloads" / "GH-Forms-CAT-II,III,IV (1).pdf")
    scan_doc = upload(Path.home() / "Downloads" / "Guest-house_form_original_backup.pdf")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.on("dialog", lambda d: d.accept())

        # ---- 1. replace an existing text line ----------------------------
        page.goto(f"{BASE}/documents/{text_doc}/edit", wait_until="networkidle")
        page.click("#edit-existing-toggle")
        page.wait_for_selector(".text-span-hit")
        target = page.locator(".text-span-hit[title='NITT GUEST HOUSE']")
        target.click()
        check("panel prefilled with original text",
              page.input_value("#edit-existing-text") == "NITT GUEST HOUSE")
        page.fill("#edit-existing-text", "NITT GUEST HOUSE - EDITED")
        page.click("text=Apply")
        page.wait_for_selector(".overlay-el.text >> text=NITT GUEST HOUSE - EDITED")
        check("white-out cover rendered in preview", page.locator(".redact-cover").count() == 1)
        page.screenshot(path=SHOTS / "1_replaced.png", full_page=True)

        # ---- 2. double-click to re-edit the text we just replaced --------
        page.click("#edit-existing-toggle")  # leave edit-existing mode
        el = page.locator(".overlay-el.text").first
        el.dblclick()
        page.keyboard.press("Meta+A")
        page.keyboard.type("GUEST HOUSE (re-edited)")
        page.keyboard.press("Enter")
        page.wait_for_selector(".overlay-el.text >> text=GUEST HOUSE (re-edited)")
        check("double-click edit saved and re-rendered", True)
        check("redaction kept after re-edit", page.locator(".redact-cover").count() == 1)

        # ---- 3. place + remove an image element --------------------------
        page.set_input_files("input[name=file]", str(SIGNATURE))
        page.click("form[action$='/assets'] button[type=submit]")
        page.wait_for_selector(".asset-thumb")
        stage = wait_for_page_image(page)
        page.locator(".asset-thumb").last.click()
        # locator.click scrolls into view; raw mouse.click uses viewport coords
        # and silently misses anything below the fold of a tall page.
        page.locator("#page-stage").click(
            position={"x": stage["width"] * 0.6, "y": stage["height"] * 0.85}
        )
        page.wait_for_selector(".overlay-el:not(.text) img")
        check("image element placed", page.locator(".overlay-el:not(.text)").count() == 1)
        img_el = page.locator(".overlay-el:not(.text)").first
        img_el.hover()
        img_el.locator(".remove-btn").click()
        page.wait_for_function("document.querySelectorAll('.overlay-el:not(.text)').length === 0")
        check("image element removed", True)

        # ---- 4. drag-a-box erase on a scanned page -----------------------
        page.goto(f"{BASE}/documents/{scan_doc}/edit", wait_until="networkidle")
        page.click("#edit-existing-toggle")
        page.wait_for_selector("text=No selectable text on this page")
        stage = wait_for_page_image(page)
        x0, y0 = stage["x"] + stage["width"] * 0.30, stage["y"] + stage["height"] * 0.18
        page.mouse.move(x0, y0)
        page.mouse.down()
        page.mouse.move(x0 + stage["width"] * 0.35, y0 + stage["height"] * 0.03, steps=8)
        page.mouse.up()
        page.wait_for_selector("#edit-existing-panel", state="visible")
        page.fill("#edit-existing-text", "")  # erase only
        page.click("text=Apply")
        page.wait_for_selector(".redact-cover")
        check("area erase on scanned page", page.locator(".redact-cover").count() == 1)
        page.screenshot(path=SHOTS / "4_scan_erased.png", full_page=True)
        browser.close()

    # ---- 5. export: original text must be truly gone ---------------------
    out = httpx.post(f"{BASE}/documents/{text_doc}/export", data={}).content
    exported = fitz.open(stream=out, filetype="pdf")
    page_text = exported[0].get_text()
    check("export has the edited text", "GUEST HOUSE (re-edited)" in page_text)
    check("export no longer contains original line",
          "NITT GUEST HOUSE\n" not in page_text and "\nNITT GUEST HOUSE" not in page_text)
    exported[0].get_pixmap(dpi=110).save(SHOTS / "5_export_page0.png")

    out = httpx.post(f"{BASE}/documents/{scan_doc}/export", data={}).content
    fitz.open(stream=out, filetype="pdf")[0].get_pixmap(dpi=110).save(SHOTS / "5_export_scan.png")
    print(f"\nAll checks passed. Screenshots in {SHOTS}")


if __name__ == "__main__":
    main()
