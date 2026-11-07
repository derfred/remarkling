"""Transport protocol: everything Ink Bridge needs from the tablet, independent of cloud vs SSH."""

from __future__ import annotations

import io
import json
import time
import uuid as uuid_lib
import zipfile
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class RemoteDoc:
    uuid: str
    name: str
    parent: str  # folder UUID, "" for root
    # Changes whenever the document changes (blob hash on cloud, lastModified on SSH).
    version: str = ""
    last_modified: float = 0.0


@dataclass
class RmDoc:
    """The unpacked contents of a .rmdoc: a document's files keyed by name relative to its UUID."""

    uuid: str
    files: dict[str, bytes] = field(default_factory=dict)  # "<uuid>.pdf", "<uuid>/<page>.rm", ...

    @property
    def metadata(self) -> dict:
        return json.loads(self.files.get(f"{self.uuid}.metadata", b"{}"))

    @property
    def content(self) -> dict:
        return json.loads(self.files.get(f"{self.uuid}.content", b"{}"))

    @property
    def pdf(self) -> bytes | None:
        return self.files.get(f"{self.uuid}.pdf")

    def page_ids(self) -> list[str]:
        """Page IDs in display order (v6 cPages, falling back to the older pages list)."""
        content = self.content
        cpages = content.get("cPages", {}).get("pages")
        if cpages:
            return [p["id"] for p in cpages if not p.get("deleted")]
        return list(content.get("pages", []))

    def pdf_page_index(self) -> dict[str, int]:
        """Page ID -> index of the PDF page it sits on."""
        content = self.content
        cpages = content.get("cPages", {}).get("pages")
        if cpages:
            out = {}
            for i, p in enumerate(cpages):
                redir = p.get("redir", {}).get("value")
                out[p["id"]] = int(redir) if redir is not None else i
            return out
        return {pid: i for i, pid in enumerate(content.get("pages", []))}

    def rm_file(self, page_id: str) -> bytes | None:
        return self.files.get(f"{self.uuid}/{page_id}.rm")

    def to_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in sorted(self.files.items()):
                z.writestr(name, data)
        return buf.getvalue()

    @classmethod
    def from_zip(cls, data: bytes) -> "RmDoc":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
        meta = next((n for n in files if n.endswith(".metadata") and "/" not in n), None)
        if meta is None:
            raise ValueError("not an .rmdoc: no .metadata file")
        return cls(uuid=meta.removesuffix(".metadata"), files=files)


class Transport(Protocol):
    async def ensure_folder(self, name: str) -> str:
        """Return the UUID of a top-level folder, creating it if missing."""

    async def list_folder(self, name: str) -> list[RemoteDoc]: ...

    async def upload_pdf(self, pdf: bytes, visible_name: str, folder: str) -> str:
        """Upload a PDF into a top-level folder; return the new document UUID."""

    async def download(self, doc: RemoteDoc) -> RmDoc: ...

    async def move(self, doc: RemoteDoc, folder: str) -> None: ...

    async def aclose(self) -> None: ...


def new_uuid() -> str:
    return str(uuid_lib.uuid4())


def now_ms() -> str:
    return str(int(time.time() * 1000))


def document_metadata(name: str, parent: str, kind: str = "DocumentType") -> dict:
    return {
        "createdTime": now_ms(),
        "deleted": False,
        "lastModified": now_ms(),
        "lastOpened": "0",
        "lastOpenedPage": 0,
        "metadatamodified": False,
        "modified": False,
        "parent": parent,
        "pinned": False,
        "synced": True,
        "type": kind,
        "version": 0,
        "visibleName": name,
    }


def pdf_content(page_count: int) -> dict:
    # fitToWidth so tall single-page PDFs open as one scrolling column.
    return {
        "coverPageNumber": 0,
        "documentMetadata": {},
        "extraMetadata": {},
        "fileType": "pdf",
        "fontName": "",
        "formatVersion": 1,
        "lineHeight": -1,
        "margins": 125,
        "orientation": "portrait",
        "pageCount": page_count,
        "sizeInBytes": "0",
        "tags": [],
        "textAlignment": "justify",
        "textScale": 1,
        "zoomMode": "fitToWidth",
    }
