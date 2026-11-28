import pymupdf
import pytest

from inkbridge.core.joinpdf import MAX_HEIGHT_PT, join_pages


def paged_pdf(pages=4, width=595, height=842, header=True, footer=True, background=False, body=None):
    """An A4 export: a running header, body text, "Page n of N" in the footer."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        if background:
            page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
        if header:
            page.insert_text((50, 40), "Learning page structure from ink", fontsize=8)
        for j, line in enumerate(body(i) if body else [f"Section {i + 1} line {j + 1}" for j in range(5)]):
            page.insert_text((50, 150 + 30 * j), line, fontsize=11)
        if footer:
            page.insert_text((270, height - 30), f"Page {i + 1} of {pages}", fontsize=8)
    data = doc.tobytes()
    doc.close()
    return data


def text_of(pdf):
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        return [p.get_text() for p in d], [p.rect for p in d]


def test_joins_pages_and_cuts_running_headers_and_footers():
    result = join_pages(paged_pdf(4, background=True))
    assert result.layout == "tall" and result.joined == 4 and result.pages == 1
    [text], [rect] = text_of(result.pdf)
    assert rect.width == 595 and rect.height < 4 * 842 / 2  # margins and page gaps gone
    assert "Page 1 of 4" not in text and "Learning page structure" not in text
    order = [text.index(f"Section {i} line 1") for i in range(1, 5)]
    assert order == sorted(order)  # pages in order, text kept as text
    assert set(result.removed_lines) == {"Learning page structure from ink", "Page 1 of 4", "Page 2 of 4",
                                         "Page 3 of 4", "Page 4 of 4"}
    assert result.report() == {"layout": "tall", "pages": 1, "joined": 4,
                               "removed_headers_footers": result.removed_lines}


def test_two_pages_keep_their_header_lines():
    """Too few pages to tell a running header from content: nothing is cut."""
    result = join_pages(paged_pdf(2))
    [text], _ = text_of(result.pdf)
    assert result.layout == "tall" and "Page 1 of 2" in text and result.removed_lines == []


def test_blank_pages_are_dropped():
    result = join_pages(paged_pdf(3, header=False, footer=False,
                                  body=lambda i: [] if i == 1 else [f"Text on page {i + 1}"]))
    [text], _ = text_of(result.pdf)
    assert result.layout == "tall" and "Text on page 1" in text and "Text on page 3" in text


def test_long_documents_become_several_tall_pages():
    result = join_pages(paged_pdf(6), max_height=400)
    texts, rects = text_of(result.pdf)
    assert result.layout == "tall" and result.pages > 1
    assert all(r.height <= 400 for r in rects)
    joined = "".join(texts)
    assert all(f"Section {i} line 5" in joined for i in range(1, 7))


@pytest.mark.parametrize("pdf,reason", [
    (paged_pdf(1), "single page"),
    (None, "differ in width"),
    (None, "text layer"),
])
def test_keeps_the_pages_when_a_guard_fails(pdf, reason):
    if reason == "differ in width":
        doc = pymupdf.open()
        for w in (595, 612):
            doc.new_page(width=w, height=842).insert_text((50, 100), "text", fontsize=11)
        pdf = doc.tobytes()
    elif reason == "text layer":  # a scan: pages with only an image
        doc = pymupdf.open()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
        pix.clear_with(128)
        for _ in range(3):
            doc.new_page(width=595, height=842).insert_image(pymupdf.Rect(50, 50, 545, 792), pixmap=pix)
        pdf = doc.tobytes()
    result = join_pages(pdf)
    assert result.layout == "pages" and result.pdf == pdf and reason in result.reason
    assert result.report() == {"layout": "pages", "pages": result.pages, "reason": result.reason}


def test_result_stays_below_the_pdf_height_limit():
    result = join_pages(paged_pdf(30))
    _, rects = text_of(result.pdf)
    assert all(r.height <= MAX_HEIGHT_PT for r in rects)


def test_a_title_with_the_running_headers_words_is_kept():
    """Claude Docs exports repeat the title as a small running header; the big title stays."""
    def body(i):
        return ["Learning page structure from ink", "Intro text"] if i == 0 else [f"More text {i}"]
    result = join_pages(paged_pdf(4, body=body))
    [text], _ = text_of(result.pdf)
    assert text.count("Learning page structure from ink") == 1  # the title, not the header
    assert "Page 1 of 4" not in text


def test_content_reaching_into_the_footer_does_not_keep_it():
    """A table at the page bottom whose borders run past the footer's top."""
    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 150), f"Body {i + 1}", fontsize=11)
        page.draw_rect(pymupdf.Rect(45, 786, 550, 830), color=(0, 0, 0))
        page.insert_text((500, 828), f"Page {i + 1} of 3", fontsize=8)
    result = join_pages(doc.tobytes())
    [text], _ = text_of(result.pdf)
    assert result.layout == "tall" and "of 3" not in text
