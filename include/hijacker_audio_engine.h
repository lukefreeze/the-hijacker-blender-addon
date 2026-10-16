#pragma once
// =============================================================================
// hijacker_audio_engine.h
// The Hijacker — Real-time multi-channel audio engine for Blender
//
// Architecture
//   Audio thread (PortAudio callback)
//     - owns the transport (playhead in integer sample frames)
//     - pulls each channel's pre-rendered audio out of a lock-free ring buffer
//     - applies fader / mute / pan with click-free gain smoothing, the DSP
//       effect chain, metering, the master soft limiter
//     - never allocates, never takes a lock, never touches a file
//
//   Disk thread (one worker for all channels)
//     - opens the WAV files, decodes and (if needed) resamples them, and
//       renders every channel's playlist - segment placement is
//       sample-accurate, with short fades at segment edges - into that
//       channel's ring buffer ahead of the playhead
//
//   Python thread(s)
//     - publish playlists (shared_ptr swap under a short mutex that only the
//       disk thread shares) and mixer/effect parameters
//       (atomics / a seqlock per effect slot)
//
// All channels are rendered against ONE timeline, so every channel is
// sample-aligned by construction, and mute / unmute never changes sync.
// =============================================================================

#include "hijacker_processor.h"
#include <atomic>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <cstdint>

// Maximum segments per channel (strips in the VSE)
#define HJ_MAX_SEGMENTS  8192
// Legacy constant (the callback now handles whatever size the device uses)
#define HJ_BUFFER_FRAMES 512

// ---------------------------------------------------------------------------
// Segment — one VSE sound strip mapped to file + timeline positions
// ---------------------------------------------------------------------------
struct HijackerSegment {
    char   filepath[512]  = {};   // absolute path to decoded WAV
    double file_offset_s  = 0.0;  // where in the file audio starts
    double duration_s     = 0.0;  // how long this segment plays
    double timeline_pos_s = 0.0;  // when it starts on the timeline
    float  volume         = 1.0f; // strip.volume baked in — per-segment gain
                                  // applied pre-DSP so the compressor sees it
};

// ---------------------------------------------------------------------------
// HijackerEngine — main engine class
// ---------------------------------------------------------------------------
class HijackerEngine {
public:
    HijackerEngine();
    ~HijackerEngine();

    // Lifecycle
    bool  init(int sample_rate = 44100, int buffer_frames = HJ_BUFFER_FRAMES);
    void  shutdown();
    bool  is_running() const;

    // Transport — called from Python
    void  play(double timeline_pos_s);
    void  stop();
    void  seek(double timeline_pos_s);

    // Channel setup — called from Python at play-start (safe during playback)
    void  set_channel_playlist(int ch, const std::vector<HijackerSegment>& segs);
    void  clear_channel(int ch);
    void  clear_all_channels();

    // Real-time mixer params — safe to call during playback
    void  set_volume(int ch, float vol);
    void  set_mute(int ch, bool muted);
    void  set_solo(int ch, bool soloed);
    void  set_pan(int ch, float pan);   // 0=left, 0.5=centre, 1=right

    // Effect params — take effect next buffer (~5ms)
    void  set_effect_slot(int ch, int slot, int type,
                          const std::vector<float>& params);
    void  clear_effect_slot(int ch, int slot);

    // Metering — called from Python meter timer
    float get_meter_rms(int ch)  const;
    float get_meter_peak(int ch) const;
    double get_playhead_s()      const;

    // Diagnostics
    int    get_sample_rate()      const;
    double get_output_latency_s() const;   // device output latency (0 if unknown)
    int    get_xrun_count()       const;   // output underflows + decoder underruns

    // Latency compensation. The sound for a timeline position reaches the
    // speakers one output-latency (plus the start-up pre-roll) after the engine
    // renders it, so play()/seek() start the audio that much *ahead* of the
    // position they are given. get_playhead_s() then reports the position the
    // listener is hearing, which matches the Blender cursor / video.
    //   seconds < 0 : automatic (device output latency + measured pre-roll)  [default]
    //   seconds = 0 : off
    //   seconds > 0 : fixed offset (capped at 250 ms)
    void   set_latency_compensation(double seconds);
    double get_latency_compensation_s() const;   // offset currently in effect

    // Expose EngineState for GR levels, FFT bins (used by rack UI)
    // Returns the global g_state which hijacker_processor.cpp also uses
    EngineState* get_state();  // defined in hijacker_audio_engine.cpp

    // PortAudio callback — public so C callback can reach it
    int audio_callback(const void* input, void* output,
                       unsigned long frames_per_buffer);
    void note_device_xrun();

private:
    HijackerEngine(const HijackerEngine&) = delete;
    HijackerEngine& operator=(const HijackerEngine&) = delete;

    struct Impl;
    std::unique_ptr<Impl> impl_;

    void*  pa_stream_   = nullptr;   // PaStream* — void* avoids portaudio.h in header
    bool   initialized_ = false;
    int    sample_rate_ = 44100;
    int    buffer_frames_ = HJ_BUFFER_FRAMES;
};

// Global engine instance (accessed via pybind11 wrapper)
extern HijackerEngine* g_hijacker_engine;
