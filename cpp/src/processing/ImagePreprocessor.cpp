// ===========================================================================
//  ImagePreprocessor.cpp
// ===========================================================================
#include "processing/ImagePreprocessor.h"

#include <algorithm>
#include <cmath>
#include <numeric>

#include <opencv2/imgproc.hpp>

#include "utils/Logger.hpp"

namespace sidb {

namespace {

constexpr const char* kComponent = "preprocess";

double now_ms() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double, std::milli>(clock::now().time_since_epoch()).count();
}

/// Separation between the dark and the light significant modes of a histogram.
double peak_separation(const std::vector<double>& probability) {
    std::vector<double> significant;
    const double peak = *std::max_element(probability.begin(), probability.end());
    if (peak <= 0.0) return 0.0;
    for (const double value : probability) {
        if (value > peak * 0.15) significant.push_back(value);
    }
    if (significant.size() < 2) return 0.0;
    const std::size_t half = significant.size() / 2;
    const double dark = std::accumulate(significant.begin(), significant.begin() + half, 0.0) /
                        static_cast<double>(half);
    const double light =
        std::accumulate(significant.begin() + half, significant.end(), 0.0) /
        static_cast<double>(significant.size() - half);
    return std::max(0.0, light - dark);
}

int odd(int value) { return (value % 2 == 0) ? value + 1 : value; }

}  // namespace

ImagePreprocessor::ImagePreprocessor(const PreprocessingConfig& config, const CameraConfig& camera)
    : config_(config), camera_(camera) {}

cv::Mat ImagePreprocessor::to_gray(const cv::Mat& input) {
    if (input.empty()) return {};
    if (input.channels() == 1) return input;
    cv::Mat gray;
    if (input.channels() == 4) {
        cv::cvtColor(input, gray, cv::COLOR_BGRA2GRAY);
    } else {
        cv::cvtColor(input, gray, cv::COLOR_BGR2GRAY);
    }
    return gray;
}

cv::Rect ImagePreprocessor::roi_for(const cv::Mat& frame, const CameraConfig& camera) {
    const int width = frame.cols;
    const int height = frame.rows;
    int x = static_cast<int>(std::max(0.0, camera.roi_x) * width);
    int y = static_cast<int>(std::max(0.0, camera.roi_y) * height);
    int w = static_cast<int>(std::max(0.0, camera.roi_w) * width);
    int h = static_cast<int>(std::max(0.0, camera.roi_h) * height);
    x = std::clamp(x, 0, std::max(0, width - 1));
    y = std::clamp(y, 0, std::max(0, height - 1));
    w = std::clamp(w, 1, width - x);
    h = std::clamp(h, 1, height - y);
    return cv::Rect(x, y, w, h);
}

ImagePreprocessor::Statistics ImagePreprocessor::measure(const cv::Mat& gray) {
    Statistics stats;
    if (gray.empty()) return stats;
    cv::Mat scalar_mean;
    cv::mean(gray, scalar_mean);
    stats.mean_luminance = scalar_mean.at<double>(0) / 255.0;

    cv::Mat laplacian;
    cv::Laplacian(gray, laplacian, CV_64F);
    cv::Scalar variance;
    cv::meanStdDev(laplacian, cv::noArray(), variance);
    stats.noise_energy = variance[0] * variance[0];

    int histogram[256] = {0};
    const std::size_t sample_step = std::max<std::size_t>(1, gray.total() / 40000);
    std::size_t sampled = 0;
    for (std::size_t i = 0; i < gray.total(); i += sample_step) {
        ++histogram[static_cast<int>(gray.ptr<unsigned char>(0)[i])];
        ++sampled;
    }
    if (sampled == 0) return stats;
    std::vector<double> probability(256, 0.0);
    for (int i = 0; i < 256; ++i) probability[i] = static_cast<double>(histogram[i]) / sampled;
    stats.bimodality = peak_separation(probability);
    return stats;
}

PreprocessReport ImagePreprocessor::process(const cv::Mat& input, cv::Mat& output,
                                            bool aggressive) const {
    const double started = now_ms();
    PreprocessReport report;
    if (input.empty()) {
        output = input;
        return report;
    }

    cv::Mat image = input;

    // 1. region of interest -------------------------------------------------
    if (config_.enable_roi_crop) {
        const cv::Rect roi = roi_for(image, camera_);
        const cv::Rect full(0, 0, image.cols, image.rows);
        if (roi != full) {
            image = image(roi).clone();
            report.steps.emplace_back("roi_crop");
        }
    }

    // 2. resize -------------------------------------------------------------
    if (config_.target_width > 0 && image.cols > config_.target_width) {
        const double scale = static_cast<double>(config_.target_width) / image.cols;
        const cv::Size target(config_.target_width,
                              std::max(1, static_cast<int>(image.rows * scale)));
        cv::Mat resized;
        cv::resize(image, resized, target, 0, 0, cv::INTER_AREA);
        image = resized;
        report.steps.emplace_back("resize");
    }

    // 3. grayscale ----------------------------------------------------------
    if (config_.enable_grayscale && image.channels() != 1) {
        image = to_gray(image);
        report.steps.emplace_back("grayscale");
    }

    const Statistics stats = measure(image);
    report.mean_luminance = stats.mean_luminance;
    report.noise_energy = stats.noise_energy;
    report.bimodality = stats.bimodality;

    // 4. contrast, gated on darkness ---------------------------------------
    if (config_.enable_clahe && stats.mean_luminance < config_.clahe_dark_trigger) {
        cv::Ptr<cv::CLAHE> clahe = cv::createCLAHE(
            config_.clahe_clip_limit, cv::Size(8, 8));
        clahe->apply(image, image);
        report.steps.emplace_back("clahe");
    }

    // 5. denoise, gated on noise energy ------------------------------------
    if (config_.enable_denoise && stats.noise_energy > config_.denoise_trigger &&
        stats.mean_luminance < 0.75) {
        const int h = std::max(1, static_cast<int>(config_.denoise_h));
        cv::fastNlMeansDenoising(image, image, h, std::max(5, h));
        report.steps.emplace_back("denoise");
    }

    // 6. adaptive threshold, gated on bimodality or an explicit retry -------
    const bool want_threshold = config_.enable_adaptive_threshold || aggressive;
    if (want_threshold && (aggressive || stats.bimodality >= config_.adaptive_bimodal_threshold)) {
        cv::adaptiveThreshold(image, image, 255, cv::ADAPTIVE_THRESH_GAUSSIAN_C, cv::THRESH_BINARY,
                              odd(std::max(3, config_.adaptive_block_size)), config_.adaptive_c);
        report.steps.emplace_back("adaptive_threshold");
    }

    // 7. sharpen (off by default - it is only useful on a very soft focus) --
    if (config_.enable_sharpen) {
        cv::Mat blurred;
        cv::GaussianBlur(image, blurred, cv::Size(0, 0), 1.0);
        cv::addWeighted(image, 1.0 + config_.sharpen_amount, blurred, -config_.sharpen_amount, 0,
                        image);
        report.steps.emplace_back("sharpen");
    }

    output = std::move(image);
    report.total_ms = now_ms() - started;
    report.width = output.cols;
    report.height = output.rows;
    report.channels = output.channels();
    return report;
}

}  // namespace sidb
