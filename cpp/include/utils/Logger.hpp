// ===========================================================================
//  Logger.h - minimal, dependency-free structured logging for the C++ scanner
//
//  Design notes
//  ------------
//  * Header-only so every translation unit can log without a link-order
//    dependency on Logger.cpp.
//  * Thread safe: the scanner runs capture, decode and API threads.
//  * Mirrors the Python logger's text format so both halves of the system
//    produce comparable lines:
//        2026-09-30 09:42:17.123 INFO  [capture] Camera initialized
//  * Secrets are redacted: any key matching password|token|secret|authorization
//    is replaced before formatting (defence in depth).
// ===========================================================================
#ifndef SIDB_UTILS_LOGGER_H
#define SIDB_UTILS_LOGGER_H

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <map>
#include <mutex>
#include <sstream>
#include <string>
#include <string_view>
#include <thread>

namespace sidb::log {

enum class Level { Trace = 0, Debug, Info, Warning, Error, Critical, Off };

inline const char* level_name(Level level) {
    switch (level) {
        case Level::Trace:    return "TRACE";
        case Level::Debug:    return "DEBUG";
        case Level::Info:     return "INFO";
        case Level::Warning:  return "WARN";
        case Level::Error:    return "ERROR";
        case Level::Critical: return "CRIT";
        case Level::Off:      return "OFF";
    }
    return "?";
}

inline bool parse_level(std::string_view text, Level& out) {
    if (text == "TRACE" || text == "trace") { out = Level::Trace; return true; }
    if (text == "DEBUG" || text == "debug") { out = Level::Debug; return true; }
    if (text == "INFO"  || text == "info")  { out = Level::Info; return true; }
    if (text == "WARN"  || text == "warning" || text == "WARNING") { out = Level::Warning; return true; }
    if (text == "ERROR" || text == "error") { out = Level::Error; return true; }
    if (text == "CRIT"  || text == "critical") { out = Level::Critical; return true; }
    if (text == "OFF"   || text == "off")   { out = Level::Off; return true; }
    return false;
}

inline std::string redact(std::string_view text) {
    // Replaces the value that follows a sensitive key.  Deliberately simple and
    // cheap: this runs on the logging path, so it must not allocate much.
    static const char* kKeys[] = {"password", "token", "secret", "authorization", "cookie"};
    std::string out(text);
    for (const char* key : kKeys) {
        std::string needle;
        needle.push_back(key[0]);
        for (std::size_t i = 1; key[i] != '\0'; ++i) {
            needle.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(key[i]))));
            needle.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(key[i]))));
        }
        std::string lowered = out;
        for (char& c : lowered) {
            c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        }
        std::size_t pos = lowered.find(needle);
        while (pos != std::string::npos) {
            std::size_t value_start = pos + needle.size();
            while (value_start < out.size() && (out[value_start] == '=' || out[value_start] == ':' ||
                                                out[value_start] == ' ')) {
                ++value_start;
            }
            std::size_t value_end = out.find_first_of("&,; \n", value_start);
            if (value_end == std::string::npos) value_end = out.size();
            if (value_end > value_start) out.replace(value_start, value_end - value_start, "***");
            pos = lowered.find(needle, value_end);
        }
    }
    return out;
}

class Logger {
public:
    static Logger& instance() {
        static Logger logger;
        return logger;
    }

    void set_level(Level level) { level_ = level; }
    Level level() const { return level_; }
    bool enabled(Level level) const { return static_cast<int>(level) >= static_cast<int>(level_); }

    void set_thread_name(std::string name) {
        std::lock_guard<std::mutex> lock(mutex_);
        thread_names_[std::this_thread::get_id()] = std::move(name);
    }

    std::string thread_name() {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = thread_names_.find(std::this_thread::get_id());
        return it == thread_names_.end() ? std::string("main") : it->second;
    }

    void log(Level level, const char* component, std::string_view message) {
        if (!enabled(level)) return;
        const auto now = std::chrono::system_clock::now();
        const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                            now.time_since_epoch()) % 1000;
        const std::time_t seconds = std::chrono::system_clock::to_time_t(now);
        std::tm tm{};
        ::localtime_r(&seconds, &tm);

        std::ostringstream line;
        line << std::put_time(&tm, "%Y-%m-%d %H:%M:%S") << '.' << std::setfill('0') << std::setw(3)
             << ms.count() << ' ' << level_name(level) << ' ' << thread_name() << " [" << component
             << "] " << redact(message);

        std::lock_guard<std::mutex> lock(mutex_);
        std::fputs(line.str().c_str(), stdout);
        std::fputc('\n', stdout);
        std::fflush(stdout);
        if (file_.is_open()) {
            file_ << line.str() << '\n';
            file_.flush();
        }
    }

    void open_file(const std::string& path) {
        std::lock_guard<std::mutex> lock(mutex_);
        file_.open(path, std::ios::app);
    }

    void close_file() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (file_.is_open()) file_.close();
    }

private:
    Logger() = default;
    Level level_ = Level::Info;
    std::mutex mutex_;
    std::ofstream file_;
    std::map<std::thread::id, std::string> thread_names_;
};

}  // namespace sidb::log

// Convenience macros: the component name is the enclosing file's short name.
#define SIDB_LOG(level, component, message) \
    ::sidb::log::Logger::instance().log(level, component, message)

#endif  // SIDB_UTILS_LOGGER_H
