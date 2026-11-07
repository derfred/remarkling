import shutil

import pymupdf
import pytest

from inkbridge.core import render
from rmfixtures import sample_pdf


def test_plan_splits_cuts_at_last_h2_before_cap():
    assert render.plan_splits(900, [100, 500], cap=1000) == [(0, 900)]
    assert render.plan_splits(2500, [300, 900, 1400, 2100], cap=1000) == [(0, 900), (900, 1400), (1400, 2100), (2100, 2500)]


def test_plan_splits_hard_cut_when_section_too_tall():
    assert render.plan_splits(2500, [], cap=1000) == [(0, 1000), (1000, 2000), (2000, 2500)]


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


@pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")
async def test_render_tall_pdf_is_one_page_as_tall_as_content():
    paras = "".join(f"<p>Paragraph {i} with some text to fill the line.</p>" for i in range(80))
    parts = await render.render_tall_pdfs("rm-abc234", "Long", html=f"<html><body><h1>Long</h1>{paras}</body></html>")
    assert len(parts) == 1
    doc = pymupdf.open(stream=parts[0].pdf, filetype="pdf")
    assert doc.page_count == 1
    assert doc[0].rect.height > 2 * doc[0].rect.width
    assert render.read_id(parts[0].pdf) == "rm-abc234"
