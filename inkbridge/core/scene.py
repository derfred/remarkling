"""Parse one .rm (v6) page with rmscene into what the renderer draws: strokes, highlights, text.

Coordinates stay in .rm units, x = 0 at the horizontal centre of the page. Groups offset their
children by `anchor_origin_x`; a group anchored to a character of the page's typed text also
moves down with that character's line, which only the text layout knows, so strokes keep their
anchor and `pagetext` resolves it. Hidden layers are dropped. Eraser strokes are dropped too: on
v6 pages the tablet has already removed what they erased from the strokes underneath.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field, replace

from rmscene import read_tree
from rmscene import scene_items as si
from rmscene.tagged_block_common import CrdtId

# Screen size in .rm units on a reMarkable 2 (RM_PAGE_WIDTH_UNITS overrides the width; 3:4).
RM_WIDTH = 1404
RM_HEIGHT = 1872

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
# Anchor IDs the tablet uses for "top of page" and "bottom of page"; they move nothing.
SPECIAL_ANCHORS = {CrdtId(0, 0xFFFFFFFFFFFE), CrdtId(0, 0xFFFFFFFFFFFF)}


@dataclass
class Sample:
    """One point of a stroke as the pen recorded it (rmscene's units: width and speed x 4)."""

    x: float
    y: float
    speed: float
    direction: float
    width: float
    pressure: float


@dataclass
class Stroke:
    samples: list[Sample]
    tool: int
    thickness: float  # the pen's thickness setting (thickness_scale)
    color: tuple[float, float, float]
    color_name: str
    highlighter: bool
    anchor: CrdtId | None = None  # text character the stroke's group hangs from

    @property
    def points(self) -> list[tuple[float, float]]:
        return [(s.x, s.y) for s in self.samples]

    @property
    def width(self) -> float:
        """Average width in .rm units."""
        return sum(s.width for s in self.samples) / len(self.samples) / 4

    def moved(self, dy: float) -> "Stroke":
        return replace(self, samples=[replace(s, y=s.y + dy) for s in self.samples], anchor=None)


@dataclass
class Highlight:
    text: str
    color: str
    rgb: tuple[float, float, float]
    rects: list[tuple[float, float, float, float]]  # .rm units: x, y, w, h


@dataclass
class PageInk:
    pdf_page: int
    strokes: list[Stroke] = field(default_factory=list)
    highlights: list[Highlight] = field(default_factory=list)
    text: si.Text | None = None  # the page's typed text, if any

    @property
    def has_marks(self) -> bool:
        return bool(self.strokes or self.highlights)


def _rgb(rgba: tuple[int, int, int, int] | None, fallback: int) -> tuple[float, float, float]:
    if rgba:
        r, g, b, _ = rgba
        return (r / 255, g / 255, b / 255)
    return COLORS.get(fallback, (0, 0, 0))


def _visible(group: si.Group) -> bool:
    return group.visible is None or group.visible.value is not False


def _walk(item, dx: float = 0.0, anchor: CrdtId | None = None):
    """Yield (leaf item, x offset, anchor) for visible items, honouring group anchors."""
    if isinstance(item, si.Group):
        if not _visible(item):
            return
        ox = dx + (item.anchor_origin_x.value if item.anchor_origin_x is not None else 0.0)
        if item.anchor_id is not None and item.anchor_id.value not in SPECIAL_ANCHORS:
            anchor = item.anchor_id.value
        for child in item.children.values():
            yield from _walk(child, ox, anchor)
    else:
        yield item, dx, anchor


def parse_page(rm_bytes: bytes, pdf_page: int = 0) -> PageInk:
    ink = PageInk(pdf_page)
    tree = read_tree(io.BytesIO(rm_bytes))
    text = tree.root_text
    if text is not None and any(isinstance(v, str) and v.strip() for v in text.items.values()):
        ink.text = text
    for item, dx, anchor in _walk(tree.root):
        if isinstance(item, si.Line):
            if item.tool in ERASERS or not item.points:
                continue
            ink.strokes.append(Stroke(
                samples=[Sample(p.x + dx, p.y, p.speed, p.direction, p.width, p.pressure) for p in item.points],
                tool=int(item.tool),
                thickness=item.thickness_scale or 1.0,
                color=_rgb(item.color_rgba, item.color),
                color_name=COLOR_NAMES.get(item.color, "black"),
                highlighter=si.Pen.is_highlighter(item.tool),
                anchor=anchor if ink.text is not None else None,
            ))
        elif isinstance(item, si.GlyphRange):
            ink.highlights.append(Highlight(
                text=item.text or "",
                color=COLOR_NAMES.get(item.color, "yellow"),
                rgb=_rgb(getattr(item, "color_rgba", None), item.color if item.color != si.PenColor.HIGHLIGHT
                         else si.PenColor.YELLOW),
                rects=[(r.x + dx, r.y, r.w, r.h) for r in item.rectangles],
            ))
    return ink
