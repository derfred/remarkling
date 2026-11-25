"""MCP server (Streamable HTTP) exposing Ink Bridge to Claude conversations."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import datetime, timezone

from mcp.server.mcpserver import Image, MCPServer

from .core.pipeline import NotOnTablet, get_bridge

MAX_IMAGES = 10

mcp = MCPServer(
    name="Ink Bridge",
    instructions=(
        "Ink Bridge sends documents to the user's reMarkable tablet and returns their handwritten "
        "markup as structured feedback. To send a claude.ai artifact, read it with your artifact tools "
        "and pass its full HTML (CSS and assets inlined) to push_document. Keep the doc ID it returns: "
        "the document is tracked by it wherever the user files it, so they need not move it anywhere. "
        "When the user says they annotated it, call pull_document with the doc ID: it returns the "
        "annotated page strips as images, and the interpreted marks once interpretation is done; if "
        "interpretation is pending, off or failed, read the marks from the images yourself. "
        "document_status and list_documents tell whether a doc has ink not yet pulled, without pulling. "
        "Documents the user moved to /Outbox are pulled by poll_now (that is the flow without a "
        "conversation). If a pull fails, tell the user the error rather than retrying."
    ),
)


def _annotations(doc_id: str, lead: list | None = None) -> list:
    views, images = get_bridge().get_annotations(doc_id)
    if not views:
        return [f"No document {doc_id}. Check the ID with list_documents or list_returned."]
    body = views[0] if len(views) == 1 else {"parts": views}
    out: list = [*(lead or []), json.dumps(body, indent=2, ensure_ascii=False)]
    if not images:
        out.append(f"No pulled annotations for {doc_id} yet. Call pull_document once the user has annotated it.")
    out += [Image(path=p) for p in images[:MAX_IMAGES]]
    if len(images) > MAX_IMAGES:
        out.append(f"{len(images) - MAX_IMAGES} more strips not shown.")
    return out


def _since(value: str | None) -> float | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


@mcp.tool()
async def push_document(
    title: str,
    html: str | None = None,
    markdown: str | None = None,
    pdf_base64: str | None = None,
    url: str | None = None,
    ready_selector: str | None = None,
) -> dict:
    """Render content to one tall scrolling PDF page and upload it to /Inbox on the tablet.

    Pass exactly one of html, markdown, pdf_base64 or url (a public URL). ready_selector waits for
    a CSS selector before rendering (JS-drawn charts, Mermaid). Returns the doc ID and tablet path.
    """
    result = await get_bridge().push_document(
        title,
        html=html,
        markdown=markdown,
        pdf=base64.b64decode(pdf_base64) if pdf_base64 else None,
        url=url,
        ready_selector=ready_selector,
        source_kind="artifact" if html else None,
    )
    return {"doc_id": result.doc_id, "tablet_path": result.tablet_paths, "parts": result.parts}


@mcp.tool()
async def poll_now() -> dict:
    """Pull every document the user moved to /Outbox, note new ink on tracked documents, then
    interpret the handwriting where enabled. For a document you pushed, prefer pull_document.

    Returns {"pulled": [...], "failed": [...], "changed": [...]}; "changed" lists tracked docs whose
    ink changed since the last poll and is not yet pulled. A pulled doc's "interpretation" is done, pending, failed
    or off; its strips are available from get_annotations either way. A failed doc carries the stage,
    error, attempt count and retry_at (epoch seconds); it is retried then, or as soon as it changes.
    """
    return asdict(await get_bridge().poll())


@mcp.tool()
async def list_returned(since: str | None = None) -> list[dict]:
    """Recently pulled documents, and ones that failed to pull, with status, interpretation state and
    feedback summary. since: ISO date or datetime."""
    return get_bridge().list_returned(_since(since))


@mcp.tool()
async def get_annotations(doc_id: str) -> list:
    """State and feedback JSON for a document plus its annotated page strips as images.

    The strips and highlights are there as soon as the doc is pulled; marks, notes and summary once
    "interpretation" is "done". A doc that failed to pull carries "failure" with the error.
    """
    return _annotations(doc_id)


@mcp.tool()
async def pull_document(doc_id: str, force: bool = False) -> list:
    """Fetch a document's current annotations from the tablet, wherever it is, and return them as
    get_annotations does: state and feedback JSON plus the annotated page strips as images.

    The doc stays where it is. If its ink has not changed since the last pull, nothing is fetched
    ("new": false); force=True pulls anyway. Interpretation into marks starts afterwards in the
    background; call get_annotations later for the marks, or read the strips yourself.
    """
    bridge = get_bridge()
    try:
        outcomes = await bridge.pull_document(doc_id, force=force)
    except NotOnTablet as e:
        return [f"{e}. Annotations pulled earlier, if any, follow.", *_annotations(doc_id)]
    except LookupError:
        return [f"No document {doc_id}. Check the ID with list_documents."]
    return _annotations(doc_id, [json.dumps({"pulled": [asdict(o) for o in outcomes]}, ensure_ascii=False)])


@mcp.tool()
async def document_status(doc_id: str) -> list[dict]:
    """Where a document is on the tablet and whether it has ink not yet pulled, checked now.

    Per part: location (folder), on_tablet, has_new_ink, changed_pages (page numbers whose ink
    differs from the last pull), ink_changed_at (epoch seconds), status, interpretation.
    """
    return await get_bridge().document_status(doc_id)


@mcp.tool()
async def list_documents(changed_only: bool = False, limit: int = 20) -> list[dict]:
    """Documents on the tablet that Ink Bridge tracks, most recently active first, with the same
    fields as document_status (changed_pages is a count here). changed_only: only those with ink
    not yet pulled."""
    return await get_bridge().list_documents(changed_only=changed_only, limit=limit)


@mcp.tool()
async def remove_document(doc_id: str) -> dict:
    """Move a document (every part) to the tablet's trash, where the user can still restore it,
    and stop tracking it. Pulled annotations stay available. Only on the user's request."""
    try:
        return {"removed": await get_bridge().remove_document(doc_id)}
    except LookupError:
        return {"removed": [], "error": f"No document {doc_id}"}


@mcp.tool()
async def list_inbox() -> list[dict]:
    """Documents waiting in /Inbox on the tablet."""
    return await get_bridge().list_inbox()
