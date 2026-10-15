# shellcheck shell=bash
# 镜像构建的下载来源参数；发布构建与检查里的集成镜像共用，保证两者来源一致。
# 只在设置了对应环境变量时追加，本机默认仍用 Dockerfile 里的官方来源。
image_build_args() {
  build_args=()
  if [[ -n ${TKADA_IMAGE_MIRROR:-} ]]; then
    build_args+=(--build-arg "BUN_IMAGE=${TKADA_IMAGE_MIRROR}oven-bun-1.4.2")
    build_args+=(--build-arg "PYTHON_IMAGE=${TKADA_IMAGE_MIRROR}python-3.14")
  fi
  local key
  for key in DEBIAN_MIRROR PYPI_INDEX_URL PYPI_FILES_URL NPM_REGISTRY; do
    if [[ -n ${!key:-} ]]; then build_args+=(--build-arg "$key=${!key}"); fi
  done
}
