// ===========================================================================
//  ApiClient.cpp
// ===========================================================================
#include "api/ApiClient.h"

#include <cmath>
#include <cstdlib>
#include <sstream>

#include "database/DatabaseClient.h"
#include "utils/Logger.hpp"
#include "utils/TimeUtil.h"

namespace sidb {

namespace {

constexpr const char* kComponent = "api";

/// Locate `"key":` in `json` and return the offset of the value, or npos.
std::size_t find_value(const std::string& json, const std::string& key, std::size_t from = 0) {
    std::string needle = "\"" + key + "\"";
    const std::size_t position = json.find(needle, from);
    if (position == std::string::npos) return std::string::npos;
    std::size_t cursor = position + needle.size();
    while (cursor < json.size() &&
           (json[cursor] == ' ' || json[cursor] == '\t' || json[cursor] == '\n' ||
            json[cursor] == '\r')) {
        ++cursor;
    }
    if (cursor >= json.size() || json[cursor] != ':') return std::string::npos;
    ++cursor;
    while (cursor < json.size() && (json[cursor] == ' ' || json[cursor] == '\t')) ++cursor;
    return cursor;
}

std::string unescape(const std::string& value) {
    std::string out;
    out.reserve(value.size());
    for (std::size_t i = 0; i < value.size(); ++i) {
        if (value[i] != '\\' || i + 1 >= value.size()) {
            out += value[i];
            continue;
        }
        const char next = value[++i];
        switch (next) {
            case 'n': out += '\n'; break;
            case 't': out += '\t'; break;
            case 'r': out += '\r'; break;
            case 'b': out += '\b'; break;
            case 'f': out += '\f'; break;
            case 'u': {
                // Only the BMP subset the API can emit (names, courses).
                if (i + 4 < value.size()) {
                    const std::string hex = value.substr(i + 1, 4);
                    const long code = std::strtol(hex.c_str(), nullptr, 16);
                    if (code < 0x80) {
                        out += static_cast<char>(code);
                    } else if (code < 0x800) {
                        out += static_cast<char>(0xC0 | (code >> 6));
                        out += static_cast<char>(0x80 | (code & 0x3F));
                    } else {
                        out += static_cast<char>(0xE0 | (code >> 12));
                        out += static_cast<char>(0x80 | ((code >> 6) & 0x3F));
                        out += static_cast<char>(0x80 | (code & 0x3F));
                    }
                    i += 4;
                }
                break;
            }
            default: out += next;
        }
    }
    return out;
}

}  // namespace

ApiClient::ApiClient(const ApiConfig& config)
    : config_(config),
      base_url_(config.scheme + "://" + config.host + ":" + std::to_string(config.port)),
      client_(std::make_unique<http::HttpClient>(config.host, config.port, config.timeout_ms)) {
    SIDB_LOG(log::Level::Info, kComponent, "API client targeting " + base_url_ +
                                              " (timeout " + std::to_string(config.timeout_ms) +
                                              " ms, retries " + std::to_string(config.retries) + ")");
}

ApiClient::~ApiClient() = default;

std::string ApiClient::json_string(const std::string& json, const std::string& key) {
    const std::size_t start = find_value(json, key);
    if (start == std::string::npos) return {};
    if (start >= json.size()) return {};
    if (json[start] == '"') {
        std::string out;
        for (std::size_t i = start + 1; i < json.size(); ++i) {
            if (json[i] == '\\' && i + 1 < json.size()) {
                out += json[i];
                out += json[i + 1];
                ++i;
                continue;
            }
            if (json[i] == '"') return unescape(out);
            out += json[i];
        }
        return unescape(out);
    }
    // Non-string value: read until the next structural character.
    std::string out;
    for (std::size_t i = start; i < json.size(); ++i) {
        if (json[i] == ',' || json[i] == '}' || json[i] == ']') break;
        out += json[i];
    }
    return out;
}

double ApiClient::json_number(const std::string& json, const std::string& key, double fallback) {
    const std::string raw = json_string(json, key);
    if (raw.empty()) return fallback;
    try {
        return std::stod(raw);
    } catch (const std::exception&) {
        return fallback;
    }
}

bool ApiClient::json_bool(const std::string& json, const std::string& key, bool fallback) {
    const std::string raw = json_string(json, key);
    if (raw == "true") return true;
    if (raw == "false") return false;
    return fallback;
}

std::string ApiClient::json_nested(const std::string& json, const std::string& outer_key,
                                   const std::string& key) {
    const std::size_t start = find_value(json, outer_key);
    if (start == std::string::npos) return {};
    // Only search inside the outer object, not the whole document.
    int depth = 0;
    for (std::size_t i = start; i < json.size(); ++i) {
        if (json[i] == '{') ++depth;
        if (json[i] == '}') {
            --depth;
            if (depth <= 0) return {};
        }
        if (depth == 1) {
            const std::string slice = json.substr(i);
            const std::string value = json_string(slice, key);
            if (!value.empty()) return value;
        }
    }
    return {};
}

VerificationResult ApiClient::verify(const std::string& barcode, const std::string& barcode_type,
                                     double detection_ms, double processing_ms,
                                     const std::string& device_name, double confidence) {
    VerificationResult result;
    result.barcode = barcode;
    result.barcode_type = barcode_type;
    result.detection_ms = detection_ms;
    const auto started = std::chrono::steady_clock::now();

    // --- build the request body -------------------------------------------
    std::ostringstream body;
    body << '{'
         << "\"barcode\":\"" << http::HttpClient::json_escape(barcode) << "\","
         << "\"barcode_type\":\"" << http::HttpClient::json_escape(barcode_type) << "\","
         << "\"timestamp\":\"" << TimeUtil::now_iso8601() << "\","
         << "\"device_name\":\"" << http::HttpClient::json_escape(device_name) << "\","
         << "\"source\":\"cpp\","
         << "\"detection_time_ms\":" << detection_ms << ","
         << "\"processing_time_ms\":" << processing_ms;
    if (confidence >= 0.0) body << ",\"confidence\":" << confidence;
    body << '}';

    const std::string path = config_.base_path + "/scans";
    http::Response response;
    std::string error;
    for (int attempt = 0; attempt <= config_.retries; ++attempt) {
        response = client_->post_json(path, body.str());
        if (response.transport_ok()) break;
        error = response.error;
        if (attempt < config_.retries) {
            std::this_thread::sleep_for(
                std::chrono::milliseconds(config_.retry_backoff_ms * (attempt + 1)));
        }
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        ++stats_.requests;
        stats_.last_latency_ms = response.duration_ms;
        // Exponential moving average keeps the overlay readable.
        stats_.avg_latency_ms = stats_.avg_latency_ms == 0.0
                                    ? response.duration_ms
                                    : stats_.avg_latency_ms * 0.8 + response.duration_ms * 0.2;
        stats_.reachable = response.transport_ok();
    }

    result.total_ms =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();

    if (!response.transport_ok()) {
        SIDB_LOG(log::Level::Error, kComponent,
                 "API unreachable (" + error + "); the scan will be queued locally");
        if (offline_queue_ != nullptr) {
            result.queued_offline = offline_queue_->enqueue(
                barcode, barcode_type, device_name, detection_ms, processing_ms);
            std::lock_guard<std::mutex> lock(mutex_);
            if (result.queued_offline) ++stats_.queued;
        }
        result.result = "ERROR";
        result.error_code = "API_UNAVAILABLE";
        result.message = "The local service is unavailable. The scan was saved and will be synced.";
        return result;
    }

    if (!response.status_ok()) {
        // The service answered with an error envelope; surface its code.
        result.result = json_string(response.body, "code");
        if (result.result.empty()) result.result = "ERROR";
        result.error_code = result.result;
        result.message = json_string(response.body, "message");
        if (result.message.empty()) {
            result.message = "The scan could not be processed (HTTP " +
                             std::to_string(response.status) + ").";
        }
        SIDB_LOG(log::Level::Error, kComponent,
                 "API returned HTTP " + std::to_string(response.status) + ": " + result.error_code);
        return result;
    }

    // --- parse the verification envelope ----------------------------------
    result.transport_ok = true;
    result.result = json_string(response.body, "result");
    result.message = json_string(response.body, "message");
    result.error_code = json_string(response.body, "error_code");
    result.success = json_bool(response.body, "success", false) || result.result == "VERIFIED";
    result.can_transact = json_bool(response.body, "can_transact", false);
    result.duplicate = json_bool(response.body, "duplicate", false);
    result.barcode_type = json_string(response.body, "barcode_type");
    if (result.barcode_type.empty()) result.barcode_type = barcode_type;
    result.database_ms = json_number(response.body, "database_ms", 0.0);
    result.detection_ms = result.detection_ms > 0.0 ? result.detection_ms : detection_ms;
    result.total_ms = std::max(result.total_ms, result.detection_ms + result.database_ms);
    result.scan_id = std::strtoll(json_string(response.body, "scan_id").c_str(), nullptr, 10);

    // The student block is nested; use the timing block position to anchor it.
    const std::string student_block = [&]() {
        const std::size_t timing = response.body.find("\"timing\"");
        const std::size_t student = response.body.find("\"student\"");
        if (student == std::string::npos) return response.body;
        if (timing != std::string::npos && timing < student) {
            return response.body.substr(student);
        }
        return response.body;
    }();
    result.student_id = json_nested(student_block, "student", "student_id");
    result.student_name = json_nested(student_block, "student", "name");
    result.course = json_nested(student_block, "student", "course");
    result.year_level = json_nested(student_block, "student", "year_level");
    result.section = json_nested(student_block, "student", "section");
    result.status = json_nested(student_block, "student", "status");

    if (result.result == "DUPLICATE_SCAN") {
        result.success = false;
        result.can_transact = false;
    }
    if (result.result == "VERIFIED" && result.student_id.empty()) {
        SIDB_LOG(log::Level::Error, kComponent,
                 "malformed verification response: VERIFIED without a student block");
        result.result = "ERROR";
        result.error_code = "UNKNOWN_ERROR";
        result.message = "The local service returned an unexpected response.";
        result.success = false;
    }
    return result;
}

bool ApiClient::publish_frame(const std::string& jpeg_bytes, const std::string& detections_json,
                              const std::string& error_code, const std::string& error_message) {
    if (jpeg_bytes.empty()) return false;
    std::vector<http::MultipartField> fields;
    fields.push_back({"image", {}, true, "frame.jpg", "image/jpeg", jpeg_bytes});
    fields.push_back({"source", "cpp", false, {}, {}, {}});
    fields.push_back({"detections", detections_json, false, {}, {}, {}});
    if (!error_code.empty()) fields.push_back({"error_code", error_code, false, {}, {}, {}});
    if (!error_message.empty()) {
        fields.push_back({"error_message", error_code.empty() ? error_message : error_code,
                          false, {}, {}, {}});
    }
    const http::Response response =
        client_->post_multipart(config_.base_path + "/stream/frame", fields);
    return response.transport_ok() && response.status_ok();
}

bool ApiClient::healthy(int timeout_ms) {
    const int previous = config_.timeout_ms;
    http::HttpClient probe(config_.host, config_.port, timeout_ms);
    (void)previous;
    const http::Response response = probe.get(config_.base_path + "/health");
    std::lock_guard<std::mutex> lock(mutex_);
    stats_.reachable = response.transport_ok() && response.status_ok();
    return stats_.reachable;
}

std::string ApiClient::last_error() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_.reachable ? std::string() : std::string("API_UNAVAILABLE");
}

ApiClient::Stats ApiClient::stats() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

}  // namespace sidb
