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
    pipeline.py    # push_document(), poll_outbox()
  mcp_server.py    # push_document, poll_now, list_returned, get_annotations, list_inbox
  api.py           # FastAPI /push /poll /healthz + MCP at /mcp, bearer-token auth
  poller.py        # poll_outbox() every POLL_INTERVAL_S
  cli.py           # inkbridge push|poll|inbox|register|serve|poller
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

Runs on the bugzoo cluster as a **custom app** of
[bugzoo-infrastructure](https://github.com/derfred/bugzoo-infrastructure). Production is deployed
**only from that repo**, by Flux: this repo publishes releases, and a release goes live when its
deploy PR in bugzoo-infrastructure is merged.

```
this repo                                   bugzoo-infrastructure                  cluster
Release workflow ("Run workflow", or        app-update opens PR
scripts/release.sh)                         "remarkling v0.2.0": the pin of
  VERSION 0.2.0, tag v0.2.0           ───►  deploy/jsonnet@v0.2.0 + its render
  image ghcr.io/derfred/remarkling:0.2.0    in deploy/remarkling/
                                              │ merge (also from GitHub Mobile)
                                              ▼
                                            main  ◄── Flux applies it within a minute
```

- **Manifests** are the jsonnet library in `deploy/jsonnet/`: `objects(config { host: ... })`
  returns the Deployment, Service, PVC and Ingress. Settings and their defaults are in
  `deploy/jsonnet/config.libsonnet`. bugzoo-infrastructure vendors it at a release tag
  (jsonnet-bundler) and sets the hostname, the Trilium URL and the auth in front of the Ingress.
  The library renders no Namespace, no Secret and no RBAC: those belong to the platform.
- **Versions.** `deploy/jsonnet/VERSION` is the release version. Release `vX.Y.Z` publishes image
  `ghcr.io/derfred/remarkling:X.Y.Z`, and the library at that tag deploys exactly that image.
  Release images are never overwritten. `pyproject.toml` keeps its own version.
- **Releasing.** Run the **Release** workflow with a bump (`patch`/`minor`/`major`), from the
  GitHub app or website, or run `scripts/release.sh [patch|minor|major]` on `main`. Either tests
  the commit, publishes the image and a GitHub release, and pings bugzoo-infrastructure (with the
  `INFRA_DISPATCH_TOKEN` secret; without it, that repo finds the release within the hour).
- **CI** (`.github/workflows/ci.yml`) runs the tests and checks the library on every push, and
  builds `sha-<full sha>` images for trying a commit out. Production never runs those.
- **Configuration.** Credentials come from the `remarkling-secrets` Secret, which the platform
  syncs from 1Password into the namespace; this repo never creates or reads it. Every key becomes
  an env var (`inkbridge/config.py`): `RM_DEVICE_TOKEN`, `ANTHROPIC_API_KEY`, `API_TOKEN`,
  `MCP_TOKEN`, `TRILIUM_ETAPI_TOKEN`, `TRILIUM_PARENT_NOTE`. Non-secret settings are plain env vars
  set through the library's `env` (bugzoo sets `TRILIUM_URL` and `TRILIUM_PUBLIC_URL`).
  `MCP_ALLOWED_HOSTS` defaults to the hostname. Env is read only at start, so after a credential
  changes: `kubectl -n remarkling rollout restart deploy/remarkling`.
- **Postgres** is provisioned by bugzoo-infrastructure as a CloudNativePG Cluster `postgres` in the
  namespace (database and owner `remarkling`). CNPG generates the Secret `postgres-app`, whose `uri`
  the library wires into `DATABASE_URL` of both containers (`database: null` for SQLite instead).
  The instance is small (one instance, 2 Gi node-local, no replica, **no backups yet**): draining
  its node is a short outage and losing the node loses the data. Each process holds one connection.
- **Previews.** To try a commit on the cluster without releasing, render the library with your
  own settings and the `sha-<sha>` image into a namespace of your own. The `remarkling` namespace
  belongs to Flux, which reverts anything applied there by hand.

One Deployment (`Recreate`), two containers (`server`, `poller`) sharing a 1 Gi PVC. Polls are
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
the server and poller share them and a restart does not refetch the whole library. The PVC is still needed either way: it
holds the rendered and annotated files and the cross-container poll lock, and a SQLite file must
sit on it too so both containers see the same one. Switching does not migrate existing rows.

## iPhone Shortcuts (M6)

- **Pull reMarkable**: Get Contents of URL, POST `https://<host>/poll`, header
  `Authorization: Bearer <API_TOKEN>` → Get Dictionary Value `count`, `titles` → if count > 0, Show
  Notification "Pulled N: titles" (optionally open `trilium_url`), else "Nothing in Outbox".
- **Send to reMarkable**: Show in Share Sheet for URLs and PDFs; POST `https://<host>/push` as a form
  with `url` or `file` (and optional `title`), same header.

## Claude connector (M7)

Add `https://<host>/mcp` as a custom connector in claude.ai. The endpoint takes `Authorization:
Bearer <MCP_TOKEN>`, or `?token=<MCP_TOKEN>` in the URL for clients that cannot set headers.
To push an artifact, Claude reads it with its own artifact tools and calls
`push_document(html=...)`. The server can't fetch private artifacts itself.
