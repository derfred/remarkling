"""push_document() and poll_outbox(): the two paths every trigger (chat, phone, schedule) calls."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import Settings, get_settings
from . import render
from .interpret import Interpreter, slice_strips
from .overlay import overlay
from .store import Doc, Store, now, open_store
from .transport import RemoteDoc, Transport, make_transport
from .trilium import Trilium

log = logging.getLogger(__name__)


@dataclass
class PushResult:
    doc_id: str
    title: str
    tablet_paths: list[str]
    parts: list[str] = field(default_factory=list)


@dataclass
class PulledDoc:
    doc_id: str
    title: str
    summary: str
    trilium_url: str | None
    marks: int
    external: bool


class InkBridge:
    def __init__(
        self,
        settings: Settings | None = None,
        store: Store | None = None,
        transport: Transport | None = None,
        interpreter: Interpreter | None = None,
        trilium: Trilium | None = None,
    ):
        self.settings = settings or get_settings()
        self.store = store or open_store(self.settings)
        self._transport = transport
        self._interpreter = interpreter
        self._trilium = trilium
        self._poll_lock = asyncio.Lock()

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = make_transport(self.settings)
        return self._transport

    @property
    def interpreter(self) -> Interpreter:
        if self._interpreter is None:
            self._interpreter = Interpreter(self.settings.anthropic_api_key, self.settings.interpret_model)
        return self._interpreter

    @property
    def trilium(self) -> Trilium | None:
        if self._trilium is None and self.settings.trilium_enabled:
            s = self.settings
            self._trilium = Trilium(s.trilium_url, s.trilium_etapi_token, s.trilium_parent_note, s.trilium_public_url)
        return self._trilium

    async def aclose(self) -> None:
        if self._transport is not None:
            await self._transport.aclose()
        if self._trilium is not None:
            await self._trilium.aclose()

    def doc_dir(self, doc_id: str) -> Path:
        return self.settings.data_dir / "docs" / doc_id

    # --- push -------------------------------------------------------------------------------

    async def push_document(
        self,
        title: str | None = None,
        *,
        html: str | None = None,
        markdown: str | None = None,
        pdf: bytes | None = None,
        url: str | None = None,
        ready_selector: str | None = None,
        source_kind: str | None = None,
        source_ref: str | None = None,
    ) -> PushResult:
        given = [k for k, v in {"html": html, "markdown": markdown, "pdf": pdf, "url": url}.items() if v]
        if len(given) != 1:
            raise ValueError("pass exactly one of html, markdown, pdf or url")
        kind = given[0]
        title = (title or "").strip() or _default_title(url) or "Untitled"
        base_id = self.store.mint_id()

        if kind == "pdf":
            parts = [render.RenderedPart(base_id, render.passthrough_pdf(pdf, base_id, title), 0, 1)]
        else:
            if kind == "markdown":
                html = render.markdown_to_html(markdown, title)
            parts = await render.render_tall_pdfs(
                base_id, title, html=html if kind != "url" else None, url=url, ready_selector=ready_selector
            )

        paths = []
        for part in parts:
            name = render.tablet_title(title, part.doc_id, part.part, part.parts)
            rm_uuid = await self.transport.upload_pdf(part.pdf, name, self.settings.inbox_folder)
            d = self.doc_dir(part.doc_id)
            render.write(d / "original.pdf", part.pdf)
            self.store.upsert(Doc(
                doc_id=part.doc_id, base_id=base_id, title=title,
                source_kind=source_kind or kind, source_ref=source_ref or url,
                rm_uuid=rm_uuid, status="pushed", pushed_at=now(), dir=str(d),
            ))
            paths.append(f"/{self.settings.inbox_folder}/{name}")
        return PushResult(base_id, title, paths, [p.doc_id for p in parts])

    # --- pull -------------------------------------------------------------------------------

    async def poll_outbox(self) -> list[PulledDoc]:
        """Process everything in the Outbox folder. Idempotent; concurrent calls run one at a time.

        The asyncio lock covers callers in this process; the file lock covers the poller container,
        which shares the data volume.
        """
        async with self._poll_lock, _file_lock(self.settings.data_dir / "poll.lock"):
            pulled = []
            for remote in await self.transport.list_folder(self.settings.outbox_folder):
                try:
                    result = await self._process(remote)
                except Exception:
                    log.exception("failed to process %s (%s)", remote.name, remote.uuid)
                    continue
                if result:
                    pulled.append(result)
            return pulled

    async def _process(self, remote: RemoteDoc) -> PulledDoc | None:
        known = self.store.by_rm_uuid(remote.uuid)
        if known and known.status == "pulled" and known.rm_hash == remote.version:
            # Processed already but the move to Archive did not land; finish it.
            await self.transport.move(remote, self.settings.archive_folder)
            return None

        rmdoc = await self.transport.download(remote)
        original = rmdoc.pdf
        if original is None:
            log.warning("skipping %s: not a PDF document", remote.name)
            return None

        doc_id = render.read_id(original) or render.id_from_title(remote.name)
        doc = self.store.get(doc_id) if doc_id else None
        if doc is None:
            doc = known or Doc(
                doc_id=doc_id or f"ext-{remote.uuid[:8]}",
                base_id=(doc_id or f"ext-{remote.uuid[:8]}").split(".")[0],
                title=remote.name,
                source_kind="external",
                status="external",
            )
        external = doc.source_kind == "external"
        title = remote.name if external else doc.title

        d = self.doc_dir(doc.doc_id)
        ov = overlay(rmdoc, rm_width=self.settings.rm_page_width_units)
        render.write(d / "original.pdf", original)
        render.write(d / "annotated.pdf", ov.annotated_pdf)
        render.write(d / "annotated.png", ov.png)
        for old in d.glob("strip-*.png"):
            old.unlink()
        for s in slice_strips(ov, original):
            render.write(d / f"strip-{s.index:02d}.png", s.png)

        interpreted = await self.interpreter.interpret(ov, original, title)
        feedback = {
            "doc_id": doc.doc_id,
            "title": title,
            "source": {"kind": doc.source_kind, "ref": doc.source_ref},
            **{k: interpreted[k] for k in ("highlights", "marks", "free_notes", "summary")},
        }
        if external:
            feedback["tags"] = ["external"]
        (d / "feedback.json").write_text(json.dumps(feedback, indent=2, ensure_ascii=False))

        if self.trilium:
            try:
                doc.trilium_note_id = await self.trilium.upsert(
                    doc.doc_id, remote.name, feedback, ov.annotated_pdf, original, doc.trilium_note_id
                )
                doc.trilium_url = self.trilium.note_url(doc.trilium_note_id)
            except Exception:
                log.exception("Trilium upload failed for %s; feedback is kept locally", doc.doc_id)

        doc.rm_uuid = remote.uuid
        doc.feedback = feedback
        doc.pulled_at = now()
        doc.dir = str(d)
        doc.status = "pulled"
        # Saved before the move with the pre-move version: if the move fails, the next poll only
        # retries the move. The move itself changes the version, so a doc moved back to Outbox later
        # is processed again and updates its note.
        doc.rm_hash = remote.version
        self.store.upsert(doc)
        await self.transport.move(remote, self.settings.archive_folder)
        return PulledDoc(doc.doc_id, title, feedback["summary"], doc.trilium_url, len(feedback["marks"]), external)

    # --- queries ----------------------------------------------------------------------------

    async def list_inbox(self) -> list[dict[str, Any]]:
        docs = await self.transport.list_folder(self.settings.inbox_folder)
        return [
            {"name": d.name, "doc_id": render.id_from_title(d.name), "uuid": d.uuid, "last_modified": d.last_modified}
            for d in sorted(docs, key=lambda d: -d.last_modified)
        ]

    def list_returned(self, since: float | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "doc_id": d.doc_id,
                "title": d.title,
                "pulled_at": d.pulled_at,
                "summary": (d.feedback or {}).get("summary"),
                "marks": len((d.feedback or {}).get("marks", [])),
                "trilium_url": d.trilium_url,
            }
            for d in self.store.returned(since, limit)
        ]

    def get_annotations(self, doc_id: str) -> tuple[list[dict[str, Any]], list[Path]]:
        """Feedback JSON for a doc (every part, for split docs) and its annotated strip images."""
        doc_id = doc_id.strip()
        docs = [self.store.get(doc_id)] if "." in doc_id else self.store.parts(doc_id)
        docs = [d for d in docs if d and d.feedback]
        images = []
        for d in docs:
            if d.path and d.path.exists():
                images += sorted(d.path.glob("strip-*.png"))
        return [d.feedback for d in docs], images

    def prune(self) -> int:
        """Delete rendered files older than the retention window; feedback stays in the store."""
        cutoff = now() - self.settings.retention_days * 86400
        count = 0
        for d in self.store.older_than(cutoff):
            if d.path and d.path.exists():
                shutil.rmtree(d.path, ignore_errors=True)
            d.dir = None
            self.store.upsert(d)
            count += 1
        return count


@asynccontextmanager
async def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        await asyncio.to_thread(fcntl.flock, f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _default_title(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url)
    tail = p.path.rstrip("/").rsplit("/", 1)[-1]
    return f"{p.netloc} {tail}".strip() if tail else p.netloc


_bridge: InkBridge | None = None


def get_bridge() -> InkBridge:
    global _bridge
    if _bridge is None:
        _bridge = InkBridge()
    return _bridge
