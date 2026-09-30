// ===========================================================================
//  BarcodeDetector.h - barcode localisation
//
//  ZXing-C++ does its own localisation internally, so this class is *not* on the
//  critical path for decoding.  It exists for two reasons that the brief asks
//  for explicitly:
//
//  1. Drawing a detection rectangle even when the decode *fails* (so the
//     operator sees "a barcode is here, it is unreadable" instead of "nothing
//     found"), and
//  2. A cheap, allocation-light candidate finder used to decide whether a frame
//     is worth sending to the expensive decoder: a frame with no barcode-shaped
//     contour is skipped, which is what keeps idle FPS high.
//
//  The detector is deliberately conservative: it reports *candidates*, and only
//  the decoder decides whether a candidate is a real symbol.
// ===========================================================================
#ifndef SIDB_BARCODE_BARCODE_DETECTOR_H
#define SIDB_BARCODE_BARCODE_DETECTOR_H

#include <vector>

#include <opencv2/core.hpp>

#include "utils/Config.h"

namespace sidb {

struct Candidate {
    cv::Rect bounds;
    double fill_ratio = 0.0;   ///< ink coverage inside the contour
    bool likely_barcode = false;
};

struct DetectionResult {
    std::vector<Candidate> candidates;
    bool any_candidate = false;
    double duration_ms = 0.0;
};

class BarcodeDetector {
public:
    struct Options {
        int min_width = 120;        ///< px; below this a symbol cannot be read
        int min_height = 24;        ///< px
        int max_candidates = 12;
        double min_aspect = 1.2;    ///< barcodes are wider than tall
        double max_aspect = 6.0;
        double min_fill_ratio = 0.15;
        int canny_low = 60;
        int canny_high = 200;
        bool enabled = true;
    };

    explicit BarcodeDetector(const Options& options = {});

    /// Find barcode-shaped regions in a frame (or in the ROI).
    DetectionResult detect(const cv::Mat& frame, const CameraConfig& camera) const;

    /// True when the frame contains anything worth decoding.  Cheap early-out.
    bool worth_decoding(const cv::Mat& frame, const CameraConfig& camera) const;

    const Options& options() const { return options_; }

private:
    Options options_;
};

}  // namespace sidb

#endif  // SIDB_BARCODE_BARCODE_DETECTOR_H
