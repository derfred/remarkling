import json

import httpx
import pytest

from inkbridge.core.transport.cloud import CloudTransport, Entry, Index, crc32c, sha256
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
