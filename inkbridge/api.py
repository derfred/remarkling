"""HTTP entry point: FastAPI routes for iPhone Shortcuts plus the MCP endpoint at /mcp."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path, PurePath
from urllib.parse import parse_qs

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import get_settings
from .core.explorer import KINDS, CacheSnapshot
from .core.pipeline import get_bridge
from .mcp_server import mcp

MARKDOWN_SUFFIXES = {".md", ".markdown", ".txt"}
HTML_SUFFIXES = {".html", ".htm"}
# The cache explorer UI: a Svelte app in web/, built into here by `npm run build` (not in git).
UI_DIR = Path(__file__).parent / "web"


def _is_public(path: str) -> bool:
    """Served without a token. The UI's files hold no data; the app asks for the API token itself."""
    return path in ("/healthz", "/", "/ui") or path.startswith("/ui/")


class UIFiles(StaticFiles):
    """Vite fingerprints everything under assets/, so those can be cached for good; index.html never."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        immutable = PurePath(full_path).parent.name == "assets"
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return response


class TokenAuth:
    """Bearer-token auth per route group. /mcp also accepts ?token= for connectors that cannot set headers."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_public(scope["path"]):
            return await self.app(scope, receive, send)
        settings = get_settings()
        is_mcp = scope["path"].startswith("/mcp")
        expected = settings.mcp_token if is_mcp else settings.api_token
        headers = dict(scope.get("headers") or [])
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
