cmake_minimum_required(VERSION 3.20)

# ===========================================================================
#  Top-level CMake project
#
#  The C++ scanner is the only compiled component, so this file exists to give
#  the documented top-level build a single entry point:
#
#      cmake -S . -B build
#      cmake --build build -j$(nproc)
#      ctest --test-dir build --output-on-failure
#
#  It also configures the Python side (creates .venv, installs dependencies,
#  initialises the database) when SIDB_CONFIGURE_PYTHON=ON, which is what
#  scripts/setup.sh uses.  The Python half is *not* required to build the C++
#  scanner, so the default is ON for a one-command setup and can be turned off
#  in CI.
# ===========================================================================
project(student_id_barcode_system
        VERSION 1.0.0
        DESCRIPTION "Real-Time Student ID Barcode Detection and Verification System"
        LANGUAGES CXX)

option(SIDB_CONFIGURE_PYTHON "Create the venv, install dependencies and seed the database" ON)
option(SIDB_RUN_TESTS "Run the Python test suite during configuration" OFF)

if(SIDB_CONFIGURE_PYTHON)
  find_package(Python3 3.11 REQUIRED COMPONENTS Interpreter)

  set(VENV_DIR "${CMAKE_CURRENT_SOURCE_DIR}/.venv")
  if(NOT EXISTS "${VENV_DIR}/bin/python")
    message(STATUS "creating the Python virtual environment in ${VENV_DIR}")
    execute_process(
      COMMAND "${Python3_EXECUTABLE}" -m venv "${VENV_DIR}"
      RESULT_VARIABLE VENV_RESULT
      OUTPUT_VARIABLE VENV_OUTPUT
      ERROR_VARIABLE VENV_ERROR)
    if(NOT VENV_RESULT EQUAL 0)
      message(FATAL_ERROR "could not create the virtual environment:\n${VENV_ERROR}")
    endif()
  endif()

  set(PYTHON "${VENV_DIR}/bin/python")
  message(STATUS "installing Python dependencies (requirements.txt)")
  execute_process(
    COMMAND "${PYTHON}" -m pip install --quiet --upgrade pip
    RESULT_VARIABLE PIP_UPGRADE_RESULT)
  execute_process(
    COMMAND "${PYTHON}" -m pip install --quiet -r
            "${CMAKE_CURRENT_SOURCE_DIR}/requirements.txt"
    RESULT_VARIABLE PIP_RESULT
    OUTPUT_VARIABLE PIP_OUTPUT
    ERROR_VARIABLE PIP_ERROR)
  if(NOT PIP_RESULT EQUAL 0)
    message(WARNING "pip install reported a problem:\n${PIP_ERROR}\n"
                    "The C++ scanner will still build; run ./scripts/setup.sh for details.")
  endif()

  message(STATUS "initialising the SQLite database")
  execute_process(
    COMMAND "${PYTHON}" -m app.cli db-init
    WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
    RESULT_VARIABLE DB_RESULT
    OUTPUT_VARIABLE DB_OUTPUT
    ERROR_VARIABLE DB_ERROR)
  if(NOT DB_RESULT EQUAL 0)
    message(WARNING "db-init failed:\n${DB_ERROR}")
  else()
    message(STATUS "  ${DB_OUTPUT}")
  endif()

  if(SIDB_RUN_TESTS)
    message(STATUS "running the Python test suite")
    execute_process(
      COMMAND "${PYTHON}" -m pytest -q
      WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
      RESULT_VARIABLE PYTEST_RESULT)
    if(NOT PYTEST_RESULT EQUAL 0)
      message(WARNING "the Python test suite reported failures")
    endif()
  endif()
endif()

# --- the C++ scanner -------------------------------------------------------
add_subdirectory(cpp)

message(STATUS "")
message(STATUS "Next steps")
message(STATUS "  cmake --build build -j            build the C++ scanner")
message(STATUS "  ctest --test-dir build            run the C++ test suites")
message(STATUS "  ./scripts/setup.sh                full setup (Python + C++ + database)")
message(STATUS "  ./scripts/start.sh                start the API and the web UI")
message(STATUS "")
