"""Print a PDF as ink: a notebook whose pages are pen strokes instead of a PDF background.

A "printer driver" for the tablet. Text is redrawn in a single-line (Hershey) font, one glyph per
character, each fitted to the box the PDF gives that character, so the PDF's layout (Chromium's,
for markdown) is kept: tables, code blocks, headings. Line art (rules, borders, outlines) becomes
strokes along its paths, small dark fills (bullets, rules drawn as boxes) are hatched, and light
fills (code and table backgrounds) are outlined in gray. Images are left out.

Every stroke goes on a bottom layer named "Print", with a second, empty layer "Notes" above it for
the reader's own ink. Coordinates follow the renderer's "width" mapping: the PDF page width spans
`rm_width` .rm units, x = 0 at its centre, y = 0 at its top.
"""

from __future__ import annotations

import io
import json
import math
import unicodedata
import uuid as uuidlib
from dataclasses import dataclass
from functools import lru_cache

import pymupdf
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import (
    AuthorIdsBlock,
    MigrationInfoBlock,
    PageInfoBlock,
    SceneGroupItemBlock,
    SceneLineItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
    write_blocks,
)
from rmscene.tagged_block_common import CrdtId, LwwValue

from .cloud import new_uuid
from .rmdoc import RmDoc
from .scene import RM_WIDTH

FONTS = {"plotter": "futural", "bold": "futuram", "script": "scripts", "serif": "rowmans", "italic": "timesi"}
DEFAULT_FONT = "plotter"

PRINT_LAYER = CrdtId(0, 11)
NOTES_LAYER = CrdtId(0, 21)
AUTHOR = 2  # our strokes' author index; the tablet writes its own strokes under its own
AUTHOR_UUID = uuidlib.uuid5(uuidlib.NAMESPACE_URL, "https://github.com/derfred/remarkling/inkprint")
TS = CrdtId(AUTHOR, 1)

CAP_RATIO = 0.72  # cap height / font size, for typical text fonts
PEN_WIDTH_PT = 0.55  # body text stroke width, in PDF points
BOLD_FACTOR = 1.9
ITALIC_SLANT = 0.21
CURVE_STEPS = 8  # segments per Bézier curve
HATCH_PT = 0.6  # hatch line spacing for dark fills
LIGHT = 0.8  # fills lighter than this (luma) are backgrounds: outlined, not hatched
MAX_HATCH_AREA_PT2 = 400  # bigger dark fills are outlined only

# Characters the Hershey fonts lack, drawn as their nearest ASCII.
FALLBACK = {
    "\u2018": "'", "\u2019": "'", "\u201a": ",", "\u201c": '"', "\u201d": '"', "\u201e": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u00b7": ".", "\u2026": "...",
    "\u00a0": " ", "\u2192": "->", "\u2190": "<-", "\u00d7": "x", "\u2264": "<=", "\u2265": ">=",
}


def _circle(cx, cy, r, n=12):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


_SQUARE = [[(-3, -10), (3, -10), (3, -4), (-3, -4), (-3, -10)], [(-3, -8), (3, -8)], [(-3, -6), (3, -6)]]
# Glyphs drawn here, in font units (cap height 21, baseline y = 0, y down): (strokes, advance).
EXTRA_GLYPHS = {
    "\u2022": ([_circle(0, -7, 3.2), _circle(0, -7, 2), _circle(0, -7, 0.8)], 12),  # bullet
    "\u25e6": ([_circle(0, -7, 3.2)], 12),  # white bullet
    "\u25cb": ([_circle(0, -7, 3.2)], 12),  # white circle
    "\u25aa": (_SQUARE, 12),
    "\u25a0": (_SQUARE, 12),
    "\u20ac": ([[(6, -18), (3, -21), (-1, -21), (-4, -18), (-6, -12), (-6, -9), (-4, -3), (-1, 0), (3, 0), (6, -3)],
                [(-9, -13), (3, -13)], [(-9, -8), (2, -8)]], 20),  # euro
    "\u2713": ([[(-7, -9), (-2, -3), (7, -18)]], 18),  # check mark
    "\u2717": ([[(-6, -16), (6, -2)], [(6, -16), (-6, -2)]], 16),  # ballot x
}


@dataclass
class Glyph:
    strokes: list[list[tuple[float, float]]]  # font units, y down, baseline at y = 0
    left: float
    width: float  # advance


@dataclass
class Font:
    glyphs: dict[str, Glyph]
    cap: float  # cap height in font units


@lru_cache(maxsize=None)
def load_font(name: str = DEFAULT_FONT) -> Font:
    from HersheyFonts import HersheyFonts

    hf = HersheyFonts()
    hf.load_default_font(FONTS.get(name, name))
    glyphs, base, cap = {}, 9, 21
    for code in range(32, 127):
        g = next(iter(hf.glyphs_for_text(chr(code))), None)
        if g is None:
            continue
        base, cap = g.base_line, g.base_line - g.cap_line
        glyphs[chr(code)] = Glyph(
            strokes=[[(x, y - g.base_line) for x, y in s] for s in g.strokes],
            left=g.left_offset, width=g.char_width,
        )
    for c, (strokes, width) in EXTRA_GLYPHS.items():
        glyphs[c] = Glyph(strokes=strokes, left=-width / 2, width=width)
    return Font(glyphs, cap)


def _ascii(c: str) -> str:
    if c in FALLBACK:
        return FALLBACK[c]
    return unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode() or "?"


@dataclass
class PrintStroke:
    points: list[tuple[float, float]]  # .rm units
    width: float  # .rm units
    color: int = si.PenColor.BLACK


class _Page:
    """Collects one PDF page's strokes, in .rm units."""

    def __init__(self, page: pymupdf.Page, rm_width: float):
        self.s = rm_width / page.rect.width
        self.x0 = page.rect.width / 2
        self.strokes: list[PrintStroke] = []

    def pt(self, x: float, y: float) -> tuple[float, float]:
        return ((x - self.x0) * self.s, y * self.s)

    def add(self, pts: list[tuple[float, float]], width_pt: float, color: int = si.PenColor.BLACK):
        if len(pts) == 1:
            pts = pts * 2
        if len(pts) >= 2:
            self.strokes.append(PrintStroke([self.pt(x, y) for x, y in pts], max(width_pt, 0.3) * self.s, color))


def _luma(rgb) -> float:
    if not rgb:
        return 0.0
    r, g, b = rgb[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b


def _pen_color(rgb) -> int | None:
    """Black or gray for a PDF colour; None for white (invisible on paper)."""
    luma = _luma(rgb)
    if luma > 0.92:
        return None
    return si.PenColor.GRAY if luma > 0.45 else si.PenColor.BLACK


def _text(page: pymupdf.Page, out: _Page, font: Font, bold: Font, mono: Font):
    raw = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            dx, dy = line["dir"]
            if abs(dy) > 1e-3 or dx <= 0:
                continue  # rotated text: not yet
            for span in line["spans"]:
                rgb = pymupdf.sRGB_to_pdf(span["color"])
                color = _pen_color(rgb)
                if color is None:
                    continue
                is_bold = bool(span["flags"] & pymupdf.TEXT_FONT_BOLD) or "bold" in span["font"].lower()
                is_italic = bool(span["flags"] & pymupdf.TEXT_FONT_ITALIC) or "italic" in span["font"].lower()
                is_mono = bool(span["flags"] & pymupdf.TEXT_FONT_MONOSPACED) or "mono" in span["font"].lower()
                f = mono if is_mono else bold if is_bold else font
                size = span["size"]
                scale = CAP_RATIO * size / f.cap  # pt per font unit
                width = PEN_WIDTH_PT * (size / 11) ** 0.5 * (
                    (BOLD_FACTOR if f is font else 1.35) if is_bold else 1)
                for ch in span["chars"]:
                    _draw_char(out, f, ch, scale, width, is_italic, color)


def _draw_char(out: _Page, font: Font, ch: dict, scale: float, width: float, italic: bool, color: int):
    text = ch["c"] if ch["c"] in font.glyphs else _ascii(ch["c"])
    glyphs = [font.glyphs[c] for c in text if c in font.glyphs and c != " "]
    if not glyphs:
        return
    x0, _, x1, _ = ch["bbox"]
    ox, oy = ch["origin"]
    advance = sum(g.width for g in glyphs) * scale
    # Fit the glyphs to the character's box: proportional fonts vary, monospace must line up.
    sx = scale * min(max((x1 - x0) / advance, 0.6), 1.5) if advance > 0 else scale
    # Centred in the box: in monospace text a narrow glyph (i, l, .) sits mid-cell.
    x = x0 + ((x1 - x0) - sum(g.width for g in glyphs) * sx) / 2
    for g in glyphs:
        for s in g.strokes:
            pts = []
            for gx, gy in s:
                y = oy + gy * scale
                px = x + (gx - g.left) * sx + (ITALIC_SLANT * (oy - y) if italic else 0)
                pts.append((px, y))
            out.add(pts, width, color)
        x += g.width * sx


def _bezier(p0, p1, p2, p3, steps=CURVE_STEPS):
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        out.append((u**3 * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t**3 * p3.x,
                    u**3 * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t**3 * p3.y))
    return out


def _subpaths(path: dict) -> list[list[tuple[float, float]]]:
    """A drawing's items as polylines: connected items join, rectangles and quads close."""
    subs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []

    def start(p):
        nonlocal cur
        if not cur or math.dist(cur[-1], (p.x, p.y)) > 0.01:
            if len(cur) >= 2:
                subs.append(cur)
            cur = [(p.x, p.y)]

    for item in path["items"]:
        kind = item[0]
        if kind == "l":
            start(item[1])
            cur.append((item[2].x, item[2].y))
        elif kind == "c":
            start(item[1])
            cur += _bezier(*item[1:5])
        elif kind == "re":
            r = item[1]
            if len(cur) >= 2:
                subs.append(cur)
            cur = []
            subs.append([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x0, r.y0)])
        elif kind == "qu":
            q = item[1]
            if len(cur) >= 2:
                subs.append(cur)
            cur = []
            subs.append([(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y), (q.ul.x, q.ul.y)])
    if len(cur) >= 2:
        subs.append(cur)
    if path.get("closePath"):
        subs = [s if s[0] == s[-1] else s + [s[0]] for s in subs]
    return subs


def _hatch(polys: list[list[tuple[float, float]]], spacing: float) -> list[list[tuple[float, float]]]:
    """Horizontal even-odd scanlines through closed polygons."""
    ys = [y for p in polys for _, y in p]
    y0, y1 = min(ys), max(ys)
    out = []
    n = max(int((y1 - y0) / spacing), 1)
    for i in range(n):
        y = y0 + (i + 0.5) * (y1 - y0) / n
        xs = []
        for p in polys:
            for (ax, ay), (bx, by) in zip(p, p[1:] + p[:1]):
                if (ay <= y < by) or (by <= y < ay):
                    xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
        xs.sort()
        out += [[(xs[j], y), (xs[j + 1], y)] for j in range(0, len(xs) - 1, 2) if xs[j + 1] - xs[j] > 0.05]
    return out


def _drawings(page: pymupdf.Page, out: _Page):
    for path in page.get_drawings():
        subs = _subpaths(path)
        if not subs:
            continue
        kind = path.get("type") or ""
        if "s" in kind and (color := _pen_color(path.get("color"))) is not None:
            for s in subs:
                out.add(s, path.get("width") or 1.0, color)
        if "f" in kind and path.get("fill"):
            fill = path["fill"]
            xs = [x for s in subs for x, _ in s]
            ys = [y for s in subs for _, y in s]
            w, h = max(xs) - min(xs), max(ys) - min(ys)
            if _luma(fill) > LIGHT:
                if _luma(fill) < 0.99 and min(w, h) < 5 and max(w, h) > 5:  # a light bar: blockquote, rule
                    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                    pts = [(min(xs), cy), (max(xs), cy)] if w >= h else [(cx, min(ys)), (cx, max(ys))]
                    out.add(pts, max(min(w, h), 0.4), si.PenColor.GRAY)
                elif _luma(fill) < 0.99 and w > 20 and h > 10 and "s" not in kind:
                    for s in subs:
                        out.add(s, 0.4, si.PenColor.GRAY)  # a background: its outline
                continue
            color = _pen_color(fill) or si.PenColor.BLACK
            if min(w, h) < 2.5:  # a rule or border drawn as a thin box: one line along it
                cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                pts = [(min(xs), cy), (max(xs), cy)] if w >= h else [(cx, min(ys)), (cx, max(ys))]
                out.add(pts, max(min(w, h), 0.3), color)
            elif w * h <= MAX_HATCH_AREA_PT2:
                closed = [s if s[0] == s[-1] else s + [s[0]] for s in subs]
                for seg in _hatch(closed, HATCH_PT):
                    out.add(seg, HATCH_PT * 1.1, color)
                for s in closed:
                    out.add(s, 0.3, color)
            else:
                for s in subs:
                    out.add(s, 0.5, color)


def page_strokes(page: pymupdf.Page, rm_width: float = RM_WIDTH, font: str = DEFAULT_FONT) -> list[PrintStroke]:
    out = _Page(page, rm_width)
    _drawings(page, out)
    # Code stays in the plain plotter font whatever the body font: it has to be read exactly.
    _text(page, out, load_font(font), load_font("bold") if font == "plotter" else load_font(font), load_font("plotter"))
    return out.strokes


def rm_page(strokes: list[PrintStroke]) -> bytes:
    """A v6 .rm page: `strokes` (fineliner) on a "Print" layer, an empty "Notes" layer on top."""
    def blocks():
        yield AuthorIdsBlock(author_uuids={AUTHOR: AUTHOR_UUID})
        yield MigrationInfoBlock(migration_id=CrdtId(AUTHOR, 1), is_device=False)
        yield PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0)
        for layer in (PRINT_LAYER, NOTES_LAYER):
            yield SceneTreeBlock(tree_id=layer, node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1))
        yield TreeNodeBlock(si.Group(node_id=CrdtId(0, 1)))
        yield TreeNodeBlock(si.Group(node_id=PRINT_LAYER, label=LwwValue(TS, "Print")))
        yield TreeNodeBlock(si.Group(node_id=NOTES_LAYER, label=LwwValue(TS, "Notes")))
        yield SceneGroupItemBlock(parent_id=CrdtId(0, 1), item=CrdtSequenceItem(
            CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, PRINT_LAYER))
        yield SceneGroupItemBlock(parent_id=CrdtId(0, 1), item=CrdtSequenceItem(
            CrdtId(0, 23), CrdtId(0, 13), CrdtId(0, 0), 0, NOTES_LAYER))
        prev = CrdtId(0, 0)
        for i, st in enumerate(strokes):
            item_id = CrdtId(AUTHOR, 100 + i)
            w = max(round(st.width * 4), 1)  # rmscene stores width x 4, as an integer
            line = si.Line(
                color=st.color, tool=si.Pen.FINELINER_2, thickness_scale=1.0, starting_length=0.0,
                points=[si.Point(x=x, y=y, speed=0, direction=0, width=w, pressure=100) for x, y in st.points],
            )
            yield SceneLineItemBlock(parent_id=PRINT_LAYER, item=CrdtSequenceItem(item_id, prev, CrdtId(0, 0), 0, line))
            prev = item_id

    buf = io.BytesIO()
    write_blocks(buf, blocks())
    return buf.getvalue()


def _index(i: int) -> str:
    """Fractional page index strings the tablet orders pages by: ba, bb, ..., bz, ca, ..."""
    return chr(ord("b") + i // 26) + chr(ord("a") + i % 26)


def notebook_content(page_ids: list[str]) -> dict:
    return {
        "cPages": {
            "lastOpened": {"timestamp": "1:1", "value": page_ids[0] if page_ids else ""},
            "original": {"timestamp": "0:0", "value": -1},
            "pages": [{"id": pid, "idx": {"timestamp": "1:2", "value": _index(i)},
                       "template": {"timestamp": "1:1", "value": "Blank"}} for i, pid in enumerate(page_ids)],
            "uuids": [{"first": str(AUTHOR_UUID), "second": 1}],
        },
        "coverPageNumber": 0,
        "documentMetadata": {},
        "extraMetadata": {},
        "fileType": "notebook",
        "fontName": "",
        "formatVersion": 2,
        "lineHeight": -1,
        "orientation": "portrait",
        "pageCount": len(page_ids),
        "pageTags": [],
        "sizeInBytes": "0",
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "zoomMode": "bestFit",
    }


def printed_strokes(doc: RmDoc) -> dict[str, list[list[int]]]:
    """Page ID -> IDs of the strokes the print put there, to tell them from the reader's later."""
    from .scene import stroke_ids

    out = {}
    for pid in doc.page_ids():
        data = doc.rm_file(pid)
        if data:
            out[pid] = sorted([i.part1, i.part2] for i in stroke_ids(data))
    return out


def unprinted(downloaded: RmDoc, source_pdf: bytes) -> RmDoc:
    """A downloaded printed notebook as the pull pipeline reads a PDF doc: the source PDF as its
    background, one tablet page per PDF page. Pass the printed strokes to `overlay(skip=...)`."""
    return RmDoc(uuid=downloaded.uuid, files={**downloaded.files, f"{downloaded.uuid}.pdf": source_pdf})


def pdf_to_rmdoc(pdf: bytes, title: str, rm_width: float = RM_WIDTH, font: str = DEFAULT_FONT) -> RmDoc:
    """A notebook with every PDF page drawn as strokes (see the module docstring)."""
    doc_uuid = new_uuid()
    files: dict[str, bytes] = {}
    page_ids = []
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        for page in d:
            pid = new_uuid()
            page_ids.append(pid)
            files[f"{doc_uuid}/{pid}.rm"] = rm_page(page_strokes(page, rm_width, font))
    files[f"{doc_uuid}.content"] = json.dumps(notebook_content(page_ids)).encode()
    files[f"{doc_uuid}.metadata"] = json.dumps({"visibleName": title, "type": "DocumentType", "parent": ""}).encode()
    files[f"{doc_uuid}.pagedata"] = b"\n".join([b"Blank"] * len(page_ids))
    return RmDoc(uuid=doc_uuid, files=files)
