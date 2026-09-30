// ===========================================================================
//  test_scanner.cpp - state machine, cooldown, HTTP client, API contract, queue
//
//  None of these tests need a camera or a running service, except the ones that
//  explicitly start a local listener (see "http: ..." below) - those bind
//  127.0.0.1 on an ephemeral port, so the suite is self-contained and offline.
// ===========================================================================
#include <atomic>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <set>
#include <string>
#include <utility>
#include <vector>
#include <thread>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include "api/ApiClient.h"
#include "api/HttpClient.h"
#include "database/DatabaseClient.h"
#include "scanner/ScannerEngine.h"
#include "scanner/ScannerState.h"
#include "tests/Testing.h"
#include "utils/Config.h"

namespace {

// ---------------------------------------------------------------------------
// A throwaway HTTP server so the client can be tested without the real service.
// ---------------------------------------------------------------------------
class TinyServer {
public:
    struct Reply {
        int status = 200;
        std::string body = "{}";
        std::string content_type = "application/json";
        int delay_ms = 0;
    };

    TinyServer(Reply reply) : reply_(std::move(reply)) {
        fd_ = ::socket(AF_INET, SOCK_STREAM, 0);
        int one = 1;
        ::setsockopt(fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = ::htonl(INADDR_LOOPBACK);
        address.sin_port = 0;  // ephemeral
        ::bind(fd_, reinterpret_cast<sockaddr*>(&address), sizeof(address));
        ::listen(fd_, 4);
        socklen_t length = sizeof(address);
        ::getsockname(fd_, reinterpret_cast<sockaddr*>(&address), &length);
        port_ = ::ntohs(address.sin_port);
        thread_ = std::thread(&TinyServer::serve, this);
    }

    ~TinyServer() {
        stop_.store(true);
        ::shutdown(fd_, SHUT_RDWR);
        ::close(fd_);
        if (thread_.joinable()) thread_.join();
    }

    int port() const { return port_; }
    const std::string& last_request() const { return last_request_; }
    int request_count() const { return requests_.load(); }

private:
    void serve() {
        while (!stop_.load()) {
            sockaddr_in peer{};
            socklen_t length = sizeof(peer);
            timeval tv{};
            tv.tv_sec = 0;
            tv.tv_usec = 200000;
            setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
            const int client = ::accept(fd_, reinterpret_cast<sockaddr*>(&peer), &length);
            if (client < 0) continue;

            std::string request;
            char buffer[2048];
            ssize_t received = ::recv(client, buffer, sizeof(buffer), 0);
            if (received > 0) {
                request.assign(buffer, static_cast<std::size_t>(received));
                last_request_ = request;
            }
            ++requests_;
            if (reply_.delay_ms > 0) {
                std::this_thread::sleep_for(std::chrono::milliseconds(reply_.delay_ms));
            }
            std::string body = reply_.body;
            std::string response = "HTTP/1.1 " + std::to_string(reply_.status) + " X\r\nContent-Type: " +
                                   reply_.content_type + "\r\nContent-Length: " +
                                   std::to_string(body.size()) + "\r\nConnection: close\r\n\r\n" + body;
            ::send(client, response.data(), response.size(), MSG_NOSIGNAL);
            ::close(client);
        }
    }

    Reply reply_;
    int fd_ = -1;
    int port_ = 0;
    std::thread thread_;
    std::atomic<bool> stop_{false};
    std::atomic<int> requests_{0};
    std::string last_request_;
};

std::filesystem::path temp_path(const std::string& name) {
    return std::filesystem::temp_directory_path() / ("sidb_test_" + name);
}

}  // namespace

// ---------------------------------------------------------------------------
// state machine
//
// The seventeen states of `app/services/scan_workflow.py`. A Python test and
// this one assert the same table; `scripts/check_state_mirror.py` diffs the
// three source files so they cannot drift apart silently.
// ---------------------------------------------------------------------------
namespace {

/// Every canonical state, in enum order.
const std::vector<sidb::ScanState> kAllStates = {
    sidb::ScanState::Idle,              sidb::ScanState::CameraInitializing,
    sidb::ScanState::Scanning,          sidb::ScanState::BarcodeDetected,
    sidb::ScanState::Decoding,          sidb::ScanState::Validating,
    sidb::ScanState::LookingUpStudent,  sidb::ScanState::StudentFound,
    sidb::ScanState::ReadyForTransaction, sidb::ScanState::TransactionProcessing,
    sidb::ScanState::Success,           sidb::ScanState::CameraError,
    sidb::ScanState::BarcodeInvalid,    sidb::ScanState::StudentNotFound,
    sidb::ScanState::DatabaseError,     sidb::ScanState::TransactionError,
    sidb::ScanState::DuplicateScan,
};

/// The transitions the brief spells out, one by one.
const std::vector<std::pair<sidb::ScanState, sidb::ScanState>> kRequiredEdges = {
    {sidb::ScanState::Idle, sidb::ScanState::CameraInitializing},
    {sidb::ScanState::CameraInitializing, sidb::ScanState::Scanning},
    {sidb::ScanState::CameraInitializing, sidb::ScanState::CameraError},
    {sidb::ScanState::Scanning, sidb::ScanState::BarcodeDetected},
    {sidb::ScanState::Scanning, sidb::ScanState::CameraError},
    {sidb::ScanState::BarcodeDetected, sidb::ScanState::Decoding},
    {sidb::ScanState::Decoding, sidb::ScanState::Validating},
    {sidb::ScanState::Decoding, sidb::ScanState::BarcodeInvalid},
    {sidb::ScanState::Validating, sidb::ScanState::LookingUpStudent},
    {sidb::ScanState::Validating, sidb::ScanState::BarcodeInvalid},
    {sidb::ScanState::LookingUpStudent, sidb::ScanState::StudentFound},
    {sidb::ScanState::LookingUpStudent, sidb::ScanState::StudentNotFound},
    {sidb::ScanState::LookingUpStudent, sidb::ScanState::DatabaseError},
    {sidb::ScanState::StudentFound, sidb::ScanState::ReadyForTransaction},
    {sidb::ScanState::ReadyForTransaction, sidb::ScanState::TransactionProcessing},
    {sidb::ScanState::ReadyForTransaction, sidb::ScanState::Idle},
    {sidb::ScanState::ReadyForTransaction, sidb::ScanState::Scanning},
    {sidb::ScanState::TransactionProcessing, sidb::ScanState::Success},
    {sidb::ScanState::TransactionProcessing, sidb::ScanState::TransactionError},
    {sidb::ScanState::TransactionProcessing, sidb::ScanState::DuplicateScan},
    {sidb::ScanState::Success, sidb::ScanState::Idle},
    {sidb::ScanState::CameraError, sidb::ScanState::CameraInitializing},
    {sidb::ScanState::BarcodeInvalid, sidb::ScanState::Scanning},
    {sidb::ScanState::StudentNotFound, sidb::ScanState::Scanning},
    {sidb::ScanState::StudentNotFound, sidb::ScanState::Idle},
    {sidb::ScanState::DatabaseError, sidb::ScanState::LookingUpStudent},
    {sidb::ScanState::DatabaseError, sidb::ScanState::Idle},
    {sidb::ScanState::TransactionError, sidb::ScanState::ReadyForTransaction},
    {sidb::ScanState::TransactionError, sidb::ScanState::Idle},
    {sidb::ScanState::DuplicateScan, sidb::ScanState::Scanning},
};

/// The shortcuts the brief forbids: no state may skip validation, the lookup,
/// or the "processing" step that stands between a form and a success.
const std::vector<std::pair<sidb::ScanState, sidb::ScanState>> kForbiddenEdges = {
    {sidb::ScanState::Scanning, sidb::ScanState::StudentFound},
    {sidb::ScanState::BarcodeDetected, sidb::ScanState::StudentFound},
    {sidb::ScanState::Decoding, sidb::ScanState::ReadyForTransaction},
    {sidb::ScanState::Validating, sidb::ScanState::Success},
    {sidb::ScanState::LookingUpStudent, sidb::ScanState::Success},
    {sidb::ScanState::Idle, sidb::ScanState::Success},
    {sidb::ScanState::ReadyForTransaction, sidb::ScanState::Success},
    // An outage must never look like a student, and a failed charge must
    // never look like a success.
    {sidb::ScanState::DatabaseError, sidb::ScanState::StudentFound},
    {sidb::ScanState::DatabaseError, sidb::ScanState::ReadyForTransaction},
    {sidb::ScanState::TransactionError, sidb::ScanState::Success},
};

}  // namespace

TEST_CASE("state: names match the specification") {
    CHECK_EQ(static_cast<int>(kAllStates.size()),
             static_cast<int>(sidb::kScanStateCount));
    std::set<std::string> names;
    for (const sidb::ScanState state : kAllStates) {
        const std::string name = sidb::to_string(state);
        CHECK(!name.empty());
        names.insert(name);
    }
    // Seventeen distinct canonical names.
    CHECK_EQ(static_cast<int>(names.size()), 17);
    CHECK_EQ(names.count("LOOKING_UP_STUDENT"), static_cast<std::size_t>(1));
    CHECK_EQ(names.count("READY_FOR_TRANSACTION"), static_cast<std::size_t>(1));
    CHECK_EQ(names.count("DUPLICATE_SCAN"), static_cast<std::size_t>(1));
}

TEST_CASE("state: every state documents itself") {
    for (const sidb::ScanState state : kAllStates) {
        CHECK(std::string(sidb::state_message(state)).size() > 0);
        CHECK(std::string(sidb::display_label(state)).size() > 0);
        CHECK(!sidb::state_next(state).empty() || state == sidb::ScanState::Idle ||
              state == sidb::ScanState::CameraError ||
              state == sidb::ScanState::BarcodeInvalid ||
              state == sidb::ScanState::StudentNotFound ||
              state == sidb::ScanState::DatabaseError ||
              state == sidb::ScanState::TransactionError ||
              state == sidb::ScanState::DuplicateScan ||
              state == sidb::ScanState::Success);
        // A busy state accepts no operator action at all.
        if (sidb::state_is_busy(state)) {
            CHECK(sidb::state_actions(state).empty());
        }
        // Every error state offers a way out.
        if (sidb::state_is_error(state)) {
            CHECK(!sidb::state_actions(state).empty());
        }
    }
}

TEST_CASE("state: operator messages never leak internals") {
    const char* forbidden[] = {"select ", "sqlite", "traceback", "/home/", ".cpp"};
    for (const sidb::ScanState state : kAllStates) {
        const std::string message = sidb::state_message(state);
        for (const char* needle : forbidden) {
            CHECK(message.find(needle) == std::string::npos);
        }
    }
}

TEST_CASE("state: the transitions the brief requires are allowed") {
    for (const auto& edge : kRequiredEdges) {
        if (!sidb::StateMachine::is_allowed(edge.first, edge.second)) {
            CHECK_MSG(sidb::StateMachine::is_allowed(edge.first, edge.second),
                          std::string("missing edge ") + sidb::to_string(edge.first) + " -> " +
                              sidb::to_string(edge.second));
        }
    }
}

TEST_CASE("state: the shortcuts the brief forbids are rejected") {
    for (const auto& edge : kForbiddenEdges) {
        CHECK(!sidb::StateMachine::is_allowed(edge.first, edge.second));
    }
}

TEST_CASE("state: no state is a dead end") {
    for (const sidb::ScanState start : kAllStates) {
        std::set<sidb::ScanState> seen{start};
        std::vector<sidb::ScanState> frontier{start};
        while (!frontier.empty()) {
            const sidb::ScanState current = frontier.back();
            frontier.pop_back();
            for (const sidb::ScanState next : sidb::state_next(current)) {
                if (seen.insert(next).second) frontier.push_back(next);
            }
        }
        // The cashier can always get back to the resting state.
        CHECK_EQ(seen.count(sidb::ScanState::Idle), static_cast<std::size_t>(1));
    }
}

TEST_CASE("state: the happy path is allowed end to end") {
    const std::vector<sidb::ScanState> path = {
        sidb::ScanState::CameraInitializing, sidb::ScanState::Scanning,
        sidb::ScanState::BarcodeDetected,    sidb::ScanState::Decoding,
        sidb::ScanState::Validating,         sidb::ScanState::LookingUpStudent,
        sidb::ScanState::StudentFound,       sidb::ScanState::ReadyForTransaction,
        sidb::ScanState::TransactionProcessing, sidb::ScanState::Success,
        sidb::ScanState::Idle};
    sidb::StateMachine machine;
    for (const sidb::ScanState state : path) {
        CHECK_MSG(machine.transition(state, "walk"),
                      std::string("could not enter ") + sidb::to_string(state));
    }
    CHECK(machine.state() == sidb::ScanState::Idle);
}

TEST_CASE("state: an illegal transition is rejected and does not corrupt the state") {
    sidb::StateMachine machine;
    CHECK(machine.state() == sidb::ScanState::Idle);
    CHECK(machine.transition(sidb::ScanState::CameraInitializing, "test"));
    CHECK(machine.transition(sidb::ScanState::Scanning, "test"));
    // Scanning cannot jump straight to StudentFound.
    CHECK(!machine.transition(sidb::ScanState::StudentFound, "illegal"));
    CHECK(machine.state() == sidb::ScanState::Scanning);
    CHECK_EQ(machine.rejected(), static_cast<long long>(1));
    CHECK(machine.transition(sidb::ScanState::BarcodeDetected, "test"));
    CHECK(machine.transition(sidb::ScanState::Decoding, "test"));
    CHECK(machine.transition(sidb::ScanState::Validating, "test"));
    CHECK(machine.transition(sidb::ScanState::LookingUpStudent, "test"));
    CHECK(machine.transition(sidb::ScanState::StudentFound, "test"));
    CHECK(machine.state() == sidb::ScanState::StudentFound);
    CHECK_EQ(machine.visits(sidb::ScanState::StudentFound), static_cast<long long>(1));
}

TEST_CASE("state: a repeated frame is not an error") {
    sidb::StateMachine machine;
    machine.transition(sidb::ScanState::CameraInitializing);
    machine.transition(sidb::ScanState::Scanning);
    for (int i = 0; i < 10; ++i) {
        CHECK(machine.transition(sidb::ScanState::Scanning, "same frame"));
    }
    CHECK_EQ(machine.rejected(), static_cast<long long>(0));
}

TEST_CASE("state: force bypasses the table for teardown") {
    sidb::StateMachine machine;
    machine.transition(sidb::ScanState::CameraInitializing);
    machine.transition(sidb::ScanState::Scanning);
    machine.transition(sidb::ScanState::BarcodeDetected);
    machine.transition(sidb::ScanState::Decoding);
    // Shutting down from a mid-flight state must always land in Idle.
    CHECK(machine.force(sidb::ScanState::Idle, "shutdown"));
    CHECK(machine.state() == sidb::ScanState::Idle);
}

TEST_CASE("state: actions are refused while the workflow is busy") {
    sidb::StateMachine machine;
    machine.transition(sidb::ScanState::CameraInitializing);
    machine.transition(sidb::ScanState::Scanning);
    machine.transition(sidb::ScanState::BarcodeDetected);
    machine.transition(sidb::ScanState::Decoding);
    CHECK(sidb::state_is_busy(machine.state()));
    CHECK(!machine.allows(sidb::ScanAction::Cancel));
    machine.force(sidb::ScanState::StudentFound, "test");
    machine.transition(sidb::ScanState::ReadyForTransaction, "proceed");
    CHECK(machine.allows(sidb::ScanAction::SubmitTransaction));
    machine.transition(sidb::ScanState::TransactionProcessing, "submitting");
    CHECK(!machine.allows(sidb::ScanAction::SubmitTransaction));
}

TEST_CASE("state: a new card supersedes the previous result") {
    // STUDENT_FOUND and DUPLICATE_SCAN have no edge to BARCODE_DETECTED, so
    // ScannerEngine::enter_detection() returns to SCANNING first.
    for (const sidb::ScanState holding : {sidb::ScanState::StudentFound,
                                           sidb::ScanState::StudentNotFound,
                                           sidb::ScanState::DuplicateScan,
                                           sidb::ScanState::BarcodeInvalid,
                                           sidb::ScanState::Success}) {
        sidb::StateMachine machine;
        machine.force(holding, "test setup");
        CHECK(machine.can_accept_scan());
        // The two-step walk the engine performs must be legal.
        sidb::ScanState current = holding;
        if (!sidb::StateMachine::is_allowed(current, sidb::ScanState::BarcodeDetected) &&
            sidb::StateMachine::is_allowed(current, sidb::ScanState::Scanning)) {
            current = sidb::ScanState::Scanning;
        }
        CHECK_MSG(sidb::StateMachine::is_allowed(current, sidb::ScanState::BarcodeDetected),
                  std::string("no legal entry to BARCODE_DETECTED from ") +
                      sidb::to_string(holding));
    }
}

TEST_CASE("state: a state that cannot succeed refuses a new card") {
    // Scanning another card cannot help while the database is down, so the
    // frame is dropped instead of decoded into another error. A broken camera
    // is the exception: the state offers MANUAL_SEARCH, and a typed payload
    // never came from a frame, so it enters at VALIDATING.
    for (const sidb::ScanState blocking : {sidb::ScanState::DatabaseError,
                                            sidb::ScanState::TransactionError,
                                            sidb::ScanState::TransactionProcessing,
                                            sidb::ScanState::LookingUpStudent}) {
        sidb::StateMachine machine;
        machine.force(blocking, "test setup");
        CHECK(!machine.can_accept_scan());
    }
    // ...and every state that *can* accept one is not accidentally excluded.
    for (const sidb::ScanState accepting : {sidb::ScanState::CameraInitializing,
                                             sidb::ScanState::Scanning,
                                             sidb::ScanState::BarcodeDetected,
                                             sidb::ScanState::Decoding,
                                             sidb::ScanState::Validating,
                                             sidb::ScanState::CameraError,
                                             sidb::ScanState::StudentFound,
                                             sidb::ScanState::StudentNotFound,
                                             sidb::ScanState::BarcodeInvalid,
                                             sidb::ScanState::DuplicateScan,
                                             sidb::ScanState::Success}) {
        sidb::StateMachine machine;
        machine.force(accepting, "test setup");
        CHECK_MSG(machine.can_accept_scan(),
                  std::string("should accept a scan in ") + sidb::to_string(accepting));
    }
}

TEST_CASE("state: legacy names are still accepted on the wire") {
    sidb::ScanState state{};
    CHECK(sidb::coerce_state("VERIFYING", state));
    CHECK(state == sidb::ScanState::LookingUpStudent);
    CHECK(sidb::coerce_state("VERIFIED", state));
    CHECK(state == sidb::ScanState::StudentFound);
    CHECK(sidb::coerce_state("UNKNOWN_ID", state));
    CHECK(state == sidb::ScanState::StudentNotFound);
    CHECK(sidb::coerce_state("CAMERA_DISCONNECTED", state));
    CHECK(state == sidb::ScanState::CameraError);
    CHECK(sidb::coerce_state("STOPPED", state));
    CHECK(state == sidb::ScanState::Idle);
    CHECK(sidb::coerce_state("READY_FOR_TRANSACTION", state));
    CHECK(state == sidb::ScanState::ReadyForTransaction);
    CHECK(!sidb::coerce_state("NOT_A_STATE", state));
    CHECK(!sidb::coerce_state("", state));
}

TEST_CASE("state: errors clear when the scanner recovers") {
    sidb::StateMachine machine;
    machine.transition(sidb::ScanState::CameraInitializing, "test");
    machine.transition(sidb::ScanState::Scanning, "test");
    machine.set_error("CAMERA_DISCONNECTED", "Camera disconnected");
    CHECK_EQ(machine.error_code(), std::string("CAMERA_DISCONNECTED"));
    machine.transition(sidb::ScanState::CameraError, "test");
    machine.transition(sidb::ScanState::CameraInitializing, "reconnecting");
    machine.transition(sidb::ScanState::Scanning, "resumed");
    CHECK(machine.error_code().empty());
    CHECK(machine.error_message().empty());
}

// ---------------------------------------------------------------------------
// cooldown
// ---------------------------------------------------------------------------
TEST_CASE("cooldown: suppresses a repeat within the window") {
    sidb::CooldownTracker tracker(16);
    const double t0 = 1000.0;
    CHECK(!tracker.is_suppressed("barcode:A", 2000, t0));
    tracker.block("barcode:A", 2000, t0);
    CHECK(tracker.is_suppressed("barcode:A", 2000, t0 + 100));
    CHECK(tracker.is_suppressed("barcode:A", 2000, t0 + 1900));
    // Past the window it is allowed again.
    CHECK(!tracker.is_suppressed("barcode:A", 2000, t0 + 2100));
}

TEST_CASE("cooldown: a zero window disables suppression") {
    sidb::CooldownTracker tracker;
    tracker.block("barcode:A", 0, 0.0);
    CHECK(!tracker.is_suppressed("barcode:A", 0, 0.0));
    CHECK_EQ(tracker.remaining_ms("barcode:A", 0.0), 0);
}

TEST_CASE("cooldown: different barcodes do not affect each other") {
    sidb::CooldownTracker tracker;
    tracker.block("barcode:A", 5000, 0.0);
    CHECK(tracker.is_suppressed("barcode:A", 5000, 100.0));
    CHECK(!tracker.is_suppressed("barcode:B", 5000, 100.0));
}

TEST_CASE("cooldown: the map is bounded (LRU eviction)") {
    sidb::CooldownTracker tracker(4);
    for (int i = 0; i < 10; ++i) {
        tracker.block("barcode:" + std::to_string(i), 10000, 0.0);
    }
    CHECK_EQ(tracker.active_count(1.0), static_cast<std::size_t>(4));
    CHECK_EQ(tracker.capacity(), static_cast<std::size_t>(4));
    // The oldest entries were evicted, the newest survive.
    CHECK(!tracker.is_suppressed("barcode:0", 10000, 1.0));
    CHECK(tracker.is_suppressed("barcode:9", 10000, 1.0));
    tracker.clear();
    CHECK_EQ(tracker.active_count(1.0), static_cast<std::size_t>(0));
}

TEST_CASE("cooldown: remaining_ms counts down") {
    sidb::CooldownTracker tracker;
    tracker.block("barcode:A", 1000, 0.0);
    const int remaining = tracker.remaining_ms("barcode:A", 250.0);
    CHECK(remaining > 700 && remaining <= 750);
    CHECK_EQ(tracker.remaining_ms("barcode:missing", 0.0), 0);
}

// ---------------------------------------------------------------------------
// HTTP client
// ---------------------------------------------------------------------------
TEST_CASE("http: url encoding and JSON escaping") {
    CHECK_EQ(sidb::http::HttpClient::url_encode("2026-000001"), std::string("2026-000001"));
    CHECK_EQ(sidb::http::HttpClient::url_encode("a b/c"), std::string("a%20b%2Fc"));
    CHECK_EQ(sidb::http::HttpClient::json_escape("a\"b\\c"), std::string("a\\\"b\\\\c"));
    CHECK_EQ(sidb::http::HttpClient::json_escape("line\nbreak"), std::string("line\\nbreak"));
    // A control character becomes a \u escape, so the body stays valid JSON.
    CHECK_EQ(sidb::http::HttpClient::json_escape(std::string(1, '\x01')), std::string("\\u0001"));
}

TEST_CASE("http: GET and POST against a local listener") {
    TinyServer::Reply reply;
    reply.body = R"({"status":"ok"})";
    TinyServer server(reply);

    sidb::http::HttpClient client("127.0.0.1", server.port(), 2000);
    const sidb::http::Response response = client.get("/api/health");
    CHECK(response.ok);
    CHECK(response.status_ok());
    CHECK_EQ(response.status, 200);
    CHECK(response.json_contains("\"status\""));
    CHECK_EQ(response.headers.count("content-type"), static_cast<std::size_t>(1));

    const sidb::http::Response posted = client.post_json("/api/scans", R"({"barcode":"2026-000001"})");
    CHECK(posted.status_ok());
    CHECK(server.request_count() >= 2);
    CHECK(server.last_request().find("POST /api/scans") != std::string::npos);
    CHECK(server.last_request().find("2026-000001") != std::string::npos);
    CHECK(server.last_request().find("Content-Type: application/json") != std::string::npos);
}

TEST_CASE("http: a bearer token is sent when provided") {
    TinyServer::Reply reply;
    reply.body = "{}";
    TinyServer server(reply);
    sidb::http::HttpClient client("127.0.0.1", server.port(), 2000);
    const sidb::http::Response response =
        client.post_json_auth("/api/students", "{}", "secret-token-value");
    CHECK(response.status_ok());
    CHECK(server.last_request().find("Authorization: Bearer secret-token-value") !=
          std::string::npos);
}

TEST_CASE("http: an unreachable port fails fast without throwing") {
    // Port 1 on loopback is never listening in this test environment.
    sidb::http::HttpClient client("127.0.0.1", 1, 300);
    const auto started = std::chrono::steady_clock::now();
    const sidb::http::Response response = client.get("/api/health");
    const double elapsed =
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started)
            .count();
    CHECK(!response.ok);
    CHECK(!response.error.empty());
    CHECK_EQ(response.status, 0);
    CHECK(elapsed < 2000.0);  // the timeout is honoured
    CHECK(!sidb::http::HttpClient::is_reachable("127.0.0.1", 1, 200));
}

TEST_CASE("http: multipart bodies are well formed") {
    TinyServer::Reply reply;
    reply.status = 204;
    reply.body = "";
    TinyServer server(reply);
    sidb::http::HttpClient client("127.0.0.1", server.port(), 2000);

    std::vector<sidb::http::MultipartField> fields;
    fields.push_back({"image", {}, true, "frame.jpg", "image/jpeg", "\xff\xd8\xff\xe0fakejpeg"});
    fields.push_back({"source", "cpp", false, {}, {}, {}});
    const sidb::http::Response response =
        client.post_multipart("/api/stream/frame", fields);
    CHECK(response.ok);
    CHECK_EQ(response.status, 204);
    CHECK(server.last_request().find("multipart/form-data; boundary=") != std::string::npos);
    CHECK(server.last_request().find("filename=\"frame.jpg\"") != std::string::npos);
    CHECK(server.last_request().find("fakejpeg") != std::string::npos);
}

// ---------------------------------------------------------------------------
// API contract / JSON parsing
// ---------------------------------------------------------------------------
TEST_CASE("api: JSON helpers read the documented response shape") {
    const std::string body =
        R"({"success":true,"result":"VERIFIED","message":"Verified: Juan Dela Cruz",)"
        R"("scan_id":42,"barcode_type":"Code128","can_transact":true,"duplicate":false,)"
        R"("timing":{"database_ms":1.87},"student":{"student_id":"2026-000123",)"
        R"("name":"Juan Dela Cruz","course":"BS Computer Engineering","year_level":3,)"
        R"("section":"A","status":"active"}})";
    CHECK_EQ(sidb::ApiClient::json_string(body, "result"), std::string("VERIFIED"));
    CHECK_EQ(sidb::ApiClient::json_string(body, "message"), std::string("Verified: Juan Dela Cruz"));
    CHECK_EQ(static_cast<long long>(sidb::ApiClient::json_number(body, "scan_id")),
             static_cast<long long>(42));
    CHECK(sidb::ApiClient::json_bool(body, "can_transact"));
    CHECK(!sidb::ApiClient::json_bool(body, "duplicate"));
    CHECK_NEAR(sidb::ApiClient::json_number(body, "database_ms", -1.0), 1.87, 1e-9);
    // Nested access must not pick up a same-named key from a later object.
    CHECK_EQ(sidb::ApiClient::json_nested(body, "student", "student_id"),
             std::string("2026-000123"));
    CHECK_EQ(sidb::ApiClient::json_nested(body, "student", "status"), std::string("active"));
    CHECK_EQ(sidb::ApiClient::json_nested(body, "timing", "database_ms"), std::string("1.87"));
    // Missing keys degrade gracefully.
    CHECK_EQ(sidb::ApiClient::json_string(body, "nope"), std::string(""));
    CHECK_NEAR(sidb::ApiClient::json_number(body, "nope", -1.0), -1.0, 1e-9);
}

TEST_CASE("api: escapes in a student name are decoded") {
    const std::string body = R"({"student":{"name":"Juan \"JR\" Dela\u0020Cruz"}})";
    CHECK_EQ(sidb::ApiClient::json_nested(body, "student", "name"),
             std::string("Juan \"JR\" Dela Cruz"));
}

TEST_CASE("api: verify() parses a VERIFIED response") {
    TinyServer::Reply reply;
    reply.body =
        R"({"success":true,"result":"VERIFIED","message":"Verified: Maria Santos",)"
        R"("scan_id":7,"barcode_type":"Code128","can_transact":true,)"
        R"("timing":{"database_ms":0.9},"student":{"student_id":"2026-000002",)"
        R"("name":"Maria Santos","course":"BS Computer Engineering","year_level":3,)"
        R"("section":"A","status":"active"}})";
    TinyServer server(reply);

    sidb::ApiConfig config;
    config.host = "127.0.0.1";
    config.port = server.port();
    config.timeout_ms = 2000;
    sidb::ApiClient client(config);

    const sidb::VerificationResult result =
        client.verify("2026-000002", "Code128", 18.5, 20.0, "video0");
    CHECK(result.transport_ok);
    CHECK(result.success);
    CHECK_EQ(result.result, std::string("VERIFIED"));
    CHECK_EQ(result.student_id, std::string("2026-000002"));
    CHECK_EQ(result.student_name, std::string("Maria Santos"));
    CHECK_EQ(result.section, std::string("A"));
    CHECK(result.can_transact);
    CHECK_EQ(result.scan_id, static_cast<long long>(7));
    CHECK(!result.queued_offline);
    // The request carried the right shape.
    CHECK(server.last_request().find("\"barcode\":\"2026-000002\"") != std::string::npos);
    CHECK(server.last_request().find("\"source\":\"cpp\"") != std::string::npos);
    CHECK(server.last_request().find("\"detection_time_ms\":18.5") != std::string::npos);
    CHECK_EQ(client.stats().requests, static_cast<long long>(1));
}

TEST_CASE("api: verify() reports UNKNOWN_ID without inventing a student") {
    TinyServer::Reply reply;
    reply.body =
        R"({"success":false,"result":"UNKNOWN_ID","message":"Student record not found.",)"
        R"("error_code":"STUDENT_NOT_FOUND","barcode":"999999999","can_transact":false,)"
        R"("timing":{"database_ms":0.3}})";
    TinyServer server(reply);

    sidb::ApiConfig config;
    config.port = server.port();
    config.timeout_ms = 2000;
    sidb::ApiClient client(config);

    const sidb::VerificationResult result = client.verify("999999999", "Code128", 12.0, 13.0, "cam0");
    CHECK(result.transport_ok);
    CHECK(!result.success);
    CHECK_EQ(result.result, std::string("UNKNOWN_ID"));
    CHECK_EQ(result.error_code, std::string("STUDENT_NOT_FOUND"));
    CHECK(result.student_name.empty());
    CHECK(!result.can_transact);
}

TEST_CASE("api: verify() marks a suspended student as not transactable") {
    TinyServer::Reply reply;
    reply.body =
        R"({"success":true,"result":"VERIFIED","can_transact":false,)"
        R"("message":"Card is valid but cannot transact - suspended",)"
        R"("student":{"student_id":"2026-000012","name":"Hannah Lim Salazar","status":"suspended"}})";
    TinyServer server(reply);
    sidb::ApiConfig config;
    config.port = server.port();
    config.timeout_ms = 2000;
    sidb::ApiClient client(config);
    const sidb::VerificationResult result = client.verify("2026-000012", "Code128", 5, 6, "cam0");
    CHECK(result.success);
    CHECK(!result.can_transact);
    CHECK_EQ(result.status, std::string("suspended"));
}

TEST_CASE("api: an unreachable service queues the scan offline") {
    const std::filesystem::path queue_path = temp_path("queue.db");
    std::filesystem::remove(queue_path);

    sidb::DatabaseClient queue;
    CHECK(queue.open(queue_path.string(), 1000));
    CHECK_EQ(queue.pending_count(), 0);

    sidb::ApiConfig config;
    config.port = 1;  // nothing is listening
    config.timeout_ms = 250;
    config.retries = 1;
    config.retry_backoff_ms = 10;
    sidb::ApiClient client(config);
    client.set_offline_queue(&queue);

    const sidb::VerificationResult result = client.verify("2026-000001", "Code128", 10.0, 11.0, "cam0");
    CHECK(!result.transport_ok);
    CHECK_EQ(result.error_code, std::string("API_UNAVAILABLE"));
    CHECK(result.queued_offline);
    CHECK_EQ(queue.pending_count(), static_cast<long long>(1));

    const auto pending = queue.pending(10);
    CHECK_EQ(pending.size(), static_cast<std::size_t>(1));
    if (!pending.empty()) {
        CHECK_EQ(pending[0].barcode_value, std::string("2026-000001"));
        CHECK_EQ(pending[0].barcode_type, std::string("Code128"));
        CHECK_NEAR(pending[0].detection_ms, 10.0, 1e-9);
        CHECK(queue.remove(pending[0].id));
        CHECK_EQ(queue.pending_count(), static_cast<long long>(0));
    }
    queue.close();
    std::filesystem::remove(queue_path);
}

TEST_CASE("api: health probe reports reachability") {
    TinyServer::Reply reply;
    reply.body = R"({"status":"ok","offline":true})";
    TinyServer server(reply);
    sidb::ApiConfig config;
    config.port = server.port();
    config.timeout_ms = 1000;
    sidb::ApiClient client(config);
    CHECK(client.healthy(1000));
    CHECK(client.stats().reachable);

    sidb::ApiConfig broken;
    broken.port = 1;
    broken.timeout_ms = 200;
    sidb::ApiClient offline(broken);
    CHECK(!offline.healthy(200));
}

// ---------------------------------------------------------------------------
// offline queue
// ---------------------------------------------------------------------------
TEST_CASE("queue: survives reopening and keeps FIFO order") {
    const std::filesystem::path path = temp_path("fifo.db");
    std::filesystem::remove(path);
    {
        sidb::DatabaseClient queue;
        CHECK(queue.open(path.string(), 1000));
        CHECK(queue.enqueue("A1", "Code128", "cam0", 1.0, 2.0));
        CHECK(queue.enqueue("A2", "Code128", "cam0", 1.0, 2.0));
        CHECK(queue.enqueue("A3", "QRCode", "cam0", 1.0, 2.0));
        CHECK_EQ(queue.pending_count(), static_cast<long long>(3));
    }
    {
        sidb::DatabaseClient queue;
        CHECK(queue.open(path.string(), 1000));
        const auto pending = queue.pending(10);
        CHECK_EQ(pending.size(), static_cast<std::size_t>(3));
        if (pending.size() == 3) {
            CHECK_EQ(pending[0].barcode_value, std::string("A1"));
            CHECK_EQ(pending[1].barcode_value, std::string("A2"));
            CHECK_EQ(pending[2].barcode_value, std::string("A3"));
            CHECK_EQ(pending[2].barcode_type, std::string("QRCode"));
            CHECK(!pending[0].queued_at.empty());
        }
        // Paging is respected.
        CHECK_EQ(queue.pending(2).size(), static_cast<std::size_t>(2));
        queue.close();
    }
    std::filesystem::remove(path);
}

TEST_CASE("queue: a failed open is reported, not thrown") {
    sidb::DatabaseClient queue;
    const bool opened = queue.open("/proc/definitely/not/writable.db", 500);
    CHECK(!opened);
    CHECK(!queue.is_open());
    CHECK(!queue.error().empty());
    // Enqueueing without a handle must fail cleanly.
    CHECK(!queue.enqueue("X", "Code128", "cam", 0.0, 0.0));
}

TEST_CASE("queue: purging old rows keeps the file bounded") {
    const std::filesystem::path path = temp_path("purge.db");
    std::filesystem::remove(path);
    sidb::DatabaseClient queue;
    CHECK(queue.open(path.string(), 1000));
    CHECK(queue.enqueue("A", "Code128", "cam", 0.0, 0.0));
    // Nothing is old enough to purge yet.
    CHECK_EQ(queue.purge_older_than(7), 0);
    CHECK_EQ(queue.pending_count(), static_cast<long long>(1));
    queue.close();
    std::filesystem::remove(path);
}

// ---------------------------------------------------------------------------
// engine wiring (no camera)
// ---------------------------------------------------------------------------
TEST_CASE("engine: constructs and reports a clean status line without a camera") {
    sidb::Config config;
    config.database.enabled = false;   // do not create a queue file during the test
    config.audio.enabled = false;
    sidb::ScannerEngine engine(config, sidb::EngineMode::Service);
    CHECK(engine.state() == sidb::ScannerState::Initializing);
    const std::string status = engine.status_line();
    CHECK(status.find("state=") != std::string::npos);
    CHECK(status.find("camera=") != std::string::npos);
    CHECK(status.find("api=") != std::string::npos);
    // start() must fail cleanly and set an error rather than throw.
    const bool started = engine.start();
    if (!started) {
        CHECK(!engine.status_line().empty());
    }
    engine.stop();
}

int main() { RUN_ALL_TESTS(); }
