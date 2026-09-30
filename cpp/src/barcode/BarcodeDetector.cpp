// ===========================================================================
//  BarcodeDetector.cpp
// ===========================================================================
#include "barcode/BarcodeDetector.h"

#include <algorithm>
#include <chrono>

#include <opencv2/imgproc.hpp>

#include "processing/ImagePreprocessor.h"
#include "utils/Logger.hpp"

namespace sidb {

namespace {

double now_ms() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double, std::milli>(clock::now().time_since_epoch()).count();
}

}  // namespace

BarcodeDetector::BarcodeDetector(const Options& options) : options_(options) {}

DetectionResult BarcodeDetector::detect(const cv::Mat& frame, const CameraConfig& camera) const {
    DetectionResult result;
    if (frame.empty() || !options_.enabled) return result;

    const double started = now_ms();
    const cv::Rect roi = ImagePreprocessor::roi_for(frame, camera);

    cv::Mat gray;
    if (frame.channels() == 1) {
        gray = frame(roi);
    } else {
        cv::cvtColor(frame(roi), gray, cv::COLOR_BGR2GRAY);
    }
    // Downscale: a 640 px wide copy is plenty to find bar/space transitions and
    // keeps this stage well under a millisecond on 720p input.
    if (gray.cols > 640) {
        const double scale = 640.0 / gray.cols;
        cv::Mat resized;
        cv::resize(gray, resized, cv::Size(640, std::max(1, static_cast<int>(gray.rows * scale))));
        gray = resized;
    }
    cv::GaussianBlur(gray, gray, cv::Size(3, 3), 0);

    cv::Mat edges;
    cv::Canny(gray, edges, options_.canny_low, options_.canny_high);
    // Close 1px gaps so a barcode becomes one contour rather than many bars.
    const cv::Mat kernel =
        cv::getStructuringElement(cv::MORPH_RECT, cv::Size(17, 1));
    cv::morphologyEx(edges, edges, cv::MORPH_CLOSE, kernel);

    std::vector<std::vector<cv::Point>> contours;
    std::vector<cv::Vec4i> hierarchy;
    cv::findContours(edges, contours, hierarchy, cv::RETR_EXTERNAL, cv::CHAIN_APPROX_SIMPLE);

    for (const std::vector<cv::Point>& contour : contours) {
        if (contour.size() < 2) continue;
        const cv::Rect bounds = cv::boundingRect(contour);
        if (bounds.width < options_.min_width || bounds.height < options_.min_height) continue;
        const double aspect = static_cast<double>(bounds.width) / bounds.height;
        if (aspect < options_.min_aspect || aspect > options_.max_aspect) continue;

        // Ink coverage: a real barcode alternates black/white, so the fraction of
        // dark pixels inside the box is roughly 0.4-0.6, never near 0 or 1.
        const cv::Mat patch = gray(bounds);
        cv::Mat binary;
        cv::threshold(patch, binary, 0, 255, cv::THRESH_BINARY_INV | cv::THRESH_OTSU);
        const double fill = cv::countNonZero(binary) / static_cast<double>(binary.total());

        Candidate candidate;
        candidate.bounds = cv::Rect(bounds.x + roi.x, bounds.y + roi.y, bounds.width, bounds.height);
        candidate.fill_ratio = fill;
        candidate.likely_barcode = fill >= options_.min_fill_ratio && fill <= 0.95;
        result.candidates.push_back(candidate);
    }

    std::sort(result.candidates.begin(), result.candidates.end(),
              [](const Candidate& a, const Candidate& b) {
                  if (a.likely_barcode != b.likely_barcode) return a.likely_barcode;
                  return a.bounds.area() > b.bounds.area();
              });
    if (static_cast<int>(result.candidates.size()) > options_.max_candidates) {
        result.candidates.resize(static_cast<std::size_t>(options_.max_candidates));
    }
    result.any_candidate = std::any_of(
        result.candidates.begin(), result.candidates.end(),
        [](const Candidate& c) { return c.likely_barcode; });
    result.duration_ms = now_ms() - started;
    return result;
}

bool BarcodeDetector::worth_decoding(const cv::Mat& frame, const CameraConfig& camera) const {
    if (frame.empty()) return false;
    if (!options_.enabled) return true;  // detection disabled: always try
    // Reuse the contour pass but only look at the top candidate; this is the
    // cheap path used by the decode loop to skip empty frames.
    return detect(frame, camera).any_candidate;
}

}  // namespace sidb
