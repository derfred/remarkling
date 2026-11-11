import io
import json
from uuid import uuid4

import pymupdf
import pytest
from PIL import Image

from inkbridge.core.renderer import PT_PER_UNIT, PageNotFound, Renderer
from inkbridge.core.transport.base import RmDoc
from rmfixtures import line, notebook, rm_page, rmdoc, sample_pdf


def dark_pixels(png: bytes) -> int:
    img = Image.open(io.BytesIO(png)).convert("L")
    return sum(1 for v in img.tobytes() if v < 128)


def test_notebook_pages_render_on_blank_screen_sized_pages():
    doc = notebook([rm_page([line([(-300, 200), (300, 400)])]), None], deleted=1)
    r = Renderer(doc)
    assert [(p.number, p.pdf_page, p.has_ink) for p in r.pages] == [(1, None, True), (2, None, False)]
    assert r.pages[0].width == pytest.approx(1404 * PT_PER_UNIT)
    assert r.pages[0].height == pytest.approx(1872 * PT_PER_UNIT)

    out = pymupdf.open(stream=r.pdf(), filetype="pdf")
    assert out.page_count == 2
    assert out[0].get_drawings() and not out[1].get_drawings()

    png = r.png(1, width=400)
    assert Image.open(io.BytesIO(png)).width == 400
    assert dark_pixels(png) > 0 and dark_pixels(r.png(2, width=400)) == 0
    with pytest.raises(PageNotFound):
        r.png(3)


def test_notebook_page_grows_to_fit_ink_below_and_beside_the_screen():
    r = Renderer(notebook([rm_page([line([(0, 100), (900, 4000)])])]))
    page = r.pages[0]
    assert page.height == pytest.approx((4000 + 60) * PT_PER_UNIT)
    assert page.width == pytest.approx(2 * (900 + 60) * PT_PER_UNIT)
    # x = 0 stays at the centre: the stroke's bottom end lands near the right edge, not off the page.
    img = Image.open(io.BytesIO(r.png(1, width=600))).convert("L")
    w, h = img.size
    x, y = w / 2 + w / 2 * 900 / 960, h * 4000 / 4060
    assert min(img.crop((int(x) - 6, int(y) - 6, int(x) + 2, int(y) + 2)).tobytes()) < 128


def test_paper_pro_width_sets_the_screen_size():
    r = Renderer(notebook([None]), rm_width=1620)
    assert (r.pages[0].width, r.pages[0].height) == pytest.approx((1620 * PT_PER_UNIT, 2160 * PT_PER_UNIT))


def test_pdf_with_a_page_inserted_on_the_tablet():
    pdf = pymupdf.open()
    for i in range(2):
        pdf.new_page(width=500, height=700).insert_text((50, 100), f"Original page {i + 1}")
    doc = rmdoc(pdf.tobytes(), [None, rm_page([line([(0, 500), (100, 600)])])])
    content = doc.content
    # A page inserted between the two original pages: no redir.
    inserted = str(uuid4())
    content["cPages"]["pages"].insert(1, {"id": inserted})
    doc.files[f"{doc.uuid}.content"] = json.dumps(content).encode()
    doc.files[f"{doc.uuid}/{inserted}.rm"] = rm_page([line([(-100, 100), (100, 100)])])

    r = Renderer(doc)
    assert [(p.pdf_page, p.has_ink) for p in r.pages] == [(0, False), (None, True), (1, True)]
    assert (r.pages[0].width, r.pages[0].height) == (500, 700)
    out = pymupdf.open(stream=r.pdf(), filetype="pdf")
    assert [out[i].get_text().strip() for i in range(3)] == ["Original page 1", "", "Original page 2"]
    assert out[1].get_drawings() and out[2].get_drawings() and not out[0].get_drawings()


def test_pdf_never_opened_on_the_tablet_renders_as_is():
    u = str(uuid4())
    doc = RmDoc(u, {f"{u}.pdf": sample_pdf(height_pt=800), f"{u}.content": b"{}", f"{u}.metadata": b"{}"})
    r = Renderer(doc)
    assert [(p.number, p.id, p.pdf_page) for p in r.pages] == [(1, None, 0)]
    assert "Map viewer spec" in pymupdf.open(stream=r.pdf(), filetype="pdf")[0].get_text()


def test_broken_page_renders_without_its_ink():
    r = Renderer(notebook([b"not an rm file", rm_page([line([(0, 100), (10, 110)])])]))
    assert [p.has_ink for p in r.pages] == [False, True]
    assert pymupdf.open(stream=r.pdf(), filetype="pdf").page_count == 2


def test_tall_pages_are_capped_in_pixels():
    doc = rmdoc(sample_pdf(height_pt=14000), [None])
    img = Image.open(io.BytesIO(Renderer(doc).png(1, width=2000)))
    assert img.width * img.height <= 24_000_000 and img.width < 2000


def test_empty_document_has_no_pdf():
    with pytest.raises(PageNotFound):
        Renderer(notebook([])).pdf()


# --- typed text, anchors, layers, templates ------------------------------------------------------

from rmscene import scene_items as si  # noqa: E402

from inkbridge.core import pagetext, templates  # noqa: E402
from inkbridge.core.scene import parse_page  # noqa: E402
from rmfixtures import page_text, text_char  # noqa: E402

PS = si.ParagraphStyle


def test_typed_text_renders_with_styles():
    text = page_text([(PS.HEADING, "Plan"), (PS.BULLET, "Ship beta"), (PS.CHECKBOX_CHECKED, "Book room"),
                      (PS.PLAIN, "Grüße")])
    r = Renderer(notebook([rm_page([], text=text)]))
    page = r.pages[0]
    assert page.has_text and not page.has_ink
    out = pymupdf.open(stream=r.pdf(), filetype="pdf")
    words = out[0].get_text()
    for s in ("Plan", "• Ship beta", "☑ Book room", "Grüße"):
        assert s.replace(" ", " ", 1) in words or s in words, s
    assert r.text() == ["Plan\nShip beta\nBook room\nGrüße"]
    # The heading is set larger than the body text.
    sizes = {s["text"].strip(): s["size"] for b in out[0].get_text("dict")["blocks"] for ln in b["lines"]
             for s in ln["spans"]}
    assert sizes["Plan"] > sizes["Grüße"]
    # ink=False keeps the text: it is the page, not markup.
    assert "Plan" in pymupdf.open(stream=r.pdf(ink=False), filetype="pdf")[0].get_text()


def test_wrapped_words_anchor_to_their_own_line():
    long = " ".join(f"word{i}" for i in range(60))
    lay = pagetext.layout(parse_page(rm_page([], text=page_text([(PS.PLAIN, long)]))).text)
    first, last = lay.anchors[text_char(0)], lay.anchors[text_char(len(long) - 1)]
    assert first == pytest.approx(234 + pagetext.TEXT_TOP_Y)
    assert last - first >= 3 * pagetext.LINE and (last - first) % pagetext.LINE == pytest.approx(0)
    assert lay.height >= (last - first) + pagetext.LINE


def test_ink_anchored_to_text_moves_with_its_line():
    text = page_text([(PS.PLAIN, "First"), (PS.PLAIN, "Second line")])
    second = len("First\n")  # first character of the second paragraph
    data = rm_page([line([(0, 50), (10, 50)])], text=text,
                   anchored=[(text_char(second), 100.0, [line([(0, 50), (10, 50)])])])
    r = Renderer(notebook([data]))
    free, anchored = sorted(r._pages[0].ink.strokes, key=lambda s: s.points[0][1])
    assert free.points[0] == (0, 50)
    line_top = 234 + pagetext.TEXT_TOP_Y + pagetext.LINE
    assert anchored.points[0] == pytest.approx((100, line_top + 50))


def test_hidden_layers_are_not_drawn():
    r = Renderer(notebook([rm_page([], hidden=[line([(0, 100), (300, 300)])])]))
    assert not r.pages[0].has_ink
    assert not pymupdf.open(stream=r.pdf(), filetype="pdf")[0].get_drawings()


def test_long_text_grows_the_page():
    text = page_text([(PS.PLAIN, f"Line {i}") for i in range(40)])
    page = Renderer(notebook([rm_page([], text=text)])).pages[0]
    assert page.height > 1872 * PT_PER_UNIT
    assert page.height / PT_PER_UNIT >= 234 + pagetext.TEXT_TOP_Y + 40 * pagetext.LINE


def test_template_names_patterns_and_drawing():
    doc = notebook([None, None, None], templates=["P Lines small", "Blank", "P Calendar Year"])
    r = Renderer(doc)
    assert [(p.template, p.template_drawn) for p in r.pages] == [
        ("P Lines small", True), ("Blank", False), ("P Calendar Year", False)]
    out = pymupdf.open(stream=r.pdf(), filetype="pdf")
    assert out[0].get_drawings() and not out[1].get_drawings() and not out[2].get_drawings()
    assert templates.pattern("P Grid large") == ("grid", 89)
    assert templates.pattern("P Dots S") == ("dots", 53)
    assert templates.pattern("LS Lines medium") == ("lines", 71)


def test_template_names_from_pagedata():
    u = str(uuid4())
    doc = RmDoc(u, {f"{u}.content": json.dumps({"pages": ["a", "b"]}).encode(),
                    f"{u}.pagedata": b"P Grid medium\nBlank\n"})
    assert templates.names(doc) == {"a": "P Grid medium", "b": "Blank"}
    assert [p.template_drawn for p in Renderer(doc).pages] == [True, False]


def test_highlights_and_pen_colors():
    page = rm_page([line([(0, 100), (10, 110)], color=si.PenColor.RED),
                    line([(0, 200), (100, 200)], tool=si.Pen.HIGHLIGHTER_2, color=si.PenColor.HIGHLIGHT)],
                   [("blast radius", (-300, 130, 200, 20))])
    r = Renderer(notebook([page]))
    assert r.pen_colors() == ["red"]
    assert r.highlights() == [{"text": "blast radius", "color": "yellow"}]
