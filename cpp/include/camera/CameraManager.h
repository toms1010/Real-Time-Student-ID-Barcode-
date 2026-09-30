// ===========================================================================
//  CameraManager.h - webcam capture with explicit error codes
//
//  Owns a cv::VideoCapture and translates V4L2 failures into the error codes
//  the brief requires.  A plain `VideoCapture::isOpened()` cannot distinguish
//  "device missing" from "permission denied", so the manager inspects the
//  /dev/video* nodes itself and, when the device exists but `open()` failed,
//  distinguishes a permission problem from a busy driver by probing the node.
//
//  RAII: the capture is held in a std::unique_ptr with a custom deleter, so an
//  early return can never leak a device handle.
// ===========================================================================
#ifndef SIDB_CAMERA_CAMERA_MANAGER_H
#define SIDB_CAMERA_CAMERA_MANAGER_H

#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <opencv2/core.hpp>
#include <opencv2/videoio.hpp>

#include "utils/Config.h"

namespace sidb {

/// Error codes shared with the Python service (app/utils/errors.py).
namespace error {
inline constexpr const char* kCameraNotFound        = "CAMERA_NOT_FOUND";
inline constexpr const char* kCameraPermissionDenied = "CAMERA_PERMISSION_DENIED";
inline constexpr const char* kCameraDisconnected     = "CAMERA_DISCONNECTED";
inline constexpr const char* kCameraInUse            = "CAMERA_IN_USE";
inline constexpr const char* kFrameCaptureFailed     = "FRAME_CAPTURE_FAILED";
}  // namespace error

/// A captured frame plus the timings the overlay and the scan log need.
struct Frame {
    cv::Mat image;
    double timestamp_ms = 0.0;   ///< monotonic capture time
    long long sequence = 0;      ///< monotonically increasing frame counter
};

struct CameraStatus {
    bool open = false;
    std::string device;                 ///< /dev/videoN, file path or URL
    int width = 0;
    int height = 0;
    double fps = 0.0;
    long long frames_captured = 0;
    long long frames_failed = 0;
    std::string last_error;             ///< last ErrorCode string
    std::string last_error_message;     ///< operator-facing text
};

class CameraManager {
public:
    explicit CameraManager(const CameraConfig& config);
    ~CameraManager();

    CameraManager(const CameraManager&) = delete;
    CameraManager& operator=(const CameraManager&) = delete;

    /// Open the configured source.  Returns false and sets status().last_error.
    bool open();

    /// Release the device.  Safe to call when already closed.
    void close();

    /// Read one frame.  On failure `frame.image` is empty and the error code is
    /// returned.  A device that stops delivering frames yields
    /// CAMERA_DISCONNECTED so the caller can decide to reconnect.
    std::string read(Frame& frame);

    /// List the /dev/video* nodes present on this machine.
    static std::vector<std::string> available_devices();

    /// Resolve a device index to its node path, or an empty string.
    static std::string device_path(int index);

    bool is_open() const { return capture_ != nullptr; }
    const std::string& device() const { return device_; }
    CameraStatus status() const;

    /// Human-readable device label used in logs and scan_logs.device_name.
    std::string device_label() const;

    /// Reconfigure resolution/framerate on the open device (Settings screen).
    bool apply_resolution(int width, int height, int fps);

private:
    struct CaptureCloser {
        void operator()(cv::VideoCapture* capture) const {
            if (capture != nullptr) {
                capture->release();
                delete capture;
            }
        }
    };
    using CapturePtr = std::unique_ptr<cv::VideoCapture, CaptureCloser>;

    void read_properties();
    void note_error(const char* code, const std::string& message);

    CameraConfig config_;
    CapturePtr capture_;
    std::string device_;
    std::string last_error_;
    std::string last_error_message_;
    mutable std::mutex mutex_;
    long long frames_captured_ = 0;
    long long frames_failed_ = 0;
    int width_ = 0;
    int height_ = 0;
    double fps_ = 0.0;
};

}  // namespace sidb

#endif  // SIDB_CAMERA_CAMERA_MANAGER_H
