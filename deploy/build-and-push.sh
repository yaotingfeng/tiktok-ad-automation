#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo_root"
fail() { echo "[deploy/build] $*" >&2; exit 2; }
registry=sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test
project_name=tiktok-ad-automation
# 仓库/项目名固定，避免环境变量误将本项目推入其他项目镜像。
[[ "${ACR_REGISTRY:-$registry}" == "$registry" ]] || fail "ACR_REGISTRY must use the company registry"
[[ "${PROJECT_NAME:-$project_name}" == "$project_name" ]] || fail "PROJECT_NAME must be tiktok-ad-automation"
head_sha=$(git rev-parse HEAD)
release_sha=${RELEASE_SHA:-$head_sha}
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || fail "release id must be a 40-character Git SHA"
[[ "$release_sha" == "$head_sha" ]] || fail "RELEASE_SHA must equal the checked-out HEAD"
platform=${PLATFORM:-linux/amd64}
image="${registry}/${project_name}:${release_sha}"
echo "image=$image"
echo "platform=$platform"
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "dry-run: docker buildx build --platform $platform --pull -f backend/Dockerfile --build-arg VCS_REF=$release_sha -t $image --push ."
  echo "dry-run: cleanliness and remote publication are checked only on a real release"
  exit 0
fi
branch=$(git branch --show-current)
# Jenkins checkout 可为 detached HEAD；后面的实时 main SHA 校验仍为强制门禁。
[[ "$branch" == "main" || -z "$branch" ]] || fail "release must be built from main or its exact detached commit"
[[ -z "$(git status --porcelain --untracked-files=all)" ]] || fail "refusing to build from a dirty worktree"
company_url=$(git remote get-url company)
[[ "$company_url" =~ ^https://git\.gzjunbo\.net/[^/@]+/tiktok-ad-automation\.git$ ]] || fail "company remote must use the project Gitea HTTPS repository"
# 实时查询，避免本地 remote-tracking 引用过期而错误认定已推送。
remote_sha=$(git ls-remote --exit-code company refs/heads/main | awk '{print $1}')
[[ "$remote_sha" == "$release_sha" ]] || fail "push this exact main commit to company Gitea before building"
command -v docker >/dev/null 2>&1 || fail "docker is required"
docker info >/dev/null 2>&1 || fail "Docker engine is not ready"
docker buildx version >/dev/null 2>&1 || fail "docker buildx is required"
docker buildx build --platform "$platform" --pull -f backend/Dockerfile \
  --build-arg "VCS_REF=$release_sha" -t "$image" --push .
