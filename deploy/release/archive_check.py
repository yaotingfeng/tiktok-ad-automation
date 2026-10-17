"""校验一个备份目录：清单校验和、归档成员安全与可读性。

只用标准库，经 `python - <备份目录>` 在发布辅助 Pod 的应用容器内运行，备份文件无需传出集群。
结果以一行 JSON 输出（{"ok": true} 或 {"ok": false, "error": ...}），退出码恒为 0，
由发布端根据内容判定；这样 kubectl exec 的失败只代表执行本身出错，而非备份损坏。
"""

import hashlib
import json
import sys
import tarfile
from pathlib import Path

ARCHIVES = ("runtime-app", "project", "beat-data", "config")


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def entry_digest(path):
    """文件取 sha256；目录按文件名字节序拼接 "<sha256>  <名称>\\n" 后再取 sha256（与 sha256sum 输出一致）。"""
    if not path.is_dir():
        return file_digest(path)
    lines = "".join(
        f"{file_digest(item)}  {item.name}\n"
        for item in sorted(path.iterdir(), key=lambda p: p.name.encode())
        if item.is_file()
    )
    return hashlib.sha256(lines.encode()).hexdigest()


def check(folder):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    for name, checksum in manifest["files"].items():
        if Path(name).name != name:
            return "Unsafe manifest path"
        if entry_digest(folder / name) != checksum:
            return "Backup checksum mismatch: " + name
    for name in ARCHIVES:
        with tarfile.open(folder / (name + ".tar.gz")) as archive:
            members = archive.getmembers()
            if not members:
                return "Empty archive: " + name
            if not all(
                not m.name.startswith("/") and ".." not in Path(m.name).parts
                for m in members
            ):
                return "Unsafe archive member"
            if name == "runtime-app" and not any(
                m.name.endswith("backend/app/frontend/index.html") for m in members
            ):
                return "Frontend artifact missing"
            # 解包普通文件验证压缩内容可读，避免仅检查目录。
            for member in members:
                if member.isfile():
                    with archive.extractfile(member) as stream:
                        while stream.read(1024 * 1024):
                            pass
    return None


if __name__ == "__main__":
    try:
        error = check(sys.argv[1])
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        error = type(exc).__name__ + ": " + str(exc)
    print(json.dumps({"ok": error is None, "error": error}))
