// Defaults for remarkling's Kubernetes objects. A deployment overrides what it
// needs and passes the result to objects():
//
//   local remarkling = import 'github.com/derfred/remarkling/deploy/jsonnet/main.libsonnet';
//   remarkling.objects(remarkling.config)
{
  name: 'remarkling',
  namespace: 'remarkling',

  // The release this library belongs to. `scripts/release.sh` bumps VERSION,
  // and the release workflow publishes the image under the same tag, so a
  // library pinned at tag vX.Y.Z always deploys image X.Y.Z.
  version: std.stripChars(importstr 'VERSION', ' \n'),
  image: 'ghcr.io/derfred/remarkling:' + self.version,

  // The library renders no Ingress: exposing the Service (Ingress, Gateway,
  // auth proxy) is the deployment's job. `host` is the public hostname it is
  // exposed under, if any; with the Service's in-cluster names it is the
  // default for MCP_ALLOWED_HOSTS. null: in-cluster names only.
  host: null,

  // Every key of this Secret becomes an env var of both containers (see
  // inkbridge/config.py). Credentials: RM_DEVICE_TOKEN, ANTHROPIC_API_KEY,
  // TRILIUM_ETAPI_TOKEN. Created by the platform.
  secretName: 'remarkling-secrets',

  // Postgres: DATABASE_URL from this Secret and key (the CloudNativePG
  // `<cluster>-app` Secret). null: no DATABASE_URL, i.e. SQLite on the volume.
  database: { secretName: 'postgres-app', key: 'uri' },

  // Plain, non-secret settings, as env vars of both containers, e.g.
  // { TRILIUM_URL: 'http://trilium.trilium.svc.cluster.local' }. They win over
  // keys of the same name in `secretName`. Poller knobs: RM_NOTIFICATIONS_URL
  // (cloud websocket; 'off' for timer polling), POLL_FALLBACK_S, POLL_INTERVAL_S.
  env: {},

  // DATA_DIR: rendered documents (original/annotated PDFs, the page strips
  // get_annotations returns as images), the lock the two containers poll
  // under, and the SQLite file when `database` is null.
  storage: {
    persistent: false,  // persistent: true makes it a PersistentVolumeClaim `claimName` instead
    size: '1Gi',  // emptyDir sizeLimit, or the claim's request
    claimName: 'remarkling-data',
    storageClass: null,  // null: the cluster's default class
  },

  resources: {
    // Chromium renders and 2x page PNGs.
    server: {
      requests: { cpu: '100m', memory: '384Mi' },
      limits: { cpu: '2', memory: '1536Mi' },
    },
    poller: {
      requests: { cpu: '50m', memory: '256Mi' },
      limits: { cpu: '1', memory: '1Gi' },
    },
  },
}
