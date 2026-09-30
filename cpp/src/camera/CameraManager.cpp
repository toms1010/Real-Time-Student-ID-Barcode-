// ===========================================================================
//  CameraManager.cpp
// ===========================================================================
#include "camera/CameraManager.h"

#include <algorithm>
#include <cstdio>
#include <dirent.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include "utils/Logger.hpp"

namespace sidb {

namespace {
constexpr const char* kComponent = "camera";

std::vector<std::string> list_video_nodes() {
    std::vector<std::string> nodes;
    DIR* dir = ::opendir("/dev");
    if (dir == nullptr) return nodes;
    while (const dirent* entry = ::readdir(dir)) {
        const std::string name = entry->d_name;
        if (name.rfind("video", 0) == 0) {
            nodes.push_back("/dev/" + name);
        }
    }
    ::closedir(dir);
    // Sort numerically so video10 comes after video2.
    std::sort(nodes.begin(), nodes.end(), [](const std::string& a, const std::string& b) {
        const int na = std::atoi(a.c_str() + 6);
        const int nb = std::atoi(b.c_str() + 6);
        return na == nb ? a < b : na < nb;
    });
    return nodes;
}

/// True when the process may read and write the device node.
bool has_device_access(const std::string& path) {
    return ::access(path.c_str(), R_OK | W_OK) == 0;
}

/// True when some *other* process already holds the device.
bool device_is_busy(const std::string& path) {
    // Non-blocking exclusive open: succeeds only when nobody else holds it.
    const int fd = ::open(path.c_str(), O_RDWR | O_NONBLOCK);
    if (fd < 0) {
        return true;  // EACCES or EBUSY - treat as unavailable
    }
    ::close(fd);
    return false;
}

double now_ms() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double, std::milli>(clock::now().time_since_epoch()).count();
}

}  // namespace

CameraManager::CameraManager(const CameraConfig& config) : config_(config) {}

CameraManager::~CameraManager() { close(); }

std::vector<std::string> CameraManager::available_devices() { return list_video_nodes(); }

std::string CameraManager::device_path(int index) {
    const std::vector<std::string> nodes = list_video_nodes();
    if (index < 0 || index >= static_cast<int>(nodes.size())) return {};
    return nodes[static_cast<std::size_t>(index)];
}

void CameraManager::note_error(const char* code, const std::string& message) {
    last_error_ = code;
    last_error_message_ = message;
    SIDB_LOG(log::Level::Error, kComponent, message + " [" + code + "]");
}

bool CameraManager::open() {
    std::lock_guard<std::mutex> lock(mutex_);
    close();

    if (!config_.source.empty()) {
        device_ = config_.source;
    } else {
        const std::string node = device_path(config_.device_index);
        if (node.empty()) {
            note_error(error::kCameraNotFound,
                       "No camera found at index " + std::to_string(config_.device_index) +
                           ". Connected devices: " + std::to_string(available_devices().size()));
            return false;
        }
        if (!has_device_access(node)) {
            note_error(error::kCameraPermissionDenied,
                       "No read/write permission on " + node +
                           ". Add your user to the 'video' group and log in again.");
            return false;
        }
        device_ = node;
    }

    capture_.reset(new cv::VideoCapture());
    bool opened = false;
    if (!config_.source.empty()) {
        opened = capture_->open(config_.source, cv::CAP_FFMPEG);
        if (!opened) opened = capture_->open(config_.source);
    } else {
        opened = capture_->open(device_, cv::CAP_V4L2);
        if (!opened) opened = capture_->open(device_);
    }

    if (!opened) {
        if (!config_.source.empty()) {
            note_error(error::kCameraNotFound, "Could not open the source " + config_.source);
        } else if (device_is_busy(device_)) {
            note_error(error::kCameraInUse,
                       "The camera " + device_ +
                           " is already in use by another process. Stop the other scanner or set "
                           "scanner.stream_source in config.yaml.");
        } else {
            note_error(error::kCameraNotFound, "Could not open the camera " + device_);
        }
        capture_.reset();
        return false;
    }

    capture_->set(cv::CAP_PROP_FRAME_WIDTH, config_.width);
    capture_->set(cv::CAP_PROP_FRAME_HEIGHT, config_.height);
    capture_->set(cv::CAP_PROP_FPS, config_.fps);
    capture_->set(cv::CAP_PROP_BUFFERSIZE, 1);  // low latency
    for (int i = 0; i < config_.warmup_frames; ++i) {
        cv::Mat warmup;
        capture_->read(warmup);  // let auto-exposure / auto-focus settle
    }
    read_properties();
    last_error_.clear();
    last_error_message_.clear();

    SIDB_LOG(log::Level::Info, kComponent,
             "Camera initialized: " + device_label() + " " + std::to_string(width_) + "x" +
                 std::to_string(height_) + " @" + std::to_string(static_cast<int>(fps_)) + "fps");
    return true;
}

void CameraManager::close() {
    if (capture_) {
        capture_.reset();
        SIDB_LOG(log::Level::Info, kComponent, "Camera closed");
    }
}

std::string CameraManager::read(Frame& frame) {
    if (!capture_) {
        note_error(error::kCameraNotFound, "The camera is not open");
        return last_error_;
    }

    const double started = now_ms();
    cv::Mat image;
    const bool ok = capture_->read(image);
    const double waited = now_ms() - started;

    if (!ok || image.empty()) {
        std::lock_guard<std::mutex> lock(mutex_);
        ++frames_failed_;
        note_error(error::kCameraDisconnected,
                   "Frame capture failed after " + std::to_string(static_cast<int>(waited)) +
                       " ms from " + device_ + " (disconnected?)");
        return last_error_;
    }
    if (config_.flip) {
        cv::flip(image, image, 1);
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        ++frames_captured_;
        frame.sequence = frames_captured_;
        last_error_.clear();
        last_error_message_.clear();
    }
    frame.image = std::move(image);
    frame.timestamp_ms = now_ms();
    return {};
}

void CameraManager::read_properties() {
    if (!capture_) return;
    width_ = static_cast<int>(capture_->get(cv::CAP_PROP_FRAME_WIDTH));
    height_ = static_cast<int>(capture_->get(cv::CAP_PROP_FRAME_HEIGHT));
    fps_ = capture_->get(cv::CAP_PROP_FPS);
    if (width_ <= 0) width_ = config_.width;
    if (height_ <= 0) height_ = config_.height;
    if (fps_ <= 0.0) fps_ = config_.fps;
}

CameraStatus CameraManager::status() const {
    std::lock_guard<std::mutex> lock(mutex_);
    CameraStatus status;
    status.open = capture_ != nullptr;
    status.device = device_;
    status.width = width_;
    status.height = height_;
    status.fps = fps_;
    status.frames_captured = frames_captured_;
    status.frames_failed = frames_failed_;
    status.last_error = last_error_;
    status.last_error_message = last_error_message_;
    return status;
}

std::string CameraManager::device_label() const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (device_.empty()) return "no-camera";
    const std::size_t slash = device_.find_last_of('/');
    return slash == std::string::npos ? device_ : device_.substr(slash + 1);
}

bool CameraManager::apply_resolution(int width, int height, int fps) {
    if (!capture_) return false;
    if (capture_->set(cv::CAP_PROP_FRAME_WIDTH, width) &&
        capture_->set(cv::CAP_PROP_FRAME_HEIGHT, height)) {
        config_.width = width;
        config_.height = height;
        if (fps > 0) {
            capture_->set(cv::CAP_PROP_FPS, fps);
            config_.fps = fps;
        }
        read_properties();
        SIDB_LOG(log::Level::Info, kComponent,
                 "Capture resolution changed to " + std::to_string(width_) + "x" +
                     std::to_string(height_));
        return true;
    }
    return false;
}

}  // namespace sidb
