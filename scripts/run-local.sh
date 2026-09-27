#!/usr/bin/env bash
# 单机版（不使用 Docker）：Python ≥ 3.10、Node ≥ 20
#   ./scripts/run-local.sh            → http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q -e backend
fi
if [ ! -d frontend/dist ]; then
  (cd frontend && npm ci --no-audit --no-fund && npm run build)
fi
export REVIEWDESK_PROFILE=local
export REVIEWDESK_DATA_DIR="${REVIEWDESK_DATA_DIR:-$PWD/data}"
exec backend/.venv/bin/reviewdesk serve --host 127.0.0.1 --port "${PORT:-8000}"
