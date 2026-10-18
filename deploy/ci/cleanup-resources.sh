#!/usr/bin/env bash
# 只回收指定构建的临时测试资源；不按名称前缀或年龄猜测归属。
set -euo pipefail
owner=${1:-}
[[ $owner =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$ ]] || { echo 'Invalid CI resource owner' >&2; exit 2; }
selector="label=net.shortclipflix.tkada.ci-owner=$owner"
# 先终止可能仍在创建子资源的工具进程，再枚举测试资源，避免枚举后又创建新容器。
ids=$(docker ps -aq --filter "$selector" --filter 'label=net.shortclipflix.tkada.ci-role=tools')
if [[ -n $ids ]]; then docker rm -fv $ids; fi
ids=$(docker ps -aq --filter "$selector")
if [[ -n $ids ]]; then
  # Docker 返回的 ID 不含空白，允许按行拆分；先删容器再删网络和卷。
  docker rm -fv $ids
fi
ids=$(docker network ls -q --filter "$selector")
if [[ -n $ids ]]; then docker network rm $ids; fi
ids=$(docker volume ls -q --filter "$selector")
if [[ -n $ids ]]; then docker volume rm $ids; fi
