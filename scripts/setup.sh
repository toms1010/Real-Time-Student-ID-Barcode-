#!/usr/bin/env bash
# ===========================================================================
#  setup.sh - one-command setup for a fresh Linux machine
#
#  What it does, in order, and why:
#    1. check the distribution and the required tools, and say exactly what to
#       install when something is missing (it cannot install for you: that
#       needs root, and the script must not pretend otherwise)
#    2. create .venv and install the pinned Python dependencies
#    3. create the SQLite database, apply migrations, load the fictional demo
#       students
#    4. generate the barcode / ID-card / sound assets used by the tests
#    5. build the web interface
#    6. configure and build the C++ scanner (OpenCV + SQLite3 + ZXing-C++)
#    7. run the C++ and Python test suites
#
#  Usage:
#    ./scripts/setup.sh                 full setup
#    ./scripts/setup.sh --skip-cpp      Python + database + web only
#    ./scripts/setup.sh --skip-tests    do not run the test suites
#    ./scripts/setup.sh --with-frontend also build the web interface (default)
#    ./scripts/setup.sh --help
#
#  Requires: bash 4+, python3 (3.11+), git.  Needs network access the first
#  time (to download Python wheels, npm packages and ZXing-C++).
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

VENV="${PROJECT_ROOT}/.venv"
PYTHON_BIN="${VENV}/bin/python"
BUILD_DIR="${PROJECT_ROOT}/build"
LOG_DIR="${PROJECT_ROOT}/logs"

SKIP_CPP=0
SKIP_TESTS=0
WITH_FRONTEND=1
FORCE_SEED=0

# --- pretty output ----------------------------------------------------------
if [[ -t 1 ]]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
step()  { printf '\n%s==> %s%s\n' "${BOLD}" "$*" "${RESET}"; }
ok()    { printf '  %s✓%s %s\n' "${GREEN}" "${RESET}" "$*"; }
warn()  { printf '  %s!%s %s\n' "${YELLOW}" "${RESET}" "$*"; }
fail()  { printf '  %sx%s %s\n' "${RED}" "${RESET}" "$*" >&2; }
die()   { fail "$*"; exit 1; }

usage() { sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-cpp)      SKIP_CPP=1 ;;
    --skip-tests)    SKIP_TESTS=1 ;;
    --with-frontend) WITH_FRONTEND=1 ;;
    --no-frontend)   WITH_FRONTEND=0 ;;
    --force-seed)    FORCE_SEED=1 ;;
    -h|--help)       usage ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

printf '%s\n' "${BOLD}============================================================${RESET}"
printf '%s\n' "${BOLD} Real-Time Student ID Barcode Detection System - setup${RESET}"
printf '%s\n' "${BOLD}============================================================${RESET}"
printf ' project root: %s\n' "${PROJECT_ROOT}"

# ---------------------------------------------------------------------------
step "1/7  Checking the environment"
# ---------------------------------------------------------------------------
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  ok "${PRETTY_NAME:-unknown distribution} (${ID:-?} ${VERSION_ID:-?})"
  case "${ID:-}" in
    ubuntu|kubuntu|pop|debian) : ;;
    *) warn "This script is written for Ubuntu/Kubuntu and derivatives." ;;
  esac
else
  warn "/etc/os-release not found; skipping the distribution check."
fi

MISSING=()
for tool in bash git python3 cmake make g++; do
  if command -v "${tool}" >/dev/null 2>&1; then
    ok "$(printf '%-8s' "${tool}") $(${tool} --version 2>&1 | head -1)"
  else
    MISSING+=("${tool}")
  fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  fail "missing required tools: ${MISSING[*]}"
  cat <<'EOF'

  Install them with:

      sudo apt update
      sudo apt install -y build-essential cmake git pkg-config

EOF
  exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 - <<'PY' || die "Python 3.11 or newer is required (found ${PY_VERSION})."
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
ok "python ${PY_VERSION}"

# --- system libraries the C++ scanner needs --------------------------------
MISSING_PKGS=()
pkg_config_ok() { pkg-config --exists "$1" 2>/dev/null; }
pkg_config_ok opencv4   || MISSING_PKGS+=("libopencv-dev")
pkg_config_ok sqlite3   || MISSING_PKGS+=("libsqlite3-dev")

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
  fail "missing system libraries: ${MISSING_PKGS[*]}"
  cat <<EOF

  The C++ scanner needs these development packages. This script cannot
  install them for you (it would need root), so please run:

      sudo apt update
      sudo apt install -y ${MISSING_PKGS[*]}

  Re-run this script afterwards. To set up only the Python half in the
  meantime, use:  ./scripts/setup.sh --skip-cpp
EOF
  [[ ${SKIP_CPP} -eq 1 ]] || exit 1
fi
[[ ${#MISSING_PKGS[@]} -eq 0 ]] && ok "OpenCV and SQLite3 development files found"

if [[ ${SKIP_CPP} -eq 0 ]]; then
  if command -v aplay >/dev/null 2>&1 || command -v paplay >/dev/null 2>&1; then
    ok "an audio player is available (sound feedback enabled)"
  else
    warn "no aplay/paplay found - sound feedback will be disabled (install alsa-utils)"
  fi
  if [[ -e /dev/video0 ]]; then
    ok "camera device(s): $(ls /dev/video* 2>/dev/null | tr '\n' ' ')"
    if ! [[ -r /dev/video0 && -w /dev/video0 ]]; then
      warn "no read/write access to /dev/video0. Add your user to the 'video' group:"
      warn "    sudo usermod -aG video \$USER      then log out and back in"
    fi
  else
    warn "no /dev/video* device found - the scanner will run in file/demo mode only"
  fi
fi

mkdir -p "${LOG_DIR}"

# ---------------------------------------------------------------------------
step "2/7  Python virtual environment"
# ---------------------------------------------------------------------------
if [[ -x "${PYTHON_BIN}" ]]; then
  ok "reusing ${VENV}"
else
  python3 -m venv "${VENV}" || die "could not create the virtual environment"
  ok "created ${VENV}"
fi
"${PYTHON_BIN}" -m pip install --quiet --upgrade pip || warn "could not upgrade pip (offline?)"
"${PYTHON_BIN}" -m pip install --quiet -r requirements.txt \
  || die "pip install failed; check the network connection and try again"
ok "installed the Python dependencies (requirements.txt)"
"${PYTHON_BIN}" -m pip freeze > requirements-lock.txt
ok "wrote requirements-lock.txt (exact versions of this machine)"

# ---------------------------------------------------------------------------
step "3/7  Database"
# ---------------------------------------------------------------------------
export PYTHONPATH="${PROJECT_ROOT}/python"
"${PYTHON_BIN}" -m app.cli db-init || die "db-init failed"
if [[ ${FORCE_SEED} -eq 1 ]]; then
  "${PYTHON_BIN}" -m app.cli db-seed --force || warn "seeding reported a problem"
else
  "${PYTHON_BIN}" -m app.cli db-seed || true
fi
"${PYTHON_BIN}" -m app.cli db-migrate --status | sed 's/^/    /'

if [[ "${SIDB_NO_DEMO:-0}" == "1" ]]; then
  warn "SIDB_NO_DEMO=1 - create a real administrator with:"
  warn "    ${PYTHON_BIN} -m app.cli create-admin --username admin"
else
  "${PYTHON_BIN}" -m app.cli create-admin \
      --username admin --password 'DemoPass!2026' --full-name 'Demo Administrator' \
      --role admin --no-must-change >/dev/null 2>&1 \
    || warn "could not ensure the demo administrator exists"
fi

# ---------------------------------------------------------------------------
step "4/7  Test assets (barcodes, sample ID cards, sounds)"
# ---------------------------------------------------------------------------
if [[ -f assets/test-barcodes/code128_primary.png ]]; then
  ok "assets already present (delete assets/ to regenerate)"
else
  "${PYTHON_BIN}" -m app.cli generate-assets || warn "asset generation reported a problem"
fi

# ---------------------------------------------------------------------------
step "5/7  Web interface"
# ---------------------------------------------------------------------------
if [[ ${WITH_FRONTEND} -eq 1 ]]; then
  if command -v npm >/dev/null 2>&1; then
    pushd frontend >/dev/null
    npm install --no-audit --no-fund --loglevel=error || warn "npm install reported a problem"
    npm run build --silent || warn "the web build failed; the API still works without it"
    popd >/dev/null
    if [[ -f frontend/dist/index.html ]]; then
      ok "built the web interface (frontend/dist)"
    else
      warn "frontend/dist/index.html is missing - the root URL will show setup instructions"
    fi
  else
    warn "npm was not found; skipping the web interface build"
    warn "install Node.js 20+ and run: cd frontend && npm install && npm run build"
  fi
else
  warn "skipped (--no-frontend)"
fi

# ---------------------------------------------------------------------------
if [[ ${SKIP_CPP} -eq 1 ]]; then
  step "6/7  C++ scanner - skipped (--skip-cpp)"
  step "7/7  Tests"
  if [[ ${SKIP_TESTS} -eq 0 ]]; then
    "${PYTHON_BIN}" -m pytest -q python/tests tests/integration 2>&1 | tail -20 || warn "the Python tests reported failures"
  fi
  printf '\n%sSetup complete.%s  Start everything with:  ./scripts/start.sh\n' "${GREEN}" "${RESET}"
  exit 0
fi

step "6/7  C++ scanner build"
# ---------------------------------------------------------------------------
mkdir -p "${BUILD_DIR}"
ZXING_ARGS=()
if [[ -d "${PROJECT_ROOT}/third_party/zxing-cpp" ]]; then
  # Fully offline build against a local checkout.
  ZXING_ARGS=(-DSIDB_ZXING_SOURCE_DIR="${PROJECT_ROOT}/third_party/zxing-cpp")
  ok "using the local ZXing-C++ checkout"
fi

cmake -S "${PROJECT_ROOT}" -B "${BUILD_DIR}" -DCMAKE_BUILD_TYPE=Release \
      -DSIDB_CONFIGURE_PYTHON=OFF "${ZXING_ARGS[@]}" \
  | sed 's/^/    /' || die "CMake configuration failed"

# The first build downloads and compiles ZXing-C++; this takes a few minutes.
JOBS="$(nproc 2>/dev/null || echo 4)"
cmake --build "${BUILD_DIR}" -j "${JOBS}" 2>&1 | tail -25 || die "the C++ build failed"
if [[ -x "${BUILD_DIR}/cpp/student_id_scanner" ]]; then
  ok "built ${BUILD_DIR}/cpp/student_id_scanner"
else
  die "the scanner binary was not produced"
fi

# ---------------------------------------------------------------------------
step "7/7  Tests"
# ---------------------------------------------------------------------------
if [[ ${SKIP_TESTS} -eq 1 ]]; then
  warn "skipped (--skip-tests)"
else
  ctest --test-dir "${BUILD_DIR}" --output-on-failure 2>&1 | tail -30 || warn "the C++ tests reported failures"
  "${PYTHON_BIN}" -m pytest -q python/tests tests/integration 2>&1 | tail -20 || warn "the Python tests reported failures"
fi

printf '\n%s============================================================%s\n' "${GREEN}" "${RESET}"
printf '%s Setup complete.%s\n\n' "${BOLD}" "${RESET}"
cat <<EOF
  Start the system:      ./scripts/start.sh
  Open the dashboard:    http://127.0.0.1:8000
  API documentation:     http://127.0.0.1:8000/docs
  Run the C++ scanner:   ${BUILD_DIR}/cpp/student_id_scanner
  Stop everything:       ./scripts/stop.sh

  Demo sign-in (from database/seed.sql):
      admin   / DemoPass!2026      full access
      cashier / DemoPass!2026      scan, verify, transactions
      staff   / DemoPass!2026      scan, verify, read-only reports

  Change the administrator password before using this for anything real:
      python -m app.cli set-password admin

EOF
