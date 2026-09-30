# ===========================================================================
#  CheckAssets.cmake - fail early with an actionable message
#
#  Registered as a CTest test so `ctest` reports a *missing fixture* as its own
#  named failure instead of the barcode suite reporting seven confusing decode
#  errors.  Run by scripts/test.sh before the Python suite as well.
# ===========================================================================

if(NOT DEFINED SIDB_PROJECT_ROOT)
  message(FATAL_ERROR "SIDB_PROJECT_ROOT must be defined")
endif()

set(barcode_dir "${SIDB_PROJECT_ROOT}/assets/test-barcodes")
set(card_dir "${SIDB_PROJECT_ROOT}/assets/sample-id-cards")

if(NOT EXISTS "${barcode_dir}")
  message(FATAL_ERROR
    "assets/test-barcodes is missing.\n"
    "Create the fixtures with:\n"
    "  source .venv/bin/activate\n"
    "  python -m app.cli db-seed\n"
    "  python -m app.cli generate-assets")
endif()

foreach(fixture
    code128_primary.png
    code39_legacy.png
    qrcode_matrix.png
    datamatrix_matrix.png
    ean13_retail.png
    ean8_retail.png)
  if(NOT EXISTS "${barcode_dir}/${fixture}")
    message(FATAL_ERROR "missing barcode fixture: ${barcode_dir}/${fixture}")
  endif()
endforeach()

if(NOT EXISTS "${card_dir}/card_2026-000001.png")
  message(FATAL_ERROR
    "missing sample ID card: ${card_dir}/card_2026-000001.png\n"
    "Run: python -m app.cli generate-assets")
endif()

message(STATUS "all barcode fixtures are present")
