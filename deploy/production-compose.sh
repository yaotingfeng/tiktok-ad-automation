#!/usr/bin/env bash
set -euo pipefail

# 固定生产项目、环境文件与版本目录，避免操作同机其他应用。
release_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
release_sha=$(basename "$release_dir")
if [[ $EUID -ne 0 || ! $release_sha =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Run with sudo from /opt/tt-ada/releases/<40-character Git SHA>/deploy/production-compose.sh' >&2
  exit 1
fi
[[ "$release_dir" == "/opt/tt-ada/releases/$release_sha" ]]
[[ -f /etc/tt-ada/production.env ]]
export APP_IMAGE="tt-ada:$release_sha"
exec docker compose --project-name tt-ada-production \
  --project-directory "$release_dir" \
  --env-file /etc/tt-ada/production.env \
  -f "$release_dir/compose.yml" -f "$release_dir/compose.production.yml" "$@"
