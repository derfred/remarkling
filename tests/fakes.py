"""In-memory transport and interpreter for pipeline tests."""

from __future__ import annotations

import json
from uuid import uuid4

from inkbridge.core.transport.base import LibraryEntry, RemoteDoc, RmDoc, pdf_content


class MemoryTransport:
    """Documents and folders in memory. Top-level folders made by ensure_folder use their name as UUID."""

    def __init__(self):
        self.docs: dict[str, tuple[RemoteDoc, RmDoc]] = {}
        self.folders: dict[str, dict] = {}  # uuid -> metadata
        self.moves = 0

    async def aclose(self):
        pass

    async def ensure_folder(self, name):
        if name not in self.folders:
            self.folders[name] = {"visibleName": name, "type": "CollectionType", "parent": ""}
        return name

    async def create_folder(self, name, parent):
        u = str(uuid4())
        self.folders[u] = {"visibleName": name, "type": "CollectionType", "parent": parent}
        return u

    async def entries(self):
        out = [LibraryEntry(u, "f-" + json.dumps(m, sort_keys=True), dict(m), {}) for u, m in self.folders.items()]
        for r, doc in self.docs.values():
            meta = {"type": "DocumentType", **doc.metadata, "parent": r.parent}
            out.append(LibraryEntry(r.uuid, r.version, meta, doc.content))
        return out

    async def update(self, doc_uuid, metadata=None, content=None):
        if doc_uuid in self.folders:
            self.folders[doc_uuid] = {**self.folders[doc_uuid], **(metadata or {})}
            return
        if doc_uuid not in self.docs:
            raise FileNotFoundError(doc_uuid)
        r, doc = self.docs[doc_uuid]
        meta = {**doc.metadata, **(metadata or {})}
        doc.files[f"{doc_uuid}.metadata"] = json.dumps(meta).encode()
        if content:
            doc.files[f"{doc_uuid}.content"] = json.dumps({**doc.content, **content}).encode()
        self.moves += 1
        self.docs[doc_uuid] = (RemoteDoc(r.uuid, meta.get("visibleName", r.name), meta.get("parent", r.parent),
                                         version=f"v{self.moves + 1}"), doc)

    async def list_folder(self, name):
        return [r for r, _ in self.docs.values() if r.parent == name]

    async def upload_pdf(self, pdf, visible_name, folder, parent=None):
        if parent is None:
            parent = await self.ensure_folder(folder)
        folder = parent
        u = str(uuid4())
        page = str(uuid4())
        content = pdf_content(1)
        content["cPages"] = {"pages": [{"id": page, "redir": {"value": 0}}]}
        doc = RmDoc(u, {f"{u}.pdf": pdf, f"{u}.content": json.dumps(content).encode(),
                        f"{u}.metadata": json.dumps({"visibleName": visible_name, "parent": folder}).encode()})
        self.docs[u] = (RemoteDoc(u, visible_name, folder, version="v1"), doc)
        return u

    async def download(self, remote):
        if remote.uuid not in self.docs:
            raise FileNotFoundError(remote.uuid)
        return self.docs[remote.uuid][1]

    async def download_file(self, doc_uuid, name):
        try:
            return self.docs[doc_uuid][1].files[name]
        except KeyError:
            raise FileNotFoundError(name) from None

    async def move(self, remote, folder):
        await self.update(remote.uuid, metadata={"parent": await self.ensure_folder(folder)})

    # test helpers
    def annotate(self, uuid, rm_bytes, move_to="Outbox"):
        r, doc = self.docs[uuid]
        page = doc.page_ids()[0]
        doc.files[f"{uuid}/{page}.rm"] = rm_bytes
        if move_to is not None:
            doc.files[f"{uuid}.metadata"] = json.dumps({**doc.metadata, "parent": move_to}).encode()
        self.docs[uuid] = (RemoteDoc(r.uuid, r.name, move_to if move_to is not None else r.parent,
                                     version=r.version + "a"), doc)

    def add(self, rmdoc, parent=""):
        """Put an existing RmDoc (e.g. rmfixtures.notebook) into the library."""
        meta = {**rmdoc.metadata, "parent": parent}
        rmdoc.files[f"{rmdoc.uuid}.metadata"] = json.dumps(meta).encode()
        self.docs[rmdoc.uuid] = (RemoteDoc(rmdoc.uuid, meta.get("visibleName", ""), parent, version="v1"), rmdoc)
        return rmdoc.uuid


class FakeInterpreter:
    def __init__(self):
        self.calls = 0

    async def interpret(self, result, original_pdf, title):
        self.calls += 1
        return {
            "highlights": result.highlights,
            "marks": [{"kind": "comment", "anchor_text": "blast radius", "note": "define",
                       "instruction": "Explain blast radius", "confidence": 0.8}],
            "free_notes": [],
            "summary": f"Feedback on {title}.",
        }
