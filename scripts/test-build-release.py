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
        self.env = dict(
            os.environ,
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


if __name__ == "__main__":
    unittest.main()
