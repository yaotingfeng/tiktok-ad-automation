#!/usr/bin/env bash
set -euo pipefail

namespace=${K8S_NAMESPACE:-}
host=${INGRESS_HOST:-tk-ada.137-220-150-31.sslip.io}
ingress_name=${INGRESS_NAME:-public}
service_name=tiktok-ad-automation

fail() {
  echo "[deploy/k8s/ingress] $*" >&2
  exit 2
}

[[ "$namespace" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || fail "K8S_NAMESPACE must be a Kubernetes namespace"
[[ "$host" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || fail "INGRESS_HOST must be a DNS host"

echo "namespace=$namespace"
echo "ingress=$ingress_name"
echo "append_host=$host"
echo "service=$service_name"
echo "existing_rules_are_preserved=true"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  exit 0
fi

command -v kubectl >/dev/null 2>&1 || fail "kubectl is required"
kubectl -n "$namespace" get service "$service_name" >/dev/null || fail "project Service does not exist: $service_name"
service_app=$(kubectl -n "$namespace" get service "$service_name" -o jsonpath='{.metadata.labels.app}')
[[ "$service_app" == "tiktok-ad-automation" ]] || fail "refusing a Service that is not labeled for this project"

existing_hosts=$(kubectl -n "$namespace" get ingress "$ingress_name" -o jsonpath='{range .spec.rules[*]}{.host}{"\n"}{end}')
[[ -n "$existing_hosts" ]] || fail "shared Ingress has no existing spec.rules; inspect it before patching"
if printf '%s\n' "$existing_hosts" | grep -Fqx "$host"; then
  fail "host already exists on the shared Ingress: $host"
fi

patch_payload=$(printf '[{"op":"add","path":"/spec/rules/-","value":{"host":"%s","http":{"paths":[{"path":"/","pathType":"Prefix","backend":{"service":{"name":"%s","port":{"number":80}}}}]}}}]' "$host" "$service_name")
kubectl -n "$namespace" patch ingress "$ingress_name" --type=json --patch "$patch_payload"

updated_hosts=$(kubectl -n "$namespace" get ingress "$ingress_name" -o jsonpath='{range .spec.rules[*]}{.host}{"\n"}{end}')
printf '%s\n' "$updated_hosts" | grep -Fqx "$host" || fail "patch returned successfully but the new host was not read back"
echo "readback_host=$host"
