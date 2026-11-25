"""MCP server (Streamable HTTP) exposing Ink Bridge to Claude conversations."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import datetime, timezone

from mcp.server.mcpserver import Context, Image, MCPServer

from .config import get_settings
from .core.pipeline import NotOnTablet, get_bridge
from .uploads import get_uploads

MAX_IMAGES = 10

mcp = MCPServer(
    name="Ink Bridge",
    instructions=(
        "Ink Bridge sends documents to the user's reMarkable tablet and returns their handwritten "
        "markup as structured feedback. Text you write in the conversation: push_document(markdown=...); "
        "markdown and HTML become one tall scrolling page, which suits annotation best. A file, an "
        "artifact or anything over ~50 KB: if you have a shell, call create_upload and send the file "
        "with the curl command it returns, so the bytes never pass through your context. Artifacts go "
        "as PDF (a Claude Docs doc: its PDF export; an HTML artifact may also go as its HTML file); "
        "a PDF keeps its own pages. Without a shell, read a small HTML artifact with your artifact "
        "tools and pass its full HTML (CSS and assets inlined) to push_document. Keep the doc ID it returns: "
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

    Pass exactly one of html, markdown, pdf_base64 or url (a public URL). Prefer markdown for text
    you wrote. A PDF keeps its own pages. For files and anything large, use create_upload instead:
    pdf_base64 puts every byte through your context. ready_selector waits for a CSS selector
    before rendering (JS-drawn charts, Mermaid). Returns the doc ID and tablet path.
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


def _first(value: str | None) -> str:
    """The first entry of a forwarded header; proxies chained behind each other append theirs."""
    return (value or "").split(",")[0].strip()


def request_base_url(request) -> str:
    """The base URL a client used to reach this app, as the proxy in front of it reports it.

    Through a public proxy that is the public URL; called through the Service, the Service URL. The
    Host header is checked by the MCP endpoint when MCP_ALLOWED_HOSTS is set; the forwarded
    headers come from whatever proxy the infrastructure puts in front.
    """
    if request is None:
        return ""
    h = request.headers
    host = _first(h.get("x-forwarded-host")) or h.get("host", "")
    if not host:
        return ""
    proto = _first(h.get("x-forwarded-proto")) or request.url.scheme
    prefix = _first(h.get("x-forwarded-prefix")).rstrip("/")
    return f"{proto}://{host}{prefix}"


@mcp.tool()
async def create_upload(title: str, ctx: Context, kind: str | None = None, ready_selector: str | None = None) -> dict:
    """A one-time URL to push a file from a shell, so its bytes never pass through your context.

    Send the file with the returned command (`curl -sS -T <file> <url>`); the response is the
    same JSON as push_document's: doc_id, tablet path, parts. PDFs are detected; for HTML or
    Markdown pass kind ("html" or "markdown") or send a matching Content-Type. The URL expires
    after expires_in_s and works once (a failed upload can be retried with it).
    """
    settings = get_settings()
    try:
        request = ctx.request_context.request
    except ValueError:  # called outside an HTTP request
        request = None
    base = settings.public_url or request_base_url(request)
    if not base:
        return {"error": "No base URL for uploads: set PUBLIC_URL, or call this over HTTP."}
    try:
        token, grant = get_uploads().create(title, kind, ready_selector)
    except ValueError as e:
        return {"error": str(e)}
    url = f"{base}/upload/{token}"
    return {
        "upload_url": url,
        "command": f"curl -sS --fail-with-body -T <file> '{url}'",
        "expires_in_s": round(grant.expires - datetime.now(timezone.utc).timestamp()),
        "max_mb": settings.upload_max_mb,
    }


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
