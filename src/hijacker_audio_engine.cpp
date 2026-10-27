// =============================================================================
// hijacker_audio_engine.cpp
// The Hijacker — Real-time multi-channel audio engine
//
// Threads
//   audio thread  (PortAudio callback)  - mixes; never allocates, locks, or
//                                         touches files
//   disk thread   (one worker)          - decodes WAV files and renders every
//                                         channel's playlist into a lock-free
//                                         ring buffer, ahead of the playhead
//   Python thread (GIL held)            - publishes playlists / parameters
//
// The whole engine runs against ONE integer-frame timeline. Each channel's
// ring buffer holds "what this channel sounds like on the timeline", so
//   * segment starts/ends are sample-accurate (short edge fades remove clicks)
//   * channels can never drift against each other, muted or not
//   * a seek is "re-render the ring from here"
//
// Ring-buffer protocol (single producer / single consumer, per channel)
//   consumer -> producer : req_epoch, req_frame, read_pos
//   producer -> consumer : served_epoch, ring_end
//   The consumer only reads while served_epoch == its expected epoch, and only
//   the consumer ever starts a new epoch (seek / playlist change), so the
//   producer can never be repositioned underneath a running read.
// =============================================================================

#include "hijacker_audio_engine.h"
#include "hijacker_loudness.h"
#include <portaudio.h>
#include <cstring>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <algorithm>
#include <vector>
#include <thread>
#include <condition_variable>
#include <memory>
#include <chrono>

#if defined(_WIN32)
  #ifndef NOMINMAX
    #define NOMINMAX
  #endif
  #ifndef WIN32_LEAN_AND_MEAN
    #define WIN32_LEAN_AND_MEAN
  #endif
  #include <windows.h>
#endif

#if defined(__SSE2__) || defined(_M_X64) || defined(_M_IX86)
  #include <xmmintrin.h>
  #define HJ_HAS_SSE 1
#endif

// ---------------------------------------------------------------------------
// Build-time choices
//   HJ_PAN_LAW 1 : balance law, unity at centre, opposite side fades with a
//                  cosine (no level jump when leaving centre)
//   HJ_PAN_LAW 0 : legacy sin/cos law with a +-0.01 bypass around centre
// ---------------------------------------------------------------------------
#ifndef HJ_PAN_LAW
#define HJ_PAN_LAW 1
#endif

// g_state is defined in wrapper.cpp — shared with hijacker_processor.cpp
extern "C" { extern EngineState g_state; }

HijackerEngine* g_hijacker_engine = nullptr;

EngineState* HijackerEngine::get_state() { return &g_state; }

namespace {

constexpr int     RING_FRAMES      = 1 << 15;        // per channel, power of two
constexpr int     RING_MASK        = RING_FRAMES - 1;
constexpr int     MAX_BLOCK        = 1024;           // frames per DSP chunk
constexpr int     PROD_BLOCK       = 1024;           // frames per disk-thread render
constexpr int     PREFETCH_FRAMES  = 16384;          // how far ahead the disk thread runs
constexpr int     EDGE_FADE_FRAMES = 48;             // segment edge fade (~1 ms)
constexpr int     DECLICK_FRAMES   = 96;             // fade-in after seek / dropout / start
constexpr int     STOP_FADE_FRAMES = 256;            // fade-out when stopping
constexpr int     SRC_CAP          = 8400;           // resampler source scratch (frames)
constexpr int64_t NO_SEEK          = INT64_MIN;

// ---------------------------------------------------------------------------
// File helpers
// ---------------------------------------------------------------------------
#if defined(_WIN32)
  #define HJ_FSEEK _fseeki64
  #define HJ_FTELL _ftelli64
#else
  #define HJ_FSEEK fseeko
  #define HJ_FTELL ftello
#endif

FILE* hj_fopen(const char* utf8)
{
#if defined(_WIN32)
    // Paths arrive from Python as UTF-8; plain fopen() would interpret them in
    // the ANSI code page and fail on non-ASCII user/folder names.
    wchar_t wpath[1024];
    int n = MultiByteToWideChar(CP_UTF8, 0, utf8, -1, wpath, 1024);
    if (n > 0) {
        FILE* f = nullptr;
        if (_wfopen_s(&f, wpath, L"rb") == 0 && f) return f;
    }
    FILE* f = nullptr;
    if (fopen_s(&f, utf8, "rb") == 0) return f;
    return nullptr;
#else
    return fopen(utf8, "rb");
#endif
}

// ---------------------------------------------------------------------------
// WAV reader (disk thread only)
// ---------------------------------------------------------------------------
class WavReader {
public:
    ~WavReader() { close(); }

    void close() {
        if (f_) { fclose(f_); f_ = nullptr; }
        path_[0] = 0; pos_ = -1; n_frames_ = 0;
    }

    bool is_open_for(const char* path) const { return f_ && strcmp(path_, path) == 0; }

    bool open(const char* path) {
        close();
        FILE* f = hj_fopen(path);
        if (!f) return false;
        setvbuf(f, nullptr, _IOFBF, 1 << 16);
        f_ = f;
        if (!parse()) { close(); return false; }
        snprintf(path_, sizeof(path_), "%s", path);
        pos_ = -1;
        return true;
    }

    int     sample_rate() const { return sr_; }
    int64_t n_frames()    const { return n_frames_; }

    // Decode source frames [lo, lo+cnt) as interleaved stereo float into dst
    // (2*cnt floats). Frames outside the file are silence. Mono is duplicated;
    // files with more than two channels use the first two.
    void read_frames(int64_t lo, int cnt, float* dst) {
        memset(dst, 0, sizeof(float) * 2 * (size_t)cnt);
        if (!f_ || cnt <= 0) return;
        int64_t a = std::max<int64_t>(lo, 0);
        int64_t b = std::min<int64_t>(lo + cnt, n_frames_);
        if (b <= a) return;

        if (pos_ != a) {
            HJ_FSEEK(f_, data_start_ + a * frame_bytes_, SEEK_SET);
            pos_ = a;
        }
        float* d = dst + 2 * (a - lo);
        int64_t remaining = b - a;
        while (remaining > 0) {
            int n = (int)std::min<int64_t>(remaining, RAW_FRAMES);
            size_t got = fread(raw_, (size_t)frame_bytes_, (size_t)n, f_);
            decode(raw_, (int)got, d);
            pos_ += (int64_t)got;
            d += 2 * got;
            remaining -= (int64_t)got;
            if ((int)got < n) { pos_ = -1; break; }     // short file: rest stays silent
        }
    }

private:
    static constexpr int RAW_FRAMES = 2048;

    bool parse() {
        HJ_FSEEK(f_, 0, SEEK_END);
        int64_t file_size = HJ_FTELL(f_);
        HJ_FSEEK(f_, 0, SEEK_SET);

        uint8_t hdr[12];
        if (fread(hdr, 1, 12, f_) != 12) return false;
        if (memcmp(hdr, "RIFF", 4) != 0 || memcmp(hdr + 8, "WAVE", 4) != 0) return false;

        bool    got_fmt = false;
        int     tag = 0, nch = 0, block_align = 0, bits = 0;
        uint32_t srate = 0;
        int64_t pos = 12;

        while (pos + 8 <= file_size) {
            HJ_FSEEK(f_, pos, SEEK_SET);
            uint8_t ch[8];
            if (fread(ch, 1, 8, f_) != 8) break;
            uint32_t sz; memcpy(&sz, ch + 4, 4);
            int64_t body = pos + 8;

            if (memcmp(ch, "fmt ", 4) == 0 && sz >= 16) {
                uint8_t fmt[40] = {};
                size_t want = std::min<size_t>(sz, sizeof(fmt));
                if (fread(fmt, 1, want, f_) != want) return false;
                uint16_t t16, c16, ba16, b16; uint32_t sr32;
                memcpy(&t16, fmt + 0, 2);  memcpy(&c16, fmt + 2, 2);
                memcpy(&sr32, fmt + 4, 4); memcpy(&ba16, fmt + 12, 2);
                memcpy(&b16, fmt + 14, 2);
                tag = t16; nch = c16; srate = sr32; block_align = ba16; bits = b16;
                if (tag == 0xFFFE && sz >= 26) {          // WAVE_FORMAT_EXTENSIBLE
                    uint16_t sub; memcpy(&sub, fmt + 24, 2);
                    tag = sub;
                }
                got_fmt = true;
            } else if (memcmp(ch, "data", 4) == 0) {
                if (!got_fmt) return false;
                int64_t data_size = (int64_t)sz;
                if (sz == 0 || sz == 0xFFFFFFFFu || body + data_size > file_size)
                    data_size = file_size - body;          // streamed / truncated file
                if (nch < 1 || srate == 0) return false;

                int container = block_align / nch;
                if (container < 1) container = (bits + 7) / 8;
                bool is_float = (tag == 3);
                bool ok = (tag == 1 && (container == 1 || container == 2 ||
                                        container == 3 || container == 4)) ||
                          (is_float && (container == 4 || container == 8));
                if (!ok) {
                    printf("[HIJACKER] WAV format not supported (tag=%d, %d-bit, %d bytes/sample)\n",
                           tag, bits, container);
                    return false;
                }
                sr_          = (int)srate;
                nch_         = nch;
                bytes_       = container;
                is_float_    = is_float;
                frame_bytes_ = (block_align >= nch * container) ? block_align : nch * container;
                data_start_  = body;
                n_frames_    = data_size / frame_bytes_;
                if (frame_bytes_ > RAW_BYTES_MAX / RAW_FRAMES) return false;
                return true;
            }
            pos = body + (int64_t)sz + (int64_t)(sz & 1u);   // chunks are word aligned
        }
        return false;
    }

    inline float sample_at(const uint8_t* p) const {
        float s;
        switch (bytes_) {
        case 1:  s = ((int)p[0] - 128) * (1.0f / 128.0f); break;
        case 2:  { int16_t v; memcpy(&v, p, 2); s = v * (1.0f / 32768.0f); break; }
        case 3:  { int32_t v = (int32_t)(((uint32_t)p[2] << 24) | ((uint32_t)p[1] << 16) |
                                         ((uint32_t)p[0] << 8)) >> 8;
                   s = v * (1.0f / 8388608.0f); break; }
        default:
            if (is_float_) {
                if (bytes_ == 8) { double d; memcpy(&d, p, 8); s = (float)d; }
                else             { memcpy(&s, p, 4); }
                if (!std::isfinite(s)) s = 0.0f;
            } else {
                int32_t v; memcpy(&v, p, 4); s = (float)((double)v * (1.0 / 2147483648.0));
            }
        }
        return s;
    }

    void decode(const uint8_t* raw, int frames, float* dst) const {
        for (int i = 0; i < frames; ++i) {
            const uint8_t* fr = raw + (size_t)i * frame_bytes_;
            float l = sample_at(fr);
            float r = (nch_ > 1) ? sample_at(fr + bytes_) : l;
            dst[2 * i] = l; dst[2 * i + 1] = r;
        }
    }

    static constexpr int RAW_BYTES_MAX = 2048 * 64;      // sanity bound for frame size
    FILE*   f_ = nullptr;
    char    path_[512] = {};
    int     sr_ = 0, nch_ = 0, bytes_ = 0, frame_bytes_ = 0;
    bool    is_float_ = false;
    int64_t data_start_ = 0, n_frames_ = 0, pos_ = -1;
    uint8_t raw_[2048 * 64];
};

// ---------------------------------------------------------------------------
// Playlist (immutable once published)
// ---------------------------------------------------------------------------
struct PlaylistSeg {
    HijackerSegment seg;
    bool fade_in  = true;     // false when this segment continues the previous one
    bool fade_out = true;     // false when the next segment continues this one
};

struct Playlist {
    std::vector<PlaylistSeg> segs;
};

// Producer-side view of a segment in integer timeline frames
struct SegFrames {
    const PlaylistSeg* ps = nullptr;
    int64_t s0 = 0, s1 = 0;
    int     fi = 0, fo = 0;
};

inline float edge_gain(int64_t rel, int64_t len, bool fin, bool fout, int fi, int fo)
{
    float g = 1.0f;
    if (fin && rel < fi)
        g = (float)(rel + 1) / (float)(fi + 1);
    int64_t rem = len - 1 - rel;
    if (fout && rem < fo) {
        float h = (float)(rem + 1) / (float)(fo + 1);
        if (h < g) g = h;
    }
    return g;
}

// Segment gain at frame `rel` (0..len-1): constant, or a straight line from `volume`
// at the start to `volume_end` at the end when the segment carries volume automation.
inline float seg_gain(const HijackerSegment& seg, int64_t rel, int64_t len)
{
    if (seg.volume_end < 0.0f) return seg.volume;
    return seg.volume + (seg.volume_end - seg.volume) * ((float)rel / (float)len);
}

// ---------------------------------------------------------------------------
// Sample-rate-conversion kernel: Kaiser-windowed sinc, 16 zero crossings per
// side, 512 sub-sample phases (linearly interpolated). When the file rate is
// higher than the engine rate the sinc is stretched to the engine Nyquist
// (anti-aliasing). Built once per (file rate, engine rate) pair on the disk
// thread.
// ---------------------------------------------------------------------------
struct SrcKernel {
    static constexpr int PHASES = 512;
    static constexpr int HALF_W = 16;
    int fsr = 0, esr = 0;
    int taps = 0;                     // even
    std::vector<float> tab;           // (PHASES + 1) * taps

    static double bessel_i0(double x) {
        double sum = 1.0, term = 1.0, q = x * x * 0.25;
        for (int k = 1; k < 60; ++k) { term *= q / ((double)k * (double)k); sum += term; if (term < 1e-14 * sum) break; }
        return sum;
    }
    void build(int file_sr, int eng_sr) {
        fsr = file_sr; esr = eng_sr;
        const double ratio = (double)fsr / (double)esr;
        const double scale = std::max(1.0, ratio);                 // stretch when decimating
        taps = 2 * (int)std::ceil(HALF_W * scale);
        const int left = taps / 2 - 1;
        const double beta = 9.0, i0b = bessel_i0(beta);
        tab.assign((size_t)(PHASES + 1) * (size_t)taps, 0.0f);
        const double pi = 3.14159265358979323846;
        for (int ph = 0; ph <= PHASES; ++ph) {
            double frac = (double)ph / (double)PHASES;
            double sum = 0.0;
            std::vector<double> h((size_t)taps);
            for (int j = 0; j < taps; ++j) {
                double x = (double)(j - left) - frac;               // source-sample distance from the output instant
                double xs = x / scale;
                double sinc = (std::fabs(xs) < 1e-12) ? 1.0 : std::sin(pi * xs) / (pi * xs);
                double r = x / (HALF_W * scale);
                double w = (std::fabs(r) >= 1.0) ? 0.0 : bessel_i0(beta * std::sqrt(1.0 - r * r)) / i0b;
                h[(size_t)j] = sinc * w / scale;
                sum += h[(size_t)j];
            }
            for (int j = 0; j < taps; ++j)
                tab[(size_t)ph * (size_t)taps + (size_t)j] = (float)(h[(size_t)j] / sum);   // unity DC gain
        }
    }
};

// ---------------------------------------------------------------------------
// One mixer channel: ring buffer + parameters + consumer/producer state
// ---------------------------------------------------------------------------
struct Stream {
    // ---- parameters written by Python, read by the audio thread -----------
    std::atomic<float> volume   {1.0f};
    std::atomic<bool>  muted    {false};
    std::atomic<bool>  soloed   {false};
    std::atomic<float> pan      {0.5f};
    std::atomic<float> meter_rms  {0.0f};
    std::atomic<float> meter_peak {0.0f};
    std::atomic<float> peak_acc   {0.0f};       // highest peak since get_meter_peak() last read it
    std::atomic<bool>  meters_dirty {false};     // set by clear_channel; the audio thread zeroes the meters

    // ---- playlist hand-off (Python <-> disk thread; never the audio thread)
    std::mutex                      pl_mtx;
    std::shared_ptr<const Playlist> pl_ptr;
    std::atomic<bool>               has_playlist {false};
    std::atomic<uint32_t>           pl_gen {0};
    std::atomic<uint32_t>           reader_gen {0};      // disk thread: last pl_gen whose file it has released

    // ---- consumer (audio thread) -> producer (disk thread) ----------------
    std::atomic<uint32_t> req_epoch  {0};
    std::atomic<int64_t>  req_frame  {0};
    std::atomic<int64_t>  read_pos   {0};
    // ---- producer -> consumer ---------------------------------------------
    std::atomic<uint32_t> served_epoch {0};
    std::atomic<int64_t>  ring_end     {0};
    std::vector<float>    ring;                  // RING_FRAMES * 2

    // ---- consumer-only (audio thread) -------------------------------------
    uint32_t expect_epoch = 0;
    uint32_t seen_gen     = 0;
    float    fader_g      = -1.0f;               // smoothed pre-DSP gain (<0 = unset)
    float    mute_g       = -1.0f;                // smoothed mute/solo gain
    float    pan_c        = 0.5f;                // smoothed pan
    int      fade_in_left = 0;
    bool     was_ready    = false;

    // ---- producer-only (disk thread) --------------------------------------
    uint32_t                         p_epoch = 0;
    uint32_t                         p_gen_seen = 0;     // playlist generation the reader state belongs to
    bool                             p_stale = false;    // playlist replaced; wait for a new epoch
    int64_t                          p_end   = 0;
    std::shared_ptr<const Playlist>  p_pl;
    std::vector<SegFrames>           p_segs;
    WavReader                        reader;
    char                             failed_path[512] = {};
};

// Everything that depends on the output sample rate, so the same render code can
// run for the live stream (device rate) and for an offline render (any rate).
struct RenderCtx {
    int   sr = 44100;
    float k_fader = 0.0f, k_mute = 0.0f, k_pan = 0.0f;
    SrcKernel kernels[4];
    int   n_kernels = 0;
    void set_rate(int rate) {
        sr = rate;
        const float fs = (float)rate;
        k_fader = 1.0f - std::exp(-1.0f / (0.004f * fs));
        k_mute  = 1.0f - std::exp(-1.0f / (0.004f * fs));
        k_pan   = 1.0f - std::exp(-1.0f / (0.010f * fs));
        n_kernels = 0;
    }
};

#if defined(_MSC_VER)
#pragma warning(push)
#pragma warning(disable : 4324)   // padded because of alignas(16) on the mix buffers - intentional
#endif
// State of one offline render (see HijackerEngine::offline_begin). It owns private
// copies of the per-channel playback state and its own file readers, so it never
// touches the disk thread's data.
struct OfflineJob {
    RenderCtx ctx;
    std::unique_ptr<Stream> st[PB_MAX_CHANNELS];
    bool    used[PB_MAX_CHANNELS] = {};
    bool    any_solo = false;
    bool    respect_ms = true;
    bool    apply_master = true;
    float   master_gain = 1.0f;
    int64_t frame = 0;
    hjl::LoudnessMeter meter;                 // loudness of this render's output
    alignas(16) float chbuf[MAX_BLOCK * 2];
    alignas(16) float mixbuf[MAX_BLOCK * 2];
};
#if defined(_MSC_VER)
#pragma warning(pop)
#endif

// Smoothed gains (fader, mute, pan, master) jump to their target once they are this
// close. It has to be well above the float resolution: with a one-pole smoother the
// remaining distance stops shrinking when each step is smaller than half a float
// step, which happens at about 3e-5 for a 10 ms smoother, so a smaller threshold
// is never reached and the gain stays slightly off target for ever.
constexpr float kSnap = 1e-4f;

// Master-stage safety net, shared by the live callback and the offline renderer:
// sanitises NaN/runaway samples and (optionally) soft-limits. Transparent below
// 0.95. Above it the level is bent smoothly towards 1.0 and never reaches it:
//   y = 0.95 + o / (1 + o/0.05),  o = |x| - 0.95   (slope 1 at the knee, asymptote 1.0)
// Returns the largest |sample| seen before limiting (the master meter reading).
inline float hj_limit_block(float* out, unsigned long n_out, bool limit)
{
    float pk = 0.0f;
    for (unsigned long i = 0; i < n_out; ++i) {
        float s = out[i];
        if (!(s == s) || s > 1e6f || s < -1e6f) { out[i] = 0.0f; continue; }   // NaN / runaway
        { const float a = s < 0.0f ? -s : s; if (a > pk) pk = a; }
        if (!limit) continue;
        if (s > 0.95f) {
            float over = s - 0.95f;
            out[i] = 0.95f + over / (1.0f + over * 20.0f);
        } else if (s < -0.95f) {
            float over = -s - 0.95f;
            out[i] = -(0.95f + over / (1.0f + over * 20.0f));
        }
    }
    return pk;
}

inline HijackerLoudness to_public(const hjl::Result& r)
{
    HijackerLoudness o;
    o.momentary = r.momentary; o.short_term = r.short_term; o.integrated = r.integrated; o.lra = r.lra;
    o.momentary_max = r.momentary_max; o.short_term_max = r.short_term_max;
    o.true_peak_db = r.true_peak_db; o.sample_peak_db = r.sample_peak_db; o.duration_s = r.duration_s;
    return o;
}

} // namespace

// =============================================================================
// Impl
// =============================================================================
#if defined(_MSC_VER)
#pragma warning(push)
#pragma warning(disable : 4324)   // padded because of alignas(16) on the mix buffers - intentional
#endif
struct HijackerEngine::Impl {
    Stream streams[PB_MAX_CHANNELS];

    // worker
    std::thread             worker;
    std::atomic<bool>       quit {false};
    std::mutex              wake_mtx;
    std::condition_variable wake_cv;
    bool                    worker_running = false;

    // output device selection (set before init)
    std::string device_pref;          // wanted device name ("" = system default)
    std::string device_active;        // device the stream was opened on

    // engine rate (set before the worker / stream start)
    int   sr = 44100;
    RenderCtx live;                   // render settings for the live stream (rate, smoothing, SRC kernels)

    // offline rendering (HijackerEngine::offline_begin / _render / _end)
    std::atomic<bool> offline_active {false};   // audio callback stays silent while true
    std::atomic<int>  cb_busy {0};              // 1 while the audio callback is running
    std::unique_ptr<OfflineJob> job;

    // transport (Python -> audio thread)
    std::atomic<bool>     playing      {false};
    std::atomic<bool>     stop_req     {false};
    std::atomic<int64_t>  pending_seek {NO_SEEK};
    std::atomic<uint32_t> play_counter {0};
    // published by the audio thread
    std::atomic<double>   playhead_s   {0.0};
    std::atomic<int>      xruns        {0};
    std::atomic<double>   out_latency_s{0.0};
    // Latency compensation: the sound for timeline position X leaves the speakers
    // `output latency` after the callback that produced it (plus, at play start,
    // the pre-roll wait), so the engine renders that much *ahead* of the cursor
    // Python gives it. comp_s < 0 = automatic, 0 = off, > 0 = fixed seconds.
    std::atomic<double>   comp_s       {-1.0};
    std::atomic<int64_t>  bias_frames  {0};      // frames added to the last play()/seek() position
    std::atomic<double>   hold_est_s   {0.0};    // measured play()->first-audio delay (smoothed)
    std::atomic<int64_t>  play_t0_ns   {0};      // steady-clock time of the last play()
    // Master bus: a fader after the channel sum and before the safety limiter.
    // master_target is set from Python; master_cur is the smoothed value the
    // audio thread actually applies (touched only by the audio thread).
    std::atomic<float>    master_target{1.0f};
    float                 master_cur   {1.0f};
    std::atomic<float>    master_peak  {0.0f};   // max |sample| since get_master_peak() last read it
    std::atomic<bool>     master_clip  {false};  // latched until clear_master_clip()
    // Loudness of the master output. `loud` is touched only by the audio thread; the readings
    // are published through `loud_pub` (one atomic per field) for Python to read.
    hjl::LoudnessMeter    loud;
    std::atomic<bool>     loud_ok      {false};  // loud.init() has run for the current stream
    std::atomic<bool>     loud_reset   {false};  // asks the audio thread to restart the measurement
    static constexpr int  LOUD_FIELDS = 9;
    std::atomic<double>   loud_pub[LOUD_FIELDS];
    HijackerLoudness      off_loud;              // result of the last offline render

    // audio-thread-only state
    int64_t  play_frame   = 0;
    uint32_t seen_play    = 0;
    bool     hold         = false;
    int64_t  hold_waited  = 0;
    int      fadeout_pos  = 0;
    bool     any_solo     = false;
    alignas(16) float chbuf[MAX_BLOCK * 2];
    alignas(16) float mixbuf[MAX_BLOCK * 2];

    Impl() { for (int i = 0; i < LOUD_FIELDS; ++i) loud_pub[i].store(i == 3 || i == 8 ? 0.0 : -200.0, std::memory_order_relaxed); }

    // ---- control / helpers --------------------------------------------------
    void start_worker(int rate);
    void stop_worker();
    void worker_main();
    void wake() { std::lock_guard<std::mutex> lk(wake_mtx); wake_cv.notify_one(); }
    // Wait (bounded) until the disk thread has released the files of playlist generation `gen`.
    void wait_released(Stream& s, uint32_t gen);

    // producer
    bool service(Stream& s);
    void producer_reset(Stream& s, uint32_t epoch);
    void adopt_playlist(Stream& s, int rate);
    void render_block(Stream& s, int64_t frame0, int n, float* out, RenderCtx& c);
    void render_piece(Stream& s, const SegFrames& sf, int64_t i0, int64_t i1,
                      int64_t frame0, float* out, RenderCtx& c);

    // consumer
    void issue_request(Stream& s, int64_t frame);
    void sync_streams();
    void do_seek(int64_t frame);
    bool stream_ready(Stream& s, int need);
    void mix_chunk(float* out, int n);
    // fader -> DSP -> meters -> pan/mute/declick -> added into `out`, for one channel's block `cb`
    void mix_stream(Stream& s, int ch, float* cb, int n, float* out, RenderCtx& c,
                    bool any_solo_f, bool live_meters, bool respect_ms);
    bool offline_begin(double start_s, int rate, uint32_t mask, bool respect_ms, bool apply_master);
    int  offline_render(float* out, int n);
    void offline_end();
};
#if defined(_MSC_VER)
#pragma warning(pop)
#endif

// -----------------------------------------------------------------------------
// Worker lifecycle
// -----------------------------------------------------------------------------
void HijackerEngine::Impl::start_worker(int rate)
{
    if (worker_running) return;
    sr = rate;
    live.set_rate(rate);
    loud_ok.store(false, std::memory_order_seq_cst);
    loud.init(rate);
    for (int i = 0; i < LOUD_FIELDS; ++i) loud_pub[i].store(i == 3 || i == 8 ? 0.0 : -200.0, std::memory_order_relaxed);
    loud_ok.store(true, std::memory_order_seq_cst);

    for (int i = 0; i < PB_MAX_CHANNELS; ++i) {
        Stream& s = streams[i];
        s.ring.assign((size_t)RING_FRAMES * 2, 0.0f);
        s.expect_epoch = 0;
        s.seen_gen     = s.pl_gen.load() - 1;    // force a request on first sight of a playlist
        s.p_epoch = s.req_epoch.load();
        s.served_epoch.store(s.p_epoch);
        s.ring_end.store(0);
        s.p_end = 0;
        s.fade_in_left = 0;
        s.fader_g = -1.0f; s.mute_g = -1.0f; s.pan_c = s.pan.load();   // <0: snap to target on first use
        s.was_ready = false;
    }
    play_frame = 0;
    hold = false;
    fadeout_pos = 0;

    quit.store(false);
    worker = std::thread([this] { worker_main(); });
    worker_running = true;
}

void HijackerEngine::Impl::stop_worker()
{
    if (!worker_running) return;
    quit.store(true);
    wake();
    if (worker.joinable()) worker.join();
    worker_running = false;
    for (int i = 0; i < PB_MAX_CHANNELS; ++i) {
        streams[i].reader.close();
        streams[i].p_pl.reset();
        streams[i].p_segs.clear();
    }
}

void HijackerEngine::Impl::worker_main()
{
#if defined(_WIN32)
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
#endif
    while (!quit.load(std::memory_order_acquire)) {
        bool did = false;
        for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch)
            did |= service(streams[ch]);
        if (!did) {
            std::unique_lock<std::mutex> lk(wake_mtx);
            wake_cv.wait_for(lk, std::chrono::milliseconds(2));
        }
    }
}

void HijackerEngine::Impl::wait_released(Stream& s, uint32_t gen)
{
    if (!worker_running) return;
    auto t0 = std::chrono::steady_clock::now();
    while ((int32_t)(s.reader_gen.load(std::memory_order_acquire) - gen) < 0) {
        if (std::chrono::steady_clock::now() - t0 > std::chrono::milliseconds(50)) break;   // never hang the caller
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
}

// -----------------------------------------------------------------------------
// Producer
// -----------------------------------------------------------------------------
void HijackerEngine::Impl::adopt_playlist(Stream& s, int rate)
{
    {
        std::lock_guard<std::mutex> lk(s.pl_mtx);
        s.p_pl = s.pl_ptr;
    }
    s.p_segs.clear();
    if (!s.p_pl) return;
    const double fs = (double)rate;
    for (const PlaylistSeg& ps : s.p_pl->segs) {
        SegFrames sf;
        sf.ps = &ps;
        sf.s0 = (int64_t)std::llround(ps.seg.timeline_pos_s * fs);
        sf.s1 = (int64_t)std::llround((ps.seg.timeline_pos_s + ps.seg.duration_s) * fs);
        if (sf.s1 <= sf.s0) continue;
        int half = (int)std::min<int64_t>((sf.s1 - sf.s0) / 2, EDGE_FADE_FRAMES);
        sf.fi = sf.fo = half;
        s.p_segs.push_back(sf);
    }
}

void HijackerEngine::Impl::producer_reset(Stream& s, uint32_t epoch)
{
    s.p_epoch = epoch;
    uint32_t g = s.pl_gen.load(std::memory_order_acquire);
    adopt_playlist(s, live.sr);
    s.p_gen_seen = g;
    s.p_stale = false;
    s.reader.close();
    s.failed_path[0] = 0;
    int64_t f0 = s.req_frame.load(std::memory_order_acquire);
    int64_t rp = s.read_pos.load(std::memory_order_acquire);
    s.p_end = std::max(f0, rp);
    s.ring_end.store(s.p_end, std::memory_order_release);
    s.served_epoch.store(epoch, std::memory_order_release);
}

void HijackerEngine::Impl::render_piece(Stream& s, const SegFrames& sf,
                                        int64_t i0, int64_t i1,
                                        int64_t frame0, float* out, RenderCtx& c)
{
    const HijackerSegment& seg = sf.ps->seg;
    const int64_t len = sf.s1 - sf.s0;

    if (!s.reader.is_open_for(seg.filepath)) {
        if (strcmp(s.failed_path, seg.filepath) == 0) return;       // already failed
        if (!s.reader.open(seg.filepath)) {
            printf("[HIJACKER] cannot open/parse %s\n", seg.filepath);
            snprintf(s.failed_path, sizeof(s.failed_path), "%s", seg.filepath);
            return;
        }
    }
    const int fsr = s.reader.sample_rate();

    static thread_local std::vector<float> tmp;
    static thread_local std::vector<float> src;
    if (tmp.empty()) { tmp.resize((size_t)PROD_BLOCK * 2); src.resize((size_t)SRC_CAP * 2); }

    if (fsr == c.sr) {
        const int64_t src0 = (int64_t)std::llround(seg.file_offset_s * (double)fsr) + (i0 - sf.s0);
        const int cnt = (int)(i1 - i0);
        s.reader.read_frames(src0, cnt, tmp.data());
        for (int k = 0; k < cnt; ++k) {
            int64_t rel = i0 - sf.s0 + k;
            float g = seg_gain(seg, rel, len) * edge_gain(rel, len, sf.ps->fade_in, sf.ps->fade_out, sf.fi, sf.fo);
            float* o = out + 2 * (i0 - frame0 + k);
            o[0] += tmp[2 * k]     * g;
            o[1] += tmp[2 * k + 1] * g;
        }
        return;
    }

    // Sample-rate conversion (windowed sinc). The phase is derived from the
    // absolute timeline position, so it is continuous across blocks (no drift,
    // no clicks at block joins).
    const double ratio = (double)fsr / (double)c.sr;
    if (ratio > 16.0 || ratio < 1.0 / 16.0) return;
    SrcKernel* kern = nullptr;
    for (int i = 0; i < c.n_kernels; ++i)
        if (c.kernels[i].fsr == fsr && c.kernels[i].esr == c.sr) { kern = &c.kernels[i]; break; }
    if (!kern) {
        int idx = (c.n_kernels < 4) ? c.n_kernels++ : 0;
        c.kernels[idx].build(fsr, c.sr);
        kern = &c.kernels[idx];
    }
    const int T    = kern->taps;
    const int left = T / 2 - 1;
    const int sub  = std::max(16, std::min(512, (int)((SRC_CAP - T - 16) / std::max(ratio, 1.0))));
    const double base = seg.file_offset_s * (double)fsr;

    for (int64_t a = i0; a < i1; a += sub) {
        const int m = (int)std::min<int64_t>(sub, i1 - a);
        const double pos0 = base + (double)(a - sf.s0) * ratio;
        const int64_t lo = (int64_t)std::floor(pos0) - left;
        const int64_t hi = (int64_t)std::floor(pos0 + (double)(m - 1) * ratio) + T / 2;
        const int cnt = (int)(hi - lo + 1);
        s.reader.read_frames(lo, cnt, src.data());
        for (int k = 0; k < m; ++k) {
            const double p = pos0 + (double)k * ratio;
            const int64_t ip = (int64_t)std::floor(p);
            const double fph = (p - (double)ip) * SrcKernel::PHASES;
            int ph = (int)fph;
            const float w = (float)(fph - (double)ph);
            const float* k0 = &kern->tab[(size_t)ph * (size_t)T];
            const float* k1 = k0 + T;
            const float* y  = src.data() + 2 * (ip - left - lo);
            float accl = 0.0f, accr = 0.0f;
            for (int j = 0; j < T; ++j) {
                float c = k0[j] + w * (k1[j] - k0[j]);
                accl += c * y[2 * j];
                accr += c * y[2 * j + 1];
            }
            int64_t rel = a - sf.s0 + k;
            float g = seg_gain(seg, rel, len) * edge_gain(rel, len, sf.ps->fade_in, sf.ps->fade_out, sf.fi, sf.fo);
            float* d = out + 2 * (a - frame0 + k);
            d[0] += accl * g;
            d[1] += accr * g;
        }
    }
}

void HijackerEngine::Impl::render_block(Stream& s, int64_t frame0, int n, float* out, RenderCtx& c)
{
    memset(out, 0, sizeof(float) * 2 * (size_t)n);
    const int64_t frame1 = frame0 + n;
    for (const SegFrames& sf : s.p_segs) {
        if (sf.s1 <= frame0 || sf.s0 >= frame1) continue;
        render_piece(s, sf, std::max(frame0, sf.s0), std::min(frame1, sf.s1), frame0, out, c);
    }
}

// Returns true if any work was done.
bool HijackerEngine::Impl::service(Stream& s)
{
    // The playlist was replaced or cleared: release the file right away (the
    // Python side waits for this, so it can safely delete or rewrite the WAV)
    // and stop rendering the old playlist until the audio thread asks for a
    // fresh epoch.
    uint32_t g = s.pl_gen.load(std::memory_order_acquire);
    if (g != s.p_gen_seen) {
        s.reader.close();
        s.p_gen_seen = g;
        s.p_stale = true;
        s.reader_gen.store(g, std::memory_order_release);
    }
    if (!s.has_playlist.load(std::memory_order_acquire)) {
        if (s.p_pl) { s.p_pl.reset(); s.p_segs.clear(); }
        return false;
    }

    bool did = false;
    uint32_t e = s.req_epoch.load(std::memory_order_acquire);
    if (e != s.p_epoch) { producer_reset(s, e); did = true; }
    if (s.p_stale) return did;
    if (s.served_epoch.load(std::memory_order_relaxed) != s.p_epoch) return did;

    float blk[PROD_BLOCK * 2];
    for (int iter = 0; iter < 4; ++iter) {
        if (s.req_epoch.load(std::memory_order_acquire) != s.p_epoch) break;   // new request pending
        int64_t rp = s.read_pos.load(std::memory_order_acquire);
        if (rp > s.p_end) {                    // consumer ran past us (underrun): skip ahead
            s.p_end = rp;
            s.ring_end.store(rp, std::memory_order_release);
        }
        if (s.p_end - rp >= PREFETCH_FRAMES) break;
        if (s.p_end + PROD_BLOCK - rp > RING_FRAMES) break;

        render_block(s, s.p_end, PROD_BLOCK, blk, live);

        // copy into the ring (at most two pieces across the wrap point)
        int slot = (int)(s.p_end & RING_MASK);
        int first = std::min(PROD_BLOCK, RING_FRAMES - slot);
        memcpy(&s.ring[(size_t)slot * 2], blk, sizeof(float) * 2 * (size_t)first);
        if (first < PROD_BLOCK)
            memcpy(&s.ring[0], blk + 2 * first, sizeof(float) * 2 * (size_t)(PROD_BLOCK - first));

        s.p_end += PROD_BLOCK;
        s.ring_end.store(s.p_end, std::memory_order_release);
        did = true;
    }
    return did;
}

// -----------------------------------------------------------------------------
// Consumer (audio thread)
// -----------------------------------------------------------------------------
void HijackerEngine::Impl::issue_request(Stream& s, int64_t frame)
{
    s.read_pos.store(frame, std::memory_order_release);
    s.req_frame.store(frame, std::memory_order_release);
    s.expect_epoch = s.req_epoch.fetch_add(1, std::memory_order_acq_rel) + 1;
    s.seen_gen = s.pl_gen.load(std::memory_order_acquire);
    s.was_ready = false;
}

void HijackerEngine::Impl::sync_streams()
{
    for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch) {
        Stream& s = streams[ch];
        uint32_t g = s.pl_gen.load(std::memory_order_acquire);
        if (!s.has_playlist.load(std::memory_order_acquire)) {
            s.seen_gen = g;
            if (s.meters_dirty.exchange(false, std::memory_order_acq_rel)) {
                s.meter_rms.store(0.0f);
                s.meter_peak.store(0.0f);
                s.peak_acc.store(0.0f, std::memory_order_relaxed);
                g_state.meter_levels[ch] = 0.0f;
            }
            continue;
        }
        s.meters_dirty.store(false, std::memory_order_relaxed);
        if (g != s.seen_gen) {
            issue_request(s, play_frame);
            s.fade_in_left = DECLICK_FRAMES;
        }
    }
}

bool HijackerEngine::Impl::stream_ready(Stream& s, int need)
{
    if (s.served_epoch.load(std::memory_order_acquire) != s.expect_epoch) return false;
    int64_t end = s.ring_end.load(std::memory_order_acquire);
    return (end - play_frame) >= need;
}

void HijackerEngine::Impl::do_seek(int64_t frame)
{
    if (frame < 0) frame = 0;

    // Seeking to where we already are, with every stream primed, needs no flush
    // (this is the normal "resume after stop" case).
    bool noop = (frame == play_frame);
    if (noop) {
        for (int ch = 0; ch < PB_MAX_CHANNELS && noop; ++ch) {
            Stream& s = streams[ch];
            if (!s.has_playlist.load(std::memory_order_acquire)) continue;
            if (s.pl_gen.load(std::memory_order_acquire) != s.seen_gen ||
                s.served_epoch.load(std::memory_order_acquire) != s.expect_epoch)
                noop = false;
        }
    }

    play_frame = frame;
    playhead_s.store((double)(frame - bias_frames.load(std::memory_order_relaxed)) / (double)sr, std::memory_order_release);

    for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch) {
        Stream& s = streams[ch];
        if (!s.has_playlist.load(std::memory_order_acquire)) continue;
        s.fade_in_left = DECLICK_FRAMES;
        if (!noop) {
            issue_request(s, frame);
            // tails / filter memory from before the jump must not leak into the new audio
            hj_reset_dsp_for_active_fx(ch);
        }
    }
}

namespace {
inline void pan_gains(float pan, float& gl, float& gr)
{
#if HJ_PAN_LAW == 1
    if (pan >= 0.5f) { gl = std::cos((pan - 0.5f) * 3.14159265f); gr = 1.0f; }
    else             { gl = 1.0f; gr = std::cos((0.5f - pan) * 3.14159265f); }
    if (gl < 0.0f) gl = 0.0f;
    if (gr < 0.0f) gr = 0.0f;
#else
    if (std::fabs(pan - 0.5f) > 0.01f) {
        float a = pan * 1.5707963f;
        gl = std::cos(a); gr = std::sin(a);
    } else { gl = gr = 1.0f; }
#endif
}
} // namespace

void HijackerEngine::Impl::mix_stream(Stream& s, int ch, float* cb, int n, float* out, RenderCtx& c,
                                      bool any_solo_f, bool live_meters, bool respect_ms)
{
    // ---- mute / solo / fader targets ----------------------------------
    const bool  silent_t = respect_ms &&
                           (s.muted.load(std::memory_order_relaxed) ||
                            (any_solo_f && !s.soloed.load(std::memory_order_relaxed)));
    const float vol_t    = s.volume.load(std::memory_order_relaxed);
    const float mute_t   = silent_t ? 0.0f : 1.0f;
    if (s.fader_g < 0.0f) s.fader_g = vol_t;      // first use: no ramp from zero
    if (s.mute_g  < 0.0f) s.mute_g  = mute_t;

    if ((mute_t == 0.0f && s.mute_g <= 1e-4f) || (vol_t < 1e-4f && s.fader_g < 1e-4f)) {
        // Fully silent: skip DSP and mixing (as before), keep meters at zero.
        if (mute_t == 0.0f) s.mute_g = 0.0f;
        if (vol_t < 1e-4f)  s.fader_g = vol_t;
        if (live_meters) {
            s.meter_rms.store(0.0f);
            s.meter_peak.store(0.0f);
            g_state.meter_levels[ch] = 0.0f;
        }
        return;
    }

    // ---- fader (pre-DSP, so the compressor sees the right level) -------
    {
        float g = s.fader_g;
        if (g == vol_t) {
            if (g != 1.0f)
                for (int i = 0; i < n * 2; ++i) cb[i] *= g;
        } else {
            for (int i = 0; i < n; ++i) {
                g += (vol_t - g) * c.k_fader;
                if (std::fabs(vol_t - g) < kSnap) g = vol_t;
                cb[2 * i] *= g; cb[2 * i + 1] *= g;
            }
            s.fader_g = g;
        }
    }

    // ---- DSP effect chain ---------------------------------------------
    {
        if (hj_channel_has_fx(ch)) apply_effect_chain_batch(ch, cb, n, 2, (float)c.sr);
    }

    // ---- metering (post-fader, post-DSP) + runaway protection ---------
    {
        float rms_sum = 0.0f, peak = 0.0f;
        for (int i = 0; i < n * 2; ++i) {
            float v = cb[i];
            rms_sum += v * v;
            float a = v < 0 ? -v : v;
            if (a > peak) peak = a;
        }
        if (!std::isfinite(rms_sum)) {
            // A NaN/Inf got into the IIR state: it would never recover by itself.
            hj_reset_channel_dsp(ch);
            memset(cb, 0, sizeof(float) * 2 * (size_t)n);
            rms_sum = 0.0f; peak = 0.0f;
        }
        float rms = std::sqrt(rms_sum / (float)(n * 2));
        if (live_meters) {
            s.meter_rms.store(rms);
            s.meter_peak.store(peak);
            {   // keep the loudest peak until Python reads it, so a poll never misses a short peak
                float prev = s.peak_acc.load(std::memory_order_relaxed);
                while (peak > prev && !s.peak_acc.compare_exchange_weak(prev, peak, std::memory_order_relaxed)) {}
            }
            g_state.meter_levels[ch] = rms;
        }
    }

    // ---- mute ramp, pan, declick fade-in, mix ---------------------------
    {
        const float pan_t = s.pan.load(std::memory_order_relaxed);
        float pc = s.pan_c, mg = s.mute_g;
        float gl, gr;
        pan_gains(pc, gl, gr);
        int fl = s.fade_in_left;
        for (int i = 0; i < n; ++i) {
            if (mg != mute_t) {
                mg += (mute_t - mg) * c.k_mute;
                if (std::fabs(mute_t - mg) < kSnap) mg = mute_t;
            }
            if (pc != pan_t) {
                pc += (pan_t - pc) * c.k_pan;
                if (std::fabs(pan_t - pc) < kSnap) pc = pan_t;
                pan_gains(pc, gl, gr);
            }
            float fg = 1.0f;
            if (fl > 0) { fg = 1.0f - (float)fl / (float)(DECLICK_FRAMES + 1); --fl; }
            float g = mg * fg;
            out[2 * i]     += cb[2 * i]     * gl * g;
            out[2 * i + 1] += cb[2 * i + 1] * gr * g;
        }
        s.pan_c = pc; s.mute_g = mg; s.fade_in_left = fl;
    }
}

void HijackerEngine::Impl::mix_chunk(float* out, int n)
{
    for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch) {
        Stream& s = streams[ch];
        if (!s.has_playlist.load(std::memory_order_acquire)) continue;

        // ---- pull this channel's audio for the chunk from its ring --------
        float* cb = chbuf;
        int got = 0;
        bool ready = (s.served_epoch.load(std::memory_order_acquire) == s.expect_epoch);
        if (ready) {
            int64_t end   = s.ring_end.load(std::memory_order_acquire);
            int64_t avail = end - play_frame;
            got = (int)std::max<int64_t>(0, std::min<int64_t>(avail, n));
            if (got > 0) {
                int slot = (int)(play_frame & RING_MASK);
                int first = std::min(got, RING_FRAMES - slot);
                memcpy(cb, &s.ring[(size_t)slot * 2], sizeof(float) * 2 * (size_t)first);
                if (first < got)
                    memcpy(cb + 2 * first, &s.ring[0], sizeof(float) * 2 * (size_t)(got - first));
            }
            if (got < n) xruns.fetch_add(1, std::memory_order_relaxed);
        }
        if (got < n) memset(cb + 2 * got, 0, sizeof(float) * 2 * (size_t)(n - got));
        s.read_pos.store(play_frame + n, std::memory_order_release);

        if (!ready || got < n) {
            s.was_ready = false;
        } else if (!s.was_ready) {
            s.was_ready = true;
            if (s.fade_in_left < DECLICK_FRAMES) s.fade_in_left = DECLICK_FRAMES;
        }

        mix_stream(s, ch, cb, n, out, live, any_solo, true, true);
    }
    play_frame += n;
}

// =============================================================================
// PortAudio glue
// =============================================================================
static int _pa_callback(const void* input, void* output,
                         unsigned long frames,
                         const PaStreamCallbackTimeInfo* /*time_info*/,
                         PaStreamCallbackFlags flags,
                         void* user_data)
{
    HijackerEngine* eng = static_cast<HijackerEngine*>(user_data);
    if (flags & (paOutputUnderflow | paOutputOverflow)) eng->note_device_xrun();
    return eng->audio_callback(input, output, frames);
}

// -----------------------------------------------------------------------------
// Offline render: the same per-channel mix code as the live callback, run on the
// caller's thread against private copies of the playback state.
// -----------------------------------------------------------------------------
bool HijackerEngine::Impl::offline_begin(double start_s, int rate, uint32_t mask,
                                         bool respect_ms, bool apply_master)
{
    if (job) { printf("[HIJACKER] offline render already running\n"); return false; }
    if (playing.load(std::memory_order_acquire)) {
        printf("[HIJACKER] offline render refused: playback is running (stop it first)\n");
        return false;
    }
    if (rate < 8000 || rate > 384000) return false;

    // Keep the live callback silent, and wait for one that is already running to finish.
    offline_active.store(true, std::memory_order_seq_cst);
    auto t0 = std::chrono::steady_clock::now();
    while (cb_busy.load(std::memory_order_seq_cst) != 0 &&
           std::chrono::steady_clock::now() - t0 < std::chrono::milliseconds(250))
        std::this_thread::sleep_for(std::chrono::microseconds(100));

    auto j = std::make_unique<OfflineJob>();
    j->ctx.set_rate(rate);
    j->respect_ms    = respect_ms;
    j->apply_master  = apply_master;
    j->master_gain   = master_target.load(std::memory_order_relaxed);
    j->meter.init(rate);
    off_loud = HijackerLoudness();
    for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch)
        if (streams[ch].soloed.load(std::memory_order_relaxed)) j->any_solo = true;

    for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch) {
        if (!((mask >> ch) & 1u)) continue;
        Stream& ls = streams[ch];
        if (!ls.has_playlist.load(std::memory_order_acquire)) continue;
        auto t = std::make_unique<Stream>();
        {
            std::lock_guard<std::mutex> lk(ls.pl_mtx);
            t->pl_ptr = ls.pl_ptr;
        }
        t->volume.store(ls.volume.load());
        t->muted.store(ls.muted.load());
        t->soloed.store(ls.soloed.load());
        t->pan.store(ls.pan.load());
        t->pan_c = t->pan.load();
        t->fader_g = -1.0f; t->mute_g = -1.0f;       // snap to target on first use
        t->fade_in_left = 0; t->was_ready = false;
        adopt_playlist(*t, rate);
        if (t->p_segs.empty()) continue;
        hj_reset_dsp_for_active_fx(ch);              // start from clean filter / reverb / delay state
        j->used[ch] = true;
        j->st[ch] = std::move(t);
    }
    j->frame = std::max<int64_t>(0, (int64_t)std::llround(start_s * (double)rate));
    job = std::move(j);
    return true;
}

int HijackerEngine::Impl::offline_render(float* out, int n)
{
    if (!job || n <= 0) return 0;
    OfflineJob& j = *job;
    int done = 0;
    while (done < n) {
        const int m = std::min(MAX_BLOCK, n - done);
        float* mix = j.mixbuf;
        memset(mix, 0, sizeof(float) * 2 * (size_t)m);
        for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch) {
            if (!j.used[ch]) continue;
            Stream& s = *j.st[ch];
            render_block(s, j.frame, m, j.chbuf, j.ctx);
            if (!s.was_ready) {                       // same start-of-audio declick the live path applies
                s.was_ready = true;
                if (s.fade_in_left < DECLICK_FRAMES) s.fade_in_left = DECLICK_FRAMES;
            }
            mix_stream(s, ch, j.chbuf, m, mix, j.ctx, j.any_solo, false, j.respect_ms);
        }
        if (j.master_gain != 1.0f)
            for (int i = 0; i < m * 2; ++i) mix[i] *= j.master_gain;
        hj_limit_block(mix, (unsigned long)m * 2, j.apply_master);
        j.meter.process(mix, m);
        memcpy(out + (size_t)done * 2, mix, sizeof(float) * 2 * (size_t)m);
        j.frame += m;
        done += m;
    }
    return done;
}

void HijackerEngine::Impl::offline_end()
{
    if (job) {
        off_loud = to_public(job->meter.result());
        for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch)
            if (job->used[ch]) hj_reset_dsp_for_active_fx(ch);   // don't leak reverb tails into live playback
        job.reset();                                              // closes the render's own file readers
    }
    offline_active.store(false, std::memory_order_seq_cst);
}

HijackerEngine::HijackerEngine() : impl_(new Impl) {}

HijackerEngine::~HijackerEngine() {
    shutdown();
}

// ---------------------------------------------------------------------------
// Output devices
// ---------------------------------------------------------------------------
namespace {
struct HostPick { PaHostApiIndex idx = -1; const char* label = "default"; };

// The driver the engine uses: WASAPI, else WMME, else the platform default (idx -1).
HostPick pick_host_api()
{
    PaHostApiIndex wasapi = -1, wmme = -1;
    const int n = Pa_GetHostApiCount();
    for (int i = 0; i < n; ++i) {
        const PaHostApiInfo* api = Pa_GetHostApiInfo(i);
        if (!api) continue;
        if (api->type == paMME)    wmme   = i;
        if (api->type == paWASAPI) wasapi = i;
    }
    if (wasapi >= 0 && Pa_GetHostApiInfo(wasapi)->defaultOutputDevice != paNoDevice)
        return {wasapi, "WASAPI"};
    if (wmme >= 0 && Pa_GetHostApiInfo(wmme)->defaultOutputDevice != paNoDevice)
        return {wmme, "WMME"};
    return {};
}
} // namespace

std::vector<HijackerOutputDevice> hj_list_output_devices()
{
    std::vector<HijackerOutputDevice> out;
    if (Pa_Initialize() != paNoError) return out;          // (reference counted: safe while running)
    const HostPick host = pick_host_api();
    PaDeviceIndex default_dev = (host.idx >= 0) ? Pa_GetHostApiInfo(host.idx)->defaultOutputDevice
                                                : Pa_GetDefaultOutputDevice();
    const int n = Pa_GetDeviceCount();
    for (int i = 0; i < n; ++i) {
        const PaDeviceInfo* di = Pa_GetDeviceInfo(i);
        if (!di || di->maxOutputChannels < 2) continue;
        if (host.idx >= 0 && di->hostApi != host.idx) continue;
        HijackerOutputDevice d;
        d.index = i;
        d.name = di->name ? di->name : "";
        const PaHostApiInfo* api = Pa_GetHostApiInfo(di->hostApi);
        d.host_api = (api && api->name) ? api->name : "";
        d.max_output_channels = di->maxOutputChannels;
        d.default_sample_rate = di->defaultSampleRate;
        d.low_latency_s  = di->defaultLowOutputLatency;
        d.high_latency_s = di->defaultHighOutputLatency;
        d.is_default = (i == default_dev);
        out.push_back(std::move(d));
    }
    Pa_Terminate();
    return out;
}

void HijackerEngine::set_output_device(const std::string& name) { impl_->device_pref = name; }
std::string HijackerEngine::get_output_device_name() const { return impl_->device_active; }
int HijackerEngine::get_buffer_frames() const { return buffer_frames_; }

bool HijackerEngine::init(int sample_rate, int buffer_frames) {
    if (initialized_) return true;

    sample_rate_   = sample_rate;
    buffer_frames_ = buffer_frames;

    PaError err = Pa_Initialize();
    if (err != paNoError) {
        printf("[HIJACKER] PortAudio init failed: %s\n", Pa_GetErrorText(err));
        return false;
    }

    // Print all available host APIs for diagnostics
    int n_apis = Pa_GetHostApiCount();
    printf("[HIJACKER] Available host APIs:\n");
    for (int i = 0; i < n_apis; ++i) {
        const PaHostApiInfo* api = Pa_GetHostApiInfo(i);
        printf("[HIJACKER]   [%d] %s (default device: %d)\n",
               i, api->name, api->defaultOutputDevice);
    }

    // Prefer WASAPI on Windows - natively supports float32 output.
    // WMME may reinterpret float32 bytes as int16 causing distortion.
    const HostPick host = pick_host_api();
    PaDeviceIndex dev_idx = Pa_GetDefaultOutputDevice();
    const char* api_name  = host.label;
    if (host.idx >= 0) {
        dev_idx = Pa_GetHostApiInfo(host.idx)->defaultOutputDevice;
        printf("[HIJACKER] Using %s output device\n", host.label);
    }

    // A specific device was asked for in the settings: find it by name.
    if (!impl_->device_pref.empty()) {
        PaDeviceIndex found = paNoDevice;
        const int n_dev = Pa_GetDeviceCount();
        for (int i = 0; i < n_dev; ++i) {
            const PaDeviceInfo* di = Pa_GetDeviceInfo(i);
            if (!di || di->maxOutputChannels < 2) continue;
            if (host.idx >= 0 && di->hostApi != host.idx) continue;
            if (impl_->device_pref == di->name) { found = i; break; }
        }
        if (found != paNoDevice) {
            dev_idx = found;
            printf("[HIJACKER] Output device chosen in settings: %s\n", impl_->device_pref.c_str());
        } else {
            printf("[HIJACKER] Output device '%s' not found - using the system default\n",
                   impl_->device_pref.c_str());
        }
    }

    const PaDeviceInfo* dev_info = (dev_idx != paNoDevice) ? Pa_GetDeviceInfo(dev_idx) : nullptr;
    if (!dev_info) {
        printf("[HIJACKER] no audio output device available\n");
        Pa_Terminate();
        return false;
    }

    PaStreamParameters out_params;
    out_params.device                    = dev_idx;
    out_params.channelCount              = 2;
    out_params.sampleFormat              = paFloat32;
    out_params.suggestedLatency          = dev_info->defaultHighOutputLatency;
    if (buffer_frames_ > 0)   // a buffer size was requested: ask the driver for (at least) that latency
        out_params.suggestedLatency = std::max(dev_info->defaultLowOutputLatency,
                                               (double)buffer_frames_ / (double)sample_rate);
    out_params.hostApiSpecificStreamInfo = nullptr;

    double device_sr = dev_info->defaultSampleRate;
    printf("[HIJACKER] Device default SR: %.0fHz, low latency: %.3fs, high: %.3fs\n",
           device_sr,
           dev_info->defaultLowOutputLatency,
           dev_info->defaultHighOutputLatency);

    if (Pa_IsFormatSupported(nullptr, &out_params, (double)sample_rate_)
        == paFormatIsSupported) {
        printf("[HIJACKER] float32 @ %dHz supported\n", sample_rate_);
    } else {
        sample_rate_ = (int)device_sr;
        printf("[HIJACKER] using device native rate %dHz\n", sample_rate_);
    }
    printf("[HIJACKER] opening: %s via %s @ %dHz\n",
           dev_info->name, api_name, sample_rate_);

    // Delay lines are allocated here (not on the audio thread) for any delay
    // effect that is already assigned.
    for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch)
        for (int sl = 0; sl < PB_MAX_EFFECTS; ++sl)
            if (g_state.effect_chain[ch][sl].type == EffectType::DELAY_PARAM)
                hj_ensure_delay_buffers(ch, (float)sample_rate_);

    // Start the disk thread before the stream so the first callback finds it ready.
    impl_->start_worker(sample_rate_);

    err = Pa_OpenStream(
        (PaStream**)&pa_stream_,
        nullptr,
        &out_params,
        sample_rate_,
        buffer_frames_ > 0 ? (unsigned long)buffer_frames_ : (unsigned long)paFramesPerBufferUnspecified,
        paClipOff,
        _pa_callback,
        this
    );

    if (err != paNoError) {
        printf("[HIJACKER] Pa_OpenStream failed: %s\n", Pa_GetErrorText(err));
        impl_->stop_worker();
        pa_stream_ = nullptr;
        Pa_Terminate();
        return false;
    }

    err = Pa_StartStream((PaStream*)pa_stream_);
    if (err != paNoError) {
        printf("[HIJACKER] Pa_StartStream failed: %s\n", Pa_GetErrorText(err));
        Pa_CloseStream((PaStream*)pa_stream_);
        pa_stream_ = nullptr;
        impl_->stop_worker();
        Pa_Terminate();
        return false;
    }

    const PaStreamInfo* si = Pa_GetStreamInfo((PaStream*)pa_stream_);
    if (si) impl_->out_latency_s.store(si->outputLatency);

    impl_->device_active = dev_info->name ? dev_info->name : "";
    initialized_ = true;
    if (buffer_frames_ > 0)
        printf("[HIJACKER] Audio engine started on '%s' - %dHz, %d-frame buffer requested, "
               "output latency %.1f ms\n",
               impl_->device_active.c_str(), sample_rate_, buffer_frames_, si ? si->outputLatency * 1000.0 : 0.0);
    else
        printf("[HIJACKER] Audio engine started on '%s' - %dHz, driver-chosen buffer size, "
               "output latency %.1f ms\n",
               impl_->device_active.c_str(), sample_rate_, si ? si->outputLatency * 1000.0 : 0.0);
    return true;
}

void HijackerEngine::shutdown() {
    impl_->offline_end();
    if (!initialized_) return;
    impl_->playing.store(false);
    if (pa_stream_) {
        Pa_StopStream((PaStream*)pa_stream_);     // waits for the callback to finish
        Pa_CloseStream((PaStream*)pa_stream_);
        pa_stream_ = nullptr;
    }
    Pa_Terminate();
    impl_->stop_worker();
    hj_release_all_delay_buffers();
    initialized_ = false;
    printf("[HIJACKER] Audio engine shut down\n");
}

bool HijackerEngine::offline_begin(double start_s, int sample_rate, unsigned channel_mask,
                                   bool respect_mute_solo, bool apply_master) {
    return impl_->offline_begin(start_s, sample_rate, (uint32_t)channel_mask,
                                respect_mute_solo, apply_master);
}
int  HijackerEngine::offline_render(float* out_interleaved, int n_frames) {
    return impl_->offline_render(out_interleaved, n_frames);
}
void HijackerEngine::offline_end()           { impl_->offline_end(); }
bool HijackerEngine::offline_active() const  { return impl_->job != nullptr; }

bool HijackerEngine::is_running() const {
    return initialized_ && pa_stream_ &&
           Pa_IsStreamActive((PaStream*)pa_stream_) == 1;
}

// =============================================================================
// Transport (Python thread) — these only post commands; the audio thread
// executes them at the start of its next callback.
// =============================================================================

namespace {
int64_t steady_ns()
{
    return (int64_t)std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
}

// Frames to render ahead of the position Python asks for.
static int64_t hj_comp_frames(double comp_s, double out_latency_s, double hold_est_s,
                              bool include_preroll, int sr)
{
    double c = (comp_s < 0.0) ? out_latency_s + (include_preroll ? hold_est_s : 0.0)
                              : comp_s;
    if (!(c > 0.0)) return 0;
    if (c > 0.25) c = 0.25;                     // sanity cap: never skip more than 250 ms
    return (int64_t)std::llround(c * (double)sr);
}

void HijackerEngine::set_latency_compensation(double seconds)
{
    impl_->comp_s.store(std::isfinite(seconds) ? seconds : -1.0);
}

double HijackerEngine::get_latency_compensation_s() const
{
    double c = impl_->comp_s.load();
    double L = impl_->out_latency_s.load();
    return (double)hj_comp_frames(c, L, impl_->hold_est_s.load(), true, sample_rate_) / (double)sample_rate_;
}

void HijackerEngine::play(double timeline_pos_s) {
    if (impl_->offline_active.load()) {
        printf("[HIJACKER] play ignored: an offline render is running\n");
        return;
    }
    if (!(timeline_pos_s > 0.0)) timeline_pos_s = 0.0;
    int64_t bias  = hj_comp_frames(impl_->comp_s.load(), impl_->out_latency_s.load(),
                                   impl_->hold_est_s.load(), true, sample_rate_);
    int64_t frame = (int64_t)std::llround(timeline_pos_s * (double)sample_rate_) + bias;
    impl_->stop_req.store(false);
    impl_->bias_frames.store(bias, std::memory_order_relaxed);
    impl_->play_t0_ns.store(steady_ns(), std::memory_order_relaxed);
    impl_->playhead_s.store(timeline_pos_s);
    impl_->pending_seek.store(frame, std::memory_order_release);
    impl_->play_counter.fetch_add(1, std::memory_order_acq_rel);
    impl_->playing.store(true, std::memory_order_release);
    printf("[HIJACKER] play from %.3fs (latency compensation %.1f ms)\n",
           timeline_pos_s, 1000.0 * (double)bias / (double)sample_rate_);
}

void HijackerEngine::stop() {
    impl_->stop_req.store(true, std::memory_order_release);
    printf("[HIJACKER] stopped at %.3fs\n", impl_->playhead_s.load());
}

void HijackerEngine::seek(double timeline_pos_s) {
    if (!(timeline_pos_s > 0.0)) timeline_pos_s = 0.0;
    int64_t bias  = hj_comp_frames(impl_->comp_s.load(), impl_->out_latency_s.load(),
                                   0.0, false, sample_rate_);     // no pre-roll on a seek
    int64_t frame = (int64_t)std::llround(timeline_pos_s * (double)sample_rate_) + bias;
    impl_->bias_frames.store(bias, std::memory_order_relaxed);
    impl_->playhead_s.store(timeline_pos_s);
    impl_->pending_seek.store(frame, std::memory_order_release);
    printf("[HIJACKER] seek to %.3fs\n", timeline_pos_s);
}

// =============================================================================
// Channel playlist management (Python thread)
// =============================================================================
void HijackerEngine::set_channel_playlist(int ch,
    const std::vector<HijackerSegment>& segs)
{
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return;

    auto pl = std::make_shared<Playlist>();
    int n = std::min((int)segs.size(), HJ_MAX_SEGMENTS);
    if ((int)segs.size() > HJ_MAX_SEGMENTS)
        printf("[HIJACKER] ch%d: %d strips exceeds the per-channel limit of %d - extra strips ignored\n",
               ch + 1, (int)segs.size(), HJ_MAX_SEGMENTS);
    pl->segs.reserve((size_t)n);
    for (int i = 0; i < n; ++i) {
        if (segs[i].duration_s <= 0.0 || segs[i].filepath[0] == 0) continue;
        PlaylistSeg ps; ps.seg = segs[i];
        ps.seg.filepath[sizeof(ps.seg.filepath) - 1] = 0;
        if (!std::isfinite(ps.seg.volume)) ps.seg.volume = 1.0f;
        if (!std::isfinite(ps.seg.volume_end) || ps.seg.volume_end < 0.0f) ps.seg.volume_end = -1.0f;   // constant gain
        pl->segs.push_back(ps);
    }
    std::stable_sort(pl->segs.begin(), pl->segs.end(),
        [](const PlaylistSeg& a, const PlaylistSeg& b) {
            return a.seg.timeline_pos_s < b.seg.timeline_pos_s; });
    // Segments that continue straight on from the previous one (same file, contiguous
    // in both timeline and file, and the gain carries on from where the last one ended)
    // are not faded at the join.
    for (size_t i = 1; i < pl->segs.size(); ++i) {
        PlaylistSeg& a = pl->segs[i - 1];
        PlaylistSeg& b = pl->segs[i];
        if (strcmp(a.seg.filepath, b.seg.filepath) == 0 &&
            std::fabs(a.seg.timeline_pos_s + a.seg.duration_s - b.seg.timeline_pos_s) < 1e-4 &&
            std::fabs(a.seg.file_offset_s  + a.seg.duration_s - b.seg.file_offset_s)  < 1e-4 &&
            std::fabs((a.seg.volume_end >= 0.0f ? a.seg.volume_end : a.seg.volume) - b.seg.volume) < 1e-6f) {
            a.fade_out = false;
            b.fade_in  = false;
        }
    }
    const int count = (int)pl->segs.size();

    Stream& s = impl_->streams[ch];
    {
        std::lock_guard<std::mutex> lk(s.pl_mtx);
        s.pl_ptr = pl;
    }
    s.has_playlist.store(count > 0, std::memory_order_release);
    uint32_t gen = s.pl_gen.fetch_add(1, std::memory_order_acq_rel) + 1;
    impl_->wake();
    impl_->wait_released(s, gen);      // old files are closed when this returns
    printf("[HIJACKER] ch%d playlist: %d segments\n", ch + 1, count);
}

void HijackerEngine::clear_channel(int ch) {
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return;
    Stream& s = impl_->streams[ch];
    {
        std::lock_guard<std::mutex> lk(s.pl_mtx);
        s.pl_ptr.reset();
    }
    s.has_playlist.store(false, std::memory_order_release);
    uint32_t gen = s.pl_gen.fetch_add(1, std::memory_order_acq_rel) + 1;
    impl_->wake();
    impl_->wait_released(s, gen);      // old file is closed when this returns
    // Reset meters so deleted strips don't leave stale readings that
    // keep the VU bar and rack waveforms animating after removal.
    s.meters_dirty.store(true, std::memory_order_release);    // zeroed by the audio thread
}

void HijackerEngine::clear_all_channels() {
    // Publish every clear first, wake the worker once, then wait once: the
    // worker releases all the files in a single pass.
    uint32_t gens[PB_MAX_CHANNELS];
    for (int i = 0; i < PB_MAX_CHANNELS; ++i) {
        Stream& s = impl_->streams[i];
        {
            std::lock_guard<std::mutex> lk(s.pl_mtx);
            s.pl_ptr.reset();
        }
        s.has_playlist.store(false, std::memory_order_release);
        gens[i] = s.pl_gen.fetch_add(1, std::memory_order_acq_rel) + 1;
    }
    impl_->wake();
    for (int i = 0; i < PB_MAX_CHANNELS; ++i) {
        impl_->wait_released(impl_->streams[i], gens[i]);
        impl_->streams[i].meters_dirty.store(true, std::memory_order_release);
    }
}

// =============================================================================
// Mixer params
// =============================================================================
void HijackerEngine::set_volume(int ch, float vol) {
    if (ch < 0 || ch >= PB_MAX_CHANNELS || !std::isfinite(vol)) return;
    impl_->streams[ch].volume.store(vol);
}

void HijackerEngine::set_mute(int ch, bool muted) {
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return;
    impl_->streams[ch].muted.store(muted);
    // Muted channels keep being rendered, so unmuting is always in sync.
}

void HijackerEngine::set_solo(int ch, bool soloed) {
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return;
    impl_->streams[ch].soloed.store(soloed);
}

void HijackerEngine::set_pan(int ch, float pan) {
    if (ch < 0 || ch >= PB_MAX_CHANNELS || !std::isfinite(pan)) return;
    impl_->streams[ch].pan.store(std::max(0.0f, std::min(1.0f, pan)));
}

// =============================================================================
// Effect slots — written under a per-slot seqlock so the audio thread always
// sees a consistent type/enabled/params set.
// =============================================================================
void HijackerEngine::set_effect_slot(int ch, int slot, int type,
                                      const std::vector<float>& params) {
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return;
    if (slot < 0 || slot >= PB_MAX_EFFECTS) return;

    // Allocate the delay line here, on the calling thread, never in the callback.
    if ((EffectType)type == EffectType::DELAY_PARAM)
        hj_ensure_delay_buffers(ch, (float)sample_rate_);

    std::atomic<uint32_t>& seq = g_state.effect_seq[ch][slot];
    seq.fetch_add(1, std::memory_order_acq_rel);                 // odd: write in progress
    EffectSlot& fx = g_state.effect_chain[ch][slot];
    fx.type    = (EffectType)type;
    fx.enabled = (type != 0);
    int n = std::min((int)params.size(), 24);
    for (int i = 0; i < n; ++i) fx.params[i] = params[i];
    seq.fetch_add(1, std::memory_order_release);                 // even: stable
}

void HijackerEngine::clear_effect_slot(int ch, int slot) {
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return;
    if (slot < 0 || slot >= PB_MAX_EFFECTS) return;
    std::atomic<uint32_t>& seq = g_state.effect_seq[ch][slot];
    seq.fetch_add(1, std::memory_order_acq_rel);
    g_state.effect_chain[ch][slot].type    = EffectType::NONE;
    g_state.effect_chain[ch][slot].enabled = false;
    seq.fetch_add(1, std::memory_order_release);
}

// =============================================================================
// Metering / diagnostics
// =============================================================================
float  HijackerEngine::get_meter_rms(int ch)  const {
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return 0.0f;
    return impl_->streams[ch].meter_rms.load();
}
float  HijackerEngine::get_meter_peak(int ch) const {
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return 0.0f;
    // Highest peak since the last call (peak-hold friendly: nothing between two polls is lost).
    return impl_->streams[ch].peak_acc.exchange(0.0f, std::memory_order_relaxed);
}
double HijackerEngine::get_playhead_s() const {
    return impl_->playhead_s.load();
}
int    HijackerEngine::get_sample_rate() const { return sample_rate_; }
void  HijackerEngine::set_master_gain(float g) {
    if (!(g == g)) return;
    impl_->master_target.store(std::min(2.0f, std::max(0.0f, g)), std::memory_order_relaxed);
}
float HijackerEngine::get_master_gain() const { return impl_->master_target.load(std::memory_order_relaxed); }
float HijackerEngine::get_master_peak() const { return impl_->master_peak.exchange(0.0f, std::memory_order_relaxed); }
bool  HijackerEngine::get_master_clip() const { return impl_->master_clip.load(std::memory_order_relaxed); }
void  HijackerEngine::clear_master_clip()     { impl_->master_clip.store(false, std::memory_order_relaxed); }

HijackerLoudness HijackerEngine::get_loudness() const {
    HijackerLoudness o; const auto& p = impl_->loud_pub; const auto rl = std::memory_order_relaxed;
    o.momentary = p[0].load(rl); o.short_term = p[1].load(rl); o.integrated = p[2].load(rl); o.lra = p[3].load(rl);
    o.momentary_max = p[4].load(rl); o.short_term_max = p[5].load(rl);
    o.true_peak_db = p[6].load(rl); o.sample_peak_db = p[7].load(rl); o.duration_s = p[8].load(rl);
    return o;
}
void HijackerEngine::reset_loudness() {
    // The audio thread restarts the measurement at its next buffer. The published numbers are
    // cleared here too so a stopped (or silent) engine does not keep showing the old ones.
    impl_->loud_reset.store(true, std::memory_order_release);
    for (int i = 0; i < Impl::LOUD_FIELDS; ++i)
        impl_->loud_pub[i].store(i == 3 || i == 8 ? 0.0 : -200.0, std::memory_order_relaxed);
}
HijackerLoudness HijackerEngine::offline_loudness() const {
    return impl_->job ? to_public(impl_->job->meter.result()) : impl_->off_loud;
}

double HijackerEngine::get_output_latency_s() const { return impl_->out_latency_s.load(); }
int    HijackerEngine::get_xrun_count() const { return impl_->xruns.load(); }
void   HijackerEngine::note_device_xrun() { impl_->xruns.fetch_add(1, std::memory_order_relaxed); }

// =============================================================================
// Audio callback — THE HOT PATH
// No allocation, no locks, no file I/O, no printf.
// =============================================================================
int HijackerEngine::audio_callback(const void* /*input*/, void* output,
                                    unsigned long frames_per_buffer)
{
    Impl& I = *impl_;
    float* out = static_cast<float*>(output);
    const unsigned long n_out = frames_per_buffer * 2;
    memset(out, 0, n_out * sizeof(float));

    // While an offline render owns the DSP state the live stream stays silent.
    struct BusyGuard {
        std::atomic<int>& b;
        explicit BusyGuard(std::atomic<int>& x) : b(x) { b.store(1, std::memory_order_seq_cst); }
        ~BusyGuard() { b.store(0, std::memory_order_release); }
    } busy_guard(I.cb_busy);
    if (I.offline_active.load(std::memory_order_seq_cst)) return paContinue;

#ifdef HJ_HAS_SSE
    // Flush denormals to zero: reverb/filter tails decaying through the
    // denormal range can otherwise cost 100x CPU per operation.
    _mm_setcsr(_mm_getcsr() | 0x8040);
#endif

    // ---- transport commands ------------------------------------------------
    int64_t sk = I.pending_seek.exchange(NO_SEEK, std::memory_order_acq_rel);
    uint32_t pc = I.play_counter.load(std::memory_order_acquire);
    if (pc != I.seen_play) {
        I.seen_play = pc;
        I.hold = true;                  // wait for the rings before starting
        I.hold_waited = 0;
        I.fadeout_pos = 0;
    }
    if (sk != NO_SEEK) I.do_seek(sk);

    // keep rings primed / re-primed when playlists change (also while stopped)
    I.sync_streams();

    if (!I.playing.load(std::memory_order_acquire)) return paContinue;

    // ---- start-up pre-roll: don't start until every channel has audio ready --
    if (I.hold) {
        bool ready = true;
        int need = (int)std::min<unsigned long>(frames_per_buffer + 64, (unsigned long)RING_FRAMES / 2);
        for (int ch = 0; ch < PB_MAX_CHANNELS && ready; ++ch) {
            Stream& s = I.streams[ch];
            if (s.has_playlist.load(std::memory_order_acquire) && !I.stream_ready(s, need))
                ready = false;
        }
        if (ready || I.hold_waited >= I.sr / 2) {
            I.hold = false;
            // Remember how long play() -> first audio took, so the next play()
            // can start that much further ahead and land in sync with the cursor.
            double waited = (double)(steady_ns() - I.play_t0_ns.load(std::memory_order_relaxed)) * 1e-9;
            if (waited < 0.0) waited = 0.0;
            if (waited > 0.25) waited = 0.25;
            double prev = I.hold_est_s.load(std::memory_order_relaxed);
            I.hold_est_s.store(prev <= 0.0 ? waited : 0.5 * prev + 0.5 * waited, std::memory_order_relaxed);
        } else {
            I.hold_waited += (int64_t)frames_per_buffer;
            return paContinue;          // silence; the playhead does not move
        }
    }

    // ---- solo state ----------------------------------------------------------
    I.any_solo = false;
    for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch)
        if (I.streams[ch].has_playlist.load(std::memory_order_relaxed) &&
            I.streams[ch].soloed.load(std::memory_order_relaxed)) { I.any_solo = true; break; }

    // ---- mix in chunks the DSP is sized for ----------------------------------
    unsigned long done = 0;
    while (done < frames_per_buffer) {
        int n = (int)std::min<unsigned long>(MAX_BLOCK, frames_per_buffer - done);
        I.mix_chunk(out + done * 2, n);
        done += (unsigned long)n;
    }

    // ---- master fader (smoothed over ~10 ms so dragging it never zippers) -----
    {
        const float tgt = I.master_target.load(std::memory_order_relaxed);
        float cur = I.master_cur;
        if (cur != tgt) {
            const float k = 1.0f - expf(-1.0f / (0.010f * (float)I.sr));
            for (unsigned long i = 0; i < frames_per_buffer; ++i) {
                cur += (tgt - cur) * k;
                if (fabsf(tgt - cur) < kSnap) cur = tgt;
                out[2 * i] *= cur; out[2 * i + 1] *= cur;
            }
            I.master_cur = cur;
        } else if (cur != 1.0f) {
            for (unsigned long i = 0; i < n_out; ++i) out[i] *= cur;
        }
    }

    // ---- stop request: short fade-out so stopping never clicks ----------------
    if (I.stop_req.load(std::memory_order_acquire)) {
        for (unsigned long i = 0; i < frames_per_buffer; ++i) {
            float g = 1.0f - (float)I.fadeout_pos / (float)STOP_FADE_FRAMES;
            if (g < 0.0f) g = 0.0f;
            out[2 * i] *= g; out[2 * i + 1] *= g;
            if (I.fadeout_pos < STOP_FADE_FRAMES) ++I.fadeout_pos;
        }
        if (I.fadeout_pos >= STOP_FADE_FRAMES) {
            I.playing.store(false, std::memory_order_release);
            I.stop_req.store(false, std::memory_order_release);
            I.fadeout_pos = 0;
        }
    }

    // ---- master soft limiter + sanitiser (shared with the offline renderer) -----
    const float master_pk = hj_limit_block(out, n_out, true);

    // ---- master meter: peak since last read (pre-limiter, post-fader) + clip latch
    if (master_pk > 0.0f) {
        float prev = I.master_peak.load(std::memory_order_relaxed);
        while (master_pk > prev && !I.master_peak.compare_exchange_weak(prev, master_pk, std::memory_order_relaxed)) {}
        if (master_pk >= 1.0f) I.master_clip.store(true, std::memory_order_relaxed);
    }

    // ---- loudness (BS.1770): measures exactly what leaves the engine --------------
    if (I.loud_ok.load(std::memory_order_acquire)) {
        if (I.loud_reset.exchange(false, std::memory_order_acq_rel)) I.loud.reset();
        I.loud.process(out, (int)frames_per_buffer);
        if (I.loud.take_fresh()) {
            const hjl::Result r = I.loud.result();
            I.loud_pub[0].store(r.momentary, std::memory_order_relaxed);
            I.loud_pub[1].store(r.short_term, std::memory_order_relaxed);
            I.loud_pub[2].store(r.integrated, std::memory_order_relaxed);
            I.loud_pub[3].store(r.lra, std::memory_order_relaxed);
            I.loud_pub[4].store(r.momentary_max, std::memory_order_relaxed);
            I.loud_pub[5].store(r.short_term_max, std::memory_order_relaxed);
            I.loud_pub[6].store(r.true_peak_db, std::memory_order_relaxed);
            I.loud_pub[7].store(r.sample_peak_db, std::memory_order_relaxed);
            I.loud_pub[8].store(r.duration_s, std::memory_order_relaxed);
        }
    }

    // ---- publish the playhead (unless a seek is waiting to be applied) ---------
    if (I.pending_seek.load(std::memory_order_acquire) == NO_SEEK)
        I.playhead_s.store(std::max(0.0, (double)(I.play_frame - I.bias_frames.load(std::memory_order_relaxed)) / (double)I.sr),
                           std::memory_order_release);

    return paContinue;
}
