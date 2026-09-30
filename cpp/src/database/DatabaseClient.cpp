// ===========================================================================
//  DatabaseClient.cpp
// ===========================================================================
#include "database/DatabaseClient.h"

#include <sqlite3.h>

#include "utils/Logger.hpp"
#include "utils/TimeUtil.h"

namespace sidb {

namespace {

constexpr const char* kComponent = "dbqueue";

const char* const kSchema = R"SQL(
CREATE TABLE IF NOT EXISTS pending_scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode_value   TEXT    NOT NULL,
    barcode_type    TEXT,
    device_name     TEXT,
    detection_ms    REAL,
    processing_ms   REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    queued_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_queued ON pending_scans (queued_at);
)SQL";

/// RAII statement wrapper: sqlite3_stmt_finalize on every path.
class Statement {
public:
    Statement(sqlite3* handle, const char* sql) : handle_(handle) {
        if (handle_ != nullptr) {
            rc_ = sqlite3_prepare_v2(handle_, sql, -1, &statement_, nullptr);
        }
    }
    ~Statement() {
        if (statement_ != nullptr) sqlite3_finalize(statement_);
    }
    Statement(const Statement&) = delete;
    Statement& operator=(const Statement&) = delete;

    bool ok() const { return rc_ == SQLITE_OK && statement_ != nullptr; }
    sqlite3_stmt* get() { return statement_; }

    void bind(int index, const std::string& value) {
        sqlite3_bind_text(statement_, index, value.c_str(), static_cast<int>(value.size()),
                          SQLITE_TRANSIENT);
    }
    void bind(int index, double value) { sqlite3_bind_double(statement_, index, value); }
    void bind(int index, int value) { sqlite3_bind_int(statement_, index, value); }

    bool step() { return sqlite3_step(statement_) == SQLITE_DONE; }

    std::string column_text(int index) const {
        const unsigned char* text = sqlite3_column_text(statement_, index);
        return text != nullptr ? reinterpret_cast<const char*>(text) : std::string();
    }
    double column_double(int index) const { return sqlite3_column_double(statement_, index); }
    long long column_int(int index) const { return sqlite3_column_int64(statement_, index); }

private:
    sqlite3* handle_ = nullptr;
    sqlite3_stmt* statement_ = nullptr;
    int rc_ = SQLITE_ERROR;
};

}  // namespace

DatabaseClient::~DatabaseClient() { close(); }

bool DatabaseClient::open(const std::string& path, int busy_timeout_ms) {
    std::lock_guard<std::mutex> lock(mutex_);
    close();
    error_.clear();
    path_ = path;

    const int rc = sqlite3_open_v2(path.c_str(), &handle_, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE,
                                   nullptr);
    if (rc != SQLITE_OK) {
        error_ = handle_ != nullptr ? sqlite3_errmsg(handle_) : "sqlite3_open_v2 failed";
        if (handle_ != nullptr) {
            sqlite3_close(handle_);
            handle_ = nullptr;
        }
        SIDB_LOG(log::Level::Error, kComponent, "could not open the offline queue: " + error_);
        return false;
    }
    sqlite3_busy_timeout(handle_, busy_timeout_ms);
    sqlite3_exec(handle_, "PRAGMA journal_mode = WAL;", nullptr, nullptr, nullptr);
    sqlite3_exec(handle_, kSchema, nullptr, nullptr, nullptr);
    SIDB_LOG(log::Level::Info, kComponent, "offline scan queue ready at " + path);
    return true;
}

void DatabaseClient::close() {
    if (handle_ != nullptr) {
        sqlite3_close(handle_);
        handle_ = nullptr;
    }
}

bool DatabaseClient::enqueue(const std::string& barcode_value, const std::string& barcode_type,
                             const std::string& device_name, double detection_ms,
                             double processing_ms) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (handle_ == nullptr) {
        error_ = "the offline queue is not open";
        return false;
    }
    Statement statement(handle_,
                        "INSERT INTO pending_scans (barcode_value, barcode_type, device_name, "
                        "detection_ms, processing_ms, queued_at) VALUES (?, ?, ?, ?, ?, ?)");
    if (!statement.ok()) {
        error_ = sqlite3_errmsg(handle_);
        SIDB_LOG(log::Level::Error, kComponent, "queue insert failed: " + error_);
        return false;
    }
    statement.bind(1, barcode_value);
    statement.bind(2, barcode_type);
    statement.bind(3, device_name);
    statement.bind(4, detection_ms);
    statement.bind(5, processing_ms);
    statement.bind(6, TimeUtil::now_iso8601());
    if (!statement.step()) {
        error_ = sqlite3_errmsg(handle_);
        return false;
    }
    return true;
}

std::vector<DatabaseClient::PendingScan> DatabaseClient::pending(int limit) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::vector<PendingScan> rows;
    if (handle_ == nullptr) return rows;
    Statement statement(handle_,
                        "SELECT id, barcode_value, barcode_type, device_name, detection_ms, "
                        "processing_ms, attempts, queued_at FROM pending_scans ORDER BY id LIMIT ?");
    if (!statement.ok()) return rows;
    statement.bind(1, limit);
    while (sqlite3_step(statement.get()) == SQLITE_ROW) {
        PendingScan scan;
        scan.id = statement.column_int(0);
        scan.barcode_value = statement.column_text(1);
        scan.barcode_type = statement.column_text(2);
        scan.device_name = statement.column_text(3);
        scan.detection_ms = statement.column_double(4);
        scan.processing_ms = statement.column_double(5);
        scan.attempts = static_cast<int>(statement.column_int(6));
        scan.queued_at = statement.column_text(7);
        rows.push_back(std::move(scan));
    }
    return rows;
}

bool DatabaseClient::remove(long long id) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (handle_ == nullptr) return false;
    Statement statement(handle_, "DELETE FROM pending_scans WHERE id = ?");
    if (!statement.ok()) return false;
    statement.bind(1, static_cast<int>(id));
    return statement.step();
}

long long DatabaseClient::pending_count() const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (handle_ == nullptr) return 0;
    Statement statement(handle_, "SELECT COUNT(*) FROM pending_scans");
    if (!statement.ok()) return 0;
    return sqlite3_step(statement.get()) == SQLITE_ROW ? statement.column_int(0) : 0;
}

int DatabaseClient::purge_older_than(int days) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (handle_ == nullptr) return 0;
    Statement statement(handle_, "DELETE FROM pending_scans WHERE queued_at < ?");
    if (!statement.ok()) return 0;
    // Two days of slack beyond `days` so a clock skew cannot purge a fresh row.
    const std::string cutoff = [&] {
        const auto shifted = std::chrono::system_clock::now() -
                             std::chrono::hours(24 * (days + 2));
        const std::time_t raw = std::chrono::system_clock::to_time_t(
            std::chrono::time_point_cast<std::chrono::system_clock::duration>(shifted));
        std::tm utc{};
        ::gmtime_r(&raw, &utc);
        char buffer[32];
        std::snprintf(buffer, sizeof(buffer), "%04d-%02d-%02dT%02d:%02d:%02d.000Z",
                      utc.tm_year + 1900, utc.tm_mon + 1, utc.tm_mday, utc.tm_hour, utc.tm_min,
                      utc.tm_sec);
        return std::string(buffer);
    }();
    statement.bind(1, cutoff);
    statement.step();
    return sqlite3_changes(handle_);
}

}  // namespace sidb
