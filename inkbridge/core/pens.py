"""How each reMarkable pen draws: stroke width and opacity per segment, from pressure, speed, tilt.

The tablet records per point: width (already shaped by the device, x 4), pressure (0-255), speed
(x 4) and direction (tilt, 0-255 for a full turn). The models below follow the ones rmc
(github.com/ricklupton/rmc) worked out from device output: a fineliner is uniform, a ballpoint
and pencil fade with speed and lighter pressure, a brush and calligraphy pen swell with pressure
and tilt. Pencil grain is approximated by opacity. A stroke is drawn as runs of segments that
share a (quantised) width and opacity, so a uniform pen costs one path and a brush stroke a few.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pymupdf
from rmscene import scene_items as si

from .scene import Sample, Stroke

P = si.Pen
FINELINER = {P.FINELINER_1, P.FINELINER_2}
BALLPOINT = {P.BALLPOINT_1, P.BALLPOINT_2}
MARKER = {P.MARKER_1, P.MARKER_2}
PENCIL = {P.PENCIL_1, P.PENCIL_2}
MECHANICAL_PENCIL = {P.MECHANICAL_PENCIL_1, P.MECHANICAL_PENCIL_2}
BRUSH = {P.PAINTBRUSH_1, P.PAINTBRUSH_2}
CALLIGRAPHY = {P.CALIGRAPHY}
HIGHLIGHTER = {P.HIGHLIGHTER_1, P.HIGHLIGHTER_2}
SHADER = {P.SHADER}

MIN_WIDTH = 0.8  # .rm units; thinner strokes would vanish when rendered small
WIDTH_STEP = 0.25  # quantisation, so neighbouring segments share a path
OPACITY_STEP = 0.05


@dataclass
class Run:
    points: list[tuple[float, float]]  # .rm units
    width: float  # .rm units
    opacity: float


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _segment(tool: int, s: Sample, thickness: float, last_width: float) -> tuple[float, float]:
    """(width in .rm units, opacity) of the segment ending at sample `s`."""
    w = s.width / 4
    pr = s.pressure / 255
    speed = s.speed / 4
    tilt = s.direction * 2 * math.pi / 255
    if tool in BALLPOINT:
        width = 0.5 + pr + w - 0.5 * (speed / 50)
        opacity = _clamp(0.5 + 1.2 * pr - 0.1 * (speed / 35), 0.35)
    elif tool in MARKER:
        width = 0.9 * w - 0.4 * tilt + 0.1 * last_width
        opacity = 1.0
    elif tool in PENCIL:
        width = 0.7 * ((0.8 * thickness + 0.5 * pr) * w - 0.25 * tilt ** 1.8 - 0.6 * (speed / 50))
        width = min(width, thickness * 10)
        opacity = _clamp(pr - 0.1 * (speed / 35) - 0.1, 0.15)
    elif tool in MECHANICAL_PENCIL:
        width, opacity = w, 0.7
    elif tool in BRUSH:
        width = 0.7 * ((1 + 1.4 * pr) * w - 0.5 * tilt - speed / 50)
        opacity = _clamp(pr ** 1.5 - 0.2 * (speed / 50), 0.2)
    elif tool in CALLIGRAPHY:
        width = 0.9 * ((1 + pr) * w - 0.3 * tilt) + 0.1 * last_width
        opacity = 1.0
    else:  # fineliner and anything unknown: as recorded
        width, opacity = w, 1.0
    return max(width, MIN_WIDTH, 0.25 * w), opacity


def _q(v: float, step: float) -> float:
    return round(v / step) * step


def runs(stroke: Stroke) -> list[Run]:
    """The stroke as runs of segments with one width and opacity each."""
    pts = stroke.points
    if stroke.tool in HIGHLIGHTER or stroke.tool in SHADER:
        # Translucent: one path, so overlapping segments do not darken.
        return [Run(pts, max(stroke.width, MIN_WIDTH), 0.4 if stroke.tool in HIGHLIGHTER else 0.25)]
    if stroke.tool in FINELINER or len(pts) < 2:
        _, opacity = _segment(stroke.tool, stroke.samples[0], stroke.thickness, 0)
        return [Run(pts, max(stroke.width, MIN_WIDTH), opacity)]
    out: list[Run] = []
    last = stroke.samples[0].width / 4
    for i in range(1, len(pts)):
        width, opacity = _segment(stroke.tool, stroke.samples[i], stroke.thickness, last)
        last = width
        width, opacity = max(_q(width, WIDTH_STEP), MIN_WIDTH), _q(opacity, OPACITY_STEP)
        if out and out[-1].width == width and out[-1].opacity == opacity:
            out[-1].points.append(pts[i])
        else:
            out.append(Run([pts[i - 1], pts[i]], width, opacity))
    return out


def draw_stroke(shape: pymupdf.Shape, stroke: Stroke, mapping) -> tuple[float, float]:
    """Add the stroke to `shape` (commit it afterwards); return its vertical extent in points."""
    translucent = stroke.tool in HIGHLIGHTER or stroke.tool in SHADER
    top, bottom = math.inf, -math.inf
    for run in runs(stroke):
        pts = [mapping.pt(x, y) for x, y in run.points]
        width = run.width * mapping.scale
        if len(pts) == 1 or all(p == pts[0] for p in pts):
            shape.draw_circle(pts[0], width / 2)
            shape.finish(color=None, fill=stroke.color, fill_opacity=run.opacity)
        else:
            shape.draw_polyline(pts)
            # Round ends on opaque runs. Translucent ones (highlighters, and pencil or brush
            # where they fade) get flat ends: round caps would overlap the neighbouring run and
            # show as beads.
            flat = translucent or run.opacity < 1
            shape.finish(width=width, color=stroke.color, stroke_opacity=run.opacity,
                         lineCap=0 if flat else 1, lineJoin=1, closePath=False)
        ys = [p.y for p in pts]
        top, bottom = min(top, min(ys) - width), max(bottom, max(ys) + width)
    return top, bottom
