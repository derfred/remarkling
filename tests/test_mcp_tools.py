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
    assert names == {"push_document", "create_upload", "pull_document", "document_status", "list_documents",
                     "remove_document", "poll_now", "list_returned", "get_annotations", "list_inbox"}


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

    from inkbridge import config, mcp_server, uploads

    monkeypatch.setattr(uploads, "_uploads", uploads.Uploads(b"k" * 32))
    monkeypatch.setattr(mcp_server, "get_settings",
                        lambda: config.Settings(data_dir=tmp_path, public_url="https://ink.test"))

    result = json.loads(_blocks(await mcp.call_tool("create_upload", {"title": "Spec"}))[0].text)
    assert result["upload_url"].startswith("https://ink.test/upload/")
    assert result["command"].endswith(f"-T <file> '{result['upload_url']}'")
    assert 0 < result["expires_in_s"] <= 900
    token = result["upload_url"].rsplit("/", 1)[1]
    assert uploads.get_uploads().check(token).title == "Spec"

    bad = json.loads(_blocks(await mcp.call_tool("create_upload", {"title": "x", "kind": "docx"}))[0].text)
    assert "kind must be" in bad["error"]
    # No PUBLIC_URL and no HTTP request to take the URL from.
    monkeypatch.setattr(mcp_server, "get_settings", lambda: config.Settings(data_dir=tmp_path))
    none = json.loads(_blocks(await mcp.call_tool("create_upload", {"title": "x"}))[0].text)
    assert "PUBLIC_URL" in none["error"]


@pytest.mark.parametrize("headers,expected", [
    ({}, "http://testserver/upload/"),  # called through the Service: its own URL
    ({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "ink.example.com"}, "https://ink.example.com/upload/"),
    ({"X-Forwarded-Proto": "https, http", "X-Forwarded-Host": "ink.example.com, inner",
      "X-Forwarded-Prefix": "/remarkling/"}, "https://ink.example.com/remarkling/upload/"),
])
def test_create_upload_url_comes_from_the_request(tmp_path, monkeypatch, headers, expected):
    import json

    from starlette.testclient import TestClient

    from inkbridge import config, uploads

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.setattr(uploads, "_uploads", None)
    config.get_settings.cache_clear()
    from inkbridge.api import create_app

    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "create_upload", "arguments": {"title": "Spec"}}}
    with TestClient(create_app()) as client:
        r = client.post("/mcp", json=call, headers={
            "Accept": "application/json, text/event-stream", "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-06-18", **headers})
    config.get_settings.cache_clear()
    assert r.status_code == 200, r.text
    body = r.json() if r.headers["content-type"].startswith("application/json") else json.loads(
        next(line[5:] for line in r.text.splitlines() if line.startswith("data:")))
    result = json.loads(body["result"]["content"][0]["text"])
    assert result["upload_url"].startswith(expected), result


async def test_pull_document_tool_returns_strips(bridge):
    import json

    pushed = await bridge.push_document("Spec", pdf=sample_pdf())
    bridge.transport.annotate(bridge.store.get(pushed.doc_id).rm_uuid, rm_page([line([(0, 300), (10, 310)])]),
                              move_to=None)
    status = json.loads(_blocks(await mcp.call_tool("document_status", {"doc_id": pushed.doc_id}))[0].text)
    assert status["has_new_ink"] and status["location"] == "/Inbox"

    blocks = _blocks(await mcp.call_tool("pull_document", {"doc_id": pushed.doc_id}))
    assert json.loads(blocks[0].text)["pulled"][0]["new"] is True
    assert json.loads(blocks[1].text)["status"] == "pulled"
    assert any(getattr(b, "type", "") == "image" for b in blocks[2:])
    await bridge.drain()

    removed = json.loads(_blocks(await mcp.call_tool("remove_document", {"doc_id": pushed.doc_id}))[0].text)
    assert removed == {"removed": [pushed.doc_id]}
    gone = _blocks(await mcp.call_tool("pull_document", {"doc_id": pushed.doc_id}))
    assert "not on the tablet" in gone[0].text and any(getattr(b, "type", "") == "image" for b in gone)
    missing = _blocks(await mcp.call_tool("pull_document", {"doc_id": "rm-zzzzzz"}))
    assert "No document rm-zzzzzz" in missing[0].text
