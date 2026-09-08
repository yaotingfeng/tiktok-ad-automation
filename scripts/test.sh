#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
# The pytest fixture refuses non-test databases before migration.
uv run --frozen pytest "$@"
