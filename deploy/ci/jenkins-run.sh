#!/usr/bin/env bash
# Jenkins 入口：在工具镜像中执行 deploy/local.py，与本机发布共用同一套检查、备份和发布编排。
# 用法：bash deploy/ci/jenkins-run.sh check|deploy|status|recover|finish [local.py 其余参数]
#
# 公司 Jenkins 本身运行在容器里（宿主 /data/jenkins 挂载为 /var/jenkins_home，网络为 host）。
# 工具容器经宿主 docker.sock 启动，挂载路径必须写宿主路径；这里把工作区、私有目录都以
# “宿主路径 = 容器内路径”挂载，检查/发布脚本里再挂载文件时无需任何路径换算。
#
# 工具镜像默认 C 语言环境下，Linux 版 Chromium 无法保存中文下载文件名（退回为 "download"），
# 因此固定以 UTF-8 运行；check.sh 会把 LANG 透传给检查进程。
#
# 需要的环境变量（Jenkinsfile 注入）：TOOLS_IMAGE、TKADA_PRIVATE_DIR、KUBECONFIG_FILE、
# RETIRED_SOURCE_FILE、REG_USER/REG_PASS（ACR）、GIT_USER/GIT_TOKEN（公司 Gitea 只读）。
set -euo pipefail
action=${1:?action required}
shift
workspace=$(pwd -P)
fail() { echo "[jenkins-run] $*" >&2; exit 2; }
for key in TOOLS_IMAGE TKADA_PRIVATE_DIR KUBECONFIG_FILE RETIRED_SOURCE_FILE REG_USER REG_PASS GIT_USER GIT_TOKEN; do
  [[ -n ${!key:-} ]] || fail "$key must be provided by Jenkins"
done

# 从 Jenkins 自身容器的挂载表换算宿主路径，不写死 /data/jenkins。
self_id=$(sed -n 's#.*/docker/\([0-9a-f]\{64\}\).*#\1#p' /proc/1/cgroup | head -1)
[[ -n $self_id ]] || fail "Jenkins must run in a Docker container to map host paths"
mounts=$(docker inspect "$self_id" --format '{{range .Mounts}}{{.Destination}}={{.Source}}{{println}}{{end}}')
host_path() {
  local path=$1 best="" best_source="" destination source
  while IFS='=' read -r destination source; do
    [[ -n $destination ]] || continue
    if [[ $path == "$destination" || $path == "$destination"/* ]] && (( ${#destination} > ${#best} )); then
      best=$destination best_source=$source
    fi
  done <<<"$mounts"
  [[ -n $best ]] || fail "path is not on a host mount: $path"
  printf '%s%s' "$best_source" "${path#"$best"}"
}

# 私有目录保存发布状态、检查记录和备份清单（发布中含密钥副本），权限 0700，不进构建产物。
umask 077
mkdir -p "$TKADA_PRIVATE_DIR"
chmod 700 "$TKADA_PRIVATE_DIR"
private_host=$(host_path "$TKADA_PRIVATE_DIR")
workspace_host=$(host_path "$workspace")
mkdir -p "$TKADA_PRIVATE_DIR/cache/home"

# 本次运行的临时凭据只放在私有目录，结束即删；镜像仓库登录不写入 Jenkins 全局配置。
secrets_dir=$(mktemp -d "$TKADA_PRIVATE_DIR/.run-XXXXXXXX")
container="tkada-jenkins-${BUILD_NUMBER:-manual}-$$"
# Jenkins 中止构建时先停止工具容器再删凭据，避免发布在后台无人看管地继续；
# 停在任何阶段都由 release.py 保留锁和状态，之后用 ACTION=status/recover/finish 处理。
cleanup() {
  docker stop -t 60 "$container" >/dev/null 2>&1 || true
  rm -rf "$secrets_dir"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
secrets_host=$(host_path "$secrets_dir")
cp "$KUBECONFIG_FILE" "$secrets_dir/kubeconfig"
cp "$RETIRED_SOURCE_FILE" "$secrets_dir/retired-source.json"
mkdir "$secrets_dir/docker"
printf '%s' "$REG_PASS" | DOCKER_CONFIG="$secrets_dir/docker" docker login \
  --username "$REG_USER" --password-stdin sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com >/dev/null
printf '#!/bin/sh\ncase "$1" in Username*) printf %%s "$GIT_USER";; *) printf %%s "$GIT_TOKEN";; esac\n' >"$secrets_dir/askpass"
chmod 700 "$secrets_dir/askpass"
# local.py 以配置文件所在目录为状态根目录；固定放在私有目录，跨构建保留检查记录与发布批次。
# 配置只含本次凭据文件的路径，每次运行重写。
printf '{"kubeconfig": "%s", "retired_source_inventory": "%s"}\n' \
  "$secrets_host/kubeconfig" "$secrets_host/retired-source.json" >"$TKADA_PRIVATE_DIR/config.json"

# local.py 要求在 main 上、远端名为 company；Jenkins 检出为 detached HEAD 且远端名为 origin。
sha=$(git rev-parse HEAD)
git checkout -q -B main "$sha"
git remote remove company >/dev/null 2>&1 || true
git remote add company https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git

docker_gid=$(stat -c %g /var/run/docker.sock)
DOCKER_CONFIG="$secrets_dir/docker" docker pull -q "$TOOLS_IMAGE" >/dev/null
# 不用 exec：结束后还要由 trap 删除本次临时凭据。
docker run --rm --init --name "$container" \
  --network host --user "$(id -u):$(id -g)" --group-add "$docker_gid" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$workspace_host:$workspace_host" -v "$private_host:$private_host" \
  -w "$workspace_host" \
  -e HOME="$private_host/cache/home" -e UV_CACHE_DIR="$private_host/cache/uv" \
  -e LANG=C.UTF-8 \
  -e DOCKER_CONFIG="$secrets_host/docker" \
  -e GIT_ASKPASS="$secrets_host/askpass" -e GIT_TERMINAL_PROMPT=0 \
  -e GIT_USER -e GIT_TOKEN \
  -e TKADA_IMAGE_MIRROR -e DEBIAN_MIRROR -e PYPI_INDEX_URL -e PYPI_FILES_URL -e NPM_REGISTRY \
  -e CHECK_BACKEND_SHARDS -e CHECK_ACCEPTANCE_SHARDS -e CHECK_BROWSER_WORKERS \
  "$TOOLS_IMAGE" python3 deploy/local.py "$action" --config "$private_host/config.json" "$@"
