"""A document's files as the tablet stores them (the contents of an .rmdoc export)."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field


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
