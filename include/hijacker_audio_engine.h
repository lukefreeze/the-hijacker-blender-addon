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
    float  volume_end     = -1.0f;// < 0: constant `volume`. >= 0: the gain moves in a straight line from
                                  // `volume` at the segment start to `volume_end` at its end (volume
                                  // automation; a keyframed strip is sent as a chain of these)
};

// ---------------------------------------------------------------------------
// Loudness reading (ITU-R BS.1770-4 / EBU R128). Levels in LUFS / dBFS / dBTP;
// -200 means "no reading yet".
// ---------------------------------------------------------------------------
struct HijackerLoudness {
    double momentary      = -200.0;   // LUFS, last 400 ms
    double short_term     = -200.0;   // LUFS, last 3 s
    double integrated     = -200.0;   // LUFS, gated, since the last reset
    double lra            = 0.0;      // LU, loudness range since the last reset
    double momentary_max  = -200.0;
    double short_term_max = -200.0;
    double true_peak_db   = -200.0;   // dBTP since the last reset
    double sample_peak_db = -200.0;   // dBFS since the last reset
    double duration_s     = 0.0;      // seconds of signal measured since the last reset
};

// ---------------------------------------------------------------------------
// Output device description (for the settings page)
// ---------------------------------------------------------------------------
struct HijackerOutputDevice {
    int         index = -1;               // PortAudio device index (changes between sessions - use the name)
    std::string name;                     // device name, as the driver reports it
    std::string host_api;                 // "WASAPI", "Core Audio", "ALSA", ...
    int         max_output_channels = 0;
    double      default_sample_rate = 0.0;
    double      low_latency_s  = 0.0;
    double      high_latency_s = 0.0;
    bool        is_default = false;       // the system default output of that driver
};

// Lists the stereo-capable output devices of the driver the engine uses
// (WASAPI on Windows). Safe to call before init(), and while it is running
// (a running engine reports the device list from when it started).
std::vector<HijackerOutputDevice> hj_list_output_devices();

// ---------------------------------------------------------------------------
// HijackerEngine — main engine class
// ---------------------------------------------------------------------------
class HijackerEngine {
public:
    HijackerEngine();
    ~HijackerEngine();

    // Lifecycle
    // buffer_frames: 0 = let the driver choose (default), otherwise the requested
    // device buffer size in frames. The device must be chosen with
    // set_output_device() *before* init(); an empty name = the system default.
    bool  init(int sample_rate = 44100, int buffer_frames = 0);
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

    // Master bus: fader after the channel sum (before the safety limiter).
    void  set_master_gain(float linear);    // 0..2 (+6 dB max), smoothed
    float get_master_gain() const;
    float get_master_peak() const;          // max |sample| since last call (post-fader, pre-limiter)
    bool  get_master_clip() const;          // true if the master reached 0 dBFS since clear_master_clip()
    void  clear_master_clip();

    // Loudness of the master output (what you hear, after the master fader and limiter).
    // Measured continuously while the engine runs; reset_loudness() starts a fresh measurement.
    HijackerLoudness get_loudness() const;
    void  reset_loudness();
    double get_playhead_s()      const;

    // Offline render: runs the same mix as live playback (fader, effects, pan,
    // mute/solo, resampling, master gain and limiter) faster than real time, into
    // memory, at any sample rate. For mixdowns and stems.
    //   offline_begin : refuses while playing. Live audio stays silent until offline_end().
    //                   channel_mask: bit n = channel n. respect_mute_solo: obey the desk's
    //                   mute/solo. apply_master: master fader + safety limiter (mixes yes, stems no).
    //   offline_render: renders the next n_frames (interleaved stereo float) and returns n_frames.
    bool offline_begin(double start_s, int sample_rate, unsigned channel_mask,
                       bool respect_mute_solo = true, bool apply_master = true);
    int  offline_render(float* out_interleaved, int n_frames);
    void offline_end();
    bool offline_active() const;
    // Loudness of the offline render so far (valid during the render and after offline_end()
    // until the next offline_begin()).
    HijackerLoudness offline_loudness() const;

    // Output device (call before init())
    void        set_output_device(const std::string& name);
    std::string get_output_device_name() const;   // the device the stream is actually using
    int         get_buffer_frames() const;        // requested buffer size (0 = driver's choice)

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
    int    buffer_frames_ = 0;
};

// Global engine instance (accessed via pybind11 wrapper)
extern HijackerEngine* g_hijacker_engine;
