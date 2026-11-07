import pytest
from starlette.testclient import TestClient

from inkbridge import config
from inkbridge.core import pipeline
from inkbridge.core.store import Store
from fakes import FakeInterpreter, MemoryTransport
from rmfixtures import line, rm_page, sample_pdf


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "phone")
    monkeypatch.setenv("MCP_TOKEN", "chat")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    bridge = pipeline.InkBridge(config.get_settings(), Store(":memory:"), MemoryTransport(), FakeInterpreter())
    monkeypatch.setattr(pipeline, "_bridge", bridge)
    from inkbridge.api import create_app

    with TestClient(create_app()) as c:
        c.bridge = bridge
        yield c
    config.get_settings.cache_clear()


def test_healthz_is_open(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_routes_require_their_own_token(client):
    assert client.post("/poll").status_code == 401
    assert client.post("/poll", headers={"Authorization": "Bearer chat"}).status_code == 401
    assert client.post("/mcp", headers={"Authorization": "Bearer phone"}).status_code == 401


def test_push_pdf_then_poll(client):
    auth = {"Authorization": "Bearer phone"}
    r = client.post("/push", headers=auth, files={"file": ("spec.pdf", sample_pdf(), "application/pdf")})
    assert r.status_code == 200
    doc_id = r.json()["doc_id"]
    assert r.json()["title"] == "spec"

    t = client.bridge.transport
    t.annotate(client.bridge.store.get(doc_id).rm_uuid, rm_page([line([(0, 300), (10, 310)])]))
    r = client.post("/poll", headers=auth)
    assert r.json() == {"count": 1, "titles": ["spec"], "trilium_url": None}
    assert client.post("/poll", headers=auth).json()["count"] == 0


def test_push_rejects_unknown_types(client):
    auth = {"Authorization": "Bearer phone"}
    assert client.post("/push", headers=auth, files={"file": ("x.bin", b"\x00\x01", "application/octet-stream")}).status_code == 415
    assert client.post("/push", headers=auth, data={}).status_code == 400


def test_mcp_endpoint_accepts_query_token(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    r = client.post("/mcp?token=chat", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert "Ink Bridge" in r.text
