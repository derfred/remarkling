"""Upload grants: short-lived, single-use URLs that take a file's bytes straight to push_document.

An agent with a shell asks for one over MCP (`create_upload`) and sends the file with
`curl -T file.pdf <url>`, so large files never pass through the model's context. The URL is the
credential: the token is signed (HMAC-SHA256) with a key in DATA_DIR, so a grant needs no
database row, and it expires after UPLOAD_TTL_S. Used tokens are remembered until they expire,
per process (one server container); a restart forgets them, but they still expire.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

KINDS = ("pdf", "html", "markdown")


class UploadError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Grant:
    title: str
    kind: str | None  # pdf | html | markdown; None: from the body and Content-Type
    ready_selector: str | None
    expires: float
    nonce: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_key(path: Path) -> bytes:
    """The signing key, created on first use. The data volume keeps it across restarts."""
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(key)
    tmp.chmod(0o600)
    tmp.replace(path)
    return key


class Uploads:
    def __init__(self, key: bytes, ttl_s: float = 900):
        self.key = key
        self.ttl_s = ttl_s
        self._used: dict[str, float] = {}  # nonce -> expiry
        self._lock = threading.Lock()

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest())

    def create(self, title: str, kind: str | None = None, ready_selector: str | None = None) -> tuple[str, Grant]:
        if kind is not None and kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        grant = Grant(title, kind, ready_selector, time.time() + self.ttl_s, secrets.token_urlsafe(12))
        payload = _b64(json.dumps(asdict(grant), separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}", grant

    def check(self, token: str) -> Grant:
        """The grant a token carries, if its signature holds and it is unexpired and unused."""
        payload, _, sig = token.partition(".")
        if not sig or not hmac.compare_digest(sig, self._sign(payload)):
            raise UploadError(403, "invalid upload URL")
        try:
            grant = Grant(**json.loads(_unb64(payload)))
        except (ValueError, TypeError) as e:
            raise UploadError(403, "invalid upload URL") from e
        if grant.expires < time.time():
            raise UploadError(410, "upload URL expired; ask for a new one with create_upload")
        with self._lock:
            if grant.nonce in self._used:
                raise UploadError(410, "upload URL already used; ask for a new one with create_upload")
        return grant

    def consume(self, grant: Grant) -> None:
        """Mark a grant used. Called after a successful push, so a failed upload can be retried."""
        t = time.time()
        with self._lock:
            self._used = {n: e for n, e in self._used.items() if e >= t}
            if grant.nonce in self._used:
                raise UploadError(410, "upload URL already used; ask for a new one with create_upload")
            self._used[grant.nonce] = grant.expires

    def claim(self, token: str) -> Grant:
        """check() and consume() in one step: the token cannot be used by two uploads at once."""
        grant = self.check(token)
        self.consume(grant)
        return grant

    def release(self, grant: Grant) -> None:
        """Make a claimed grant usable again (the upload failed before anything reached the tablet)."""
        with self._lock:
            self._used.pop(grant.nonce, None)


def sniff_kind(data: bytes, content_type: str, grant_kind: str | None) -> str:
    """pdf by magic bytes; otherwise the grant's kind, then the Content-Type; else an error."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if grant_kind == "pdf":
        raise UploadError(415, "the upload is not a PDF")
    if grant_kind:
        return grant_kind
    ctype = content_type.split(";")[0].strip().lower()
    if ctype in ("text/html", "application/xhtml+xml"):
        return "html"
    if ctype in ("text/markdown", "text/x-markdown", "text/plain"):
        return "markdown"
    head = data[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return "html"
    raise UploadError(415, "unknown file type: send a PDF, or ask create_upload for kind html or markdown")


_uploads: Uploads | None = None


def get_uploads() -> Uploads:
    global _uploads
    if _uploads is None:
        from .config import get_settings

        s = get_settings()
        _uploads = Uploads(load_key(s.data_dir / "upload.key"), s.upload_ttl_s)
    return _uploads
