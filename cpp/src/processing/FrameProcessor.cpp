// ===========================================================================
//  FrameProcessor.cpp
// ===========================================================================
#include "processing/FrameProcessor.h"

#include <algorithm>
#include <cctype>

#include <opencv2/imgproc.hpp>

#include "utils/Logger.hpp"
#include "utils/TimeUtil.h"

namespace sidb {

namespace {

constexpr const char* kComponent = "frame";

std::string trim(const std::string& value) {
    const auto begin = value.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) return {};
    const auto end = value.find_last_not_of(" \t\r\n");
    return value.substr(begin, end - begin + 1);
}

}  // namespace

FrameProcessor::FrameProcessor(const Config& config)
    : config_(config),
      preprocessor_(config.preprocessing, config.camera),
      decoder_(config.scanner.allowed_formats),
      detector_(BarcodeDetector::Options{}) {}

std::string FrameProcessor::validate_payload(const std::string& payload, const ScannerConfig& config,
                                            const std::string& barcode_type) {
    if (trim(payload).empty()) return error::kInvalidBarcode;
    if (payload.size() > 128) return error::kInvalidBarcode;
    for (const char c : payload) {
        if (static_cast<unsigned char>(c) < 0x20 || c == 0x7F) return error::kInvalidBarcode;
    }
    if (!BarcodeDecoder::is_allowed_format(barcode_type, config.allowed_formats)) {
        return error::kUnsupportedFormat;
    }
    if (!BarcodeDecoder::matches_payload_pattern(payload, config.payload_pattern)) {
        return error::kInvalidBarcode;
    }
    return {};
}

cv::Mat FrameProcessor::annotate(const cv::Mat& frame, const CameraConfig& camera,
                                 const std::vector<BarcodeDetection>& detections,
                                 const std::string& status_text, const cv::Scalar& status_colour,
                                 const std::string& metrics_text) {
    cv::Mat canvas = frame.clone();
    if (canvas.empty()) return canvas;

    // --- region of interest -------------------------------------------------
    const cv::Rect roi = ImagePreprocessor::roi_for(canvas, camera);
    cv::rectangle(canvas, roi, cv::Scalar(230, 200, 60), 2);
    if (roi.y > 26) {
        cv::putText(canvas, "SCAN AREA", cv::Point(roi.x + 8, roi.y - 10),
                    cv::FONT_HERSHEY_SIMPLEX, 0.55, cv::Scalar(230, 200, 60), 1, cv::LINE_AA);
    }

    // --- detection polygons ------------------------------------------------
    for (const BarcodeDetection& detection : detections) {
        if (detection.points.size() < 3) continue;
        const cv::Scalar colour = detection.valid ? cv::Scalar(90, 210, 90)
                                                  : cv::Scalar(70, 70, 235);
        std::vector<cv::Point> points = detection.points;
        cv::polylines(canvas, points, true, colour, 2, cv::LINE_AA);

        if (!detection.text.empty()) {
            std::string label = detection.valid ? detection.text
                                                : detection.text + " (" + detection.error_type + ")";
            int baseline = 0;
            const cv::Size text_size =
                cv::getTextSize(label, cv::FONT_HERSHEY_SIMPLEX, 0.5, 1, &baseline);
            const cv::Point anchor = detection.points.front();
            const int top = std::max(0, anchor.y - text_size.height - 6);
            cv::rectangle(canvas,
                          cv::Rect(anchor.x, top, text_size.width + 8, text_size.height + 6),
                          colour, cv::FILLED);
            cv::putText(canvas, label, cv::Point(anchor.x + 4, top + text_size.height),
                        cv::FONT_HERSHEY_SIMPLEX, 0.5, cv::Scalar(20, 20, 20), 1, cv::LINE_AA);
        }
    }

    // --- status banner -----------------------------------------------------
    if (!status_text.empty()) {
        int baseline = 0;
        const cv::Size text_size =
            cv::getTextSize(status_text, cv::FONT_HERSHEY_SIMPLEX, 0.7, 2, &baseline);
        cv::rectangle(canvas, cv::Rect(0, 0, text_size.width + 28, text_size.height + 18),
                      cv::Scalar(25, 25, 25), cv::FILLED);
        cv::putText(canvas, status_text, cv::Point(12, text_size.height + 4),
                    cv::FONT_HERSHEY_SIMPLEX, 0.7, status_colour, 2, cv::LINE_AA);
    }

    // --- metrics line ------------------------------------------------------
    if (!metrics_text.empty()) {
        int baseline = 0;
        const cv::Size text_size =
            cv::getTextSize(metrics_text, cv::FONT_HERSHEY_SIMPLEX, 0.5, 1, &baseline);
        const int y = canvas.rows - 10;
        cv::rectangle(canvas,
                      cv::Rect(0, y - text_size.height - 8, text_size.width + 20,
                               text_size.height + 12),
                      cv::Scalar(25, 25, 25), cv::FILLED);
        cv::putText(canvas, metrics_text, cv::Point(10, y - 2), cv::FONT_HERSHEY_SIMPLEX, 0.5,
                    cv::Scalar(225, 225, 225), 1, cv::LINE_AA);
    }
    return canvas;
}

FrameResult FrameProcessor::process(const cv::Mat& frame, const std::string& status_text,
                                    const cv::Scalar& status_colour, bool draw_overlay) const {
    FrameResult result;
    const double started = TimeUtil::monotonic_ms();
    if (frame.empty()) {
        result.error_code = error::kFrameCaptureFailed;
        return result;
    }

    // --- 1. adaptive preprocessing ----------------------------------------
    cv::Mat prepared;
    result.preprocess = preprocessor_.process(frame, prepared, /*aggressive=*/false);
    result.preprocess_ms = result.preprocess.total_ms;

    // --- 2. cheap localisation (candidates + early-out) -------------------
    if (config_.scanner.detect_roi || true) {
        result.detect = detector_.detect(frame, config_.camera);
        result.detect_ms = result.detect.duration_ms;
    }

    // --- 3. decode ---------------------------------------------------------
    result.decode = decoder_.decode(prepared);
    result.decode_ms = result.decode.duration_ms;
    if (!result.decode.found() && config_.scanner.decode_attempts > 1) {
        // Second attempt with the aggressive path (adaptive thresholding).
        cv::Mat aggressive;
        const PreprocessReport report = preprocessor_.process(frame, aggressive, /*aggressive=*/true);
        const DecodeOutcome retry = decoder_.decode(aggressive);
        if (retry.found()) {
            result.decode = retry;
            result.used_second_attempt = true;
            result.preprocess = report;
            result.preprocess_ms = report.total_ms;
        }
    }
    result.detections = result.decode.detections;
    result.primary = result.decode.best();

    // --- 4. local validation ----------------------------------------------
    if (result.primary != nullptr && result.primary->valid) {
        result.error_code = validate_payload(result.primary->text, config_.scanner,
                                             result.primary->format);
        if (!result.error_code.empty()) {
            // The payload is unusable, but the symbol *was* read: keep the
            // detection for the overlay and report the reason.
            SIDB_LOG(log::Level::Info, kComponent,
                     "payload rejected (" + result.error_code + "): " + result.primary->text);
        }
    } else if (result.decode.error_code != nullptr && !result.decode.error_code.empty()) {
        result.error_code = result.decode.error_code;
    }

    result.total_ms = TimeUtil::monotonic_ms() - started;
    if (draw_overlay && config_.display.draw_overlay) {
        result.annotated = annotate(frame, config_.camera, result.detections, status_text,
                                    status_colour, {});
    } else {
        result.annotated = frame;
    }
    return result;
}

}  // namespace sidb
