#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"

registry=${TCR_REGISTRY:-junbo-image.tencentcloudcr.com/prod}
project_name=${PROJECT_NAME:-tiktok-ad-automation}
release_sha=${RELEASE_SHA:-$(git rev-parse HEAD)}
platform=${PLATFORM:-linux/amd64}
image="${registry}/${project_name}:${release_sha}"

if [[ ! "$release_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "release id must be a 40-character Git SHA: $release_sha" >&2
  exit 2
fi

if [[ "${DRY_RUN:-0}" != "1" ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    echo "refusing to build from a dirty worktree; commit or remove all changes first" >&2
    exit 2
  fi
  command -v docker >/dev/null 2>&1 || {
    echo "docker is required; run the deploy-help environment installer first" >&2
    exit 2
  }
  docker buildx version >/dev/null 2>&1 || {
    echo "docker buildx is required" >&2
    exit 2
  }
fi

echo "image=$image"
echo "platform=$platform"
echo "docker buildx build --platform $platform --pull -f backend/Dockerfile -t $image --push ."

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  exit 0
fi

docker buildx build \
  --platform "$platform" \
  --pull \
  -f backend/Dockerfile \
  -t "$image" \
  --push \
  .
