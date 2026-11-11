// remarkling's Kubernetes objects, as a dependency-free jsonnet library.
//
//   local remarkling = import 'github.com/derfred/remarkling/deploy/jsonnet/main.libsonnet';
//   remarkling.objects(remarkling.config { host: 'remarkling.example.com' })
//
// objects(cfg) returns a flat list: Deployment (server + poller), Service,
// a PVC with cfg.storage.persistent, and an Ingress unless cfg.ingress.enabled
// is false. See config.libsonnet
// for every setting and for what the library leaves to the platform.
{
  config:: import 'config.libsonnet',

  objects(cfg):: (
    assert cfg.database != null || cfg.storage.persistent :
           'remarkling: SQLite (database: null) needs storage.persistent: true, or the database dies with the pod';
    local labels = { 'app.kubernetes.io/name': cfg.name };
    local meta(name) = { name: name, namespace: cfg.namespace, labels: labels };

    local env =
      [{ name: 'DATA_DIR', value: '/data' }]
      + (if std.objectHas(cfg.env, 'MCP_ALLOWED_HOSTS') then [] else [{ name: 'MCP_ALLOWED_HOSTS', value: cfg.host }])
      + [{ name: k, value: cfg.env[k] } for k in std.objectFields(cfg.env)]
      // Deliberately not optional: a missing Secret or key stops the pods
      // (CreateContainerConfigError) instead of silently falling back to SQLite.
      + (if cfg.database == null then [] else [{
           name: 'DATABASE_URL',
           valueFrom: { secretKeyRef: { name: cfg.database.secretName, key: cfg.database.key } },
         }]);

    local container(name, args, resources) = {
      name: name,
      image: cfg.image,
      args: args,
      securityContext: {
        allowPrivilegeEscalation: false,
        capabilities: { drop: ['ALL'] },
      },
      envFrom: [{ secretRef: { name: cfg.secretName } }],
      env: env,
      resources: resources,
      volumeMounts: [{ name: 'data', mountPath: '/data' }],
    };

    [
      {
        apiVersion: 'apps/v1',
        kind: 'Deployment',
        metadata: meta(cfg.name) + { labels+: { 'app.kubernetes.io/version': cfg.version } },
        spec: {
          replicas: 1,
          // One pod at a time: the poll lock is per pod (a rolling update
          // would briefly run two pollers), and a ReadWriteOnce claim, when
          // persistent, mounts on one node only.
          strategy: { type: 'Recreate' },
          selector: { matchLabels: labels },
          template: {
            metadata: { labels: labels },
            spec: {
              securityContext: {
                runAsNonRoot: true,
                runAsUser: 1001,  // pwuser in the Playwright image (asserted in the Dockerfile)
                runAsGroup: 1001,
                fsGroup: 1001,
                seccompProfile: { type: 'RuntimeDefault' },
              },
              containers: [
                container('server', ['inkbridge', 'serve', '--port', '8000'], cfg.resources.server) {
                  ports: [{ name: 'http', containerPort: 8000, protocol: 'TCP' }],
                  readinessProbe: {
                    httpGet: { path: '/healthz', port: 'http' },
                    initialDelaySeconds: 5,
                    periodSeconds: 10,
                  },
                  livenessProbe: {
                    httpGet: { path: '/healthz', port: 'http' },
                    initialDelaySeconds: 15,
                    periodSeconds: 30,
                  },
                  volumeMounts+: [{ name: 'shm', mountPath: '/dev/shm' }],
                },
                container('poller', ['python', '-m', 'inkbridge.poller'], cfg.resources.poller),
              ],
              volumes: [
                if cfg.storage.persistent
                then { name: 'data', persistentVolumeClaim: { claimName: cfg.storage.claimName } }
                else { name: 'data', emptyDir: { sizeLimit: cfg.storage.size } },
                { name: 'shm', emptyDir: { medium: 'Memory', sizeLimit: '256Mi' } },
              ],
            },
          },
        },
      },
      {
        apiVersion: 'v1',
        kind: 'Service',
        metadata: meta(cfg.name),
        spec: {
          type: 'ClusterIP',
          selector: labels,
          ports: [{ name: 'http', port: 80, targetPort: 'http', protocol: 'TCP' }],
        },
      },
    ]
    + (if !cfg.storage.persistent then [] else [{
         apiVersion: 'v1',
         kind: 'PersistentVolumeClaim',
         metadata: meta(cfg.storage.claimName),
         spec: {
           accessModes: ['ReadWriteOnce'],
           resources: { requests: { storage: cfg.storage.size } },
         } + (if cfg.storage.storageClass == null then {} else { storageClassName: cfg.storage.storageClass }),
       }])
    + (if !cfg.ingress.enabled then [] else [{
         apiVersion: 'networking.k8s.io/v1',
         kind: 'Ingress',
         metadata: meta(cfg.name) + {
           annotations: {
             'cert-manager.io/cluster-issuer': cfg.ingress.clusterIssuer,
             'nginx.ingress.kubernetes.io/ssl-redirect': 'true',
             'nginx.ingress.kubernetes.io/proxy-body-size': '50m',
             'nginx.ingress.kubernetes.io/proxy-read-timeout': '300',
           } + cfg.ingress.annotations,
         },
         spec: {
           ingressClassName: cfg.ingress.className,
           tls: [{ hosts: [cfg.host], secretName: cfg.ingress.tlsSecretName }],
           rules: [{
             host: cfg.host,
             http: { paths: [{
               path: '/',
               pathType: 'Prefix',
               backend: { service: { name: cfg.name, port: { name: 'http' } } },
             }] },
           }],
         },
       }])
  ),
}
