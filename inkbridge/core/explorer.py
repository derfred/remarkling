"""Read-only view of the document store's `cache` table, for the /api/cache routes and the web UI.

The cloud transport caches index, .metadata and .content blobs by hash (`cloud.BlobCache`), and
prunes them to what the current root references. That is enough to rebuild the library: the root
index lists one doc index per document or folder, each doc index names its files, and the
metadata gives names and parents. Payloads (.pdf, .rm) are never cached, so they show as "not
cached". Nothing here talks to the cloud: the view is as fresh as the last listing.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

from .store import Store
from .transport.cloud import CACHE_PREFIX, Index

KINDS = ("root", "index", "metadata", "content", "other")


@dataclass
class Blob:
    key: str
    hash: str | None  # set for cloud blobs
    created_at: float
    raw: str  # JSON text as stored
    data: bytes | None = None  # decoded cloud blob
    kind: str = "other"
    filename: str | None = None  # name the referencing doc index gives it
    doc_uuid: str | None = None
    index: Index | None = None

    @property
    def size(self) -> int:
        return len(self.data) if self.data is not None else len(self.raw)

    def summary(self, names: dict[str, str]) -> dict[str, Any]:
        return {
            "key": self.key,
            "hash": self.hash,
            "kind": self.kind,
            "size": self.size,
            "created_at": self.created_at,
            "filename": self.filename,
            "doc_uuid": self.doc_uuid,
            "doc_name": names.get(self.doc_uuid or ""),
        }


@dataclass
class Node:
    uuid: str
    hash: str
    size: int
    subfiles: int
    index: Index | None = None
    metadata: dict[str, Any] | None = None
    content: dict[str, Any] | None = None
    files: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        return (self.metadata or {}).get("visibleName") or self.uuid

    @property
    def type(self) -> str:
        t = (self.metadata or {}).get("type")
        return "folder" if t == "CollectionType" else "document" if t == "DocumentType" else "unknown"

    @property
    def parent(self) -> str | None:
        return (self.metadata or {}).get("parent") if self.metadata is not None else None


class CacheSnapshot:
    """All cache rows, classified, plus the library the newest cached root describes."""

    def __init__(self, store: Store):
        self.store = store
        self.blobs: dict[str, Blob] = {}
        for row in store.cache_rows():
            key = row["key"]
            blob = Blob(key, key.removeprefix(CACHE_PREFIX) if key.startswith(CACHE_PREFIX) else None,
                        float(row["created_at"]), row["value"])
            if blob.hash is not None:
                blob.data = _decode_blob(row["value"])
            self.blobs[key] = blob
        self._classify()
        self.root = self._find_root()
        self.nodes: dict[str, Node] = {}
        if self.root is not None:
            self._build_library(self.root)
        self.names = {u: n.name for u, n in self.nodes.items()}
        self._link_docs()

    # --- classification ---------------------------------------------------------------------

    def blob_by_hash(self, h: str) -> Blob | None:
        return self.blobs.get(CACHE_PREFIX + h)

    def _classify(self) -> None:
        for blob in self.blobs.values():
            if blob.data is None:
                continue
            index = _parse_index(blob.data)
            if index is not None:
                blob.index = index
                # A root lists documents (ids are bare UUIDs); a doc index lists files (<uuid>.<ext>).
                is_root = all("." not in e.id for e in index.entries) and (
                    index.entries or blob.data.splitlines()[1:2] in ([], [b"0:.:0:0"]))
                blob.kind = "root" if is_root else "index"
                continue
            parsed = _parse_json(blob.data)
            if isinstance(parsed, dict):
                blob.kind = "metadata" if "visibleName" in parsed or "parent" in parsed else "content"
        # Doc indexes name their files: use that over the heuristic.
        for blob in self.blobs.values():
            if blob.kind != "index" or blob.index is None:
                continue
            for e in blob.index.entries:
                target = self.blob_by_hash(e.hash)
                if target is None:
                    continue
                uuid, _, ext = e.id.partition(".")
                target.filename, target.doc_uuid = e.id, uuid.split("/")[0]
                if ext in ("metadata", "content"):
                    target.kind = ext

    def _find_root(self) -> Blob | None:
        # Pruning keeps only the current root's blobs, but a concurrent listing can leave an older
        # root behind for a while; the newest one wins.
        roots = [b for b in self.blobs.values() if b.kind == "root"]
        return max(roots, key=lambda b: (b.created_at, len(b.index.entries)), default=None)

    # --- library ----------------------------------------------------------------------------

    def _build_library(self, root: Blob) -> None:
        for e in root.index.entries:
            node = Node(e.id, e.hash, e.size, e.subfiles)
            idx_blob = self.blob_by_hash(e.hash)
            if idx_blob is not None and idx_blob.index is not None:
                idx_blob.doc_uuid = e.id
                idx_blob.filename = f"{e.id}.docSchema"
                node.index = idx_blob.index
                for f in idx_blob.index.entries:
                    cached = self.blob_by_hash(f.hash)
                    node.files.append({"name": f.id, "hash": f.hash, "size": f.size, "cached": cached is not None})
                    if cached is None or cached.data is None:
                        continue
                    if f.id.endswith(".metadata"):
                        node.metadata = _parse_json(cached.data)
                    elif f.id.endswith(".content"):
                        node.content = _parse_json(cached.data)
            self.nodes[e.id] = node

    def _link_docs(self) -> None:
        self.inkbridge: dict[str, dict[str, Any]] = {}
        for d in self.store.with_rm_uuid():
            self.inkbridge[d.rm_uuid] = {
                "doc_id": d.doc_id, "title": d.title, "status": d.status, "source_kind": d.source_kind,
                "pushed_at": d.pushed_at, "pulled_at": d.pulled_at, "trilium_url": d.trilium_url,
            }

    def node_summary(self, node: Node) -> dict[str, Any]:
        meta, content = node.metadata or {}, node.content or {}
        parent = node.parent
        return {
            "uuid": node.uuid,
            "name": node.name,
            "type": node.type,
            "parent": parent,
            "deleted": bool(meta.get("deleted")) or parent == "trash",
            "pinned": bool(meta.get("pinned")),
            "last_modified": _ms(meta.get("lastModified")),
            "last_opened": _ms(meta.get("lastOpened")),
            "file_type": content.get("fileType") or None,
            "page_count": _page_count(content),
            "tags": [t.get("name") if isinstance(t, dict) else t for t in content.get("tags") or []],
            "size": node.size,
            "files": node.subfiles,
            "hash": node.hash,
            "metadata_cached": node.metadata is not None,
            "children": sum(1 for n in self.nodes.values() if n.parent == node.uuid),
            "inkbridge": self.inkbridge.get(node.uuid),
        }

    def path(self, uuid: str) -> list[dict[str, str]]:
        """Breadcrumbs from the top level down to `uuid`'s parent folder."""
        out, seen = [], set()
        node = self.nodes.get(uuid)
        parent = node.parent if node else None
        while parent and parent not in seen and parent in self.nodes:
            seen.add(parent)
            out.append({"uuid": parent, "name": self.nodes[parent].name})
            parent = self.nodes[parent].parent
        if parent == "trash":
            out.append({"uuid": "trash", "name": "Trash"})
        return out[::-1]

    # --- API payloads -----------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        by_kind = {k: {"count": 0, "bytes": 0} for k in KINDS}
        for b in self.blobs.values():
            by_kind[b.kind]["count"] += 1
            by_kind[b.kind]["bytes"] += b.size
        referenced = self._referenced()
        nodes = list(self.nodes.values())
        times = [b.created_at for b in self.blobs.values()]
        return {
            "entries": len(self.blobs),
            "bytes": sum(b.size for b in self.blobs.values()),
            "by_kind": by_kind,
            "oldest": min(times, default=None),
            "newest": max(times, default=None),
            "root": None if self.root is None else {
                "hash": self.root.hash, "created_at": self.root.created_at, "entries": len(self.root.index.entries),
                "roots_cached": sum(1 for b in self.blobs.values() if b.kind == "root"),
            },
            "library": {
                "documents": sum(1 for n in nodes if n.type == "document"),
                "folders": sum(1 for n in nodes if n.type == "folder"),
                "trashed": sum(1 for n in nodes if n.parent == "trash" or (n.metadata or {}).get("deleted")),
                "uncached": sum(1 for n in nodes if n.metadata is None),
                "tracked": sum(1 for u in self.nodes if u in self.inkbridge),
            },
            "unreferenced": sum(1 for k in self.blobs if k not in referenced),
        }

    def _referenced(self) -> set[str]:
        if self.root is None:
            return set()
        keys = {self.root.key}
        for node in self.nodes.values():
            keys.add(CACHE_PREFIX + node.hash)
            keys |= {CACHE_PREFIX + f["hash"] for f in node.files}
        return keys

    def library(self) -> list[dict[str, Any]]:
        return sorted((self.node_summary(n) for n in self.nodes.values()),
                      key=lambda n: (n["type"] != "folder", n["name"].lower()))

    def node_detail(self, uuid: str) -> dict[str, Any] | None:
        node = self.nodes.get(uuid)
        if node is None:
            return None
        return {
            **self.node_summary(node),
            "path": self.path(uuid),
            "metadata": node.metadata,
            "content": node.content,
            "file_list": node.files,
            "index_key": CACHE_PREFIX + node.hash if self.blob_by_hash(node.hash) else None,
        }

    def blob_list(self, kind: str | None = None, q: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        q = q.strip().lower()
        items = [b.summary(self.names) for b in self.blobs.values() if not kind or b.kind == kind]
        if q:
            items = [i for i in items if any(q in (i[f] or "").lower() for f in ("key", "filename", "doc_name"))]
        items.sort(key=lambda i: (-i["created_at"], i["key"]))
        return {"total": len(items), "items": items[offset:offset + limit]}

    def blob_detail(self, key: str) -> dict[str, Any] | None:
        blob = self.blobs.get(key)
        if blob is None:
            return None
        out = blob.summary(self.names)
        if blob.data is None:
            out["encoding"], out["value"] = "json", _parse_json(blob.raw.encode())
        elif blob.index is not None:
            out["encoding"] = "index"
            out["value"] = blob.data.decode()
            out["index"] = {"schema": blob.index.schema, "entries": [
                {"id": e.id, "hash": e.hash, "type": e.type, "subfiles": e.subfiles, "size": e.size,
                 "cached": self.blob_by_hash(e.hash) is not None,
                 "name": self.names.get(e.id.split(".")[0].split("/")[0])}
                for e in blob.index.entries]}
        elif (parsed := _parse_json(blob.data)) is not None:
            out["encoding"], out["value"] = "json", parsed
        else:
            try:
                out["encoding"], out["value"] = "text", blob.data.decode()
            except UnicodeDecodeError:
                out["encoding"], out["value"] = "base64", base64.b64encode(blob.data[:65536]).decode()
        return out


def _decode_blob(raw: str) -> bytes | None:
    try:
        value = json.loads(raw)
        return base64.b64decode(value, validate=True) if isinstance(value, str) else None
    except (ValueError, binascii.Error):
        return None


def _parse_json(data: bytes) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        return None


def _parse_index(data: bytes) -> Index | None:
    head = data[:2]
    if head not in (b"3\n", b"4\n"):
        return None
    try:
        return Index.parse(data)
    except (ValueError, UnicodeDecodeError):
        return None


def _ms(value: Any) -> float | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v / 1000 if v > 0 else None


def _page_count(content: dict[str, Any]) -> int | None:
    pages = (content.get("cPages") or {}).get("pages")
    if pages:
        return sum(1 for p in pages if not p.get("deleted"))
    if content.get("pages"):
        return len(content["pages"])
    return content.get("pageCount") or None
