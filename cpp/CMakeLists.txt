cmake_minimum_required(VERSION 3.20)

# ===========================================================================
#  Real-Time Student ID Barcode Detection System - C++ scanner
#
#  Components
#    sidb_core        static library with the whole scanner
#    student_id_scanner   the executable (native / service / file / benchmark)
#    ctest suites: test_barcode, test_processing, test_scanner
#
#  Dependencies
#    OpenCV        REQUIRED  (libopencv-dev)          camera + image processing
#    SQLite3       REQUIRED  (libsqlite3-dev)         offline scan queue
#    ZXing-C++     fetched   (github.com/zxing-cpp)   real barcode decoder
#
#  Offline builds: pass -DSIDB_ZXING_SOURCE_DIR=/path/to/zxing-cpp to use a
#  local checkout instead of downloading (see docs/installation.md).
# ===========================================================================
project(student_id_scanner
        VERSION 1.0.0
        DESCRIPTION "Real-time student ID barcode detection and verification"
        LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(SIDB_BUILD_TESTS "Build the C++ test suites" ON)
option(SIDB_BUILD_TOOLS "Build the developer tools" ON)
option(SIDB_WERROR "Treat warnings as errors" OFF)

# --- warnings --------------------------------------------------------------
add_library(sidb_warnings INTERFACE)
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(sidb_warnings INTERFACE
    -Wall -Wextra -Wpedantic -Wno-unused-parameter)
  if(SIDB_WERROR)
    target_compile_options(sidb_warnings INTERFACE -Werror)
  endif()
endif()

# --- OpenCV ----------------------------------------------------------------
find_package(OpenCV REQUIRED COMPONENTS core imgproc imgcodecs highgui videoio)
message(STATUS "OpenCV ${OpenCV_VERSION} (${OpenCV_INCLUDE_DIRS})")

# --- SQLite3 ---------------------------------------------------------------
find_package(SQLite3 REQUIRED)
message(STATUS "SQLite3 ${SQLite3_VERSION}")

# --- threads ---------------------------------------------------------------
set(THREADS_PREFER_PTHREAD_FLAG ON)
find_package(Threads REQUIRED)

# --- ZXing-C++ -------------------------------------------------------------
# A real, offline-capable decoder.  Downloaded once, then cached in
# <build>/_deps; use -DSIDB_ZXING_SOURCE_DIR=... to build fully offline.
set(ZXING_VERSION "v2.3.0" CACHE STRING "ZXing-C++ git tag to build against")
if(SIDB_ZXING_SOURCE_DIR)
  message(STATUS "ZXing-C++ from local checkout: ${SIDB_ZXING_SOURCE_DIR}")
  add_subdirectory(${SIDB_ZXING_SOURCE_DIR} ${CMAKE_BINARY_DIR}/_zxing EXCLUDE_FROM_ALL)
else()
  include(FetchContent)
  set(ZXING_EXAMPLES OFF CACHE BOOL "" FORCE)
  set(ZXING_EXPERIMENTAL_API OFF CACHE BOOL "" FORCE)
  set(ZXING_UNIT_TESTS OFF CACHE BOOL "" FORCE)
  set(ZXING_BLACKBOX_TESTS OFF CACHE BOOL "" FORCE)
  set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
  FetchContent_Declare(
    zxing-cpp
    GIT_REPOSITORY https://github.com/zxing-cpp/zxing-cpp.git
    GIT_TAG        ${ZXING_VERSION}
    GIT_SHALLOW    TRUE
  )
  FetchContent_MakeAvailable(zxing-cpp)
endif()
if(NOT TARGET ZXing::ZXing)
  message(FATAL_ERROR "ZXing::ZXing target not found - check SIDB_ZXING_SOURCE_DIR")
endif()

# --- the scanner library ---------------------------------------------------
add_library(sidb_core STATIC
  src/camera/CameraManager.cpp
  src/processing/ImagePreprocessor.cpp
  src/processing/FrameProcessor.cpp
  src/barcode/BarcodeDecoder.cpp
  src/barcode/BarcodeDetector.cpp
  src/scanner/ScannerState.cpp
  src/scanner/PerformanceMonitor.cpp
  src/scanner/ScannerEngine.cpp
  src/api/HttpClient.cpp
  src/api/ApiClient.cpp
  src/database/DatabaseClient.cpp
  src/audio/AudioFeedback.cpp
  src/utils/Config.cpp
  src/utils/Logger.cpp
  src/utils/TimeUtil.cpp
)

target_include_directories(sidb_core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/include
  ${OpenCV_INCLUDE_DIRS}
)

target_link_libraries(sidb_core
  PUBLIC
    ZXing::ZXing
    Threads::Threads
  PRIVATE
    ${OpenCV_LIBS}
    SQLite::SQLite3
)

target_compile_definitions(sidb_core PUBLIC SIDB_VERSION="${PROJECT_VERSION}")
target_compile_options(sidb_core PRIVATE $<$<CONFIG:Release>:-O3>)

# --- the executable --------------------------------------------------------
add_executable(student_id_scanner src/main.cpp)
target_link_libraries(student_id_scanner PRIVATE sidb_core)
target_compile_options(student_id_scanner PRIVATE $<$<CONFIG:Release>:-O2>)
set_target_properties(student_id_scanner PROPERTIES OUTPUT_NAME student_id_scanner)

# --- developer tools -------------------------------------------------------
if(SIDB_BUILD_TOOLS)
  add_executable(sidb_check_config src/tools/check_config.cpp)
  target_link_libraries(sidb_check_config PRIVATE sidb_core)
endif()

# --- tests -----------------------------------------------------------------
if(SIDB_BUILD_TESTS)
  enable_testing()

  foreach(suite test_barcode test_processing test_scanner)
    add_executable(${suite} tests/${suite}.cpp)
    target_link_libraries(${suite} PRIVATE sidb_core)
    target_compile_options(${suite} PRIVATE $<$<CONFIG:Release>:-O1>)
    add_test(NAME ${suite} COMMAND ${suite})
    # The tests resolve fixtures through SIDB_PROJECT_ROOT.
    set_tests_properties(${suite} PROPERTIES
      ENVIRONMENT "SIDB_PROJECT_ROOT=${CMAKE_CURRENT_SOURCE_DIR}/.."
      TIMEOUT 300
    )
  endforeach()

  # Barcode tests need the generated assets; make the dependency explicit so a
  # missing fixture produces a clear message instead of a confusing failure.
  add_test(NAME test_assets_present
           COMMAND ${CMAKE_COMMAND}
                   -DSIDB_PROJECT_ROOT=${CMAKE_CURRENT_SOURCE_DIR}/..
                   -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/CheckAssets.cmake)
endif()

message(STATUS "")
message(STATUS "student_id_scanner ${PROJECT_VERSION}")
message(STATUS "  build type   : ${CMAKE_BUILD_TYPE}")
message(STATUS "  tests        : ${SIDB_BUILD_TESTS}")
message(STATUS "  tools        : ${SIDB_BUILD_TOOLS}")
message(STATUS "  ZXing-C++    : ${ZXING_VERSION}")
message(STATUS "")
