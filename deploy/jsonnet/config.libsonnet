// Defaults for remarkling's Kubernetes objects. A deployment overrides what it
// needs and passes the result to objects():
//
//   local remarkling = import 'github.com/derfred/remarkling/deploy/jsonnet/main.libsonnet';
//   remarkling.objects(remarkling.config { host: 'remarkling.example.com' })
//
// What this library deliberately does NOT render, because the platform owns it:
//   - the Namespace,
//   - any Secret with a value: the app's credentials arrive as the Secret
//     `secretName`, and the database URL as `database.secretName`,
//   - RBAC.
{
  name: 'remarkling',
  namespace: 'remarkling',

  // The release this library belongs to. `scripts/release.sh` bumps VERSION,
  // and the release workflow publishes the image under the same tag, so a
  // library pinned at tag vX.Y.Z always deploys image X.Y.Z.
  version: std.stripChars(importstr 'VERSION', ' \n'),
  image: 'ghcr.io/derfred/remarkling:' + self.version,

  // Public hostname. Also the default for MCP_ALLOWED_HOSTS.
  host: error 'remarkling: config.host must be set',

  ingress: {
    enabled: true,
    className: 'nginx',
    clusterIssuer: 'letsencrypt-prod',
    tlsSecretName: 'remarkling-tls',
    // Extra annotations, e.g. an auth proxy in front of the app.
    annotations: {},
  },

  // Every key of this Secret becomes an env var of both containers (see
  // inkbridge/config.py). Credentials: RM_DEVICE_TOKEN, ANTHROPIC_API_KEY,
  // API_TOKEN, MCP_TOKEN, TRILIUM_ETAPI_TOKEN. Created by the platform.
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
  //
  // By default an emptyDir: with Postgres, nothing on it has to outlive the
  // pod. A restart only drops the files, so get_annotations then returns the
  // feedback of earlier documents without their strip images.
  //
  // persistent: true makes it a PersistentVolumeClaim `claimName` instead,
  // which SQLite needs. ReadWriteOnce, hence one pod and the Recreate strategy;
  // the storage class can't change once the claim exists.
  storage: {
    persistent: false,
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
