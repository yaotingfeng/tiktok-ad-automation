#! /usr/bin/env bash

set -euo pipefail
cd "$(dirname "$0")/.."
if command -v bun >/dev/null 2>&1; then
  bun_command=bun
else
  bun_command="$PWD/.tools/node_modules/.bin/bun"
fi
(
  cd backend
  uv run --frozen python -c 'import json; from pathlib import Path; from app.main import app; Path("../frontend/openapi.json").write_text(json.dumps(app.openapi()))'
)
"$bun_command" run --filter frontend generate-client
# The pinned generator emits whitespace-only separators for required query
# arguments. Normalize its output deterministically without editing API types.
python3 - <<'PY'
from pathlib import Path

for path in Path("frontend/src/client").rglob("*.ts"):
    value = path.read_text()
    normalized = "\n".join(line.rstrip() for line in value.splitlines()) + "\n"
    if normalized != value:
        path.write_text(normalized)
PY
