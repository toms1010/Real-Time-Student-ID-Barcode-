// ===========================================================================
//  BarcodeDecoder.cpp - ZXing-C++ adapter
// ===========================================================================
#include "barcode/BarcodeDecoder.h"

#include <algorithm>
#include <chrono>
#include <regex>

#include <opencv2/imgproc.hpp>
#include <Barcode.h>
#include <BarcodeFormat.h>
#include <ImageView.h>
#include <ReadBarcode.h>
#include <ReaderOptions.h>

#include "utils/Logger.hpp"

namespace sidb {

namespace {

constexpr const char* kComponent = "barcode";

double now_ms() {
    using clock = std::chrono::steady_clock;
    return std::chrono::duration<double, std::milli>(clock::now().time_since_epoch()).count();
}

std::string trim(const std::string& value) {
    const auto begin = value.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) return {};
    const auto end = value.find_last_not_of(" \t\r\n");
    return value.substr(begin, end - begin + 1);
}

/// Collapse a name to a comparable key: "QR Code" and "qrcode" both -> "QRCODE".
std::string normalise_key(const std::string& value) {
    std::string key;
    for (const char c : value) {
        if (std::isalnum(static_cast<unsigned char>(c))) {
            key.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
        }
    }
    return key;
}

/// Map a normalised key to the spelling used by the configuration file.
const char* canonical_for_key(const std::string& key) {
    static const std::pair<const char*, const char*> kTable[] = {
        {"CODE128", "Code128"},
        {"CODE39", "Code39"},
        {"CODE93", "Code93"},
        {"CODABAR", "Codabar"},
        {"ITF", "ITF"},
        {"ITF14", "ITF"},
        {"EAN13", "EAN13"},
        {"EAN8", "EAN8"},
        {"UPCA", "UPCA"},
        {"UPCE", "UPCE"},
        {"QRCODE", "QRCode"},
        {"QRCODE2", "QRCode"},
        {"DATAMATRIX", "DataMatrix"},
        {"DATABAR", "DataBar"},
        {"DATABAROMNI", "DataBarOmni"},
        {"DATABARLTD", "DataBarLtd"},
        {"AZTEC", "Aztec"},
        {"PDF417", "PDF417"},
        {"MICROQRCODE", "MicroQRCode"},
        {"MAXICODE", "MaxiCode"},
        {"RMQRCODE", "RMQRCode"},
        {"PZN", "PZN"},
    };
    for (const auto& [from, to] : kTable) {
        if (key == from) return to;
    }
    return nullptr;
}

/// Convert a BGR/gray OpenCV image into a ZXing image with the right stride.
/// A copy is made when the source is not contiguous so the view is always valid
/// for the duration of the call.
ZXing::ImageView make_view(const cv::Mat& image, cv::Mat& scratch) {
    if (image.isContinuous()) {
        const auto* data = image.ptr<uint8_t>();
        if (image.channels() == 1) {
            return ZXing::ImageView(data, image.cols, image.rows, ZXing::ImageFormat::Lum,
                                    static_cast<int>(image.step));
        }
        if (image.channels() == 3) {
            return ZXing::ImageView(data, image.cols, image.rows, ZXing::ImageFormat::BGR,
                                    static_cast<int>(image.step));
        }
    }
    // Fall back to grayscale, which is what the decoder prefers anyway.
    scratch = image.channels() == 1 ? image.clone() : cv::Mat();
    if (scratch.empty()) {
        cv::cvtColor(image, scratch, image.channels() == 4 ? cv::COLOR_BGRA2GRAY
                                                            : cv::COLOR_BGR2GRAY);
    }
    return ZXing::ImageView(scratch.ptr<uint8_t>(), scratch.cols, scratch.rows,
                            ZXing::ImageFormat::Lum, static_cast<int>(scratch.step));
}

}  // namespace

BarcodeDecoder::BarcodeDecoder(const std::string& allowed_formats, bool try_rotate,
                               bool try_invert, bool try_downscale)
    : mask_string_(allowed_formats) {
    (void)try_rotate;
    (void)try_invert;
    (void)try_downscale;
    // Build the canonical list for the log line and for allow-list checks.
    std::string canonical;
    std::size_t start = 0;
    while (start <= mask_string_.size()) {
        const std::size_t comma = mask_string_.find(',', start);
        const std::string token = trim(
            mask_string_.substr(start, comma == std::string::npos ? std::string::npos
                                                                 : comma - start));
        if (!token.empty()) {
            const std::string key = normalise_key(token);
            const char* mapped = canonical_for_key(key);
            const std::string name = mapped != nullptr ? std::string(mapped) : token;
            if (!canonical.empty()) canonical += ",";
            canonical += name;
        }
        if (comma == std::string::npos) break;
        start = comma + 1;
    }
    canonical_mask_ = canonical;
    SIDB_LOG(log::Level::Debug, kComponent, "decoding symbologies: " + canonical_mask_);
}

std::string BarcodeDecoder::canonical_format(const std::string& raw) {
    const std::string key = normalise_key(raw);
    if (key.empty()) return {};
    const char* mapped = canonical_for_key(key);
    return mapped != nullptr ? std::string(mapped) : trim(raw);
}

bool BarcodeDecoder::is_allowed_format(const std::string& format, const std::string& allow_list) {
    const std::string canonical = canonical_format(format);
    if (canonical.empty()) return true;  // unknown symbology: let the API decide
    std::size_t start = 0;
    while (start <= allow_list.size()) {
        const std::size_t comma = allow_list.find(',', start);
        const std::string token = trim(allow_list.substr(
            start, comma == std::string::npos ? std::string::npos : comma - start));
        if (!token.empty() && canonical_format(token) == canonical) return true;
        if (comma == std::string::npos) break;
        start = comma + 1;
    }
    return false;
}

bool BarcodeDecoder::matches_payload_pattern(const std::string& payload,
                                             const std::string& pattern) {
    if (pattern.empty()) return true;
    try {
        // ECMAScript grammar: the pattern comes from our own configuration file,
        // not from the network, and std::regex is compiled once per call which
        // is acceptable at ~30 calls/second.
        const std::regex expression(pattern, std::regex::ECMAScript);
        return std::regex_match(payload, expression);
    } catch (const std::regex_error& error) {
        SIDB_LOG(log::Level::Error, kComponent,
                 std::string("invalid payload pattern, accepting all payloads: ") + error.what());
        return true;
    }
}

bool DecodeOutcome::any_valid() const {
    return std::any_of(detections.begin(), detections.end(),
                       [](const BarcodeDetection& d) { return d.valid && !d.text.empty(); });
}

const BarcodeDetection* DecodeOutcome::best() const {
    for (const BarcodeDetection& detection : detections) {
        if (detection.valid && !detection.text.empty()) return &detection;
    }
    return detections.empty() ? nullptr : &detections.front();
}

DecodeOutcome BarcodeDecoder::decode(const cv::Mat& image) const {
    DecodeOutcome outcome;
    if (image.empty()) {
        outcome.error_code = error::kFrameCaptureFailed;
        return outcome;
    }

    const double started = now_ms();
    cv::Mat scratch;
    const ZXing::ImageView view = make_view(image, scratch);

    ZXing::ReaderOptions options;
    try {
        // ZXing's parser wants compact names ("Code128"), and it *throws* on an
        // unknown token, so the mask is built from a validated list.
        const std::string compact = canonical_mask_.empty() ? std::string("Code128") : canonical_mask_;
        options.setFormats(ZXing::BarcodeFormatsFromString(compact));
    } catch (const std::exception& error) {
        SIDB_LOG(log::Level::Error, kComponent,
                 std::string("could not build the symbology mask: ") + error.what());
        outcome.error_code = error::kBarcodeDecodeFailed;
        return outcome;
    }
    options.setTryRotate(true);
    options.setTryDownscale(true);
    options.setTryInvert(true);
    options.setTryHarder(true);

    const ZXing::Barcodes results = ZXing::ReadBarcodes(view, options);
    outcome.duration_ms = now_ms() - started;
    outcome.attempts = 1;

    for (const ZXing::Barcode& barcode : results) {
        BarcodeDetection detection;
        detection.text = trim(barcode.text());
        detection.format = canonical_format(ZXing::ToString(barcode.format()));
        detection.symbology = detection.format;
        detection.valid = barcode.isValid();
        detection.error_type =
            barcode.error() ? ZXing::ToString(barcode.error()) : std::string();
        detection.orientation = barcode.orientation();
        const ZXing::Position& position = barcode.position();
        detection.points = {cv::Point(position.topLeft().x, position.topLeft().y),
                            cv::Point(position.topRight().x, position.topRight().y),
                            cv::Point(position.bottomRight().x, position.bottomRight().y),
                            cv::Point(position.bottomLeft().x, position.bottomLeft().y)};
        if (!detection.text.empty()) {
            outcome.detections.push_back(std::move(detection));
        }
    }

    if (outcome.detections.empty()) {
        outcome.error_code = error::kBarcodeNotDetected;
    } else if (!outcome.any_valid()) {
        outcome.error_code = error::kBarcodeDecodeFailed;
    }
    return outcome;
}

}  // namespace sidb
