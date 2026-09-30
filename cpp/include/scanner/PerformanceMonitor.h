// ===========================================================================
//  PerformanceMonitor.h - rolling metrics for the overlay and the benchmark
//
//  Collects FPS, frame-processing time, detection time, database time and scan
//  counts over a bounded window, and produces the machine-readable snapshot the
//  C++ scanner prints at exit and `tests/performance/` records.  Every number it
//  reports was measured on the machine that produced it.
// ===========================================================================
#ifndef SIDB_SCANNER_PERFORMANCE_MONITOR_H
#define SIDB_SCANNER_PERFORMANCE_MONITOR_H

#include <deque>
#include <mutex>
#include <string>
#include <vector>

#include "scanner/ScannerState.h"

namespace sidb {

class PerformanceMonitor {
public:
    struct Snapshot {
        // capture
        long long frames_captured = 0;
        long long frames_processed = 0;
        long long frames_skipped = 0;
        long long capture_failures = 0;
        long long decode_failures = 0;
        double uptime_s = 0.0;
        double fps = 0.0;
        // processing (ms)
        double avg_processing_ms = 0.0;
        double p95_processing_ms = 0.0;
        double max_processing_ms = 0.0;
        double avg_preprocess_ms = 0.0;
        double avg_detect_ms = 0.0;
        double avg_decode_ms = 0.0;
        double avg_database_ms = 0.0;
        double avg_total_ms = 0.0;   ///< detection + database, the cashier latency
        // scans
        long long scans_verified = 0;
        long long scans_unknown = 0;
        long long scans_invalid = 0;
        long long scans_duplicate = 0;
        long long scans_unsupported = 0;
        long long scans_queued = 0;
        long long api_requests = 0;
        long long api_failures = 0;
        bool api_reachable = true;
    };

    explicit PerformanceMonitor(std::size_t window = 120);

    void start();
    void reset();

    void record_capture();
    void record_skip();
    void record_capture_failure();
    void record_decode_failure();
    void record_processing(double processing_ms, double preprocess_ms, double detect_ms,
                           double decode_ms, double database_ms, double total_ms);
    void record_scan(const std::string& result);
    void record_api(bool success, double latency_ms, bool queued);

    Snapshot snapshot() const;
    /// Human-readable multi-line report (printed by `--benchmark` and at exit).
    std::string report() const;
    /// Machine-readable single line, e.g. "fps=29.8 processing=18.2ms db=1.4ms".
    std::string one_line() const;

    static const char* kBannerFormat;

private:
    struct Series {
        std::deque<double> values;
        void add(double value, std::size_t window) {
            values.push_back(value);
            while (values.size() > window) values.pop_front();
        }
        double mean() const;
        double max() const;
        double percentile(double fraction) const;
        double last() const { return values.empty() ? 0.0 : values.back(); }
    };

    mutable std::mutex mutex_;
    std::size_t window_;
    double started_at_ms_ = 0.0;
    double last_capture_ms_ = 0.0;
    long long frames_captured_ = 0;
    long long frames_processed_ = 0;
    long long frames_skipped_ = 0;
    long long capture_failures_ = 0;
    long long decode_failures_ = 0;
    long long scans_verified_ = 0;
    long long scans_unknown_ = 0;
    long long scans_invalid_ = 0;
    long long scans_duplicate_ = 0;
    long long scans_unsupported_ = 0;
    long long scans_queued_ = 0;
    long long api_requests_ = 0;
    long long api_failures_ = 0;
    bool api_reachable_ = true;
    Series fps_, processing_, preprocess_, detect_, decode_, database_, total_;
};

}  // namespace sidb

#endif  // SIDB_SCANNER_PERFORMANCE_MONITOR_H
