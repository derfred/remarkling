"""Join a paged PDF into tall pages: layout="tall" for PDFs (opt-in, see push_document).

For documents that flow as one text and were only cut into pages by an export or a print (a
browser's "Save as PDF", a Word or Docs export). Each page is cropped to its content, with running
headers and footers (lines repeated at the top or bottom of most pages, page numbers included)
cut away, and the crops are stacked on one tall page, as vectors: text stays text.

Not for slides, scans, forms or papers whose pages mean something on their own, and it cannot
tell those apart reliably. So it is never applied unasked, and it gives up, leaving the pages as
they are, when a guard fails: pages of different widths, no text layer, or content it cannot
measure. The result says which. A table or figure split across a page break stays split; only the
margin between its halves goes.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

# A PDF page may be at most 14,400 pt tall (200 in); stay below, as the HTML renderer does (5 m).
MAX_HEIGHT_PT = 14_000.0
GAP_PT = 8.0  # between two joined pages' content
EDGE_PT = 18.0  # kept above the first content and below the last
BAND = 0.12  # top and bottom share of a page searched for running headers and footers
REPEAT = 0.6  # share of pages a line must recur on to count as a header or footer
POS_TOLERANCE_PT = 4.0  # ... at the same height, give or take this


@dataclass
class JoinResult:
    pdf: bytes  # the joined PDF, or the input unchanged when `joined` is 0
    layout: str  # "tall" | "pages"
    joined: int = 0  # source pages joined
    pages: int = 0  # pages of the result
    removed_lines: list[str] = field(default_factory=list)  # running headers/footers cut away
    reason: str | None = None  # why the pages were kept, when they were

    def report(self) -> dict:
        out = {"layout": self.layout, "pages": self.pages}
        if self.layout == "tall":
            out["joined"] = self.joined
            if self.removed_lines:
                out["removed_headers_footers"] = self.removed_lines
        else:
            out["reason"] = self.reason
        return out


def _norm(text: str) -> str:
    """A line with its numbers masked, so "Page 2 of 8" and "Page 3 of 8" compare equal."""
    return re.sub(r"\d+", "#", " ".join(text.split())).strip()


def _lines(page: pymupdf.Page) -> list[tuple[str, pymupdf.Rect]]:
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                out.append((text, pymupdf.Rect(line["bbox"])))
    return out


def _in_band(rect: pymupdf.Rect, height: float) -> bool:
    return rect.y1 <= height * BAND or rect.y0 >= height * (1 - BAND)


def _running_lines(doc: pymupdf.Document) -> dict[str, float]:
    """Running headers and footers: normalised line -> its usual top edge. A line counts when it
    sits in the top or bottom band of at least REPEAT of the pages at the same height (so a title
    that repeats the running header's words, lower down, is kept)."""
    if doc.page_count < 3:
        return {}
    tops: dict[str, list[float]] = {}
    for page in doc:
        seen = {}
        for t, r in _lines(page):
            if _in_band(r, page.rect.height):
                seen.setdefault(_norm(t), r.y0)
        for line, y in seen.items():
            tops.setdefault(line, []).append(y)
    out = {}
    for line, ys in tops.items():
        y = sorted(ys)[len(ys) // 2]
        if line and sum(1 for v in ys if abs(v - y) <= POS_TOLERANCE_PT) >= REPEAT * doc.page_count:
            out[line] = y
    return out


def _content_box(page: pymupdf.Page, running: dict[str, float]) -> tuple[pymupdf.Rect | None, list[str]]:
    """The box around what the page paints, without full-page backgrounds and running lines."""
    area = page.rect.get_area()
    hits = [(t, r) for t, r in _lines(page)
            if _in_band(r, page.rect.height) and abs(running.get(_norm(t), -1e9) - r.y0) <= POS_TOLERANCE_PT]
    skip = [r for _, r in hits]
    removed = [t for t, _ in hits]
    box = pymupdf.Rect()
    for _kind, rect in page.get_bboxlog():
        r = pymupdf.Rect(rect) & page.rect
        if r.is_empty or r.get_area() >= 0.9 * area:  # empty, or a page background
            continue
        if any(r.intersects(s) and (r & s).get_area() >= 0.5 * r.get_area() for s in skip):
            continue
        box |= r
    if box.is_empty:
        return None, removed
    # Content may reach into a header or footer (a table's borders running to the page edge):
    # the crop stops at the running lines, so they are cut even then.
    h = page.rect.height
    for s in skip:
        if s.y0 >= h * (1 - BAND):
            box.y1 = min(box.y1, s.y0 - 1)
        else:
            box.y0 = max(box.y0, s.y1 + 1)
    return (None if box.is_empty else box), removed


def join_pages(pdf: bytes, max_height: float = MAX_HEIGHT_PT) -> JoinResult:
    src = pymupdf.open(stream=pdf, filetype="pdf")
    try:
        n = src.page_count

        def keep(reason: str) -> JoinResult:
            return JoinResult(pdf, "pages", pages=n, reason=reason)

        if n < 2:
            return keep("a single page; nothing to join")
        widths = {round(p.rect.width) for p in src}
        if len(widths) > 1:
            return keep(f"pages differ in width ({', '.join(f'{w} pt' for w in sorted(widths))})")
        if any(p.rotation for p in src):
            return keep("rotated pages")
        # A page that paints something but has no text is an image of text: a scan, or a slide.
        no_text = sum(1 for p in src if not p.get_text().strip() and _content_box(p, {})[0] is not None)
        if no_text > 0.2 * n:
            return keep(f"{no_text} of {n} pages have no text layer (a scan?)")

        running = _running_lines(src)
        crops, removed = [], []
        for page in src:
            box, lines = _content_box(page, running)
            removed += lines
            if box is not None:  # a blank page is dropped
                crops.append((page.number, pymupdf.Rect(page.rect.x0, box.y0, page.rect.x1, box.y1)))
        if not crops:
            return keep("no content found to measure")
        if any(c.height > max_height - 2 * EDGE_PT for _, c in crops):
            return keep("a page is taller than the tablet's limit")

        # Pack the crops into tall pages of at most max_height, in order.
        width = src[0].rect.width
        out_pages: list[list[tuple[int, pymupdf.Rect]]] = [[]]
        height = 2 * EDGE_PT
        for pno, clip in crops:
            add = clip.height + (GAP_PT if out_pages[-1] else 0)
            if out_pages[-1] and height + add > max_height:
                out_pages.append([])
                height = 2 * EDGE_PT
                add = clip.height
            out_pages[-1].append((pno, clip))
            height += add

        out = pymupdf.open()
        for group in out_pages:
            total = 2 * EDGE_PT + sum(c.height for _, c in group) + GAP_PT * (len(group) - 1)
            page = out.new_page(width=width, height=total)
            y = EDGE_PT
            for pno, clip in group:
                page.show_pdf_page(pymupdf.Rect(0, y, width, y + clip.height), src, pno, clip=clip)
                y += clip.height + GAP_PT
        data = out.tobytes(garbage=3, deflate=True)
        pages = out.page_count
        out.close()
        unique = list(dict.fromkeys(" ".join(t.split()) for t in removed))
        return JoinResult(data, "tall", joined=n, pages=pages, removed_lines=unique[:10])
    finally:
        src.close()
