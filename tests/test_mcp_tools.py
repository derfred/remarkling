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
    assert names == {"push_document", "create_upload", "poll_now", "list_returned", "get_annotations", "list_inbox"}


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
    assert "No document rm-zzzzzz" in missing[0].text


async def test_poll_now_reports_failures(bridge):
    import json

    uuid = await bridge.transport.upload_pdf(b"not a pdf", "Broken", "Outbox")

    result = json.loads(_blocks(await mcp.call_tool("poll_now", {}))[0].text)
    assert result["pulled"] == []
    [failed] = result["failed"]
    assert failed["rm_uuid"] == uuid and failed["stage"] == "render" and failed["attempts"] == 1

    blocks = _blocks(await mcp.call_tool("get_annotations", {"doc_id": failed["doc_id"]}))
    assert json.loads(blocks[0].text)["failure"]["stage"] == "render"
    assert "No pulled annotations" in blocks[1].text


async def test_create_upload(bridge, monkeypatch, tmp_path):
    import json

    from inkbridge import config, uploads

    monkeypatch.setattr(uploads, "_uploads", uploads.Uploads(b"k" * 32))
    monkeypatch.setattr(config, "get_settings", lambda: config.Settings(data_dir=tmp_path, mcp_allowed_hosts=["ink.test"]))
    from inkbridge import mcp_server
    monkeypatch.setattr(mcp_server, "get_settings", config.get_settings)

    result = json.loads(_blocks(await mcp.call_tool("create_upload", {"title": "Spec"}))[0].text)
    assert result["upload_url"].startswith("https://ink.test/upload/")
    assert result["command"].endswith(f"-T <file> '{result['upload_url']}'")
    assert 0 < result["expires_in_s"] <= 900
    token = result["upload_url"].rsplit("/", 1)[1]
    assert uploads.get_uploads().check(token).title == "Spec"

    bad = json.loads(_blocks(await mcp.call_tool("create_upload", {"title": "x", "kind": "docx"}))[0].text)
    assert "kind must be" in bad["error"]
    monkeypatch.setattr(mcp_server, "get_settings", lambda: config.Settings(data_dir=tmp_path))
    none = json.loads(_blocks(await mcp.call_tool("create_upload", {"title": "x"}))[0].text)
    assert "PUBLIC_URL" in none["error"]
