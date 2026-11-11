"""Render reMarkable strokes (.rm v6 via rmscene) over the original PDF; extract highlights."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import pymupdf
from PIL import Image
from rmscene import read_tree
from rmscene import scene_items as si

from .transport.base import RmDoc

log = logging.getLogger(__name__)

# Screen size in .rm units; strokes live in this frame with x = 0 at the page centre.
RM_WIDTH = 1404
RM_HEIGHT = 1872
PNG_SCALE = 2.0

COLORS: dict[int, tuple[float, float, float]] = {
    si.PenColor.BLACK: (0, 0, 0),
    si.PenColor.GRAY: (0.5, 0.5, 0.5),
    si.PenColor.WHITE: (1, 1, 1),
    si.PenColor.YELLOW: (1.0, 0.86, 0.1),
    si.PenColor.GREEN: (0.3, 0.75, 0.3),
    si.PenColor.PINK: (1.0, 0.45, 0.7),
    si.PenColor.BLUE: (0.15, 0.35, 0.9),
    si.PenColor.RED: (0.85, 0.1, 0.1),
    si.PenColor.GRAY_OVERLAP: (0.6, 0.6, 0.6),
    si.PenColor.HIGHLIGHT: (1.0, 0.86, 0.1),
    si.PenColor.GREEN_2: (0.55, 0.85, 0.3),
    si.PenColor.CYAN: (0.2, 0.8, 0.9),
    si.PenColor.MAGENTA: (0.85, 0.2, 0.75),
    si.PenColor.YELLOW_2: (1.0, 0.9, 0.3),
}

COLOR_NAMES: dict[int, str] = {
    si.PenColor.BLACK: "black", si.PenColor.GRAY: "gray", si.PenColor.WHITE: "white",
    si.PenColor.YELLOW: "yellow", si.PenColor.GREEN: "green", si.PenColor.PINK: "pink",
    si.PenColor.BLUE: "blue", si.PenColor.RED: "red", si.PenColor.GRAY_OVERLAP: "gray",
    si.PenColor.HIGHLIGHT: "yellow", si.PenColor.GREEN_2: "green", si.PenColor.CYAN: "cyan",
    si.PenColor.MAGENTA: "magenta", si.PenColor.YELLOW_2: "yellow",
}

ERASERS = {si.Pen.ERASER, si.Pen.ERASER_AREA}


@dataclass
class Stroke:
    points: list[tuple[float, float]]  # .rm units, x relative to page centre
    width: float  # .rm units
    color: tuple[float, float, float]
    color_name: str
    highlighter: bool


@dataclass
class Highlight:
    text: str
    color: str
    rects: list[tuple[float, float, float, float]]  # .rm units: x, y, w, h


@dataclass
class PageInk:
    pdf_page: int
    strokes: list[Stroke] = field(default_factory=list)
    highlights: list[Highlight] = field(default_factory=list)


@dataclass
class OverlayResult:
    annotated_pdf: bytes
    png: bytes  # all pages stacked, PNG_SCALE x
    png_size: tuple[int, int]
    # Per PDF page: (top offset in the PNG, height in the PNG), both in PNG pixels.
    page_bands: list[tuple[int, int]]
    # Vertical PNG-pixel ranges that contain ink, merged.
    ink_bands: list[tuple[int, int]]
    highlights: list[dict]
    pen_colors: list[str]

    def page_at(self, y: int) -> int:
        for i, (top, h) in enumerate(self.page_bands):
            if top <= y < top + h:
                return i
        return len(self.page_bands) - 1


def _rgba_to_color(rgba: tuple[int, int, int, int] | None, fallback: int) -> tuple[float, float, float]:
    if rgba:
        r, g, b, _ = rgba
        return (r / 255, g / 255, b / 255)
    return COLORS.get(fallback, (0, 0, 0))


def _walk(item, dx: float = 0.0):
    """Yield (leaf item, x offset) honouring group anchor offsets."""
    if isinstance(item, si.Group):
        ox = dx + (item.anchor_origin_x.value if item.anchor_origin_x is not None else 0.0)
        for child in item.children.values():
            yield from _walk(child, ox)
    else:
        yield item, dx


def parse_page(rm_bytes: bytes, pdf_page: int) -> PageInk:
    ink = PageInk(pdf_page)
    tree = read_tree(io.BytesIO(rm_bytes))
    for item, dx in _walk(tree.root):
        if isinstance(item, si.Line):
            if item.tool in ERASERS or not item.points:
                continue
            avg_w = sum(p.width for p in item.points) / len(item.points)
            highlighter = si.Pen.is_highlighter(item.tool)
            ink.strokes.append(
                Stroke(
                    points=[(p.x + dx, p.y) for p in item.points],
                    width=max(avg_w / 4 * (item.thickness_scale or 1), 1.5),
                    color=_rgba_to_color(item.color_rgba, item.color),
                    color_name=COLOR_NAMES.get(item.color, "black"),
                    highlighter=highlighter,
                )
            )
        elif isinstance(item, si.GlyphRange):
            ink.highlights.append(
                Highlight(
                    text=item.text or "",
                    color=COLOR_NAMES.get(item.color, "yellow"),
                    rects=[(r.x + dx, r.y, r.w, r.h) for r in item.rectangles],
                )
            )
    return ink


def read_ink(doc: RmDoc) -> list[PageInk]:
    pages = []
    index = doc.pdf_page_index()
    for page_id in doc.page_ids():
        data = doc.rm_file(page_id)
        if not data:
            continue
        try:
            pages.append(parse_page(data, index.get(page_id, 0)))
        except Exception:  # a broken page should not lose the rest
            log.exception("could not parse page %s of %s", page_id, doc.uuid)
    return pages


class Mapping:
    """Maps .rm units onto PDF points for one page.

    "width": the PDF width spans RM width units (tall, zoom-to-width pages).
    "fit":   the page is best-fit into the RM screen (regular pages).
    "auto":  "fit" if every point lands inside the page under it, else "width".
    """

    def __init__(self, page_rect: pymupdf.Rect, ink: PageInk, mode: str = "auto", rm_width: float = RM_WIDTH):
        w, h = page_rect.width, page_rect.height
        width_scale = w / rm_width
        fit_scale = max(w / rm_width, h / RM_HEIGHT)
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


def draw(page: pymupdf.Page, ink: PageInk, mapping: Mapping) -> list[tuple[float, float]]:
    """Draw ink onto `page`; return vertical extents (PDF points) of everything drawn."""
    extents = []
    for hl in ink.highlights:
        for x, y, w, h in hl.rects:
            r = pymupdf.Rect(mapping.pt(x, y), mapping.pt(x + w, y + h))
            page.draw_rect(r, color=None, fill=COLORS[si.PenColor.YELLOW], fill_opacity=0.35, overlay=True)
            extents.append((r.y0, r.y1))
    for st in ink.strokes:
        pts = [mapping.pt(x, y) for x, y in st.points]
        width = st.width * mapping.scale
        if len(pts) == 1:
            page.draw_circle(pts[0], width / 2, color=None, fill=st.color, overlay=True)
        else:
            page.draw_polyline(
                pts, color=st.color, width=width, stroke_opacity=0.4 if st.highlighter else 1.0,
                lineCap=1, lineJoin=1, overlay=True,
            )
        ys = [p.y for p in pts]
        extents.append((min(ys) - width, max(ys) + width))
    return extents


def _merge(bands: list[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(bands):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def overlay(doc: RmDoc, mode: str = "auto", rm_width: float = RM_WIDTH) -> OverlayResult:
    if not doc.pdf:
        raise ValueError(f"{doc.uuid} has no PDF; only PDF documents are supported")
    pdf = pymupdf.open(stream=doc.pdf, filetype="pdf")
    try:
        inks = read_ink(doc)
        page_extents: dict[int, list[tuple[float, float]]] = {}
        highlights, pen_colors = [], set()
        for ink in inks:
            if ink.pdf_page >= pdf.page_count:
                continue
            page = pdf[ink.pdf_page]
            mapping = Mapping(page.rect, ink, mode, rm_width)
            page_extents.setdefault(ink.pdf_page, []).extend(draw(page, ink, mapping))
            highlights += [{"text": h.text, "color": h.color} for h in ink.highlights if h.text.strip()]
            pen_colors |= {s.color_name for s in ink.strokes if not s.highlighter}
        annotated = pdf.tobytes(garbage=3, deflate=True)

        images, bands, ink_bands, top = [], [], [], 0
        matrix = pymupdf.Matrix(PNG_SCALE, PNG_SCALE)
        for i, page in enumerate(pdf):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            bands.append((top, pix.height))
            for y0, y1 in page_extents.get(i, []):
                ink_bands.append((top + max(int(y0 * PNG_SCALE), 0), top + min(int(y1 * PNG_SCALE) + 1, pix.height)))
            top += pix.height
        width = max(im.width for im in images)
        stacked = Image.new("RGB", (width, top), "white")
        for im, (y, _) in zip(images, bands):
            stacked.paste(im, (0, y))
        buf = io.BytesIO()
        stacked.save(buf, "PNG", optimize=True)
        return OverlayResult(
            annotated_pdf=annotated,
            png=buf.getvalue(),
            png_size=stacked.size,
            page_bands=bands,
            ink_bands=_merge(ink_bands),
            highlights=highlights,
            pen_colors=sorted(pen_colors),
        )
    finally:
        pdf.close()


def page_text(pdf_bytes: bytes, page_index: int, y0_pt: float, y1_pt: float) -> str:
    """Text of the original PDF within a vertical band of one page."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if page_index >= pdf.page_count:
            return ""
        page = pdf[page_index]
        clip = pymupdf.Rect(0, max(y0_pt, 0), page.rect.width, min(y1_pt, page.rect.height))
        return page.get_text("text", clip=clip).strip()
