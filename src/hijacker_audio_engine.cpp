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
#include <portaudio.h>
#include <cstring>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <algorithm>
#include <vector>
#include <thread>
#include <condition_variable>
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

    // engine rate (set before the worker / stream start)
    int   sr = 44100;
    float k_fader = 0.0f, k_mute = 0.0f, k_pan = 0.0f;

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

    // audio-thread-only state
    int64_t  play_frame   = 0;
    uint32_t seen_play    = 0;
    bool     hold         = false;
    int64_t  hold_waited  = 0;
    int      fadeout_pos  = 0;
    bool     any_solo     = false;
    SrcKernel kernels[4];                 // disk thread only
    int       n_kernels = 0;
    alignas(16) float chbuf[MAX_BLOCK * 2];
    alignas(16) float mixbuf[MAX_BLOCK * 2];

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
    void adopt_playlist(Stream& s);
    void render_block(Stream& s, int64_t frame0, int n, float* out);
    void render_piece(Stream& s, const SegFrames& sf, int64_t i0, int64_t i1,
                      int64_t frame0, float* out);

    // consumer
    void issue_request(Stream& s, int64_t frame);
    void sync_streams();
    void do_seek(int64_t frame);
    bool stream_ready(Stream& s, int need);
    void mix_chunk(float* out, int n);
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
    const float fs = (float)rate;
    k_fader = 1.0f - std::exp(-1.0f / (0.004f * fs));
    k_mute  = 1.0f - std::exp(-1.0f / (0.004f * fs));
    k_pan   = 1.0f - std::exp(-1.0f / (0.010f * fs));

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
void HijackerEngine::Impl::adopt_playlist(Stream& s)
{
    {
        std::lock_guard<std::mutex> lk(s.pl_mtx);
        s.p_pl = s.pl_ptr;
    }
    s.p_segs.clear();
    if (!s.p_pl) return;
    const double fs = (double)sr;
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
    adopt_playlist(s);
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
                                        int64_t frame0, float* out)
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

    if (fsr == sr) {
        const int64_t src0 = (int64_t)std::llround(seg.file_offset_s * (double)fsr) + (i0 - sf.s0);
        const int cnt = (int)(i1 - i0);
        s.reader.read_frames(src0, cnt, tmp.data());
        for (int k = 0; k < cnt; ++k) {
            int64_t rel = i0 - sf.s0 + k;
            float g = seg.volume * edge_gain(rel, len, sf.ps->fade_in, sf.ps->fade_out, sf.fi, sf.fo);
            float* o = out + 2 * (i0 - frame0 + k);
            o[0] += tmp[2 * k]     * g;
            o[1] += tmp[2 * k + 1] * g;
        }
        return;
    }

    // Sample-rate conversion (windowed sinc). The phase is derived from the
    // absolute timeline position, so it is continuous across blocks (no drift,
    // no clicks at block joins).
    const double ratio = (double)fsr / (double)sr;
    if (ratio > 16.0 || ratio < 1.0 / 16.0) return;
    SrcKernel* kern = nullptr;
    for (int i = 0; i < n_kernels; ++i)
        if (kernels[i].fsr == fsr && kernels[i].esr == sr) { kern = &kernels[i]; break; }
    if (!kern) {
        int idx = (n_kernels < 4) ? n_kernels++ : 0;
        kernels[idx].build(fsr, sr);
        kern = &kernels[idx];
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
            float g = seg.volume * edge_gain(rel, len, sf.ps->fade_in, sf.ps->fade_out, sf.fi, sf.fo);
            float* d = out + 2 * (a - frame0 + k);
            d[0] += accl * g;
            d[1] += accr * g;
        }
    }
}

void HijackerEngine::Impl::render_block(Stream& s, int64_t frame0, int n, float* out)
{
    memset(out, 0, sizeof(float) * 2 * (size_t)n);
    const int64_t frame1 = frame0 + n;
    for (const SegFrames& sf : s.p_segs) {
        if (sf.s1 <= frame0 || sf.s0 >= frame1) continue;
        render_piece(s, sf, std::max(frame0, sf.s0), std::min(frame1, sf.s1), frame0, out);
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

        render_block(s, s.p_end, PROD_BLOCK, blk);

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

        // ---- mute / solo / fader targets ----------------------------------
        const bool  silent_t = s.muted.load(std::memory_order_relaxed) ||
                               (any_solo && !s.soloed.load(std::memory_order_relaxed));
        const float vol_t    = s.volume.load(std::memory_order_relaxed);
        const float mute_t   = silent_t ? 0.0f : 1.0f;
        if (s.fader_g < 0.0f) s.fader_g = vol_t;      // first use: no ramp from zero
        if (s.mute_g  < 0.0f) s.mute_g  = mute_t;

        if ((mute_t == 0.0f && s.mute_g <= 1e-4f) || (vol_t < 1e-4f && s.fader_g < 1e-4f)) {
            // Fully silent: skip DSP and mixing (as before), keep meters at zero.
            if (mute_t == 0.0f) s.mute_g = 0.0f;
            if (vol_t < 1e-4f)  s.fader_g = vol_t;
            s.meter_rms.store(0.0f);
            s.meter_peak.store(0.0f);
            g_state.meter_levels[ch] = 0.0f;
            continue;
        }

        // ---- fader (pre-DSP, so the compressor sees the right level) -------
        {
            float g = s.fader_g;
            if (g == vol_t) {
                if (g != 1.0f)
                    for (int i = 0; i < n * 2; ++i) cb[i] *= g;
            } else {
                for (int i = 0; i < n; ++i) {
                    g += (vol_t - g) * k_fader;
                    if (std::fabs(vol_t - g) < 1e-5f) g = vol_t;
                    cb[2 * i] *= g; cb[2 * i + 1] *= g;
                }
                s.fader_g = g;
            }
        }

        // ---- DSP effect chain ---------------------------------------------
        {
            if (hj_channel_has_fx(ch)) apply_effect_chain_batch(ch, cb, n, 2, (float)sr);
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
            s.meter_rms.store(rms);
            s.meter_peak.store(peak);
            {   // keep the loudest peak until Python reads it, so a poll never misses a short peak
                float prev = s.peak_acc.load(std::memory_order_relaxed);
                while (peak > prev && !s.peak_acc.compare_exchange_weak(prev, peak, std::memory_order_relaxed)) {}
            }
            g_state.meter_levels[ch] = rms;
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
                    mg += (mute_t - mg) * k_mute;
                    if (std::fabs(mute_t - mg) < 1e-5f) mg = mute_t;
                }
                if (pc != pan_t) {
                    pc += (pan_t - pc) * k_pan;
                    if (std::fabs(pan_t - pc) < 1e-5f) pc = pan_t;
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

HijackerEngine::HijackerEngine() : impl_(new Impl) {}

HijackerEngine::~HijackerEngine() {
    shutdown();
}

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
    PaHostApiIndex wmme_idx   = -1;
    PaHostApiIndex wasapi_idx = -1;
    for (int i = 0; i < n_apis; ++i) {
        const PaHostApiInfo* api = Pa_GetHostApiInfo(i);
        printf("[HIJACKER]   [%d] %s (default device: %d)\n",
               i, api->name, api->defaultOutputDevice);
        if (api->type == paMME)    wmme_idx   = i;
        if (api->type == paWASAPI) wasapi_idx = i;
    }

    // Prefer WASAPI on Windows — natively supports float32 output.
    // WMME may reinterpret float32 bytes as int16 causing distortion.
    PaDeviceIndex dev_idx = Pa_GetDefaultOutputDevice();
    const char* api_name  = "default";
    if (wasapi_idx >= 0) {
        const PaHostApiInfo* wasapi = Pa_GetHostApiInfo(wasapi_idx);
        if (wasapi->defaultOutputDevice != paNoDevice) {
            dev_idx  = wasapi->defaultOutputDevice;
            api_name = "WASAPI";
            printf("[HIJACKER] Using WASAPI output device\n");
        }
    } else if (wmme_idx >= 0) {
        const PaHostApiInfo* wmme = Pa_GetHostApiInfo(wmme_idx);
        if (wmme->defaultOutputDevice != paNoDevice) {
            dev_idx  = wmme->defaultOutputDevice;
            api_name = "WMME";
            printf("[HIJACKER] Using WMME output device\n");
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
        paFramesPerBufferUnspecified,  // let PortAudio choose buffer size
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

    initialized_ = true;
    printf("[HIJACKER] Audio engine started — %dHz, device-chosen buffer size, "
           "output latency %.1f ms\n",
           sample_rate_, si ? si->outputLatency * 1000.0 : 0.0);
    return true;
}

void HijackerEngine::shutdown() {
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
        pl->segs.push_back(ps);
    }
    std::stable_sort(pl->segs.begin(), pl->segs.end(),
        [](const PlaylistSeg& a, const PlaylistSeg& b) {
            return a.seg.timeline_pos_s < b.seg.timeline_pos_s; });
    // Segments that continue straight on from the previous one (same file, same
    // gain, contiguous in both timeline and file) are not faded at the join.
    for (size_t i = 1; i < pl->segs.size(); ++i) {
        PlaylistSeg& a = pl->segs[i - 1];
        PlaylistSeg& b = pl->segs[i];
        if (strcmp(a.seg.filepath, b.seg.filepath) == 0 &&
            std::fabs(a.seg.timeline_pos_s + a.seg.duration_s - b.seg.timeline_pos_s) < 1e-4 &&
            std::fabs(a.seg.file_offset_s  + a.seg.duration_s - b.seg.file_offset_s)  < 1e-4 &&
            std::fabs(a.seg.volume - b.seg.volume) < 1e-6f) {
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

    // ---- master soft limiter + sanitiser ------------------------------------
    // Transparent below 0.95. Above it the level is bent smoothly towards 1.0
    // and never reaches it:  y = 0.95 + o / (1 + o/0.05),  o = |x| - 0.95
    // (slope 1 at the knee, asymptote 1.0). The previous formula,
    // 0.95 + o/(1+o), approached 1.95 and so did not limit anything above 1.0.
    for (unsigned long i = 0; i < n_out; ++i) {
        float s = out[i];
        if (!(s == s) || s > 1e6f || s < -1e6f) { out[i] = 0.0f; continue; }   // NaN / runaway
        if (s > 0.95f) {
            float over = s - 0.95f;
            out[i] = 0.95f + over / (1.0f + over * 20.0f);
        } else if (s < -0.95f) {
            float over = -s - 0.95f;
            out[i] = -(0.95f + over / (1.0f + over * 20.0f));
        }
    }

    // ---- publish the playhead (unless a seek is waiting to be applied) ---------
    if (I.pending_seek.load(std::memory_order_acquire) == NO_SEEK)
        I.playhead_s.store(std::max(0.0, (double)(I.play_frame - I.bias_frames.load(std::memory_order_relaxed)) / (double)I.sr),
                           std::memory_order_release);

    return paContinue;
}
