"""End-to-end check of undo/redo in a real browser.

Run with the server up:
    python tests/e2e_undo_redo.py http://127.0.0.1:8420

Covers:
  1. Undo/redo buttons start disabled, enable after a change.
  2. Undo removes an added text element; redo brings it back.
  3. A chain of 3 edits can be undone 3 times back to the original.
  4. A new edit after undoing clears the redo stack (standard editor behavior).
  5. Undo survives a page reload (it's server-side, not in-memory JS state).
"""
import sys
from pathlib import Path

import httpx
from playwright.sync_api import Page, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8420"
TEST_PDF = Path("/tmp/e2e_undo_redo.pdf")


def check(label: str, ok: bool, detail="") -> None:
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail else ""))
    if not ok:
        sys.exit(1)


def make_pdf() -> None:
    import pymupdf as fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "Original line one", fontsize=12)
    doc.save(TEST_PDF)


def upload() -> int:
    with TEST_PDF.open("rb") as f:
        r = httpx.post(f"{BASE}/documents", files={"file": (TEST_PDF.name, f, "application/pdf")})
    return int(r.headers["location"].split("/")[2])


def wait_for_stage(page: Page) -> dict:
    page.wait_for_load_state("networkidle")
    page.wait_for_function("document.getElementById('page-stage').getBoundingClientRect().width > 100")
    return page.locator("#page-stage").bounding_box()


def add_text(page: Page, stage: dict, label: str, x_frac: float) -> None:
    page.click("#add-text-toggle")
    page.locator("#page-stage").click(position={"x": stage["width"] * x_frac, "y": stage["height"] * 0.5})
    page.wait_for_selector("#text-panel:not([hidden])")
    page.fill("#tp-text", label)
    page.click("#tp-submit")
    page.wait_for_selector(f".overlay-el.text >> text={label}")
    page.click("#add-text-toggle")


def click_history(page: Page, button_id: str) -> None:
    """Undo/redo trigger a reload (see undo.js); wait for it, not just the click."""
    with page.expect_navigation():
        page.click(f"#{button_id}")


def main() -> None:
    make_pdf()
    doc_id = upload()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(f"{BASE}/documents/{doc_id}/edit")
        stage = wait_for_stage(page)

        # ---- 1. buttons start disabled ------------------------------------
        page.wait_for_function("document.getElementById('undo-btn').disabled === true")
        check("undo starts disabled", page.is_disabled("#undo-btn"))
        check("redo starts disabled", page.is_disabled("#redo-btn"))

        # ---- 2. one change, undo, redo ------------------------------------
        add_text(page, stage, "First addition", 0.15)
        page.wait_for_function("!document.getElementById('undo-btn').disabled")
        check("undo enables after a change", not page.is_disabled("#undo-btn"))
        check("undo button explains the change", "Add text" in page.get_attribute("#undo-btn", "title"))

        click_history(page, "undo-btn")
        wait_for_stage(page)
        check("undo removed the added text", page.locator("text=First addition").count() == 0)
        check("redo is now available", not page.is_disabled("#redo-btn"))

        click_history(page, "redo-btn")
        wait_for_stage(page)
        page.wait_for_selector(".overlay-el.text >> text=First addition")
        check("redo brought the text back", True)

        # ---- 3. a chain of edits, undone all the way back -----------------
        stage = wait_for_stage(page)
        add_text(page, stage, "Second addition", 0.4)
        stage = wait_for_stage(page)
        add_text(page, stage, "Third addition", 0.65)
        page.wait_for_function("!document.getElementById('undo-btn').disabled")

        click_history(page, "undo-btn")  # removes "Third addition"
        wait_for_stage(page)
        check("chain undo 1/3", page.locator("text=Third addition").count() == 0)
        check("earlier additions still there", page.locator("text=Second addition").count() == 1)

        click_history(page, "undo-btn")  # removes "Second addition"
        wait_for_stage(page)
        check("chain undo 2/3", page.locator("text=Second addition").count() == 0)
        check("first addition survives two undos", page.locator("text=First addition").count() == 1)

        click_history(page, "undo-btn")  # removes "First addition", back to original
        wait_for_stage(page)
        check("chain undo 3/3: back to the original", page.locator("text=First addition").count() == 0)
        check("undo now disabled (nothing before the original)", page.is_disabled("#undo-btn"))

        # ---- 4. a new edit after undoing clears the redo stack ------------
        check("redo available before the new edit", not page.is_disabled("#redo-btn"))
        add_text(page, stage, "Branched addition", 0.15)
        page.wait_for_function("!document.getElementById('undo-btn').disabled")
        check("redo cleared by a new edit", page.is_disabled("#redo-btn"))

        # ---- 5. undo state is server-side, survives a reload ---------------
        page.reload()
        wait_for_stage(page)
        page.wait_for_function("document.getElementById('undo-btn') !== null")
        check("undo still available after reload", not page.is_disabled("#undo-btn"))
        click_history(page, "undo-btn")
        wait_for_stage(page)
        check("undo after reload actually undoes", page.locator("text=Branched addition").count() == 0)

        # ---- double-undo past the beginning doesn't break anything --------
        r = httpx.post(f"{BASE}/documents/{doc_id}/undo")
        check("undo past the start returns 409, not a crash", r.status_code == 409)

        browser.close()

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
