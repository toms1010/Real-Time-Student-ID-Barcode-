// ===========================================================================
//  ApiClient.h - the C++ -> Python contract
//
//  The scanner knows nothing about SQL or about the students table: it posts a
//  decoded payload to `POST /api/scans` and renders whatever comes back
//  (ADR-001, ADR-004).  This class is the only place that knows the JSON shape,
//  so a schema change on the Python side is a one-file change here.
//
//  Responsibilities
//  ----------------
//  * verify a payload (and let the service apply the cooldown policy)
//  * fall back to a local SQLite queue when the API is unreachable, so a scan
//    during a service restart is not lost (DatabaseClient)
//  * report a tiny subset of the response: result code, student name, timings
//  * expose a health probe for the status overlay
//
//  A deliberately tiny JSON reader is included: the response shape is fixed and
//  known, so a full JSON library would be a dependency without a purpose.
// ===========================================================================
#ifndef SIDB_API_API_CLIENT_H
#define SIDB_API_API_CLIENT_H

#include <chrono>
#include <memory>
#include <mutex>
#include <string>

#include "api/HttpClient.h"
#include "utils/Config.h"

namespace sidb {

class DatabaseClient;

/// Result of verifying one payload.
struct VerificationResult {
    bool transport_ok = false;      ///< the API answered at all
    bool success = false;           ///< result == VERIFIED
    std::string result;             ///< VERIFIED | UNKNOWN_ID | ...
    std::string message;            ///< operator-facing text
    std::string error_code;
    std::string barcode;
    std::string barcode_type;
    std::string student_id;
    std::string student_name;
    std::string course;
    std::string year_level;
    std::string section;
    std::string status;             ///< student status
    bool can_transact = false;
    bool duplicate = false;
    long long scan_id = 0;
    double detection_ms = 0.0;
    double database_ms = 0.0;
    double total_ms = 0.0;
    bool queued_offline = false;    ///< stored locally because the API was down
};

class ApiClient {
public:
    explicit ApiClient(const ApiConfig& config);
    ~ApiClient();

    ApiClient(const ApiClient&) = delete;
    ApiClient& operator=(const ApiClient&) = delete;

    /// Attach the offline queue (optional; the engine does this at start-up).
    void set_offline_queue(DatabaseClient* queue) { offline_queue_ = queue; }

    /// Verify a decoded payload.  Never throws and never blocks longer than
    /// `api.timeout_ms` * (retries + 1).
    VerificationResult verify(const std::string& barcode, const std::string& barcode_type,
                              double detection_ms, double processing_ms,
                              const std::string& device_name, double confidence = -1.0);

    /// Publish an annotated JPEG so the browser dashboard mirrors this scanner.
    bool publish_frame(const std::string& jpeg_bytes, const std::string& detections_json,
                       const std::string& error_code = "", const std::string& error_message = "");

    /// `GET /api/health` - used by the status bar and by start-up.
    bool healthy(int timeout_ms = 800);

    /// Last transport error, empty when the last call succeeded.
    std::string last_error() const;

    /// Statistics for the overlay.
    struct Stats {
        long long requests = 0;
        long long failures = 0;
        long long queued = 0;
        double last_latency_ms = 0.0;
        double avg_latency_ms = 0.0;
        bool reachable = false;
    };
    Stats stats() const;

    const std::string& base_url() const { return base_url_; }

    // --- JSON helpers (public so the tests can exercise them directly) ----
    /// Extract a string field: `"key": "value"` -> value (unescaped).
    static std::string json_string(const std::string& json, const std::string& key);
    /// Extract a number field, or `fallback` when absent.
    static double json_number(const std::string& json, const std::string& key,
                              double fallback = 0.0);
    static bool json_bool(const std::string& json, const std::string& key, bool fallback = false);
    /// Find the value of `key` inside the object that contains `"outer_key"`.
    static std::string json_nested(const std::string& json, const std::string& outer_key,
                                   const std::string& key);

private:
    ApiConfig config_;
    std::string base_url_;
    std::unique_ptr<http::HttpClient> client_;
    DatabaseClient* offline_queue_ = nullptr;
    mutable std::mutex mutex_;
    Stats stats_;
};

}  // namespace sidb

#endif  // SIDB_API_API_CLIENT_H
