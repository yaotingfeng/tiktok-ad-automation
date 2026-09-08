"""Alembic must load its own metadata without pytest's model imports."""

import subprocess
import sys
from pathlib import Path


def test_fresh_alembic_cli_preserves_capability_tables():
    backend = Path(__file__).resolve().parents[4]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=backend,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        "Standalone Alembic metadata disagrees with migrated schema; "
        "capability models must be registered in env.py without test imports"
    )
