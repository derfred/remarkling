import pymupdf
from rmscene import scene_items as si

from inkbridge.core.pens import MIN_WIDTH, draw_stroke, runs
from inkbridge.core.renderer import FixedMapping
from inkbridge.core.scene import parse_page
from rmfixtures import line, rm_page

P = si.Pen


def stroke(**kw):
    return parse_page(rm_page([line(**kw)])).strokes[0]


def ramp(n=40):
    """A horizontal stroke whose pressure rises from light to full."""
    return [(i * 10, 0, int(20 + 235 * i / (n - 1))) for i in range(n)]


def test_fineliner_is_one_uniform_run():
    (run,) = runs(stroke(points=ramp(), tool=P.FINELINER_2, width=12))
    assert run.width == 3 and run.opacity == 1 and len(run.points) == 40


def test_ballpoint_darkens_with_pressure():
    rs = runs(stroke(points=ramp(), tool=P.BALLPOINT_2, width=8))
    assert len(rs) > 1
    assert rs[0].opacity < rs[-1].opacity == 1
    # Consecutive runs share their joining point, so the stroke has no gaps.
    assert all(a.points[-1] == b.points[0] for a, b in zip(rs, rs[1:]))


def test_brush_swells_and_pencil_fades_with_pressure():
    brush = runs(stroke(points=ramp(), tool=P.PAINTBRUSH_2, width=20))
    assert brush[0].width < brush[-1].width
    pencil = runs(stroke(points=ramp(), tool=P.PENCIL_2, width=12, thickness=2.0))
    assert pencil[0].opacity < pencil[-1].opacity


def test_highlighter_is_one_translucent_run():
    (run,) = runs(stroke(points=ramp(), tool=P.HIGHLIGHTER_2, width=120))
    assert run.opacity == 0.4 and run.width == 30


def test_widths_never_vanish():
    rs = runs(stroke(points=ramp(), tool=P.MARKER_2, width=1, direction=255))
    assert min(r.width for r in rs) >= MIN_WIDTH


def test_draw_stroke_reports_its_extent():
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    shape = page.new_shape()
    top, bottom = draw_stroke(shape, stroke(points=[(0, 100), (50, 300)], width=8), FixedMapping(0.5, 100))
    shape.commit()
    assert top < 50 < 150 < bottom and page.get_drawings()
    # A dot (one point) is a filled circle.
    shape = page.new_shape()
    draw_stroke(shape, stroke(points=[(0, 10)], width=8), FixedMapping(0.5, 100))
    shape.commit()
    assert any(d["fill"] for d in page.get_drawings())
