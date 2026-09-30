#!/usr/bin/env bash
# ===========================================================================
#  dev.sh - developer loop: auto-reload API + Vite dev server
#
#  Runs two processes:
#    * uvicorn --reload  on 127.0.0.1:8000  (the API, with hot reload)
#    * vite dev server   on 127.0.0.1:5173  (the UI, with HMR), which proxies
#                                           /api to 8000
#
#  Open http://127.0.0.1:5173 for the UI.  Ctrl+C stops both.
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-5173}"

[[ -x "${PYTHON_BIN}" ]] || { echo "no virtual environment - run ./scripts/setup.sh"; exit 1; }
export PYTHONPATH="${PROJECT_ROOT}/python"

mkdir -p logs
PIDS=()
cleanup() {
  printf '\nStopping...\n'
  for pid in "${PIDS[@]:-}"; do
    [[ -n "${pid}" ]] && kill -TERM "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> API with auto-reload on http://${API_HOST}:${API_PORT}"
"${PYTHON_BIN}" -m uvicorn app.main:app --host "${API_HOST}" --port "${API_PORT}" --reload &
PIDS+=($!)

if command -v npm >/dev/null 2>&1; then
  if [[ ! -d frontend/node_modules ]]; then
    echo "==> installing the frontend dependencies"
    (cd frontend && npm install --no-audit --no-fund --loglevel=error)
  fi
  echo "==> Vite dev server on http://127.0.0.1:${UI_PORT}"
  (cd frontend && npm run dev -- --port "${UI_PORT}") &
  PIDS+=($!)
  echo
  echo "    UI  : http://127.0.0.1:${UI_PORT}   (proxies /api to ${API_HOST}:${API_PORT})"
else
  echo "    npm not found - using the built interface instead."
fi
echo "    API : http://${API_HOST}:${API_PORT}/docs"
echo
echo "    Ctrl+C stops everything."
wait
