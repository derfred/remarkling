import pytest
from starlette.testclient import TestClient

from inkbridge import config
from inkbridge.core import pipeline
from inkbridge.core.store import SQLiteStore
from fakes import FakeInterpreter, MemoryTransport
from rmfixtures import line, rm_page, sample_pdf


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "phone")
    monkeypatch.setenv("MCP_TOKEN", "chat")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    bridge = pipeline.InkBridge(config.get_settings(), SQLiteStore(":memory:"), MemoryTransport(), FakeInterpreter())
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


def test_download_files_and_rmdoc(client):
    import io
    import zipfile
    from urllib.parse import quote

    auth = {"Authorization": "Bearer phone"}
    r = client.post("/push", headers=auth, files={"file": ("spec.pdf", sample_pdf(), "application/pdf")})
    u = client.bridge.store.get(r.json()["doc_id"]).rm_uuid
    pdf = client.bridge.transport.docs[u][1].pdf  # as uploaded, with its /InkBridgeId stamp
    name = client.bridge.transport.docs[u][0].name  # "spec · rm-xxxxxx"

    assert client.get(f"/api/docs/{u}/files/{u}.pdf").status_code == 401
    r = client.get(f"/api/docs/{u}/files/{u}.pdf", headers=auth)
    assert r.status_code == 200 and r.content == pdf
    assert r.headers["content-type"] == "application/pdf"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="spec _ rm-')
    assert disposition.endswith(f"filename*=UTF-8''{quote(name + '.pdf', safe='')}")

    page = client.bridge.transport.docs[u][1].page_ids()[0]
    client.bridge.transport.annotate(u, rm_page([line([(0, 300), (10, 310)])]), move_to="Inbox")
    r = client.get(f"/api/docs/{u}/files/{u}/{page}.rm", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    assert quote(f"{name} {page}.rm", safe="") in r.headers["content-disposition"]

    assert client.get(f"/api/docs/{u}/files/{u}.epub", headers=auth).status_code == 404
    assert client.get(f"/api/docs/{u}/files/{u}/../secret", headers=auth).status_code == 404
    assert client.get("/api/docs/nope/files/nope.pdf", headers=auth).status_code == 404

    r = client.get(f"/api/docs/{u}/rmdoc", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert quote(name + ".rmdoc", safe="") in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert z.read(f"{u}.pdf") == pdf and f"{u}/{page}.rm" in z.namelist()
    assert client.get("/api/docs/nope/rmdoc", headers=auth).status_code == 404


def test_render_pages_and_pdf(client):
    import io
    from urllib.parse import quote

    import pymupdf
    from PIL import Image

    auth = {"Authorization": "Bearer phone"}
    r = client.post("/push", headers=auth, files={"file": ("spec.pdf", sample_pdf(), "application/pdf")})
    u = client.bridge.store.get(r.json()["doc_id"]).rm_uuid
    client.bridge.transport.annotate(u, rm_page([line([(0, 300), (10, 310)])]), move_to="Inbox")
    name = client.bridge.transport.docs[u][0].name

    assert client.get(f"/api/docs/{u}/pages").status_code == 401
    pages = client.get(f"/api/docs/{u}/pages", headers=auth).json()["pages"]
    assert [(p["number"], p["pdf_page"], p["has_ink"]) for p in pages] == [(1, 0, True)]

    r = client.get(f"/api/docs/{u}/pages/1.png?width=300", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline;")
    assert Image.open(io.BytesIO(r.content)).width == 300
    assert client.get(f"/api/docs/{u}/pages/2.png", headers=auth).status_code == 404
    assert client.get(f"/api/docs/{u}/pages/1.png?width=5", headers=auth).status_code == 422

    r = client.get(f"/api/docs/{u}/render.pdf", headers=auth)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert quote(f"{name} (rendered).pdf", safe="") in r.headers["content-disposition"]
    assert pymupdf.open(stream=r.content, filetype="pdf")[0].get_drawings()

    assert client.get("/api/docs/nope/pages", headers=auth).status_code == 404
    assert client.get("/api/docs/nope/render.pdf", headers=auth).status_code == 404


def test_page_previews_share_one_download(client):
    auth = {"Authorization": "Bearer phone"}
    r = client.post("/push", headers=auth, files={"file": ("spec.pdf", sample_pdf(), "application/pdf")})
    u = client.bridge.store.get(r.json()["doc_id"]).rm_uuid
    t = client.bridge.transport
    calls, original = [], t.download

    async def counting(remote):
        calls.append(remote.uuid)
        return await original(remote)

    t.download = counting
    client.get(f"/api/docs/{u}/pages", headers=auth)
    client.get(f"/api/docs/{u}/pages/1.png", headers=auth)
    assert len(calls) == 1
    client.get(f"/api/docs/{u}/render.pdf", headers=auth)  # a download is always fresh
    assert len(calls) == 2
