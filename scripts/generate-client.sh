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
