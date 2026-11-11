import json

import httpx
import pytest

from inkbridge.core.transport.cloud import CloudTransport, Entry, Index, crc32c, sha256
from inkbridge.core.store import SQLiteStore
from rmfixtures import sample_pdf


def test_crc32c_known_vector():
    assert crc32c(b"123456789") == 0xE3069283


def test_index_roundtrip_schema4():
    idx = Index("4", [Entry("ab" * 32, "0", "u.pdf", 0, 10), Entry("cd" * 32, "0", "u.content", 0, 5)])
    data = idx.serialize("u")
    assert data.splitlines()[:2] == [b"4", b"0:u:2:15"]
    again = Index.parse(data)
    assert [e.id for e in again.entries] == ["u.content", "u.pdf"]
    assert idx.hash(data) == sha256(data)


def test_index_schema3_hash_is_hash_of_entry_hashes():
    idx = Index("3", [Entry("11" * 32, "0", "b", 0, 1), Entry("22" * 32, "0", "a", 0, 1)])
    data = idx.serialize()
    assert data.splitlines()[0] == b"3"
    assert idx.hash(data) == sha256(bytes.fromhex("22" * 32) + bytes.fromhex("11" * 32))


class FakeCloud:
    """Just enough of the sync15 API: blobs, a generation-checked root, token endpoints."""

    def __init__(self, schema="4"):
        self.blobs: dict[str, bytes] = {}
        self.generation = 1
        empty = Index(schema, []).serialize(".")
        self.root = sha256(empty) if schema == "4" else Index(schema, []).hash(empty)
        self.blobs[self.root] = empty
        self.user_token_calls = 0
        self.conflict_next = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/user/new"):
            self.user_token_calls += 1
            return httpx.Response(200, text=f"user-{self.user_token_calls}")
        assert request.headers["authorization"].startswith("Bearer user-")
        if path == "/sync/v4/root":
            return httpx.Response(200, json={"hash": self.root, "generation": self.generation, "schemaVersion": 4})
        if path == "/sync/v3/root":
            body = json.loads(request.content)
            if self.conflict_next or body["generation"] != self.generation:
                self.conflict_next = False
                return httpx.Response(412)
            self.root = body["hash"]
            self.generation += 1
            return httpx.Response(200, json={"generation": self.generation})
        if path.startswith("/sync/v3/files/"):
            h = path.rsplit("/", 1)[1]
            if request.method == "PUT":
                assert request.headers["x-goog-hash"].startswith("crc32c=")
                self.blobs[h] = request.content
                return httpx.Response(200)
            if not request.headers.get("rm-filename"):
                return httpx.Response(400, text="missing rm-filename")  # the real cloud rejects these too
            return httpx.Response(200, content=self.blobs[h]) if h in self.blobs else httpx.Response(404)
        return httpx.Response(404)


@pytest.fixture
def cloud():
    fake = FakeCloud()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return fake, CloudTransport("device", "https://auth.test", "https://sync.test", client=client)


async def test_upload_list_download_move(cloud):
    fake, t = cloud
    pdf = sample_pdf()
    u = await t.upload_pdf(pdf, "Spec · rm-abcdef", "Inbox")
    inbox = await t.list_folder("Inbox")
    assert [(d.uuid, d.name) for d in inbox] == [(u, "Spec · rm-abcdef")]

    doc = await t.download(inbox[0])
    assert doc.pdf == pdf
    assert doc.content["fileType"] == "pdf"

    fake.conflict_next = True  # first commit attempt loses a race and is retried
    await t.move(inbox[0], "Outbox")
    assert await t.list_folder("Inbox") == []
    outbox = await t.list_folder("Outbox")
    assert outbox[0].uuid == u and outbox[0].version != inbox[0].version
    # Folders are created once and reused.
    folders = [e for e in Index.parse(fake.blobs[fake.root]).entries]
    assert len(folders) == 3  # Inbox, Outbox, the document


async def test_user_token_refreshes_on_401(cloud):
    fake, t = cloud
    calls = {"n": 0}
    inner = fake.handler

    def flaky(request):
        if request.url.path == "/sync/v4/root" and calls["n"] == 0:
            calls["n"] += 1
            return httpx.Response(401)
        return inner(request)

    t.client = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    await t.list_folder("Inbox")
    assert fake.user_token_calls == 2


async def test_writes_v4_root_and_v3_doc_indexes():
    fake = FakeCloud(schema="3")  # a legacy root is upgraded on the first write
    t = CloudTransport("device", "https://auth.test", "https://sync.test",
                       client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    u = await t.upload_pdf(sample_pdf(), "Spec", "Inbox")

    root_data = fake.blobs[fake.root]
    root = Index.parse(root_data)
    assert root_data.splitlines()[:2] == [b"4", f"0:.:2:{sum(e.size for e in root.entries)}".encode()]
    assert fake.root == sha256(root_data)

    entry = Index.parse(root_data).get(u)
    doc_data = fake.blobs[entry.hash]
    doc_index = Index.parse(doc_data)
    assert doc_data.splitlines()[0] == b"3"
    assert entry.hash == doc_index.hash(doc_data)  # hash of the sorted file hashes
    assert entry.hash != sha256(doc_data)


async def test_lists_root_entries_typed_zero(cloud):
    fake, t = cloud
    u = await t.upload_pdf(sample_pdf(), "Spec", "Inbox")
    # Rewrite the root the way rmapi does for v4: entries typed "0" instead of "80000000".
    root = Index.parse(fake.blobs[fake.root])
    root.entries = [Entry(e.hash, "0", e.id, e.subfiles, e.size) for e in root.entries]
    data = root.serialize(".")
    fake.root = sha256(data)
    fake.blobs[fake.root] = data
    t._doc_cache.clear()

    assert [d.uuid for d in await t.list_folder("Inbox")] == [u]


def _counting(fake):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return fake.handler(request)

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _transport(fake, store=None, client=None):
    client = client or httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return CloudTransport("device", "https://auth.test", "https://sync.test", client=client, cache=store)


async def test_unchanged_root_costs_one_request():
    fake = FakeCloud()
    seen, client = _counting(fake)
    t = _transport(fake, SQLiteStore(":memory:"), client)
    u = await t.upload_pdf(sample_pdf(), "Spec", "Outbox")
    await t.list_folder("Outbox")

    seen.clear()
    assert [d.uuid for d in await t.list_folder("Outbox")] == [u]
    assert seen == [("GET", "/sync/v4/root")]


async def test_restart_reads_indexes_from_the_store():
    fake, store = FakeCloud(), SQLiteStore(":memory:")
    first = _transport(fake, store)
    u = await first.upload_pdf(sample_pdf(), "Spec", "Outbox")

    # A new process (restart, or the other container) sharing the store.
    seen, client = _counting(fake)
    reads = []
    get_many = store.cache_get_many
    store.cache_get_many = lambda keys: reads.append(len(keys)) or get_many(keys)
    second = _transport(fake, store, client)
    assert [d.uuid for d in await second.list_folder("Outbox")] == [u]
    assert [p for m, p in seen if p.startswith("/sync/v3/files/")] == []
    # Root index, then the doc indexes in one batch, then their metadata in one batch.
    assert len(reads) == 3
    # Payloads are not cached: a download still goes to the cloud.
    assert (await second.download((await second.list_folder("Outbox"))[0])).pdf
    assert any(p.startswith("/sync/v3/files/") for m, p in seen)


async def test_changed_root_fetches_only_changed_entries():
    fake = FakeCloud()
    ours, tablet = _transport(fake, SQLiteStore(":memory:")), _transport(fake, SQLiteStore(":memory:"))
    for i in range(3):
        await ours.upload_pdf(sample_pdf(), f"Spec {i}", "Outbox")
    docs = await ours.list_folder("Outbox")

    # Another device moves one document into a new folder.
    await tablet.move(docs[0], "Archive")

    before = ours.blob_fetches
    assert len(await ours.list_folder("Outbox")) == 2
    # New root index + the moved doc's index and metadata + the new folder's index and metadata.
    assert ours.blob_fetches - before == 5


async def test_store_keeps_only_blobs_of_the_current_root():
    fake, store = FakeCloud(), SQLiteStore(":memory:")
    t = _transport(fake, store)
    await t.upload_pdf(sample_pdf(), "Spec", "Inbox")
    await t.move((await t.list_folder("Inbox"))[0], "Outbox")
    await t.list_folder("Outbox")

    root_hash, _, root = await t.get_root()
    live = {root_hash}
    for e in root.entries:
        live.add(e.hash)
        live |= {f.hash for f in Index.parse(fake.blobs[e.hash]).entries if f.id.endswith((".metadata", ".content"))}
    assert {r["key"] for r in store._fetch("SELECT key FROM cache")} == {"cloud.blob:" + h for h in live}


async def test_cache_failure_falls_back_to_the_cloud():
    class Broken:
        def __getattr__(self, name):
            def fail(*a, **kw):
                raise RuntimeError("db down")
            return fail

    fake = FakeCloud()
    t = _transport(fake, Broken())
    u = await t.upload_pdf(sample_pdf(), "Spec", "Inbox")
    assert [d.uuid for d in await t.list_folder("Inbox")] == [u]
