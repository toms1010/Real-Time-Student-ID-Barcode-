// ===========================================================================
//  BarcodeDecoder.h - ZXing-C++ wrapper
//
//  Thin, testable adapter over the ZXing-C++ library (the same decoder the
//  Python service links through the `zxing-cpp` wheel).  Responsibilities:
//
//  * map a configuration string ("Code128,QRCode") onto ZXing's format mask
//  * run the decode with rotation / downscale / invert trials enabled
//  * convert ZXing positions into frame coordinates for the overlay
//  * validate the payload against the configured pattern and the symbology
//    allow-list *before* anything is sent to the API
//
//  It never fabricates a result: no detection means
//  error code BARCODE_NOT_DETECTED.
// ===========================================================================
#ifndef SIDB_BARCODE_BARCODE_DECODER_H
#define SIDB_BARCODE_BARCODE_DECODER_H

#include <string>
#include <vector>

#include <opencv2/core.hpp>

namespace sidb {

namespace error {
inline constexpr const char* kBarcodeNotDetected = "BARCODE_NOT_DETECTED";
inline constexpr const char* kBarcodeDecodeFailed = "BARCODE_DECODE_FAILED";
inline constexpr const char* kInvalidBarcode     = "INVALID_BARCODE";
inline constexpr const char* kUnsupportedFormat  = "UNSUPPORTED_FORMAT";
inline constexpr const char* kFrameCaptureFailed = "FRAME_CAPTURE_FAILED";
}  // namespace error

/// One decoded symbol.
struct BarcodeDetection {
    std::string text;
    std::string format;            ///< canonical name, e.g. "Code128"
    std::string symbology;         ///< raw ZXing symbology string
    bool valid = false;
    std::string error_type;        ///< ZXing ErrorType name, empty when valid
    float orientation = 0.0f;      ///< degrees
    std::vector<cv::Point> points; ///< quadrilateral, frame coordinates
    bool empty() const { return text.empty(); }
};

struct DecodeOutcome {
    std::vector<BarcodeDetection> detections;
    std::string error_code;        ///< empty on success
    double duration_ms = 0.0;
    int attempts = 0;
    bool used_preprocessed = false;

    bool found() const { return !detections.empty(); }
    bool any_valid() const;
    const BarcodeDetection* best() const;
};

class BarcodeDecoder {
public:
    /// `allowed_formats` is the comma separated list from the configuration,
    /// e.g. "Code128,Code39,EAN13".  Anything decoded outside that list is
    /// reported as UNSUPPORTED_FORMAT instead of being silently ignored.
    explicit BarcodeDecoder(const std::string& allowed_formats = "Code128,Code39,EAN13,EAN8,UPCA,"
                                                                "UPCE,ITF,QRCode,DataMatrix",
                            bool try_rotate = true, bool try_invert = true,
                            bool try_downscale = true);

    /// Decode every symbol in `image` (grayscale or BGR).
    DecodeOutcome decode(const cv::Mat& image) const;

    /// Translate a ZXing format name into the canonical spelling the API and
    /// the configuration use ("QR Code" -> "QRCode").
    static std::string canonical_format(const std::string& raw);

    /// True when `format` is present in a comma separated allow-list.
    static bool is_allowed_format(const std::string& format, const std::string& allow_list);

    /// True when the payload matches the configured regular expression.
    static bool matches_payload_pattern(const std::string& payload, const std::string& pattern);

    const std::string& format_mask_string() const { return mask_string_; }

private:
    std::string mask_string_;
    std::string canonical_mask_;   ///< normalised, for diagnostics
};

}  // namespace sidb

#endif  // SIDB_BARCODE_BARCODE_DECODER_H
