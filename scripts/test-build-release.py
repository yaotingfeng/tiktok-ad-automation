"""Build gate tests use a real temporary Git repository and stub external transports."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "deploy").mkdir()
        shutil.copy(REPO / "deploy/build-and-push.sh", self.root / "deploy")
        (self.root / "deploy/ci").mkdir()
        shutil.copy(REPO / "deploy/ci/image-build-args.sh", self.root / "deploy/ci")
        self.real_git = shutil.which("git")
        for args in (
            ("init", "-b", "main"),
            ("config", "user.name", "Fixture"),
            ("config", "user.email", "fixture@example.invalid"),
            ("add", "deploy"),
            ("commit", "-m", "fixture"),
        ):
            subprocess.run(
                [self.real_git, *args], cwd=self.root, check=True, capture_output=True
            )
        self.sha = subprocess.check_output(
            [self.real_git, "rev-parse", "HEAD"], cwd=self.root, text=True
        ).strip()
        self.bin = self.root / ".git" / "test-bin"
        self.bin.mkdir()
        (self.bin / "git").write_text("""#!/usr/bin/env python3
import os, subprocess, sys
if sys.argv[1:3] == ['remote', 'get-url']:
    print('https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git')
elif sys.argv[1] == 'ls-remote':
    print(os.environ['REMOTE_SHA'] + '\\trefs/heads/main')
else:
    sys.exit(subprocess.call([os.environ['REAL_GIT'], *sys.argv[1:]]))
""")
        (self.bin / "docker").write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['DOCKER_LOG'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
""")
        for path in self.bin.iterdir():
            path.chmod(0o755)
        self.log = self.root / ".git" / "docker.log"
        # 完整检查会为应用测试导出 PROJECT_NAME；镜像项目名由构建脚本固定，
        # 不能继承应用显示名，否则在检查环境里会误触项目名防护。
        # Jenkins 检查会带入镜像来源变量，这里同样清除，由各用例显式指定。
        excluded = {"PROJECT_NAME", "TKADA_IMAGE_MIRROR", "DEBIAN_MIRROR",
                    "PYPI_INDEX_URL", "PYPI_FILES_URL", "NPM_REGISTRY"}
        inherited = {
            key: value for key, value in os.environ.items() if key not in excluded
        }
        self.env = dict(
            inherited,
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
            REAL_GIT=self.real_git,
            REMOTE_SHA=self.sha,
            DOCKER_LOG=str(self.log),
        )

    def run_build(self, **env):
        return subprocess.run(
            ["bash", "deploy/build-and-push.sh"],
            cwd=self.root,
            env=dict(self.env, **env),
            capture_output=True,
            check=False,
            text=True,
        )

    def test_detached_jenkins_checkout_still_requires_exact_remote_main(self):
        subprocess.run(
            [self.real_git, "checkout", "--detach", self.sha],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.log.unlink()
        result = self.run_build(REMOTE_SHA="0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_non_head_sha_is_rejected_even_for_dry_run(self):
        result = self.run_build(DRY_RUN="1", RELEASE_SHA="0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_unpushed_head_never_starts_docker(self):
        result = self.run_build(REMOTE_SHA="0" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_foreign_repository_override_is_rejected(self):
        self.assertNotEqual(
            self.run_build(DRY_RUN="1", PROJECT_NAME="other").returncode, 0
        )
        self.assertFalse(self.log.exists())

    def test_release_uses_verified_sha_and_company_image(self):
        import json

        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        build = next(call for call in calls if call[:2] == ["buildx", "build"])
        self.assertEqual(
            build[build.index("-t") + 1],
            "sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation:"
            + self.sha,
        )
        self.assertIn("VCS_REF=" + self.sha, build)
        self.assertIn("--push", build)
        self.assertFalse(any(arg.startswith("PYTHON_IMAGE=") for arg in build))

    def test_jenkins_mirror_sources_reach_the_release_build(self):
        import json

        mirror = "registry.example.invalid/test/tiktok-ad-automation-base:"
        result = self.run_build(
            TKADA_IMAGE_MIRROR=mirror, PYPI_INDEX_URL="https://pypi.example.invalid/simple"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        build = next(call for call in calls if call[:2] == ["buildx", "build"])
        self.assertIn("PYTHON_IMAGE=" + mirror + "python-3.14", build)
        self.assertIn("BUN_IMAGE=" + mirror + "oven-bun-1.4.2", build)
        self.assertIn("PYPI_INDEX_URL=https://pypi.example.invalid/simple", build)


if __name__ == "__main__":
    unittest.main()
