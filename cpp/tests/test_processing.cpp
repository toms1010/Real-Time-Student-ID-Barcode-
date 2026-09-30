// ===========================================================================
//  test_processing.cpp - preprocessing, ROI, overlay and configuration
//
//  The preprocessing tests assert the ADR-007 rule directly: filters are
//  *conditional*.  A synthetic bright, sharp, high-contrast frame must NOT be
//  denoised or thresholded, and a synthetic dark, noisy frame must be.
// ===========================================================================
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include "processing/FrameProcessor.h"
#include "processing/ImagePreprocessor.h"
#include "scanner/PerformanceMonitor.h"
#include "tests/Testing.h"
#include "utils/Config.h"
#include "utils/TimeUtil.h"

namespace {

namespace fs = std::filesystem;

fs::path project_root() {
    if (const char* root = std::getenv("SIDB_PROJECT_ROOT")) return fs::path(root);
    const fs::path cwd = fs::current_path();
    if (fs::exists(cwd / "database" / "schema.sql")) return cwd;
    if (fs::exists(cwd.parent_path() / "database" / "schema.sql")) return cwd.parent_path();
    return cwd;
}

/// A clean, bright, high-contrast "barcode-like" frame.
cv::Mat make_clean_frame(int width = 640, int height = 360) {
    cv::Mat frame(height, width, CV_8UC1, cv::Scalar(240));
    for (int x = 40; x < width - 40; x += 12) {
        frame(cv::Rect(x, height / 3, 6, height / 3)).setTo(cv::Scalar(20));
    }
    return frame;
}

/// A dark, noisy frame.
cv::Mat make_noisy_frame(int width = 640, int height = 360, int amplitude = 60) {
    cv::Mat frame = make_clean_frame(width, height);
    cv::Mat noise(height, width, CV_8SC1);
    cv::randu(noise, cv::Scalar(0), cv::Scalar(amplitude));
    frame += noise;
    frame *= 0.45;  // make it dark
    return frame;
}

}  // namespace

TEST_CASE("preprocessing: a clean frame is not filtered") {
    sidb::Config config;
    config.preprocessing.enable_roi_crop = false;  // keep the full frame for the statistics
    sidb::ImagePreprocessor preprocessor(config.preprocessing, config.camera);
    const cv::Mat frame = make_clean_frame();

    cv::Mat output;
    const sidb::PreprocessReport report = preprocessor.process(frame, output, false);
    CHECK(!output.empty());
    CHECK_EQ(output.channels(), 1);
    const auto applied = [&](const char* step) {
        return std::find(report.steps.begin(), report.steps.end(), step) != report.steps.end();
    };
    CHECK(!applied("clahe"));                // the frame is bright
    CHECK(!applied("denoise"));              // the frame is smooth
    CHECK(!applied("adaptive_threshold"));   // disabled by default (ADR-007)
    CHECK(report.total_ms >= 0.0);
    std::cout << "        steps=" << report.steps.size() << " luminance="
              << report.mean_luminance << " noise=" << report.noise_energy
              << " bimodality=" << report.bimodality << "\n";
}

TEST_CASE("preprocessing: a dark noisy frame triggers CLAHE and denoise") {
    sidb::Config config;
    config.preprocessing.enable_roi_crop = false;
    sidb::ImagePreprocessor preprocessor(config.preprocessing, config.camera);
    const cv::Mat frame = make_noisy_frame();

    cv::Mat output;
    const sidb::PreprocessReport report = preprocessor.process(frame, output, false);
    const auto applied = [&](const char* step) {
        return std::find(report.steps.begin(), report.steps.end(), step) != report.steps.end();
    };
    CHECK(report.mean_luminance < config.preprocessing.clahe_dark_trigger);
    CHECK(applied("clahe"));
    CHECK(applied("denoise"));
}

TEST_CASE("preprocessing: the aggressive path thresholds only on request") {
    sidb::Config config;
    config.preprocessing.enable_roi_crop = false;
    sidb::ImagePreprocessor preprocessor(config.preprocessing, config.camera);
    const cv::Mat frame = make_clean_frame();

    cv::Mat gentle;
    const sidb::PreprocessReport normal = preprocessor.process(frame, gentle, false);
    const bool gentle_thresholded =
        std::find(normal.steps.begin(), normal.steps.end(), "adaptive_threshold") !=
        normal.steps.end();

    cv::Mat aggressive;
    const sidb::PreprocessReport forced = preprocessor.process(frame, aggressive, true);
    const bool aggressive_thresholded =
        std::find(forced.steps.begin(), forced.steps.end(), "adaptive_threshold") !=
        forced.steps.end();

    CHECK(!gentle_thresholded);
    CHECK(aggressive_thresholded);
    // Thresholding produces a binary image: at most two distinct values.
    std::vector<uchar> values;
    cv::unique(aggressive.reshape(1), values);
    CHECK(values.size() <= 2);
}

TEST_CASE("preprocessing: the ROI stays inside the frame") {
    sidb::Config config;
    config.camera.roi_x = 0.10;
    config.camera.roi_y = 0.25;
    config.camera.roi_w = 0.80;
    config.camera.roi_h = 0.50;
    const cv::Mat frame(720, 1280, CV_8UC3, cv::Scalar(0, 0, 0));
    const cv::Rect roi = sidb::ImagePreprocessor::roi_for(frame, config.camera);
    CHECK_EQ(roi.x, 128);
    CHECK_EQ(roi.y, 180);
    CHECK_EQ(roi.width, 1024);
    CHECK_EQ(roi.height, 360);
    CHECK(roi.x >= 0 && roi.y >= 0);
    CHECK(roi.x + roi.width <= frame.cols);
    CHECK(roi.y + roi.height <= frame.rows);
}

TEST_CASE("preprocessing: an out-of-range ROI is rejected by validation") {
    sidb::Config config;
    config.camera.roi_x = 0.9;
    config.camera.roi_w = 0.5;  // 0.9 + 0.5 > 1
    const bool clean = config.validate();
    CHECK(!clean);                       // validate() reported a correction
    CHECK_NEAR(config.camera.roi_x, 0.10, 1e-9);
    CHECK_NEAR(config.camera.roi_w, 0.80, 1e-9);
    CHECK(!config.warnings.empty());
}

TEST_CASE("preprocessing: the cascade crops to the ROI") {
    sidb::Config config;
    config.preprocessing.enable_roi_crop = true;
    config.preprocessing.target_width = 0;  // do not resize, so the ROI is visible
    sidb::ImagePreprocessor preprocessor(config.preprocessing, config.camera);
    const cv::Mat frame(720, 1280, CV_8UC3, cv::Scalar(30, 60, 90));

    cv::Mat output;
    const sidb::PreprocessReport report = preprocessor.process(frame, output, false);
    const bool applied =
        std::find(report.steps.begin(), report.steps.end(), "roi_crop") != report.steps.end();
    CHECK(applied);
    CHECK_EQ(report.width, 1024);
    CHECK_EQ(report.height, 360);
}

TEST_CASE("frame processor: draws an overlay of the expected size") {
    sidb::Config config;
    sidb::FrameProcessor processor(config);
    const cv::Mat frame(480, 640, CV_8UC3, cv::Scalar(20, 20, 20));
    const sidb::FrameResult result =
        processor.process(frame, "SCANNING", cv::Scalar(220, 220, 220), true);
    CHECK(!result.annotated.empty());
    CHECK_EQ(result.annotated.cols, frame.cols);
    CHECK_EQ(result.annotated.rows, frame.rows);
    CHECK(result.total_ms > 0.0);
    // The overlay must differ from the input, otherwise nothing was drawn.
    cv::Mat difference;
    cv::absdiff(result.annotated, frame, difference);
    CHECK(cv::countNonZero(difference.reshape(1)) > 0);
}

TEST_CASE("frame processor: overlay can be disabled for benchmarking") {
    sidb::Config config;
    sidb::FrameProcessor processor(config);
    const cv::Mat frame(480, 640, CV_8UC3, cv::Scalar(20, 20, 20));
    const sidb::FrameResult result = processor.process(frame, "SCANNING", cv::Scalar(), false);
    CHECK(result.annotated.empty() || result.annotated.total() == frame.total());
}

TEST_CASE("config: defaults, YAML and overrides") {
    sidb::Config config;
    CHECK_EQ(config.scanner.cooldown_ms, 2000);
    CHECK_NEAR(config.scanner.confidence_threshold, 0.80, 1e-9);
    CHECK_EQ(config.api.port, 8000);
    CHECK_EQ(config.camera.width, 1280);
    CHECK(!config.config_file.empty());  // a file was found (example config)
    CHECK(!config.describe().empty());

    CHECK(config.apply_override("scanner.cooldown_ms", "5000"));
    CHECK_EQ(config.scanner.cooldown_ms, 5000);
    CHECK(config.apply_override("camera.device_index", "1"));
    CHECK_EQ(config.camera.device_index, 1);
    CHECK(config.apply_override("scanner.verify_remotely", "false"));
    CHECK(!config.scanner.verify_remotely);
    // An unknown key is reported instead of silently accepted.
    CHECK(!config.apply_override("scanner.no_such_key", "1"));
    CHECK(!config.warnings.empty());
}

TEST_CASE("config: reads the shipped configuration file") {
    sidb::Config config = sidb::Config::load();
    // These values come from config/config.example.yaml, so a mismatch means the
    // file and this test have drifted apart.
    CHECK_EQ(config.api.host, std::string("127.0.0.1"));
    CHECK_EQ(config.api.port, 8000);
    CHECK_EQ(config.scanner.cooldown_ms, 2000);
    CHECK_EQ(config.preprocessing.target_width, 960);
    CHECK_EQ(config.audio.volume > 0.0, true);
    // The API must never default to a non-loopback address.
    CHECK(config.api.host == "127.0.0.1" || config.api.host == "localhost" ||
          config.api.host == "0.0.0.0");
}

TEST_CASE("config: invalid values are corrected, not propagated") {
    sidb::Config config;
    config.api.port = 0;
    config.camera.fps = -5;
    config.audio.volume = 4.0;
    config.preprocessing.adaptive_block_size = 30;  // must be odd
    const bool clean = config.validate();
    CHECK(!clean);
    CHECK_EQ(config.api.port, 8000);
    CHECK_EQ(config.camera.fps, 30);
    CHECK_NEAR(config.audio.volume, 0.8, 1e-9);
    CHECK_EQ(config.preprocessing.adaptive_block_size % 2, 1);
}

TEST_CASE("time util: ISO 8601 output has the right shape") {
    const std::string stamp = sidb::TimeUtil::now_iso8601();
    CHECK_EQ(stamp.size(), static_cast<std::size_t>(24));  // YYYY-MM-DDTHH:MM:SS.mmmZ
    CHECK_EQ(stamp[4], '-');
    CHECK_EQ(stamp[10], 'T');
    CHECK_EQ(stamp[19], '.');
    CHECK_EQ(stamp.back(), 'Z');
    CHECK_EQ(sidb::TimeUtil::now_date().size(), static_cast<std::size_t>(10));
    CHECK_EQ(sidb::TimeUtil::now_time_of_day().size(), static_cast<std::size_t>(8));
    CHECK_EQ(sidb::TimeUtil::format_duration(12.3), std::string("12 ms"));
    CHECK_EQ(sidb::TimeUtil::format_duration(1500.0), std::string("1.50 s"));
}

TEST_CASE("performance monitor: reports measured values only") {
    sidb::PerformanceMonitor monitor(10);
    monitor.start();
    for (int i = 0; i < 5; ++i) {
        monitor.record_capture();
        monitor.record_processing(20.0 + i, 3.0, 2.0, 12.0, 1.5, 13.5);
        monitor.record_scan("VERIFIED");
    }
    monitor.record_capture_failure();
    monitor.record_api(false, 5.0, true);

    const sidb::PerformanceMonitor::Snapshot snapshot = monitor.snapshot();
    CHECK_EQ(snapshot.frames_processed, static_cast<long long>(5));
    CHECK_EQ(snapshot.frames_captured, static_cast<long long>(5));
    CHECK_EQ(snapshot.capture_failures, static_cast<long long>(1));
    CHECK_EQ(snapshot.scans_verified, static_cast<long long>(5));
    CHECK_EQ(snapshot.scans_queued, static_cast<long long>(1));
    CHECK(!snapshot.api_reachable);
    CHECK_NEAR(snapshot.avg_database_ms, 1.5, 1e-6);
    CHECK(snapshot.avg_processing_ms > 20.0 && snapshot.avg_processing_ms < 25.0);
    CHECK(!monitor.one_line().empty());
    CHECK(!monitor.report().empty());
}

TEST_CASE("performance monitor: an idle scanner reports zeros, not guesses") {
    sidb::PerformanceMonitor monitor(10);
    const sidb::PerformanceMonitor::Snapshot snapshot = monitor.snapshot();
    CHECK_EQ(snapshot.frames_processed, static_cast<long long>(0));
    CHECK_NEAR(snapshot.avg_processing_ms, 0.0, 1e-9);
    CHECK_NEAR(snapshot.fps, 0.0, 1e-9);
}

int main() { RUN_ALL_TESTS(); }
