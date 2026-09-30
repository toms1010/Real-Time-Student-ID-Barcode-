// ===========================================================================
//  ScannerState.h - the scan state machine and duplicate suppression
//
//  This is the C++ mirror of `python/app/services/scan_workflow.py`. The same
//  seventeen states, the same transition table, the same operator-facing
//  wording, so the OpenCV scanner, the REST API and the cashier UI all describe
//  the workflow the same way.
//
//  Older builds reported a shorter set of names (VERIFYING, VERIFIED,
//  UNKNOWN_ID, ERROR, CAMERA_DISCONNECTED, STOPPED). `coerce_state` still
//  accepts them so a stale `state=` field from the service is not mistaken for
//  a bug; the enum and `to_string` only ever speak the canonical names.
//
//  `scripts/check_state_mirror.py` compares this file, the Python table and the
//  TypeScript table against each other, so the three cannot drift apart
//  silently.
//
//  `CooldownTracker` is the C++ half of the duplicate-suppression policy
//  (ADR-008). The service applies the same rule for HTTP callers; this copy
//  exists so the native window stops *rendering* the same card 30 times a
//  second even when no request is made.
// ===========================================================================
#ifndef SIDB_SCANNER_SCANNER_STATE_H
#define SIDB_SCANNER_SCANNER_STATE_H

#include <cstdint>
#include <deque>
#include <list>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace sidb {

/// The canonical states. Order matches the Python `ScanState` enum.
enum class ScanState : int {
    Idle = 0,
    CameraInitializing,
    Scanning,
    BarcodeDetected,
    Decoding,
    Validating,
    LookingUpStudent,
    StudentFound,
    ReadyForTransaction,
    TransactionProcessing,
    Success,
    CameraError,
    BarcodeInvalid,
    StudentNotFound,
    DatabaseError,
    TransactionError,
    DuplicateScan,
};

/// Number of states; used to size the visit counters.
inline constexpr int kScanStateCount = 17;

/// Source compatibility for code written against the previous enum.
using ScannerState = ScanState;

enum class ScanAction : int {
    StartCamera = 0,
    StopCamera,
    RetryCamera,
    Rescan,
    ScanAgain,
    ManualSearch,
    Proceed,
    Cancel,
    RetryLookup,
    SubmitTransaction,
    RetryTransaction,
    Acknowledge,
    ViewHistory,
};

enum class ScanPhase : int {
    Idle = 0,
    Acquiring,
    Detecting,
    Processing,
    Resolved,
    Actionable,
    Done,
    Error,
};

/// The canonical wire name, e.g. "LOOKING_UP_STUDENT".
const char* to_string(ScanState state);

/// The operator-facing message for the state. Newlines included, as on screen.
const char* state_message(ScanState state);

/// Short label for the on-screen status chip.
const char* display_label(ScanState state);

ScanPhase state_phase(ScanState state);
const char* to_string(ScanPhase phase);

/// True when the workflow is mid-flight and the operator must wait.
bool state_is_busy(ScanState state);

/// True for the six error states, all of which offer a recovery action.
bool state_is_error(ScanState state);

/// The actions the cashier may take in this state; empty while busy.
const std::vector<ScanAction>& state_actions(ScanState state);

const char* to_string(ScanAction action);

/// The legal successors of `state`, excluding `state` itself.
const std::vector<ScanState>& state_next(ScanState state);

/// Colour hint for the overlay / UI (BGR triple, like OpenCV scalars).
void state_colour(ScanState state, int& blue, int& green, int& red);

/// Parse a wire name, tolerating the legacy spellings. Returns false when the
/// value is not a state at all.
bool coerce_state(const std::string& value, ScanState& out);

class StateMachine {
public:
    /// Returns false when the transition is not allowed from the current state.
    /// An illegal transition is a programming error: it is logged and ignored
    /// so the scanner keeps running with the previous state.
    bool transition(ScanState next, const std::string& reason = {});

    /// As above, but bypasses the table. For teardown paths that must always
    /// land (shutting down, replay finished).
    bool force(ScanState next, const std::string& reason = {});

    ScannerState state() const { return state_; }
    const std::string& error_code() const { return error_code_; }
    const std::string& error_message() const { return error_message_; }
    const std::string& last_reason() const { return last_reason_; }

    void set_error(const std::string& code, const std::string& message);
    void clear_error();

    /// Number of rejected transitions; a non-zero value at shutdown means the
    /// workflow was driven illegally, which is a bug worth seeing in the log.
    long long rejected() const { return rejected_; }

    /// Number of times the machine entered `state` (used by the tests and by
    /// the performance report).
    long long visits(ScanState state) const;

    /// Whether the given transition is legal from `from` (static, for tests).
    static bool is_allowed(ScanState from, ScanState to);

    /// Whether the workflow currently permits `action`.
    bool allows(ScanAction action) const;

    /// Whether a new symbol may enter the workflow from the current state.
    ///
    /// The decode loop asks this before doing any work. `false` in
    /// `DATABASE_ERROR` and `CAMERA_ERROR` is deliberate: while the database
    /// or the camera is down, scanning another card cannot succeed, so the
    /// frames are dropped rather than decoded into an error.
    bool can_accept_scan() const;

private:
    bool move(ScanState next, const std::string& reason, bool bypass_table);

    mutable std::mutex mutex_;
    ScanState state_ = ScanState::Idle;
    std::string error_code_;
    std::string error_message_;
    std::string last_reason_;
    std::vector<long long> visits_ = std::vector<long long>(kScanStateCount, 0);
    long long rejected_ = 0;
};

/// Bounded, thread-safe "seen this payload N ms ago" set.
class CooldownTracker {
public:
    explicit CooldownTracker(std::size_t max_entries = 512) : max_entries_(max_entries) {}

    /// True when `key` was blocked within the last `window_ms`; also refreshes
    /// the entry so a continuously held card keeps being suppressed.
    bool is_suppressed(const std::string& key, int window_ms, double now_ms);

    /// Mark `key` as seen now.
    void block(const std::string& key, int window_ms, double now_ms);

    /// Milliseconds left before `key` may be processed again (0 when free).
    int remaining_ms(const std::string& key, double now_ms) const;

    void clear();
    std::size_t active_count(double now_ms) const;
    std::size_t capacity() const { return max_entries_; }

private:
    struct Entry {
        double expires_at_ms = 0.0;
    };
    // LRU: the front is the least recently used, the back the most recent.
    mutable std::mutex mutex_;
    std::list<std::pair<std::string, Entry>> order_;
    std::unordered_map<std::string, std::list<std::pair<std::string, Entry>>::iterator> index_;
    std::size_t max_entries_;
};

}  // namespace sidb

#endif  // SIDB_SCANNER_SCANNER_STATE_H
