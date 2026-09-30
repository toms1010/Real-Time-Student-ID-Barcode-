// ===========================================================================
//  Logger.cpp - translation unit for the logging helpers
//
//  The Logger itself is header-only (so every translation unit can log without
//  link-order surprises); this file holds the pieces that belong to exactly one
//  place: the start-up banner and the log-file teardown.
// ===========================================================================
#include "utils/Logger.hpp"

#include <string>

#include "utils/TimeUtil.h"

namespace sidb::log {

void print_banner(const std::string& application, const std::string& version,
                  const std::string& environment) {
    const std::string rule(72, '=');
    std::fprintf(stdout, "\n%s\n  %s  v%s\n  environment: %s\n  started:    %s\n%s\n",
                 rule.c_str(), application.c_str(), version.c_str(), environment.c_str(),
                 TimeUtil::now_log_stamp().c_str(), rule.c_str());
    std::fflush(stdout);
}

/// Flush and close the log file.  Called at exit so a crash still leaves a
/// complete file on disk.
void shutdown_logger() { Logger::instance().close_file(); }

}  // namespace sidb::log
