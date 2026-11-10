#!/usr/bin/env bash
# Deploy remarkling into its custom-app namespace on the bugzoo cluster.
#
# The namespace, the `registry` image pull secret and the `app` ServiceAccount
# are owned by bugzoo-infrastructure (environments/bugzoo/cluster.libsonnet,
# customApps.apps.remarkling). This script manages everything inside it.
#
# It runs as that `app` ServiceAccount, which can write anything in the
# namespace but cannot READ Secrets. Everything is therefore applied with
# `kubectl apply --server-side`, which only needs create/patch.
#
# Manifests live in k8s/ as a kustomization; this script pins the image tag and,
# if DOMAIN is set, adds the Ingress from k8s/ingress/ for that hostname. The
# app's env (credentials and instance settings) is NOT deployed from here: the
# provider injects the `remarkling-secrets` Secret into the namespace (see
# k8s/deployment.yaml for the expected keys).
#
#   [DOMAIN=remarkling.example.com] scripts/deploy.sh [git-sha]   # default: HEAD; CI must have built it
#
# Env: DOMAIN (optional; public hostname, should match MCP_ALLOWED_HOSTS in the
# Secret. Unset: no Ingress is applied because it is managed elsewhere, e.g. by
# tanka; an Ingress applied by an earlier run is left in place), KUBECONFIG (default ~/.kube/config, or remarkling.kubeconfig in the
# repo root if present), SKIP_IMAGE_CHECK=1 to deploy a tag CI didn't build.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
K8S_DIR="$PROJECT_DIR/k8s"

IMAGE="ghcr.io/derfred/remarkling"
GHCR_OWNER="derfred"           # GHCR user/org that owns the container package
GHCR_PACKAGE="remarkling"      # GHCR container package name (last path segment of IMAGE)
GH_REPO="derfred/remarkling"   # GitHub repo whose CI builds the image
NAMESPACE="remarkling"
FIELD_MANAGER="remarkling-deploy"

DOMAIN="${DOMAIN:-}"

if [[ -z "${KUBECONFIG:-}" && -f "$PROJECT_DIR/remarkling.kubeconfig" ]]; then
  KUBECONFIG="$PROJECT_DIR/remarkling.kubeconfig"
fi
KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export KUBECONFIG

# Determine image tag: explicit arg, or full SHA of current HEAD. CI publishes
# images tagged "sha-<full-sha>", so match that here.
TAG="${1:-$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null)}"
FULL_IMAGE="$IMAGE:sha-$TAG"

# verify_image — confirm CI actually built and pushed the image we are about to
# deploy, so a missing tag fails here instead of as an ImagePullBackOff.
verify_image() {
  if [[ "${SKIP_IMAGE_CHECK:-0}" == "1" ]]; then
    echo "==> Skipping image verification (SKIP_IMAGE_CHECK=1)"
    return 0
  fi
  if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: 'gh' not found — cannot verify the image exists in GHCR." >&2
    echo "       Install the GitHub CLI or set SKIP_IMAGE_CHECK=1 to bypass." >&2
    exit 1
  fi

  echo "==> Verifying image sha-$TAG exists in GHCR ($GHCR_OWNER/$GHCR_PACKAGE)"
  local tags
  if ! tags="$(gh api --paginate "/users/$GHCR_OWNER/packages/container/$GHCR_PACKAGE/versions" \
                 --jq '.[].metadata.container.tags[]' 2>/dev/null)"; then
    echo "WARNING: could not query GHCR (gh api failed; the token may lack read:packages)." >&2
    echo "         Skipping image verification." >&2
    return 0
  fi
  if grep -qx "sha-$TAG" <<<"$tags"; then
    echo "    - image found"
    return 0
  fi

  echo "ERROR: image sha-$TAG was not found in GHCR — it has not been built." >&2
  local conclusion
  conclusion="$(gh run list --repo "$GH_REPO" --commit "$TAG" --limit 1 \
                  --json status,conclusion --jq '.[0] | "\(.status)/\(.conclusion)"' 2>/dev/null)"
  case "$conclusion" in
    completed/success)
      echo "       CI for this commit succeeded but the image tag is absent — check the build-and-push job." >&2 ;;
    completed/*)
      echo "       CI for this commit did not pass (run: $conclusion). Fix the build before deploying." >&2 ;;
    in_progress/*|queued/*|requested/*|waiting/*)
      echo "       CI for this commit is still running ($conclusion). Wait for it to finish, then retry." >&2 ;;
    "")
      echo "       No CI run found for this commit — has it been pushed to origin?" >&2 ;;
    *)
      echo "       CI status for this commit: $conclusion" >&2 ;;
  esac
  echo "       To bypass this check, re-run with SKIP_IMAGE_CHECK=1." >&2
  exit 1
}

verify_image

echo "==> Deploying image: $FULL_IMAGE"
if [[ -n "$DOMAIN" ]]; then
  echo "    with Ingress for https://$DOMAIN"
else
  echo "    without Ingress (DOMAIN unset: managed elsewhere)"
fi

# A throwaway overlay on k8s/ that sets the image tag (and adds the Ingress), so the committed
# kustomization stays untouched. It sits in the repo because kustomize only
# accepts a relative path to its base.
OVERLAY="$(mktemp -d "$PROJECT_DIR/.deploy-overlay.XXXXXX")"
trap 'rm -rf "$OVERLAY"' EXIT
cat > "$OVERLAY/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
images:
  - name: $IMAGE
    newTag: sha-$TAG
resources:
  - ../k8s
EOF
if [[ -n "$DOMAIN" ]]; then
  cat >> "$OVERLAY/kustomization.yaml" <<EOF
  - ../k8s/ingress
patches:
  - target: {kind: Ingress, name: remarkling}
    patch: |-
      - {op: replace, path: /spec/tls/0/hosts/0, value: "$DOMAIN"}
      - {op: replace, path: /spec/rules/0/host, value: "$DOMAIN"}
EOF
fi

# Server-side apply only needs create/patch, which the `app` identity has even
# for kinds it can't read.
kubectl apply -k "$OVERLAY" --server-side --force-conflicts --field-manager="$FIELD_MANAGER"

echo "==> Waiting for rollout to complete"
if ! kubectl -n "$NAMESPACE" rollout status deployment/remarkling --timeout=300s; then
  echo "ERROR: rollout did not complete." >&2
  echo "       If pods show CreateContainerConfigError, the provider has not injected" >&2
  echo "       the remarkling-secrets Secret (or it lacks a key) — check with the provider." >&2
  kubectl -n "$NAMESPACE" get pods >&2 || true
  exit 1
fi

echo "==> Deployment complete!"
echo "    Image:  $FULL_IMAGE"
if [[ -n "$DOMAIN" ]]; then echo "    URL:    https://$DOMAIN"; fi
kubectl -n "$NAMESPACE" get pods
