import httpx
import pytest
from starlette.testclient import TestClient

from inkbridge import config
from inkbridge.core import pipeline
from inkbridge.core.explorer import CacheSnapshot
from inkbridge.core.store import Doc, SQLiteStore
from inkbridge.core.transport.cloud import CACHE_PREFIX, CloudTransport
from fakes import FakeInterpreter, MemoryTransport
from rmfixtures import sample_pdf
from test_cloud import FakeCloud


@pytest.fixture
async def filled():
    """A store whose cache holds a listed library: Inbox/Spec, Outbox/Notes, plus one trashed doc."""
    store = SQLiteStore(":memory:")
    fake = FakeCloud()
    t = CloudTransport("device", "https://auth.test", "https://sync.test",
                       client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)), cache=store)
    spec = await t.upload_pdf(sample_pdf(), "Spec · rm-abcdef", "Inbox")
    notes = await t.upload_pdf(sample_pdf(), "Notes", "Outbox")
    await t.list_folder("Inbox")
    store.upsert(Doc("rm-abcdef", "rm-abcdef", "Spec", "pdf", rm_uuid=spec, status="pushed", pushed_at=1.0))
    yield store, spec, notes
    await t.aclose()
    store.close()


async def test_snapshot_rebuilds_library(filled):
    store, spec, notes = filled
    snap = CacheSnapshot(store)
    lib = {n["name"]: n for n in snap.library()}
    assert set(lib) == {"Inbox", "Outbox", "Spec · rm-abcdef", "Notes"}
    assert lib["Inbox"]["type"] == "folder" and lib["Inbox"]["children"] == 1
    assert lib["Spec · rm-abcdef"]["parent"] == lib["Inbox"]["uuid"]
    assert lib["Spec · rm-abcdef"]["file_type"] == "pdf"
    assert lib["Spec · rm-abcdef"]["page_count"] == 1
    assert lib["Spec · rm-abcdef"]["inkbridge"]["doc_id"] == "rm-abcdef"
    assert lib["Notes"]["inkbridge"] is None

    detail = snap.node_detail(spec)
    assert detail["path"] == [{"uuid": lib["Inbox"]["uuid"], "name": "Inbox"}]
    cached = {f["name"].split(".")[-1]: f["cached"] for f in detail["file_list"]}
    assert cached == {"metadata": True, "content": True, "pdf": False, "pagedata": False}
    assert snap.node_detail("missing") is None


async def test_summary_and_blobs(filled):
    store, spec, _ = filled
    snap = CacheSnapshot(store)
    s = snap.summary()
    assert s["library"] == {"documents": 2, "folders": 2, "trashed": 0, "uncached": 0, "tracked": 1}
    assert s["by_kind"]["root"]["count"] == 1
    assert s["by_kind"]["index"]["count"] == 4
    assert s["by_kind"]["metadata"]["count"] == 4
    assert s["unreferenced"] == 0
    assert s["root"]["entries"] == 4

    metas = snap.blob_list(kind="metadata")
    assert metas["total"] == 4
    assert {i["doc_name"] for i in metas["items"]} == {"Inbox", "Outbox", "Spec · rm-abcdef", "Notes"}
    # Its index and metadata; the .content blob is identical for both PDFs, so it is shared (by hash)
    # and attributed to one of them.
    assert {i["kind"] for i in snap.blob_list(q="spec")["items"]} >= {"index", "metadata"}

    root = snap.blob_detail(CACHE_PREFIX + s["root"]["hash"])
    assert root["encoding"] == "index"
    assert {e["name"] for e in root["index"]["entries"]} == {"Inbox", "Outbox", "Spec · rm-abcdef", "Notes"}
    meta_key = next(i["key"] for i in metas["items"] if i["doc_uuid"] == spec)
    assert snap.blob_detail(meta_key)["value"]["visibleName"] == "Spec · rm-abcdef"


def test_empty_cache():
    snap = CacheSnapshot(SQLiteStore(":memory:"))
    assert snap.library() == []
    assert snap.summary()["root"] is None


@pytest.fixture
def client(filled, tmp_path, monkeypatch):
    store, spec, _ = filled
    monkeypatch.setenv("API_TOKEN", "phone")
    monkeypatch.setenv("MCP_TOKEN", "chat")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    bridge = pipeline.InkBridge(config.get_settings(), store, MemoryTransport(), FakeInterpreter())
    monkeypatch.setattr(pipeline, "_bridge", bridge)
    from inkbridge.api import create_app

    with TestClient(create_app()) as c:
        c.spec = spec
        yield c
    config.get_settings.cache_clear()


def test_api_requires_token_but_ui_does_not(client):
    assert client.get("/api/cache").status_code == 401
    assert client.get("/api/cache", headers={"Authorization": "Bearer chat"}).status_code == 401
    r = client.get("/ui")
    assert r.status_code == 200 and "Cache explorer" in r.text
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui"


def test_api_routes(client):
    auth = {"Authorization": "Bearer phone"}
    assert client.get("/api/cache", headers=auth).json()["library"]["documents"] == 2
    items = client.get("/api/cache/library", headers=auth).json()["items"]
    assert items[0]["type"] == "folder"  # folders first
    assert client.get(f"/api/cache/library/{client.spec}", headers=auth).json()["name"] == "Spec · rm-abcdef"
    assert client.get("/api/cache/library/nope", headers=auth).status_code == 404
    blobs = client.get("/api/cache/blobs?kind=root", headers=auth).json()
    assert blobs["total"] == 1
    assert client.get("/api/cache/blobs/" + blobs["items"][0]["key"], headers=auth).json()["kind"] == "root"
    assert client.get("/api/cache/blobs/cloud.blob:none", headers=auth).status_code == 404
    assert client.get("/api/cache/blobs?kind=bogus", headers=auth).status_code == 422
