# remarkling — reMarkable Ink Bridge

Push HTML, Markdown, PDF or a URL to a reMarkable tablet as one continuous scrolling page, mark it up
by hand, then pull the markup back as structured feedback into Trilium and Claude conversations.

```
push:  chat / Shortcut / CLI ─▶ render tall PDF + rm-xxxxxx ID ─▶ /Inbox on the tablet
pull:  /Outbox ─▶ download .rmdoc ─▶ strokes over original ─▶ Claude vision per strip ─▶ feedback JSON
                                                                 ├─▶ Trilium note (+ PDFs, labels)
                                                                 └─▶ MCP get_annotations
done:  doc moved to /Archive
```

## Layout

```
inkbridge/
  core/
    render.py      # HTML/MD/URL -> tall PDF (Playwright), split at h2 over 5,000 mm, stamp /InkBridgeId
    transport/     # base.py protocol; cloud.py (sync15 blob API); ssh.py (developer mode)
    overlay.py     # rmscene -> annotated.pdf + 2x PNG, highlights, ink bands
    interpret.py   # 1,400 px strips, 15% overlap, inked strips only -> Claude -> merged feedback
    trilium.py     # ETAPI note per doc; re-pull updates in place
    store.py       # doc state + cache: SQLiteStore or PostgresStore, chosen by DATABASE_URL
    explorer.py    # read-only view of the cache: rebuilds the library from cached blobs
    pipeline.py    # push_document(), poll_outbox()
  mcp_server.py    # push_document, poll_now, list_returned, get_annotations, list_inbox
  api.py           # FastAPI /push /poll /healthz, /api/cache, /ui + MCP at /mcp, bearer-token auth
  web/             # built cache explorer UI (from web/ at the repo root; not in git)
  poller.py        # poll_outbox() every POLL_INTERVAL_S
  cli.py           # inkbridge push|poll|inbox|register|serve|poller
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
uv run inkbridge poll            # M2/M3
uv run pytest
(cd web && npm ci && npm run build)  # the cache explorer UI at /ui/; the tests don't need it
```

Add or bump dependencies with `uv add <pkg>` (or `uv add --dev <pkg>`) and commit `uv.lock`.

Configuration is environment-only; see `inkbridge/config.py` and `.env.example`.
The cloud transport caches index and metadata blobs in the document store (see Deploy; blobs are
immutable, so the cache never goes stale and its rows are safe to delete) and reuses the last listing
while the root hash is unchanged. An idle poll is a single `GET /sync/v4/root`; hence the 60 s default for
`POLL_INTERVAL_S`.
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
  different schema or host, fix it there; `RM_TRANSPORT=ssh` is the fallback.
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

  That gives the Deployment, Service and Ingress (nginx and cert-manager by default). Every
  setting and its default is in `deploy/jsonnet/config.libsonnet`. The library renders no
  Namespace, no Secret and no RBAC: the deployment provides those.
- **Credentials** come from the Secret named by `secretName` (default `remarkling-secrets`).
  Every key becomes an env var (`inkbridge/config.py`): `RM_DEVICE_TOKEN`, `ANTHROPIC_API_KEY`,
  `API_TOKEN`, `MCP_TOKEN`, and for Trilium `TRILIUM_ETAPI_TOKEN` and `TRILIUM_PARENT_NOTE`.
  Non-secret settings go in the library's `env`. `MCP_ALLOWED_HOSTS` defaults to the hostname.
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
files are pruned after `RETENTION_DAYS` (30); feedback JSON stays in the store. The public hostname
needs a DNS record pointing at the cluster's ingress.

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

## iPhone Shortcuts (M6)

- **Pull reMarkable**: Get Contents of URL, POST `https://<host>/poll`, header
  `Authorization: Bearer <API_TOKEN>` → Get Dictionary Value `count`, `titles` → if count > 0, Show
  Notification "Pulled N: titles" (optionally open `trilium_url`), else "Nothing in Outbox".
- **Send to reMarkable**: Show in Share Sheet for URLs and PDFs; POST `https://<host>/push` as a form
  with `url` or `file` (and optional `title`), same header.

## Cache explorer

`https://<host>/ui/` is a mobile-first web UI for the cloud blob cache: browse the library as the
last cloud listing saw it (folders, documents, pages, which files are cached, Ink Bridge status),
the raw cache entries by kind (root, index, metadata, content), and cache stats. The page itself
is public and holds no data; it asks for `API_TOKEN` once and keeps it in the browser. It is
read-only and only as fresh as the last poll or push, until you tap the cloud button: that syncs
the cache with the current root, fetching any index, `.metadata` or `.content` blob it lacks (a poll
skips `.content`, which holds page counts and file types). Blobs already cached cost no request.

The UI is a Svelte 5 app styled with Tailwind CSS 4, in `web/`. `npm run build` writes it into
`inkbridge/web/`, which the server serves at `/ui/` (fingerprinted `assets/` cached for good,
`index.html` never); without a build, `/ui/` answers 503. The Docker image builds it in a Node stage.
For UI work, run `inkbridge serve` on :8000 and `npm run dev` in `web/`: Vite serves the app on
http://localhost:5173/ui/ with hot reload and proxies `/api` to the server.

The same data is a REST API with `Authorization: Bearer <API_TOKEN>`:

| Route                           | Returns                                                         |
| ------------------------------- | --------------------------------------------------------------- |
| `/api/cache`                    | counts and bytes by kind, current root, library and health stats |
| `/api/cache/library`            | every document and folder in the cached root (flat; `parent` links them) |
| `/api/cache/library/{uuid}`     | one node: breadcrumbs, metadata, content, its files and whether each is cached |
| `/api/cache/blobs`              | cache entries; `kind`, `q` (key, file or document name), `limit`, `offset` |
| `/api/cache/blobs/{key}`        | one entry decoded: parsed index, JSON or text                   |
| `POST /api/cache/sync`          | sync as above; returns root hash, entries, documents, blobs fetched (501 on SSH) |

## Claude connector (M7)

Add `https://<host>/mcp` as a custom connector in claude.ai. The endpoint takes `Authorization:
Bearer <MCP_TOKEN>`, or `?token=<MCP_TOKEN>` in the URL for clients that cannot set headers.
To push an artifact, Claude reads it with its own artifact tools and calls
`push_document(html=...)`. The server can't fetch private artifacts itself.
