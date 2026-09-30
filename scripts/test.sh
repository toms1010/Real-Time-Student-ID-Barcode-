#!/usr/bin/env bash
# ===========================================================================
#  test.sh - run every test suite
#
#  Suites
#    1. assets        the generated barcode fixtures exist (else the barcode
#                     tests would silently test nothing)
#    2. mirrors      the scan state table matches across python/cpp/typescript
#    3. C++ unit      ctest: barcode, preprocessing, scanner state, API client
#    4. Python unit   pytest: students, scans, workflow, verification, reports
#    5. integration   pytest: barcode image -> API -> database -> report
#    6. performance   optional: real timing numbers into tests/performance/results
#
#  Usage:
#    ./scripts/test.sh                 all suites except performance
#    ./scripts/test.sh --perf         also run the performance benchmarks
#    ./scripts/test.sh --python-only  skip the C++ suites (no OpenCV needed)
#    ./scripts/test.sh --verbose       show the full pytest output
# ===========================================================================
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

VENV="${PROJECT_ROOT}/.venv"
PYTHON_BIN="${VENV}/bin/python"
BUILD_DIR="${PROJECT_ROOT}/build"
export PYTHONPATH="${PROJECT_ROOT}/python"
export SIDB_PROJECT_ROOT="${PROJECT_ROOT}"

WITH_PERF=0
PYTHON_ONLY=0
VERBOSE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --perf) WITH_PERF=1 ;;
    --python-only) PYTHON_ONLY=1 ;;
    --verbose|-v) VERBOSE=1 ;;
    --help|-h) sed -n "2,24p" "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

if [[ -t 1 ]]; then BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else BOLD=""; GREEN=""; RED=""; DIM=""; RESET=""; fi

RESULTS=()
record() { RESULTS+=("$1|$2"); }

banner() { printf '\n%s========== %s ==========%s\n' "${BOLD}" "$*" "${RESET}"; }

# ---------------------------------------------------------------------------
banner "0. Test fixtures"
if [[ -f assets/test-barcodes/code128_primary.png ]]; then
  printf '  %s✓%s barcode fixtures present\n' "${GREEN}" "${RESET}"
else
  printf '  %sx%s missing. Generate them with:\n' "${RED}" "${RESET}"
  printf '      source .venv/bin/activate\n'
  printf '      python -m app.cli db-seed && python -m app.cli generate-assets\n'
  exit 1
fi

# ---------------------------------------------------------------------------
# The workflow is specified once and mirrored into the C++ scanner and the
# cashier UI. Three hand-written copies drift, so the table is diffed before
# anything else runs: a mismatch here explains any downstream state failure.
banner "1. Scan state machine mirrors agree (python / cpp / typescript)"
"${PYTHON_BIN}" scripts/check_state_mirror.py
record "State mirror" "$?"

# ---------------------------------------------------------------------------
if [[ ${PYTHON_ONLY} -eq 0 && -d "${BUILD_DIR}" ]]; then
  banner "2. C++ unit tests (ctest)"
  ctest --test-dir "${BUILD_DIR}" --output-on-failure
  record "C++ (ctest)" "$?"
else
  printf '\n%s(2. C++ tests skipped)%s\n' "${DIM}" "${RESET}"
fi

# ---------------------------------------------------------------------------
banner "3. Python unit tests"
PYTEST_ARGS=(-q --strict-markers)
[[ ${VERBOSE} -eq 1 ]] && PYTEST_ARGS=(-v)
if [[ ${VERBOSE} -eq 1 ]]; then
  "${PYTHON_BIN}" -m pytest "${PYTEST_ARGS[@]}" python/tests
else
  "${PYTHON_BIN}" -m pytest "${PYTEST_ARGS[@]}" python/tests 2>&1 | tail -25
  record "Python unit" "${PIPESTATUS[0]}"
fi

# ---------------------------------------------------------------------------
banner "4. Integration tests (barcode -> API -> database -> report)"
if ! compgen -G "tests/integration/test_*.py" >/dev/null; then
  # pytest exits 5 ("no tests collected"), which would look like a failure.
  printf '  %s-%s tests/integration/ has no test_*.py yet - skipped\n' "${DIM}" "${RESET}"
else
  if [[ ${VERBOSE} -eq 1 ]]; then
    "${PYTHON_BIN}" -m pytest "${PYTEST_ARGS[@]}" -m integration tests/integration
  else
    "${PYTHON_BIN}" -m pytest "${PYTEST_ARGS[@]}" -m integration tests/integration 2>&1 | tail -25
    record "Integration" "${PIPESTATUS[0]}"
  fi
fi

# ---------------------------------------------------------------------------
if [[ ${WITH_PERF} -eq 1 ]]; then
  banner "4. Performance benchmarks (real measurements)"
  mkdir -p tests/performance/results
  printf '  decoding the generated assets...\n'
  "${PYTHON_BIN}" -m app.cli benchmark \
      --images assets/sample-id-cards --iterations 20 \
      --output tests/performance/results/python-benchmark.json
  record "Benchmark (python)" "$?"
  if [[ -x "${BUILD_DIR}/cpp/student_id_scanner" ]]; then
    printf '  running the C++ benchmark...\n'
    "${BUILD_DIR}/cpp/student_id_scanner" --benchmark \
        --images=assets/sample-id-cards --iterations=20 --no-overlay \
        --json=tests/performance/results/cpp-benchmark.json --log-level=ERROR
    record "Benchmark (C++)" "$?"
  fi
  printf '\n  Results written to tests/performance/results/.\n'
  printf '  Copy the numbers into docs/performance-evaluation.md to replace the\n'
  printf '  [MEASURE] placeholders. Nothing is filled in automatically.\n'
else
  printf '\n%s(5. Performance benchmarks: run ./scripts/test.sh --perf)%s\n' "${DIM}" "${RESET}"
fi

# ---------------------------------------------------------------------------
banner "Summary"
FAILED=0
for entry in "${RESULTS[@]:-}"; do
  name="${entry%%|*}"
  status="${entry##*|}"
  if [[ "${status}" -eq 0 ]]; then
    printf '  %s✓%s %s\n' "${GREEN}" "${RESET}" "${name}"
  else
    printf '  %sx%s %s (exit %s)\n' "${RED}" "${RESET}" "${name}" "${status}"
    FAILED=1
  fi
done

if [[ ${FAILED} -eq 0 ]]; then
  printf '\n%sAll suites passed.%s\n' "${GREEN}" "${RESET}"
else
  printf '\n%sSome suites failed - see the output above.%s\n' "${RED}" "${RESET}"
fi
exit ${FAILED}
