"""In-memory transport and interpreter for pipeline tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

from inkbridge.core.cloud import RemoteDoc, pdf_content
from inkbridge.core.rmdoc import RmDoc


class MemoryTransport:
    def __init__(self):
        self.docs: dict[str, tuple[RemoteDoc, RmDoc]] = {}
        self.moves = 0

    async def aclose(self):
        pass

    async def ensure_folder(self, name):
        return name

    def _state(self, uuid):
        r, doc = self.docs[uuid]
        prefix = uuid + "/"
        pages = {n[len(prefix):-3]: hashlib.sha256(b).hexdigest()
                 for n, b in doc.files.items() if n.startswith(prefix) and n.endswith(".rm")}
        return replace(r, pages=pages, location=f"/{r.parent}" if r.parent else "/")

    async def list_folder(self, name):
        return [self._state(u) for u, (r, _) in self.docs.items() if r.parent == name]

    async def inspect(self, uuids):
        return {u: self._state(u) for u in uuids if u in self.docs and self.docs[u][0].parent != "trash"}

    async def trash(self, uuid):
        if uuid not in self.docs or self.docs[uuid][0].parent == "trash":
            raise FileNotFoundError(uuid)
        r, doc = self.docs[uuid]
        self.docs[uuid] = (replace(r, parent="trash", version=r.version + "t"), doc)

    async def upload_pdf(self, pdf, visible_name, folder):
        u = str(uuid4())
        page = str(uuid4())
        content = pdf_content(1)
        content["cPages"] = {"pages": [{"id": page, "redir": {"value": 0}}]}
        doc = RmDoc(u, {f"{u}.pdf": pdf, f"{u}.content": json.dumps(content).encode(),
                        f"{u}.metadata": json.dumps({"visibleName": visible_name}).encode()})
        self.docs[u] = (RemoteDoc(u, visible_name, folder, version="v1"), doc)
        return u

    async def upload_rmdoc(self, rmdoc, visible_name, folder):
        u = str(uuid4())
        doc = rmdoc.renamed(u)
        doc.files[f"{u}.metadata"] = json.dumps({**rmdoc.metadata, "visibleName": visible_name}).encode()
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
        r, doc = self.docs[remote.uuid]
        self.moves += 1
        self.docs[remote.uuid] = (RemoteDoc(r.uuid, r.name, folder, version=f"v{self.moves + 1}"), doc)

    # test helpers
    def annotate(self, uuid, rm_bytes, move_to="Outbox"):
        """Ink the first page; move_to=None leaves the doc where it is."""
        r, doc = self.docs[uuid]
        page = doc.page_ids()[0]
        doc.files[f"{uuid}/{page}.rm"] = rm_bytes
        self.docs[uuid] = (RemoteDoc(r.uuid, r.name, move_to or r.parent, version=r.version + "a"), doc)

    def open(self, uuid):
        """What opening a doc does: a new version (metadata), the same ink."""
        r, doc = self.docs[uuid]
        self.docs[uuid] = (replace(r, version=r.version + "o"), doc)


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
