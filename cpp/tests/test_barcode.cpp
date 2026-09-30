// ===========================================================================
//  test_barcode.cpp - real decoding against generated barcode images
//
//  These tests read the *actual* images produced by
//  `python -m app.cli generate-assets` (real Code 128 / QR / DataMatrix symbols
//  encoded with ZXing-C++), decode them with ZXing-C++ and assert the exact
//  payload.  A round trip is therefore verified, not a mocked result.
//
//  If the assets are missing the suite says so and fails with the command that
//  creates them - it never skips silently, because a barcode test that quietly
//  does nothing is worse than no test.
// ===========================================================================
#include <filesystem>
#include <iostream>
#include <string>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include "barcode/BarcodeDecoder.h"
#include "barcode/BarcodeDetector.h"
#include "processing/FrameProcessor.h"
#include "tests/Testing.h"
#include "utils/Config.h"

namespace {

namespace fs = std::filesystem;

fs::path project_root() {
    if (const char* root = std::getenv("SIDB_PROJECT_ROOT")) return fs::path(root);
    // Tests run from <root>/build by default.
    const fs::path cwd = fs::current_path();
    if (cwd.filename() == "build" && fs::exists(cwd.parent_path() / "database" / "schema.sql")) {
        return cwd.parent_path();
    }
    if (fs::exists(cwd / "database" / "schema.sql")) return cwd;
    return cwd.parent_path();
}

fs::path asset(const std::string& relative) { return project_root() / "assets" / relative; }

sidb::Config test_config() {
    sidb::Config config = sidb::Config::load();
    config.scanner.verify_remotely = false;   // unit tests must not touch the network
    return config;
}

struct DecodeCase {
    const char* file;
    const char* expected_text;
    const char* expected_format;
};

}  // namespace

TEST_CASE("barcode: decodes every generated symbology") {
    const fs::path directory = asset("test-barcodes");
    if (!fs::is_directory(directory)) {
        std::cerr << "        assets/test-barcodes is missing. Create it with:\n"
                  << "        source .venv/bin/activate && python -m app.cli generate-assets\n";
        CHECK(false);
        return;
    }

    const DecodeCase cases[] = {
        {"code128_primary.png", "2026-000001", "Code128"},
        {"code128_numeric.png", "2026000001", "Code128"},
        {"code39_legacy.png", "ID-2026-000001", "Code39"},
        {"ean13_retail.png", "5901234123457", "EAN13"},
        {"ean8_retail.png", "96385074", "EAN8"},
        {"qrcode_matrix.png", "2026-000002", "QRCode"},
        {"datamatrix_matrix.png", "2026-000003", "DataMatrix"},
    };

    sidb::BarcodeDecoder decoder("Code128,Code39,EAN13,EAN8,UPCA,UPCE,ITF,QRCode,DataMatrix");
    for (const DecodeCase& item : cases) {
        const fs::path path = directory / item.file;
        if (!fs::is_file(path)) {
            std::cerr << "        missing fixture: " << path.filename() << "\n";
            CHECK(false);
            continue;
        }
        const cv::Mat image = cv::imread(path.string(), cv::IMREAD_GRAYSCALE);
        CHECK(!image.empty());
        if (image.empty()) continue;

        const sidb::DecodeOutcome outcome = decoder.decode(image);
        CHECK(outcome.found());
        const sidb::BarcodeDetection* detection = outcome.best();
        CHECK_NOT_NULL(detection);
        if (detection != nullptr) {
            CHECK_EQ(detection->text, std::string(item.expected_text));
            CHECK_EQ(detection->format, std::string(item.expected_format));
            CHECK(detection->valid);
            CHECK_EQ(detection->points.size(), static_cast<std::size_t>(4));
        }
    }
}

TEST_CASE("barcode: reports NOT_DETECTED on an image without a barcode") {
    const cv::Mat blank(200, 200, CV_8UC1, cv::Scalar(255));
    sidb::BarcodeDecoder decoder;
    const sidb::DecodeOutcome outcome = decoder.decode(blank);
    CHECK(!outcome.found());
    CHECK_EQ(outcome.error_code, std::string(sidb::error::kBarcodeNotDetected));
}

TEST_CASE("barcode: an empty frame is a capture failure, not a decode failure") {
    sidb::BarcodeDecoder decoder;
    const sidb::DecodeOutcome outcome = decoder.decode(cv::Mat());
    CHECK(!outcome.found());
    CHECK_EQ(outcome.error_code, std::string(sidb::error::kFrameCaptureFailed));
}

TEST_CASE("barcode: a corrupt barcode image decodes to an invalid symbol, not a payload") {
    const fs::path path = asset("test-barcodes/code128_primary.png");
    if (!fs::is_file(path)) {
        CHECK(false);
        return;
    }
    cv::Mat image = cv::imread(path.string(), cv::IMREAD_GRAYSCALE);
    CHECK(!image.empty());
    if (image.empty()) return;

    // Obliterate the bars: keep the quiet zone so the shape is still barcode-like.
    image(cv::Rect(0, 20, image.cols, image.rows - 40)).setTo(cv::Scalar(255));
    sidb::BarcodeDecoder decoder;
    const sidb::DecodeOutcome outcome = decoder.decode(image);
    // Either nothing is found, or what is found must not be reported as valid.
    if (outcome.found()) {
        CHECK(!outcome.any_valid());
        CHECK_EQ(outcome.error_code, std::string(sidb::error::kBarcodeDecodeFailed));
    } else {
        CHECK_EQ(outcome.error_code, std::string(sidb::error::kBarcodeNotDetected));
    }
}

TEST_CASE("barcode: decodes a full mock ID card") {
    const fs::path path = asset("sample-id-cards/card_2026-000001.png");
    if (!fs::is_file(path)) {
        std::cerr << "        missing fixture: " << path.filename()
                  << " (run: python -m app.cli generate-assets)\n";
        CHECK(false);
        return;
    }
    const cv::Mat card = cv::imread(path.string(), cv::IMREAD_COLOR);
    CHECK(!card.empty());
    if (card.empty()) return;

    sidb::BarcodeDecoder decoder;
    const sidb::DecodeOutcome outcome = decoder.decode(card);
    CHECK(outcome.found());
    const sidb::BarcodeDetection* detection = outcome.best();
    CHECK_NOT_NULL(detection);
    if (detection != nullptr) {
        CHECK_EQ(detection->text, std::string("2026-000001"));
        CHECK_EQ(detection->format, std::string("Code128"));
    }
}

TEST_CASE("barcode: format names normalise across spellings") {
    CHECK_EQ(sidb::BarcodeDecoder::canonical_format("Code 128"), std::string("Code128"));
    CHECK_EQ(sidb::BarcodeDecoder::canonical_format("qr code"), std::string("QRCode"));
    CHECK_EQ(sidb::BarcodeDecoder::canonical_format("DATAMATRIX"), std::string("DataMatrix"));
    CHECK_EQ(sidb::BarcodeDecoder::canonical_format(""), std::string(""));
}

TEST_CASE("barcode: the symbology allow-list is enforced") {
    const std::string allow = "Code128,Code39,QRCode";
    CHECK(sidb::BarcodeDecoder::is_allowed_format("Code 128", allow));
    CHECK(sidb::BarcodeDecoder::is_allowed_format("QRCode", allow));
    CHECK(!sidb::BarcodeDecoder::is_allowed_format("PDF417", allow));
    CHECK(!sidb::BarcodeDecoder::is_allowed_format("DataMatrix", allow));
    // An unknown symbology is deferred to the API rather than rejected here.
    CHECK(sidb::BarcodeDecoder::is_allowed_format("", allow));
}

TEST_CASE("barcode: payload pattern validation matches the shipped configuration") {
    const sidb::Config config = test_config();
    const sidb::ScannerConfig& scanner = config.scanner;
    CHECK_EQ(sidb::FrameProcessor::validate_payload("2026-000001", scanner, "Code128"),
             std::string(""));
    CHECK_EQ(sidb::FrameProcessor::validate_payload("ID-2026-000001", scanner, "Code39"),
             std::string(""));
    // empty
    CHECK_EQ(sidb::FrameProcessor::validate_payload("", scanner, "Code128"),
             std::string(sidb::error::kInvalidBarcode));
    CHECK_EQ(sidb::FrameProcessor::validate_payload("   ", scanner, "Code128"),
             std::string(sidb::error::kInvalidBarcode));
    // control characters
    CHECK_EQ(sidb::FrameProcessor::validate_payload(std::string("2026\0abc", 8), scanner, "Code128"),
             std::string(sidb::error::kInvalidBarcode));
    // too long
    CHECK_EQ(sidb::FrameProcessor::validate_payload(std::string(200, 'A'), scanner, "Code128"),
             std::string(sidb::error::kInvalidBarcode));
    // shape violation
    CHECK_EQ(sidb::FrameProcessor::validate_payload("has space", scanner, "Code128"),
             std::string(sidb::error::kInvalidBarcode));
    CHECK_EQ(sidb::FrameProcessor::validate_payload("-leading-dash", scanner, "Code128"),
             std::string(sidb::error::kInvalidBarcode));
    // symbology not allowed
    CHECK_EQ(sidb::FrameProcessor::validate_payload("2026-000001", scanner, "PDF417"),
             std::string(sidb::error::kUnsupportedFormat));
}

TEST_CASE("barcode: detector finds the barcode region on a card") {
    const fs::path path = asset("sample-id-cards/card_2026-000001.png");
    if (!fs::is_file(path)) {
        CHECK(false);
        return;
    }
    const cv::Mat card = cv::imread(path.string(), cv::IMREAD_COLOR);
    if (card.empty()) {
        CHECK(false);
        return;
    }
    sidb::Config config = test_config();
    sidb::BarcodeDetector detector;
    const sidb::DetectionResult result = detector.detect(card, config.camera);
    CHECK(result.duration_ms >= 0.0);
    // A card image must contain at least one barcode-shaped candidate; the
    // detector is conservative, so a miss is allowed but logged.
    std::cout << "        candidates=" << result.candidates.size()
              << " any=" << (result.any_candidate ? "yes" : "no")
              << " time=" << result.duration_ms << "ms\n";
    CHECK(result.candidates.size() >= 1);
}

int main() { RUN_ALL_TESTS(); }
