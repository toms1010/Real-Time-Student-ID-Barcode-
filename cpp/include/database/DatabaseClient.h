// ===========================================================================
//  DatabaseClient.h - local offline scan queue (SQLite)
//
//  The scanner deliberately does **not** query the application database
//  (ADR-004): verification is the Python service's job.  This component exists
//  for one narrow, genuinely useful purpose - durability when that service is
//  not reachable.
//
//  Scenario: the cashier keeps scanning during a service restart or a network
//  hiccup.  Without a queue those scans vanish.  With it, every payload is
//  appended to a small local SQLite file and flushed to `POST /api/scans` once
//  the service returns, so the scan history stays complete.
//
//  Properties:
//  * one table, no schema coupling - it is a queue, not a replica
//  * survives a crash (committed per insert)
//  * `--flush-queue` replays it; duplicates are harmless because the service
//    applies the same cooldown/duplicate policy
// ===========================================================================
#ifndef SIDB_DATABASE_DATABASE_CLIENT_H
#define SIDB_DATABASE_DATABASE_CLIENT_H

#include <mutex>
#include <string>
#include <vector>

struct sqlite3;

namespace sidb {

class DatabaseClient {
public:
    struct PendingScan {
        long long id = 0;
        std::string barcode_value;
        std::string barcode_type;
        std::string device_name;
        double detection_ms = 0.0;
        double processing_ms = 0.0;
        int attempts = 0;
        std::string queued_at;
    };

    DatabaseClient() = default;
    ~DatabaseClient();

    DatabaseClient(const DatabaseClient&) = delete;
    DatabaseClient& operator=(const DatabaseClient&) = delete;

    /// Open (creating if needed) the queue database.  Returns false and sets
    /// `error()` when the file cannot be opened - the scanner then runs without
    /// offline durability instead of failing to start.
    bool open(const std::string& path, int busy_timeout_ms = 3000);

    void close();
    bool is_open() const { return handle_ != nullptr; }
    const std::string& error() const { return error_; }
    const std::string& path() const { return path_; }

    /// Append a scan.  Returns false when the write failed.
    bool enqueue(const std::string& barcode_value, const std::string& barcode_type,
                 const std::string& device_name, double detection_ms, double processing_ms);

    /// Oldest-first page of pending scans.
    std::vector<PendingScan> pending(int limit = 100) const;

    /// Remove a scan that was successfully replayed.
    bool remove(long long id);

    /// Number of scans still waiting.
    long long pending_count() const;

    /// Delete rows older than `days` so the file cannot grow without bound.
    int purge_older_than(int days);

private:
    sqlite3* handle_ = nullptr;
    std::string path_;
    std::string error_;
    mutable std::mutex mutex_;
};

}  // namespace sidb

#endif  // SIDB_DATABASE_DATABASE_CLIENT_H
