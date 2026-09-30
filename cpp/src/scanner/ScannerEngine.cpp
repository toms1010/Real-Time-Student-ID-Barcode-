// ===========================================================================
//  ScannerEngine.cpp
// ===========================================================================
#include "scanner/ScannerEngine.h"

#include <algorithm>
#include <chrono>
#include <sstream>

#include <opencv2/highgui.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include "utils/Logger.hpp"
#include "utils/TimeUtil.h"

namespace sidb {

namespace {

constexpr const char* kComponent = "engine";

/// Escape a string for the small JSON document we hand to the service.
std::string json_string(const std::string& value) {
    return http::HttpClient::json_escape(value);
}

double round2(double value) { return std::round(value * 100.0) / 100.0; }

/// Map a service error code onto the workflow state that describes it.
///
/// A payload that never validated is a BARCODE_INVALID; an unreachable
/// service or database is a DATABASE_ERROR. Collapsing both into one generic
/// "ERROR" state is what the previous version did, and it left the cashier
/// guessing whether to re-scan the card or call the administrator.
ScanState state_for_error(const std::string& code) {
    if (code == "DATABASE_ERROR" || code == "API_UNAVAILABLE" ||
        code == "UNKNOWN_ERROR") {
        return ScanState::DatabaseError;
    }
    return ScanState::BarcodeInvalid;
}

}  // namespace

ScannerEngine::ScannerEngine(const Config& config, EngineMode mode)
    : config_(config),
      mode_(mode),
      camera_(config.camera),
      processor_(config),
      api_(config.api),
      offline_queue_(std::make_unique<DatabaseClient>()),
      audio_(std::make_unique<AudioFeedback>(config.audio)),
      monitor_(120),
      cooldowns_(static_cast<std::size_t>(std::max(1, config.scanner.max_tracked_barcodes))) {
    if (config_.database.enabled) {
        if (offline_queue_->open(config_.resolve(config_.database.path),
                                 config_.database.busy_timeout_ms)) {
            api_.set_offline_queue(offline_queue_.get());
        } else {
            SIDB_LOG(log::Level::Warning, kComponent,
                     "running without the offline queue: " + offline_queue_->error());
        }
    } else {
        offline_queue_.reset();
    }
}

ScannerEngine::~ScannerEngine() { stop(); }

// ---------------------------------------------------------------------------
// lifecycle
// ---------------------------------------------------------------------------
bool ScannerEngine::start() {
    if (running_.load()) return true;
    set_state(ScanState::CameraInitializing, "starting up");

    if (!camera_.open()) {
        const CameraStatus status = camera_.status();
        machine_.set_error(status.last_error, status.last_error_message);
        set_state(ScanState::CameraError, "camera could not be opened");
        SIDB_LOG(log::Level::Error, kComponent,
                 "startup aborted: " + status.last_error + " - " + status.last_error_message);
        return false;
    }
    set_state(ScanState::Scanning, "camera opened");
    audio_->start();

    running_.store(true);
    monitor_.start();

    if (mode_ == EngineMode::Native || mode_ == EngineMode::Service) {
        capture_thread_ = std::thread(&ScannerEngine::capture_loop, this);
        processing_thread_ = std::thread(&ScannerEngine::processing_loop, this);
    }
    SIDB_LOG(log::Level::Info, kComponent,
             std::string("scanner started in ") +
                 (mode_ == EngineMode::Native ? "native"
                  : mode_ == EngineMode::Service ? "service" : "file") +
                 " mode; camera=" + camera_.device_label());
    return true;
}

void ScannerEngine::stop() {
    if (!running_.exchange(false)) {
        // Still release resources when start() failed half way.
        camera_.close();
        audio_->stop();
        return;
    }
    frame_cv_.notify_all();
    if (capture_thread_.joinable()) capture_thread_.join();
    if (processing_thread_.joinable()) processing_thread_.join();
    audio_->stop();
    camera_.close();
    force_state(ScanState::Idle, "shutdown");
    SIDB_LOG(log::Level::Info, kComponent, "scanner stopped\n\n" + monitor_.report());
}

void ScannerEngine::set_state(ScanState state, const std::string& reason) {
    if (!machine_.transition(state, reason)) return;
    if (state == ScanState::CameraError) {
        machine_.set_error(error::kCameraDisconnected, "Camera disconnected. Reconnecting...");
    }
}

void ScannerEngine::force_state(ScanState state, const std::string& reason) {
    machine_.force(state, reason);
}

void ScannerEngine::enter_detection(const std::string& payload) {
    // A symbol is in frame. When the machine is still holding the previous
    // card's result (STUDENT_FOUND, DUPLICATE_SCAN, ...) a new card
    // supersedes it, so the workflow goes back to SCANNING first: the table
    // has no edge from those states straight to BARCODE_DETECTED.
    if (!StateMachine::is_allowed(machine_.state(), ScanState::BarcodeDetected) &&
        StateMachine::is_allowed(machine_.state(), ScanState::Scanning)) {
        set_state(ScanState::Scanning, "a new symbol is in view");
    }
    set_state(ScanState::BarcodeDetected, "symbol found: " + payload);
}

// ---------------------------------------------------------------------------
// capture thread
// ---------------------------------------------------------------------------
void ScannerEngine::capture_loop() {
    int consecutive_failures = 0;
    double next_frame_due = 0.0;
    while (running_.load()) {
        if (frame_limit_ > 0.0) {
            const double period_ms = 1000.0 / frame_limit_;
            const double now = TimeUtil::monotonic_ms();
            if (now < next_frame_due) {
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
                continue;
            }
            next_frame_due = now + period_ms;
        }

        Frame frame;
        const std::string error = camera_.read(frame);
        if (!error.empty()) {
            monitor_.record_capture_failure();
            set_state(ScanState::CameraError, error);
            if (++consecutive_failures >= 5) {
                consecutive_failures = 0;
                SIDB_LOG(log::Level::Warning, kComponent, "reopening the camera after 5 failures");
                std::this_thread::sleep_for(std::chrono::milliseconds(800));
                if (!camera_.open()) {
                    std::this_thread::sleep_for(std::chrono::seconds(2));
                    continue;
                }
                set_state(ScanState::CameraInitializing, "camera reopened");
            } else {
                std::this_thread::sleep_for(std::chrono::milliseconds(60));
            }
            continue;
        }
        consecutive_failures = 0;
        monitor_.record_capture();
        if (machine_.state() == ScanState::CameraError) {
            // Frames are flowing again, but the workflow goes back through
            // CAMERA_INITIALIZING rather than straight to SCANNING: the table
            // does not allow skipping the re-acquisition, and the cashier
            // deserves to see the camera being re-established.
            set_state(ScanState::CameraInitializing, "capture resumed");
            set_state(ScanState::Scanning, "capture resumed");
        }

        {
            std::lock_guard<std::mutex> lock(frame_mutex_);
            pending_frame_ = frame.image;
            pending_sequence_ = frame.sequence;
        }
        frame_cv_.notify_one();
    }
}

// ---------------------------------------------------------------------------
// processing thread
// ---------------------------------------------------------------------------
void ScannerEngine::processing_loop() {
    long long last_sequence = -1;
    while (running_.load()) {
        cv::Mat frame;
        long long sequence = -1;
        {
            std::unique_lock<std::mutex> lock(frame_mutex_);
            if (pending_sequence_ == last_sequence) {
                frame_cv_.wait_for(lock, std::chrono::milliseconds(20));
            }
            if (pending_sequence_ == last_sequence) continue;
            frame = pending_frame_.empty() ? cv::Mat() : pending_frame_.clone();
            sequence = pending_sequence_;
        }
        last_sequence = sequence;
        if (frame.empty()) {
            monitor_.record_skip();
            continue;
        }
        process_frame(frame, sequence);
    }
}

void ScannerEngine::process_frame(const cv::Mat& frame, long long sequence) {
    // A card held at the counter is decoded on every frame. While the workflow
    // cannot accept a new symbol - a lookup or a transaction is in flight, or
    // the database or camera is down - there is no point decoding, and
    // certainly no point querying the service.
    if (!machine_.can_accept_scan()) {
        monitor_.record_skip();
        return;
    }

    const ScannerState current = machine_.state();
    int blue = 220, green = 220, red = 220;
    state_colour(current, blue, green, red);
    const std::string status = std::string(display_label(current));

    const bool draw = mode_ == EngineMode::Native;
    const FrameResult result = processor_.process(frame, status, cv::Scalar(blue, green, red), draw);

    if (result.decode.error_code == error::kBarcodeNotDetected ||
        result.decode.error_code == error::kBarcodeDecodeFailed) {
        monitor_.record_decode_failure();
    }
    monitor_.record_processing(result.total_ms, result.preprocess_ms, result.detect_ms,
                               result.decode_ms, 0.0, result.total_ms);

    ScanOutcome outcome;
    if (result.primary != nullptr && result.primary->valid) {
        outcome = handle_detection(result);
    } else if (result.error_code == error::kBarcodeNotDetected) {
        // Nothing to do: keep scanning.  Returning to Scanning from any of the
        // outcome states is what clears a stale "VERIFIED" banner.
        if (current == ScanState::BarcodeDetected || current == ScanState::StudentFound ||
            current == ScanState::StudentNotFound) {
            set_state(ScanState::Scanning, "no barcode in view");
        }
        outcome.result.clear();
        outcome.state = machine_.state();
    } else {
        outcome.result = result.error_code;
        outcome.barcode = result.primary != nullptr ? result.primary->text : "";
        outcome.format = result.primary != nullptr ? result.primary->format : "";
        // A symbol was located but the payload was rejected locally. The table
        // reaches BARCODE_INVALID through BARCODE_DETECTED, so walk it rather
        // than forcing the move.
        enter_detection(outcome.barcode);
        set_state(ScanState::BarcodeInvalid, "unreadable symbol");
    }

    publish_overlay(frame, outcome, result);
    if (mode_ == EngineMode::Service) {
        publish_service_frame(latest_overlay(), result, outcome);
    }

    {
        std::lock_guard<std::mutex> lock(outcome_mutex_);
        last_outcome_ = outcome;
    }

    if (quit_after_frames_ > 0 && monitor_.snapshot().frames_processed >= quit_after_frames_) {
        running_.store(false);
        frame_cv_.notify_all();
    }
    (void)sequence;
}

ScanOutcome ScannerEngine::handle_detection(const FrameResult& result) {
    ScanOutcome outcome;
    const BarcodeDetection& detection = *result.primary;
    outcome.decoded = true;
    outcome.barcode = detection.text;
    outcome.format = detection.format;

    // The payload was already validated locally; an unusable one is reported
    // without a round trip to the service.
    if (!result.error_code.empty()) {
        outcome.result = result.error_code;
        outcome.message = result.error_code == error::kUnsupportedFormat
                              ? "Unsupported barcode format. Please use a Code 128 ID card."
                              : "Invalid barcode. Please try scanning again.";
        enter_detection(detection.text);
        set_state(ScanState::BarcodeInvalid, outcome.message);
        return outcome;
    }

    // Duplicate suppression: the API applies the same policy, this copy stops
    // the UI from flashing the same result 30 times a second (ADR-008).
    const double now = TimeUtil::monotonic_ms();
    const std::string key = "barcode:" + detection.text;
    if (cooldowns_.is_suppressed(key, config_.scanner.cooldown_ms, now)) {
        outcome.result = "DUPLICATE_SCAN";
        outcome.duplicate = true;
        outcome.message = "Duplicate scan ignored - wait " +
                          std::to_string(cooldowns_.remaining_ms(key, now)) + " ms";
        outcome.processing_ms = result.total_ms;
        outcome.detection_ms = result.decode_ms;
        monitor_.record_scan("DUPLICATE_SCAN");
        // A duplicate is discovered once the payload is in hand and before any
        // lookup, so the walk ends in DECODING - the one state the table lets
        // reach DUPLICATE_SCAN without going through the database.
        enter_detection(detection.text);
        set_state(ScanState::Decoding, "checking the payload");
        set_state(ScanState::DuplicateScan, "duplicate suppressed");
        return outcome;
    }
    cooldowns_.block(key, config_.scanner.cooldown_ms, now);
    play_sound(SoundEvent::ScanDetected);
    // Walk the real stages rather than forcing a jump: the table only reaches
    // LOOKING_UP_STUDENT through VALIDATING, and the payload has in fact been
    // detected, decoded and locally validated on the way here.
    enter_detection(detection.text);
    set_state(ScanState::Decoding, "decoding " + detection.text);
    set_state(ScanState::Validating, "checking the payload format");
    set_state(ScanState::LookingUpStudent, "checking record for " + detection.text);

    VerificationResult verification;
    if (config_.scanner.verify_remotely) {
        verification =
            api_.verify(detection.text, detection.format, result.decode_ms, result.total_ms,
                        camera_.device_label());
        monitor_.record_api(verification.transport_ok, verification.total_ms,
                            verification.queued_offline);
    } else {
        // Offline mode: the scanner cannot verify, so it says so honestly rather
        // than pretending the card is valid (see docs/architecture/decisions.md).
        verification.transport_ok = false;
        verification.result = "ERROR";
        verification.error_code = "API_UNAVAILABLE";
        verification.message = "Remote verification is disabled (scanner.verify_remotely=false).";
    }

    outcome.result = verification.result;
    outcome.message = verification.message;
    outcome.student_name = verification.student_name;
    outcome.student_id = verification.student_id;
    outcome.course = verification.course;
    outcome.section = verification.section;
    outcome.year_level = verification.year_level;
    outcome.student_status = verification.status;
    outcome.can_transact = verification.can_transact;
    outcome.duplicate = verification.duplicate;
    outcome.queued_offline = verification.queued_offline;
    outcome.detection_ms = result.decode_ms;
    outcome.database_ms = verification.database_ms;
    outcome.processing_ms = result.total_ms;
    outcome.total_ms = result.total_ms + verification.database_ms;
    monitor_.record_scan(verification.result);

    if (verification.success) {
        set_state(ScanState::StudentFound, "verified " + verification.student_name);
        play_sound(SoundEvent::VerificationSuccess);
        SIDB_LOG(log::Level::Info, kComponent,
                 "Student verified: " + verification.student_id + " (" + verification.student_name +
                     ") total=" + TimeUtil::format_duration(outcome.total_ms));
    } else if (verification.result == "UNKNOWN_ID") {
        set_state(ScanState::StudentNotFound, verification.message);
        play_sound(SoundEvent::UnknownStudent);
    } else if (verification.result == "DUPLICATE_SCAN") {
        set_state(ScanState::DuplicateScan, verification.message);
    } else {
        machine_.set_error(verification.error_code.empty() ? "UNKNOWN_ERROR" : verification.error_code,
                           verification.message);
        set_state(state_for_error(verification.error_code), "verification failed");
        play_sound(SoundEvent::Error);
    }
    outcome.state = machine_.state();
    return outcome;
}

// ---------------------------------------------------------------------------
// overlay / service publishing
// ---------------------------------------------------------------------------
void ScannerEngine::publish_overlay(const cv::Mat& frame, const ScanOutcome& outcome,
                                    const FrameResult& result) {
    int blue = 220, green = 220, red = 220;
    state_colour(machine_.state(), blue, green, red);
    const std::string status =
        outcome.message.empty() ? std::string(display_label(machine_.state())) : outcome.message;

    std::string metrics;
    if (config_.display.show_fps) {
        char buffer[224];
        std::snprintf(buffer, sizeof(buffer), PerformanceMonitor::kBannerFormat,
                      monitor_.snapshot().fps, monitor_.snapshot().avg_processing_ms,
                      monitor_.snapshot().avg_detect_ms, monitor_.snapshot().avg_database_ms,
                      outcome.total_ms);
        metrics = buffer;
    }
    if (config_.display.show_state && !outcome.student_name.empty()) {
        std::string detail = outcome.student_id + "  " + outcome.student_name;
        if (!outcome.course.empty()) detail += "  " + outcome.course;
        if (!outcome.section.empty()) detail += "  Section " + outcome.section;
        if (outcome.can_transact) {
            detail += "  [ACTIVE]";
        } else {
            detail += "  [" + (outcome.student_status.empty() ? std::string("INACTIVE")
                                                               : outcome.student_status) + "]";
        }
        metrics += (metrics.empty() ? "" : "   ") + detail;
    }

    cv::Mat overlay = FrameProcessor::annotate(frame, config_.camera, result.detections, status,
                                               cv::Scalar(blue, green, red), metrics);
    {
        std::lock_guard<std::mutex> lock(overlay_mutex_);
        overlay_ = std::move(overlay);
    }
}

cv::Mat ScannerEngine::latest_overlay() const {
    std::lock_guard<std::mutex> lock(overlay_mutex_);
    return overlay_.empty() ? cv::Mat() : overlay_.clone();
}

std::string ScannerEngine::detections_json(const FrameResult& result) const {
    std::ostringstream out;
    out << '[';
    for (std::size_t i = 0; i < result.detections.size(); ++i) {
        const BarcodeDetection& detection = result.detections[i];
        if (i > 0) out << ',';
        out << "{\"text\":\"" << json_string(detection.text) << "\",\"format\":\""
            << json_string(detection.format) << "\",\"valid\":"
            << (detection.valid ? "true" : "false") << ",\"points\":[";
        for (std::size_t p = 0; p < detection.points.size(); ++p) {
            if (p > 0) out << ',';
            out << '[' << detection.points[p].x << ',' << detection.points[p].y << ']';
        }
        out << "]}";
    }
    out << ']';
    return out.str();
}

void ScannerEngine::publish_service_frame(const cv::Mat& frame, const FrameResult& result,
                                          const ScanOutcome& outcome) {
    if (frame.empty()) return;
    std::vector<uchar> buffer;
    const std::vector<int> params = {cv::IMWRITE_JPEG_QUALITY, config_.display.jpeg_quality};
    if (!cv::imencode(".jpg", frame, buffer, params)) return;
    api_.publish_frame(std::string(reinterpret_cast<const char*>(buffer.data()), buffer.size()),
                       detections_json(result), machine_.error_code(), outcome.message);
}

void ScannerEngine::play_sound(SoundEvent event) { audio_->play(event); }

ScanOutcome ScannerEngine::last_outcome() const {
    std::lock_guard<std::mutex> lock(outcome_mutex_);
    return last_outcome_;
}

std::string ScannerEngine::status_line() const {
    const CameraStatus camera = camera_.status();
    const PerformanceMonitor::Snapshot metrics = monitor_.snapshot();
    std::ostringstream out;
    out << "state=" << to_string(machine_.state()) << "  camera="
        << (camera.open ? camera.device : "closed") << "  " << camera.width << "x" << camera.height
        << "  fps=" << metrics.fps << "  frame=" << round2(metrics.avg_processing_ms) << "ms"
        << "  detect=" << round2(metrics.avg_detect_ms) << "ms"
        << "  db=" << round2(metrics.avg_database_ms) << "ms"
        << "  api=" << (metrics.api_reachable ? "up" : "down")
        << "  verified=" << metrics.scans_verified << "  unknown=" << metrics.scans_unknown
        << "  dup=" << metrics.scans_duplicate;
    if (!machine_.error_code().empty()) {
        out << "  error=" << machine_.error_code() << " (" << machine_.error_message() << ")";
    }
    return out.str();
}

// ---------------------------------------------------------------------------
// single-frame and file replay
// ---------------------------------------------------------------------------
ScanOutcome ScannerEngine::process_one_frame(const cv::Mat& frame) {
    ScanOutcome outcome;
    int blue = 220, green = 220, red = 220;
    state_colour(machine_.state(), blue, green, red);
    const FrameResult result = processor_.process(frame, display_label(machine_.state()),
                                                  cv::Scalar(blue, green, red), false);
    monitor_.record_processing(result.total_ms, result.preprocess_ms, result.detect_ms,
                               result.decode_ms, 0.0, result.total_ms);
    if (result.primary != nullptr && result.primary->valid) {
        outcome = handle_detection(result);
    } else {
        outcome.result = result.error_code;
    }
    {
        std::lock_guard<std::mutex> lock(outcome_mutex_);
        last_outcome_ = outcome;
    }
    return outcome;
}

int ScannerEngine::run_file(const std::string& path, bool show_window, int max_frames) {
    cv::VideoCapture capture;
    cv::Mat still;
    if (cv::imread(path, still).empty()) {
        capture.open(path);
    }
    if (capture.isOpened()) {
        SIDB_LOG(log::Level::Info, kComponent, "replaying video " + path);
    } else if (!still.empty()) {
        SIDB_LOG(log::Level::Info, kComponent, "processing image " + path);
    } else {
        machine_.set_error(error::kFrameCaptureFailed, "Could not open " + path);
        set_state(ScanState::CameraError, "file could not be opened");
        SIDB_LOG(log::Level::Error, kComponent, "could not open " + path);
        return 1;
    }

    int processed = 0;
    const std::string window = window_title_ + " - " + path;
    while (running_.load() || processed == 0) {
        cv::Mat frame;
        if (capture.isOpened()) {
            if (!capture.read(frame)) break;
        } else {
            frame = still;
        }
        const ScanOutcome outcome = process_one_frame(frame);
        if (outcome.decoded) {
            SIDB_LOG(log::Level::Info, kComponent,
                     "decoded " + outcome.barcode + " (" + outcome.format + ") -> " +
                         (outcome.result.empty() ? std::string("NO RESULT") : outcome.result));
        }
        if (show_window) {
            int blue = 220, green = 220, red = 220;
            state_colour(outcome.state, blue, green, red);
            const std::string status =
                outcome.message.empty() ? std::string(display_label(outcome.state)) : outcome.message;
            const cv::Mat overlay =
                latest_overlay().empty()
                    ? frame
                    : latest_overlay();
            cv::imshow(window, overlay);
            if (cv::waitKey(30) >= 0) break;  // any key stops the replay
        }
        ++processed;
        if (max_frames > 0 && processed >= max_frames) break;
        if (capture.isOpened() && frame_limit_ > 0.0) {
            std::this_thread::sleep_for(
                std::chrono::microseconds(static_cast<int>(1000000.0 / frame_limit_)));
        }
    }
    if (show_window) cv::destroyAllWindows();
    return processed;
}

int ScannerEngine::flush_offline_queue() {
    if (!offline_queue_ || !offline_queue_->is_open()) {
        SIDB_LOG(log::Level::Warning, kComponent, "the offline queue is not open; nothing to flush");
        return 0;
    }
    const std::vector<DatabaseClient::PendingScan> pending = offline_queue_->pending(500);
    if (pending.empty()) {
        SIDB_LOG(log::Level::Info, kComponent, "the offline queue is empty");
        return 0;
    }
    int flushed = 0;
    for (const DatabaseClient::PendingScan& scan : pending) {
        const VerificationResult result =
            api_.verify(scan.barcode_value, scan.barcode_type, scan.detection_ms,
                        scan.processing_ms, scan.device_name);
        if (result.transport_ok()) {
            offline_queue_->remove(scan.id);
            ++flushed;
            SIDB_LOG(log::Level::Info, kComponent,
                     "flushed queued scan " + scan.barcode_value + " -> " + result.result);
        } else {
            SIDB_LOG(log::Level::Warning, kComponent,
                     "still cannot reach the API; leaving " +
                         std::to_string(pending.size() - flushed) + " scans queued");
            break;
        }
    }
    return flushed;
}

void ScannerEngine::run_window(const std::string& window_title) {
    window_title_ = window_title;
    if (mode_ != EngineMode::Native) return;

    const std::string window = window_title_;
    bool quit = false;
    while (running_.load() && !quit) {
        const cv::Mat overlay = latest_overlay();
        if (!overlay.empty()) {
            cv::imshow(window, overlay);
        }
        const int key = cv::waitKey(interactive_ ? 1 : 5) & 0xFF;
        if (interactive_) {
            if (key == 'q' || key == 27) {
                quit = true;                                   // q or ESC
            } else if (key == 'c') {
                cooldowns_.clear();
                SIDB_LOG(log::Level::Info, kComponent, "cooldown map cleared by the operator");
            } else if (key == 's') {
                const cv::Mat frame = latest_overlay();
                if (!frame.empty()) {
                    const std::string path = "logs/frame-" + TimeUtil::now_iso8601() + ".jpg";
                    cv::imwrite(path, frame);
                    SIDB_LOG(log::Level::Info, kComponent, "frame saved to " + path);
                }
            } else if (key == 'i') {
                const CameraStatus status = camera_.status();
                int width = status.width > 0 ? status.width : config_.camera.width;
                int height = status.height > 0 ? status.height : config_.camera.height;
                const int next_width = (width >= config_.camera.width) ? 640 : config_.camera.width;
                const int next_height = (next_width == 640) ? 480 : config_.camera.height;
                if (camera_.apply_resolution(next_width, next_height, config_.camera.fps)) {
                    config_.camera.width = next_width;
                    config_.camera.height = next_height;
                    SIDB_LOG(log::Level::Info, kComponent,
                             "resolution toggled to " + std::to_string(next_width) + "x" +
                                 std::to_string(next_height));
                }
            }
        }
        if (cv::getWindowProperty(window, cv::WND_PROP_VISIBLE) < 0) {
            quit = true;  // the window was closed
        }
    }
    cv::destroyAllWindows();
}

}  // namespace sidb
