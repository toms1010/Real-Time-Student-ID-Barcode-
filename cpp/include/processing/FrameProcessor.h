// ===========================================================================
//  FrameProcessor.h - one frame in, one annotated result out
//
//  The pipeline the brief specifies:
//
//      frame -> ROI -> resize -> grayscale -> [CLAHE] -> [denoise] -> [threshold]
//            -> detect -> decode -> validate
//
//  FrameProcessor owns that chain and produces the timings the overlay prints
//  and the scan log stores, so ScannerEngine stays a scheduler rather than an
//  image-processing class.  It is stateless between frames apart from the
//  `previous frame failed to decode` hint that switches the aggressive path on.
// ===========================================================================
#ifndef SIDB_PROCESSING_FRAME_PROCESSOR_H
#define SIDB_PROCESSING_FRAME_PROCESSOR_H

#include <string>
#include <vector>

#include <opencv2/core.hpp>

#include "barcode/BarcodeDecoder.h"
#include "barcode/BarcodeDetector.h"
#include "processing/ImagePreprocessor.h"
#include "utils/Config.h"

namespace sidb {

/// Everything the engine and the overlay need about one processed frame.
struct FrameResult {
    cv::Mat annotated;                 ///< frame + ROI + detection boxes + status
    std::vector<BarcodeDetection> detections;
    const BarcodeDetection* primary = nullptr;   ///< best valid detection, or null
    PreprocessReport preprocess;
    DetectionResult detect;
    DecodeOutcome decode;

    double preprocess_ms = 0.0;
    double detect_ms = 0.0;
    double decode_ms = 0.0;
    double total_ms = 0.0;             ///< capture-to-result, excluding the API call
    std::string error_code;            ///< empty when a valid symbol was decoded
    bool used_second_attempt = false;
};

class FrameProcessor {
public:
    FrameProcessor(const Config& config);

    /// Process a captured frame.
    ///
    /// `status_text` / `status_colour` are drawn in the banner; `draw_overlay`
    /// can be turned off for benchmarking so no time is spent on drawing.
    FrameResult process(const cv::Mat& frame, const std::string& status_text,
                        const cv::Scalar& status_colour, bool draw_overlay = true) const;

    /// Draw only the overlay (used by the engine to re-stamp a cached frame).
    static cv::Mat annotate(const cv::Mat& frame, const CameraConfig& camera,
                            const std::vector<BarcodeDetection>& detections,
                            const std::string& status_text, const cv::Scalar& status_colour,
                            const std::string& metrics_text);

    /// Validate a decoded payload locally, before any network call.
    /// Returns an empty string when the payload is acceptable, otherwise the
    /// error code to record.
    static std::string validate_payload(const std::string& payload, const ScannerConfig& config,
                                        const std::string& barcode_type);

private:
    Config config_;
    ImagePreprocessor preprocessor_;
    BarcodeDecoder decoder_;
    BarcodeDetector detector_;
};

}  // namespace sidb

#endif  // SIDB_PROCESSING_FRAME_PROCESSOR_H
