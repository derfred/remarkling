"""The pull pipeline's view of a returned document: annotated PDF, one stacked page image, the
vertical bands that hold ink (so only those strips go to Claude), highlights and pen colours.

Rendering itself is `renderer.py`: pages come out in tablet order with each pen's own stroke shape,
typed text and templates, and notebooks work as well as PDFs. `text_pdf` is the same pages without
strokes or highlights; it lines up with `page_bands`, so it is where strip context text comes from.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pymupdf
from PIL import Image

from .renderer import Mapping, Renderer  # noqa: F401  (Mapping: re-exported for callers and tests)
from .scene import COLOR_NAMES, COLORS, RM_HEIGHT, RM_WIDTH, Highlight, PageInk, Stroke, parse_page  # noqa: F401
from .transport.base import RmDoc

PNG_SCALE = 2.0


@dataclass
class OverlayResult:
    annotated_pdf: bytes
    png: bytes  # all pages stacked, PNG_SCALE x
    png_size: tuple[int, int]
    # Per page: (top offset in the PNG, height in the PNG), both in PNG pixels.
    page_bands: list[tuple[int, int]]
    # Vertical PNG-pixel ranges that contain ink (or typed text), merged.
    ink_bands: list[tuple[int, int]]
    highlights: list[dict]
    pen_colors: list[str]
    text_pdf: bytes | None = None  # the pages without ink, aligned with page_bands

    def page_at(self, y: int) -> int:
        for i, (top, h) in enumerate(self.page_bands):
            if top <= y < top + h:
                return i
        return len(self.page_bands) - 1


def _merge(bands: list[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(bands):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def overlay(doc: RmDoc, mode: str = "auto", rm_width: float = RM_WIDTH) -> OverlayResult:
    renderer = Renderer(doc, rm_width, mode)
    if not renderer.pages:
        raise ValueError(f"{doc.uuid} has no pages")
    pdf, extents = renderer.render(ink=True)
    try:
        annotated = pdf.tobytes(garbage=3, deflate=True)
        images, bands, ink_bands, top = [], [], [], 0
        matrix = pymupdf.Matrix(PNG_SCALE, PNG_SCALE)
        for page, page_extents in zip(pdf, extents):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            bands.append((top, pix.height))
            for y0, y1 in page_extents:
                a, b = top + max(int(y0 * PNG_SCALE), 0), top + min(int(y1 * PNG_SCALE) + 1, pix.height)
                if a < b:
                    ink_bands.append((a, b))
            top += pix.height
    finally:
        pdf.close()
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
        highlights=renderer.highlights(),
        pen_colors=renderer.pen_colors(),
        text_pdf=renderer.pdf(ink=False),
    )


def page_text(pdf_bytes: bytes, page_index: int, y0_pt: float, y1_pt: float) -> str:
    """Text of a PDF within a vertical band of one page."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if page_index >= pdf.page_count:
            return ""
        page = pdf[page_index]
        clip = pymupdf.Rect(0, max(y0_pt, 0), page.rect.width, min(y1_pt, page.rect.height))
        return page.get_text("text", clip=clip).strip()
