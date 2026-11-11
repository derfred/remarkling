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


def test_overlay_renders_notebooks_with_typed_text():
    from rmfixtures import notebook, page_text

    text = page_text([(si.ParagraphStyle.PLAIN, "Ideas for the beta")])
    doc = notebook([rm_page([line([(-200, 1000), (200, 1010)])], text=text), rm_page([])])
    result = overlay(doc)
    assert len(result.page_bands) == 2
    # Bands cover the typed text near the top and the stroke further down, nothing on page 2.
    assert len(result.ink_bands) == 2 and result.ink_bands[-1][1] <= result.page_bands[0][1]
    text_pdf = pymupdf.open(stream=result.text_pdf, filetype="pdf")
    assert text_pdf.page_count == 2 and "Ideas for the beta" in text_pdf[0].get_text()
    assert not text_pdf[0].get_drawings()  # no ink in the text PDF


def test_overlay_follows_pages_inserted_on_the_tablet():
    import json
    from uuid import uuid4

    from inkbridge.core.interpret import slice_strips

    pdf = pymupdf.open()
    for i in range(2):
        pdf.new_page(width=500, height=700).insert_text((50, 100), f"Original page {i + 1}")
    doc = rmdoc(pdf.tobytes(), [None, rm_page([line([(0, 300), (100, 310)])])])
    content = doc.content
    content["cPages"]["pages"].insert(0, {"id": str(uuid4())})  # a blank page added before page 1
    doc.files[f"{doc.uuid}.content"] = json.dumps(content).encode()
    result = overlay(doc)
    assert len(result.page_bands) == 3
    (strip,) = slice_strips(result, doc.pdf)
    # Context comes from the rendered page the ink is on, not PDF page 3 (which does not exist).
    assert "Original page 2" in strip.context
