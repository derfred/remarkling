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
#   scripts/deploy.sh [git-sha]     # default: HEAD; CI must have built it
#
# Env: KUBECONFIG (default ~/.kube/config, or remarkling.kubeconfig in the
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

if [[ -z "${KUBECONFIG:-}" && -f "$PROJECT_DIR/remarkling.kubeconfig" ]]; then
  KUBECONFIG="$PROJECT_DIR/remarkling.kubeconfig"
fi
KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export KUBECONFIG

kapply() {
  kubectl -n "$NAMESPACE" apply --server-side --force-conflicts \
    --field-manager="$FIELD_MANAGER" -f "$@"
}

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

# --- Load configuration from .env.production --------------------------------
ENV_FILE="$PROJECT_DIR/.env.production"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.example to .env.production and fill in production values." >&2
  exit 1
fi

# Infrastructure values, rendered into the manifests.
DOMAIN=""
STORAGE_CLASS=""
TRILIUM_URL=""
TRILIUM_PUBLIC_URL=""
POLL_INTERVAL_S="300"

# Keys copied into the remarkling-secrets Secret (when non-empty).
SECRET_KEYS=(RM_TRANSPORT RM_DEVICE_TOKEN RM_AUTH_HOST RM_SYNC_HOST RM_PAGE_WIDTH_UNITS
             RM_INBOX RM_OUTBOX RM_ARCHIVE ANTHROPIC_API_KEY INTERPRET_MODEL
             TRILIUM_ETAPI_TOKEN TRILIUM_PARENT_NOTE API_TOKEN MCP_TOKEN RETENTION_DAYS)
declare -A SECRET_VALUES=()

# Parsed manually (not sourced) so values with shell metacharacters survive.
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ "$line" =~ ^[[:space:]]*# || -z "${line//[[:space:]]/}" ]] && continue
  if [[ "$line" =~ ^([A-Z0-9_]+)=(.*)$ ]]; then
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    value="${value#\"}"; value="${value%\"}"
    case "$key" in
      DOMAIN)             DOMAIN="$value" ;;
      STORAGE_CLASS)      STORAGE_CLASS="$value" ;;
      TRILIUM_URL)        TRILIUM_URL="$value" ;;
      TRILIUM_PUBLIC_URL) TRILIUM_PUBLIC_URL="$value" ;;
      POLL_INTERVAL_S)    if [[ -n "$value" ]]; then POLL_INTERVAL_S="$value"; fi ;;
      *)
        for k in "${SECRET_KEYS[@]}"; do
          if [[ "$k" == "$key" ]]; then SECRET_VALUES[$key]="$value"; fi
        done ;;
    esac
  fi
done < "$ENV_FILE"

missing=()
for var in DOMAIN STORAGE_CLASS; do
  if [[ -z "${!var}" ]]; then missing+=("$var"); fi
done
for var in RM_DEVICE_TOKEN ANTHROPIC_API_KEY API_TOKEN MCP_TOKEN; do
  if [[ -z "${SECRET_VALUES[$var]:-}" ]]; then missing+=("$var"); fi
done
if (( ${#missing[@]} )); then
  echo "ERROR: not set in $ENV_FILE: ${missing[*]}" >&2
  exit 1
fi

render() {
  sed \
    -e "s|__DOMAIN__|$DOMAIN|g" \
    -e "s|__STORAGE_CLASS__|$STORAGE_CLASS|g" \
    -e "s|__TRILIUM_URL__|$TRILIUM_URL|g" \
    -e "s|__TRILIUM_PUBLIC_URL__|$TRILIUM_PUBLIC_URL|g" \
    -e "s|__POLL_INTERVAL_S__|$POLL_INTERVAL_S|g" \
    -e "s|__SECRETS_CHECKSUM__|${SECRETS_CHECKSUM:-}|g" \
    "$1"
}

echo "==> Config: domain=$DOMAIN storage-class=$STORAGE_CLASS trilium=${TRILIUM_URL:-disabled}"
echo "==> Deploying image: $FULL_IMAGE"

# --- Application secret -------------------------------------------------------
echo "==> Applying secret remarkling-secrets from .env.production"
secret_args=()
for key in "${SECRET_KEYS[@]}"; do
  if [[ -n "${SECRET_VALUES[$key]:-}" ]]; then
    secret_args+=("--from-literal=$key=${SECRET_VALUES[$key]}")
  fi
done
SECRET_YAML="$(kubectl -n "$NAMESPACE" create secret generic remarkling-secrets \
                 "${secret_args[@]}" --dry-run=client -o yaml)"
SECRETS_CHECKSUM="$(sha256sum <<<"$SECRET_YAML" | cut -c1-16)"
kapply - <<<"$SECRET_YAML"

# --- Application ------------------------------------------------------------
echo "==> Applying volume, deployment, service, ingress"
render "$K8S_DIR/pvc.yaml" | kapply -
render "$K8S_DIR/deployment.yaml" \
  | sed "s|image: ghcr.io/derfred/remarkling:latest.*|image: $FULL_IMAGE|" \
  | kapply -
kapply "$K8S_DIR/service.yaml"
render "$K8S_DIR/ingress.yaml" | kapply -

echo "==> Waiting for rollout to complete"
kubectl -n "$NAMESPACE" rollout status deployment/remarkling --timeout=300s

echo "==> Deployment complete!"
echo "    Image:  $FULL_IMAGE"
echo "    URL:    https://$DOMAIN"
kubectl -n "$NAMESPACE" get pods
