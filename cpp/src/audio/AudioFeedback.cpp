// ===========================================================================
//  AudioFeedback.cpp
// ===========================================================================
#include "audio/AudioFeedback.h"

#include <array>
#include <cstdlib>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "utils/Logger.hpp"

namespace sidb {

namespace {

constexpr const char* kComponent = "audio";

bool executable_in_path(const std::string& name) {
    const char* path_env = std::getenv("PATH");
    if (path_env == nullptr) return false;
    const std::string path(path_env);
    std::size_t start = 0;
    while (start <= path.size()) {
        const std::size_t colon = path.find(':', start);
        const std::string directory =
            path.substr(start, colon == std::string::npos ? std::string::npos : colon - start);
        if (!directory.empty()) {
            const std::string candidate = directory + "/" + name;
            if (::access(candidate.c_str(), X_OK) == 0) return true;
        }
        if (colon == std::string::npos) break;
        start = colon + 1;
    }
    return false;
}

}  // namespace

AudioFeedback::AudioFeedback(const AudioConfig& config) : config_(config) {
    player_ = resolve_player(config.player);
    volume_.store(config.volume);
    if (config.enabled && !player_.empty()) {
        enabled_.store(true);
    } else {
        enabled_.store(false);
        if (config.enabled) {
            SIDB_LOG(log::Level::Warning, kComponent,
                     "no audio player found (looked for paplay, aplay, ffplay); "
                     "install alsa-utils or pulseaudio-utils to enable sound feedback");
        }
    }
}

AudioFeedback::~AudioFeedback() { stop(); }

std::string AudioFeedback::resolve_player(const std::string& configured) {
    const std::string choice = configured.empty() ? std::string("auto") : configured;
    if (choice == "none") return {};
    if (choice != "auto") {
        return executable_in_path(choice) ? choice : std::string();
    }
    for (const char* candidate : {"paplay", "aplay", "ffplay"}) {
        if (executable_in_path(candidate)) return candidate;
    }
    return {};
}

std::string AudioFeedback::sound_path(SoundEvent event) const {
    const char* name = "scan_detected";
    switch (event) {
        case SoundEvent::ScanDetected:         name = "scan_detected"; break;
        case SoundEvent::VerificationSuccess:  name = "verification_success"; break;
        case SoundEvent::UnknownStudent:       name = "unknown_student"; break;
        case SoundEvent::Error:                name = "error"; break;
    }
    std::string directory = config_.assets_dir;
    if (directory.empty() || directory.front() != '/') {
        // Relative to the working directory the launcher sets (repo root), with
        // a fallback for running the binary directly from build/.
        directory = "assets/sounds";
    }
    return directory + "/" + name + ".wav";
}

void AudioFeedback::start() {
    if (!enabled_.load() || player_.empty()) return;
    std::lock_guard<std::mutex> lock(mutex_);
    if (running_) return;
    running_ = true;
    thread_ = std::thread(&AudioFeedback::worker, this);
    SIDB_LOG(log::Level::Info, kComponent, "audio feedback enabled via " + player_);
}

void AudioFeedback::stop() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!running_) return;
        running_ = false;
    }
    cv_.notify_all();
    if (thread_.joinable()) thread_.join();
}

void AudioFeedback::set_volume(double volume) {
    const double clamped = volume < 0.0 ? 0.0 : (volume > 1.0 ? 1.0 : volume);
    volume_.store(clamped);
}

void AudioFeedback::play(SoundEvent event) {
    if (!enabled_.load() || player_.empty()) return;
    {
        // Overwrite any queued event: playing stale feedback is worse than
        // dropping it.
        std::lock_guard<std::mutex> lock(mutex_);
        if (!running_) return;
        pending_event_ = event;
        pending_ = true;
    }
    cv_.notify_one();
}

void AudioFeedback::worker() {
    while (true) {
        SoundEvent event = SoundEvent::ScanDetected;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            cv_.wait(lock, [this] { return !running_ || pending_; });
            if (!running_) return;
            event = pending_event_;
            pending_ = false;
        }
        if (!enabled_.load()) continue;

        const std::string path = sound_path(event);
        struct stat info {};
        if (::stat(path.c_str(), &info) != 0) {
            SIDB_LOG(log::Level::Warning, kComponent,
                     "sound file not found: " + path +
                         " (run: python -m app.cli generate-assets)");
            continue;
        }

        const pid_t pid = ::fork();
        if (pid == 0) {
            // Child: never returns.
            if (player_ == "aplay") {
                ::execlp("aplay", "aplay", "-q", path.c_str(), nullptr);
            } else if (player_ == "paplay") {
                ::execlp("paplay", "paplay", path.c_str(), nullptr);
            } else if (player_ == "ffplay") {
                ::execlp("ffplay", "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                         path.c_str(), nullptr);
            }
            ::_exit(127);
        }
        if (pid < 0) {
            SIDB_LOG(log::Level::Warning, kComponent, "could not fork a player process");
            continue;
        }
        // Reap asynchronously so no zombie accumulates; a short wait keeps the
        // player from being killed before it has produced any sound.
        int status = 0;
        ::usleep(60 * 1000);
        ::waitpid(pid, &status, WNOHANG);
    }
}

}  // namespace sidb
