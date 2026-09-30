// ===========================================================================
//  PerformanceMonitor.cpp
// ===========================================================================
#include "scanner/PerformanceMonitor.h"

#include <algorithm>
#include <cmath>
#include <numeric>
#include <sstream>

#include "utils/TimeUtil.h"

namespace sidb {

const char* PerformanceMonitor::kBannerFormat =
    "fps=%.1f  frame=%.1fms  detect=%.1fms  db=%.1fms  total=%.1fms";

double PerformanceMonitor::Series::mean() const {
    if (values.empty()) return 0.0;
    return std::accumulate(values.begin(), values.end(), 0.0) /
           static_cast<double>(values.size());
}

double PerformanceMonitor::Series::max() const {
    if (values.empty()) return 0.0;
    return *std::max_element(values.begin(), values.end());
}

double PerformanceMonitor::Series::percentile(double fraction) const {
    if (values.empty()) return 0.0;
    std::vector<double> sorted(values.begin(), values.end());
    std::sort(sorted.begin(), sorted.end());
    const auto index = static_cast<std::size_t>(
        std::min<double>(sorted.size() - 1, std::ceil(fraction * sorted.size()) - 1));
    return sorted[std::max<std::size_t>(0, index)];
}

PerformanceMonitor::PerformanceMonitor(std::size_t window) : window_(window) {
    start();
}

void PerformanceMonitor::start() {
    std::lock_guard<std::mutex> lock(mutex_);
    started_at_ms_ = TimeUtil::monotonic_ms();
    last_capture_ms_ = 0.0;
}

void PerformanceMonitor::reset() {
    std::lock_guard<std::mutex> lock(mutex_);
    *this = PerformanceMonitor(window_);
}

void PerformanceMonitor::record_capture() {
    std::lock_guard<std::mutex> lock(mutex_);
    const double now = TimeUtil::monotonic_ms();
    if (last_capture_ms_ > 0.0) {
        const double interval = now - last_capture_ms_;
        if (interval > 0.0) fps_.add(1000.0 / interval, window_);
    }
    last_capture_ms_ = now;
    ++frames_captured_;
}

void PerformanceMonitor::record_skip() {
    std::lock_guard<std::mutex> lock(mutex_);
    ++frames_skipped_;
}

void PerformanceMonitor::record_capture_failure() {
    std::lock_guard<std::mutex> lock(mutex_);
    ++capture_failures_;
}

void PerformanceMonitor::record_decode_failure() {
    std::lock_guard<std::mutex> lock(mutex_);
    ++decode_failures_;
}

void PerformanceMonitor::record_processing(double processing_ms, double preprocess_ms,
                                           double detect_ms, double decode_ms, double database_ms,
                                           double total_ms) {
    std::lock_guard<std::mutex> lock(mutex_);
    ++frames_processed_;
    processing_.add(processing_ms, window_);
    preprocess_.add(preprocess_ms, window_);
    detect_.add(detect_ms, window_);
    decode_.add(decode_ms, window_);
    database_.add(database_ms, window_);
    total_.add(total_ms, window_);
}

void PerformanceMonitor::record_scan(const std::string& result) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (result == "VERIFIED") ++scans_verified_;
    else if (result == "UNKNOWN_ID") ++scans_unknown_;
    else if (result == "DUPLICATE_SCAN") ++scans_duplicate_;
    else if (result == "UNSUPPORTED_FORMAT") ++scans_unsupported_;
    else ++scans_invalid_;
}

void PerformanceMonitor::record_api(bool success, double latency_ms, bool queued) {
    std::lock_guard<std::mutex> lock(mutex_);
    ++api_requests_;
    if (!success) ++api_failures_;
    if (queued) ++scans_queued_;
    if (!success) api_reachable_ = false;
    else api_reachable_ = true;
    (void)latency_ms;
}

PerformanceMonitor::Snapshot PerformanceMonitor::snapshot() const {
    std::lock_guard<std::mutex> lock(mutex_);
    Snapshot snapshot;
    snapshot.frames_captured = frames_captured_;
    snapshot.frames_processed = frames_processed_;
    snapshot.frames_skipped = frames_skipped_;
    snapshot.capture_failures = capture_failures_;
    snapshot.decode_failures = decode_failures_;
    snapshot.uptime_s = (TimeUtil::monotonic_ms() - started_at_ms_) / 1000.0;
    snapshot.fps = fps_.mean();
    snapshot.avg_processing_ms = processing_.mean();
    snapshot.p95_processing_ms = processing_.percentile(0.95);
    snapshot.max_processing_ms = processing_.max();
    snapshot.avg_preprocess_ms = preprocess_.mean();
    snapshot.avg_detect_ms = detect_.mean();
    snapshot.avg_decode_ms = decode_.mean();
    snapshot.avg_database_ms = database_.mean();
    snapshot.avg_total_ms = total_.mean();
    snapshot.scans_verified = scans_verified_;
    snapshot.scans_unknown = scans_unknown_;
    snapshot.scans_invalid = scans_invalid_;
    snapshot.scans_duplicate = scans_duplicate_;
    snapshot.scans_unsupported = scans_unsupported_;
    snapshot.scans_queued = scans_queued_;
    snapshot.api_requests = api_requests_;
    snapshot.api_failures = api_failures_;
    snapshot.api_reachable = api_reachable_;
    return snapshot;
}

std::string PerformanceMonitor::one_line() const {
    const Snapshot s = snapshot();
    char buffer[192];
    std::snprintf(buffer, sizeof(buffer), kBannerFormat, s.fps, s.avg_processing_ms,
                  s.avg_detect_ms, s.avg_database_ms, s.avg_total_ms);
    return buffer;
}

std::string PerformanceMonitor::report() const {
    const Snapshot s = snapshot();
    std::ostringstream out;
    out << "Performance Benchmark\n"
        << "  uptime                 : " << static_cast<int>(s.uptime_s) << " s\n"
        << "  frames captured        : " << s.frames_captured << '\n'
        << "  frames processed       : " << s.frames_processed << '\n'
        << "  frames skipped         : " << s.frames_skipped << '\n'
        << "  capture failures       : " << s.capture_failures << '\n'
        << "  decode failures        : " << s.decode_failures << '\n'
        << "  average FPS            : " << s.fps << '\n'
        << "  average frame time     : " << s.avg_processing_ms << " ms\n"
        << "  p95 frame time         : " << s.p95_processing_ms << " ms\n"
        << "  max frame time         : " << s.max_processing_ms << " ms\n"
        << "  average preprocess     : " << s.avg_preprocess_ms << " ms\n"
        << "  average detection      : " << s.avg_detect_ms << " ms\n"
        << "  average decode         : " << s.avg_decode_ms << " ms\n"
        << "  average database       : " << s.avg_database_ms << " ms\n"
        << "  average total response : " << s.avg_total_ms << " ms\n"
        << "  scans verified         : " << s.scans_verified << '\n'
        << "  scans unknown          : " << s.scans_unknown << '\n'
        << "  scans invalid          : " << s.scans_invalid << '\n'
        << "  scans duplicate        : " << s.scans_duplicate << '\n'
        << "  scans unsupported      : " << s.scans_unsupported << '\n'
        << "  scans queued offline   : " << s.scans_queued << '\n'
        << "  API requests / failures: " << s.api_requests << " / " << s.api_failures << '\n'
        << "  API reachable          : " << (s.api_reachable ? "yes" : "no") << '\n';
    return out.str();
}

}  // namespace sidb
