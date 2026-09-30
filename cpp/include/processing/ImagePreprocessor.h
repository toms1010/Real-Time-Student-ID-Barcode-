// ===========================================================================
//  ImagePreprocessor.h - adaptive preprocessing cascade (ADR-007)
//
//  No step is applied unconditionally.  Each filter is gated on a measured
//  property of the frame, because blind filtering measurably *hurts* Code 128
//  reads: adaptive thresholding removes the quiet zone, denoising smears bar
//  edges, and CLAHE on a well-lit frame amplifies sensor noise.
//
//  This is the C++ twin of python/app/vision/preprocess.py; both are covered by
//  tests that assert the same step list for the same input.
// ===========================================================================
#ifndef SIDB_PROCESSING_IMAGE_PREPROCESSOR_H
#define SIDB_PROCESSING_IMAGE_PREPROCESSOR_H

#include <string>
#include <vector>

#include <opencv2/core.hpp>

#include "utils/Config.h"

namespace sidb {

/// Which steps actually ran, plus their individual timings.
struct PreprocessReport {
    std::vector<std::string> steps;
    double total_ms = 0.0;
    double mean_luminance = 0.0;   ///< 0..1
    double noise_energy = 0.0;     ///< Laplacian variance
    double bimodality = 0.0;       ///< 0..1, how split the histogram is
    int width = 0;
    int height = 0;
    bool channels = 3;              ///< false when the output is grayscale
};

class ImagePreprocessor {
public:
    ImagePreprocessor(const PreprocessingConfig& config, const CameraConfig& camera);

    /// Run the cascade.  The input is never modified.
    ///
    /// `aggressive` forces the adaptive-threshold path; the scanner sets it for
    /// one frame after a failed decode (the "second attempt"), which is exactly
    /// when extra filtering is worth its cost.
    PreprocessReport process(const cv::Mat& input, cv::Mat& output, bool aggressive = false) const;

    /// Grayscale conversion only - used by the detector for localisation.
    static cv::Mat to_gray(const cv::Mat& input);

    /// Pixel rectangle of the scan region for a frame of this size.
    static cv::Rect roi_for(const cv::Mat& frame, const CameraConfig& camera);

    /// Frame statistics used to decide which filters to run.
    struct Statistics {
        double mean_luminance = 0.0;
        double noise_energy = 0.0;
        double bimodality = 0.0;
    };
    static Statistics measure(const cv::Mat& gray);

    const PreprocessingConfig& config() const { return config_; }

private:
    PreprocessingConfig config_;
    CameraConfig camera_;
};

}  // namespace sidb

#endif  // SIDB_PROCESSING_IMAGE_PREPROCESSOR_H
