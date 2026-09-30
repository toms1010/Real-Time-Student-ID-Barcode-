// ===========================================================================
//  AudioFeedback.h - non-blocking scan sounds
//
//  The brief asks for optional sound effects that must never block barcode
//  processing.  Rather than pull in SDL or miniaudio, playback shells out to the
//  ALSA/PulseAudio tools that are already installed on a Linux desktop
//  (aplay / paplay / ffplay) from a detached worker thread:
//
//    * the decode thread only *enqueues* an event (a mutex + condition variable)
//    * a single worker thread plays one sound at a time
//    * a new event replaces a queued one, so a burst of scans cannot build a
//      backlog that plays long after the student has been served
//
//  If no player is available the component silently disables itself and says so
//  in the log - audio is a nicety, never a dependency.
// ===========================================================================
#ifndef SIDB_AUDIO_AUDIO_FEEDBACK_H
#define SIDB_AUDIO_AUDIO_FEEDBACK_H

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>

#include "utils/Config.h"

namespace sidb {

enum class SoundEvent {
    ScanDetected,
    VerificationSuccess,
    UnknownStudent,
    Error,
};

class AudioFeedback {
public:
    explicit AudioFeedback(const AudioConfig& config);
    ~AudioFeedback();

    AudioFeedback(const AudioFeedback&) = delete;
    AudioFeedback& operator=(const AudioFeedback&) = delete;

    /// Start the worker.  Safe to call when already started.
    void start();
    void stop();

    /// Queue a sound.  Returns immediately; the caller's latency is unaffected.
    void play(SoundEvent event);

    void set_enabled(bool enabled) { enabled_.store(enabled); }
    void set_volume(double volume);
    bool enabled() const { return enabled_.load(); }

    /// The command that will be used, empty when audio is unavailable.
    const std::string& player_command() const { return player_; }

    /// Resolve the player: honours `audio.player`, then probes PATH in the order
    /// paplay, aplay, ffplay.
    static std::string resolve_player(const std::string& configured);

    /// Path of the wav file for an event, empty when it does not exist.
    std::string sound_path(SoundEvent event) const;

private:
    void worker();

    AudioConfig config_;
    std::string player_;
    std::thread thread_;
    std::mutex mutex_;
    std::condition_variable cv_;
    bool running_ = false;
    bool pending_ = false;
    SoundEvent pending_event_ = SoundEvent::ScanDetected;
    std::atomic<bool> enabled_{true};
    std::atomic<double> volume_{0.8};
};

}  // namespace sidb

#endif  // SIDB_AUDIO_AUDIO_FEEDBACK_H
