// ===========================================================================
//  Config.cpp - YAML subset parser, environment overrides, validation
// ===========================================================================
#include "utils/Config.h"

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <type_traits>
#include <fstream>
#include <sstream>
#include <unistd.h>

#include "utils/Logger.hpp"

namespace sidb {

namespace strutil {

std::string trim(const std::string& value) {
    std::size_t begin = 0;
    std::size_t end = value.size();
    while (begin < end && std::isspace(static_cast<unsigned char>(value[begin]))) ++begin;
    while (end > begin && std::isspace(static_cast<unsigned char>(value[end - 1]))) --end;
    return value.substr(begin, end - begin);
}

std::string to_lower(const std::string& value) {
    std::string out = value;
    std::transform(out.begin(), out.end(), out.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return out;
}

std::vector<std::string> split(const std::string& value, char delimiter) {
    std::vector<std::string> parts;
    std::string current;
    for (char c : value) {
        if (c == delimiter) {
            parts.push_back(trim(current));
            current.clear();
        } else {
            current.push_back(c);
        }
    }
    parts.push_back(trim(current));
    return parts;
}

bool parse_int(const std::string& value, int& out) {
    try {
        std::size_t consumed = 0;
        const int parsed = std::stoi(trim(value), &consumed);
        if (consumed != trim(value).size()) return false;
        out = parsed;
        return true;
    } catch (const std::exception&) {
        return false;
    }
}

bool parse_double(const std::string& value, double& out) {
    try {
        std::size_t consumed = 0;
        const double parsed = std::stod(trim(value), &consumed);
        if (consumed != trim(value).size()) return false;
        out = parsed;
        return true;
    } catch (const std::exception&) {
        return false;
    }
}

bool parse_bool(const std::string& value, bool& out) {
    const std::string lowered = to_lower(trim(value));
    if (lowered == "1" || lowered == "true" || lowered == "yes" || lowered == "on") {
        out = true;
        return true;
    }
    if (lowered == "0" || lowered == "false" || lowered == "no" || lowered == "off") {
        out = false;
        return true;
    }
    return false;
}

}  // namespace strutil

namespace yamlmini {

std::map<std::string, std::map<std::string, std::string>> parse_file(
    const std::string& path, std::vector<std::string>* warnings) {
    std::map<std::string, std::map<std::string, std::string>> result;
    std::ifstream file(path);
    if (!file.is_open()) {
        if (warnings) warnings->push_back("could not open configuration file: " + path);
        return result;
    }

    std::string section;
    std::string raw;
    int line_number = 0;
    while (std::getline(file, raw)) {
        ++line_number;
        // Strip a trailing CR from CRLF files.
        if (!raw.empty() && raw.back() == '\r') raw.pop_back();
        const std::string line = strutil::trim(raw);
        if (line.empty() || line[0] == '#') continue;

        const std::size_t indent = raw.find_first_not_of(" \t");
        const std::size_t colon = line.find(':');
        if (colon == std::string::npos) {
            if (warnings) {
                warnings->push_back(path + ":" + std::to_string(line_number) +
                                    ": expected 'key: value', got '" + line + "'");
            }
            continue;
        }
        std::string key = strutil::trim(line.substr(0, colon));
        std::string value = strutil::trim(line.substr(colon + 1));
        // Strip inline comments, but only when the '#' is preceded by a space so
        // that a value such as "rgb#123" survives.
        const std::size_t comment = value.find(" #");
        if (comment != std::string::npos) value = strutil::trim(value.substr(0, comment));
        // Strip surrounding quotes.
        if (value.size() >= 2 && ((value.front() == '"' && value.back() == '"') ||
                                  (value.front() == '\'' && value.back() == '\''))) {
            value = value.substr(1, value.size() - 2);
        }
        if (key.empty()) continue;

        if (indent == 0) {
            section = key;                       // new section
            if (!value.empty()) {                // "key: value" at top level
                result["general"][key] = value;
                section.clear();
            }
            continue;
        }
        if (section.empty()) {
            if (warnings) {
                warnings->push_back(path + ":" + std::to_string(line_number) +
                                    ": key '" + key + "' appears before any section");
            }
            section = "general";
        }
        result[section][key] = value;
    }
    return result;
}

}  // namespace yamlmini

namespace {

/// Empty *section* table, returned for a section the file does not define.
const std::map<std::string, std::string>& empty_table() {
    static const std::map<std::string, std::string> table;
    return table;
}

/// Apply one key of a parsed table, returning false for an unknown key.
template <typename T>
bool assign(T& target, const std::map<std::string, std::string>& table, const std::string& key,
            const std::string& value, const std::string& section, std::vector<std::string>* warnings) {
    if (!table.count(key)) return false;
    const std::string& raw = table.at(key);
    if constexpr (std::is_same_v<T, std::string>) {
        target = raw;
    } else if constexpr (std::is_same_v<T, bool>) {
        bool parsed = false;
        if (!strutil::parse_bool(raw, parsed)) {
            if (warnings) {
                warnings->push_back(section + "." + key + " must be a boolean, got '" + raw + "'");
            }
            return true;
        }
        target = parsed;
    } else if constexpr (std::is_same_v<T, int>) {
        int parsed = 0;
        if (!strutil::parse_int(raw, parsed)) {
            if (warnings) {
                warnings->push_back(section + "." + key + " must be an integer, got '" + raw + "'");
            }
            return true;
        }
        target = parsed;
    } else if constexpr (std::is_same_v<T, double>) {
        double parsed = 0.0;
        if (!strutil::parse_double(raw, parsed)) {
            if (warnings) {
                warnings->push_back(section + "." + key + " must be a number, got '" + raw + "'");
            }
            return true;
        }
        target = parsed;
    }
    return true;
}

/// Warn about keys present in the file but absent from the struct.
void check_unknown(const std::map<std::string, std::string>& table,
                   const std::vector<std::string>& known, const std::string& section,
                   std::vector<std::string>* warnings) {
    for (const auto& [key, value] : table) {
        (void)value;
        bool found = false;
        for (const std::string& candidate : known) {
            if (candidate == key) {
                found = true;
                break;
            }
        }
        if (!found && warnings) {
            warnings->push_back("ignoring unknown configuration key: " + section + "." + key);
        }
    }
}

const char* env_or_null(const char* name) {
    const char* value = std::getenv(name);
    return (value != nullptr && *value != '\0') ? value : nullptr;
}

}  // namespace

std::string Config::resolve(const std::string& path) const {
    if (path.empty()) return path;
    if (path.front() == '/') return path;
    if (project_root.empty()) return path;
    return project_root + "/" + path;
}

std::string Config::api_base_url() const {
    return api.scheme + "://" + api.host + ":" + std::to_string(api.port);
}

Config Config::load(const std::string& config_path) {
    Config config;
    const char* root = std::getenv("SIDB_PROJECT_ROOT");
    if (root != nullptr && *root != '\0') {
        config.project_root = root;
    } else {
        // The binary normally lives in <root>/build/, so the parent is the root.
        // The CLI also passes an explicit --project-root when that guess is wrong.
        char buffer[4096] = {0};
        const char* resolved = ::getcwd(buffer, sizeof(buffer) - 1);
        std::string cwd(resolved != nullptr ? std::string(buffer) : std::string("."));
        if (cwd.size() >= 6 && cwd.compare(cwd.size() - 6, 6, "/build") == 0) {
            config.project_root = cwd.substr(0, cwd.size() - 6);
        } else {
            config.project_root = cwd;
        }
    }

    std::string path = config_path;
    if (path.empty()) {
        const std::string primary = config.resolve("config/config.yaml");
        const std::string fallback = config.resolve("config/config.example.yaml");
        std::ifstream probe(primary);
        path = probe.good() ? primary : fallback;
    }
    if (!path.empty() && path.front() != '/') path = config.resolve(path);
    config.config_file = path;

    const auto table = yamlmini::parse_file(path, &config.warnings);
    const auto section = [&table](const char* name) -> const std::map<std::string, std::string>& {
        const auto it = table.find(name);
        return it == table.end() ? empty_table() : it->second;
    };

    const auto& app = section("application");
    assign(config.application.name, app, "name", "", "application", &config.warnings);
    assign(config.application.environment, app, "environment", "", "application", &config.warnings);
    assign(config.application.version, app, "version", "", "application", &config.warnings);
    assign(config.application.log_level, app, "log_level", "", "application", &config.warnings);
    assign(config.application.log_format, app, "log_format", "", "application", &config.warnings);
    assign(config.application.log_file, app, "log_file", "", "application", &config.warnings);
    check_unknown(app, {"name", "environment", "version", "log_level", "log_format", "log_file"},
                  "application", &config.warnings);

    const auto& cam = section("camera");
    assign(config.camera.device_index, cam, "device_index", "", "camera", &config.warnings);
    assign(config.camera.width, cam, "width", "", "camera", &config.warnings);
    assign(config.camera.height, cam, "height", "", "camera", &config.warnings);
    assign(config.camera.fps, cam, "fps", "", "camera", &config.warnings);
    assign(config.camera.source, cam, "source", "", "camera", &config.warnings);
    assign(config.camera.warmup_frames, cam, "warmup_frames", "", "camera", &config.warnings);
    assign(config.camera.frame_timeout_ms, cam, "frame_timeout_ms", "", "camera", &config.warnings);
    assign(config.camera.roi_x, cam, "roi_x", "", "camera", &config.warnings);
    assign(config.camera.roi_y, cam, "roi_y", "", "camera", &config.warnings);
    assign(config.camera.roi_w, cam, "roi_w", "", "camera", &config.warnings);
    assign(config.camera.roi_h, cam, "roi_h", "", "camera", &config.warnings);
    assign(config.camera.flip, cam, "flip", "", "camera", &config.warnings);
    check_unknown(cam,
                  {"device_index", "width", "height", "fps", "source", "warmup_frames",
                   "frame_timeout_ms", "roi_x", "roi_y", "roi_w", "roi_h", "flip"},
                  "camera", &config.warnings);

    const auto& scan = section("scanner");
    assign(config.scanner.cooldown_ms, scan, "cooldown_ms", "", "scanner", &config.warnings);
    assign(config.scanner.confidence_threshold, scan, "confidence_threshold", "", "scanner",
           &config.warnings);
    assign(config.scanner.barcode_timeout_ms, scan, "barcode_timeout_ms", "", "scanner",
           &config.warnings);
    assign(config.scanner.detect_roi, scan, "detect_roi", "", "scanner", &config.warnings);
    assign(config.scanner.max_tracked_barcodes, scan, "max_tracked_barcodes", "", "scanner",
           &config.warnings);
    assign(config.scanner.decode_attempts, scan, "decode_attempts", "", "scanner", &config.warnings);
    assign(config.scanner.allowed_formats, scan, "allowed_formats", "", "scanner", &config.warnings);
    assign(config.scanner.payload_pattern, scan, "payload_pattern", "", "scanner", &config.warnings);
    assign(config.scanner.stream_source, scan, "stream_source", "", "scanner", &config.warnings);
    assign(config.scanner.file_source, scan, "file_source", "", "scanner", &config.warnings);
    assign(config.scanner.duplicate_log_per_cooldown, scan, "duplicate_log_per_cooldown", "",
           "scanner", &config.warnings);
    check_unknown(scan,
                  {"cooldown_ms", "confidence_threshold", "barcode_timeout_ms", "detect_roi",
                   "max_tracked_barcodes", "decode_attempts", "allowed_formats", "payload_pattern",
                   "stream_source", "file_source", "duplicate_log_per_cooldown"},
                  "scanner", &config.warnings);

    const auto& pre = section("preprocessing");
    assign(config.preprocessing.enable_grayscale, pre, "enable_grayscale", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.enable_roi_crop, pre, "enable_roi_crop", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.target_width, pre, "target_width", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.enable_clahe, pre, "enable_clahe", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.clahe_clip_limit, pre, "clahe_clip_limit", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.clahe_dark_trigger, pre, "clahe_dark_trigger", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.enable_denoise, pre, "enable_denoise", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.denoise_h, pre, "denoise_h", "", "preprocessing", &config.warnings);
    assign(config.preprocessing.denoise_trigger, pre, "denoise_trigger", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.enable_adaptive_threshold, pre, "enable_adaptive_threshold", "",
           "preprocessing", &config.warnings);
    assign(config.preprocessing.adaptive_block_size, pre, "adaptive_block_size", "",
           "preprocessing", &config.warnings);
    assign(config.preprocessing.adaptive_c, pre, "adaptive_c", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.adaptive_bimodal_threshold, pre, "adaptive_bimodal_threshold", "",
           "preprocessing", &config.warnings);
    assign(config.preprocessing.enable_sharpen, pre, "enable_sharpen", "", "preprocessing",
           &config.warnings);
    assign(config.preprocessing.sharpen_amount, pre, "sharpen_amount", "", "preprocessing",
           &config.warnings);
    check_unknown(pre,
                  {"enable_grayscale", "enable_roi_crop", "target_width", "enable_clahe",
                   "clahe_clip_limit", "clahe_dark_trigger", "enable_denoise", "denoise_h",
                   "denoise_trigger", "enable_adaptive_threshold", "adaptive_block_size",
                   "adaptive_c", "adaptive_bimodal_threshold", "enable_sharpen",
                   "sharpen_amount"},
                  "preprocessing", &config.warnings);

    const auto& api = section("api");
    assign(config.api.host, api, "host", "", "api", &config.warnings);
    assign(config.api.port, api, "port", "", "api", &config.warnings);
    assign(config.api.timeout_ms, api, "timeout_ms", "", "api", &config.warnings);
    assign(config.api.retries, api, "retries", "", "api", &config.warnings);
    assign(config.api.retry_backoff_ms, api, "retry_backoff_ms", "", "api", &config.warnings);
    check_unknown(api, {"host", "port", "base_url", "timeout_ms", "retries", "retry_backoff_ms",
                        "cors_origins"},
                  "api", &config.warnings);

    const auto& database = section("database");
    // The scanner never opens the application database (ADR-004); this is the
    // local, single-file queue used when the API is unreachable.
    check_unknown(database, {"path", "busy_timeout_ms", "enable_wal"}, "database", &config.warnings);

    const auto& audio = section("audio");
    assign(config.audio.enabled, audio, "enabled", "", "audio", &config.warnings);
    assign(config.audio.volume, audio, "volume", "", "audio", &config.warnings);
    assign(config.audio.player, audio, "player", "", "audio", &config.warnings);
    assign(config.audio.assets_dir, audio, "assets_dir", "", "audio", &config.warnings);
    check_unknown(audio, {"enabled", "volume", "player", "assets_dir"}, "audio", &config.warnings);

    config.apply_environment();
    config.validate();
    return config;
}

void Config::apply_environment() {
    const auto set_int = [](const char* name, int& target, std::vector<std::string>& warnings) {
        if (const char* raw = env_or_null(name)) {
            int parsed = 0;
            if (strutil::parse_int(raw, parsed)) {
                target = parsed;
            } else {
                warnings.push_back(std::string(name) + " is not an integer: " + raw);
            }
        }
    };
    const auto set_double =
        [](const char* name, double& target, std::vector<std::string>& warnings) {
            if (const char* raw = env_or_null(name)) {
                double parsed = 0.0;
                if (strutil::parse_double(raw, parsed)) {
                    target = parsed;
                } else {
                    warnings.push_back(std::string(name) + " is not a number: " + raw);
                }
            }
        };
    const auto set_bool =
        [](const char* name, bool& target, std::vector<std::string>& warnings) {
            if (const char* raw = env_or_null(name)) {
                bool parsed = false;
                if (strutil::parse_bool(raw, parsed)) {
                    target = parsed;
                } else {
                    warnings.push_back(std::string(name) + " is not a boolean: " + raw);
                }
            }
        };
    const auto set_string = [](const char* name, std::string& target,
                               std::vector<std::string>& warnings) {
        if (const char* raw = env_or_null(name)) {
            target = raw;
        }
        (void)warnings;
    };

    set_string("APP_NAME", application.name, warnings);
    set_string("APP_ENV", application.environment, warnings);
    set_string("LOG_LEVEL", application.log_level, warnings);
    set_int("CAMERA_DEVICE_INDEX", camera.device_index, warnings);
    set_int("CAMERA_FRAME_WIDTH", camera.width, warnings);
    set_int("CAMERA_FRAME_HEIGHT", camera.height, warnings);
    set_int("CAMERA_FPS", camera.fps, warnings);
    set_string("CAMERA_SOURCE", camera.source, warnings);
    set_int("SCAN_COOLDOWN_MS", scanner.cooldown_ms, warnings);
    set_double("CONFIDENCE_THRESHOLD", scanner.confidence_threshold, warnings);
    set_int("BARCODE_TIMEOUT_MS", scanner.barcode_timeout_ms, warnings);
    set_bool("DETECT_ROI", scanner.detect_roi, warnings);
    set_string("ALLOWED_BARCODE_FORMATS", scanner.allowed_formats, warnings);
    set_string("BARCODE_PATTERN", scanner.payload_pattern, warnings);
    set_string("API_HOST", api.host, warnings);
    set_int("API_PORT", api.port, warnings);
    set_int("API_TIMEOUT_MS", api.timeout_ms, warnings);
    set_bool("AUDIO_ENABLED", audio.enabled, warnings);
    set_double("AUDIO_VOLUME", audio.volume, warnings);
    set_string("AUDIO_PLAYER", audio.player, warnings);
    set_string("SIDB_PROJECT_ROOT", project_root, warnings);
}

bool Config::apply_override(const std::string& dotted_key, const std::string& value) {
    const std::size_t dot = dotted_key.find('.');
    if (dot == std::string::npos) {
        warnings.push_back("override must look like section.key: " + dotted_key);
        return false;
    }
    const std::string section = dotted_key.substr(0, dot);
    const std::string key = dotted_key.substr(dot + 1);

    // Resolve a pointer by section name, then reuse the string/int/double/bool
    // assignment logic with a temporary table.
    struct Resolver {
        Config* config;
        std::string section;
        bool operator()(const std::string& key, const std::string& value) const {
            Config& c = *config;
            if (section == "application") {
                if (key == "name") { c.application.name = value; return true; }
                if (key == "log_level") { c.application.log_level = value; return true; }
                if (key == "log_file") { c.application.log_file = value; return true; }
                if (key == "environment") { c.application.environment = value; return true; }
            } else if (section == "camera") {
                if (key == "device_index") return strutil::parse_int(value, c.camera.device_index);
                if (key == "width") return strutil::parse_int(value, c.camera.width);
                if (key == "height") return strutil::parse_int(value, c.camera.height);
                if (key == "fps") return strutil::parse_int(value, c.camera.fps);
                if (key == "source") { c.camera.source = value; return true; }
                if (key == "flip") return strutil::parse_bool(value, c.camera.flip);
            } else if (section == "scanner") {
                if (key == "cooldown_ms") return strutil::parse_int(value, c.scanner.cooldown_ms);
                if (key == "confidence_threshold")
                    return strutil::parse_double(value, c.scanner.confidence_threshold);
                if (key == "decode_attempts")
                    return strutil::parse_int(value, c.scanner.decode_attempts);
                if (key == "allowed_formats") { c.scanner.allowed_formats = value; return true; }
                if (key == "payload_pattern") { c.scanner.payload_pattern = value; return true; }
                if (key == "verify_remotely")
                    return strutil::parse_bool(value, c.scanner.verify_remotely);
                if (key == "stream_source") { c.scanner.stream_source = value; return true; }
                if (key == "file_source") { c.scanner.file_source = value; return true; }
            } else if (section == "api") {
                if (key == "host") { c.api.host = value; return true; }
                if (key == "port") return strutil::parse_int(value, c.api.port);
                if (key == "timeout_ms") return strutil::parse_int(value, c.api.timeout_ms);
                if (key == "retries") return strutil::parse_int(value, c.api.retries);
            } else if (section == "preprocessing") {
                if (key == "target_width")
                    return strutil::parse_int(value, c.preprocessing.target_width);
                if (key == "enable_adaptive_threshold")
                    return strutil::parse_bool(value, c.preprocessing.enable_adaptive_threshold);
                if (key == "enable_clahe")
                    return strutil::parse_bool(value, c.preprocessing.enable_clahe);
                if (key == "enable_denoise")
                    return strutil::parse_bool(value, c.preprocessing.enable_denoise);
                if (key == "enable_roi_crop")
                    return strutil::parse_bool(value, c.preprocessing.enable_roi_crop);
            } else if (section == "audio") {
                if (key == "enabled") return strutil::parse_bool(value, c.audio.enabled);
                if (key == "volume") return strutil::parse_double(value, c.audio.volume);
                if (key == "player") { c.audio.player = value; return true; }
            } else if (section == "display") {
                if (key == "draw_overlay") return strutil::parse_bool(value, c.display.draw_overlay);
                if (key == "jpeg_quality")
                    return strutil::parse_int(value, c.display.jpeg_quality);
                if (key == "banner_fps")
                    return strutil::parse_int(value, c.display.banner_fps);
            }
            return false;
        }
    };

    Resolver resolver{this, section};
    if (resolver(key, value)) return true;
    warnings.push_back("unknown configuration override: " + dotted_key);
    return false;
}

bool Config::validate() {
    bool adjusted = false;
    const auto fix = [this, &adjusted](const char* message) {
        warnings.emplace_back(message);
        adjusted = true;
    };

    if (camera.width <= 0 || camera.height <= 0) {
        camera.width = 1280;
        camera.height = 720;
        fix("camera resolution must be positive; falling back to 1280x720");
    }
    if (camera.fps <= 0) {
        camera.fps = 30;
        fix("camera.fps must be positive; falling back to 30");
    }
    if (camera.device_index < 0) {
        camera.device_index = 0;
        fix("camera.device_index must be >= 0; falling back to 0");
    }
    if (camera.roi_x < 0.0 || camera.roi_y < 0.0 || camera.roi_w <= 0.0 || camera.roi_h <= 0.0 ||
        camera.roi_x + camera.roi_w > 1.0 || camera.roi_y + camera.roi_h > 1.0) {
        camera.roi_x = 0.10;
        camera.roi_y = 0.25;
        camera.roi_w = 0.80;
        camera.roi_h = 0.50;
        fix("camera ROI must stay inside the frame; falling back to 10%/25%/80%/50%");
    }
    if (scanner.cooldown_ms < 0) {
        scanner.cooldown_ms = 2000;
        fix("scanner.cooldown_ms must be >= 0; falling back to 2000");
    }
    if (scanner.confidence_threshold < 0.0 || scanner.confidence_threshold > 1.0) {
        scanner.confidence_threshold = 0.80;
        fix("scanner.confidence_threshold must be 0..1; falling back to 0.80");
    }
    if (scanner.decode_attempts < 1) {
        scanner.decode_attempts = 1;
        fix("scanner.decode_attempts must be >= 1; falling back to 1");
    }
    if (scanner.max_tracked_barcodes < 1) {
        scanner.max_tracked_barcodes = 512;
        fix("scanner.max_tracked_barcodes must be >= 1; falling back to 512");
    }
    if (api.port <= 0 || api.port > 65535) {
        api.port = 8000;
        fix("api.port must be 1..65535; falling back to 8000");
    }
    if (api.timeout_ms <= 0) {
        api.timeout_ms = 3000;
        fix("api.timeout_ms must be > 0; falling back to 3000");
    }
    if (audio.volume < 0.0 || audio.volume > 1.0) {
        audio.volume = 0.8;
        fix("audio.volume must be 0..1; falling back to 0.8");
    }
    if (display.jpeg_quality < 10 || display.jpeg_quality > 95) {
        display.jpeg_quality = 80;
        fix("display.jpeg_quality must be 10..95; falling back to 80");
    }
    if (preprocessing.target_width < 160) {
        preprocessing.target_width = 960;
        fix("preprocessing.target_width must be >= 160; falling back to 960");
    }
    if (preprocessing.adaptive_block_size % 2 == 0) {
        preprocessing.adaptive_block_size += 1;
        fix("preprocessing.adaptive_block_size must be odd; incremented by 1");
    }
    return !adjusted;
}

std::string Config::describe() const {
    std::ostringstream out;
    out << "configuration\n"
        << "  file              : " << (config_file.empty() ? "(defaults)" : config_file) << '\n'
        << "  project root      : " << project_root << '\n'
        << "  application       : " << application.name << " v" << application.version
        << " [" << application.environment << "]\n"
        << "  log level         : " << application.log_level << '\n'
        << "  camera            : index=" << camera.device_index << ' ' << camera.width << 'x'
        << camera.height << " @" << camera.fps << "fps"
        << (camera.source.empty() ? "" : " source=" + camera.source) << '\n'
        << "  scan region       : x=" << camera.roi_x << " y=" << camera.roi_y << " w=" << camera.roi_w
        << " h=" << camera.roi_h << '\n'
        << "  api               : " << api_base_url() << " timeout=" << api.timeout_ms << "ms"
        << " retries=" << api.retries << '\n'
        << "  cooldown          : " << scanner.cooldown_ms << " ms\n"
        << "  allowed formats   : " << scanner.allowed_formats << '\n'
        << "  payload pattern   : " << scanner.payload_pattern << '\n'
        << "  remote verify     : " << (scanner.verify_remotely ? "yes" : "no") << '\n'
        << "  offline queue     : " << (database.enabled ? database.path : "(disabled)") << '\n'
        << "  audio             : " << (audio.enabled ? audio.player : "disabled") << " volume="
        << audio.volume << '\n';
    for (const std::string& warning : warnings) {
        out << "  warning           : " << warning << '\n';
    }
    return out.str();
}

}  // namespace sidb
