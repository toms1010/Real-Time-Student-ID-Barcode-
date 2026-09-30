#!/usr/bin/env bash
# ===========================================================================
#  stop.sh - stop everything start.sh started
#
#  Reads the pid files in run/ (api.pid, scanner.pid) and stops those
#  processes.  It never uses a pattern match like `pkill -f uvicorn`, because on
#  a shared machine that would kill someone else's service.
# ===========================================================================
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
RUN_DIR="${PROJECT_ROOT}/run"

if [[ -t 1 ]]; then GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else GREEN=""; YELLOW=""; RESET=""; fi

stop_pidfile() {
  local name="$1" file="${RUN_DIR}/$1.pid" pid
  if [[ ! -f "${file}" ]]; then
    printf '  - %s: no pid file, nothing to stop\n' "${name}"
    return 0
  fi
  pid="$(cat "${file}")"
  if [[ -z "${pid}" ]] || ! kill -0 "${pid}" 2>/dev/null; then
    printf '  - %s: not running (stale pid file removed)\n' "${name}"
    rm -f "${file}"
    return 0
  fi
  # SIGTERM first so the service can stop its threads and close the camera;
  # SIGKILL only if it ignores the polite request.
  kill -TERM "${pid}" 2>/dev/null
  for _ in $(seq 1 20); do
    if ! kill -0 "${pid}" 2>/dev/null; then break; fi
    sleep 0.25
  done
  if kill -0 "${pid}" 2>/dev/null; then
    printf '  ! %s (pid %s) ignored SIGTERM; sending SIGKILL\n' "${name}" "${pid}"
    kill -KILL "${pid}" 2>/dev/null
    sleep 0.5
  fi
  printf '  %s✓%s %s stopped (pid %s)\n' "${GREEN}" "${RESET}" "${name}" "${pid}"
  rm -f "${file}"
}

printf 'Stopping the Student ID Barcode System\n'
stop_pidfile scanner
stop_pidfile api

# Free the camera if something else grabbed it while the service was running.
if command -v fuser >/dev/null 2>&1; then
  for device in /dev/video*; do
    [[ -e "${device}" ]] || continue
    if fuser "${device}" >/dev/null 2>&1; then
      printf '  %s!%s %s is still in use by pid(s): %s\n' \
        "${YELLOW}" "${RESET}" "${device}" "$(fuser "${device}" 2>/dev/null)"
    fi
  done
fi

printf '\nLogs are kept in logs/ (api.log, scanner.log).\n'
