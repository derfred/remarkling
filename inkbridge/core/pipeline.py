"""push_document() and poll(): the two paths every trigger (chat, phone, schedule) calls.

poll() is two stages: poll_outbox() pulls documents (transport only), interpret_pending() turns
pulled ink into marks (needs an interpreter). Each stage records its failures on the doc.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import shutil
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import Settings, get_settings
from . import render
from .interpret import Interpreter, slice_strips
from .overlay import overlay
from .store import Doc, Store, now, open_store
from .cloud import CloudTransport, RemoteDoc
from .rmdoc import RmDoc
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
    external: bool
    strips: int
    highlights: int
    interpretation: str | None  # pending | done | failed | off
    summary: str | None = None
    marks: int | None = None
    trilium_url: str | None = None
    interpretation_error: str | None = None


@dataclass
class FailedDoc:
    doc_id: str
    title: str
    rm_uuid: str | None
    stage: str | None  # download | render | save
    error: str | None
    attempts: int
    retry_at: float  # epoch seconds; a new version of the doc is retried at once


@dataclass
class PollResult:
    pulled: list[PulledDoc] = field(default_factory=list)
    failed: list[FailedDoc] = field(default_factory=list)


class PullError(Exception):
    """A pull failed at `stage`; the original exception is `__cause__`."""

    def __init__(self, stage: str, cause: Exception):
        super().__init__(f"{stage}: {cause}")
        self.stage = stage


# A failed pull or interpretation of the same version is retried after 1, 2, 4, ... minutes, at most 6 h.
RETRY_BASE_S = 60.0
RETRY_MAX_S = 6 * 3600.0


def retry_at(failed_at: float, attempts: int) -> float:
    return failed_at + min(RETRY_BASE_S * 2 ** max(attempts - 1, 0), RETRY_MAX_S)


def interpretation_status(doc: Doc) -> str | None:
    if doc.interpret_status:
        return doc.interpret_status
    # Pulled before interpretation had its own status: it ran in the same step.
    return "done" if doc.feedback and "summary" in doc.feedback else None


def _describe(e: BaseException | None) -> str:
    return f"{type(e).__name__}: {e}" if e is not None else "unknown error"


def _pulled(doc: Doc) -> PulledDoc:
    fb = doc.feedback or {}
    done = doc.interpret_status == "done"
    return PulledDoc(
        doc_id=doc.doc_id,
        title=doc.title,
        external=doc.source_kind == "external",
        strips=len(list(doc.path.glob("strip-*.png"))) if doc.path and doc.path.exists() else 0,
        highlights=len(fb.get("highlights") or []),
        interpretation=interpretation_status(doc),
        summary=fb.get("summary") if done else None,
        marks=len(fb.get("marks") or []) if done else None,
        trilium_url=doc.trilium_url,
        interpretation_error=doc.interpret_error if doc.interpret_status == "failed" else None,
    )


def _failed(doc: Doc) -> FailedDoc:
    return FailedDoc(
        doc_id=doc.doc_id, title=doc.title, rm_uuid=doc.rm_uuid, stage=doc.error_stage, error=doc.error,
        attempts=doc.attempts, retry_at=retry_at(doc.failed_at or 0, doc.attempts),
    )


class InkBridge:
    def __init__(
        self,
        settings: Settings | None = None,
        store: Store | None = None,
        transport: CloudTransport | None = None,
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
    def transport(self) -> CloudTransport:
        if self._transport is None:
            s = self.settings
            self._transport = CloudTransport(
                s.rm_device_token, s.rm_auth_host, s.rm_sync_host,
                cache=self.store, notifications_url=s.rm_notifications_url,
            )
        return self._transport

    @property
    def interpreter(self) -> Interpreter | None:
        """None without ANTHROPIC_API_KEY: documents are still pulled, just not interpreted."""
        if self._interpreter is None and self.settings.anthropic_api_key:
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
    # A pull downloads a document from the Outbox, renders its ink over the original, saves the
    # result and archives it. It needs only the reMarkable transport. Interpretation (handwriting
    # -> marks, below) runs afterwards on what the pull saved, and its failure never undoes a pull.

    async def poll(self) -> PollResult:
        """Pull everything in the Outbox, then interpret what is due. The poller and poll_now call this."""
        result = await self.poll_outbox()
        await self.interpret_pending()
        for i, p in enumerate(result.pulled):
            if (doc := self.store.get(p.doc_id)) is not None:
                result.pulled[i] = _pulled(doc)
        return result

    async def poll_outbox(self) -> PollResult:
        """Pull everything in the Outbox. Idempotent; concurrent calls run one at a time.

        The asyncio lock covers callers in this process; the file lock covers the poller container,
        which shares the data volume. A document that failed is retried when its version changes,
        or after a backoff (see `retry_at`); meanwhile it is reported in `failed` without a retry.
        """
        async with self._poll_lock, _file_lock(self.settings.data_dir / "poll.lock"):
            result = PollResult()
            for remote in await self.transport.list_folder(self.settings.outbox_folder):
                outcome = await self._pull_one(remote)
                if isinstance(outcome, PulledDoc):
                    result.pulled.append(outcome)
                elif isinstance(outcome, FailedDoc):
                    result.failed.append(outcome)
            return result

    async def _pull_one(self, remote: RemoteDoc) -> PulledDoc | FailedDoc | None:
        known = self.store.by_rm_uuid(remote.uuid) or self._get(render.id_from_title(remote.name))
        if known and known.status == "pulled" and known.rm_hash == remote.version:
            # Pulled already but the move to Archive did not land; finish it.
            try:
                await self.transport.move(remote, self.settings.archive_folder)
            except Exception:
                log.exception("failed to archive %s (%s)", remote.name, remote.uuid)
            return None
        if (known and known.status == "failed" and known.error_version == remote.version
                and now() < retry_at(known.failed_at or 0, known.attempts)):
            return _failed(known)
        try:
            return await self._pull(remote, known)
        except PullError as e:
            stage, cause = e.stage, e.__cause__
        except Exception as e:  # after the files were written: storing the result
            stage, cause = "save", e
        log.error("failed to pull %s (%s) at %s", remote.name, remote.uuid, stage, exc_info=cause)
        return self._record_failure(remote, known, stage, cause)

    async def _pull(self, remote: RemoteDoc, known: Doc | None) -> PulledDoc:
        stage = "download"
        try:
            rmdoc = await self.transport.download(remote)
            stage = "render"
            ov = overlay(rmdoc, rm_width=self.settings.rm_page_width_units)
            # Notebooks (and EPUBs) have no PDF: their "original" is the rendering without ink,
            # i.e. the template and any typed text.
            original = rmdoc.pdf or ov.text_pdf
            if rmdoc.pdf is None:
                log.info("%s has no PDF (%s); using its rendered pages", remote.name,
                         rmdoc.content.get("fileType") or "notebook")
            strips = slice_strips(ov, original)
            doc = self._identify(remote, rmdoc, known)
            stage = "save"
            d = self.doc_dir(doc.doc_id)
            # The raw document, strokes included: what interpretation (and any later model) reads.
            render.write(d / "ink.rmdoc", rmdoc.to_zip())
            render.write(d / "original.pdf", original)
            render.write(d / "annotated.pdf", ov.annotated_pdf)
            render.write(d / "annotated.png", ov.png)
            for old in d.glob("strip-*.png"):
                old.unlink()
            for s in strips:
                render.write(d / f"strip-{s.index:02d}.png", s.png)
        except Exception as e:
            raise PullError(stage, e) from e

        external = doc.source_kind == "external"
        if external:
            doc.title = remote.name
        feedback: dict[str, Any] = {
            "doc_id": doc.doc_id,
            "title": doc.title,
            "source": {"kind": doc.source_kind, "ref": doc.source_ref},
            "highlights": ov.highlights,
        }
        if external:
            feedback["tags"] = ["external"]
        (d / "feedback.json").write_text(json.dumps(feedback, indent=2, ensure_ascii=False))

        doc.rm_uuid = remote.uuid
        doc.feedback = feedback
        doc.pulled_at = now()
        doc.dir = str(d)
        doc.status = "pulled"
        doc.error = doc.error_stage = doc.error_version = doc.failed_at = None
        doc.attempts = 0
        doc.interpret_status = "pending" if self.interpreter else "off"
        doc.interpret_error = doc.interpret_failed_at = None
        doc.interpret_attempts = 0
        # Saved before the move with the pre-move version: if the move fails, the next poll only
        # retries the move. The move itself changes the version, so a doc moved back to Outbox later
        # is pulled again and updates its note.
        doc.rm_hash = remote.version
        self.store.upsert(doc)
        if known and known.doc_id != doc.doc_id and known.source_kind == "external" and known.pulled_at is None:
            # A placeholder made when an earlier attempt failed before the doc could be identified.
            self.store.delete(known.doc_id)
        if doc.interpret_status == "off":
            await self._file_note(doc)
        try:
            await self.transport.move(remote, self.settings.archive_folder)
        except Exception:
            log.exception("failed to archive %s (%s); the next poll retries", remote.name, remote.uuid)
        return _pulled(doc)

    def _get(self, doc_id: str | None) -> Doc | None:
        return self.store.get(doc_id) if doc_id else None

    def _identify(self, remote: RemoteDoc, rmdoc: RmDoc, known: Doc | None) -> Doc:
        """The stored doc a download belongs to: by the ID stamped into its PDF, else by the ID in
        its tablet name, else by its reMarkable UUID; a new external doc if none matches."""
        doc_id = (render.read_id(rmdoc.pdf) if rmdoc.pdf else None) or render.id_from_title(remote.name)
        doc = self._get(doc_id) or known
        if doc is None:
            doc_id = doc_id or f"ext-{remote.uuid[:8]}"
            return Doc(doc_id=doc_id, base_id=doc_id.split(".")[0], title=remote.name,
                       source_kind="external", status="external")
        return doc

    def _record_failure(self, remote: RemoteDoc, known: Doc | None, stage: str, cause: BaseException | None) -> FailedDoc:
        doc = known or Doc(
            doc_id=f"ext-{remote.uuid[:8]}", base_id=f"ext-{remote.uuid[:8]}", title=remote.name,
            source_kind="external",
        )
        doc.attempts = doc.attempts + 1 if doc.error_version == remote.version else 1
        doc.status = "failed"
        doc.rm_uuid = remote.uuid
        doc.error = _describe(cause)
        doc.error_stage = stage
        doc.error_version = remote.version
        doc.failed_at = now()
        self.store.upsert(doc)
        return _failed(doc)

    # --- interpret --------------------------------------------------------------------------

    async def interpret_pending(self) -> list[str]:
        """Interpret the pulled docs that are due; returns their doc IDs. A no-op without an interpreter."""
        if self.interpreter is None:
            return []
        async with self._poll_lock, _file_lock(self.settings.data_dir / "poll.lock"):
            done = []
            for doc in self.store.interpret_due():
                if doc.interpret_status == "failed" and now() < retry_at(
                        doc.interpret_failed_at or 0, doc.interpret_attempts):
                    continue
                await self._interpret(doc)
                done.append(doc.doc_id)
            return done

    async def _interpret(self, doc: Doc) -> None:
        d = doc.path
        try:
            rmdoc = RmDoc.from_zip((d / "ink.rmdoc").read_bytes())
            ov = overlay(rmdoc, rm_width=self.settings.rm_page_width_units)
            original = (d / "original.pdf").read_bytes()
            interpreted = await self.interpreter.interpret(ov, original, doc.title)
        except Exception as e:
            log.exception("failed to interpret %s", doc.doc_id)
            doc.interpret_status = "failed"
            doc.interpret_error = _describe(e)
            doc.interpret_attempts += 1
            doc.interpret_failed_at = now()
            self.store.upsert(doc)
            return
        doc.feedback = {
            **(doc.feedback or {}),
            **{k: interpreted[k] for k in ("highlights", "marks", "free_notes", "summary")},
        }
        (d / "feedback.json").write_text(json.dumps(doc.feedback, indent=2, ensure_ascii=False))
        doc.interpret_status = "done"
        doc.interpret_error = doc.interpret_failed_at = None
        doc.interpret_attempts = 0
        self.store.upsert(doc)
        await self._file_note(doc)

    async def _file_note(self, doc: Doc) -> None:
        """Create or update the doc's Trilium note. Best effort: the feedback is kept locally."""
        if not self.trilium:
            return
        d = doc.path
        try:
            original = d / "original.pdf"
            doc.trilium_note_id = await self.trilium.upsert(
                doc.doc_id,
                doc.title if doc.source_kind == "external" else f"{doc.title} · {doc.doc_id}",
                doc.feedback or {},
                (d / "annotated.pdf").read_bytes(),
                original.read_bytes() if original.exists() else None,
                doc.trilium_note_id,
            )
            doc.trilium_url = self.trilium.note_url(doc.trilium_note_id)
            self.store.upsert(doc)
        except Exception:
            log.exception("Trilium upload failed for %s; feedback is kept locally", doc.doc_id)

    # --- queries ----------------------------------------------------------------------------

    async def list_inbox(self) -> list[dict[str, Any]]:
        docs = await self.transport.list_folder(self.settings.inbox_folder)
        return [
            {"name": d.name, "doc_id": render.id_from_title(d.name), "uuid": d.uuid, "last_modified": d.last_modified}
            for d in sorted(docs, key=lambda d: -d.last_modified)
        ]

    def list_returned(self, since: float | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Docs pulled or failed since `since`, newest first, with their pull and interpretation state."""
        out = []
        for d in self.store.returned(since, limit):
            item = {
                "doc_id": d.doc_id,
                "title": d.title,
                "status": d.status,
                "pulled_at": d.pulled_at,
                "interpretation": interpretation_status(d),
                "summary": (d.feedback or {}).get("summary"),
                "marks": len((d.feedback or {}).get("marks", [])),
                "trilium_url": d.trilium_url,
            }
            if d.status == "failed":
                item["failure"] = asdict(_failed(d))
            out.append(item)
        return out

    def get_annotations(self, doc_id: str) -> tuple[list[dict[str, Any]], list[Path]]:
        """State and feedback for a doc (every part, for split docs) and its annotated strip images.

        A pulled doc has its strips and highlights even before (or without) interpretation; the
        marks, notes and summary appear once interpretation is done.
        """
        doc_id = doc_id.strip()
        docs = [self.store.get(doc_id)] if "." in doc_id else self.store.parts(doc_id)
        out, images = [], []
        for d in docs:
            if d is None:
                continue
            view = {**(d.feedback or {"doc_id": d.doc_id, "title": d.title}),
                    "status": d.status, "interpretation": interpretation_status(d)}
            if d.interpret_status == "failed":
                view["interpretation_error"] = d.interpret_error
            if d.status == "failed":
                view["failure"] = asdict(_failed(d))
            out.append(view)
            if d.feedback and d.path and d.path.exists():
                images += sorted(d.path.glob("strip-*.png"))
        return out, images

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
