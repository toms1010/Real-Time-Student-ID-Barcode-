#!/usr/bin/env bash
# ===========================================================================
#  start.sh - start the local API and the web interface
#
#  What runs, and why:
#    * the FastAPI service on 127.0.0.1:8000 (serves the API, the MJPEG stream
#      and the built web interface)
#    * optionally the C++ scanner in service mode, which owns the camera and
#      publishes annotated frames to the API so the browser mirrors it
#
#  The camera has exactly one owner at a time (ADR-006), so by default the
#  *Python* pipeline owns it and the C++ scanner stays in the background.  Use
#  --cpp-scanner to hand the device to the C++ scanner instead.
#
#  Usage:
#    ./scripts/start.sh                 API + web UI (Python owns the camera)
#    ./scripts/start.sh --cpp-scanner   also start the C++ scanner headless
#    ./scripts/start.sh --foreground    run in the foreground (Ctrl+C stops all)
#    ./scripts/start.sh --port 9000
#    ./scripts/stop.sh                  stops whatever this script started
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

VENV="${PROJECT_ROOT}/.venv"
PYTHON_BIN="${VENV}/bin/python"
RUN_DIR="${PROJECT_ROOT}/run"
LOG_DIR="${PROJECT_ROOT}/logs"
BUILD_DIR="${PROJECT_ROOT}/build"

HOST="${API_HOST:-127.0.0.1}"
PORT="${API_PORT:-8000}"
START_CPP=0
FOREGROUND=0
SCANNER_MODE="service"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cpp-scanner) START_CPP=1 ;;
    --foreground|-f) FOREGROUND=1 ;;
    --port) PORT="$2"; shift ;;
    --host) HOST="$2"; shift ;;
    --help|-h) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -t 1 ]]; then BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
else BOLD=""; GREEN=""; RED=""; RESET=""; fi
step() { printf '\n%s==> %s%s\n' "${BOLD}" "$*" "${RESET}"; }
ok()   { printf '  %s✓%s %s\n' "${GREEN}" "${RESET}" "$*"; }
die()  { printf '  %sx%s %s\n' "${RED}" "${RESET}" "$*" >&2; exit 1; }

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

# --- preflight --------------------------------------------------------------
[[ -x "${PYTHON_BIN}" ]] || die "no virtual environment. Run ./scripts/setup.sh first."
export PYTHONPATH="${PROJECT_ROOT}/python"

if [[ -f "${RUN_DIR}/api.pid" ]] && kill -0 "$(cat "${RUN_DIR}/api.pid")" 2>/dev/null; then
  die "the service is already running (pid $(cat "${RUN_DIR}/api.pid")). Use ./scripts/stop.sh first."
fi
if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :${PORT}" 2>/dev/null | grep -q LISTEN; then
  die "port ${PORT} is already in use. Free it or pass --port <other>."
fi

step "Checking the database"
"${PYTHON_BIN}" -m app.cli db-init >/dev/null || die "the database could not be initialised"
"${PYTHON_BIN}" -m app.cli db-migrate --status | sed 's/^/    /'
ok "database ready: database/student_id_system.db"

step "Checking the web build"
if [[ -f "${PROJECT_ROOT}/frontend/dist/index.html" ]]; then
  ok "frontend/dist present"
else
  printf '  ! frontend/dist is missing - run: cd frontend && npm install && npm run build\n'
  printf '    (the API and /docs still work without it)\n'
fi

# --- optional C++ scanner ---------------------------------------------------
SCANNER_PID=""
if [[ ${START_CPP} -eq 1 ]]; then
  SCANNER_BIN="${BUILD_DIR}/cpp/student_id_scanner"
  if [[ ! -x "${SCANNER_BIN}" ]]; then
    die "the C++ scanner was not built. Run: cmake -S . -B build && cmake --build build -j"
  fi
  step "Starting the C++ scanner (${SCANNER_MODE} mode)"
  if ! ls /dev/video* >/dev/null 2>&1; then
    die "no camera device found; the C++ scanner cannot own the camera"
  fi
  SIDB_PROJECT_ROOT="${PROJECT_ROOT}" nohup "${SCANNER_BIN}" \
      --mode="${SCANNER_MODE}" --project-root="${PROJECT_ROOT}" --api-port="${PORT}" \
      > "${LOG_DIR}/scanner.log" 2>&1 &
  SCANNER_PID=$!
  echo "${SCANNER_PID}" > "${RUN_DIR}/scanner.pid"
  ok "scanner pid ${SCANNER_PID} (log: logs/scanner.log)"
  sleep 2
  if ! kill -0 "${SCANNER_PID}" 2>/dev/null; then
    printf '  x the scanner exited immediately. Last lines of logs/scanner.log:\n'
    tail -20 "${LOG_DIR}/scanner.log" | sed 's/^/      /'
    exit 1
  fi
fi

# --- the API ----------------------------------------------------------------
step "Starting the local service"
if [[ ${FOREGROUND} -eq 1 ]]; then
  printf '  API:      http://%s:%s\n  API docs: http://%s:%s/docs\n\n' "${HOST}" "${PORT}" "${HOST}" "${PORT}"
  exec "${PYTHON_BIN}" -m uvicorn app.main:app --host "${HOST}" --port "${PORT}" --log-level info
fi

nohup "${PYTHON_BIN}" -m uvicorn app.main:app --host "${HOST}" --port "${PORT}" \
    --log-level info > "${LOG_DIR}/api.log" 2>&1 &
API_PID=$!
echo "${API_PID}" > "${RUN_DIR}/api.pid"
ok "service pid ${API_PID} (log: logs/api.log)"

step "Waiting for the service to answer"
for _ in $(seq 1 40); do
  if curl -fsS "http://${HOST}:${PORT}/api/health" >/dev/null 2>&1; then
    ok "health check passed"
    break
  fi
  if ! kill -0 "${API_PID}" 2>/dev/null; then
    printf '  x the service exited during start-up. Last lines of logs/api.log:\n'
    tail -25 "${LOG_DIR}/api.log" | sed 's/^/      /'
    exit 1
  fi
  sleep 0.5
done

HEALTH="$(curl -fsS "http://${HOST}:${PORT}/api/health" 2>/dev/null || echo '{}')"
printf '  status: %s\n' "$(printf '%s' "${HEALTH}" | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin).get("status","?"))' 2>/dev/null || echo unknown)"

printf '\n%s============================================================%s\n' "${GREEN}" "${RESET}"
printf ' Dashboard : http://%s:%s\n' "${HOST}" "${PORT}"
printf ' API docs  : http://%s:%s/docs\n' "${HOST}" "${PORT}"
printf ' Scanner   : %s\n' "$(
  if [[ ${START_CPP} -eq 1 ]]; then
    printf 'C++ (service mode) - this process owns the camera, so the browser mirrors it'
  else
    printf 'Python pipeline inside the service (logs/api.log)'
  fi
)"
printf '\n Sign in with admin / DemoPass!2026 (see database/seed.sql)\n'
printf ' Stop with ./scripts/stop.sh\n\n'
