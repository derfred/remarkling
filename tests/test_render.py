import shutil

import pymupdf
import pytest

from inkbridge.core import render
from rmfixtures import sample_pdf


def cuts(height, cap=1000, **marks):
    return [(c.at, c.by) for c in render.plan_pages(height, marks, cap=cap)]


def test_short_content_is_one_page():
    assert cuts(900, h2=[100, 500]) == []


def test_cuts_at_last_h2_before_cap():
    assert cuts(2500, h2=[300, 900, 1400, 2100]) == [(900, "h2"), (1400, "h2"), (2100, "h2")]


def test_hard_cut_at_cap_without_marks():
    assert cuts(2500) == [(1000, "cap"), (2000, "cap")]


def test_page_breaks_cut_even_when_short():
    assert cuts(900, hard=[400]) == [(400, "page")]
    assert cuts(2600, hard=[1200], h2=[700]) == [(700, "h2"), (1200, "page"), (2200, "cap")]


def test_prefer_beats_a_later_heading_in_the_second_half():
    assert cuts(1500, prefer=[600], h2=[950]) == [(600, "prefer")]


def test_a_mark_in_the_first_half_only_when_nothing_better():
    # the h2 in the second half wins over a prefer early on the page
    assert cuts(1500, prefer=[200], h2=[950]) == [(950, "h2")]
    assert cuts(1500, prefer=[200], gap=[100]) == [(200, "prefer"), (1200, "cap")]


def test_never_cuts_inside_an_avoid_box():
    assert cuts(1500, h2=[950], gap=[800], avoid=[[900, 1100]]) == [(800, "gap")]
    # nothing usable: cut at the top of the box instead of through it
    assert cuts(1500, avoid=[[700, 1200]]) == [(700, "cap")]
    # a box taller than a page has to be cut
    assert cuts(2500, avoid=[[100, 2400]]) == [(1000, "cap"), (2000, "cap")]


def test_stamp_and_read_id():
    pdf = render.stamp_id(sample_pdf(), "rm-abc234", "Title")
    assert render.read_id(pdf) == "rm-abc234"
    assert render.read_id(b"not a pdf") is None


def test_passthrough_keeps_pages_and_adds_footer():
    out = render.passthrough_pdf(sample_pdf(), "rm-abc234", "Spec")
    doc = pymupdf.open(stream=out, filetype="pdf")
    assert doc.page_count == 1 and "rm-abc234" in doc[0].get_text()
    assert render.read_id(out) == "rm-abc234"


def test_titles_and_ids():
    assert render.tablet_title("Spec", "rm-abc234") == "Spec · rm-abc234"
    assert render.tablet_title("Spec", "rm-abc234.2", 2, 3) == "Spec (2/3) · rm-abc234.2"
    assert render.id_from_title("Spec (2/3) · rm-abc234.2") == "rm-abc234.2"
    assert render.id_from_title("Random notebook") is None


def test_markdown_to_html_renders_tables():
    html = render.markdown_to_html("# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", "T")
    assert "<table>" in html and "<h1>T</h1>" in html


def _chromium_available() -> bool:
    try:
        from playwright._impl._driver import compute_driver_executable  # noqa: F401
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return bool(p.chromium.executable_path) and shutil.os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


needs_chromium = pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")


@needs_chromium
async def test_render_tall_pdf_is_one_page_as_tall_as_content():
    paras = "".join(f"<p>Paragraph {i} with some text to fill the line.</p>" for i in range(80))
    pdf, cut = await render.render_tall_pdf("rm-abc234", "Long", html=f"<html><body><h1>Long</h1>{paras}</body></html>")
    assert cut == []
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    assert doc.page_count == 1
    assert doc[0].rect.height > 2 * doc[0].rect.width
    assert render.read_id(pdf) == "rm-abc234"


@needs_chromium
async def test_page_break_makes_a_second_page_of_one_pdf():
    html = ("<html><body><h1>Text</h1><p>First page.</p>"
            "<h2 style='break-before: page'>Answers</h2><p>Second page.</p></body></html>")
    pdf, cut = await render.render_tall_pdf("rm-abc234", "Two", html=html)
    assert [c.by for c in cut] == ["page"]
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    assert doc.page_count == 2
    one, two = doc[0].get_text(), doc[1].get_text()
    assert "First page." in one and "Answers" not in one
    assert "Answers" in two and "Second page." in two and "First page." not in two
    assert "rm-abc234" in two  # the footer is on the last page
    assert render.read_id(pdf) == "rm-abc234"


@needs_chromium
async def test_long_content_is_cut_at_the_preferred_mark(monkeypatch):
    monkeypatch.setattr(render, "HEIGHT_CAP_PX", 3000)
    block = "".join(f"<p>Line {i} of filler text.</p>" for i in range(30))
    html = (f"<html><body><section><h2>A</h2>{block}</section>"
            f"<section data-break='prefer'><h3>B</h3>{block}</section>"
            f"<section><h2>C</h2>{block}</section></body></html>")
    pdf, cut = await render.render_tall_pdf("rm-abc234", "Long", html=html)
    assert cut and cut[0].by in ("prefer", "h2")
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    assert doc.page_count == len(cut) + 1
    assert all(p.rect.height <= 3000 * 0.75 + 1 for p in doc)
    # no line of text is cut in half: every line is on exactly one page
    text = "".join(p.get_text() for p in doc)
    assert all(text.count(f"Line {i} of filler text.") == 3 for i in range(30))


def _window(text: str, height: float) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=510, height=height)
    page.insert_text((40, 40), text)
    return doc.tobytes()


def test_stack_windows_makes_one_pdf_with_margins_at_the_cuts():
    out = pymupdf.open(stream=render.stack_windows([_window("one", 300), _window("two", 200), _window("three", 100)], 30),
                       filetype="pdf")
    assert out.page_count == 3
    assert [round(p.rect.height) for p in out] == [330, 260, 130]  # first: below only, last: above only
    assert [p.get_text().strip() for p in out] == ["one", "two", "three"]
    assert out[1].search_for("two")[0].y0 > 30  # the content sits below the top margin
