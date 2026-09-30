// ===========================================================================
//  TimeUtil.cpp
// ===========================================================================
#include "utils/TimeUtil.h"

#include <cmath>
#include <cstdio>
#include <ctime>

namespace sidb {

namespace {

/// Broken-down UTC time with milliseconds.
struct Stamp {
    std::tm utc{};
    int milliseconds = 0;
};

Stamp now_stamp() {
    const auto since_epoch = std::chrono::system_clock::now().time_since_epoch();
    const auto seconds = std::chrono::duration_cast<std::chrono::seconds>(since_epoch);
    const auto milliseconds =
        std::chrono::duration_cast<std::chrono::milliseconds>(since_epoch - seconds).count();
    const std::time_t raw = static_cast<std::time_t>(seconds.count());
    Stamp stamp;
    ::gmtime_r(&raw, &stamp.utc);
    stamp.milliseconds = static_cast<int>(milliseconds);
    return stamp;
}

}  // namespace

std::string TimeUtil::now_iso8601() {
    const Stamp stamp = now_stamp();
    char buffer[40];
    std::snprintf(buffer, sizeof(buffer), "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
                  stamp.utc.tm_year + 1900, stamp.utc.tm_mon + 1, stamp.utc.tm_mday,
                  stamp.utc.tm_hour, stamp.utc.tm_min, stamp.utc.tm_sec, stamp.milliseconds);
    return buffer;
}

std::string TimeUtil::now_log_stamp() {
    const Stamp stamp = now_stamp();
    char buffer[40];
    std::snprintf(buffer, sizeof(buffer), "%04d-%02d-%02d %02d:%02d:%02d.%03d",
                  stamp.utc.tm_year + 1900, stamp.utc.tm_mon + 1, stamp.utc.tm_mday,
                  stamp.utc.tm_hour, stamp.utc.tm_min, stamp.utc.tm_sec, stamp.milliseconds);
    return buffer;
}

std::string TimeUtil::now_time_of_day() {
    const Stamp stamp = now_stamp();
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "%02d:%02d:%02d", stamp.utc.tm_hour, stamp.utc.tm_min,
                  stamp.utc.tm_sec);
    return buffer;
}

std::string TimeUtil::now_date() {
    const Stamp stamp = now_stamp();
    char buffer[16];
    std::snprintf(buffer, sizeof(buffer), "%04d-%02d-%02d", stamp.utc.tm_year + 1900,
                  stamp.utc.tm_mon + 1, stamp.utc.tm_mday);
    return buffer;
}

double TimeUtil::monotonic_ms() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double, std::milli>(clock::now().time_since_epoch()).count();
}

double TimeUtil::wall_ms() {
    using clock = std::chrono::system_clock;
    return std::chrono::duration<double, std::milli>(clock::now().time_since_epoch()).count();
}

std::string TimeUtil::format_duration(double milliseconds) {
    char buffer[32];
    if (milliseconds < 1000.0) {
        std::snprintf(buffer, sizeof(buffer), "%.0f ms", milliseconds);
        return buffer;
    }
    if (milliseconds < 60000.0) {
        std::snprintf(buffer, sizeof(buffer), "%.2f s", milliseconds / 1000.0);
        return buffer;
    }
    const int total_seconds = static_cast<int>(milliseconds / 1000.0);
    std::snprintf(buffer, sizeof(buffer), "%dm %02ds", total_seconds / 60, total_seconds % 60);
    return buffer;
}

}  // namespace sidb
