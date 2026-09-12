"""统一定位和初始化环境配置；不读取密钥、不连接服务器、不覆盖已有配置。"""

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    environments = json.loads((root / "config/environments.json").read_text())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("environment", choices=environments)
    parser.add_argument("action", choices=("path", "info", "init"))
    args = parser.parse_args()
    entry = environments[args.environment]
    target = root / entry["file"]
    if args.action == "path":
        print(target)
    elif args.action == "info":
        # 仅输出登记的位置与文件存在性，绝不展开环境文件内容。
        print(
            json.dumps(
                {
                    "environment": args.environment,
                    "project_file": str(target),
                    "exists": target.exists(),
                    "server_runtime_file": entry["runtime_file"],
                    "runbook": str(root / entry["runbook"]),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        lines = (root / ".env.example").read_text().splitlines()
        values = entry["overrides"]
        lines = [
            line.split("=", 1)[0] + "=" + values[line.split("=", 1)[0]]
            if "=" in line and line.split("=", 1)[0] in values
            else line
            for line in lines
        ]
        lines[0] = (
            f"# {args.environment}: private configuration; never commit this file."
        )
        # 原子排他创建：已有配置（包括符号链接）一律拒绝，不重置密钥。
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            parser.exit(1, f"Configuration already exists; unchanged: {target}\n")
        with os.fdopen(descriptor, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        print(
            f"Created template: {target}; fill environment-specific values before use."
        )


if __name__ == "__main__":
    main()
