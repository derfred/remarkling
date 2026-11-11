"""Page templates (the background of notebook pages), approximated from the template's name.

The tablet draws templates from image files that live on the device, not in the cloud. A page
names its template ("P Lines medium", "P Grid small", "P Dots S", ...) in .content (`template` of
each cPages entry) or, in older documents, one name per line in .pagedata. Lined, grid and dotted
templates are redrawn here in light grey at the tablet's spacings; any other template (planners,
music, isometric, ...) is left blank and reported by name.
"""

from __future__ import annotations

import re

import pymupdf

from .transport.base import RmDoc

# Spacing in .rm units (226 dpi): about 6, 8 and 10 mm.
SPACING = {"small": 53, "medium": 71, "large": 89}
COLOR = (0.78, 0.78, 0.8)
LINE_WIDTH = 1.5  # .rm units
DOT_RADIUS = 2.5
TOP_MARGIN_LINES = 2  # lined pages start a couple of lines down, like the tablet's

KINDS = (
    ("dots", re.compile(r"dot", re.I)),
    ("grid", re.compile(r"grid|squared|square|graph", re.I)),
    ("lines", re.compile(r"line|ruled|margin|checklist", re.I)),
)


def names(doc: RmDoc) -> dict[str, str]:
    """Page ID -> template name, for pages that name one."""
    content = doc.content
    out = {}
    for p in (content.get("cPages") or {}).get("pages") or []:
        name = (p.get("template") or {}).get("value")
        if name:
            out[p["id"]] = name
    if out:
        return out
    pagedata = doc.files.get(f"{doc.uuid}.pagedata")
    if pagedata:
        lines = pagedata.decode(errors="replace").splitlines()
        for pid, name in zip(content.get("pages") or [], lines):
            if name.strip():
                out[pid] = name.strip()
    return out


def pattern(name: str | None) -> tuple[str, float] | None:
    """(kind, spacing in .rm units) for a template we can redraw; None for blank or unknown."""
    if not name or name.strip().lower() == "blank":
        return None
    kind = next((k for k, rx in KINDS if rx.search(name)), None)
    if kind is None:
        return None
    lowered = f" {name.lower()} "
    size = ("small" if re.search(r"small|\bs\b|_s\b", lowered)
            else "large" if re.search(r"large|\bl\b|_l\b", lowered) else "medium")
    return kind, SPACING[size]


def draw(page: pymupdf.Page, name: str | None, scale: float) -> bool:
    """Draw the template onto a blank page; True if it was one we can redraw."""
    found = pattern(name)
    if found is None:
        return False
    kind, spacing = found
    step = spacing * scale
    w, h = page.rect.width, page.rect.height
    shape = page.new_shape()
    if kind == "lines":
        y = step * (TOP_MARGIN_LINES + 1)
        while y < h:
            shape.draw_line((0, y), (w, y))
            y += step
        shape.finish(width=LINE_WIDTH * scale, color=COLOR)
    else:
        # Centred horizontally on the page, like the tablet's grids.
        x0 = (w / 2) % step
        xs = [x0 + i * step for i in range(int((w - x0) / step) + 1)]
        ys = [step * i for i in range(1, int(h / step) + 1)]
        if kind == "grid":
            for x in xs:
                shape.draw_line((x, 0), (x, h))
            for y in ys:
                shape.draw_line((0, y), (w, y))
            shape.finish(width=LINE_WIDTH * scale, color=COLOR)
        else:
            for x in xs:
                for y in ys:
                    shape.draw_circle((x, y), DOT_RADIUS * scale)
            shape.finish(color=None, fill=COLOR)
    shape.commit(overlay=False)
    return True
