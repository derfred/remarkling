import io

import pymupdf
from PIL import Image
from rmscene import scene_items as si

from inkbridge.core.overlay import PNG_SCALE, Mapping, overlay, parse_page
from rmfixtures import line, rm_page, rmdoc, sample_pdf


def test_parse_page_reads_lines_and_highlights():
    data = rm_page([line([(0, 100), (50, 120)], color=si.PenColor.RED)], [("blast radius", (-300, 130, 200, 20))])
    ink = parse_page(data, 0)
    assert len(ink.strokes) == 1 and ink.strokes[0].color_name == "red"
    assert ink.highlights[0].text == "blast radius"


def test_overlay_draws_strokes_and_reports_ink_bands():
    pdf = sample_pdf(height_pt=3000)
    # Width mode: the 510 pt page spans 1404 units, so y=1000 units ~ 363 pt.
    page = rm_page([line([(-200, 1000), (200, 1010), (300, 1100)])], [("blast radius", (-500, 380, 300, 30))])
    doc = rmdoc(pdf, [page])
    result = overlay(doc)

    out = pymupdf.open(stream=result.annotated_pdf, filetype="pdf")
    assert len(out[0].get_drawings()) >= 2
    img = Image.open(io.BytesIO(result.png))
    assert img.size == (int(510 * PNG_SCALE), int(3000 * PNG_SCALE))
    assert result.ink_bands, "ink bands expected"
    scale = 510 / 1404 * PNG_SCALE
    top, bottom = result.ink_bands[-1]
    assert top <= 1000 * scale <= bottom
    assert result.highlights == [{"text": "blast radius", "color": "yellow"}]
    # Pixels under the stroke are darker than the blank page.
    px = img.getpixel((int(510 / 2 * PNG_SCALE), int(1005 * scale)))
    assert sum(px) < 600


def test_mapping_auto_picks_fit_for_regular_pages_and_width_for_tall():
    regular = pymupdf.Rect(0, 0, 595, 842)
    ink = parse_page(rm_page([line([(-600, 100), (600, 1800)])]), 0)
    assert Mapping(regular, ink).mode == "fit"
    tall = pymupdf.Rect(0, 0, 510, 12000)
    assert Mapping(tall, ink).mode == "width"


def test_pages_without_rm_files_are_ignored():
    result = overlay(rmdoc(sample_pdf(), [None]))
    assert result.ink_bands == [] and result.highlights == []
