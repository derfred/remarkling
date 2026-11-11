import pytest

from inkbridge.core import pipeline
from inkbridge.core.store import SQLiteStore
from inkbridge.config import Settings
from inkbridge.mcp_server import mcp
from fakes import FakeInterpreter, MemoryTransport
from rmfixtures import line, rm_page, sample_pdf


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    b = pipeline.InkBridge(Settings(data_dir=tmp_path), SQLiteStore(":memory:"), MemoryTransport(), FakeInterpreter())
    monkeypatch.setattr(pipeline, "_bridge", b)
    return b


def _blocks(result):
    return result[0] if isinstance(result, tuple) else getattr(result, "content", result)


async def test_tools_listed():
    names = {t.name for t in await mcp.list_tools()}
    assert names == {"push_document", "poll_now", "list_returned", "get_annotations", "list_inbox"}


async def test_round_trip_through_tools(bridge):
    import base64

    pushed = await mcp.call_tool("push_document", {"title": "Spec", "pdf_base64": base64.b64encode(sample_pdf()).decode()})
    text = _blocks(pushed)[0].text
    doc_id = next(w.strip('",') for w in text.split() if w.strip('",').startswith("rm-"))

    bridge.transport.annotate(bridge.store.get(doc_id).rm_uuid, rm_page([line([(0, 300), (10, 310)])]))
    await mcp.call_tool("poll_now", {})
    blocks = _blocks(await mcp.call_tool("get_annotations", {"doc_id": doc_id}))
    assert "Explain blast radius" in blocks[0].text
    assert any(getattr(b, "type", "") == "image" for b in blocks[1:])

    missing = _blocks(await mcp.call_tool("get_annotations", {"doc_id": "rm-zzzzzz"}))
    assert "No pulled annotations" in missing[0].text
