"""MCP server (Streamable HTTP): the reMarkable library as tools for Claude conversations.

Read tools browse and read any document: listing and search, page structure, the text layer,
page images with ink, raw stroke data, and the handwriting interpreted into feedback. Write tools
push fresh documents, make folders, rename, move, tag, pin and trash; they never touch a
document's pages or ink. Everything goes through the cloud transport.

MCP_ACCESS=read leaves the write tools out; MCP_FOLDERS confines every tool to some folders.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Literal

import pymupdf
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from PIL import Image as PILImage
from rmscene import scene_items as si

from .config import Settings, get_settings
from .core.library import TRASH, TRASH_PATH, Item, Library, NotFound, OutOfScope, normalize, split, within
from .core.pipeline import InkBridge, get_bridge
from .core.renderer import PageNotFound, Renderer
from .core.transport.base import RemoteDoc

MAX_IMAGES = 8
MAX_TEXT_CHARS = 60_000
TILE_ASPECT = 1.4  # tall pages are cut into tiles this many times as tall as wide

INSTRUCTIONS = """\
Ink Bridge connects to the user's reMarkable tablet library: every notebook, PDF and EPUB, with \
the user's handwriting.

Documents are addressed by UUID, path ("/Work/Meeting notes"), Ink Bridge ID (rm-xxxxxx) or a \
unique name. Start with list_documents (it searches too), then get_document for the pages.

Reading: read_document for the text layer (PDF text, typed text, highlighted passages), \
get_pages to look at pages with the ink drawn on them, get_annotations for the handwriting \
interpreted into structured feedback, get_strokes for the raw pen data.

Writing: push_document renders HTML, Markdown, a PDF or a URL into a new document (one tall \
scrolling page). To send a claude.ai artifact, read it with your artifact tools and pass its full \
HTML (CSS and assets inlined). create_folder, update_document (rename, move, pin, tags) and \
trash_document organise the library; nothing edits a document's pages or ink.

Review loop: documents pushed to /Inbox come back when the user moves them to /Outbox. After the \
user says they are done, call process_outbox, then get_annotations, and revise from the marks."""

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


# --- helpers ----------------------------------------------------------------------------------


class Renderers:
    """Renderers of recently read documents, keyed by version: pages of one document share a download."""

    def __init__(self, size: int = 4):
        self.size = size
        self._items: OrderedDict[tuple[str, str], Renderer] = OrderedDict()
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def get(self, bridge: InkBridge, item: Item) -> Renderer:
        key = (item.uuid, item.version)
        async with self._locks.setdefault(key, asyncio.Lock()):
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
            try:
                rmdoc = await bridge.transport.download(RemoteDoc(item.uuid, item.name, item.parent))
            except FileNotFoundError as e:
                raise ToolError(f"{item.path} is gone from the tablet") from e
            renderer = await asyncio.to_thread(Renderer, rmdoc, bridge.settings.rm_page_width_units)
            self._items[key] = renderer
            while len(self._items) > self.size:
                old, _ = self._items.popitem(last=False)
                self._locks.pop(old, None)
            return renderer


def _transport(bridge: InkBridge):
    t = bridge.transport
    if not hasattr(t, "entries"):
        raise ToolError("the library tools need the cloud transport (RM_TRANSPORT=cloud)")
    return t


def _tracked(bridge: InkBridge) -> dict[str, dict[str, Any]]:
    out = {}
    for d in bridge.store.with_rm_uuid():
        out[d.rm_uuid] = {"doc_id": d.doc_id, "status": d.status,
                          "pushed_at": _iso(d.pushed_at), "pulled_at": _iso(d.pulled_at)}
        if d.trilium_url:
            out[d.rm_uuid]["trilium_url"] = d.trilium_url
    return out


async def _library(settings: Settings) -> tuple[InkBridge, Library]:
    bridge = get_bridge()
    entries = await _transport(bridge).entries()
    return bridge, Library(entries, settings.mcp_folders, _tracked(bridge))


def _resolve(lib: Library, ref: str, kind: Literal["document", "folder", "any"] = "any") -> Item:
    try:
        item = lib.resolve(ref)
    except NotFound as e:
        raise ToolError(str(e)) from None
    if kind == "document" and item.is_folder:
        raise ToolError(f"{item.path} is a folder; list_documents shows what is in it")
    if kind == "folder" and not item.is_folder:
        raise ToolError(f"{item.path} is a document, not a folder")
    return item


def _check(lib: Library, path: str) -> str:
    try:
        return lib.check_path(path)
    except OutOfScope as e:
        raise ToolError(str(e)) from None


def _pages(spec: str | None, count: int) -> list[int]:
    """"1-3,5" -> [1, 2, 3, 5]; None or "all" -> every page; "-2" counts from the end."""
    if spec is None or str(spec).strip().lower() in ("", "all"):
        return list(range(1, count + 1))
    out: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        try:
            if part.startswith("-"):
                numbers = [count + 1 + int(part)]
            elif "-" in part:
                a, b = part.split("-", 1)
                numbers = list(range(int(a), (int(b) if b else count) + 1))
            else:
                numbers = [int(part)]
        except ValueError:
            raise ToolError(f"bad page spec {spec!r}; use e.g. \"1-3,5\" or \"-1\" for the last page") from None
        for n in numbers:
            if not 1 <= n <= count:
                raise ToolError(f"page {n} does not exist; the document has {count} pages")
            if n not in out:
                out.append(n)
    return out


def _since(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"bad date {value!r}; use ISO 8601, e.g. 2026-11-16 or 2026-11-16T09:00Z") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _tool_name(tool: int) -> str:
    try:
        return si.Pen(tool).name.lower()
    except ValueError:
        return str(tool)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(c * 255):02x}" for c in rgb)


def _pdf_text(pdf: bytes | None) -> dict[int, str]:
    if not pdf:
        return {}
    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        return {i: page.get_text("text").strip() for i, page in enumerate(d)}


def _tiles(png: bytes, aspect: float = TILE_ASPECT) -> list[bytes]:
    """Cut a tall page image into tiles at most `aspect` times as tall as wide."""
    im = PILImage.open(io.BytesIO(png))
    w, h = im.size
    step = int(w * aspect)
    if h <= step * 1.15:
        return [png]
    out = []
    for top in range(0, h, step):
        buf = io.BytesIO()
        im.crop((0, top, w, min(top + step, h))).save(buf, "PNG", optimize=True)
        out.append(buf.getvalue())
    return out


async def _ensure_folder(bridge: InkBridge, lib: Library, path: str) -> tuple[str, bool]:
    """The UUID of the folder at `path` ("" for the top level), making missing folders on the way."""
    path = normalize(path)
    if path == "/":
        return "", False
    if path == TRASH_PATH or path.startswith(TRASH_PATH + "/"):
        raise ToolError("cannot create folders in the trash")
    parent, created = "", False
    walked = ""
    for name in path.strip("/").split("/"):
        walked += "/" + name
        existing = [i for i in lib.by_path.get(walked, []) if i.is_folder]
        if existing:
            parent = existing[0].uuid
            continue
        if any(not i.is_folder for i in lib.by_path.get(walked, [])):
            raise ToolError(f"{walked} is a document, not a folder")
        parent = await bridge.transport.create_folder(name, parent)
        created = True
    return parent, created


# --- server -----------------------------------------------------------------------------------


def create_server(settings: Settings | None = None) -> MCPServer:
    settings = settings or get_settings()
    writable = settings.mcp_access != "read"
    mcp = MCPServer(name="Ink Bridge", instructions=INSTRUCTIONS)
    renderers = Renderers()

    # --- read ---------------------------------------------------------------------------------

    @mcp.tool(annotations=READ)
    async def list_documents(
        folder: str = "/",
        recursive: bool = False,
        query: str | None = None,
        kind: Literal["document", "folder", "notebook", "pdf", "epub"] | None = None,
        modified_since: str | None = None,
        tag: str | None = None,
        limit: int = 100,
    ) -> dict:
        """List or search the library: documents and folders with path, UUID, type, pages, tags.

        folder: a folder path, UUID, or "/Trash". recursive: include subfolders. query: search
        names (case-insensitive) in the folder and all its subfolders. kind: only documents,
        folders, notebooks, PDFs or EPUBs. modified_since: ISO date or datetime. Newest first.
        """
        bridge, lib = await _library(settings)
        folder = folder.strip() or "/"
        if normalize(folder) == TRASH_PATH:
            if lib.scope:
                raise ToolError("the trash is outside the folders this connector may use")
            base, items = TRASH_PATH, lib.trash()
            if recursive or query:
                items = [i for i in lib.items.values() if i.trashed]
        elif normalize(folder) == "/" and not lib.items.get(folder):
            base = "/"
            items = lib.roots() if lib.scope else lib.children(None, recursive=bool(recursive or query))
            if lib.scope and (recursive or query):
                items = [i for r in items for i in [r, *lib.children(r, recursive=True)]]
        else:
            target = _resolve(lib, folder, "folder")
            base, items = target.path, lib.children(target, recursive=bool(recursive or query))
        if query:
            q = query.strip().lower()
            items = [i for i in items if q in i.name.lower()]
        if kind == "document":
            items = [i for i in items if not i.is_folder]
        elif kind == "folder":
            items = [i for i in items if i.is_folder]
        elif kind:
            items = [i for i in items if i.file_type == kind]
        if tag:
            items = [i for i in items if tag.lower() in (t.lower() for t in i.tags)]
        since = _since(modified_since)
        if since is not None:
            items = [i for i in items if (i.last_modified or 0) >= since]
        items.sort(key=lambda i: (not i.is_folder if not (query or since) else False, -(i.last_modified or 0)))
        limit = max(1, min(limit, 500))
        return {
            "folder": base,
            "scope": lib.scope or None,
            "total": len(items),
            "items": [lib.summary(i) for i in items[:limit]],
            **({"truncated": True} if len(items) > limit else {}),
        }

    @mcp.tool(annotations=READ)
    async def get_document(doc: str) -> dict:
        """One document or folder: metadata, tags, and its pages in tablet order (which carry ink,
        highlights or typed text, which PDF page is behind each), plus Ink Bridge feedback if any."""
        bridge, lib = await _library(settings)
        item = _resolve(lib, doc)
        out = lib.summary(item)
        if item.is_folder:
            out["contents"] = [lib.summary(i) for i in lib.children(item)][:200]
            return out
        r = await renderers.get(bridge, item)
        pages = []
        for p in r.pages:
            ink = r.ink(p.number)
            entry = {"number": p.number, "pdf_page": None if p.pdf_page is None else p.pdf_page + 1,
                     "strokes": len(ink.strokes), "highlights": len(ink.highlights), "typed_text": p.has_text,
                     "size_pt": [round(p.width, 1), round(p.height, 1)]}
            if p.template:
                entry["template"] = p.template
            pages.append(entry)
        out["pages"] = len(pages)
        out["page_list"] = pages
        out["created"] = _iso((int(item.metadata.get("createdTime") or 0) / 1000) or None)
        tracked = bridge.store.by_rm_uuid(item.uuid)
        if tracked and tracked.feedback:
            out["feedback_summary"] = tracked.feedback.get("summary")
        return out

    @mcp.tool(annotations=READ)
    async def read_document(doc: str, pages: str | None = None) -> dict:
        """The text layer of a document per page: the PDF's text, text typed on the tablet, and
        highlighted passages. No handwriting (use get_annotations or get_pages for that).

        pages: "1-3,5", "-1" for the last page; default all, cut off at about 60,000 characters.
        """
        bridge, lib = await _library(settings)
        item = _resolve(lib, doc, "document")
        r = await renderers.get(bridge, item)
        pdf_text = await asyncio.to_thread(_pdf_text, r.doc.pdf)
        typed = r.text()
        out, total, truncated = [], 0, None
        for n in _pages(pages, len(r.pages)):
            info = r.pages[n - 1]
            entry: dict[str, Any] = {"number": n}
            if info.pdf_page is not None and pdf_text.get(info.pdf_page):
                entry["text"] = pdf_text[info.pdf_page]
            if typed[n - 1].strip():
                entry["typed_text"] = typed[n - 1]
            hl = [{"text": h.text, "color": h.color} for h in r.ink(n).highlights if h.text.strip()]
            if hl:
                entry["highlights"] = hl
            size = sum(len(v) for v in (entry.get("text", ""), entry.get("typed_text", "")))
            if total + size > MAX_TEXT_CHARS and out:
                truncated = n
                break
            total += size
            out.append(entry)
        result: dict[str, Any] = {"uuid": item.uuid, "path": item.path, "file_type": item.file_type,
                                  "pages": out}
        if truncated:
            result["truncated"] = f"stopped before page {truncated}; ask for pages=\"{truncated}-\" next"
        if item.file_type == "epub":
            result["note"] = "EPUB text is not extracted; pages hold only ink and typed text"
        return result

    @mcp.tool(annotations=READ)
    async def get_pages(doc: str, pages: str = "1", ink: bool = True, width: int = 1200, skip: int = 0) -> list:
        """Pages as images, rendered as the tablet shows them (template, typed text, highlights, ink).

        pages: "1-3,5", "-1" for the last page. ink=false shows the page as it was before marking.
        Tall pages are cut into tiles; at most 8 images per call, skip=N continues after N images.
        """
        bridge, lib = await _library(settings)
        item = _resolve(lib, doc, "document")
        r = await renderers.get(bridge, item)
        width = max(200, min(width, 2000))
        numbers = _pages(pages, len(r.pages))

        def render() -> list[tuple[int, int, int, bytes]]:
            out = []
            for n in numbers:
                tiles = _tiles(r.png(n, width, ink))
                out += [(n, i + 1, len(tiles), t) for i, t in enumerate(tiles)]
            return out

        try:
            images = await asyncio.to_thread(render)
        except PageNotFound as e:
            raise ToolError(str(e)) from None
        shown = images[skip:skip + MAX_IMAGES]
        blocks: list = []
        for n, tile, tiles, png in shown:
            blocks.append(f"Page {n}" + (f", part {tile} of {tiles}" if tiles > 1 else ""))
            blocks.append(Image(data=png, format="png"))
        rest = len(images) - skip - len(shown)
        if rest > 0:
            blocks.append(f"{rest} more images; call again with skip={skip + len(shown)}.")
        if not blocks:
            blocks.append(f"Nothing left to show: {len(images)} images in all.")
        return blocks

    @mcp.tool(annotations=READ)
    async def get_strokes(
        doc: str,
        pages: str | None = None,
        detail: Literal["full", "xy", "bbox"] = "full",
        offset: int = 0,
        limit: int = 300,
    ) -> dict:
        """Raw pen data: every stroke with its tool, colour, thickness and sampled points, plus
        highlights (text and rectangles) and typed text, per page.

        Coordinates are .rm units (x = 0 at the page's horizontal centre, y down from the top;
        ~226 per inch). A point is [x, y, pressure, width, speed, direction] for detail="full"
        (pressure 0-255; width and speed in 1/4 units; direction 0-255 for 0-360 degrees), [x, y]
        for "xy"; "bbox" leaves points out. Each page's to_pdf_points maps units onto the page's
        size in points (x_pt = x*scale + x0, y_pt = y*scale), which get_pages images scale from.
        Strokes are paged over the whole request: offset/limit, next_offset when more remain.
        """
        bridge, lib = await _library(settings)
        item = _resolve(lib, doc, "document")
        r = await renderers.get(bridge, item)
        limit = max(1, min(limit, 2000))
        out_pages, seen, emitted, next_offset = [], 0, 0, None
        for n in _pages(pages, len(r.pages)):
            info, ink = r.pages[n - 1], r.ink(n)
            scale, x0 = r.mapping(n)
            strokes = []
            for st in ink.strokes:
                if seen < offset:
                    seen += 1
                    continue
                if emitted >= limit:
                    next_offset = seen
                    break
                seen += 1
                emitted += 1
                xs, ys = [s.x for s in st.samples], [s.y for s in st.samples]
                stroke: dict[str, Any] = {
                    "tool": _tool_name(st.tool), "color": st.color_name, "rgb": _hex(st.color),
                    "thickness": round(st.thickness, 3),
                    "bbox": [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)],
                }
                if st.highlighter:
                    stroke["highlighter"] = True
                if detail == "full":
                    stroke["points"] = [[round(s.x, 2), round(s.y, 2), s.pressure, s.width, s.speed, s.direction]
                                        for s in st.samples]
                elif detail == "xy":
                    stroke["points"] = [[round(s.x, 2), round(s.y, 2)] for s in st.samples]
                strokes.append(stroke)
            page: dict[str, Any] = {
                "number": n, "page_id": info.id,
                "pdf_page": None if info.pdf_page is None else info.pdf_page + 1,
                "size_pt": [round(info.width, 2), round(info.height, 2)],
                "to_pdf_points": {"scale": round(scale, 6), "x0": round(x0, 3)},
                "strokes": strokes,
            }
            if ink.highlights:
                page["highlights"] = [{"text": h.text, "color": h.color,
                                       "rects": [[round(v, 1) for v in rect] for rect in h.rects]}
                                      for h in ink.highlights]
            text = r.text()[n - 1]
            if text.strip():
                page["typed_text"] = text
            out_pages.append(page)
            if next_offset is not None:
                break
        result: dict[str, Any] = {"uuid": item.uuid, "path": item.path, "pages": out_pages}
        if next_offset is not None:
            result["next_offset"] = next_offset
        return result

    @mcp.tool(annotations=READ)
    async def get_annotations(doc: str, refresh: bool = False) -> list:
        """The handwriting on a document interpreted into feedback JSON (marks with the text they
        point at, instructions, free notes, highlights, a summary), plus the inked strips as images.

        Documents returned through /Outbox answer from what was filed then. Any other document is
        read where it is: the first call interprets the ink with Claude (takes a while), later
        calls reuse that until the ink changes. refresh=true interprets again.
        """
        bridge, lib = await _library(settings)
        item = _resolve(lib, doc, "document")
        tracked = bridge.store.by_rm_uuid(item.uuid)
        if tracked and tracked.feedback and tracked.status == "pulled" and not refresh:
            feedback, images = bridge.get_annotations(tracked.doc_id)
            body: Any = feedback[0] if len(feedback) == 1 else {"parts": feedback}
            body = {"uuid": item.uuid, "path": item.path, **body} if isinstance(body, dict) else body
        else:
            feedback_, images = await bridge.annotations_of(item.uuid, item.version, item.name, refresh)
            body = {"path": item.path, **feedback_}
            if not (feedback_.get("marks") or feedback_.get("highlights") or feedback_.get("free_notes")):
                body["note"] = "no handwriting or highlights found"
        out: list = [json.dumps(body, indent=2, ensure_ascii=False)]
        out += [Image(path=p) for p in images[:MAX_IMAGES]]
        if len(images) > MAX_IMAGES:
            out.append(f"{len(images) - MAX_IMAGES} more strips not shown; get_pages shows any page.")
        return out

    if not writable:
        return mcp

    # --- write --------------------------------------------------------------------------------

    @mcp.tool(annotations=WRITE)
    async def push_document(
        title: str,
        html: str | None = None,
        markdown: str | None = None,
        pdf_base64: str | None = None,
        url: str | None = None,
        ready_selector: str | None = None,
        folder: str = "/Inbox",
    ) -> dict:
        """Render content into a new document on the tablet: one tall scrolling PDF page (very long
        content is split into parts). Never changes an existing document.

        Pass exactly one of html, markdown, pdf_base64 (passed through as is) or url (public).
        ready_selector waits for a CSS selector before rendering (JS-drawn charts, Mermaid).
        folder: a path; missing folders are created. Returns the Ink Bridge ID and tablet paths.
        """
        bridge, lib = await _library(settings)
        path = _check(lib, folder)
        given = [v for v in (html, markdown, pdf_base64, url) if v]
        if len(given) != 1:
            raise ToolError("pass exactly one of html, markdown, pdf_base64 or url")
        parent, _ = await _ensure_folder(bridge, lib, path)
        try:
            pdf = base64.b64decode(pdf_base64, validate=True) if pdf_base64 else None
        except ValueError:
            raise ToolError("pdf_base64 is not valid base64") from None
        result = await bridge.push_document(
            title, html=html, markdown=markdown, pdf=pdf, url=url, ready_selector=ready_selector,
            source_kind="artifact" if html else None, parent=parent, folder_path=path,
        )
        return {"doc_id": result.doc_id, "tablet_paths": result.tablet_paths, "parts": result.parts}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                          open_world_hint=False))
    async def create_folder(path: str) -> dict:
        """Create a folder by path ("/Work/Projects/Q4"), with any missing folders above it."""
        bridge, lib = await _library(settings)
        path = _check(lib, path)
        if path == "/":
            raise ToolError("give a folder path, e.g. /Work/Projects")
        uuid, created = await _ensure_folder(bridge, lib, path)
        return {"uuid": uuid, "path": path, "created": created}

    @mcp.tool(annotations=WRITE)
    async def update_document(
        doc: str,
        name: str | None = None,
        folder: str | None = None,
        pinned: bool | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Rename, move, pin/unpin or re-tag a document or folder. Pages and ink are untouched.

        folder: destination folder path ("/" for the top level; missing folders are created). Also
        restores an item from the trash. tags replaces the whole tag list ([] clears it).
        """
        bridge, lib = await _library(settings)
        item = _resolve(lib, doc)
        metadata: dict[str, Any] = {}
        content: dict[str, Any] = {}
        new_name = item.name
        if name is not None:
            new_name = name.strip()
            if not new_name or "/" in new_name:
                raise ToolError("a name must be non-empty and contain no '/'")
            if new_name != item.name:
                metadata["visibleName"] = new_name
        parent_path = split(item.path)[0] if not item.trashed else None
        if folder is not None:
            parent_path = _check(lib, folder)
            if item.is_folder and within(parent_path, item.path) and not item.trashed:
                raise ToolError(f"cannot move {item.path} into itself")
        new_path = normalize(f"{parent_path}/{new_name}") if parent_path is not None else item.path
        if lib.scope and item.path in lib.scope and new_path != item.path:
            raise ToolError(f"{item.path} is a scope folder of this connector and cannot be renamed or moved")
        if parent_path is not None:
            _check(lib, new_path)
        if pinned is not None and pinned != bool(item.metadata.get("pinned")):
            metadata["pinned"] = pinned
        if tags is not None:
            if item.is_folder:
                raise ToolError("folders have no tags")
            ts = int(time.time() * 1000)
            content["tags"] = [{"name": t.strip(), "timestamp": ts} for t in dict.fromkeys(tags) if t.strip()]
        if folder is not None:
            parent, _ = await _ensure_folder(bridge, lib, parent_path)
            if parent != item.parent:
                metadata["parent"] = parent
        if not metadata and not content:
            return {"changed": False, **lib.summary(item)}
        try:
            await bridge.transport.update(item.uuid, metadata=metadata or None, content=content or None)
        except FileNotFoundError as e:
            raise ToolError(f"{item.path} is gone from the tablet") from e
        _, lib = await _library(settings)
        updated = lib.items.get(item.uuid)
        return {"changed": True, **(lib.summary(updated) if updated else {"uuid": item.uuid, "path": new_path})}

    @mcp.tool(annotations=DESTRUCTIVE)
    async def trash_document(doc: str) -> dict:
        """Move a document or folder (with everything in it) to the tablet's trash. The user can
        restore it there, or update_document(folder=...) brings it back; nothing is deleted."""
        bridge, lib = await _library(settings)
        item = _resolve(lib, doc)
        if item.trashed:
            return {"changed": False, "uuid": item.uuid, "path": item.path}
        if lib.scope and item.path in lib.scope:
            raise ToolError(f"{item.path} is a scope folder of this connector and cannot be trashed")
        try:
            await bridge.transport.update(item.uuid, metadata={"parent": TRASH})
        except FileNotFoundError as e:
            raise ToolError(f"{item.path} is gone from the tablet") from e
        return {"changed": True, "uuid": item.uuid, "was": item.path,
                "restore_with": f"update_document(doc=\"{item.uuid}\", folder=\"{split(item.path)[0]}\")"}

    @mcp.tool(annotations=WRITE)
    async def process_outbox() -> list[dict]:
        """Pull every document the user moved to /Outbox: interpret the ink, file feedback (and to
        Trilium if set up), move the document to /Archive. Then call get_annotations with its ID."""
        return [asdict(d) for d in await get_bridge().poll_outbox()]

    return mcp


mcp = create_server()
