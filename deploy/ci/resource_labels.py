"""为合成恢复测试的 Docker 资源标记所属构建，供外层中止清理。"""

import re

LABEL = "net.shortclipflix.tkada.ci-owner"


def with_owner(command, owner):
    if not owner:
        return command
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", owner):
        raise ValueError("Invalid CI resource owner")
    # 只标记创建动作，不改变 exec/cp 等命令，也不接触正式发布的资源。
    if command[:2] == ["docker", "run"]:
        offset = 2
    elif command[:3] in (
        ["docker", "network", "create"],
        ["docker", "volume", "create"],
    ):
        offset = 3
    else:
        return command
    return command[:offset] + ["--label", f"{LABEL}={owner}"] + command[offset:]
