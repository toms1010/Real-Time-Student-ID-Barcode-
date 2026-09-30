// ===========================================================================
//  main.cpp - Student ID Barcode Scanner
//
//  Usage (the launcher scripts wrap these):
//    student_id_scanner                       native window, camera 0
//    student_id_scanner --mode=service        headless, mirrors frames to the API
//    student_id_scanner --mode=file --source=clip.mp4
//    student_id_scanner --decode-image=card.png     one-shot decode
//    student_id_scanner --benchmark --images=assets/sample-id-cards
//    student_id_scanner --list-cameras
//    student_id_scanner --api-check
//    student_id_scanner --flush-queue
//    student_id_scanner --print-config
//
//  Startup order follows the brief: configuration -> logger -> camera ->
//  decoder -> (window) -> scanner.  A failure at any step prints a specific
//  message with the command that fixes it, and exits non-zero.
// ===========================================================================
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

#include <opencv2/highgui.hpp>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>

#include "api/ApiClient.h"
#include "camera/CameraManager.h"
#include "scanner/PerformanceMonitor.h"
#include "scanner/ScannerEngine.h"
#include "utils/Config.h"
#include "utils/Logger.hpp"
#include "utils/TimeUtil.h"

namespace {

constexpr const char* kVersion = "1.0.0";

void print_banner(const sidb::Config& config) {
    std::cout << "\n" << std::string(72, '=') << "\n"
              << "  " << config.application.name << "  v" << kVersion << "\n"
              << "  Real-time student ID barcode detection (C++17 / OpenCV / ZXing-C++)\n"
              << std::string(72, '=') << "\n";
}

void print_help() {
    std::cout << R"(student_id_scanner - Real-Time Student ID Barcode Detection System

Modes
  --mode=native              Live camera in an OpenCV window (default)
  --mode=service             Headless; publishes annotated frames to the web UI
  --mode=file                Replay a video file or a still image
  --mode=benchmark           Measure decode throughput, then exit

Options
  --config=PATH              Configuration file (default: config/config.yaml,
                             then config/config.example.yaml)
  --project-root=PATH        Override the repository root used to resolve paths
  --source=PATH|URL          Camera source; overrides camera.source
  --device=N                 Camera index; overrides camera.device_index
  --width=N --height=N       Requested capture resolution
  --fps=N                    Requested capture frame rate (0 = unlimited)
  --cooldown-ms=N            Duplicate suppression window
  --no-verify                Do not call the local API (offline demo only)
  --no-window                Do not show a window (implies --mode=service)
  --no-audio                 Disable sound feedback
  --no-overlay               Skip annotation (faster benchmarking)
  --decode-image=PATH        Decode one image and print the payload, then exit
  --decode-dir=DIR           Decode every image in a directory
  --flush-queue              Replay the offline scan queue to the API, then exit
  --api-check                Probe the local API and print its health
  --list-cameras             List the /dev/video* devices and exit
  --print-config             Print the effective configuration and exit
  --benchmark                Run the benchmark and print a performance report
  --json=PATH                Write the benchmark result as JSON
  --frames=N                 Stop after N processed frames
  --log-level=LEVEL          TRACE | DEBUG | INFO | WARN | ERROR
  -h, --help                 This text

Keys (native mode)
  q / ESC   quit
  c         clear the duplicate-cooldown map
  s         save the current frame to logs/
  i         toggle 1280x720 <-> 640x480
)";
}

/// A deliberately small `key=value` / `--key value` argument reader.
class Arguments {
public:
    Arguments(int argc, char** argv) {
        for (int i = 1; i < argc; ++i) {
            std::string token = argv[i];
            if (token.rfind("--", 0) == 0) {
                token = token.substr(2);
                const std::size_t equals = token.find('=');
                if (equals != std::string::npos) {
                    values_[token.substr(0, equals)] = token.substr(equals + 1);
                } else if (i + 1 < argc && std::strncmp(argv[i + 1], "--", 2) != 0) {
                    values_[token] = argv[++i];
                } else {
                    values_[token] = "true";
                }
            } else {
                positional_.push_back(token);
            }
        }
    }

    bool has(const std::string& key) const { return values_.count(key) > 0; }
    std::string get(const std::string& key, const std::string& fallback = "") const {
        const auto it = values_.find(key);
        return it == values_.end() ? fallback : it->second;
    }
    int get_int(const std::string& key, int fallback) const {
        const std::string raw = get(key);
        if (raw.empty()) return fallback;
        try {
            return std::stoi(raw);
        } catch (const std::exception&) {
            return fallback;
        }
    }
    double get_double(const std::string& key, double fallback) const {
        const std::string raw = get(key);
        if (raw.empty()) return fallback;
        try {
            return std::stod(raw);
        } catch (const std::exception&) {
            return fallback;
        }
    }
    bool flag(const std::string& key) const {
        const std::string raw = sidb::strutil::to_lower(get(key, "false"));
        return raw == "true" || raw == "1" || raw == "yes" || raw == "on";
    }
    const std::vector<std::string>& positional() const { return positional_; }

private:
    std::map<std::string, std::string> values_;
    std::vector<std::string> positional_;
};

int decode_one_image(const sidb::Config& config, const std::string& path) {
    const cv::Mat image = cv::imread(path, cv::IMREAD_COLOR);
    if (image.empty()) {
        std::cerr << "could not read the image: " << path << "\n";
        return 1;
    }
    sidb::FrameProcessor processor(config);
    const sidb::FrameResult result =
        processor.process(image, "decoding", cv::Scalar(220, 220, 220), false);
    std::cout << "file            : " << path << "\n"
              << "resolution      : " << image.cols << "x" << image.rows << "\n"
              << "preprocess      : "
              << (result.preprocess.steps.empty() ? "(none)" : "") ;
    for (const std::string& step : result.preprocess.steps) std::cout << step << " ";
    std::cout << "\n"
              << "preprocess time : " << result.preprocess_ms << " ms\n"
              << "detect time     : " << result.detect_ms << " ms\n"
              << "decode time     : " << result.decode_ms << " ms\n"
              << "total time      : " << result.total_ms << " ms\n"
              << "candidates      : " << result.detect.candidates.size() << "\n"
              << "detections      : " << result.detections.size() << "\n";
    if (result.detections.empty()) {
        std::cout << "error           : " << result.error_code << "\n";
        return 2;
    }
    for (const sidb::BarcodeDetection& detection : result.detections) {
        const std::string validation =
            sidb::FrameProcessor::validate_payload(detection.text, config.scanner, detection.format);
        std::cout << "barcode         : " << detection.text << "\n"
                  << "symbology       : " << detection.format << "\n"
                  << "valid           : " << (detection.valid ? "yes" : "no")
                  << (detection.error_type.empty() ? "" : " (" + detection.error_type + ")") << "\n"
                  << "orientation     : " << detection.orientation << " deg\n"
                  << "payload check   : "
                  << (validation.empty() ? "accepted" : validation) << "\n";
    }
    return 0;
}

int run_benchmark(const sidb::Config& config, const Arguments& args) {
    sidb::PerformanceMonitor monitor(1000000);
    sidb::FrameProcessor processor(config);

    std::vector<std::string> paths;
    const std::string image_arg = args.get("images", args.get("image"));
    if (!image_arg.empty()) {
        paths.push_back(image_arg);
    }
    const std::string directory = args.get("decode-dir");
    if (!directory.empty()) {
        for (const auto& entry : std::filesystem::directory_iterator(directory)) {
            if (entry.is_regular_file()) {
                const std::string extension = entry.path().extension().string();
                if (extension == ".png" || extension == ".jpg" || extension == ".jpeg" ||
                    extension == ".bmp" || extension == ".webp") {
                    paths.push_back(entry.path().string());
                }
            }
        }
    }
    if (paths.empty()) {
        paths = {"assets/sample-id-cards"};
    }
    std::vector<cv::Mat> images;
    for (const std::string& path : paths) {
        if (std::filesystem::is_directory(path)) {
            for (const auto& entry : std::filesystem::directory_iterator(path)) {
                const cv::Mat image = cv::imread(entry.path().string(), cv::IMREAD_COLOR);
                if (!image.empty()) images.push_back(image);
            }
        } else {
            const cv::Mat image = cv::imread(path, cv::IMREAD_COLOR);
            if (!image.empty()) {
                images.push_back(image);
            } else {
                std::cerr << "skipping unreadable image: " << path << "\n";
            }
        }
    }
    if (images.empty()) {
        std::cerr << "no readable images to benchmark; run: python -m app.cli generate-assets\n";
        return 1;
    }

    const int iterations = std::max(1, args.get_int("iterations", 20));
    // Warm-up: the first call initialises internal tables and would skew the mean.
    processor.process(images.front(), "", cv::Scalar(), false);

    int detected_images = 0;
    for (const cv::Mat& image : images) {
        bool hit = false;
        for (int i = 0; i < iterations; ++i) {
            const sidb::FrameResult result = processor.process(image, "", cv::Scalar(), false);
            monitor.record_processing(result.total_ms, result.preprocess_ms, result.detect_ms,
                                      result.decode_ms, 0.0, result.total_ms);
            if (result.primary != nullptr) hit = true;
        }
        if (hit) ++detected_images;
    }

    const sidb::PerformanceMonitor::Snapshot snapshot = monitor.snapshot();
    std::cout << "\n" << monitor.report() << "  images benchmarked  : " << images.size() << "\n"
              << "  images detected     : " << detected_images << " / " << images.size() << "\n"
              << "  iterations per image: " << iterations << "\n"
              << "  decode throughput   : "
              << (snapshot.avg_processing_ms > 0.0
                      ? std::to_string(static_cast<int>(1000.0 / snapshot.avg_processing_ms)) + " /s"
                      : std::string("n/a"))
              << "\n";

    const std::string json_path = args.get("json");
    if (!json_path.empty()) {
        std::ofstream out(json_path);
        out << "{\n"
            << "  \"note\": \"produced by student_id_scanner --benchmark on "
            << TimeUtil::now_iso8601() << "\",\n"
            << "  \"images\": " << images.size() << ",\n"
            << "  \"images_detected\": " << detected_images << ",\n"
            << "  \"iterations_per_image\": " << iterations << ",\n"
            << "  \"avg_processing_ms\": " << snapshot.avg_processing_ms << ",\n"
            << "  \"p95_processing_ms\": " << snapshot.p95_processing_ms << ",\n"
            << "  \"max_processing_ms\": " << snapshot.max_processing_ms << ",\n"
            << "  \"avg_preprocess_ms\": " << snapshot.avg_preprocess_ms << ",\n"
            << "  \"avg_detect_ms\": " << snapshot.avg_detect_ms << ",\n"
            << "  \"avg_decode_ms\": " << snapshot.avg_decode_ms << "\n}\n";
        std::cout << "wrote " << json_path << "\n";
    }
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc > 1 && (std::strcmp(argv[1], "-h") == 0 || std::strcmp(argv[1], "--help") == 0)) {
        print_help();
        return 0;
    }

    Arguments args(argc, argv);
    if (args.has("project-root")) {
        ::setenv("SIDB_PROJECT_ROOT", args.get("project-root").c_str(), 1);
    }
    sidb::Config config = sidb::Config::load(args.get("config"));

    if (args.has("device")) config.camera.device_index = args.get_int("device", 0);
    if (args.has("width")) config.camera.width = args.get_int("width", config.camera.width);
    if (args.has("height")) config.camera.height = args.get_int("height", config.camera.height);
    if (args.has("fps")) config.camera.fps = args.get_int("fps", config.camera.fps);
    if (args.has("source")) config.camera.source = args.get("source");
    if (args.has("cooldown-ms")) config.scanner.cooldown_ms = args.get_int("cooldown-ms", 2000);
    if (args.has("no-verify")) config.scanner.verify_remotely = false;
    if (args.has("no-audio")) config.audio.enabled = false;
    if (args.has("no-overlay")) config.display.draw_overlay = false;
    if (args.has("api")) config.api.base_path = args.get("api");
    if (args.has("api-host")) config.api.host = args.get("api-host");
    if (args.has("api-port")) config.api.port = args.get_int("api-port", config.api.port);
    if (args.has("log-level")) config.application.log_level = args.get("log-level");
    config.validate();

    sidb::log::Level level = sidb::log::Level::Info;
    sidb::log::parse_level(config.application.log_level, level);
    sidb::log::Logger::instance().set_level(level);
    if (!config.application.log_file.empty()) {
        sidb::log::Logger::instance().open_file(config.resolve(config.application.log_file));
    }
    sidb::log::Logger::instance().set_thread_name("main");

    if (args.has("print-config")) {
        print_banner(config);
        std::cout << config.describe();
        return 0;
    }
    if (args.has("list-cameras")) {
        const std::vector<std::string> devices = sidb::CameraManager::available_devices();
        std::cout << devices.size() << " capture device(s) found:\n";
        for (std::size_t i = 0; i < devices.size(); ++i) {
            std::cout << "  [" << i << "] " << devices[i] << '\n';
        }
        if (devices.empty()) {
            std::cout << "  (none - check that the webcam is connected and that your user is in "
                         "the 'video' group)\n";
        }
        return 0;
    }

    print_banner(config);
    for (const std::string& warning : config.warnings) {
        sidb::log::Logger::instance().log(sidb::log::Level::Warning, "config", warning);
    }

    if (args.has("decode-image")) {
        return decode_one_image(config, args.get("decode-image"));
    }
    if (args.has("benchmark") || args.get("mode") == "benchmark") {
        return run_benchmark(config, args);
    }

    sidb::ApiClient api(config.api);
    if (args.has("api-check")) {
        const bool ok = api.healthy(1500);
        std::cout << "api " << api.base_url() << ": " << (ok ? "reachable" : "UNREACHABLE")
                  << "\n";
        return ok ? 0 : 1;
    }
    if (args.has("flush-queue")) {
        sidb::ScannerEngine engine(config, sidb::EngineMode::Service);
        const int flushed = engine.flush_offline_queue();
        std::cout << "flushed " << flushed << " queued scan(s)\n";
        return 0;
    }

    // --- choose the mode ---------------------------------------------------
    sidb::EngineMode mode = sidb::EngineMode::Native;
    std::string mode_name = args.get("mode", "native");
    if (args.has("no-window")) {
        mode = sidb::EngineMode::Service;
        mode_name = "service";
    } else if (mode_name == "service") {
        mode = sidb::EngineMode::Service;
    } else if (mode_name == "file") {
        mode = sidb::EngineMode::File;
        if (config.camera.source.empty() && !config.scanner.file_source.empty()) {
            config.camera.source = config.scanner.file_source;
        }
        if (config.camera.source.empty()) {
            std::cerr << "--mode=file needs --source=<video or image path>\n";
            return 2;
        }
    } else if (mode_name != "native") {
        std::cerr << "unknown --mode: " << mode_name << " (expected native, service or file)\n";
        return 2;
    }

    sidb::ScannerEngine engine(config, mode);
    if (mode == sidb::EngineMode::File) {
        // File mode does not need a camera: it opens the file directly.
        if (!config.camera.source.empty() &&
            !std::filesystem::exists(config.camera.source)) {
            std::cerr << "file not found: " << config.resolve(config.camera.source) << "\n";
            return 1;
        }
    } else if (!engine.start()) {
        const sidb::CameraStatus status = sidb::CameraManager(config.camera).status();
        std::cerr << "\nThe scanner could not start.\n"
                  << "  error   : " << sidb::to_string(engine.state()) << "\n"
                  << "  message : " << engine.status_line() << "\n"
                  << "  devices : ";
        const std::vector<std::string> devices = sidb::CameraManager::available_devices();
        for (std::size_t i = 0; i < devices.size(); ++i) {
            std::cerr << "[" << i << "]" << devices[i] << (i + 1 < devices.size() ? " " : "");
        }
        std::cerr << (devices.empty() ? "(none found)" : "") << "\n"
                  << "  fix     : plug in a webcam, then run './student_id_scanner --list-cameras'\n"
                  << "            and grant access with: sudo usermod -aG video $USER\n";
        return 3;
    }

    engine.set_frame_limit(config.camera.fps > 0 ? 0.0 : 0.0);
    if (args.has("frames")) engine.set_quit_after_frames(args.get_int("frames", 0));

    if (mode == sidb::EngineMode::File) {
        const bool show = !args.has("no-window");
        engine.run_file(config.resolve(config.camera.source), show, args.get_int("frames", 0));
        engine.stop();
        std::cout << "\n" << engine.metrics().frames_processed << " frame(s) processed\n";
        return 0;
    }

    // --- native / service ---------------------------------------------------
    std::cout << "Press q in the window to stop.\n" << std::endl;
    if (mode == sidb::EngineMode::Native) {
        engine.run_window(config.application.name + " - " + to_string(engine.state()));
    } else {
        std::cout << "running headless in service mode; the browser dashboard mirrors this "
                     "scanner.\n"
                     << "Press Ctrl+C to stop.\n"
                     << std::endl;
        // Exit on EOF (Ctrl+D, or a non-interactive stdin) or on an explicit
        // quit. The guard is `running()`, not the workflow state: the machine
        // rests in `Idle`, which is also the state before start() and after
        // stop(), so comparing against a state would end this loop at once.
        std::string line;
        while (engine.running() && std::getline(std::cin, line)) {
            if (line == "quit" || line == "exit") break;
            // `status` and `flush` make the headless mode scriptable.
            if (line == "status") std::cout << engine.status_line() << "\n";
            if (line == "flush") {
                std::cout << "flushed " << engine.flush_offline_queue() << " scan(s)\n";
            }
        }
    }
    engine.stop();
    std::cout << "\n" << engine.metrics().frames_processed << " frame(s) processed, "
              << engine.metrics().scans_verified << " verified\n";
    return 0;
}
