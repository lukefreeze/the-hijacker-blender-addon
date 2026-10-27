// hijacker_loudness.h
// ITU-R BS.1770-4 / EBU R128 loudness measurement for a stereo signal:
//   momentary (400 ms), short-term (3 s), integrated (gated), loudness range (LRA),
//   plus sample peak and true peak (8x oversampled).
//
// Header-only and real-time safe: init() allocates, process() / reset() never do.
// Used by the audio callback (live master meter) and by the offline renderer (Mixdown).
//
// Level values are in LUFS / dBTP / dBFS. "No reading yet" is reported as HJL_NO_READING.
#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

namespace hjl {

constexpr double NO_READING = -200.0;

struct Result {
    double momentary      = NO_READING;   // LUFS, last 400 ms
    double short_term     = NO_READING;   // LUFS, last 3 s (needs 3 s of signal)
    double integrated     = NO_READING;   // LUFS, gated, since reset
    double lra            = 0.0;          // LU, loudness range since reset
    double momentary_max  = NO_READING;
    double short_term_max = NO_READING;
    double true_peak_db   = NO_READING;   // dBTP since reset
    double sample_peak_db = NO_READING;   // dBFS since reset
    double duration_s     = 0.0;          // signal measured since reset
};

class LoudnessMeter {
public:
    // Allocates. Call from a non-real-time thread.
    void init(int rate) {
        fs_ = (double)rate;
        sub_len_ = std::max(1, (int)std::llround(0.1 * fs_));
        design_k_weighting();
        design_oversampler();
        hist_blk_.assign(NBINS, 0u);
        hist_st_.assign(NBINS, 0u);
        bin_energy_.resize(NBINS);
        for (int i = 0; i < NBINS; ++i) bin_energy_[i] = std::pow(10.0, (bin_centre(i) + 0.691) / 10.0);
        reset();
        ready_ = true;
    }
    bool ready() const { return ready_; }

    // Forget everything measured so far (filters, windows, histograms, peaks).
    void reset() {
        for (int c = 0; c < 2; ++c) for (int k = 0; k < 2; ++k) z_[c][k][0] = z_[c][k][1] = 0.0;
        acc_ = 0.0; acc_n_ = 0;
        for (int i = 0; i < ST_SUBS; ++i) sub_[i] = 0.0;
        sub_pos_ = 0; n_sub_ = 0;
        std::fill(hist_blk_.begin(), hist_blk_.end(), 0u);
        std::fill(hist_st_.begin(), hist_st_.end(), 0u);
        n_blk_ = 0; n_st_ = 0;
        mom_ = st_ = NO_READING; mom_max_ = st_max_ = NO_READING;
        peak_ = tpeak_ = 0.0f;
        for (int c = 0; c < 2; ++c) for (int i = 0; i < 2 * TP_TAPS; ++i) tp_hist_[c][i] = 0.0f;
        tp_pos_ = 0; frames_ = 0; fresh_ = false;
    }

    // x = interleaved stereo. Real-time safe.
    void process(const float* x, int frames) {
        if (!ready_) return;
        for (int n = 0; n < frames; ++n) {
            double e = 0.0;
            for (int c = 0; c < 2; ++c) {
                const float in = x[2 * n + c];
                const float a = in < 0 ? -in : in;
                if (a > peak_) peak_ = a;
                double v = biquad(sh_, z_[c][0], (double)in);
                v = biquad(hp_, z_[c][1], v);
                e += v * v;
            }
            acc_ += e;
            if (++acc_n_ >= sub_len_) { finish_subblock(); acc_ = 0.0; acc_n_ = 0; }
            if (tp_phases_ > 1) true_peak_step(x[2 * n], x[2 * n + 1]);
        }
        frames_ += frames;
    }

    // True once since the last call if a new 100 ms measurement was completed.
    bool take_fresh() { const bool f = fresh_; fresh_ = false; return f; }

    Result result() const {
        Result r;
        r.momentary = mom_; r.short_term = st_;
        r.momentary_max = mom_max_; r.short_term_max = st_max_;
        r.integrated = integrated();
        r.lra = loudness_range();
        r.sample_peak_db = to_db(peak_);
        const float tp = std::max(peak_, tpeak_);
        r.true_peak_db = to_db(tp);
        r.duration_s = fs_ > 0 ? (double)frames_ / fs_ : 0.0;
        return r;
    }

private:
    static constexpr int ST_SUBS = 30;                   // 3 s of 100 ms sub-blocks
    static constexpr int MO_SUBS = 4;                    // 400 ms
    static constexpr int NBINS   = 8000;                 // 0.01 LU bins from -70 LUFS to +10 LUFS
    static constexpr double BIN_LO = -70.0, BIN_W = 0.01;
    static constexpr int TP_TAPS = 16;                   // taps per interpolated phase

    struct Biquad { double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0; };

    static double biquad(const Biquad& f, double* z, double x) {     // transposed direct form II
        const double y = f.b0 * x + z[0];
        z[0] = f.b1 * x - f.a1 * y + z[1];
        z[1] = f.b2 * x - f.a2 * y;
        return y;
    }
    static double bin_centre(int i) { return BIN_LO + (i + 0.5) * BIN_W; }
    static double lufs_of(double energy) { return energy > 0.0 ? -0.691 + 10.0 * std::log10(energy) : NO_READING; }
    static double to_db(double a) { return a > 1e-10 ? 20.0 * std::log10(a) : NO_READING; }

    // BS.1770 K-weighting: a high-shelf pre-filter followed by the RLB high-pass.
    // Computed for any sample rate from the analogue prototype (matches the published
    // 48 kHz coefficients).
    void design_k_weighting() {
        const double PI = 3.14159265358979323846;
        {
            const double f0 = 1681.974450955533, G = 3.999843853973347, Q = 0.7071752369554196;
            const double K = std::tan(PI * f0 / fs_), Vh = std::pow(10.0, G / 20.0), Vb = std::pow(Vh, 0.4996667741545416);
            const double a0 = 1.0 + K / Q + K * K;
            sh_.b0 = (Vh + Vb * K / Q + K * K) / a0;
            sh_.b1 = 2.0 * (K * K - Vh) / a0;
            sh_.b2 = (Vh - Vb * K / Q + K * K) / a0;
            sh_.a1 = 2.0 * (K * K - 1.0) / a0;
            sh_.a2 = (1.0 - K / Q + K * K) / a0;
        }
        {
            const double f0 = 38.13547087602444, Q = 0.5003270373238773;
            const double K = std::tan(PI * f0 / fs_);
            const double a0 = 1.0 + K / Q + K * K;
            hp_.b0 = 1.0; hp_.b1 = -2.0; hp_.b2 = 1.0;
            hp_.a1 = 2.0 * (K * K - 1.0) / a0;
            hp_.a2 = (1.0 - K / Q + K * K) / a0;
        }
    }

    // True peak: windowed-sinc interpolation to 8x (below 96 kHz), 4x (below 192 kHz) or 2x
    // above. BS.1770-4 asks for at least 4x below 96 kHz; 8x brings the worst-case error of the
    // estimate down from about 0.3 dB to under 0.1 dB.
    void design_oversampler() {
        tp_phases_ = fs_ < 96000.0 ? 8 : (fs_ < 192000.0 ? 4 : 2);
        const double PI = 3.14159265358979323846, beta = 9.0;
        auto i0 = [](double x) { double s = 1, t = 1, q = x * x * 0.25;
                                 for (int k = 1; k < 50; ++k) { t *= q / ((double)k * k); s += t; if (t < 1e-15 * s) break; } return s; };
        const double norm = i0(beta);
        for (int p = 0; p < 8; ++p)
            for (int j = 0; j < TP_TAPS; ++j) tp_tab_[p][j] = 0.0f;
        for (int p = 1; p < tp_phases_; ++p) {
            for (int j = 0; j < TP_TAPS; ++j) {
                const double d = (double)(j - (TP_TAPS / 2 - 1)) - (double)p / (double)tp_phases_;
                const double sinc = std::fabs(d) < 1e-12 ? 1.0 : std::sin(PI * d) / (PI * d);
                const double r = d / (TP_TAPS / 2.0);
                const double w = std::fabs(r) >= 1.0 ? 0.0 : i0(beta * std::sqrt(1.0 - r * r)) / norm;
                tp_tab_[p][j] = (float)(sinc * w);
            }
        }
    }

    void true_peak_step(float l, float r) {
        const float in[2] = {l, r};
        for (int c = 0; c < 2; ++c) {
            float* h = tp_hist_[c];
            h[tp_pos_] = in[c]; h[tp_pos_ + TP_TAPS] = in[c];
        }
        tp_pos_ = (tp_pos_ + 1) % TP_TAPS;                 // now points at the oldest sample
        for (int c = 0; c < 2; ++c) {
            const float* w = tp_hist_[c] + tp_pos_;
            for (int p = 1; p < tp_phases_; ++p) {
                float acc = 0.0f;
                const float* t = tp_tab_[p];
                for (int j = 0; j < TP_TAPS; ++j) acc += w[j] * t[j];
                const float a = acc < 0 ? -acc : acc;
                if (a > tpeak_) tpeak_ = a;
            }
        }
    }

    int bin_of(double lufs) const {
        int i = (int)std::floor((lufs - BIN_LO) / BIN_W);
        return i < 0 ? -1 : (i >= NBINS ? NBINS - 1 : i);
    }

    void finish_subblock() {
        const double e = acc_ / (double)sub_len_;
        sub_[sub_pos_] = e;
        sub_pos_ = (sub_pos_ + 1) % ST_SUBS;
        if (n_sub_ < 1000000000) ++n_sub_;
        if (n_sub_ >= MO_SUBS) {
            double s = 0.0;
            for (int k = 1; k <= MO_SUBS; ++k) s += sub_[(sub_pos_ - k + ST_SUBS) % ST_SUBS];
            mom_ = lufs_of(s / MO_SUBS);
            if (mom_ > mom_max_) mom_max_ = mom_;
            const int b = bin_of(mom_);
            if (b >= 0) { ++hist_blk_[b]; ++n_blk_; }
        }
        if (n_sub_ >= ST_SUBS) {
            double s = 0.0;
            for (int k = 0; k < ST_SUBS; ++k) s += sub_[k];
            st_ = lufs_of(s / ST_SUBS);
            if (st_ > st_max_) st_max_ = st_;
            const int b = bin_of(st_);
            if (b >= 0) { ++hist_st_[b]; ++n_st_; }
        }
        fresh_ = true;
    }

    // Gated integrated loudness: absolute gate -70 LUFS, then relative gate 10 LU below
    // the mean of what passed.
    double integrated() const {
        if (n_blk_ == 0) return NO_READING;
        double sum = 0.0; uint64_t n = 0;
        for (int i = 0; i < NBINS; ++i) if (hist_blk_[i]) { sum += (double)hist_blk_[i] * bin_energy_[i]; n += hist_blk_[i]; }
        const double rel = lufs_of(sum / (double)n) - 10.0;
        const int i0 = std::max(0, (int)std::ceil((rel - BIN_LO) / BIN_W - 0.5));
        double s2 = 0.0; uint64_t n2 = 0;
        for (int i = i0; i < NBINS; ++i) if (hist_blk_[i]) { s2 += (double)hist_blk_[i] * bin_energy_[i]; n2 += hist_blk_[i]; }
        return n2 ? lufs_of(s2 / (double)n2) : NO_READING;
    }

    // EBU Tech 3342 loudness range: short-term values above -70 LUFS and 20 LU below their
    // mean; the spread between the 10th and 95th percentile.
    double loudness_range() const {
        if (n_st_ < 2) return 0.0;
        double sum = 0.0; uint64_t n = 0;
        for (int i = 0; i < NBINS; ++i) if (hist_st_[i]) { sum += (double)hist_st_[i] * bin_energy_[i]; n += hist_st_[i]; }
        const double rel = lufs_of(sum / (double)n) - 20.0;
        const int i0 = std::max(0, (int)std::ceil((rel - BIN_LO) / BIN_W - 0.5));
        uint64_t tot = 0;
        for (int i = i0; i < NBINS; ++i) tot += hist_st_[i];
        if (tot < 2) return 0.0;
        auto pct = [&](double p) {
            const uint64_t want = (uint64_t)std::floor(p * (double)(tot - 1)) + 1;   // 1-based rank
            uint64_t cum = 0;
            for (int i = i0; i < NBINS; ++i) { cum += hist_st_[i]; if (cum >= want) return bin_centre(i); }
            return bin_centre(NBINS - 1);
        };
        return std::max(0.0, pct(0.95) - pct(0.10));
    }

    bool   ready_ = false;
    double fs_ = 48000.0;
    int    sub_len_ = 4800;
    Biquad sh_, hp_;
    double z_[2][2][2];                   // [channel][stage][state]
    double acc_ = 0.0; int acc_n_ = 0;
    double sub_[ST_SUBS];
    int    sub_pos_ = 0, n_sub_ = 0;
    std::vector<uint32_t> hist_blk_, hist_st_;
    std::vector<double>   bin_energy_;
    uint64_t n_blk_ = 0, n_st_ = 0;
    double mom_ = NO_READING, st_ = NO_READING, mom_max_ = NO_READING, st_max_ = NO_READING;
    float  peak_ = 0.0f, tpeak_ = 0.0f;
    int    tp_phases_ = 8;
    float  tp_tab_[8][TP_TAPS];
    float  tp_hist_[2][2 * TP_TAPS];
    int    tp_pos_ = 0;
    int64_t frames_ = 0;
    bool   fresh_ = false;
};

} // namespace hjl
