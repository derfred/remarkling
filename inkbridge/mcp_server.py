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
        "Ink Bridge puts documents on the user's reMarkable tablet and brings back their handwritten "
        "markup.\n\n"
        "Layout: the tablet works best with one tall scrolling page, which the server renders from "
        "markdown or HTML. A PDF is passed through with its own pages, so never make a PDF from content "
        "that has a source: send the source and let the server render it. Send a PDF only when a PDF is "
        "all there is (a paper, a scan, a file the user gave you). If that PDF is one flowing text that "
        "an export or a print cut into pages, send it with layout=\"tall\" to have the server join "
        "the pages; not for slides, scans, forms or papers whose pages stand on their own. Rendered "
        "content that is very long (over ~5 m) continues on a second tall page of the same doc; in "
        "HTML, break-before: page starts a new page, data-break=\"prefer\" marks where a long text "
        "should continue if it has to, and break-inside: avoid keeps a block on one page.\n\n"
        "Sending (each way returns a doc_id; keep it, every later call needs it):\n"
        "- Text you wrote: push_document(markdown=...).\n"
        "- A Claude Docs doc (a claude.ai/artifact link can be one: your artifact tools say so): not "
        "through your artifact tools (they return its viewer, not its content), and not the docs read "
        "tool (it returns fragment XML, several times larger than the text). Export the tab with the "
        "docs export tool as markdown (not html, which comes unstyled; not pdf); its bytes_b64 is the "
        "markdown in base64. A result with saved_to instead of bytes_b64: your client already wrote the "
        "decoded file there; send that path with create_upload(title, \"markdown\") and decode nothing. "
        "In your context: decode it yourself as you write the text into "
        "push_document(markdown=...). No tool decodes it for you without you typing the base64 out "
        "first, which costs a third more than the text, and one wrong character corrupts it. Saved to a "
        "file by your client instead: decode it there in a shell (bytes_b64 of the JSON, base64 -d) and "
        "send that file with create_upload(title, \"markdown\"), never through your context.\n"
        "- Another artifact: its HTML (CSS and assets inlined), or its markdown.\n"
        "- A file on disk (one the user gave you, or a result your client saved to a file): with a shell, "
        "create_upload(title, kind) and run the curl command it returns, with <file> replaced by the path; "
        "the doc_id is in curl's output. Without a shell, or if the URL is unreachable from it, pass "
        "markdown or HTML to push_document. create_upload saves context only when the bytes are already "
        "on disk: content already in your context goes to push_document; over ~50 KB, write the text you "
        "would have passed to a file and upload that.\n\n"
        "Getting annotations back: when the user says they are done, pull_document(doc_id). It fetches "
        "from the tablet, wherever the doc is, and returns only the stretches of page that hold ink, as "
        "image strips. Interpretation into marks starts after the pull, so its result usually says "
        "\"pending\": read the marks from the strips yourself, and say where each one is by quoting the "
        "printed text it is written on or beside. Call get_annotations later for the server's marks. "
        "get_annotations returns what was last pulled without contacting the tablet. document_status "
        "and list_documents show whether a doc has new ink, without pulling.\n\n"
        "poll_now, list_returned and list_inbox serve the Outbox flow, which needs no conversation; you "
        "rarely need them. If a pull fails, tell the user the error rather than retrying it."
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
    layout: str = "pages",
) -> dict:
    """Upload content to /Inbox on the tablet. Returns doc_id (keep it), tablet path and parts.

    Pass exactly one of markdown, html, url (a public page) or pdf_base64. Markdown, HTML and URLs
    are rendered to one tall scrolling page, the layout the tablet works best with; very long
    content continues on more tall pages of the same doc (see create_upload for choosing where). A PDF
    keeps its own pages, so pass one only when no source exists, and only a small one: every byte
    goes through your context. Files and anything over ~50 KB go through create_upload.
    ready_selector (HTML, url) waits for a CSS selector before rendering: JS-drawn charts, Mermaid.
    layout (PDFs): "pages" or "tall", as for create_upload.
    """
    result = await get_bridge().push_document(
        title,
        html=html,
        markdown=markdown,
        pdf=base64.b64decode(pdf_base64) if pdf_base64 else None,
        url=url,
        ready_selector=ready_selector,
        source_kind="artifact" if html else None,
        layout=layout,
    )
    out = {"doc_id": result.doc_id, "tablet_path": result.tablet_paths, "parts": result.parts}
    return {**out, "layout": result.layout} if result.layout else out


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
async def create_upload(title: str, ctx: Context, kind: str = "pdf", ready_selector: str | None = None,
                        layout: str = "pages") -> dict:
    """A one-time URL to send a file already on disk from a shell, so its bytes never pass through your
    context. For content you already hold (an export, text you wrote), use push_document.

    kind is what you will send: "markdown" or "html" (UTF-8; rendered to one tall page, preferred),
    or "pdf" (the default; keeps its pages, for files that only exist as PDF). ready_selector (HTML)
    waits for a CSS selector before rendering.

    Pages of rendered content: one tall page up to ~5 m, then more tall pages of the same doc, cut at
    the best place below the limit: an element with data-break="prefer", else an h2, an h3, a gap
    between blocks. In HTML, break-before: page (or data-break="page") always starts a new page, for
    an answer key or an appendix, and a block with break-inside: avoid (and every image, figure,
    table row and pre) is never cut. The response's "layout" lists the cuts made.

    Run the returned "command" with <file> replaced by the path. curl prints push_document's JSON:
    keep its doc_id for pull_document. On an error it prints {"detail": ...}: 415 means the file
    does not match kind (ask again with the right kind), 410 that the URL expired or was used
    (ask for a new one). Other failures can be retried with the same URL until it expires
    (expires_in_s). If the shell cannot reach the URL, use push_document instead.

    layout (PDFs only): "pages" (default) keeps the PDF's pages. "tall" joins them into one tall
    page, cropping margins and running headers/footers (page numbers), for a document that flows
    as one text and was only cut into pages by an export or a print. Not for slides, scans, forms
    or papers whose pages stand on their own. The server keeps the pages when the PDF does not
    fit (pages of different widths, no text layer) and says why in the response's "layout".
    """
    if layout not in ("pages", "tall"):
        return {"error": 'layout must be "pages" or "tall"'}
    if layout == "tall" and kind != "pdf":
        return {"error": 'layout="tall" applies to PDFs; markdown and HTML always become one tall page'}
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
    url = f"{base}/upload/{token}" + ("?layout=tall" if layout == "tall" else "")
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
    """History of pulls (Outbox and pull_document, external docs included) and failed pulls, newest
    first, with interpretation state and feedback summary. since: ISO date or datetime. For the docs
    on the tablet and whether they have new ink, use list_documents."""
    return get_bridge().list_returned(_since(since))


@mcp.tool()
async def get_annotations(doc_id: str) -> list:
    """State and feedback JSON for a document plus its annotated page strips, as last pulled.

    Does not contact the tablet: for ink added since, use pull_document. The strips and highlights are there as soon as the doc is pulled; marks, notes and summary once
    "interpretation" is "done". A doc that failed to pull carries "failure" with the error.
    """
    return _annotations(doc_id)


@mcp.tool()
async def pull_document(doc_id: str, force: bool = False) -> list:
    """Fetch a document's current annotations from the tablet, wherever it is, and return them as
    get_annotations does: state and feedback JSON plus the annotated page strips as images.

    The doc stays where it is. If its ink has not changed since the last pull, nothing is fetched
    ("new": false); force=True pulls anyway. Interpretation into marks starts afterwards in the
    background, so it is usually "pending" here: read the strips yourself, placing each mark by the
    printed text it sits on or beside, and call get_annotations later for the marks.
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
    """Documents still in /Inbox on the tablet. To find a doc you pushed, use list_documents: the user
    may have moved it."""
    return await get_bridge().list_inbox()
