"""Copy reviewable sources, never local credentials or generated build artifacts."""

import shutil
import subprocess
import sys
from pathlib import Path


def copy_sources(source: Path, destination: Path) -> None:
    # Include unstaged source fixes while honoring Git's private-file exclusions.
    names = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=source,
    ).decode().split("\0")
    for name in sorted(set(names) - {""}):
        relative = Path(name)
        if any(
            part in {".git", ".runtime", ".tools", ".venv", "node_modules", "__pycache__"}
            or (part.startswith(".env") and not part.endswith(".example"))
            for part in relative.parts
        ):
            continue
        if name in {"deploy/traefik.yml", "deploy/k8s/runtime.env", "deploy/k8s/mcp-client-registration.json"}:
            continue
        original = source / relative
        if not original.exists() and not original.is_symlink():
            continue  # A tracked deletion is part of the working tree being checked.
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if original.is_symlink():
            # Preserve in-tree source links without following external targets.
            if not original.resolve().is_relative_to(source.resolve()):
                raise RuntimeError(f"Source symlink escapes repository: {name}")
            target.symlink_to(original.readlink())
        else:
            shutil.copy2(original, target)


if __name__ == "__main__":
    copy_sources(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
