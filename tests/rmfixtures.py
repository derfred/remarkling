"""Build synthetic .rm v6 pages and .rmdoc bundles for tests (no tablet needed)."""

from __future__ import annotations

import io
import json
from uuid import uuid4

import pymupdf
from rmscene import scene_items as si
from rmscene.crdt_sequence import CrdtSequenceItem
from rmscene.scene_stream import (
    AuthorIdsBlock,
    MigrationInfoBlock,
    PageInfoBlock,
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


def line(points, color=si.PenColor.BLACK, tool=si.Pen.FINELINER_2) -> si.Line:
    return si.Line(
        color=color,
        tool=tool,
        points=[si.Point(x=x, y=y, speed=0, direction=0, width=8, pressure=100) for x, y in points],
        thickness_scale=1.0,
        starting_length=0.0,
    )


def rm_page(lines: list[si.Line], highlights: list[tuple[str, tuple[float, float, float, float]]] = ()) -> bytes:
    def blocks():
        yield AuthorIdsBlock(author_uuids={1: uuid4()})
        yield MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True)
        yield PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0)
        yield SceneTreeBlock(tree_id=LAYER, node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1))
        yield TreeNodeBlock(si.Group(node_id=CrdtId(0, 1)))
        yield TreeNodeBlock(si.Group(node_id=LAYER, label=LwwValue(timestamp=CrdtId(0, 12), value="Layer 1")))
        yield SceneGroupItemBlock(
            parent_id=CrdtId(0, 1),
            item=CrdtSequenceItem(CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, LAYER),
        )
        prev = CrdtId(0, 0)
        for i, ln in enumerate(lines):
            item_id = CrdtId(1, 100 + i)
            yield SceneLineItemBlock(
                parent_id=LAYER, item=CrdtSequenceItem(item_id, prev, CrdtId(0, 0), 0, ln)
            )
            prev = item_id
        for i, (text, (x, y, w, h)) in enumerate(highlights):
            item_id = CrdtId(1, 500 + i)
            glyph = si.GlyphRange(start=0, length=len(text), text=text, color=si.PenColor.YELLOW,
                                  rectangles=[si.Rectangle(x, y, w, h)])
            yield SceneGlyphItemBlock(parent_id=LAYER, item=CrdtSequenceItem(item_id, prev, CrdtId(0, 0), 0, glyph))
            prev = item_id

    buf = io.BytesIO()
    write_blocks(buf, blocks())
    return buf.getvalue()


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
