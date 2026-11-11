"""HTTP entry point: FastAPI routes for iPhone Shortcuts plus the MCP endpoint at /mcp."""

from __future__ import annotations

import asyncio
import hmac
import json
import mimetypes
import time
from contextlib import asynccontextmanager
from pathlib import Path, PurePath
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import get_settings
from .core.explorer import KINDS, CacheSnapshot
from .core.pipeline import get_bridge
from .core.renderer import PageNotFound, Renderer
from .core.transport.base import RemoteDoc
from .mcp_server import mcp

MARKDOWN_SUFFIXES = {".md", ".markdown", ".txt"}
HTML_SUFFIXES = {".html", ".htm"}
# The cache explorer UI: a Svelte app in web/, built into here by `npm run build` (not in git).
UI_DIR = Path(__file__).parent / "web"


def _is_public(path: str) -> bool:
    """Served without a token. The UI's files hold no data; its /api/ calls carry the proxy's session."""
    return path in ("/healthz", "/", "/ui") or path.startswith("/ui/")


class UIFiles(StaticFiles):
    """Vite fingerprints everything under assets/, so those can be cached for good; index.html never."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        immutable = PurePath(full_path).parent.name == "assets"
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return response


class TokenAuth:
    """Bearer-token auth per route group. /mcp also accepts ?token= for connectors that cannot set headers.

    /api/ also admits requests an auth proxy vouched for through API_AUTH_HEADER (the web UI's session).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_public(scope["path"]):
            return await self.app(scope, receive, send)
        settings = get_settings()
        is_mcp = scope["path"].startswith("/mcp")
        expected = settings.mcp_token if is_mcp else settings.api_token
        headers = dict(scope.get("headers") or [])
        if (settings.api_auth_header and (scope["path"] + "/").startswith("/api/")
                and headers.get(settings.api_auth_header.encode(), b"").strip()):
            return await self.app(scope, receive, send)
        auth = headers.get(b"authorization", b"").decode()
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not given and is_mcp:
            given = parse_qs(scope.get("query_string", b"").decode()).get("token", [""])[0]
        if not expected:
            response = JSONResponse({"detail": "token not configured"}, status_code=503)
        elif not (given and hmac.compare_digest(given, expected)):
            response = JSONResponse({"detail": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        else:
            return await self.app(scope, receive, send)
        await response(scope, receive, send)


# Types browsers know but mimetypes may not; everything else is application/octet-stream.
FILE_TYPES = {".pdf": "application/pdf", ".epub": "application/epub+zip", ".json": "application/json",
              ".metadata": "application/json", ".content": "application/json", ".pagedata": "text/plain",
              ".rmdoc": "application/zip", ".rm": "application/octet-stream"}  # not RealMedia


def download_name(visible_name: str, doc_uuid: str, name: str) -> str:
    """Name a file after its document: <uuid>.pdf -> Spec.pdf, <uuid>/<page>.rm -> Spec <page>.rm."""
    rest = name.removeprefix(doc_uuid)
    if rest.startswith("/"):
        rest = " " + rest[1:].replace("/", " ")
    base = "".join(c for c in visible_name if c not in '/\\:*?"<>|' and c >= " ").strip() or doc_uuid
    return base + rest


def file_response(data: bytes, filename: str, inline: bool = False, cache: str = "private, no-store") -> Response:
    suffix = PurePath(filename).suffix.lower()
    ctype = FILE_TYPES.get(suffix) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    # filename= for old clients (ASCII only), filename*= (RFC 6266) for the real name.
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    disposition = (f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; "
                   f"filename*=UTF-8''{quote(filename, safe='')}")
    return Response(data, media_type=ctype, headers={"Content-Disposition": disposition, "Cache-Control": cache})


class RenderCache:
    """Renderers of recently viewed documents, so a page of previews downloads the document once.

    Entries live `ttl` seconds: previews may lag the tablet by that much. Full downloads bypass it.
    """

    def __init__(self, ttl: float = 120, size: int = 4):
        self.ttl, self.size = ttl, size
        self._items: dict[str, tuple[float, Renderer]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, uuid: str, fresh: bool = False) -> Renderer:
        lock = self._locks.setdefault(uuid, asyncio.Lock())
        async with lock:  # concurrent previews of one document share one download
            hit = self._items.get(uuid)
            if hit and not fresh and hit[0] > time.monotonic():
                return hit[1]
            try:
                rmdoc = await get_bridge().transport.download(RemoteDoc(uuid, "", ""))
            except FileNotFoundError as e:
                raise HTTPException(404, "no such document") from e
            rm_width = get_settings().rm_page_width_units
            renderer = await asyncio.to_thread(Renderer, rmdoc, rm_width)
            self._items.pop(uuid, None)
            self._items[uuid] = (time.monotonic() + self.ttl, renderer)
            while len(self._items) > self.size:
                oldest = next(iter(self._items))
                del self._items[oldest]
                self._locks.pop(oldest, None)
            return renderer


def _mcp_app():
    settings = get_settings()
    security = (
        TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=settings.mcp_allowed_hosts,
                                  allowed_origins=[f"https://{h}" for h in settings.mcp_allowed_hosts]
                                  + ["https://claude.ai"])
        if settings.mcp_allowed_hosts
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    return mcp.streamable_http_app(streamable_http_path="/mcp", transport_security=security,
                                   stateless_http=True, host="0.0.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield
    await get_bridge().aclose()


def create_app() -> ASGIApp:
    mcp_app = _mcp_app()
    app = FastAPI(title="Ink Bridge", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/poll")
    async def poll():
        docs = await get_bridge().poll_outbox()
        return {
            "count": len(docs),
            "titles": [d.title for d in docs],
            "trilium_url": docs[0].trilium_url if docs else None,
        }

    @app.post("/push")
    async def push(
        file: UploadFile | None = File(None),
        url: str | None = Form(None),
        title: str | None = Form(None),
    ):
        bridge = get_bridge()
        if file is None and not url:
            raise HTTPException(400, "send a file or a url")
        if file is None:
            result = await bridge.push_document(title, url=url, source_kind="url", source_ref=url)
        else:
            data = await file.read()
            suffix = PurePath(file.filename or "").suffix.lower()
            ctype = (file.content_type or "").split(";")[0]
            name = title or PurePath(file.filename or "").stem or None
            if ctype == "application/pdf" or suffix == ".pdf" or data[:5] == b"%PDF-":
                result = await bridge.push_document(name, pdf=data, source_ref=file.filename)
            elif ctype == "text/html" or suffix in HTML_SUFFIXES:
                result = await bridge.push_document(name, html=data.decode(), source_ref=file.filename)
            elif ctype in ("text/markdown", "text/plain") or suffix in MARKDOWN_SUFFIXES:
                result = await bridge.push_document(name, markdown=data.decode(), source_ref=file.filename)
            else:
                raise HTTPException(415, f"unsupported file type {ctype or suffix}")
        return {"doc_id": result.doc_id, "title": result.title}

    # --- cache explorer: read-only REST API under /api/cache, web UI at /ui ---------------------

    def snapshot() -> CacheSnapshot:
        return CacheSnapshot(get_bridge().store)

    @app.get("/api/cache")
    def cache_summary():
        return snapshot().summary()

    @app.post("/api/cache/sync")
    async def cache_sync():
        sync = getattr(get_bridge().transport, "sync", None)
        if sync is None:
            raise HTTPException(501, "only the cloud transport keeps a cache")
        return await sync()

    @app.get("/api/cache/library")
    def cache_library():
        return {"items": snapshot().library()}

    @app.get("/api/cache/library/{uuid}")
    def cache_node(uuid: str):
        detail = snapshot().node_detail(uuid)
        if detail is None:
            raise HTTPException(404, "not in the cached library")
        return detail

    @app.get("/api/cache/blobs")
    def cache_blobs(
        kind: str | None = Query(None, pattern="^(" + "|".join(KINDS) + ")$"),
        q: str = "",
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        return snapshot().blob_list(kind, q, limit, offset)

    @app.get("/api/cache/blobs/{key:path}")
    def cache_blob(key: str):
        detail = snapshot().blob_detail(key)
        if detail is None:
            raise HTTPException(404, "no such cache entry")
        return detail

    # --- downloads: a document's files, fetched from the tablet (cloud or SSH), not the cache -----

    async def visible_name(uuid: str) -> str:
        node = snapshot().nodes.get(uuid)
        if node is not None and node.metadata is not None:
            return node.name
        try:
            meta = json.loads(await get_bridge().transport.download_file(uuid, f"{uuid}.metadata"))
            return meta.get("visibleName") or uuid
        except (FileNotFoundError, ValueError):
            return uuid

    @app.get("/api/docs/{uuid}/rmdoc")
    async def download_rmdoc(uuid: str):
        """The whole document as an .rmdoc (zip): the original PDF/EPUB, metadata and page strokes."""
        try:
            rmdoc = await get_bridge().transport.download(RemoteDoc(uuid, "", ""))
        except FileNotFoundError as e:
            raise HTTPException(404, "no such document") from e
        name = rmdoc.metadata.get("visibleName") or await visible_name(uuid)
        return file_response(rmdoc.to_zip(), download_name(name, uuid, ".rmdoc"))

    @app.get("/api/docs/{uuid}/files/{name:path}")
    async def download_file(uuid: str, name: str):
        """One file of a document, by its name in the doc index ("<uuid>.pdf", "<uuid>/<page>.rm")."""
        try:
            data = await get_bridge().transport.download_file(uuid, name)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e
        return file_response(data, download_name(await visible_name(uuid), uuid, name))

    # --- rendering: pages as the tablet shows them, ink included (see core/renderer.py) ---------

    renders = RenderCache()

    def page_or_404(fn, *args):
        try:
            return fn(*args)
        except PageNotFound as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/docs/{uuid}/pages")
    async def doc_pages(uuid: str):
        """Every page in tablet order: number, page ID, the PDF page behind it, ink, size in points."""
        return {"pages": [p.as_dict() for p in (await renders.get(uuid)).pages]}

    @app.get("/api/docs/{uuid}/pages/{number}.png")
    async def page_png(uuid: str, number: int, width: int | None = Query(None, ge=32, le=4000)):
        renderer = await renders.get(uuid)
        png = await asyncio.to_thread(page_or_404, renderer.png, number, width)
        name = download_name(await visible_name(uuid), uuid, f" p{number}.png")
        return file_response(png, name, inline=True, cache="private, max-age=60")

    @app.get("/api/docs/{uuid}/render.pdf")
    async def render_pdf(uuid: str):
        """The whole document rendered with its ink, fetched fresh from the tablet."""
        renderer = await renders.get(uuid, fresh=True)
        pdf = await asyncio.to_thread(page_or_404, renderer.pdf)
        name = renderer.doc.metadata.get("visibleName") or await visible_name(uuid)
        return file_response(pdf, download_name(name, uuid, " (rendered).pdf"))

    @app.get("/", include_in_schema=False)
    @app.get("/ui", include_in_schema=False)
    async def to_ui():
        return RedirectResponse("/ui/")

    if (UI_DIR / "index.html").is_file():
        app.mount("/ui", UIFiles(directory=UI_DIR, html=True), name="ui")
    else:
        @app.get("/ui/{path:path}", include_in_schema=False)
        async def ui_missing(path: str):
            return HTMLResponse("The web UI is not built: run <code>npm ci && npm run build</code> in web/.",
                                status_code=503)

    app.mount("/", mcp_app)
    return TokenAuth(app)


app = create_app()
