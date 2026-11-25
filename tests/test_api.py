import pytest
from starlette.testclient import TestClient

from inkbridge import config, uploads
from inkbridge.core import pipeline
from inkbridge.core.store import SQLiteStore
from fakes import FakeInterpreter, MemoryTransport
from rmfixtures import line, rm_page, sample_pdf


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_URL", "https://ink.test")
    monkeypatch.setattr(uploads, "_uploads", None)
    config.get_settings.cache_clear()
    bridge = pipeline.InkBridge(config.get_settings(), SQLiteStore(":memory:"), MemoryTransport(), FakeInterpreter())
    monkeypatch.setattr(pipeline, "_bridge", bridge)
    from inkbridge.api import create_app

    with TestClient(create_app()) as c:
        c.bridge = bridge
        yield c
    config.get_settings.cache_clear()


def test_healthz_is_open(client):
    assert client.get("/healthz").json() == {"ok": True, "interpretation": "off"}


def test_the_app_does_no_auth(client):
    """Access control is the infrastructure's job: no route asks for credentials."""
    assert client.post("/poll").status_code == 200
    assert client.get("/api/cache").status_code == 200
    assert client.get("/api/docs/nope/pages").status_code == 404  # reached the route


def test_push_pdf_then_poll(client):
    r = client.post("/push", files={"file": ("spec.pdf", sample_pdf(), "application/pdf")})
    assert r.status_code == 200
    doc_id = r.json()["doc_id"]
    assert r.json()["title"] == "spec"

    t = client.bridge.transport
    t.annotate(client.bridge.store.get(doc_id).rm_uuid, rm_page([line([(0, 300), (10, 310)])]))
    r = client.post("/poll")
    assert r.json() == {"count": 1, "titles": ["spec"], "trilium_url": None, "failed": []}
    assert client.post("/poll").json()["count"] == 0


def test_push_rejects_unknown_types(client):
    assert client.post("/push", files={"file": ("x.bin", b"\x00\x01", "application/octet-stream")}).status_code == 415
    assert client.post("/push", data={}).status_code == 400


def test_mcp_endpoint(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    r = client.post("/mcp", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert "Ink Bridge" in r.text


def test_download_files_and_rmdoc(client):
    import io
    import zipfile
    from urllib.parse import quote

    r = client.post("/push", files={"file": ("spec.pdf", sample_pdf(), "application/pdf")})
    u = client.bridge.store.get(r.json()["doc_id"]).rm_uuid
    pdf = client.bridge.transport.docs[u][1].pdf  # as uploaded, with its /InkBridgeId stamp
    name = client.bridge.transport.docs[u][0].name  # "spec · rm-xxxxxx"

    r = client.get(f"/api/docs/{u}/files/{u}.pdf")
    assert r.status_code == 200 and r.content == pdf
    assert r.headers["content-type"] == "application/pdf"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="spec _ rm-')
    assert disposition.endswith(f"filename*=UTF-8''{quote(name + '.pdf', safe='')}")

    page = client.bridge.transport.docs[u][1].page_ids()[0]
    client.bridge.transport.annotate(u, rm_page([line([(0, 300), (10, 310)])]), move_to="Inbox")
    r = client.get(f"/api/docs/{u}/files/{u}/{page}.rm")
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    assert quote(f"{name} {page}.rm", safe="") in r.headers["content-disposition"]

    assert client.get(f"/api/docs/{u}/files/{u}.epub").status_code == 404
    assert client.get(f"/api/docs/{u}/files/{u}/../secret").status_code == 404
    assert client.get("/api/docs/nope/files/nope.pdf").status_code == 404

    r = client.get(f"/api/docs/{u}/rmdoc")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert quote(name + ".rmdoc", safe="") in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert z.read(f"{u}.pdf") == pdf and f"{u}/{page}.rm" in z.namelist()
    assert client.get("/api/docs/nope/rmdoc").status_code == 404


def test_render_pages_and_pdf(client):
    import io
    from urllib.parse import quote

    import pymupdf
    from PIL import Image

    r = client.post("/push", files={"file": ("spec.pdf", sample_pdf(), "application/pdf")})
    u = client.bridge.store.get(r.json()["doc_id"]).rm_uuid
    client.bridge.transport.annotate(u, rm_page([line([(0, 300), (10, 310)])]), move_to="Inbox")
    name = client.bridge.transport.docs[u][0].name

    pages = client.get(f"/api/docs/{u}/pages").json()["pages"]
    assert [(p["number"], p["pdf_page"], p["has_ink"]) for p in pages] == [(1, 0, True)]

    r = client.get(f"/api/docs/{u}/pages/1.png?width=300")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["content-disposition"].startswith("inline;")
    assert Image.open(io.BytesIO(r.content)).width == 300
    assert client.get(f"/api/docs/{u}/pages/2.png").status_code == 404
    assert client.get(f"/api/docs/{u}/pages/1.png?width=5").status_code == 422

    r = client.get(f"/api/docs/{u}/render.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert quote(f"{name} (rendered).pdf", safe="") in r.headers["content-disposition"]
    assert pymupdf.open(stream=r.content, filetype="pdf")[0].get_drawings()

    assert client.get("/api/docs/nope/pages").status_code == 404
    assert client.get("/api/docs/nope/render.pdf").status_code == 404


def test_page_previews_share_one_download(client):
    r = client.post("/push", files={"file": ("spec.pdf", sample_pdf(), "application/pdf")})
    u = client.bridge.store.get(r.json()["doc_id"]).rm_uuid
    t = client.bridge.transport
    calls, original = [], t.download

    async def counting(remote):
        calls.append(remote.uuid)
        return await original(remote)

    t.download = counting
    client.get(f"/api/docs/{u}/pages")
    client.get(f"/api/docs/{u}/pages/1.png")
    assert len(calls) == 1
    client.get(f"/api/docs/{u}/render.pdf")  # a download is always fresh
    assert len(calls) == 2


def _upload_url(**kw):
    token, _ = uploads.get_uploads().create(**{"title": "Spec", **kw})
    return f"/upload/{token}"


def test_upload_pdf_once(client):
    url = _upload_url()
    r = client.put(url, content=sample_pdf())
    assert r.status_code == 200, r.text
    doc = client.bridge.store.get(r.json()["doc_id"])
    assert doc.title == "Spec" and doc.source_kind == "pdf" and doc.source_ref == "upload"
    assert r.json()["tablet_path"] == [f"/Inbox/Spec · {doc.doc_id}"]
    assert client.put(url, content=sample_pdf()).status_code == 410


def test_upload_markdown_and_html(client, monkeypatch):
    calls = []

    async def fake_push(title, **kw):
        calls.append((title, kw))
        return pipeline.PushResult("rm-aaaaaa", title, ["/Inbox/x"], ["rm-aaaaaa"])

    monkeypatch.setattr(client.bridge, "push_document", fake_push)
    assert client.put(_upload_url(kind="markdown"), content="# Hi".encode()).status_code == 200
    r = client.post(_upload_url(ready_selector="#done"), content=b"<p>x</p>",
                    headers={"Content-Type": "text/html"})
    assert r.status_code == 200
    assert calls[0] == ("Spec", {"markdown": "# Hi", "ready_selector": None, "source_ref": "upload"})
    assert calls[1] == ("Spec", {"html": "<p>x</p>", "ready_selector": "#done", "source_ref": "upload"})


def test_failed_upload_can_be_retried(client, monkeypatch):
    url = _upload_url()
    assert client.put(url, content=b"\x00\x01").status_code == 415
    assert client.put(url, content=b"").status_code == 400
    assert client.put(url, content=sample_pdf()).status_code == 200


def test_upload_size_limit(client, monkeypatch):
    monkeypatch.setenv("UPLOAD_MAX_MB", "0")
    config.get_settings.cache_clear()
    assert client.put(_upload_url(), content=sample_pdf()).status_code == 413


def test_bad_upload_tokens(client):
    assert client.put("/upload/nonsense", content=sample_pdf()).status_code == 403
