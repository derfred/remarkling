"""MCP server (Streamable HTTP) exposing Ink Bridge to Claude conversations."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import datetime, timezone

from mcp.server.mcpserver import Image, MCPServer

from .core.pipeline import get_bridge

MAX_IMAGES = 10

mcp = MCPServer(
    name="Ink Bridge",
    instructions=(
        "Ink Bridge sends documents to the user's reMarkable tablet and returns their handwritten "
        "markup as structured feedback. To send a claude.ai artifact, read it with your artifact tools "
        "and pass its full HTML (CSS and assets inlined) to push_document. After the user says they "
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
