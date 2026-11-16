import base64
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from inkbridge.config import Settings
from inkbridge.core import pipeline
from inkbridge.core.store import SQLiteStore
from inkbridge.mcp_server import create_server, mcp
from fakes import FakeInterpreter, MemoryTransport
from rmfixtures import line, notebook, page_text, rm_page, rmdoc, sample_pdf
from rmscene import scene_items as si

WRITE_TOOLS = {"push_document", "create_folder", "update_document", "trash_document", "process_outbox"}
READ_TOOLS = {"list_documents", "get_document", "read_document", "get_pages", "get_strokes", "get_annotations"}


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    b = pipeline.InkBridge(Settings(data_dir=tmp_path), SQLiteStore(":memory:"), MemoryTransport(), FakeInterpreter())
    monkeypatch.setattr(pipeline, "_bridge", b)
    monkeypatch.setattr("inkbridge.mcp_server.get_bridge", lambda: b)
    return b


@pytest.fixture
def library(bridge):
    """/Work/Notes (a notebook with ink and typed text), /Work/Spec (a PDF with a highlight), /Inbox."""
    t = bridge.transport
    t.folders["work-uuid"] = {"visibleName": "Work", "type": "CollectionType", "parent": ""}
    text = page_text([(si.ParagraphStyle.PLAIN, "Typed on the tablet")])
    notes = notebook([rm_page([line([(0, 300), (40, 320, 200), (80, 360)])], text=text), None])
    notes.files[f"{notes.uuid}.metadata"] = json.dumps({"visibleName": "Notes", "type": "DocumentType"}).encode()
    spec = rmdoc(sample_pdf(), [rm_page([], highlights=[("blast radius", (-300, 100, 200, 30))])], name="Spec")
    return {"notes": t.add(notes, "work-uuid"), "spec": t.add(spec, "work-uuid")}


def _blocks(result):
    return result[0] if isinstance(result, tuple) else getattr(result, "content", result)


async def call(name, args=None, server=None):
    blocks = _blocks(await (server or mcp).call_tool(name, args or {}))
    texts = [b.text for b in blocks if getattr(b, "type", "") == "text"]
    if len(blocks) == 1 and texts:
        try:
            return json.loads(texts[0])
        except ValueError:
            return texts[0]
    return blocks


async def test_tools_listed():
    assert {t.name for t in await mcp.list_tools()} == READ_TOOLS | WRITE_TOOLS
    read_only = create_server(Settings(mcp_access="read"))
    tools = await read_only.list_tools()
    assert {t.name for t in tools} == READ_TOOLS
    assert all(t.annotations.read_only_hint for t in tools)
    trash = next(t for t in await mcp.list_tools() if t.name == "trash_document")
    assert trash.annotations.destructive_hint


async def test_round_trip_through_tools(bridge):
    pushed = await call("push_document", {"title": "Spec", "pdf_base64": base64.b64encode(sample_pdf()).decode()})
    doc_id = pushed["doc_id"]
    assert pushed["tablet_paths"][0].startswith("/Inbox/Spec")

    bridge.transport.annotate(bridge.store.get(doc_id).rm_uuid, rm_page([line([(0, 300), (10, 310)])]))
    pulled = await call("process_outbox")
    assert pulled["doc_id"] == doc_id if isinstance(pulled, dict) else pulled[0]["doc_id"] == doc_id
    blocks = await call("get_annotations", {"doc": doc_id})
    assert "Explain blast radius" in blocks[0].text
    assert any(getattr(b, "type", "") == "image" for b in blocks[1:])
    assert bridge.interpreter.calls == 1  # answered from what process_outbox filed

    listed = await call("list_documents", {"folder": "/Archive"})
    assert listed["items"][0]["inkbridge"]["status"] == "pulled"

    with pytest.raises(ToolError, match="nothing found"):
        await call("get_annotations", {"doc": "rm-zzzzzz"})


async def test_list_and_search(library):
    top = await call("list_documents")
    assert [i["path"] for i in top["items"]] == ["/Work"]
    work = await call("list_documents", {"folder": "/Work"})
    assert {i["name"] for i in work["items"]} == {"Notes", "Spec"}
    assert await call("list_documents", {"folder": "work-uuid"}) == work

    found = await call("list_documents", {"query": "spe"})
    assert [(i["path"], i["file_type"]) for i in found["items"]] == [("/Work/Spec", "pdf")]
    notebooks = await call("list_documents", {"recursive": True, "kind": "notebook"})
    assert [i["path"] for i in notebooks["items"]] == ["/Work/Notes"]
    assert (await call("list_documents", {"modified_since": "2999-01-01"}))["total"] == 0

    with pytest.raises(ToolError, match="no folder|nothing found"):
        await call("list_documents", {"folder": "/Nope"})


async def test_resolve_by_path_name_and_uuid(library):
    by_path = await call("get_document", {"doc": "/Work/Notes"})
    assert by_path["uuid"] == library["notes"]
    assert (await call("get_document", {"doc": "Notes"}))["uuid"] == library["notes"]
    assert (await call("get_document", {"doc": library["notes"]}))["path"] == "/Work/Notes"
    assert by_path["pages"] == 2
    first = by_path["page_list"][0]
    assert first["strokes"] == 1 and first["typed_text"] and first["pdf_page"] is None

    folder = await call("get_document", {"doc": "/Work"})
    assert folder["type"] == "folder" and len(folder["contents"]) == 2


async def test_read_document_text_layer(library):
    spec = await call("read_document", {"doc": "/Work/Spec"})
    page = spec["pages"][0]
    assert "blast radius" in page["text"]
    assert page["highlights"] == [{"text": "blast radius", "color": "yellow"}]

    notes = await call("read_document", {"doc": "/Work/Notes", "pages": "1"})
    assert notes["pages"] == [{"number": 1, "typed_text": "Typed on the tablet"}]
    with pytest.raises(ToolError, match="has 2 pages"):
        await call("read_document", {"doc": "/Work/Notes", "pages": "3"})


async def test_get_pages_tiles_tall_pages(library):
    blocks = await call("get_pages", {"doc": "/Work/Spec", "width": 400})
    images = [b for b in blocks if getattr(b, "type", "") == "image"]
    # 510 x 2000 pt at 400 px wide is ~1570 px tall: three tiles of 560 px.
    assert len(images) == 3
    assert "part 1 of 3" in blocks[0].text

    clean = await call("get_pages", {"doc": "/Work/Spec", "width": 400, "ink": False})
    assert len([b for b in clean if getattr(b, "type", "") == "image"]) == 3

    more = await call("get_pages", {"doc": "/Work/Notes", "pages": "1-2", "skip": 1})
    assert [b.text for b in more if getattr(b, "type", "") == "text"] == ["Page 2"]


async def test_get_strokes(library):
    full = await call("get_strokes", {"doc": "/Work/Notes"})
    page = full["pages"][0]
    stroke = page["strokes"][0]
    assert stroke["tool"] == "fineliner_2" and stroke["color"] == "black"
    assert stroke["points"][1] == [40, 320, 200, 8, 0, 0]
    assert stroke["bbox"] == [0, 300, 80, 360]
    assert page["typed_text"] == "Typed on the tablet"
    assert page["to_pdf_points"]["scale"] > 0
    assert full["pages"][1]["strokes"] == []

    xy = await call("get_strokes", {"doc": "/Work/Notes", "pages": "1", "detail": "xy"})
    assert xy["pages"][0]["strokes"][0]["points"][0] == [0, 300]
    bbox = await call("get_strokes", {"doc": "/Work/Notes", "detail": "bbox", "limit": 1, "offset": 1})
    assert all(p["strokes"] == [] for p in bbox["pages"]) and "next_offset" not in bbox

    spec = await call("get_strokes", {"doc": "/Work/Spec"})
    assert spec["pages"][0]["highlights"][0]["text"] == "blast radius"


async def test_annotations_of_any_document_are_cached_per_version(bridge, library):
    blocks = await call("get_annotations", {"doc": "/Work/Notes"})
    body = json.loads(blocks[0].text)
    assert body["path"] == "/Work/Notes" and body["marks"]
    assert bridge.interpreter.calls == 1
    await call("get_annotations", {"doc": "/Work/Notes"})
    assert bridge.interpreter.calls == 1
    await call("get_annotations", {"doc": "/Work/Notes", "refresh": True})
    assert bridge.interpreter.calls == 2
    # Nothing moved, nothing filed.
    assert (await call("get_document", {"doc": "/Work/Notes"}))["path"] == "/Work/Notes"
    assert bridge.store.with_rm_uuid() == []


async def test_folders_push_and_organise(bridge, library):
    made = await call("create_folder", {"path": "/Work/Projects/Q4"})
    assert made["created"] and made["path"] == "/Work/Projects/Q4"
    again = await call("create_folder", {"path": "Work/Projects/Q4/"})
    assert again == {**made, "created": False}

    pdf = base64.b64encode(sample_pdf()).decode()
    pushed = await call("push_document", {"title": "Plan", "pdf_base64": pdf, "folder": "/Work/Projects/Q4"})
    assert pushed["tablet_paths"][0].startswith("/Work/Projects/Q4/Plan")

    moved = await call("update_document", {"doc": "/Work/Spec", "name": "Spec v1", "folder": "/Archive/Old",
                                           "pinned": True, "tags": ["review", "q4", "review"]})
    assert moved["changed"] and moved["path"] == "/Archive/Old/Spec v1"
    assert moved["pinned"] and moved["tags"] == ["review", "q4"]
    assert (await call("list_documents", {"tag": "Q4", "recursive": True}))["items"][0]["name"] == "Spec v1"
    unchanged = await call("update_document", {"doc": "/Archive/Old/Spec v1", "pinned": True})
    assert unchanged["changed"] is False

    with pytest.raises(ToolError, match="into itself"):
        await call("update_document", {"doc": "/Work", "folder": "/Work/Projects"})
    with pytest.raises(ToolError, match="no '/'"):
        await call("update_document", {"doc": "/Work", "name": "a/b"})

    trashed = await call("trash_document", {"doc": "/Archive/Old/Spec v1"})
    assert trashed["changed"] and "update_document" in trashed["restore_with"]
    assert [i["name"] for i in (await call("list_documents", {"folder": "/Trash"}))["items"]] == ["Spec v1"]
    assert (await call("list_documents", {"query": "Spec"}))["total"] == 0
    restored = await call("update_document", {"doc": library["spec"], "folder": "/Work"})
    assert restored["path"] == "/Work/Spec v1"


async def test_push_needs_exactly_one_source(bridge):
    with pytest.raises(ToolError, match="exactly one"):
        await call("push_document", {"title": "x", "markdown": "a", "url": "https://example.com"})


async def test_scope_confines_every_tool(bridge, library, monkeypatch):
    t = bridge.transport
    t.folders["private"] = {"visibleName": "Private", "type": "CollectionType", "parent": ""}
    t.add(rmdoc(sample_pdf(), [None], name="Diary"), "private")
    scoped = create_server(Settings(mcp_folders=["/Work", "/Inbox"]))

    top = await call("list_documents", server=scoped)
    assert [i["path"] for i in top["items"]] == ["/Work"]  # /Inbox does not exist yet
    assert top["scope"] == ["/Work", "/Inbox"]
    everything = await call("list_documents", {"recursive": True}, server=scoped)
    assert {i["path"] for i in everything["items"]} == {"/Work", "/Work/Notes", "/Work/Spec"}

    for ref in ("Diary", "/Private/Diary", "/Private"):
        with pytest.raises(ToolError, match="nothing found"):
            await call("get_document", {"doc": ref}, server=scoped)
    with pytest.raises(ToolError, match="outside"):
        await call("update_document", {"doc": "/Work/Spec", "folder": "/Private"}, server=scoped)
    with pytest.raises(ToolError, match="outside"):
        await call("create_folder", {"path": "/Elsewhere"}, server=scoped)
    with pytest.raises(ToolError, match="outside"):
        await call("push_document", {"title": "x", "markdown": "a", "folder": "/"}, server=scoped)
    with pytest.raises(ToolError, match="outside"):
        await call("list_documents", {"folder": "/Trash"}, server=scoped)
    with pytest.raises(ToolError, match="scope folder"):
        await call("trash_document", {"doc": "/Work"}, server=scoped)
    with pytest.raises(ToolError, match="scope folder"):
        await call("update_document", {"doc": "/Work", "name": "Job"}, server=scoped)

    await call("trash_document", {"doc": "/Work/Spec"}, server=scoped)
    with pytest.raises(ToolError, match="nothing found"):
        await call("get_document", {"doc": library["spec"]}, server=scoped)
