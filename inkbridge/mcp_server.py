"""MCP server (Streamable HTTP) exposing Ink Bridge to Claude conversations."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import datetime, timezone

from mcp.server.mcpserver import Image, MCPServer

from .config import get_settings
from .core.pipeline import get_bridge
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
        "tools and pass its full HTML (CSS and assets inlined) to push_document. After the user says they "
        "annotated something, call poll_now, then get_annotations with the doc ID. It returns the annotated "
        "page strips as images, and the interpreted marks once interpretation is done; if interpretation "
        "is pending, off or failed, read the marks from the images yourself. poll_now lists documents that "
        "failed to pull, with the error; tell the user rather than retrying."
    ),
)


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


@mcp.tool()
async def create_upload(title: str, kind: str | None = None, ready_selector: str | None = None) -> dict:
    """A one-time URL to push a file from a shell, so its bytes never pass through your context.

    Send the file with the returned command (`curl -sS -T <file> <url>`); the response is the
    same JSON as push_document's: doc_id, tablet path, parts. PDFs are detected; for HTML or
    Markdown pass kind ("html" or "markdown") or send a matching Content-Type. The URL expires
    after expires_in_s and works once (a failed upload can be retried with it).
    """
    settings = get_settings()
    if not settings.base_url:
        return {"error": "The server has no PUBLIC_URL (or MCP_ALLOWED_HOSTS) to build upload URLs from."}
    try:
        token, grant = get_uploads().create(title, kind, ready_selector)
    except ValueError as e:
        return {"error": str(e)}
    url = f"{settings.base_url}/upload/{token}"
    return {
        "upload_url": url,
        "command": f"curl -sS --fail-with-body -T <file> '{url}'",
        "expires_in_s": round(grant.expires - datetime.now(timezone.utc).timestamp()),
        "max_mb": settings.upload_max_mb,
    }


@mcp.tool()
async def poll_now() -> dict:
    """Pull every document the user moved to /Outbox, then interpret the handwriting where enabled.

    Returns {"pulled": [...], "failed": [...]}. A pulled doc's "interpretation" is done, pending, failed
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
    views, images = get_bridge().get_annotations(doc_id)
    if not views:
        return [f"No document {doc_id}. Check the ID with list_returned or list_inbox."]
    body = views[0] if len(views) == 1 else {"parts": views}
    out: list = [json.dumps(body, indent=2, ensure_ascii=False)]
    if not images:
        out.append(f"No pulled annotations for {doc_id} yet. Call poll_now after the user moves it to Outbox.")
    out += [Image(path=p) for p in images[:MAX_IMAGES]]
    if len(images) > MAX_IMAGES:
        out.append(f"{len(images) - MAX_IMAGES} more strips not shown.")
    return out


@mcp.tool()
async def list_inbox() -> list[dict]:
    """Documents waiting in /Inbox on the tablet."""
    return await get_bridge().list_inbox()
