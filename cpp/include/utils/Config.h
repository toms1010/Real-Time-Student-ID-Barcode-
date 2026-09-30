// ===========================================================================
//  Config.h - shared configuration for the C++ scanner
//
//  The scanner reads the *same* YAML file as the Python service
//  (config/config.yaml, falling back to config/config.example.yaml), so a
//  deployment has one file to edit.  Environment variables override it using
//  the same names as the Python side (CAMERA_DEVICE_INDEX, SCAN_COOLDOWN_MS,
//  API_HOST, ...), and command-line flags override everything.
//
//  The parser is a deliberately small subset of YAML - two levels of
//  `section: / key: value` - which is exactly what the shipped configuration
//  uses.  Depending on libyaml/yaml-cpp for that would add a system package for
//  no benefit; see docs/architecture/decisions.md.
//
//  Unknown keys are reported, not ignored silently, so a typo is visible.
// ===========================================================================
#ifndef SIDB_UTILS_CONFIG_H
#define SIDB_UTILS_CONFIG_H

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace sidb {

// --- application -----------------------------------------------------------
struct ApplicationConfig {
    std::string name = "Student ID Barcode Detection System";
    std::string environment = "development";
    std::string version = "1.0.0";
    std::string log_level = "INFO";
    std::string log_format = "text";
    std::string log_file;
};

// --- camera ----------------------------------------------------------------
struct CameraConfig {
    int device_index = 0;
    int width = 1280;
    int height = 720;
    int fps = 30;
    std::string source;            // empty = device index, else path or rtsp URL
    int warmup_frames = 5;
    int frame_timeout_ms = 2000;
    double roi_x = 0.10;
    double roi_y = 0.25;
    double roi_w = 0.80;
    double roi_h = 0.50;
    bool flip = false;
};

// --- scanner ---------------------------------------------------------------
struct ScannerConfig {
    int cooldown_ms = 2000;
    double confidence_threshold = 0.80;
    int barcode_timeout_ms = 3000;
    bool detect_roi = true;
    int max_tracked_barcodes = 512;
    int decode_attempts = 2;
    std::string allowed_formats = "Code128,Code39,EAN13,EAN8,UPCA,UPCE,ITF,QRCode,DataMatrix";
    std::string payload_pattern = "^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$";
    std::string stream_source = "python";
    std::string file_source;
    bool duplicate_log_per_cooldown = true;
    bool verify_remotely = true;   // POST /api/scans; false = local verification only
};

// --- preprocessing (mirrors app/vision/preprocess.py) ----------------------
struct PreprocessingConfig {
    bool enable_grayscale = true;
    bool enable_roi_crop = true;
    int target_width = 960;
    bool enable_clahe = true;
    double clahe_clip_limit = 2.0;
    double clahe_dark_trigger = 0.45;
    bool enable_denoise = true;
    double denoise_h = 5.0;
    double denoise_trigger = 0.02;
    bool enable_adaptive_threshold = false;
    int adaptive_block_size = 31;
    double adaptive_c = 5.0;
    double adaptive_bimodal_threshold = 0.55;
    bool enable_sharpen = false;
    double sharpen_amount = 0.6;
};

// --- api -------------------------------------------------------------------
struct ApiConfig {
    std::string host = "127.0.0.1";
    int port = 8000;
    int timeout_ms = 3000;
    int retries = 2;
    int retry_backoff_ms = 120;
    std::string scheme = "http";
    std::string base_path = "/api";
};

// --- database (local offline queue only) -----------------------------------
struct DatabaseConfig {
    std::string path = "database/scanner_offline_queue.db";
    int busy_timeout_ms = 3000;
    bool enabled = true;
};

// --- audio -----------------------------------------------------------------
struct AudioConfig {
    bool enabled = true;
    double volume = 0.8;
    std::string player = "auto";           // auto | aplay | paplay | ffplay | none
    std::string assets_dir = "assets/sounds";
};

// --- overlay / display -----------------------------------------------------
struct DisplayConfig {
    bool draw_overlay = true;
    bool show_fps = true;
    bool show_state = true;
    int jpeg_quality = 80;
    int banner_fps = 100;                  // 0 = every frame
};

// --- the whole configuration ----------------------------------------------
struct Config {
    ApplicationConfig application;
    CameraConfig camera;
    ScannerConfig scanner;
    PreprocessingConfig preprocessing;
    ApiConfig api;
    DatabaseConfig database;
    AudioConfig audio;
    DisplayConfig display;

    std::string config_file;               // resolved path, empty when defaults
    std::string project_root;              // repository root, for relative paths
    std::vector<std::string> warnings;     // unknown / invalid keys

    // --- loading ----------------------------------------------------------
    /// Load defaults, then the YAML file, then environment variables.
    /// `config_path` may be empty: config/config.yaml and then
    /// config/config.example.yaml are tried automatically.
    static Config load(const std::string& config_path = "");

    /// Apply environment overrides (SCAN_COOLDOWN_MS, API_PORT, ...).
    void apply_environment();

    /// Apply a single `section.key=value` override (command-line flags).
    bool apply_override(const std::string& dotted_key, const std::string& value);

    /// Resolve a possibly relative path against the project root.
    std::string resolve(const std::string& path) const;

    /// Base URL of the local API, e.g. "http://127.0.0.1:8000".
    std::string api_base_url() const;

    /// Validate ranges; appends messages to `warnings` and returns false when a
    /// value had to be corrected.
    bool validate();

    /// Multi-line dump used by `--print-config` and the log at start-up.
    std::string describe() const;
};

// --- helpers shared by Config.cpp and the CLI ------------------------------
namespace yamlmini {
/// Parse the two-level subset of YAML the configuration uses.
/// Returns section -> key -> value.  Values have quotes stripped.
std::map<std::string, std::map<std::string, std::string>> parse_file(
    const std::string& path, std::vector<std::string>* warnings);
}  // namespace yamlmini

namespace strutil {
std::string trim(const std::string& value);
std::string to_lower(const std::string& value);
std::vector<std::string> split(const std::string& value, char delimiter);
bool parse_int(const std::string& value, int& out);
bool parse_double(const std::string& value, double& out);
bool parse_bool(const std::string& value, bool& out);
}  // namespace strutil

}  // namespace sidb

#endif  // SIDB_UTILS_CONFIG_H
