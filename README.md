# remarkling — reMarkable Ink Bridge

Push HTML, Markdown, PDF or a URL to a reMarkable tablet as one continuous scrolling page, mark it up
by hand, then pull the markup back as structured feedback into Trilium and Claude conversations.

```
push:  chat / Shortcut / CLI ─▶ render tall PDF + rm-xxxxxx ID ─▶ /Inbox on the tablet
track: every poll ─▶ .rm hashes of each pushed doc, in any folder ─▶ has_new_ink, changed pages
pull:  pull_document(id), doc stays put ─┐
       /Outbox (no agent) ───────────────┴▶ download .rmdoc ─▶ strokes over original ─▶ strips + highlights
                                            (Outbox docs then move to /Archive)
interpret: pulled ink ─▶ Claude vision per strip ─▶ marks, notes, summary (optional)
                                                   ├─▶ Trilium note (+ PDFs, labels)
                                                   └─▶ MCP get_annotations (strips as images, always)
```

Pulling needs only the reMarkable transport. Interpretation runs after it, on the files the pull
saved (`ink.rmdoc` keeps the raw strokes), and only when `ANTHROPIC_API_KEY` is set; without it a
pulled doc's interpretation is `off` and an agent reads the strip images itself. A failure in
either stage is stored on the doc (stage, error, attempt count) and shown by `poll_now`,
`list_returned` and `get_annotations`. The same version is retried after 1, 2, 4 ... minutes, at
most every 6 hours; a new version (more ink) is retried at once.

A pushed document is tracked by its reMarkable UUID wherever the user files it. Its ink is the set
of `.rm` file hashes in its cloud index: opening or moving a doc gives it a new version but leaves
those alone, so they tell new strokes from everything else, and checking them downloads nothing
(the indexes are cached). `poll()` records them for every tracked doc and reports the docs whose
ink changed; `document_status` and `list_documents` check on demand and say which pages have ink
not yet pulled. `pull_document` pulls only when the ink differs from the last pull and leaves the
doc where it is, then interprets in the background (its own lock: a pull never waits for a model
call). A doc deleted or trashed on the tablet stops being tracked; `remove_document` trashes one.

## Layout

```
inkbridge/
  core/
    render.py      # HTML/MD/URL -> tall PDF (Playwright), split at h2 over 5,000 mm, stamp /InkBridgeId
    cloud.py       # reMarkable cloud client (sync15 blob API, notifications websocket)
    rmdoc.py       # a document's files (.rmdoc contents): metadata, content, PDF, page .rm
    scene.py       # .rm (v6) page -> strokes with per-point pen data, highlights, typed text (rmscene)
    renderer.py    # any document (notebooks too) -> PDF or per-page PNG, tablet page order
    pens.py        # per-pen stroke width and opacity from pressure, speed, tilt
    pagetext.py    # typed text layout; where text-anchored ink goes
    templates.py   # lined, grid and dotted page templates, redrawn from their names
    overlay.py     # pull pipeline: annotated.pdf + 2x PNG, highlights, ink bands (on renderer.py)
    interpret.py   # 1,400 px strips, 15% overlap, inked strips only -> Claude -> merged feedback
    trilium.py     # ETAPI note per doc; re-pull updates in place
    store.py       # doc state + cache: SQLiteStore or PostgresStore, chosen by DATABASE_URL
    explorer.py    # read-only view of the cache: rebuilds the library from cached blobs
    pipeline.py    # push_document(), pull_document(), check_ink(); poll() = poll_outbox() + check_ink() + interpret_pending()
  mcp_server.py    # push_document, create_upload, pull_document, document_status, list_documents,
                   # remove_document, get_annotations, poll_now, list_returned, list_inbox
  uploads.py       # one-time upload URLs, grants in the store (create_upload -> PUT /upload/<token>)
  api.py           # FastAPI /push /poll /healthz, /api/cache, /api/docs, /ui + MCP at /mcp (no auth of its own)
  web/             # built cache explorer UI (from web/ at the repo root; not in git)
  poller.py        # poll() on cloud websocket notifications (timer fallback)
  cli.py           # inkbridge push|pull|status|poll|inbox|register|serve|poller
web/               # cache explorer UI: Svelte 5 + Tailwind CSS 4, built by Vite into inkbridge/web
prompts/interpret.md
deploy/jsonnet/    # Kubernetes objects as a jsonnet library; VERSION = the release version
```

## Local setup

```sh
uv sync                          # creates .venv from uv.lock, dev group included
uv run playwright install chromium
uv run inkbridge register <code> # code from my.remarkable.com/device/desktop/connect
cp .env.example .env            # fill in RM_DEVICE_TOKEN, ANTHROPIC_API_KEY
uv run inkbridge push spec.html  # M1
uv run inkbridge status          # tracked docs and whether they have new ink
uv run inkbridge pull rm-xxxxxx  # fetch a doc's annotations, wherever it is on the tablet
uv run inkbridge poll            # M2/M3: the Outbox
uv run pytest
(cd web && npm ci && npm run build)  # the cache explorer UI at /ui/; the tests don't need it
```

Add or bump dependencies with `uv add <pkg>` (or `uv add --dev <pkg>`) and commit `uv.lock`.

Configuration is environment-only; see `inkbridge/config.py` and `.env.example`.
The cloud transport caches index and metadata blobs in the document store (see Deploy; blobs are
immutable, so the cache never goes stale and its rows are safe to delete) and reuses the last listing
while the root hash is unchanged. An idle poll is a single `GET /sync/v4/root`.

The poller does not poll on a timer when it can avoid it. It holds the cloud's notifications
websocket open (`wss://<RM_SYNC_HOST>/notifications/ws/json/1`, override with `RM_NOTIFICATIONS_URL`).
Every sync, including the tablet's, sends a message there, and each message triggers a
`poll()` after a 2 s debounce. A reconnect also triggers one, to catch up on anything missed. While the
socket is up, a safety poll runs every `POLL_FALLBACK_S` (900 s). Without a socket
(`RM_NOTIFICATIONS_URL=off`, or while reconnecting with backoff), it polls every `POLL_INTERVAL_S` (60 s).
Trilium is skipped until `TRILIUM_URL`, `TRILIUM_ETAPI_TOKEN` and `TRILIUM_PARENT_NOTE` are all set.

## Things to verify on the device (M0)

These are assumptions the code makes that only the real tablet and cloud can confirm:

- **Tall-page scrolling.** Push a long doc; check that zoom-to-width scrolling works. Uploads set
  `zoomMode: fitToWidth` in `.content`.
- **Stroke mapping.** `overlay.Mapping` maps `.rm` units to PDF points. `auto` picks best-fit for
  regular pages and fit-to-width for tall ones; the width in units is `RM_PAGE_WIDTH_UNITS`
  (1404 on reMarkable 2). Annotate a known spot on the Paper Pro and adjust if strokes land off.
- **Cloud protocol.** `cloud.py` follows the rmapi sync15 flow (`/sync/v4/root`, `/sync/v3/files`,
  generation-checked root swap). Hosts are `RM_AUTH_HOST` / `RM_SYNC_HOST`. If the account uses a
  different schema or host, fix it there.
- **Notifications websocket.** The endpoint (`/notifications/ws/json/1` on the sync host, bearer
  user token) and message shape (`{"message": {"attributes": {"event": "SyncComplete", ...}}}`)
  come from rmapi/rmfakecloud. Annotate a page and move it to Outbox, then check that the poller logs
  `notification SyncComplete` within seconds. If it logs `notifications unavailable`, set
  `RM_NOTIFICATIONS_URL` to the right host, or to `off` to go back to plain polling.
- **Fixtures.** Tests use synthetic `.rm` pages built with rmscene (`tests/rmfixtures.py`). Add the
  three hand-annotated `.rmdoc` exports to `tests/fixtures/` once M0 works.

## Deploy (M5)

remarkling ships as a container image plus a jsonnet library that renders its Kubernetes objects.
This repo publishes releases. It deploys nothing itself: a deployment pins a release of the
library and moves the pin when a new release appears.

- **Releases.** `deploy/jsonnet/VERSION` is the release version. Release `vX.Y.Z` publishes the
  image `ghcr.io/derfred/remarkling:X.Y.Z` and a GitHub release, and the library at tag `vX.Y.Z`
  deploys exactly that image. Release images are never overwritten. To cut one, run the
  **Release** workflow with a bump (`patch`/`minor`/`major`), from the GitHub app or website, or
  run `scripts/release.sh [patch|minor|major]` on `main`. Either way, the commit is tested first.
  `pyproject.toml` keeps its own version.
- **CI** (`.github/workflows/ci.yml`) runs the tests and checks the library on every push. It also
  builds `sha-<full sha>` images for trying a commit out.
- **Manifests.** The library in `deploy/jsonnet/` is dependency-free. Vendor it, e.g. with
  jsonnet-bundler (`jb install github.com/derfred/remarkling/deploy/jsonnet@vX.Y.Z`), and render it:

  ```jsonnet
  local remarkling = import 'github.com/derfred/remarkling/deploy/jsonnet/main.libsonnet';
  remarkling.objects(remarkling.config {
    host: 'remarkling.example.com',
    env: { TRILIUM_URL: 'http://trilium.trilium.svc.cluster.local' },
  })
  ```

  That gives the Deployment and the Service (`<name>.<namespace>`, port 80). Every setting and
  its default is in `deploy/jsonnet/config.libsonnet`. The library renders no Ingress, no
  Namespace, no Secret and no RBAC: the deployment provides those, and decides whether and how
  the Service is exposed (see Access control). `host` is optional: the public hostname, if the
  Service is exposed under one, so `/mcp` accepts it.
- **Credentials** come from the Secret named by `secretName` (default `remarkling-secrets`).
  Every key becomes an env var (`inkbridge/config.py`): `RM_DEVICE_TOKEN`, `ANTHROPIC_API_KEY`,
  and for Trilium `TRILIUM_ETAPI_TOKEN` and `TRILIUM_PARENT_NOTE`. Non-secret settings go in the
  library's `env`. `MCP_ALLOWED_HOSTS` defaults to `host` (if set) plus the Service's in-cluster names.
  Env is read only at start, so restart the Deployment after a credential changes.
- **Postgres.** By default `DATABASE_URL` comes from the Secret `postgres-app`, key `uri`: what
  CloudNativePG generates for a Cluster named `postgres`. Point `database` at another Secret, or
  set it to `null` for SQLite, which also needs `storage: { persistent: true }`.
- **Files** (`DATA_DIR`) are on an `emptyDir` by default: with Postgres, nothing there has to
  outlive the pod. A restart drops the rendered files, so `get_annotations` then returns earlier
  documents' feedback without their strip images. `storage: { persistent: true }` puts them on a
  PersistentVolumeClaim instead.

One Deployment (`Recreate`), two containers (`server`, `poller`) sharing the data volume. Polls are
serialised by an asyncio lock in-process and a file lock on the volume across containers. Rendered
files are pruned after `RETENTION_DAYS` (30); feedback JSON stays in the store.

Document state (doc IDs, reMarkable UUIDs, status, Trilium links, feedback JSON) lives in the
database `DATABASE_URL` names; its tables are created on start:

| `DATABASE_URL`                          | Store                                         |
| --------------------------------------- | --------------------------------------------- |
| unset or empty                          | SQLite at `DATA_DIR/inkbridge.sqlite3`        |
| `sqlite:///relative/path.db`            | SQLite, relative to the working directory     |
| `sqlite:////absolute/path.db`           | SQLite, absolute path (four slashes)          |
| `postgresql://user:pass@host:5432/db`   | Postgres (`postgres://` too; libpq options as query parameters) |

The same store also holds a `cache` table: the cloud transport keeps the root, document index,
metadata and content blobs there, keyed by hash and pruned to what the current root references, so
the server and poller share them and a restart does not refetch the whole library. The data volume
holds the rendered and annotated files and the cross-container poll lock; a SQLite file must sit on
it too, so both containers see the same one, and then the volume has to be persistent. Switching
does not migrate existing rows.

Schema changes are migrations: `SCHEMA` in `store.py` is the v0.2.1 baseline, and `MIGRATIONS` is an
append-only list of SQL steps applied in order on start, each once and in its own transaction, with
the count applied kept in `schema_version`. Both backends run the same steps; Postgres under an
advisory lock, SQLite under `BEGIN IMMEDIATE`, so a server and poller starting together don't race.
A database migrated by a newer release is refused rather than used. To change the schema, append a
step (and update `Doc`/`_COLUMNS`); never edit one that has shipped.

## Access control

The app does no authentication: whoever can reach it can use every route. Access is the
infrastructure's job, outside this repo, so it can differ per realm and per path into the cluster
(external through whatever exposes the Service, internal through the Service itself):

| Realm   | Paths                                                     | Typical clients                     |
| ------- | --------------------------------------------------------- | ----------------------------------- |
| API     | `/api/`, `/push`, `/poll` (and `/docs`, `/openapi.json`)  | web UI, iPhone Shortcuts, REST      |
| MCP     | `/mcp`                                                    | claude.ai connector, in-cluster agents |
| upload  | `/upload/<token>`                                         | agents' shells (`curl -T`), via `create_upload` |
| public  | `/healthz`, `/`, `/ui/`                                   | probes; the UI's static files (no data) |

Externally, put an auth proxy in front of each realm (e.g. oauth2-proxy via Ingress annotations),
except the upload realm: its URLs carry their own credential, a random token that expires after
`UPLOAD_TTL_S` (15 min) and works for one upload. The store keeps the grant behind it (the
`uploads` table, with the token's SHA-256 only and the doc each upload pushed) so a URL stays
single-use across restarts and containers. So
whatever exposes the app must let `/upload/` through without the API realm's auth (agents' shells
have no browser session): with ingress-nginx, a separate Ingress for the `/upload/` prefix
without the auth annotations; with oauth2-proxy, `--skip-auth-route=^/upload/`. Its body size
limit should allow `UPLOAD_MAX_MB` (50 MB).

`create_upload` builds its URL from the request that reached `/mcp`: `X-Forwarded-Proto`,
`X-Forwarded-Host` and `X-Forwarded-Prefix` if the proxy sets them (ingress-nginx does), else the
`Host` header. An agent connected through the public host gets a public URL, one connected
through the Service a Service URL, so it needs no configuration in or out of the cluster.
`PUBLIC_URL` (the library's optional `publicUrl`) overrides it, for a proxy that rewrites the host
without saying so.
Internally, restrict who can reach the Service (NetworkPolicy, or a mesh for per-path rules).
Without either, the app is open to the internet or to every pod in the cluster.

## iPhone Shortcuts (M6)

- **Pull reMarkable**: Get Contents of URL, POST `https://<host>/poll`, header
  whatever credentials your proxy asks for on `/poll` → Get Dictionary Value `count`, `titles` → if count > 0, Show
  Notification "Pulled N: titles" (optionally open `trilium_url`), else "Nothing in Outbox".
- **Send to reMarkable**: Show in Share Sheet for URLs and PDFs; POST `https://<host>/push` as a form
  with `url` or `file` (and optional `title`), same credentials.

## Cache explorer

`https://<host>/ui/` is a mobile-first web UI for the cloud blob cache: browse the library as the
last cloud listing saw it (folders, documents, pages, which files are cached, Ink Bridge status),
the raw cache entries by kind (root, index, metadata, content), and cache stats. The page itself
holds no data. It has no login of its own: it relies on the auth proxy in front of the app (see
Access control) and sends its session cookie with every API call. When the session lapses (a 401
or a redirect to the proxy's sign-in) the UI reloads to sign in again. It is read-only and only as fresh as the last poll or push, until you tap the cloud button: that syncs
the cache with the current root, fetching any index, `.metadata` or `.content` blob it lacks (a poll
skips `.content`, which holds page counts and file types). Blobs already cached cost no request.

The UI is a Svelte 5 app styled with Tailwind CSS 4, in `web/`. `npm run build` writes it into
`inkbridge/web/`, which the server serves at `/ui/` (fingerprinted `assets/` cached for good,
`index.html` never); without a build, `/ui/` answers 503. The Docker image builds it in a Node stage.
For UI work, run `inkbridge serve` on :8000 and `npm run dev` in `web/`: Vite serves the app on
http://localhost:5173/ui/ with hot reload and proxies `/api` to the server.

The same data is a REST API:

| Route                           | Returns                                                         |
| ------------------------------- | --------------------------------------------------------------- |
| `/api/cache`                    | counts and bytes by kind, current root, library and health stats |
| `/api/cache/library`            | every document and folder in the cached root (flat; `parent` links them) |
| `/api/cache/library/{uuid}`     | one node: breadcrumbs, metadata, content, its files and whether each is cached |
| `/api/cache/blobs`              | cache entries; `kind`, `q` (key, file or document name), `limit`, `offset` |
| `/api/cache/blobs/{key}`        | one entry decoded: parsed index, JSON or text                   |
| `POST /api/cache/sync`          | sync as above; returns root hash, entries, documents, blobs fetched |

### Downloads

A document's page in the UI downloads its original (PDF or EPUB), any single file from its index,
or the whole document as an `.rmdoc` (a zip with the original, metadata and every page's strokes).
Unlike the rest of the explorer, downloads don't come from the cache. The server fetches them from
the cloud on request, so they work for files marked "not cached". Downloaded files are named after the document, e.g. `Spec.pdf` or `Spec <page>.rm`.

| Route                                | Returns                                                    |
| ------------------------------------ | ---------------------------------------------------------- |
| `/api/docs/{uuid}/files/{name}`      | one file by its index name (`<uuid>.pdf`, `<uuid>/<page>.rm`); 404 if missing |
| `/api/docs/{uuid}/rmdoc`             | the whole document as an `.rmdoc` zip                      |
| `/api/docs/{uuid}/render.pdf`        | the whole document rendered with its ink (see below)       |
| `/api/docs/{uuid}/pages`             | its pages in tablet order: number, page ID, PDF page behind it, ink, size |
| `/api/docs/{uuid}/pages/{n}.png`     | page `n` (1-based) rendered; `width` in pixels (32–4000)   |

### Rendering

`inkbridge/core/renderer.py` renders a document the way the tablet shows it. Each tablet page
becomes one output page, in tablet order, so pages added or deleted on the tablet come out right.
A page is drawn in layers:

1. **Background**: the original PDF page, or for notebooks and pages added on the tablet, a blank
   page at screen size (3:4, `RM_PAGE_WIDTH_UNITS` wide) that grows to fit ink or text beyond the
   screen. Lined, grid and dotted templates are redrawn from the template's name
   (`templates.py`). Other templates (planners, isometric, ...) stay blank, because their images
   live on the tablet; the UI says so per page.
2. **Typed text** (`pagetext.py`): headings, bold, bullets and checkboxes, laid out with MuPDF's
   HTML engine and sans-serif fonts. Ink drawn next to text is stored relative to a character of
   it; it moves down to that character's line.
3. **Highlights and strokes**: each pen has its own model of width and opacity per segment,
   driven by pressure, speed and tilt (`pens.py`): fineliner uniform, ballpoint and pencil fading
   with lighter pressure, brush and calligraphy pen swelling with pressure and tilt, highlighter
   and shader translucent. Hidden layers are skipped.

`.rm` pages are parsed once (`scene.py`). The pull pipeline uses the same renderer (`overlay.py`),
so notebooks returned to the Outbox are interpreted too. Their "original" is the rendering without
ink: the template plus typed text. Strip context comes from that ink-free rendering, so it stays
right when pages were added on the tablet.

The document page in the UI shows the rendered pages on request ("Show pages") and offers the
rendered PDF as a download. Page previews reuse one download per document for 2 minutes; the
rendered PDF is always fetched fresh.

Approximations to check against the tablet (M0):

- **Pens**: the formulas follow rmc (github.com/ricklupton/rmc), not the tablet's own brushes.
  Pencil and brush grain is approximated by opacity.
- **Text**: fonts are not the tablet's, so long paragraphs can wrap differently. Ink anchored to a
  word then follows our line, not the tablet's. The anchor convention (first line's top 88 units
  above the text block's `pos_y`) comes from rmc.
- **Erasers**: eraser strokes are skipped. v6 pages store strokes with the erased parts already
  removed; documents from much older firmware (v5 and earlier) are not readable by rmscene at all.
- **EPUBs**: ink and text are drawn on blank pages, because the tablet lays the book out itself.
- Landscape notebooks render in portrait.

## Claude connector (M7)

Add `https://<host>/mcp` as a custom connector in claude.ai, with whatever credentials your
proxy asks for on `/mcp` (see Access control).
The tablet works best with one tall scrolling page, which the server renders from markdown or
HTML; a PDF keeps its own pages. So the server instructions tell Claude to send the source, never a
PDF made from it: text it wrote as markdown, an artifact as its HTML (read with its own artifact
tools; the server can't fetch private artifacts), a Claude Docs doc as its `markdown` export. A
PDF goes only when a PDF is all there is. (A Claude Docs doc read as an artifact is a viewer that
renders the doc in the browser; its `html` export is a bare fragment without CSS, which renders
with the browser's defaults: tables without borders. The `markdown` export gets the server's
stylesheet.)

Anything big goes around the model instead of through it: a `push_document` argument passes
through Claude's output, which fails long before a large document is done. An agent with a shell
calls `create_upload(title, kind)` instead and runs the command it returns:

```sh
curl -sS --fail-with-body -T spec.md 'https://<host>/upload/<token>'   # create_upload(..., kind="markdown")
# {"doc_id": "rm-abc123", "title": "Spec", "tablet_path": ["/Inbox/Spec · rm-abc123"], "parts": [...]}
```

`kind` says what is sent: `pdf` (the default), `html` or `markdown`; a body that doesn't match
(no `%PDF-` header, or not UTF-8 text) is refused with 415. The URL is good
for one upload within 15 minutes, needs no other credential, and a failed upload can be retried
with it.

The round trip needs no folder moves: Claude keeps the doc ID from `push_document`, the user
annotates the doc wherever it is, and when they say so Claude calls `pull_document(doc_id)`, which
returns the annotated strips as images (and the marks, once interpreted). `document_status` and
`list_documents(changed_only=true)` show which docs have ink not yet pulled. An MCP server cannot
start a turn, so new ink is shown when Claude asks; `poll_now` also lists it under `changed`.
