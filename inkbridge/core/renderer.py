"""Render a whole reMarkable document, page by page, as the tablet shows it: PDF or PNG.

One output page per tablet page, in tablet order, so this covers notebooks (no PDF at all), pages
inserted into a PDF on the tablet, and pages deleted there. A page is drawn in layers:

1. the background: the original PDF page, or a blank page with its template (`templates.py`);
2. typed text (`pagetext.py`), which also says where ink anchored to the text goes;
3. highlights, then strokes, each pen with its own shape (`pens.py`).

Blank pages are the tablet's screen size (3:4, RM_PAGE_WIDTH_UNITS wide) and grow down and
sideways to fit ink or text beyond the screen, since notebook pages scroll. EPUBs render their
ink and text on blank pages, because the tablet lays the book out itself. `overlay.py` builds the
pull pipeline's annotated PDF and page image on top of this.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import pymupdf

from . import pagetext, templates
from .pens import draw_stroke
from .scene import RM_HEIGHT, RM_WIDTH, PageInk, parse_page
from .transport.base import RmDoc

log = logging.getLogger(__name__)

# PDF points per .rm unit on blank pages: the reMarkable 2 screen is 226 dpi.
PT_PER_UNIT = 72 / 226
# Room left under (or beside) the content of a page grown to fit it, in .rm units.
GROW_MARGIN = 60
# Largest PNG we render, in pixels; taller pages render narrower.
MAX_PIXELS = 24_000_000
HIGHLIGHT_OPACITY = 0.35


class PageNotFound(LookupError):
    pass


class Mapping:
    """Maps .rm units onto PDF points for one PDF page.

    "width": the PDF width spans RM width units (tall, zoom-to-width pages).
    "fit":   the page is best-fit into the RM screen (regular pages).
    "auto":  "fit" if every point lands inside the page under it, else "width".
    """

    def __init__(self, page_rect: pymupdf.Rect, ink: PageInk, mode: str = "auto", rm_width: float = RM_WIDTH):
        w, h = page_rect.width, page_rect.height
        width_scale = w / rm_width
        fit_scale = max(w / rm_width, h / (rm_width * RM_HEIGHT / RM_WIDTH))
        if mode == "auto":
            mode = "fit" if self._inside(ink, fit_scale, w, h) else "width"
        self.mode = mode
        self.scale = fit_scale if mode == "fit" else width_scale
        self.x0 = w / 2  # .rm x = 0 is the horizontal centre of the page

    @staticmethod
    def _inside(ink: PageInk, s: float, w: float, h: float) -> bool:
        xs = [x for st in ink.strokes for x, _ in st.points] + [r[0] for hl in ink.highlights for r in hl.rects]
        ys = [y for st in ink.strokes for _, y in st.points] + [r[1] for hl in ink.highlights for r in hl.rects]
        if not xs:
            return True
        tol = 0.05
        return (
            min(xs) * s + w / 2 >= -tol * w
            and max(xs) * s + w / 2 <= w * (1 + tol)
            and min(ys) * s >= -tol * h
            and max(ys) * s <= h * (1 + tol)
        )

    def pt(self, x: float, y: float) -> pymupdf.Point:
        return pymupdf.Point(x * self.scale + self.x0, y * self.scale)


class FixedMapping:
    """A fixed scale with .rm x = 0 at `x0`: the mapping of blank pages."""

    def __init__(self, scale: float, x0: float):
        self.scale, self.x0 = scale, x0

    def pt(self, x: float, y: float) -> pymupdf.Point:
        return pymupdf.Point(x * self.scale + self.x0, y * self.scale)


@dataclass
class PageInfo:
    number: int  # 1-based, in tablet order
    id: str | None  # page ID in .content; None for a PDF page the .content does not list
    pdf_page: int | None  # page of the original PDF behind it; None for a blank page
    has_ink: bool  # strokes or highlights
    has_text: bool  # typed text
    template: str | None  # the template the page names (blank pages only)
    template_drawn: bool  # whether we could redraw that template
    width: float  # output size in PDF points
    height: float

    def as_dict(self) -> dict:
        return {"number": self.number, "id": self.id, "pdf_page": self.pdf_page, "has_ink": self.has_ink,
                "has_text": self.has_text, "template": self.template, "template_drawn": self.template_drawn,
                "width": round(self.width, 2), "height": round(self.height, 2)}


@dataclass
class _Page:
    info: PageInfo
    ink: PageInk
    text: pagetext.TextLayout | None
    blank_half_width: float = 0.0  # .rm units from the centre to an edge, blank pages only


def _bounds(ink: PageInk, text: pagetext.TextLayout | None) -> tuple[float, float]:
    """(largest |x|, largest y) of everything on the page, in .rm units."""
    xs = [abs(x) for st in ink.strokes for x, _ in st.points]
    ys = [y for st in ink.strokes for _, y in st.points]
    for hl in ink.highlights:
        for x, y, w, h in hl.rects:
            xs += [abs(x), abs(x + w)]
            ys.append(y + h)
    if text is not None:
        xs += [abs(text.x), abs(text.x + text.width)]
        ys.append(text.bottom)
    return max(xs, default=0.0), max(ys, default=0.0)


def _anchor(ink: PageInk, text: pagetext.TextLayout | None) -> None:
    """Move strokes anchored to the typed text down to their character's line."""
    if text is None:
        return
    ink.strokes = [s.moved(text.anchors[s.anchor]) if s.anchor in text.anchors else s for s in ink.strokes]


class Renderer:
    """Renders an RmDoc. `mode` and `rm_width` place ink on PDF pages as in `Mapping`."""

    def __init__(self, doc: RmDoc, rm_width: float = RM_WIDTH, mode: str = "auto"):
        self.doc, self.rm_width, self.mode = doc, rm_width, mode
        self._pdf_bytes = doc.pdf
        self._pdf_sizes: list[tuple[float, float]] = []
        if self._pdf_bytes:
            with pymupdf.open(stream=self._pdf_bytes, filetype="pdf") as pdf:
                self._pdf_sizes = [(p.rect.width, p.rect.height) for p in pdf]
        self._templates = templates.names(doc)
        self._pages = self._layout()

    # --- layout ------------------------------------------------------------------------------

    def _page_order(self) -> list[tuple[str | None, int | None]]:
        """(page ID, PDF page or None) per tablet page, in tablet order."""
        content, n_pdf = self.doc.content, len(self._pdf_sizes)

        def pdf_page(i: int | None) -> int | None:
            return i if i is not None and 0 <= i < n_pdf else None

        cpages = (content.get("cPages") or {}).get("pages")
        if cpages:
            live = [p for p in cpages if not p.get("deleted")]
            if any("redir" in p for p in live):
                # An original PDF page carries redir (its PDF page); a page added on the tablet does not.
                return [(p["id"], pdf_page((p.get("redir") or {}).get("value"))) for p in live]
            # No redirections at all: pages follow the PDF one to one.
            return [(p["id"], pdf_page(i)) for i, p in enumerate(live)]
        pages = content.get("pages")
        if pages:
            redirect = content.get("redirectionPageMap")  # older format: -1 marks an added page
            if isinstance(redirect, list) and len(redirect) == len(pages):
                return [(pid, pdf_page(r)) for pid, r in zip(pages, redirect)]
            return [(pid, pdf_page(i)) for i, pid in enumerate(pages)]
        # Never opened on the tablet: just the PDF.
        return [(None, i) for i in range(n_pdf)]

    def _layout(self) -> list[_Page]:
        out = []
        screen_h = self.rm_width * RM_HEIGHT / RM_WIDTH
        for number, (page_id, pdf_page) in enumerate(self._page_order(), start=1):
            ink = PageInk(pdf_page or 0)
            data = self.doc.rm_file(page_id) if page_id else None
            if data:
                try:
                    ink = parse_page(data, pdf_page or 0)
                except Exception:  # a broken page should not lose the rest
                    log.exception("could not parse page %s of %s", page_id, self.doc.uuid)
            text = pagetext.layout(ink.text) if ink.text is not None else None
            _anchor(ink, text)
            if pdf_page is not None:
                w, h = self._pdf_sizes[pdf_page]
                info = PageInfo(number, page_id, pdf_page, ink.has_marks, text is not None, None, False, w, h)
                out.append(_Page(info, ink, text))
                continue
            template = self._templates.get(page_id) if page_id else None
            max_x, max_y = _bounds(ink, text)
            half = max(self.rm_width / 2, max_x + GROW_MARGIN if max_x > self.rm_width / 2 else 0)
            height = max(screen_h, max_y + GROW_MARGIN if max_y > screen_h else 0)
            info = PageInfo(number, page_id, None, ink.has_marks, text is not None, template,
                            templates.pattern(template) is not None,
                            2 * half * PT_PER_UNIT, height * PT_PER_UNIT)
            out.append(_Page(info, ink, text, half))
        return out

    @property
    def pages(self) -> list[PageInfo]:
        return [p.info for p in self._pages]

    def highlights(self) -> list[dict]:
        return [{"text": h.text, "color": h.color} for p in self._pages for h in p.ink.highlights if h.text.strip()]

    def pen_colors(self) -> list[str]:
        return sorted({s.color_name for p in self._pages for s in p.ink.strokes if not s.highlighter})

    def text(self) -> list[str]:
        """Each page's typed text ("" where there is none)."""
        return [p.text.plain if p.text else "" for p in self._pages]

    # --- rendering ---------------------------------------------------------------------------

    def _draw_page(self, out: pymupdf.Document, src: pymupdf.Document | None, p: _Page,
                   ink: bool) -> list[tuple[float, float]]:
        if p.info.pdf_page is not None:
            out.insert_pdf(src, from_page=p.info.pdf_page, to_page=p.info.pdf_page)
            page = out[-1]
            mapping = Mapping(page.rect, p.ink, self.mode, self.rm_width)
        else:
            page = out.new_page(width=p.info.width, height=p.info.height)
            mapping = FixedMapping(PT_PER_UNIT, p.blank_half_width * PT_PER_UNIT)
            templates.draw(page, p.info.template, PT_PER_UNIT)
        extents = []
        if p.text is not None:
            extents.append(pagetext.draw(page, p.text, mapping))
        if not ink or not p.ink.has_marks:
            return extents
        shape = page.new_shape()
        for hl in p.ink.highlights:
            for x, y, w, h in hl.rects:
                r = pymupdf.Rect(mapping.pt(x, y), mapping.pt(x + w, y + h))
                shape.draw_rect(r)
                shape.finish(color=None, fill=hl.rgb, fill_opacity=HIGHLIGHT_OPACITY)
                extents.append((r.y0, r.y1))
        for st in p.ink.strokes:
            extents.append(draw_stroke(shape, st, mapping))
        shape.commit(overlay=True)
        return extents

    def render(self, ink: bool = True, numbers: list[int] | None = None
               ) -> tuple[pymupdf.Document, list[list[tuple[float, float]]]]:
        """The rendered pages (all, or the 1-based `numbers`) and, per page, the vertical extents
        (points) of what was drawn on it. The caller closes the document."""
        pages = self._pages if numbers is None else [self._pages[n - 1] for n in numbers]
        out = pymupdf.open()
        src = pymupdf.open(stream=self._pdf_bytes, filetype="pdf") if self._pdf_bytes else None
        try:
            extents = [self._draw_page(out, src, p, ink) for p in pages]
        except Exception:
            out.close()
            raise
        finally:
            if src is not None:
                src.close()
        return out, extents

    def pdf(self, ink: bool = True) -> bytes:
        """Every page as one PDF; ink=False leaves out strokes and highlights (text stays)."""
        if not self._pages:
            raise PageNotFound(f"{self.doc.uuid} has no pages")
        out, _ = self.render(ink)
        with out:
            return out.tobytes(garbage=3, deflate=True)

    def png(self, number: int, width: int | None = None) -> bytes:
        """One page (1-based) as a PNG, `width` pixels wide (default: 2 px per point)."""
        if not 1 <= number <= len(self._pages):
            raise PageNotFound(f"{self.doc.uuid} has no page {number}")
        info = self._pages[number - 1].info
        scale = width / info.width if width else 2.0
        # Pixel sizes round up, so leave a little room under the cap.
        scale = min(scale, math.sqrt(MAX_PIXELS / ((info.width + 1) * (info.height + 1))) * 0.99)
        out, _ = self.render(numbers=[number])
        with out:
            return out[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
