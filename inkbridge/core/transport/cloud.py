"""reMarkable cloud transport: an rmapi-style client for the sync 1.5 ("sync15") blob protocol.

The protocol is unofficial. Storage is a content-addressed blob store: a root index lists one
entry per document, each document index lists its files (.metadata, .content, .pdf, page .rm
files). Writing means uploading new blobs, then swapping the root hash with a generation check.
A root swap with `broadcast: true` makes the cloud send a "SyncComplete" message to every client
on the notifications websocket, which is how the poller learns that the tablet has synced.
Hosts and index details are the parts most likely to change; verify them during M0.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .base import Cache, RemoteDoc, RmDoc, document_metadata, new_uuid, now_ms, pdf_content

log = logging.getLogger(__name__)

DOC_TYPE = "80000000"
FILE_TYPE = "0"
# The cloud rejects new v3 root indexes; document indexes stay v3 (hash of the sorted entry hashes).
ROOT_SCHEMA = "4"
DOC_SCHEMA = "3"


class CloudError(RuntimeError):
    pass


@dataclass
class Entry:
    hash: str
    type: str
    id: str
    subfiles: int
    size: int

    def line(self) -> str:
        return f"{self.hash}:{self.type}:{self.id}:{self.subfiles}:{self.size}"


@dataclass
class Index:
    schema: str
    entries: list[Entry]

    @classmethod
    def parse(cls, data: bytes) -> "Index":
        lines = data.decode().splitlines()
        if not lines:
            return cls("3", [])
        schema = lines[0].strip()
        body = lines[1:]
        if schema == "4" and body and body[0].startswith("0:"):
            body = body[1:]  # summary line "0:<id or .>:<count>:<size>"
        entries = []
        for line in body:
            if not line.strip():
                continue
            h, t, i, n, s = line.split(":", 4)
            entries.append(Entry(h, t, i, int(n), int(s)))
        return cls(schema, entries)

    def serialize(self, own_id: str = ".") -> bytes:
        entries = sorted(self.entries, key=lambda e: e.id)
        lines = [self.schema]
        if self.schema == "4":
            lines.append(f"0:{own_id}:{len(entries)}:{sum(e.size for e in entries)}")
        lines += [e.line() for e in entries]
        return ("\n".join(lines) + "\n").encode()

    def hash(self, data: bytes) -> str:
        if self.schema == "3":
            # Schema 3: hash of the sorted entry hashes, not of the index file.
            h = hashlib.sha256()
            for e in sorted(self.entries, key=lambda e: e.id):
                h.update(bytes.fromhex(e.hash))
            return h.hexdigest()
        return hashlib.sha256(data).hexdigest()

    def get(self, entry_id: str) -> Entry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def put(self, entry: Entry) -> None:
        self.entries = [e for e in self.entries if e.id != entry.id] + [entry]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _crc32c_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC32C = _crc32c_table()


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = _CRC32C[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


async def register_device(code: str, auth_host: str) -> str:
    """Exchange a one-time code from my.remarkable.com/device/desktop/connect for a device token."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{auth_host}/token/json/2/device/new",
            json={"code": code, "deviceDesc": "desktop-linux", "deviceID": new_uuid()},
        )
        r.raise_for_status()
        return r.text.strip()


# Index and metadata blobs: small, fetched on every listing, worth caching.
# Payloads (.pdf, .rm, ...) are fetched once per document and the pipeline keeps its own copy.
_CACHED_SUFFIXES = (".docSchema", ".metadata", ".content")
# Key prefix of cached blobs in the document store's cache table.
CACHE_PREFIX = "cloud.blob:"
NOTIFICATIONS_PATH = "/notifications/ws/json/1"


def default_notifications_url(sync_host: str) -> str:
    """wss://<sync host>/notifications/ws/json/1, the endpoint rmapi and rmfakecloud use."""
    host = sync_host.rstrip("/")
    for http, ws in (("https://", "wss://"), ("http://", "ws://")):
        if host.startswith(http):
            return ws + host.removeprefix(http) + NOTIFICATIONS_PATH
    return host + NOTIFICATIONS_PATH


def parse_notification(raw: str | bytes) -> dict:
    """The attributes of a notification: {"event": "SyncComplete", "sourceDeviceID": ..., ...}.

    Messages look like {"message": {"attributes": {...}, "messageId": ..., "publishTime": ...},
    "subscription": ...}. Anything else still counts as "something changed", so it parses to {}.
    """
    try:
        msg = json.loads(raw)
        attrs = msg.get("message", {}).get("attributes", {})
        return dict(attrs) if isinstance(attrs, dict) else {}
    except (ValueError, AttributeError):
        return {}


class BlobCache:
    """Blobs keyed by hash, in the document store (SQLite or Postgres), which the server and the
    poller share and which survives restarts. A blob never changes once stored, so entries never
    go stale; `retain` drops the ones the current root no longer references.

    The cache only saves requests: if the store fails, callers fall back to the cloud.
    """

    def __init__(self, store: Cache | None):
        self.store = store
        self._prefetched: dict[str, bytes] = {}

    def _call(self, what: str, fn, default=None):
        if self.store is None:
            return default
        try:
            return fn(self.store)
        except Exception:
            log.warning("blob cache %s failed; continuing without it", what, exc_info=True)
            return default

    def prefetch(self, hashes: list[str]) -> None:
        """Load many blobs with one query, so a listing does not cost a query per blob."""
        missing = [h for h in hashes if h not in self._prefetched]
        if missing:
            found = self._call("read", lambda s: s.cache_get_many([CACHE_PREFIX + h for h in missing]), {})
            for key, value in found.items():
                self._prefetched[key.removeprefix(CACHE_PREFIX)] = base64.b64decode(value)

    def peek(self, h: str) -> bytes | None:
        return self._prefetched.get(h)

    def get(self, h: str) -> bytes | None:
        if h in self._prefetched:
            return self._prefetched.pop(h)
        found = self._call("read", lambda s: s.cache_get_many([CACHE_PREFIX + h]), {})
        value = found.get(CACHE_PREFIX + h)
        return base64.b64decode(value) if value is not None else None

    def put(self, h: str, data: bytes) -> None:
        self._call("write", lambda s: s.cache_put_many({CACHE_PREFIX + h: base64.b64encode(data).decode()}))

    def retain(self, hashes: set[str]) -> None:
        self._prefetched.clear()
        self._call("prune", lambda s: s.cache_retain(CACHE_PREFIX, {CACHE_PREFIX + h for h in hashes}))


class CloudTransport:
    def __init__(
        self,
        device_token: str,
        auth_host: str,
        sync_host: str,
        client: httpx.AsyncClient | None = None,
        cache: Cache | None = None,
        notifications_url: str = "",
    ):
        if not device_token:
            raise CloudError("RM_DEVICE_TOKEN is not set; run `inkbridge register <code>` first")
        self.device_token = device_token
        self.auth_host = auth_host.rstrip("/")
        self.sync_host = sync_host.rstrip("/")
        # "" derives the URL from the sync host; "off" disables notifications.
        self.notifications_url = notifications_url or default_notifications_url(self.sync_host)
        self.client = client or httpx.AsyncClient(timeout=60)
        self._user_token: str | None = None
        self._lock = asyncio.Lock()
        self.cache = BlobCache(cache)
        # doc index hash -> (index, metadata) for the entries of the last listed root.
        self._doc_cache: dict[str, tuple[Index, dict]] = {}
        # (root hash, live entries with metadata): an unchanged root needs no blob fetches at all.
        self._listing: tuple[str, list[tuple[Entry, dict]]] | None = None
        self.blob_fetches = 0

    async def aclose(self) -> None:
        await self.client.aclose()

    # --- HTTP -------------------------------------------------------------------------------

    async def _refresh_user_token(self) -> str:
        r = await self.client.post(
            f"{self.auth_host}/token/json/2/user/new",
            headers={"Authorization": f"Bearer {self.device_token}"},
        )
        if r.status_code != 200:
            raise CloudError(f"user token refresh failed: {r.status_code} {r.text[:200]}")
        self._user_token = r.text.strip()
        return self._user_token

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        token = self._user_token or await self._refresh_user_token()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {token}", **kw.pop("headers", {})}
            r = await self.client.request(method, f"{self.sync_host}{path}", headers=headers, **kw)
            if r.status_code == 401 and attempt == 0:
                token = await self._refresh_user_token()
                kw["headers"] = {k: v for k, v in headers.items() if k != "Authorization"}
                continue
            return r
        return r

    @property
    def notifications_enabled(self) -> bool:
        return self.notifications_url.lower() not in ("off", "none", "false", "0")

    async def _connect_notifications(self) -> ClientConnection:
        token = self._user_token or await self._refresh_user_token()
        for attempt in range(2):
            try:
                return await connect(
                    self.notifications_url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    open_timeout=30,
                    max_size=2**20,
                )
            except InvalidStatus as e:
                status = e.response.status_code
                if status == 401 and attempt == 0:
                    token = await self._refresh_user_token()
                    continue
                raise CloudError(f"notifications websocket: {status}") from e
        raise AssertionError("unreachable")

    @asynccontextmanager
    async def notifications(self) -> AsyncIterator[AsyncIterator[dict]]:
        """Connect to the notifications websocket; yield an iterator over its messages' attributes.

        The iterator ends when the connection closes (the caller reconnects). Pings keep the
        connection alive and detect a dead one.
        """
        if not self.notifications_enabled:
            raise CloudError("notifications are disabled (RM_NOTIFICATIONS_URL=off)")
        ws = await self._connect_notifications()

        async def events() -> AsyncIterator[dict]:
            try:
                async for raw in ws:
                    yield parse_notification(raw)
            except ConnectionClosed as e:
                log.info("notifications websocket closed: %s", e)

        try:
            yield events()
        finally:
            await ws.close()

    async def get_blob(self, h: str, filename: str) -> bytes:
        cacheable = filename.endswith(_CACHED_SUFFIXES)
        if cacheable and (data := self.cache.get(h)) is not None:
            return data
        self.blob_fetches += 1
        # The cloud answers 400 unless rm-filename names the blob (e.g. "root.docSchema", "<uuid>.metadata").
        r = await self._request("GET", f"/sync/v3/files/{h}", headers={"rm-filename": filename})
        if r.status_code != 200:
            raise CloudError(f"GET blob {filename} ({h}): {r.status_code} {r.text[:200]}")
        if cacheable:
            self.cache.put(h, r.content)
        return r.content

    async def put_blob(self, data: bytes, filename: str, h: str | None = None) -> str:
        h = h or sha256(data)
        crc = base64.b64encode(struct.pack(">I", crc32c(data))).decode()
        r = await self._request(
            "PUT",
            f"/sync/v3/files/{h}",
            content=data,
            headers={
                "rm-filename": filename,
                "Content-Type": "application/octet-stream",
                "x-goog-hash": f"crc32c={crc}",
            },
        )
        if r.status_code not in (200, 201, 202, 204):
            raise CloudError(f"PUT blob {filename}: {r.status_code} {r.text[:200]}")
        if filename.endswith(_CACHED_SUFFIXES):
            self.cache.put(h, data)
        return h

    async def get_root(self) -> tuple[str, int, Index]:
        r = await self._request("GET", "/sync/v4/root")
        if r.status_code != 200:
            raise CloudError(f"GET root: {r.status_code} {r.text[:200]}")
        info = r.json()
        root_hash, generation = info["hash"], int(info.get("generation", 0))
        index = Index.parse(await self.get_blob(root_hash, "root.docSchema")) if root_hash else Index(ROOT_SCHEMA, [])
        return root_hash, generation, index

    async def _put_root(self, index: Index, generation: int) -> bool:
        index = Index(ROOT_SCHEMA, index.entries)  # upgrade a v3 root on first write
        data = index.serialize(".")
        h = await self.put_blob(data, "root.docSchema", index.hash(data))
        r = await self._request("PUT", "/sync/v3/root", json={"broadcast": True, "generation": generation, "hash": h})
        if r.status_code == 412:
            return False
        if r.status_code not in (200, 201):
            raise CloudError(f"PUT root: {r.status_code} {r.text[:200]}")
        return True

    # --- documents --------------------------------------------------------------------------

    async def _doc(self, entry: Entry) -> tuple[Index, dict]:
        cached = self._doc_cache.get(entry.hash)
        if cached:
            return cached
        index = Index.parse(await self.get_blob(entry.hash, f"{entry.id}.docSchema"))
        meta_entry = next((e for e in index.entries if e.id.endswith(".metadata")), None)
        meta = json.loads(await self.get_blob(meta_entry.hash, meta_entry.id)) if meta_entry else {}
        self._doc_cache[entry.hash] = (index, meta)
        return index, meta

    def _prefetch(self, entries: list[Entry]) -> None:
        """Load the cached index, then metadata, blobs of entries not in memory: two queries in all."""
        missing = [e for e in entries if e.hash not in self._doc_cache]
        self.cache.prefetch([e.hash for e in missing])
        meta_hashes = []
        for e in missing:
            data = self.cache.peek(e.hash)
            if data is not None:
                meta_hashes += [f.hash for f in Index.parse(data).entries if f.id.endswith(".metadata")]
        self.cache.prefetch(meta_hashes)

    async def _all_docs(self, root_hash: str, root: Index) -> list[tuple[Entry, dict]]:
        if self._listing and self._listing[0] == root_hash:
            log.debug("root %s unchanged, reusing listing", root_hash[:12])
            return self._listing[1]
        fetches = self.blob_fetches
        self._prefetch(root.entries)
        sem = asyncio.Semaphore(8)

        async def one(e: Entry):
            async with sem:
                _, meta = await self._doc(e)
                return e, meta

        # Every root entry is a document or folder; v4 roots may type them "0" instead of DOC_TYPE.
        results = await asyncio.gather(*(one(e) for e in root.entries))
        # Keep only the current entries so neither the memo nor the store grows with every edit.
        # A concurrent lister on a newer root may lose blobs it just cached; it refetches them.
        current = {e.hash for e in root.entries}
        self._doc_cache = {h: v for h, v in self._doc_cache.items() if h in current}
        live = {root_hash} | current
        for index, _ in self._doc_cache.values():
            live |= {f.hash for f in index.entries if f.id.endswith(_CACHED_SUFFIXES)}
        self.cache.retain(live)
        docs = [(e, m) for e, m in results if not m.get("deleted") and m.get("parent") != "trash"]
        self._listing = (root_hash, docs)
        log.info("listed root %s: %d entries, %d blobs fetched", root_hash[:12], len(root.entries), self.blob_fetches - fetches)
        return docs

    async def sync(self) -> dict:
        """Fill the blob cache for the current root: every index, .metadata and .content blob.

        A listing caches indexes and metadata only, and skips the store entirely while the root
        is unchanged; this re-reads the store and fetches whatever is missing, .content included
        (which carries page counts and file types). Blobs already cached cost no request.
        """
        fetches = self.blob_fetches
        root_hash, _, root = await self.get_root()
        self._listing = None
        self._doc_cache.clear()  # go through the store, so rows missing there are refetched
        docs = await self._all_docs(root_hash, root)
        # By hash: identical .content files (e.g. one-page PDFs) are one blob.
        contents = list({f.hash: f for index, _ in self._doc_cache.values() for f in index.entries
                         if f.id.endswith(".content")}.values())
        self.cache.prefetch([f.hash for f in contents])
        sem = asyncio.Semaphore(8)

        async def one(f: Entry):
            async with sem:
                await self.get_blob(f.hash, f.id)

        await asyncio.gather(*(one(f) for f in contents))
        log.info("synced root %s: %d entries, %d blobs fetched", root_hash[:12], len(root.entries),
                 self.blob_fetches - fetches)
        return {"root": root_hash, "entries": len(root.entries), "documents": len(docs),
                "fetched": self.blob_fetches - fetches}

    async def _upload_doc_files(self, doc_uuid: str, files: dict[str, bytes]) -> Entry:
        entries = []
        for name, data in files.items():
            h = await self.put_blob(data, name)
            entries.append(Entry(h, FILE_TYPE, name, 0, len(data)))
        index = Index(DOC_SCHEMA, entries)
        data = index.serialize(doc_uuid)
        h = await self.put_blob(data, f"{doc_uuid}.docSchema", index.hash(data))
        return Entry(h, DOC_TYPE, doc_uuid, len(entries), sum(e.size for e in entries))

    async def _commit(self, mutate) -> None:
        """Apply `mutate(root_index) -> None` (which may upload blobs) and swap the root, retrying on conflicts."""
        async with self._lock:
            for _ in range(5):
                _, generation, root = await self.get_root()
                await mutate(root)
                if await self._put_root(root, generation):
                    return
                log.info("root changed during commit, retrying")
            raise CloudError("could not update root after 5 attempts")

    async def _folder_uuid(self, name: str, docs: list[tuple[Entry, dict]]) -> str | None:
        for e, m in docs:
            if m.get("type") == "CollectionType" and m.get("visibleName") == name and m.get("parent", "") == "":
                return e.id
        return None

    async def ensure_folder(self, name: str) -> str:
        root_hash, _, root = await self.get_root()
        found = await self._folder_uuid(name, await self._all_docs(root_hash, root))
        if found:
            return found
        folder_uuid = new_uuid()

        async def mutate(root: Index):
            files = {
                f"{folder_uuid}.metadata": json.dumps(document_metadata(name, "", "CollectionType")).encode(),
                f"{folder_uuid}.content": b"{}",
            }
            root.put(await self._upload_doc_files(folder_uuid, files))

        await self._commit(mutate)
        return folder_uuid

    async def list_folder(self, name: str) -> list[RemoteDoc]:
        root_hash, _, root = await self.get_root()
        docs = await self._all_docs(root_hash, root)
        folder = await self._folder_uuid(name, docs)
        if folder is None:
            return []
        return [
            RemoteDoc(
                uuid=e.id,
                name=m.get("visibleName", ""),
                parent=folder,
                version=e.hash,
                last_modified=int(m.get("lastModified", "0") or 0) / 1000,
            )
            for e, m in docs
            if m.get("parent") == folder and m.get("type") == "DocumentType"
        ]

    async def upload_pdf(self, pdf: bytes, visible_name: str, folder: str) -> str:
        import pymupdf

        folder_uuid = await self.ensure_folder(folder)
        with pymupdf.open(stream=pdf, filetype="pdf") as d:
            pages = d.page_count
        doc_uuid = new_uuid()
        files = {
            f"{doc_uuid}.metadata": json.dumps(document_metadata(visible_name, folder_uuid)).encode(),
            f"{doc_uuid}.content": json.dumps(pdf_content(pages)).encode(),
            f"{doc_uuid}.pdf": pdf,
            f"{doc_uuid}.pagedata": b"\n".join([b"Blank"] * pages),
        }

        async def mutate(root: Index):
            root.put(await self._upload_doc_files(doc_uuid, files))

        await self._commit(mutate)
        return doc_uuid

    async def download(self, doc: RemoteDoc) -> RmDoc:
        _, _, root = await self.get_root()
        entry = root.get(doc.uuid)
        if entry is None:
            raise CloudError(f"document {doc.uuid} not found")
        index, _ = await self._doc(entry)
        blobs = await asyncio.gather(*(self.get_blob(e.hash, e.id) for e in index.entries))
        return RmDoc(uuid=doc.uuid, files={e.id: b for e, b in zip(index.entries, blobs)})

    async def move(self, doc: RemoteDoc, folder: str) -> None:
        folder_uuid = await self.ensure_folder(folder)

        async def mutate(root: Index):
            entry = root.get(doc.uuid)
            if entry is None:
                raise CloudError(f"document {doc.uuid} not found")
            index, meta = await self._doc(entry)
            meta = {**meta, "parent": folder_uuid, "lastModified": now_ms(), "metadatamodified": True}
            meta_name = f"{doc.uuid}.metadata"
            meta_bytes = json.dumps(meta).encode()
            index = Index(index.schema, list(index.entries))
            index.put(Entry(await self.put_blob(meta_bytes, meta_name), FILE_TYPE, meta_name, 0, len(meta_bytes)))
            data = index.serialize(doc.uuid)
            h = await self.put_blob(data, f"{doc.uuid}.docSchema", index.hash(data))
            root.put(Entry(h, DOC_TYPE, doc.uuid, len(index.entries), sum(e.size for e in index.entries)))

        await self._commit(mutate)
