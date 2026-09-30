// ===========================================================================
//  ScannerState.cpp - the state table, and duplicate suppression
//
//  The tables below are the C++ copy of `app/services/scan_workflow.py`.
//  `scripts/check_state_mirror.py` diffs the two; keep them in step.
// ===========================================================================
#include "scanner/ScannerState.h"

#include <algorithm>
#include <array>

#include "utils/Logger.hpp"

namespace sidb {

namespace {

constexpr const char* kComponent = "state";

// --- state metadata --------------------------------------------------------
// Indexed by ScanState. Keep the order identical to the enum.
struct StateInfo {
    const char* name;
    const char* label;
    const char* message;
    ScanPhase phase;
    bool busy;
};

constexpr std::array<StateInfo, kScanStateCount> kStates = {{
    {"IDLE", "Ready to scan",
     "Ready to Scan\n\nPlease position the student ID inside the scanning area.",
     ScanPhase::Idle, false},
    {"CAMERA_INITIALIZING", "Starting camera", "Starting camera...",
     ScanPhase::Acquiring, true},
    {"SCANNING", "Scanning", "Scanning...\nPosition the ID inside the box.",
     ScanPhase::Detecting, false},
    {"BARCODE_DETECTED", "Barcode detected", "Barcode detected...",
     ScanPhase::Detecting, true},
    {"DECODING", "Decoding", "Decoding barcode...",
     ScanPhase::Processing, true},
    {"VALIDATING", "Validating", "Validating barcode...",
     ScanPhase::Processing, true},
    {"LOOKING_UP_STUDENT", "Looking up student", "Checking student record...",
     ScanPhase::Processing, true},
    {"STUDENT_FOUND", "Student found", "Student found.",
     ScanPhase::Resolved, false},
    {"READY_FOR_TRANSACTION", "Ready", "Ready to process the transaction.",
     ScanPhase::Actionable, false},
    {"TRANSACTION_PROCESSING", "Processing", "Processing transaction...\nPlease wait.",
     ScanPhase::Processing, true},
    {"SUCCESS", "Success", "Transaction successful.",
     ScanPhase::Done, false},
    {"CAMERA_ERROR", "Camera error", "Camera unavailable.\n\nPlease check the webcam connection.",
     ScanPhase::Error, false},
    {"BARCODE_INVALID", "Invalid barcode",
     "Invalid barcode.\n\nPlease position the ID correctly and try again.",
     ScanPhase::Error, false},
    {"STUDENT_NOT_FOUND", "Not found",
     "Student record not found.\n\nPlease verify the ID and try again.",
     ScanPhase::Error, false},
    {"DATABASE_ERROR", "Database error",
     "Unable to access student records.\n\nPlease try again or contact the administrator.",
     ScanPhase::Error, false},
    {"TRANSACTION_ERROR", "Transaction failed",
     "Transaction failed.\n\nNo transaction was recorded.\nPlease try again.",
     ScanPhase::Error, false},
    {"DUPLICATE_SCAN", "Duplicate",
     "This ID was already scanned.\n\nPlease wait or verify the transaction history.",
     ScanPhase::Error, false},
}};

// --- the transition table ---------------------------------------------------
constexpr ScanState kIdle[] = {ScanState::CameraInitializing, ScanState::Validating};
constexpr ScanState kCameraInitializing[] = {ScanState::Scanning, ScanState::CameraError};
constexpr ScanState kScanning[] = {ScanState::BarcodeDetected, ScanState::CameraError,
                                   ScanState::Validating, ScanState::Idle};
constexpr ScanState kBarcodeDetected[] = {ScanState::Decoding, ScanState::BarcodeInvalid,
                                          ScanState::Scanning};
constexpr ScanState kDecoding[] = {ScanState::Validating, ScanState::BarcodeInvalid,
                                  ScanState::DuplicateScan};
constexpr ScanState kValidating[] = {ScanState::LookingUpStudent, ScanState::BarcodeInvalid};
constexpr ScanState kLookingUp[] = {ScanState::StudentFound, ScanState::StudentNotFound,
                                    ScanState::DatabaseError, ScanState::BarcodeInvalid,
                                    ScanState::DuplicateScan};
constexpr ScanState kStudentFound[] = {ScanState::ReadyForTransaction, ScanState::Scanning,
                                       ScanState::Idle};
constexpr ScanState kReady[] = {ScanState::TransactionProcessing, ScanState::Scanning,
                                ScanState::Idle, ScanState::TransactionError};
constexpr ScanState kProcessing[] = {ScanState::Success, ScanState::TransactionError,
                                     ScanState::DuplicateScan};
constexpr ScanState kSuccess[] = {ScanState::Idle, ScanState::Scanning};
constexpr ScanState kCameraError[] = {ScanState::CameraInitializing, ScanState::Validating,
                                    ScanState::Idle};
constexpr ScanState kBarcodeInvalid[] = {ScanState::Scanning, ScanState::Idle};
constexpr ScanState kStudentNotFound[] = {ScanState::Scanning, ScanState::Idle};
constexpr ScanState kDatabaseError[] = {ScanState::LookingUpStudent, ScanState::Idle};
constexpr ScanState kTransactionError[] = {ScanState::ReadyForTransaction, ScanState::Idle};
constexpr ScanState kDuplicate[] = {ScanState::Scanning, ScanState::Idle};

struct TransitionRow {
    const ScanState* targets;
    std::size_t count;
};

// Indexed by ScanState, in enum order.
constexpr std::array<TransitionRow, kScanStateCount> kTransitions = {{
    {kIdle, std::size(kIdle)},
    {kCameraInitializing, std::size(kCameraInitializing)},
    {kScanning, std::size(kScanning)},
    {kBarcodeDetected, std::size(kBarcodeDetected)},
    {kDecoding, std::size(kDecoding)},
    {kValidating, std::size(kValidating)},
    {kLookingUp, std::size(kLookingUp)},
    {kStudentFound, std::size(kStudentFound)},
    {kReady, std::size(kReady)},
    {kProcessing, std::size(kProcessing)},
    {kSuccess, std::size(kSuccess)},
    {kCameraError, std::size(kCameraError)},
    {kBarcodeInvalid, std::size(kBarcodeInvalid)},
    {kStudentNotFound, std::size(kStudentNotFound)},
    {kDatabaseError, std::size(kDatabaseError)},
    {kTransactionError, std::size(kTransactionError)},
    {kDuplicate, std::size(kDuplicate)},
}};

// --- actions ----------------------------------------------------------------
// `std::array` rather than a C array: a state with no actions needs a
// well-formed empty container, and `T x[0] = {}` is not valid C++.
struct ActionRow {
    const ScanAction* actions;
    std::size_t count;
};

using ActionList = std::array<ScanAction, 3>;

constexpr ScanAction aIdle[] = {ScanAction::StartCamera, ScanAction::ScanAgain,
                                ScanAction::ManualSearch};
constexpr ActionList aNone{};
constexpr ScanAction aScanning[] = {ScanAction::StopCamera, ScanAction::ScanAgain};
constexpr ScanAction aStudentFound[] = {ScanAction::Proceed, ScanAction::Cancel,
                                        ScanAction::ScanAgain};
constexpr ScanAction aReady[] = {ScanAction::Proceed, ScanAction::Cancel, ScanAction::ScanAgain,
                                 ScanAction::SubmitTransaction};
constexpr ScanAction aSuccess[] = {ScanAction::Acknowledge, ScanAction::ScanAgain};
constexpr ScanAction aCameraError[] = {ScanAction::RetryCamera, ScanAction::ManualSearch,
                                       ScanAction::Cancel};
constexpr ScanAction aBarcodeInvalid[] = {ScanAction::Rescan, ScanAction::Cancel,
                                          ScanAction::ManualSearch};
constexpr ScanAction aStudentNotFound[] = {ScanAction::ScanAgain, ScanAction::ManualSearch,
                                           ScanAction::Cancel};
constexpr ScanAction aDatabaseError[] = {ScanAction::RetryLookup, ScanAction::Cancel};
constexpr ScanAction aTransactionError[] = {ScanAction::RetryTransaction, ScanAction::Cancel};
constexpr ScanAction aDuplicate[] = {ScanAction::ScanAgain, ScanAction::ViewHistory,
                                     ScanAction::Cancel};

constexpr std::array<ActionRow, kScanStateCount> kActions = {{
    {aIdle, std::size(aIdle)},
    {aNone.data(), aNone.size()},
    {aScanning, std::size(aScanning)},
    {aNone.data(), aNone.size()},
    {aNone.data(), aNone.size()},
    {aNone.data(), aNone.size()},
    {aNone.data(), aNone.size()},
    {aStudentFound, std::size(aStudentFound)},
    {aReady, std::size(aReady)},
    {aNone.data(), aNone.size()},
    {aSuccess, std::size(aSuccess)},
    {aCameraError, std::size(aCameraError)},
    {aBarcodeInvalid, std::size(aBarcodeInvalid)},
    {aStudentNotFound, std::size(aStudentNotFound)},
    {aDatabaseError, std::size(aDatabaseError)},
    {aTransactionError, std::size(aTransactionError)},
    {aDuplicate, std::size(aDuplicate)},
}};

/// Names emitted by builds before the workflow state machine (ADR-011).
struct LegacyAlias {
    const char* name;
    ScanState state;
};

constexpr LegacyAlias kLegacyAliases[] = {
    {"INITIALIZING", ScanState::CameraInitializing},
    {"CAMERA_READY", ScanState::Scanning},
    {"VERIFYING", ScanState::LookingUpStudent},
    {"VERIFIED", ScanState::StudentFound},
    {"UNKNOWN_ID", ScanState::StudentNotFound},
    {"ERROR", ScanState::DatabaseError},
    {"CAMERA_DISCONNECTED", ScanState::CameraError},
    {"STOPPED", ScanState::Idle},
    {"INVALID_BARCODE", ScanState::BarcodeInvalid},
    {"TRANSACTION_COMPLETED", ScanState::Success},
};

constexpr std::size_t index_of(ScanState state) {
    return static_cast<std::size_t>(state);
}

constexpr bool in(const ScanState* targets, std::size_t count, ScanState value) {
    for (std::size_t i = 0; i < count; ++i) {
        if (targets[i] == value) return true;
    }
    return false;
}

}  // namespace

const char* to_string(ScanState state) {
    if (index_of(state) >= kStates.size()) return "UNKNOWN";
    return kStates[index_of(state)].name;
}

const char* state_message(ScanState state) {
    if (index_of(state) >= kStates.size()) return "";
    return kStates[index_of(state)].message;
}

const char* display_label(ScanState state) {
    if (index_of(state) >= kStates.size()) return "";
    return kStates[index_of(state)].label;
}

ScanPhase state_phase(ScanState state) {
    if (index_of(state) >= kStates.size()) return ScanPhase::Error;
    return kStates[index_of(state)].phase;
}

const char* to_string(ScanPhase phase) {
    switch (phase) {
        case ScanPhase::Idle: return "idle";
        case ScanPhase::Acquiring: return "acquiring";
        case ScanPhase::Detecting: return "detecting";
        case ScanPhase::Processing: return "processing";
        case ScanPhase::Resolved: return "resolved";
        case ScanPhase::Actionable: return "actionable";
        case ScanPhase::Done: return "done";
        case ScanPhase::Error: return "error";
    }
    return "error";
}

bool state_is_busy(ScanState state) {
    if (index_of(state) >= kStates.size()) return false;
    return kStates[index_of(state)].busy;
}

bool state_is_error(ScanState state) { return state_phase(state) == ScanPhase::Error; }

const std::vector<ScanAction>& state_actions(ScanState state) {
    static const std::vector<std::vector<ScanAction>> cache = [] {
        std::vector<std::vector<ScanAction>> rows;
        rows.reserve(kActions.size());
        for (const ActionRow& row : kActions) {
            rows.emplace_back(row.actions, row.actions + row.count);
        }
        return rows;
    }();
    static const std::vector<ScanAction> kNoActions;
    if (index_of(state) >= cache.size()) return kNoActions;
    // A busy state offers nothing: the workflow is between two points.
    return state_is_busy(state) ? kNoActions : cache[index_of(state)];
}

const char* to_string(ScanAction action) {
    switch (action) {
        case ScanAction::StartCamera: return "START_CAMERA";
        case ScanAction::StopCamera: return "STOP_CAMERA";
        case ScanAction::RetryCamera: return "RETRY_CAMERA";
        case ScanAction::Rescan: return "RESCAN";
        case ScanAction::ScanAgain: return "SCAN_AGAIN";
        case ScanAction::ManualSearch: return "MANUAL_SEARCH";
        case ScanAction::Proceed: return "PROCEED";
        case ScanAction::Cancel: return "CANCEL";
        case ScanAction::RetryLookup: return "RETRY_LOOKUP";
        case ScanAction::SubmitTransaction: return "SUBMIT_TRANSACTION";
        case ScanAction::RetryTransaction: return "RETRY_TRANSACTION";
        case ScanAction::Acknowledge: return "ACKNOWLEDGE";
        case ScanAction::ViewHistory: return "VIEW_HISTORY";
    }
    return "UNKNOWN";
}

const std::vector<ScanState>& state_next(ScanState state) {
    static const std::vector<std::vector<ScanState>> cache = [] {
        std::vector<std::vector<ScanState>> rows;
        rows.reserve(kTransitions.size());
        for (const TransitionRow& row : kTransitions) {
            rows.emplace_back(row.targets, row.targets + row.count);
        }
        return rows;
    }();
    static const std::vector<ScanState> empty;
    if (index_of(state) >= cache.size()) return empty;
    return cache[index_of(state)];
}

void state_colour(ScanState state, int& blue, int& green, int& red) {
    switch (state) {
        case ScanState::StudentFound:
        case ScanState::ReadyForTransaction:
        case ScanState::Success:            blue = 90;  green = 200; red = 90;  break;
        case ScanState::StudentNotFound:
        case ScanState::DuplicateScan:      blue = 60;  green = 170; red = 240; break;
        case ScanState::CameraError:
        case ScanState::BarcodeInvalid:
        case ScanState::DatabaseError:
        case ScanState::TransactionError:  blue = 60;  green = 60;  red = 235; break;
        case ScanState::BarcodeDetected:
        case ScanState::Decoding:
        case ScanState::Validating:
        case ScanState::LookingUpStudent:
        case ScanState::TransactionProcessing:
                                         blue = 70;  green = 200; red = 235; break;
        default:                           blue = 220; green = 220; red = 220; break;
    }
}

bool coerce_state(const std::string& value, ScanState& out) {
    if (value.empty()) return false;
    for (const StateInfo& info : kStates) {
        if (value == info.name) {
            out = static_cast<ScanState>(&info - kStates.data());
            return true;
        }
    }
    for (const LegacyAlias& alias : kLegacyAliases) {
        if (value == alias.name) {
            out = alias.state;
            return true;
        }
    }
    return false;
}

bool StateMachine::is_allowed(ScanState from, ScanState to) {
    if (from == to) return true;
    if (index_of(from) >= kTransitions.size()) return false;
    const TransitionRow& row = kTransitions[index_of(from)];
    return in(row.targets, row.count, to);
}

bool StateMachine::allows(ScanAction action) const {
    if (state_is_busy(state_)) return false;
    for (const ScanAction& candidate : state_actions(state_)) {
        if (candidate == action) return true;
    }
    return false;
}

bool StateMachine::can_accept_scan() const {
    // Mirrors `_entry_state()` in app/services/scan_workflow_service.py: a
    // payload can be taken at BARCODE_DETECTED when the camera is live, or at
    // VALIDATING when it arrived without a frame behind it. The two must agree,
    // or the decode loop would skip frames the service would happily accept.
    std::lock_guard<std::mutex> lock(mutex_);
    return is_allowed(state_, ScanState::BarcodeDetected) ||
           is_allowed(state_, ScanState::Scanning) ||
           is_allowed(state_, ScanState::Validating);
}

bool StateMachine::transition(ScanState next, const std::string& reason) {
    return move(next, reason, /*bypass_table=*/false);
}

bool StateMachine::force(ScanState next, const std::string& reason) {
    return move(next, reason, /*bypass_table=*/true);
}

bool StateMachine::move(ScanState next, const std::string& reason, bool bypass_table) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (index_of(next) >= kStates.size()) {
        ++rejected_;
        SIDB_LOG(log::Level::Error, kComponent,
                 std::string("rejected transition to an unknown state (") + reason + ")");
        return false;
    }
    if (!bypass_table && !is_allowed(state_, next)) {
        ++rejected_;
        SIDB_LOG(log::Level::Error, kComponent,
                 std::string("illegal transition ") + to_string(state_) + " -> " +
                     to_string(next) + " (" + reason + ") - keeping the previous state");
        return false;
    }
    if (next == state_) {
        // A repeated frame is not an error; refresh the reason so the overlay
        // still reflects why we are here.
        last_reason_ = reason;
        return true;
    }
    if (!state_is_error(next)) {
        error_code_.clear();
        error_message_.clear();
    }
    last_reason_ = reason;
    state_ = next;
    ++visits_[index_of(next)];
    return true;
}

void StateMachine::set_error(const std::string& code, const std::string& message) {
    std::lock_guard<std::mutex> lock(mutex_);
    error_code_ = code;
    error_message_ = message;
}

void StateMachine::clear_error() {
    std::lock_guard<std::mutex> lock(mutex_);
    error_code_.clear();
    error_message_.clear();
}

long long StateMachine::visits(ScanState state) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (index_of(state) >= visits_.size()) return 0;
    return visits_[index_of(state)];
}

// --- CooldownTracker --------------------------------------------------------

bool CooldownTracker::is_suppressed(const std::string& key, int window_ms, double now_ms) {
    if (window_ms <= 0 || key.empty()) return false;
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = index_.find(key);
    if (found == index_.end()) return false;
    if (found->second->second.expires_at_ms <= now_ms) {
        order_.erase(found->second);
        index_.erase(found);
        return false;
    }
    // Refresh: a card held in front of the lens must stay suppressed.
    found->second->second.expires_at_ms = now_ms + window_ms;
    order_.splice(order_.end(), order_, found->second);
    return true;
}

void CooldownTracker::block(const std::string& key, int window_ms, double now_ms) {
    if (window_ms <= 0 || key.empty()) return;
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = index_.find(key);
    if (found != index_.end()) {
        found->second->second.expires_at_ms = now_ms + window_ms;
        order_.splice(order_.end(), order_, found->second);
        return;
    }
    order_.emplace_back(key, Entry{now_ms + window_ms});
    auto iterator = order_.end();
    --iterator;
    index_[key] = iterator;
    while (order_.size() > max_entries_) {
        index_.erase(order_.front().first);
        order_.pop_front();
    }
}

int CooldownTracker::remaining_ms(const std::string& key, double now_ms) const {
    std::lock_guard<std::mutex> lock(mutex_);
    const auto found = index_.find(key);
    if (found == index_.end()) return 0;
    const double remaining = found->second->second.expires_at_ms - now_ms;
    return remaining <= 0.0 ? 0 : static_cast<int>(remaining);
}

void CooldownTracker::clear() {
    std::lock_guard<std::mutex> lock(mutex_);
    order_.clear();
    index_.clear();
}

std::size_t CooldownTracker::active_count(double now_ms) const {
    std::lock_guard<std::mutex> lock(mutex_);
    std::size_t count = 0;
    for (const auto& entry : order_) {
        if (entry.second.expires_at_ms > now_ms) ++count;
    }
    return count;
}

}  // namespace sidb
