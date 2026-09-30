// ===========================================================================
//  ScannerEngine.h - the real-time scanning loop
//
//  Threading (mirrors python/app/vision/pipeline.py):
//
//      capture thread --> FrameSlot --> processing thread --> API --> overlay
//
//  Why two threads: a V4L2 read blocks, and a decode takes tens of
//  milliseconds.  In one thread the device buffer overflows during a slow
//  decode and the driver starts dropping frames.  Splitting them keeps capture
//  at camera rate; the processing thread takes only *new* frames, so a slow
//  decode degrades the overlay refresh rate instead of building a queue.
//
//  Modes (--mode on the command line):
//    native   OpenCV highgui window, fully offline-capable
//    service  no window; publishes annotated JPEG frames to the Python API so
//             the browser dashboard mirrors this scanner (ADR-006)
//    file     replays a video or image, for reproducible tests and demos
// ===========================================================================
#ifndef SIDB_SCANNER_SCANNER_ENGINE_H
#define SIDB_SCANNER_SCANNER_ENGINE_H

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include <opencv2/core.hpp>

#include "api/ApiClient.h"
#include "audio/AudioFeedback.h"
#include "camera/CameraManager.h"
#include "database/DatabaseClient.h"
#include "processing/FrameProcessor.h"
#include "scanner/PerformanceMonitor.h"
#include "scanner/ScannerState.h"
#include "utils/Config.h"

namespace sidb {

enum class EngineMode { Native, Service, File, Benchmark };

/// The last thing the engine did, for the UI and the tests.
struct ScanOutcome {
    bool decoded = false;
    std::string barcode;
    std::string format;
    std::string result;            ///< from the API, or computed locally
    std::string message;
    std::string student_name;
    std::string student_id;
    std::string course;
    std::string section;
    std::string year_level;
    std::string student_status;
    bool can_transact = false;
    bool duplicate = false;
    bool queued_offline = false;
    ScanState state = ScanState::Idle;
    double processing_ms = 0.0;
    double detection_ms = 0.0;
    double database_ms = 0.0;
    double total_ms = 0.0;
};

class ScannerEngine {
public:
    /// `display_window` is honoured only in Native mode; Service mode always
    /// runs headless so it can be started from a script.
    explicit ScannerEngine(const Config& config, EngineMode mode = EngineMode::Native);
    ~ScannerEngine();

    ScannerEngine(const ScannerEngine&) = delete;
    ScannerEngine& operator=(const ScannerEngine&) = delete;

    /// Open the camera (or the file) and start the threads.
    bool start();

    /// Stop the threads and release the camera.  Idempotent.
    void stop();

    /// Run the highgui loop until a key is pressed or the window is closed.
    /// Returns when the user quits.  A no-op outside Native mode.
    void run_window(const std::string& window_title);

    /// Process exactly one frame (used by File mode and by the tests).
    ScanOutcome process_one_frame(const cv::Mat& frame);

    /// Replay every frame of a video/image file, then stop.
    int run_file(const std::string& path, bool show_window, int max_frames = 0);

    /// Replay the offline scan queue to the API.  Returns the number flushed.
    int flush_offline_queue();

    /// Current state and metrics, for the overlay and for the API dump.
    ScanState state() const { return machine_.state(); }
    /// Whether the capture and processing threads are live. Prefer this over
    /// `state()` when the question is "is the engine up?": the workflow rests
    /// in `Idle`, which is also the state before `start()` and after `stop()`.
    bool running() const { return running_.load(); }
    PerformanceMonitor::Snapshot metrics() const { return monitor_.snapshot(); }
    ScanOutcome last_outcome() const;
    std::string status_line() const;

    /// Maximum frames per second, or 0 for "as fast as possible" (benchmarks).
    void set_frame_limit(double fps) { frame_limit_ = fps; }

    void set_interactive(bool interactive) { interactive_ = interactive; }
    void set_window_title(const std::string& title) { window_title_ = title; }
    void set_quit_after_frames(long long frames) { quit_after_frames_ = frames; }

private:
    void capture_loop();
    void processing_loop();
    void process_frame(const cv::Mat& frame, long long sequence);
    ScanOutcome handle_detection(const FrameResult& result);
    void publish_overlay(const cv::Mat& frame, const ScanOutcome& outcome,
                         const FrameResult& result);
    void publish_service_frame(const cv::Mat& frame, const FrameResult& result,
                               const ScanOutcome& outcome);
    std::string detections_json(const FrameResult& result) const;
    void play_sound(SoundEvent event);
    void set_state(ScanState state, const std::string& reason = {});

    /// Bypass the transition table, for teardown paths that must always land
    /// (shutdown, replay finished) regardless of the state we are leaving.
    void force_state(ScanState state, const std::string& reason = {});

    /// Enter BARCODE_DETECTED from wherever the workflow currently is,
    /// returning to SCANNING first when a previous card's result is on screen.
    void enter_detection(const std::string& payload);
    cv::Mat latest_overlay() const;

    Config config_;
    EngineMode mode_;
    CameraManager camera_;
    FrameProcessor processor_;
    ApiClient api_;
    std::unique_ptr<DatabaseClient> offline_queue_;
    std::unique_ptr<AudioFeedback> audio_;
    StateMachine machine_;
    PerformanceMonitor monitor_;
    CooldownTracker cooldowns_;

    // frame hand-off
    mutable std::mutex frame_mutex_;
    cv::Mat pending_frame_;
    long long pending_sequence_ = -1;
    std::condition_variable frame_cv_;

    mutable std::mutex overlay_mutex_;
    cv::Mat overlay_;

    std::atomic<bool> running_{false};
    std::thread capture_thread_;
    std::thread processing_thread_;

    mutable std::mutex outcome_mutex_;
    ScanOutcome last_outcome_;

    std::string window_title_ = "Student ID Barcode Scanner";
    double frame_limit_ = 0.0;
    bool interactive_ = true;
    long long quit_after_frames_ = 0;
};

}  // namespace sidb

#endif  // SIDB_SCANNER_SCANNER_ENGINE_H
