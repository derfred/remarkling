"""In-memory transport and interpreter for pipeline tests."""

from __future__ import annotations

import json
from uuid import uuid4

from inkbridge.core.transport.base import RemoteDoc, RmDoc, pdf_content


class MemoryTransport:
    def __init__(self):
        self.docs: dict[str, tuple[RemoteDoc, RmDoc]] = {}
        self.moves = 0

    async def aclose(self):
        pass

    async def ensure_folder(self, name):
        return name

    async def list_folder(self, name):
        return [r for r, _ in self.docs.values() if r.parent == name]

    async def upload_pdf(self, pdf, visible_name, folder):
        u = str(uuid4())
        page = str(uuid4())
        content = pdf_content(1)
        content["cPages"] = {"pages": [{"id": page, "redir": {"value": 0}}]}
        doc = RmDoc(u, {f"{u}.pdf": pdf, f"{u}.content": json.dumps(content).encode(),
                        f"{u}.metadata": json.dumps({"visibleName": visible_name}).encode()})
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
        r, doc = self.docs[uuid]
        page = doc.page_ids()[0]
        doc.files[f"{uuid}/{page}.rm"] = rm_bytes
        self.docs[uuid] = (RemoteDoc(r.uuid, r.name, move_to, version=r.version + "a"), doc)


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
