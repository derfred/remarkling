"""Printing a PDF as pen strokes, and pulling only the reader's ink back off it."""

import io

import pymupdf
import pytest
from rmscene import read_tree
from rmscene import scene_items as si

from inkbridge.config import Settings
from inkbridge.core import inkprint
from inkbridge.core.pipeline import PRINT_RECORD, InkBridge
from inkbridge.core.rmdoc import RmDoc
from inkbridge.core.scene import RM_WIDTH, parse_page
from inkbridge.core.store import SQLiteStore
from fakes import FakeInterpreter, MemoryTransport
from rmfixtures import sample_pdf


def two_page_pdf() -> bytes:
    doc = pymupdf.open()
    for text in ("First page", "Second page • € 5 “quoted”"):
        page = doc.new_page(width=510, height=700)
        page.insert_text((40, 80), text, fontsize=14)
        page.draw_line((40, 100), (470, 100), width=1)
        page.draw_rect(pymupdf.Rect(40, 120, 470, 200), color=(0, 0, 0), width=0.5)
    data = doc.tobytes()
    doc.close()
    return data


def test_pages_become_strokes_on_a_print_layer():
    doc = inkprint.pdf_to_rmdoc(two_page_pdf(), "Spec")
    assert doc.pdf is None and doc.content["fileType"] == "notebook"
    assert doc.metadata["visibleName"] == "Spec" and len(doc.page_ids()) == 2
    for pid in doc.page_ids():
        tree = read_tree(io.BytesIO(doc.rm_file(pid)))
        layers = [g for g in tree.root.children.values() if isinstance(g, si.Group)]
        assert [g.label.value for g in layers] == ["Print", "Notes"]
        print_layer, notes = layers
        assert len(list(print_layer.children.values())) > 10 and not list(notes.children.values())
        assert all(isinstance(ln, si.Line) and ln.tool == si.Pen.FINELINER_2 for ln in print_layer.children.values())


def test_strokes_land_where_the_pdf_draws():
    pdf = two_page_pdf()
    doc = inkprint.pdf_to_rmdoc(pdf, "Spec")
    s = RM_WIDTH / 510
    strokes = parse_page(doc.rm_file(doc.page_ids()[0])).strokes
    xs = [x / s + 255 for st in strokes for x, _ in st.points]
    ys = [y / s for st in strokes for _, y in st.points]
    # The rectangle's corners and the text's top bound the drawing, in PDF points.
    assert min(xs) == pytest.approx(40, abs=1) and max(xs) == pytest.approx(470, abs=1)
    assert min(ys) == pytest.approx(70, abs=2) and max(ys) == pytest.approx(200, abs=1)
    # The text line: glyph strokes between the baseline's cap height and the baseline.
    text = [(x, y) for x, y in zip(xs, ys) if 60 < y < 85]
    assert text and min(x for x, _ in text) == pytest.approx(40, abs=3)


def test_characters_without_a_glyph_use_a_fallback():
    font = inkprint.load_font()
    for c in "\u2022\u25e6\u25cb\u20ac\u2713":  # drawn here
        assert font.glyphs[c].strokes
    for c, ascii_ in (("\u201c", '"'), ("\u2014", "-"), ("\u00e9", "e"), ("\u2192", "->")):
        assert c not in font.glyphs and inkprint._ascii(c) == ascii_


def test_fonts_can_be_chosen():
    pdf = sample_pdf()
    plotter = inkprint.pdf_to_rmdoc(pdf, "x", font="plotter")
    script = inkprint.pdf_to_rmdoc(pdf, "x", font="script")
    assert plotter.rm_file(plotter.page_ids()[0]) != script.rm_file(script.page_ids()[0])


def test_parse_page_can_skip_strokes():
    doc = inkprint.pdf_to_rmdoc(two_page_pdf(), "Spec")
    pid = doc.page_ids()[0]
    record = inkprint.printed_strokes(doc)
    all_strokes = parse_page(doc.rm_file(pid)).strokes
    from rmscene.tagged_block_common import CrdtId

    assert parse_page(doc.rm_file(pid), skip={CrdtId(a, b) for a, b in record[pid]}).strokes == []
    assert len(record[pid]) == len(all_strokes)


@pytest.fixture
def bridge(tmp_path):
    settings = Settings(data_dir=tmp_path, anthropic_api_key="x", trilium_url="")
    return InkBridge(settings, SQLiteStore(":memory:"), MemoryTransport(), FakeInterpreter())


def _annotated(printed_page: bytes, extra: list[inkprint.PrintStroke]) -> bytes:
    """The printed page plus the reader's strokes, as the tablet would sync it."""
    tree = read_tree(io.BytesIO(printed_page))
    layer = next(g for g in tree.root.children.values() if isinstance(g, si.Group))
    strokes = [inkprint.PrintStroke([(p.x, p.y) for p in ln.points], ln.points[0].width / 4)
               for ln in layer.children.values()]
    return inkprint.rm_page(strokes + extra)  # the extra strokes get IDs the print did not use


async def test_push_as_ink_then_pull_only_the_readers_ink(bridge):
    t = bridge.transport
    result = await bridge.push_document("Spec", pdf=two_page_pdf(), as_ink=True)
    doc = bridge.store.get(result.doc_id)
    remote, on_tablet = t.docs[doc.rm_uuid]
    assert on_tablet.pdf is None and len(on_tablet.page_ids()) == 2
    assert (doc.path / PRINT_RECORD).exists() and (doc.path / "original.pdf").exists()
    [status] = await bridge.document_status(result.doc_id)
    assert status["has_new_ink"] is False  # the print is not the reader's ink

    first = on_tablet.page_ids()[0]
    mark = inkprint.PrintStroke([(-300.0, 900.0), (300.0, 920.0)], 3.0, si.PenColor.RED)
    on_tablet.files[f"{doc.rm_uuid}/{first}.rm"] = _annotated(on_tablet.rm_file(first), [mark])
    t.docs[doc.rm_uuid] = (type(remote)(remote.uuid, remote.name, remote.parent, version="v2"), on_tablet)
    [status] = await bridge.document_status(result.doc_id)
    assert status["has_new_ink"] is True and status["changed_pages"] == [1]

    [pulled] = await bridge.pull_document(result.doc_id)
    assert pulled.doc_id == result.doc_id and pulled.strips == 1  # the red line only, not the print
    d = bridge.store.get(result.doc_id).path
    with pymupdf.open(d / "annotated.pdf") as annotated:
        assert annotated.page_count == 2
        assert "First page" in annotated[0].get_text()  # the source PDF is the background
    assert RmDoc.from_zip((d / "ink.rmdoc").read_bytes()).pdf is None  # the raw download is kept as is
