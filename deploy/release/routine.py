"""普通发布只切换程序；数据合同变化须人工选定完整发布方案。"""

import re

from common import ROOT, run


def needs_full_backup(old_sha, sha):
    paths = (
        run(["git", "-C", str(ROOT), "diff", "--name-only", old_sha, sha])
        .decode()
        .splitlines()
    )
    # 这些变更不能仅 set image；还需迁移、队列合同或正式部署清单应用。
    sensitive = re.compile(
        r"^(backend/app/(alembic/|jobs/|models\.py|core/)"
        r"|backend/app/modules/.*/jobs(?:/|\.py$)"
        r"|backend/.*(?:models?|schema|contracts?)(?:/|\.py$)"
        r"|deploy/k8s/|config/|\.env\.example$|backend/Dockerfile$"
        r"|uv\.lock$|pyproject\.toml$|backend/pyproject\.toml$)"
    )
    return any(sensitive.search(path) for path in paths if not path.endswith(".md"))
