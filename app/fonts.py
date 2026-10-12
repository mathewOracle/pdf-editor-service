"""Pick the font to draw new text in, so it matches the text around it.

PDFs usually embed only a *subset* of each font: just the letters that
document happened to use. So for every piece of new text, try in order:

  1. the font embedded in this PDF, if it has every letter needed (exact)
  2. one of the 14 standard PDF fonts, if that's what the PDF names
  3. an installed font with the same name (e.g. Calibri from MS Office)
  4. the closest standard font by style: serif/sans/mono, bold, italic

The browser preview loads the same font file through @font-face, so the
editor shows what the export will contain.
"""
import hashlib
import json
import re
import sys
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import pymupdf as fitz

from .db import STORAGE_DIR

FLAG_ITALIC, FLAG_BOLD = 2, 16  # PyMuPDF text span flag bits

Generic = Literal["serif", "sans-serif", "monospace"]
FontKey = tuple[str, bool, bool]  # (normalized family, bold, italic)

_INDEX_PATH = STORAGE_DIR / "font_index.json"
_INDEX_VERSION = 2
_FONT_SUFFIXES = (".ttf", ".otf", ".ttc")
_WEB_LOADABLE = ("ttf", "otf")  # browsers can't load bare CFF/Type1

_STYLE_WORDS = re.compile(r"bold|italic|oblique|regular")
_TRAILING_JUNK = re.compile(r"(mt|ps|roman)$")
_SERIF_HINTS = ("times", "georgia", "garamond", "bookman", "cambria", "palatino", "serif", "baskerville")
_MONO_HINTS = ("courier", "mono", "consolas", "menlo", "code")


# ------------------------------------------------------------- font names --

def family_label(font_name: str) -> str:
    """Display family of a PDF font name:
    'ABCDEE+Calibri-Bold' -> 'Calibri', 'TimesNewRomanPSMT' -> 'Times New Roman'.
    """
    family = re.split(r"[-,]", font_name.split("+", 1)[-1])[0] or font_name
    family = re.sub(r"(PSMT|MT|PS)$", "", family)
    family = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", family)
    # "Calibri Bold Italic" (how PyMuPDF names fonts it inserted) -> "Calibri"
    return re.sub(r"(\s+(Regular|Bold|Italic|Oblique))+$", "", family) or family


def font_key(font_name: str, flags: int = 0) -> FontKey:
    """One key for every spelling of a font, so the PDF's name, an installed
    font's name and the standard fonts can be matched against each other.
    'ABCDEE+TimesNewRomanPS-BoldMT' and 'Times New Roman Bold' give the same
    key. Bold/italic come from the name *or* the flags.
    """
    name = font_name.split("+", 1)[-1].lower()
    bold = bool(flags & FLAG_BOLD) or any(w in name for w in ("bold", "black", "heavy"))
    italic = bool(flags & FLAG_ITALIC) or "italic" in name or "oblique" in name
    family = re.sub(r"[^a-z0-9]", "", _STYLE_WORDS.sub("", name))
    while _TRAILING_JUNK.search(family):
        family = _TRAILING_JUNK.sub("", family)
    return family, bold, italic


def _index_key(key: FontKey) -> str:
    family, bold, italic = key
    return f"{family}|{int(bold)}|{int(italic)}"


# ------------------------------------------------------ standard 14 fonts --

# (generic, bold, italic) -> PyMuPDF short name
_BASE14_BY_STYLE: dict[tuple[Generic, bool, bool], str] = {
    ("sans-serif", False, False): "helv", ("sans-serif", True, False): "hebo",
    ("sans-serif", False, True): "heit", ("sans-serif", True, True): "hebi",
    ("serif", False, False): "tiro", ("serif", True, False): "tibo",
    ("serif", False, True): "tiit", ("serif", True, True): "tibi",
    ("monospace", False, False): "cour", ("monospace", True, False): "cobo",
    ("monospace", False, True): "coit", ("monospace", True, True): "cobi",
}
_BASE14_FAMILY = {"sans-serif": "Helvetica", "serif": "Times", "monospace": "Courier"}
_BASE14_BY_FAMILY = {font_key(name)[0]: generic for generic, name in _BASE14_FAMILY.items()}
# What the browser should use to preview a standard font.
_LOCAL_STACKS = {
    "sans-serif": "Helvetica, Arial, sans-serif",
    "serif": "'Times New Roman', Times, serif",
    "monospace": "'Courier New', Courier, monospace",
}

# Offered in the font dropdown even when the PDF doesn't use them.
STANDARD_FAMILIES = tuple(_BASE14_FAMILY.values())


# --------------------------------------------------- installed font index --

def _font_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        dirs = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), home / "Library/Fonts"]
        # MS Office ships Calibri, Cambria, Bookman... inside the app bundle.
        return dirs + list(Path("/Applications").glob("Microsoft */Contents/Resources/DFonts"))
    if sys.platform == "win32":
        return [Path("C:/Windows/Fonts"), home / "AppData/Local/Microsoft/Windows/Fonts"]
    return [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), home / ".fonts"]


def _build_index() -> dict[str, str]:
    """Font key -> font file path, for every installed font."""
    index: dict[str, str] = {}
    for font_dir in _font_dirs():
        if not font_dir.is_dir():
            continue
        for path in sorted(font_dir.rglob("*")):
            if path.suffix.lower() not in _FONT_SUFFIXES:
                continue
            try:
                name = fitz.Font(fontfile=str(path)).name
            except Exception:  # unreadable or unsupported font file: skip it
                continue
            index.setdefault(_index_key(font_key(name)), str(path))
    return index


_index_lock = threading.Lock()
_index: Optional[dict[str, str]] = None


def installed_fonts() -> dict[str, str]:
    """Scanning takes several seconds, so the result is cached on disk.
    Delete font_index.json after installing new fonts to rescan.
    """
    global _index
    with _index_lock:
        if _index is None:
            try:
                cached = json.loads(_INDEX_PATH.read_text())
                if cached.get("version") != _INDEX_VERSION:
                    raise ValueError("stale font index")
                _index = cached["fonts"]
            except (OSError, ValueError, KeyError):
                _index = _build_index()
                _INDEX_PATH.write_text(json.dumps({"version": _INDEX_VERSION, "fonts": _index}))
        return _index


def warm_font_index() -> None:
    """Build the index in the background so the first edit isn't slow."""
    threading.Thread(target=installed_fonts, daemon=True).start()


def _path_id(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:16]


def _installed_path(path_id: str) -> Optional[str]:
    return next((p for p in installed_fonts().values() if _path_id(p) == path_id), None)


@lru_cache(maxsize=64)
def _load_installed(path: str) -> fitz.Font:
    return fitz.Font(fontfile=path)


def _generic_of(font: fitz.Font) -> Generic:
    flags = font.flags
    return "monospace" if flags.get("mono") else "serif" if flags.get("serif") else "sans-serif"


# --------------------------------------------------------------- resolve --

@dataclass(frozen=True)
class ResolvedFont:
    font_id: str  # stable and URL-safe: "emb-15", "sys-ab12cd...", "b14-hebo"
    label: str  # human name, e.g. "Calibri Bold"
    quality: Literal["exact", "installed", "similar"]
    note: str  # one sentence for the UI
    family: str  # the family that was asked for, e.g. "Calibri"
    bold: bool
    italic: bool
    generic: Generic
    baseline_ratio: float  # baseline distance below the top of a 1em box, per pt
    web_loadable: bool  # can the browser preview load the font file itself?

    @property
    def base14(self) -> Optional[str]:
        return self.font_id[4:] if self.font_id.startswith("b14-") else None

    @property
    def css_family(self) -> str:
        """CSS font-family for the preview, best match first."""
        stack = f"'{self.family}', {_LOCAL_STACKS[self.generic]}"
        return f"'pdf-{self.font_id}', {stack}" if self.web_loadable else stack


def _baseline_ratio(font: fitz.Font) -> float:
    """Where the baseline sits in a 1em-tall box. That's how browsers lay
    out text with CSS line-height: 1, so preview and export line up.
    """
    return (1 + font.ascender + font.descender) / 2


class FontResolver:
    """Font lookups for one PDF. Opens the PDF, so use it as a context manager."""

    def __init__(self, pdf_path: Path):
        self._doc = fitz.open(pdf_path)
        self._embedded: dict[FontKey, tuple[int, str, fitz.Font]] = {}  # xref, ext, font
        self.families: list[str] = []  # display names, in order of first use
        for page in self._doc:
            for font_info in page.get_fonts():
                xref, basefont = font_info[0], font_info[3]
                label = family_label(basefont)
                if label and label not in self.families:
                    self.families.append(label)
                key = font_key(basefont)
                if key not in self._embedded:
                    self._load_embedded(key, xref)

    def _load_embedded(self, key: FontKey, xref: int) -> None:
        _, ext, _, buffer = self._doc.extract_font(xref)
        if not buffer:
            return
        try:
            self._embedded[key] = (xref, ext, fitz.Font(fontbuffer=buffer))
        except Exception:  # damaged embedded font: treat as not embedded
            pass

    def __enter__(self) -> "FontResolver":
        return self

    def __exit__(self, *exc) -> None:
        self._doc.close()

    def dropdown_families(self) -> list[str]:
        return self.families + [f for f in STANDARD_FAMILIES if f not in self.families]

    def resolve(self, family: Optional[str], bold: bool, italic: bool, text: str) -> ResolvedFont:
        family = family or "Helvetica"
        key = (font_key(family)[0], bold, italic)
        needed = {ord(c) for c in text if not c.isspace()}
        generic = self._generic(key[0])

        def build(font_id, label, quality, note, font, web_loadable) -> ResolvedFont:
            return ResolvedFont(
                font_id, label, quality, note, family, bold, italic, generic,
                _baseline_ratio(font), web_loadable,
            )

        embedded = self._embedded.get(key)
        if embedded and all(embedded[2].has_glyph(c) for c in needed):
            xref, ext, font = embedded
            return build(f"emb-{xref}", font.name, "exact", "Exact font, taken from this PDF.",
                         font, ext in _WEB_LOADABLE)

        if key[0] in _BASE14_BY_FAMILY:
            short = _BASE14_BY_STYLE[(_BASE14_BY_FAMILY[key[0]], bold, italic)]
            return self._base14(build, short, "exact", "Standard PDF font, exact match.")

        path = installed_fonts().get(_index_key(key))
        if path:
            font = _load_installed(path)
            if all(font.has_glyph(c) for c in needed):
                note = (
                    "This PDF only has some of this font's letters, so using the copy installed on this computer."
                    if embedded
                    else "Using the copy installed on this computer."
                )
                return build(f"sys-{_path_id(path)}", font.name, "installed", note, font,
                             Path(path).suffix.lower().lstrip(".") in _WEB_LOADABLE)

        style = " ".join(w for w, on in (("bold", bold), ("italic", italic)) if on)
        return self._base14(
            build, _BASE14_BY_STYLE[(generic, bold, italic)], "similar",
            f"{family} {style} isn't available on this computer, so using the closest standard font."
            .replace("  ", " "),
        )

    @staticmethod
    def _base14(build, short: str, quality: str, note: str) -> ResolvedFont:
        font = fitz.Font(short)
        return build(f"b14-{short}", font.name, quality, note, font, False)

    def _generic(self, family: str) -> Generic:
        """Serif, sans or mono, for picking the closest standard font."""
        if family in _BASE14_BY_FAMILY:
            return _BASE14_BY_FAMILY[family]
        for (fam, _, _), (_, _, font) in self._embedded.items():
            if fam == family:
                return _generic_of(font)
        for bold, italic in ((False, False), (True, False), (False, True), (True, True)):
            path = installed_fonts().get(_index_key((family, bold, italic)))
            if path:
                return _generic_of(_load_installed(path))
        if any(h in family for h in _MONO_HINTS):
            return "monospace"
        return "serif" if any(h in family for h in _SERIF_HINTS) else "sans-serif"

    def font_file(self, font_id: str) -> Optional[tuple[bytes, str]]:
        """(font file bytes, extension) behind a ResolvedFont.font_id, for
        embedding in the export and for the browser's @font-face. None for
        unknown ids and for the standard fonts (they need no file).
        """
        kind, _, value = font_id.partition("-")
        if kind == "emb" and value.isdigit():
            xref = int(value)
            for x, ext, _ in self._embedded.values():
                if x == xref:
                    return self._doc.extract_font(xref)[3], ext
        elif kind == "sys":
            path = _installed_path(value)
            if path:
                return Path(path).read_bytes(), Path(path).suffix.lower().lstrip(".")
        return None
