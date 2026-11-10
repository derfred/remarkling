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
    store.py       # SQLite
    pipeline.py    # push_document(), poll_outbox()
  mcp_server.py    # push_document, poll_now, list_returned, get_annotations, list_inbox
  api.py           # FastAPI /push /poll /healthz + MCP at /mcp, bearer-token auth
  poller.py        # poll_outbox() every POLL_INTERVAL_S
  cli.py           # inkbridge push|poll|inbox|register|serve|poller
prompts/interpret.md
k8s/               # deployment (server + poller), pvc, service, ingress; rendered by scripts/deploy.sh
```

## Local setup

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]' && playwright install chromium
inkbridge register <code>        # code from my.remarkable.com/device/desktop/connect
export RM_DEVICE_TOKEN=... ANTHROPIC_API_KEY=...
inkbridge push spec.html         # M1
inkbridge poll                   # M2/M3
pytest
```

Configuration is environment-only; see `inkbridge/config.py` and `.env.example`.
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
[bugzoo-infrastructure](https://github.com/derfred/bugzoo-infrastructure): that repo owns the
`remarkling` namespace, the `registry` image pull secret and the `app` ServiceAccount
(`customApps.apps.remarkling` in `environments/bugzoo/cluster.libsonnet`). Everything inside the
namespace is deployed from here.

- **CI** (`.github/workflows/ci.yml`) runs the tests, then builds and pushes
  `ghcr.io/derfred/remarkling:sha-<full sha>` on every push (`:latest` on `main`).
- **Credentials** come from the `remarkling-secrets` Secret, which the provider injects into the
  namespace; this repo never creates or reads it. Keys: `RM_DEVICE_TOKEN`, `ANTHROPIC_API_KEY`,
  `API_TOKEN`, `MCP_TOKEN` and, with Trilium, `TRILIUM_ETAPI_TOKEN`. Pods don't start
  (`CreateContainerConfigError`) until it exists. Env is read only at start, so after the
  provider rotates it: `kubectl -n remarkling rollout restart deploy/remarkling`.
- **Manifests** are a kustomization in `k8s/` (`kubectl kustomize k8s` shows the result). Plain
  settings (hostname, Trilium URLs, poll interval, retention) are in its `configMapGenerator`; the
  generated ConfigMap name carries a content hash, so changing a value rolls the pods.
- **Deploy** with `scripts/deploy.sh [sha]` (default `HEAD`). It checks that CI built the image,
  pins `sha-<sha>` in a throwaway overlay and runs `kubectl apply -k --server-side` (the `app`
  identity can write but not read Secrets), then waits for the rollout.

```sh
# kubeconfig for the `app` ServiceAccount: minted from bugzoo-infrastructure,
# see environments/bugzoo/custom-apps/README.md ("Handing the app to its team")
export KUBECONFIG=remarkling.kubeconfig  # or drop it in the repo root; it is gitignored
scripts/deploy.sh
```

One Deployment (`Recreate`), two containers (`server`, `poller`) sharing a 1 Gi PVC. Polls are
serialised by an asyncio lock in-process and a file lock on the volume across containers. Rendered
files are pruned after `RETENTION_DAYS` (30); feedback JSON stays in SQLite. Trilium is reached
in-cluster at `http://trilium.trilium.svc.cluster.local`. `DOMAIN` needs a DNS record pointing at
the bugzoo ingress; cert-manager (`letsencrypt-prod`) issues the certificate.

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
