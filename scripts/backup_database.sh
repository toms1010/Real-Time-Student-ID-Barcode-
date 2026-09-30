#!/usr/bin/env bash
# ===========================================================================
#  backup_database.sh - a consistent backup of the student database
#
#  Uses SQLite's online backup API via `app.cli backup`, which takes a
#  transactionally consistent copy even while the scanner is writing - copying
#  the .db file with `cp` while WAL is active can produce a torn backup.
#
#  Usage:
#    ./scripts/backup_database.sh                     -> backups/<timestamp>.db
#    ./scripts/backup_database.sh --output /mnt/usb  -> another directory
#    ./scripts/backup_database.sh --prune 14          # also delete copies older
#                                                    # than 14 days
#    ./scripts/backup_database.sh --verify            # integrity_check each copy
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

VENV="${PROJECT_ROOT}/.venv"
PYTHON_BIN="${VENV}/bin/python"
export PYTHONPATH="${PROJECT_ROOT}/python"

OUTPUT=""
PRUNE_DAYS=0
VERIFY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output|-o) OUTPUT="$2"; shift ;;
    --prune) PRUNE_DAYS="${2:-14}"; shift ;;
    --verify) VERIFY=1 ;;
    --help|-h) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -t 1 ]]; then GREEN=$'\033[32m'; RESET=$'\033[0m'; else GREEN=""; RESET=""; fi

DB_PATH="${PROJECT_ROOT}/database/student_id_system.db"
if [[ ! -f "${DB_PATH}" ]]; then
  printf 'no database at %s - run ./scripts/setup.sh first\n' "${DB_PATH}" >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  printf 'no virtual environment - run ./scripts/setup.sh first\n' >&2
  exit 1
fi

echo "==> creating the backup"
"${PYTHON_BIN}" -m app.cli backup ${OUTPUT:+--output "${OUTPUT}"}

LATEST="$(find "${OUTPUT:-${PROJECT_ROOT}/backups}" -name 'student_id_system-*.db' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)"

if [[ -n "${LATEST}" && -f "${LATEST}" ]]; then
  SIZE="$(du -h "${LATEST}" | cut -f1)"
  printf '  %s✓%s %s (%s)\n' "${GREEN}" "${RESET}" "${LATEST}" "${SIZE}"
  printf '    %s rows in scan_logs, %s transactions\n' \
    "$(sqlite3 "${LATEST}" 'SELECT COUNT(*) FROM scan_logs;' 2>/dev/null || echo '?')" \
    "$(sqlite3 "${LATEST}" 'SELECT COUNT(*) FROM transactions;' 2>/dev/null || echo '?')"

  if [[ ${VERIFY} -eq 1 ]]; then
    RESULT="$(sqlite3 "${LATEST}" 'PRAGMA integrity_check;' 2>/dev/null || echo 'sqlite3 CLI not available')"
    if [[ "${RESULT}" == "ok" ]]; then
      printf '    %s✓%s integrity_check: ok\n' "${GREEN}" "${RESET}"
    else
      printf '    x integrity_check: %s\n' "${RESULT}" >&2
      exit 1
    fi
  fi
fi

if [[ ${PRUNE_DAYS} -gt 0 ]]; then
  echo "==> removing backups older than ${PRUNE_DAYS} days"
  find "${OUTPUT:-${PROJECT_ROOT}/backups}" -name 'student_id_system-*.db' -type f \
       -mtime "+${PRUNE_DAYS}" -print -delete | sed 's/^/    removed /'
fi

printf '\nRestore with:  cp backups/student_id_system-<timestamp>.db database/student_id_system.db\n'
printf '                 (stop the service first with ./scripts/stop.sh)\n'
