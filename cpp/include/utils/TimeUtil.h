// ===========================================================================
//  TimeUtil.h - UTC timestamps in the project's storage format
//
//  Every timestamp written by the system is ISO-8601 in UTC with millisecond
//  precision, matching database/schema.sql's
//  `strftime('%Y-%m-%dT%H:%M:%fZ','now')` default and the Python
//  `app.utils.timeutil` helpers, so reports sort correctly across languages.
// ===========================================================================
#ifndef SIDB_UTILS_TIME_UTIL_H
#define SIDB_UTILS_TIME_UTIL_H

#include <chrono>
#include <string>

namespace sidb {

class TimeUtil {
public:
    /// "2026-09-30T09:42:17.412Z"
    static std::string now_iso8601();

    /// Local-time banner stamp, e.g. "2026-09-30 09:42:17.412".
    static std::string now_log_stamp();

    /// Monotonic milliseconds, for duration measurement.
    static double monotonic_ms();

    /// Wall-clock milliseconds since the epoch, for absolute frame timestamps.
    static double wall_ms();

    /// "09:42:17" - the time part of a log line.
    static std::string now_time_of_day();

    /// "2026-09-30" - the date part, used for the on-screen date.
    static std::string now_date();

    /// Human-friendly duration: "412 ms", "1.24 s", "2m 03s".
    static std::string format_duration(double milliseconds);
};

}  // namespace sidb

#endif  // SIDB_UTILS_TIME_UTIL_H
