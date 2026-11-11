"""Typed text on a page: lay it out with MuPDF's HTML engine and find where anchored ink sits.

A page's text is one block at (pos_x, pos_y), `width` wide, in .rm units. Paragraphs carry a style
(plain, heading, bold, bullets, checkboxes) and spans bold or italic. Ink drawn next to text is
stored relative to a character of it ("anchored") so it moves when the text reflows. The anchor is
the top of that character's line, the convention rmc uses: lines are boxes from their top, and the
first line's top is TEXT_TOP_Y above pos_y. Fonts are MuPDF's sans-serif with its Unicode fallbacks,
not the tablet's, so line breaks can differ from the tablet's in long paragraphs.

Layout happens once in .rm units (1 unit = 1 pt); drawing scales the same HTML and CSS, so line
breaks do not depend on the output size.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field

import pymupdf
from rmscene import scene_items as si
from rmscene.tagged_block_common import CrdtId
from rmscene.text import TextDocument

log = logging.getLogger(__name__)

TEXT_TOP_Y = -88  # the first line's top, relative to pos_y
FONT = 34  # .rm units
LINE = 70
HEADING_FONT = 52
HEADING_LINE = 105
INDENT = 40
LAYOUT_HEIGHT = 100_000  # tall enough for any page's text

S = si.ParagraphStyle
PREFIX = {S.BULLET: "• ", S.BULLET2: "◦ ", S.CHECKBOX: "☐ ", S.CHECKBOX_CHECKED: "☑ "}
CLASS = {S.HEADING: "heading", S.BOLD: "bold", S.BULLET: "bullet", S.BULLET2: "bullet2",
         S.CHECKBOX: "bullet", S.CHECKBOX_CHECKED: "bullet"}


def css(scale: float = 1.0) -> str:
    def u(v: float) -> str:
        return f"{v * scale:.3f}pt"

    return (
        f"body {{margin: 0; padding: 0; font-family: sans-serif; font-size: {u(FONT)}; "
        f"line-height: {u(LINE)}; color: black}}\n"
        "p {margin: 0; padding: 0}\n"
        f".heading {{font-size: {u(HEADING_FONT)}; line-height: {u(HEADING_LINE)}; font-weight: bold}}\n"
        ".bold {font-weight: bold}\n"
        f".bullet {{margin-left: {u(INDENT)}}}\n"
        f".bullet2 {{margin-left: {u(2 * INDENT)}}}\n"
    )


@dataclass
class TextLayout:
    html: str
    x: float  # .rm units: left edge, top of the first line, width, height
    top: float
    width: float
    height: float
    anchors: dict[CrdtId, float] = field(default_factory=dict)  # character -> its line's top (y)
    plain: str = ""

    @property
    def bottom(self) -> float:
        return self.top + self.height


def _html(doc: TextDocument) -> tuple[str, dict[str, tuple[int, list[CrdtId]]]]:
    """HTML with one span per word, and per span ID: (its paragraph, its characters)."""
    parts, words = [], {}
    for i, para in enumerate(doc.contents):
        style = para.style.value
        cls = CLASS.get(style)
        out = [f'<p id="p{i}"' + (f' class="{cls}"' if cls else "") + ">", PREFIX.get(style, "")]
        for span in para.contents:
            props = span.properties or {}
            open_, close = "", ""
            if props.get("font-weight") == "bold":
                open_, close = open_ + "<b>", "</b>" + close
            if props.get("font-style") == "italic":
                open_, close = open_ + "<i>", "</i>" + close
            out.append(open_)
            word, ids = "", []
            for ch, cid in zip(span.s, span.i):
                if ch.isspace():
                    if word:
                        out.append(_word(word, i, ids, words))
                        word, ids = "", []
                    out.append(" ")
                else:
                    word += ch
                    ids.append(cid)
            if word:
                out.append(_word(word, i, ids, words))
            out.append(close)
        if not str(para).strip():
            out.append(" ")  # an empty line still takes its height
        out.append("</p>")
        parts.append("".join(out))
    return "\n".join(parts), words


def _word(word: str, para: int, ids: list[CrdtId], words: dict) -> str:
    wid = f"w{len(words)}"
    words[wid] = (para, ids)
    return f'<span id="{wid}">{html.escape(word)}</span>'


def layout(text: si.Text) -> TextLayout | None:
    try:
        doc = TextDocument.from_scene_item(text)
    except Exception:
        log.exception("could not read the page's text")
        return None
    if not any(str(p).strip() for p in doc.contents):
        return None
    body, words = _html(doc)
    story = pymupdf.Story(body, user_css=css())
    _, filled = story.place(pymupdf.Rect(0, 0, text.width, LAYOUT_HEIGHT))
    tops: dict[str, float] = {}

    def seen(pos):
        if pos.id and pos.id not in tops and pos.open_close & 1:
            tops[pos.id] = pos.rect[1]  # (x0, y0, x1, y1)

    story.element_positions(seen)
    top = text.pos_y + TEXT_TOP_Y
    paragraphs = doc.contents
    anchors: dict[CrdtId, float] = {}
    # Every character first anchors to its paragraph's first line...
    for i, para in enumerate(paragraphs):
        para_top = tops.get(f"p{i}")
        if para_top is None:
            continue
        anchors[para.start_id] = top + para_top
        for span in para.contents:
            for cid in span.i:
                anchors[cid] = top + para_top
    # ...then words on later lines of a wrapped paragraph to their own line.
    for wid, (i, ids) in words.items():
        y, para_top = tops.get(wid), tops.get(f"p{i}")
        if y is None or para_top is None:
            continue
        line = HEADING_LINE if paragraphs[i].style.value == S.HEADING else LINE
        line_top = para_top + int(max(y - para_top, 0) // line) * line
        for cid in ids:
            anchors[cid] = top + line_top
    return TextLayout(body, text.pos_x, top, text.width, pymupdf.Rect(filled).y1, anchors,
                      "\n".join(str(p) for p in paragraphs))


def draw(page: pymupdf.Page, lay: TextLayout, mapping) -> tuple[float, float]:
    """Draw the text onto `page`; return its vertical extent in points."""
    tl = mapping.pt(lay.x, lay.top)
    br = mapping.pt(lay.x + lay.width, lay.bottom + LINE)
    rect = pymupdf.Rect(tl, pymupdf.Point(br.x, min(br.y, page.rect.y1))) & page.rect
    if rect.is_empty:
        return rect.y0, rect.y0
    spare, _ = page.insert_htmlbox(rect, lay.html, css=css(mapping.scale), scale_low=1)
    if spare < 0:  # runs off the page (a PDF page cannot grow): shrink to fit instead of dropping it
        page.insert_htmlbox(rect, lay.html, css=css(mapping.scale), scale_low=0)
        return rect.y0, rect.y1
    return rect.y0, min(rect.y0 + lay.height * mapping.scale, rect.y1)
