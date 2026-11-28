"""Upload grants: short-lived, single-use URLs that take a file's bytes straight to push_document.

An agent with a shell asks for one over MCP (`create_upload`) and sends the file with
`curl -T file.pdf <url>`, so large files never pass through the model's context. The URL is the
credential: its token is a random secret, and the store keeps only the token's SHA-256 with the
grant (title, kind, expiry). Claiming a grant is one conditional UPDATE, so a URL works once,
across the server and poller containers and across restarts, and expires after UPLOAD_TTL_S.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .core.store import Store

KINDS = ("pdf", "html", "markdown", "rmdoc")
# Expired grants stay this long, as a record of what was uploaded, then are deleted.
KEEP_S = 7 * 86400


class UploadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Grant:
    title: str
    kind: str  # pdf | html | markdown | rmdoc, declared by create_upload
    ready_selector: str | None
    expires: float
    token_hash: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Uploads:
    def __init__(self, store: Store, ttl_s: float = 900):
        self.store = store
        self.ttl_s = ttl_s

    def create(self, title: str, kind: str = "pdf", ready_selector: str | None = None) -> tuple[str, Grant]:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        token = secrets.token_urlsafe(24)
        t = time.time()
        grant = Grant(title, kind, ready_selector, t + self.ttl_s, token_hash(token))
        self.store.upload_prune(t - KEEP_S)
        self.store.upload_create(grant.token_hash, title, kind, ready_selector, t, grant.expires)
        return token, grant

    def claim(self, token: str) -> Grant:
        """The grant behind a token, claimed for one upload; an UploadError if there is none to claim."""
        h = token_hash(token)
        claimed = self.store.upload_claim(h, time.time())
        row = self.store.upload_get(h)
        if row is None:
            raise UploadError(403, "invalid upload URL")
        if not claimed:
            if row["claimed_at"] is not None:
                raise UploadError(410, "upload URL already used; ask for a new one with create_upload")
            raise UploadError(410, "upload URL expired; ask for a new one with create_upload")
        return Grant(row["title"], row["kind"], row["ready_selector"], row["expires_at"], h)

    def release(self, grant: Grant) -> None:
        """Make a claimed grant usable again: the upload failed before anything reached the tablet."""
        self.store.upload_release(grant.token_hash)

    def complete(self, grant: Grant, doc_id: str) -> None:
        self.store.upload_done(grant.token_hash, doc_id)


def upload_content(data: bytes, kind: str) -> dict[str, str | bytes]:
    """push_document's content argument for an upload of the grant's kind; checks the body fits it."""
    if kind == "pdf":
        if data[:5] != b"%PDF-":
            raise UploadError(415, 'not a PDF; for HTML or Markdown ask create_upload with kind="html" or "markdown"')
        return {"pdf": data}
    if kind == "rmdoc":
        if data[:4] != b"PK\x03\x04":
            raise UploadError(415, "not an .rmdoc (a zip archive); for a PDF ask create_upload with kind=\"pdf\"")
        return {"rmdoc": data}
    try:
        return {kind: data.decode("utf-8-sig")}
    except UnicodeDecodeError as e:
        raise UploadError(415, f"not UTF-8 text, which kind {kind!r} needs") from e


def get_uploads() -> Uploads:
    from .config import get_settings
    from .core.pipeline import get_bridge

    return Uploads(get_bridge().store, get_settings().upload_ttl_s)
