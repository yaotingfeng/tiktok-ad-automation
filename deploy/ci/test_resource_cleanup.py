"""验证工具进程退出后的宿主资源回收边界，不连接外部服务。"""

import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))


class ResourceCleanupTests(unittest.TestCase):
    def test_only_resource_creation_gets_owner_label(self):
        from resource_labels import with_owner

        for command in (
            ["docker", "run", "--rm", "image", "sleep", "1"],
            ["docker", "network", "create", "test-network"],
            ["docker", "volume", "create", "test-volume"],
        ):
            tagged = with_owner(command, "build-42")
            self.assertIn("net.shortclipflix.tkada.ci-owner=build-42", tagged)
            self.assertEqual(with_owner(command, ""), command)
        command = ["docker", "exec", "container", "true"]
        self.assertEqual(with_owner(command, "build-42"), command)

    def test_cleanup_uses_exact_owner_and_stops_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            docker = root / "docker"
            docker.write_text(
                "#!/bin/bash\n"
                'echo "$*" >> "$CALLS"\n'
                'case "$*" in\n'
                '  *"ci-role=tools") ;;\n'
                '  "ps -aq "*) echo container-42;;\n'
                '  "network ls -q "*) echo network-42;;\n'
                '  "volume ls -q "*) echo volume-42;;\n'
                '  "rm -fv "*) exit "${REMOVE_STATUS:-0}";;\n'
                "esac\n"
            )
            docker.chmod(0o700)
            log = root / "calls"
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "CALLS": str(log),
            }
            command = ["bash", str(ROOT / "deploy/ci/cleanup-resources.sh"), "build-42"]
            result = subprocess.run(command, env=env, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(
                calls,
                [
                    "ps -aq --filter label=net.shortclipflix.tkada.ci-owner=build-42 --filter label=net.shortclipflix.tkada.ci-role=tools",
                    "ps -aq --filter label=net.shortclipflix.tkada.ci-owner=build-42",
                    "rm -fv container-42",
                    "network ls -q --filter label=net.shortclipflix.tkada.ci-owner=build-42",
                    "network rm network-42",
                    "volume ls -q --filter label=net.shortclipflix.tkada.ci-owner=build-42",
                    "volume rm volume-42",
                ],
            )
            result = subprocess.run(
                command,
                env={**env, "REMOVE_STATUS": "1"},
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            log.unlink()
            for owner in ("", "bad owner", "*", "--all"):
                result = subprocess.run(
                    command[:-1] + [owner], env=env, capture_output=True, check=False
                )
                self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())

    @unittest.skipUnless(
        os.environ.get("TEST_CI_RESOURCE_DOCKER") == "1", "opt-in real Docker"
    )
    def test_hard_stopped_tool_leaves_children_for_scoped_cleanup(self):
        from resource_labels import with_owner

        token = secrets.token_hex(6)
        owners = [f"tkada-cleanup-{token}-{suffix}" for suffix in ("a", "b")]

        def docker(*args, owner=""):
            return subprocess.check_output(
                with_owner(["docker", *args], owner), text=True
            ).strip()

        def cleanup(owner):
            subprocess.run(
                ["bash", str(ROOT / "deploy/ci/cleanup-resources.sh"), owner],
                check=True,
                capture_output=True,
            )

        try:
            for owner in owners:
                docker("network", "create", owner, owner=owner)
                docker("volume", "create", owner, owner=owner)
                docker(
                    "run",
                    "-d",
                    "--name",
                    owner,
                    "--network",
                    owner,
                    "-v",
                    f"{owner}:/fixture",
                    "redis:8",
                    "sleep",
                    "300",
                    owner=owner,
                )
            outer = docker("run", "-d", "redis:8", "sleep", "300", owner=owners[0])
            # 模拟工具容器被强制终止：宿主上的兄弟测试容器仍然运行。
            docker("rm", "-f", outer)
            self.assertEqual(
                docker("inspect", "--format", "{{.State.Running}}", owners[0]), "true"
            )
            # post 还需处理外层 shell 被强杀、工具容器仍在线的分支。
            docker(
                "run",
                "-d",
                "--label",
                "net.shortclipflix.tkada.ci-role=tools",
                "redis:8",
                "sleep",
                "300",
                owner=owners[0],
            )
            cleanup(owners[0])
            cleanup(owners[0])  # post always 重复执行必须安全。
            for kind, args in (
                ("container", ("ps", "-aq")),
                ("network", ("network", "ls", "-q")),
                ("volume", ("volume", "ls", "-q")),
            ):
                selector = "label=net.shortclipflix.tkada.ci-owner="
                self.assertEqual(
                    docker(*args, "--filter", selector + owners[0]), "", kind
                )
                self.assertTrue(docker(*args, "--filter", selector + owners[1]), kind)
        finally:
            for owner in owners:
                cleanup(owner)


if __name__ == "__main__":
    unittest.main()
