#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
manifest_dir="$repo_root/deploy/k8s"
namespace=${K8S_NAMESPACE:-}
app_image=${APP_IMAGE:-}
release_id=${RELEASE_ID:-}
runtime_env_file=${RUNTIME_ENV_FILE:-}
mcp_registration_file=${MCP_CLIENT_REGISTRATION_FILE:-}

fail() {
  echo "[deploy/k8s] $*" >&2
  exit 2
}

[[ "$namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || fail "K8S_NAMESPACE must be a Kubernetes namespace"
[[ "$app_image" =~ ^[A-Za-z0-9./:_-]+$ ]] || fail "APP_IMAGE must be a concrete immutable image reference"
[[ "$release_id" =~ ^[0-9a-f]{40}$ ]] || fail "RELEASE_ID must be a 40-character Git SHA"
[[ -f "$runtime_env_file" ]] || fail "RUNTIME_ENV_FILE does not exist: $runtime_env_file"
[[ -f "$mcp_registration_file" ]] || fail "MCP_CLIENT_REGISTRATION_FILE does not exist: $mcp_registration_file"

for key in SECRET_KEY FIRST_SUPERUSER FIRST_SUPERUSER_PASSWORD DATABASE_URL REDIS_URL CONNECTION_ENCRYPTION_KEY S3_ENDPOINT_URL S3_BUCKET S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY TIKTOK_CALL_POLICIES; do
  grep -Eq "^${key}=.+$" "$runtime_env_file" || fail "runtime env is missing a non-empty ${key}"
done

if grep -Eq '^DATABASE_URL=.*@(localhost|127\.0\.0\.1)(:|/)' "$runtime_env_file"; then
  fail "DATABASE_URL still points to loopback; use a PostgreSQL endpoint reachable from the cluster"
fi
if grep -Eq '^REDIS_URL=.*@(localhost|127\.0\.0\.1)(:|/)' "$runtime_env_file"; then
  fail "REDIS_URL still points to loopback; use a Redis endpoint reachable from the cluster"
fi

job_name="tiktok-ad-automation-migration-${release_id}"
rendered_manifest=$(mktemp)
rendered_job=$(mktemp)
trap 'rm -f "$rendered_manifest" "$rendered_job"' EXIT
sed "s|__APP_IMAGE__|$app_image|g" "$manifest_dir/tk-ada-workloads.yaml" >"$rendered_manifest"
sed -e "s|__APP_IMAGE__|$app_image|g" -e "s|__RELEASE_ID__|$release_id|g" "$manifest_dir/migration-job.yaml" >"$rendered_job"

echo "namespace=$namespace"
echo "image=$app_image"
echo "migration_job=$job_name"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "dry-run: would create runtime and MCP registration Secrets"
  echo "dry-run: would apply static resources, run migration, then apply workloads"
  exit 0
fi

command -v kubectl >/dev/null 2>&1 || fail "kubectl is required"
kubectl get namespace "$namespace" >/dev/null || fail "namespace does not exist: $namespace"

kubectl -n "$namespace" create secret generic tiktok-ad-automation-runtime \
  --from-env-file="$runtime_env_file" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$namespace" create secret generic tiktok-ad-automation-mcp-registration \
  --from-file=mcp-client.json="$mcp_registration_file" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$namespace" apply -f "$manifest_dir/tk-ada.yaml"
if kubectl -n "$namespace" get job "$job_name" >/dev/null 2>&1; then
  completed=$(kubectl -n "$namespace" get job "$job_name" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}')
  [[ "$completed" == "True" ]] || fail "migration Job already exists but is not complete: $job_name"
  echo "migration_job=$job_name (already complete)"
else
  kubectl -n "$namespace" create -f "$rendered_job"
  kubectl -n "$namespace" wait --for=condition=complete --timeout=15m "job/$job_name"
fi
kubectl -n "$namespace" apply -f "$rendered_manifest"

for deployment in \
  tiktok-ad-automation-api \
  tiktok-ad-automation-worker-resource \
  tiktok-ad-automation-worker-results \
  tiktok-ad-automation-worker-builds \
  tiktok-ad-automation-worker-control \
  tiktok-ad-automation-worker-ads-directory \
  tiktok-ad-automation-worker-ads-reporting \
  tiktok-ad-automation-worker-ad-management \
  tiktok-ad-automation-beat; do
  kubectl -n "$namespace" rollout status "deployment/$deployment" --timeout=15m
done
