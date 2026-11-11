"""Build synthetic .rm v6 pages and .rmdoc bundles for tests (no tablet needed)."""

from __future__ import annotations

import io
import json
from uuid import uuid4

import pymupdf
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.crdt_sequence import CrdtSequence
from rmscene.scene_stream import (
    AuthorIdsBlock,
    MigrationInfoBlock,
    PageInfoBlock,
    RootTextBlock,
    SceneGlyphItemBlock,
    SceneGroupItemBlock,
    SceneLineItemBlock,
    SceneTreeBlock,
    TreeNodeBlock,
    write_blocks,
)
from rmscene.tagged_block_common import CrdtId, LwwValue

from inkbridge.core.transport.base import RmDoc, pdf_content

LAYER = CrdtId(0, 11)


def line(points, color=si.PenColor.BLACK, tool=si.Pen.FINELINER_2, width=8, pressure=100, speed=0,
         direction=0, thickness=1.0) -> si.Line:
    """A stroke; a point is (x, y) or (x, y, pressure) to vary the pressure along it."""
    def point(p):
        x, y, *rest = p
        return si.Point(x=x, y=y, speed=speed, direction=direction, width=width,
                        pressure=rest[0] if rest else pressure)

    return si.Line(color=color, tool=tool, points=[point(p) for p in points], thickness_scale=thickness,
                   starting_length=0.0)


TS = CrdtId(1, 1)
TEXT_START = 16  # char IDs of the page text are CrdtId(1, 16), CrdtId(1, 17), ...


def text_char(index: int) -> CrdtId:
    """ID of the index-th character of the page text (newlines count)."""
    return CrdtId(1, TEXT_START + index)


def page_text(paragraphs: list[tuple[si.ParagraphStyle, str]], pos=(-468.0, 234.0), width=936.0) -> si.Text:
    """Typed text: paragraphs joined by newlines, each with its style."""
    body = "\n".join(t for _, t in paragraphs)
    styles, index = {}, 0
    for i, (style, t) in enumerate(paragraphs):
        # A paragraph's style is keyed by the newline that starts it (the first by the end marker).
        styles[CrdtId(0, 0) if i == 0 else text_char(index - 1)] = LwwValue(TS, style)
        index += len(t) + 1
    item = CrdtSequenceItem(text_char(0), CrdtId(0, 0), CrdtId(0, 0), 0, body)
    return si.Text(items=CrdtSequence([item]), styles=styles, pos_x=pos[0], pos_y=pos[1], width=width)


def rm_page(lines: list[si.Line], highlights: list[tuple[str, tuple[float, float, float, float]]] = (),
            text: si.Text | None = None, hidden: list[si.Line] = (),
            anchored: list[tuple[CrdtId, float, list[si.Line]]] = ()) -> bytes:
    """A page: `lines` on layer 1, `hidden` on a hidden layer 2, and per (anchor char, origin x,
    lines) a group anchored to a character of `text`."""
    counter = iter(range(1000, 100000))

    def nid() -> CrdtId:
        return CrdtId(2, next(counter))

    def blocks():
        yield AuthorIdsBlock(author_uuids={1: uuid4()})
        yield MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True)
        yield PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0)
        yield SceneTreeBlock(tree_id=LAYER, node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1))
        groups = []
        if hidden:
            groups.append((CrdtId(0, 21), CrdtId(0, 1), hidden,
                           dict(label=LwwValue(TS, "Layer 2"), visible=LwwValue(TS, False))))
        for anchor, origin_x, group_lines in anchored:
            groups.append((nid(), LAYER, group_lines, dict(
                anchor_id=LwwValue(TS, anchor), anchor_type=LwwValue(TS, 2),
                anchor_threshold=LwwValue(TS, 0.0), anchor_origin_x=LwwValue(TS, origin_x))))
        for gid, parent, _, _ in groups:
            yield SceneTreeBlock(tree_id=gid, node_id=CrdtId(0, 0), is_update=True, parent_id=parent)
        if text is not None:
            yield RootTextBlock(block_id=CrdtId(0, 0), value=text)
        yield TreeNodeBlock(si.Group(node_id=CrdtId(0, 1)))
        yield TreeNodeBlock(si.Group(node_id=LAYER, label=LwwValue(timestamp=CrdtId(0, 12), value="Layer 1")))
        for gid, _, _, attrs in groups:
            yield TreeNodeBlock(si.Group(node_id=gid, **attrs))
        yield SceneGroupItemBlock(
            parent_id=CrdtId(0, 1),
            item=CrdtSequenceItem(CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, LAYER),
        )
        prev_in = {}
        for gid, parent, _, _ in groups:
            item_id = nid()
            yield SceneGroupItemBlock(parent_id=parent, item=CrdtSequenceItem(
                item_id, prev_in.get(parent, CrdtId(0, 13) if parent == CrdtId(0, 1) else CrdtId(0, 0)),
                CrdtId(0, 0), 0, gid))
            prev_in[parent] = item_id
        prev = CrdtId(0, 0)
        for i, ln in enumerate(lines):
            item_id = CrdtId(1, 100 + i)
            yield SceneLineItemBlock(
                parent_id=LAYER, item=CrdtSequenceItem(item_id, prev, CrdtId(0, 0), 0, ln)
            )
            prev = item_id
        for i, (text_, (x, y, w, h)) in enumerate(highlights):
            item_id = CrdtId(1, 500 + i)
            glyph = si.GlyphRange(start=0, length=len(text_), text=text_, color=si.PenColor.YELLOW,
                                  rectangles=[si.Rectangle(x, y, w, h)])
            yield SceneGlyphItemBlock(parent_id=LAYER, item=CrdtSequenceItem(item_id, prev, CrdtId(0, 0), 0, glyph))
            prev = item_id
        for gid, _, group_lines, _ in groups:
            prev = CrdtId(0, 0)
            for ln in group_lines:
                item_id = nid()
                yield SceneLineItemBlock(parent_id=gid, item=CrdtSequenceItem(item_id, prev, CrdtId(0, 0), 0, ln))
                prev = item_id

    buf = io.BytesIO()
    write_blocks(buf, blocks())
    return buf.getvalue()


def notebook(pages: list[bytes | None], deleted: int = 0, templates: list[str | None] | None = None) -> RmDoc:
    """A notebook: no PDF, pages without redir. `deleted` extra pages are marked deleted."""
    u = str(uuid4())
    ids = [str(uuid4()) for _ in pages]
    cpages = []
    for i, pid in enumerate(ids):
        entry = {"id": pid}
        if templates and templates[i]:
            entry["template"] = {"timestamp": "1:1", "value": templates[i]}
        cpages.append(entry)
    cpages += [{"id": str(uuid4()), "deleted": {"value": 1}} for _ in range(deleted)]
    files = {f"{u}.content": json.dumps({"fileType": "notebook", "cPages": {"pages": cpages}}).encode(),
             f"{u}.metadata": json.dumps({"visibleName": "Notes", "type": "DocumentType"}).encode()}
    for pid, data in zip(ids, pages):
        if data:
            files[f"{u}/{pid}.rm"] = data
    return RmDoc(u, files)


def sample_pdf(height_pt: float = 2000, text: str = "The blast radius of this change is small.") -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=510, height=height_pt)
    page.insert_text((40, 80), "Map viewer spec", fontsize=18)
    page.insert_text((40, 140), text, fontsize=11)
    page.insert_text((40, height_pt - 200), "Closing paragraph near the bottom.", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def rmdoc(pdf: bytes, pages: list[bytes | None], name: str = "doc", parent: str = "") -> RmDoc:
    u = str(uuid4())
    page_ids = [str(uuid4()) for _ in pages]
    content = pdf_content(len(pages))
    content["cPages"] = {"pages": [{"id": pid, "redir": {"value": i}} for i, pid in enumerate(page_ids)]}
    files = {
        f"{u}.pdf": pdf,
        f"{u}.content": json.dumps(content).encode(),
        f"{u}.metadata": json.dumps({"visibleName": name, "parent": parent, "type": "DocumentType"}).encode(),
    }
    for pid, data in zip(page_ids, pages):
        if data:
            files[f"{u}/{pid}.rm"] = data
    return RmDoc(uuid=u, files=files)
