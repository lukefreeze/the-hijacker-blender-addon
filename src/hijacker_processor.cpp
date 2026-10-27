// ---------------------------------------------------------------------------
// hijacker_processor.cpp
// The Hijacker — DSP effect chain (EQ, compressor, reverb, gate, delay)
//
// Real-time rules followed in this file (everything here can run on the
// audio thread):
//   * no heap allocation (delay lines are created by hj_ensure_delay_buffers()
//     when an effect slot is assigned; apply_delay() only allocates as a last
//     resort for the offline path)
//   * no locks, no I/O
//   * no per-sample transcendental calls on the hot path where a cheaper
//     exact alternative exists
// ---------------------------------------------------------------------------
#include "hijacker_processor.h"
#include "mixer_ui.h"
#include "ISound.h"
#include "IReader.h"

#include <cmath>
#include <algorithm>
#include <memory>
#include <cstring>
#include <cstdio>
#include <atomic>
#include <mutex>
#include <vector>

extern "C" { extern EngineState g_state; }

static const float PI = 3.14159265358979f;

// ---------------------------------------------------------------------------
// EQ output stage.
//   1 = legacy: s / (1 + |s|) soft saturation after every EQ pass. This colours
//       the sound (and lowers the level) whenever the EQ slot is enabled.
//   0 = off: the EQ is a clean linear filter bank.
//   2 = transparent safety: perfectly linear below 0.9, smooth soft-clip above
//       (only catches true overs).
// Defaults to 2 (clean below 0.9, soft safety above). Build with
// -DHJ_EQ_OUTPUT_MODE=1 to get the legacy colouring back.
// ---------------------------------------------------------------------------
// Reverb wet level at "Wet = 1", relative to the input level (dB). 0 = the same
// loudness as the dry signal; the old engine was about +21..+35 dB.
#ifndef HJ_REVERB_WET_TRIM_DB
#define HJ_REVERB_WET_TRIM_DB 0.0f
#endif

#ifndef HJ_EQ_OUTPUT_MODE
#define HJ_EQ_OUTPUT_MODE 2
#endif

// ===========================================================================
// Param denormalisation
// ===========================================================================
// Two separate threshold ranges for the two compressor types:
//
// Single-band: operates on the FULL unfiltered signal (-1..+1 PCM range).
// A loud voice peaks at -3 to -6dBFS full-spectrum.
// Range -40..0dB: n=0.5 → -20dB fires on typical programme, n=0.8 → -8dB for peaks.
static inline float denorm_threshold_full(float n) { return -40.0f + n * 40.0f; }
//
// Multiband: operates on BAND-SPLIT signal which is 15-25dB lower than full.
// Splitting a -18dBFS signal into 4 bands gives ~-24 to -30dBFS per band.
// Range -60..-20dB: n=0.5 → -40dB, n=0.8 → -28dB — fires correctly on band content.
static inline float denorm_threshold_band(float n) { return -60.0f + n * 40.0f; }
// Legacy alias — multiband uses band version
static inline float denorm_threshold(float n) { return denorm_threshold_band(n); }
static inline float denorm_ratio    (float n) { return  1.0f  + n * 19.0f; }
static inline float denorm_attack   (float n) { return  0.1f  + n * 99.9f; }
static inline float denorm_release  (float n) { return 10.0f  + n * 990.0f; }
static inline float denorm_makeup   (float n) { return  n * 24.0f; }
// Piecewise gain: n=0→-30dB, n=0.5→0dB, n=1→+12dB.
// n<0.5 maps 0..0.5 → -30..0dB (60dB/unit)
// n≥0.5 maps 0.5..1.0 → 0..+12dB (24dB/unit)
// n=0.5 is always 0dB — the fader default.
static inline float denorm_gain(float n) {
    if (n < 0.5f) return (n - 0.5f) * 60.0f;   // 0→-30, 0.5→0
    else          return (n - 0.5f) * 24.0f;   // 0.5→0, 1→+12
}
static inline float denorm_knee     (float n) { return  0.5f + n * 23.5f; }

static inline float time_coeff(float ms, float sr)
{
    if (ms <= 0.0f) return 0.0f;
    return std::exp(-1.0f / (sr * ms * 0.001f));
}

static inline float db2lin(float db) { return std::pow(10.0f, db / 20.0f); }
static inline float lin2db(float lin) { return 20.0f * std::log10(lin + 1e-9f); }

// ===========================================================================
// Soft-knee gain computer
//
// Returns the gain change in dB (<= 0). The caller pre-computes
// `lin_floor` = linear level below which the gain computer is exactly 0 dB
// (threshold minus half the knee), so the common "not compressing" case costs
// one comparison instead of a log10 + pow per sample.
// ===========================================================================
static inline float knee_floor_lin(float thr_db, float knee_db)
{
    return db2lin(thr_db - knee_db * 0.5f) - 1e-9f;
}

static inline float soft_knee_gr_db(float level_lin, float lin_floor,
                                    float thr_db, float ratio, float knee_db)
{
    if (level_lin <= lin_floor) return 0.0f;
    float in_db  = lin2db(level_lin);
    float half_k = knee_db * 0.5f;

    float out_db;
    if (in_db <= thr_db - half_k) {
        out_db = in_db;
    } else if (in_db <= thr_db + half_k && knee_db > 0.0f) {
        float x  = in_db - thr_db + half_k;
        out_db   = in_db + (1.0f/ratio - 1.0f) * (x * x) / (2.0f * knee_db);
    } else {
        out_db = thr_db + (in_db - thr_db) / ratio;
    }
    float gr_db = out_db - in_db;
    return gr_db < 0.0f ? gr_db : 0.0f;
}

// Forward declaration — defined after apply_comp_multi
static void update_fft(int ch, int band, const float* mono_buf, int frames);

// ===========================================================================
// Single-band compressor
// ===========================================================================
static void apply_comp_single(int ch, aud::sample_t* buf,
                               int frames, int n_ch,
                               const EffectSlot& fx,
                               float sr)
{
    float thr_db  = denorm_threshold_full(fx.params[0]);  // -40..0dB for full signal
    float ratio   = denorm_ratio    (fx.params[1]);
    float atk_ms  = denorm_attack   (fx.params[2]);
    float rel_ms  = denorm_release  (fx.params[3]);
    float mkp_lin = db2lin(denorm_makeup(fx.params[4]));
    float knee_db = denorm_knee     (fx.params[5]);

    float atk_c = time_coeff(atk_ms, sr);
    float rel_c = time_coeff(rel_ms, sr);
    float floor_lin = knee_floor_lin(thr_db, knee_db);

    CompressorBandState& st = g_state.comp_state[ch].single;
    float min_gr = 1.0f;

    for (int f = 0; f < frames; ++f) {
        float peak = 0.0f;
        for (int c = 0; c < n_ch; ++c)
            peak = std::max(peak, std::abs(buf[f * n_ch + c]));

        float coeff = (peak > st.envelope) ? atk_c : rel_c;
        st.envelope = coeff * st.envelope + (1.0f - coeff) * peak;

        float gr_db = soft_knee_gr_db(st.envelope, floor_lin, thr_db, ratio, knee_db);
        float gr    = (gr_db < 0.0f) ? db2lin(gr_db) : 1.0f;
        if (gr < min_gr) min_gr = gr;

        float g = gr * mkp_lin;
        for (int c = 0; c < n_ch; ++c)
            buf[f * n_ch + c] *= g;
    }

    float max_gr_db = (min_gr < 1.0f) ? lin2db(min_gr) : 0.0f;
    const float sm = 0.6f;  // faster decay than 0.85 — GR meter responds more snappily
    g_state.gr_levels[ch][0] = sm * g_state.gr_levels[ch][0]
                              + (1.0f - sm) * (-max_gr_db);

    float rms = 0.0f;
    for (int f = 0; f < frames; ++f)
        for (int c = 0; c < n_ch; ++c) {
            float s = buf[f * n_ch + c]; rms += s * s;
        }
    rms = std::sqrt(rms / (frames * n_ch + 1e-9f));
    g_state.band_levels[ch][0] = sm * g_state.band_levels[ch][0]
                                + (1.0f - sm) * rms;
}

// ===========================================================================
// Biquad filter
// ===========================================================================
struct BiquadCoeffs { float b0,b1,b2,a1,a2; };

static BiquadCoeffs butter_lp(float freq, float sr) {
    float w = 2.0f * PI * freq / sr;
    float cw = std::cos(w), sw = std::sin(w);
    float alpha = sw / (2.0f * 0.7071068f), a0 = 1.0f + alpha;
    BiquadCoeffs c;
    c.b0=(1.0f-cw)*0.5f/a0; c.b1=(1.0f-cw)/a0; c.b2=c.b0;
    c.a1=-2.0f*cw/a0; c.a2=(1.0f-alpha)/a0;
    return c;
}

static BiquadCoeffs butter_hp(float freq, float sr) {
    float w = 2.0f * PI * freq / sr;
    float cw = std::cos(w), sw = std::sin(w);
    float alpha = sw / (2.0f * 0.7071068f), a0 = 1.0f + alpha;
    BiquadCoeffs c;
    c.b0=(1.0f+cw)*0.5f/a0; c.b1=-(1.0f+cw)/a0; c.b2=c.b0;
    c.a1=-2.0f*cw/a0; c.a2=(1.0f-alpha)/a0;
    return c;
}

// Second-order all-pass with the same poles as the Butterworth section above.
// LP4 + HP4 of a Linkwitz-Riley crossover sums to exactly this response, so it
// is used to phase-align the bands that skip a later crossover.
static BiquadCoeffs butter_ap(float freq, float sr) {
    float w = 2.0f * PI * freq / sr;
    float cw = std::cos(w), sw = std::sin(w);
    float alpha = sw / (2.0f * 0.7071068f), a0 = 1.0f + alpha;
    BiquadCoeffs c;
    c.b0=(1.0f-alpha)/a0; c.b1=-2.0f*cw/a0; c.b2=1.0f;     // (1+alpha)/a0 == 1
    c.a1=-2.0f*cw/a0; c.a2=(1.0f-alpha)/a0;
    return c;
}

static inline float biquad_step(float x, const BiquadCoeffs& co,
                                 float& z1, float& z2)
{
    float w = x - co.a1*z1 - co.a2*z2;
    float y = co.b0*w + co.b1*z1 + co.b2*z2;
    z2 = z1; z1 = w;
    return y;
}

// lr_filter: apply biquad twice in series (two separate 2-float state arrays).
// state_a[0..1] = first pass z1,z2
// state_b[0..1] = second pass z1,z2
// Two cascaded identical biquads = 4th-order Linkwitz-Riley when LP+HP pair
// is applied at the same crossover frequency.
// NOTE: each state pointer must point to a 2-float array. Do NOT pass a single
// 4-float array and offset — the state arrays in CompressorChannelState are
// lp_z[crossover][pass][channel][0..1], providing exactly 2 floats each.
static void lr_filter(float* buf, int n, const BiquadCoeffs& co,
                      float* state_a, float* state_b)
{
    for (int f = 0; f < n; ++f) {
        float s = buf[f];
        s = biquad_step(s, co, state_a[0], state_a[1]);
        s = biquad_step(s, co, state_b[0], state_b[1]);
        buf[f] = s;
    }
}

static void ap_filter(float* buf, int n, const BiquadCoeffs& co, float* state)
{
    for (int f = 0; f < n; ++f)
        buf[f] = biquad_step(buf[f], co, state[0], state[1]);
}

// ===========================================================================
// Parametric EQ coefficient builders (Audio EQ Cookbook)
// ===========================================================================
static BiquadCoeffs eq_low_shelf(float gain_db, float freq, float Q, float sr)
{
    float A  = std::sqrt(db2lin(gain_db));
    float w0 = 2.0f * PI * freq / sr;
    float cw = std::cos(w0), sw = std::sin(w0);
    float alpha = sw / (2.0f * Q), sqA = std::sqrt(A);
    float b0 =  A*((A+1)-(A-1)*cw+2*sqA*alpha);
    float b1 =  2*A*((A-1)-(A+1)*cw);
    float b2 =  A*((A+1)-(A-1)*cw-2*sqA*alpha);
    float a0 =    (A+1)+(A-1)*cw+2*sqA*alpha;
    float a1 = -2*((A-1)+(A+1)*cw);
    float a2 =    (A+1)+(A-1)*cw-2*sqA*alpha;
    BiquadCoeffs c;
    c.b0=b0/a0; c.b1=b1/a0; c.b2=b2/a0; c.a1=a1/a0; c.a2=a2/a0;
    return c;
}

static BiquadCoeffs eq_high_shelf(float gain_db, float freq, float Q, float sr)
{
    float A  = std::sqrt(db2lin(gain_db));
    float w0 = 2.0f * PI * freq / sr;
    float cw = std::cos(w0), sw = std::sin(w0);
    float alpha = sw / (2.0f * Q), sqA = std::sqrt(A);
    float b0 =  A*((A+1)+(A-1)*cw+2*sqA*alpha);
    float b1 = -2*A*((A-1)+(A+1)*cw);
    float b2 =  A*((A+1)+(A-1)*cw-2*sqA*alpha);
    float a0 =    (A+1)-(A-1)*cw+2*sqA*alpha;
    float a1 =  2*((A-1)-(A+1)*cw);
    float a2 =    (A+1)-(A-1)*cw-2*sqA*alpha;
    BiquadCoeffs c;
    c.b0=b0/a0; c.b1=b1/a0; c.b2=b2/a0; c.a1=a1/a0; c.a2=a2/a0;
    return c;
}

static BiquadCoeffs eq_peak(float gain_db, float freq, float Q, float sr)
{
    float A  = db2lin(gain_db / 2.0f);
    float w0 = 2.0f * PI * freq / sr;
    float cw = std::cos(w0), sw = std::sin(w0);
    float alpha = sw / (2.0f * Q);
    float b0=1+alpha*A, b1=-2*cw, b2=1-alpha*A;
    float a0=1+alpha/A, a1=-2*cw, a2=1-alpha/A;
    BiquadCoeffs c;
    c.b0=b0/a0; c.b1=b1/a0; c.b2=b2/a0; c.a1=a1/a0; c.a2=a2/a0;
    return c;
}

static const float EQ_DEF_FREQ[7] = {80.f,250.f,700.f,2000.f,5000.f,10000.f,16000.f};
static const float EQ_DEF_Q[7]    = {0.7f,1.0f,1.0f,1.0f,1.0f,1.0f,0.7f};

static inline float eq_freq_from_norm(float n) {
    return std::pow(10.0f, 1.30103f + n * 2.69897f);
}
static inline float eq_q_from_norm(float n) {
    return std::pow(10.0f, -1.0f + n * 2.0f);
}

static inline float eq_output_stage(float s)
{
#if HJ_EQ_OUTPUT_MODE == 1
    return s / (1.0f + std::abs(s));
#elif HJ_EQ_OUTPUT_MODE == 2
    const float knee = 0.9f;
    float a = std::abs(s);
    if (a <= knee) return s;
    float over = a - knee;
    float y = knee + over / (1.0f + over / (1.0f - knee));   // asymptote 1.0, C1 at knee
    return s < 0.0f ? -y : y;
#else
    return s;
#endif
}

static void apply_eq_param(int ch, aud::sample_t* buf, int frames, int n_ch,
                            const EffectSlot& fx, float sr)
{
    EqChannelState& eq = g_state.eq_state[ch];
    const BiquadCoeffs ident = {1.0f, 0.0f, 0.0f, 0.0f, 0.0f};

    BiquadCoeffs tgt[7], start[7];
    bool run[7], ramp[7];

    for (int bi = 0; bi < 7; ++bi) {
        float gdb = (fx.params[bi] - 0.5f) * 48.0f;
        bool  active = std::abs(gdb) >= 0.5f;
        if (active) {
            float fn = fx.params[bi+7], qn = fx.params[bi+14];
            float freq = (fn>0.0f) ? eq_freq_from_norm(fn) : EQ_DEF_FREQ[bi];
            float q    = (qn>0.0f) ? eq_q_from_norm(qn)    : EQ_DEF_Q[bi];
            freq = std::max(20.0f, std::min(freq, sr*0.49f));
            q    = std::max(0.1f,  std::min(q,    10.0f));
            if      (bi==0) tgt[bi] = eq_low_shelf (gdb, freq, q, sr);
            else if (bi==6) tgt[bi] = eq_high_shelf(gdb, freq, q, sr);
            else            tgt[bi] = eq_peak      (gdb, freq, q, sr);
        } else {
            tgt[bi] = ident;
        }
        const float* pc = eq.bands[bi].pc;
        start[bi] = { pc[0], pc[1], pc[2], pc[3], pc[4] };
        bool start_is_ident = (pc[0]==1.0f && pc[1]==0.0f && pc[2]==0.0f &&
                               pc[3]==0.0f && pc[4]==0.0f);
        run[bi]  = active || !start_is_ident;      // still fading out => keep running
        ramp[bi] = run[bi] && (start[bi].b0 != tgt[bi].b0 || start[bi].b1 != tgt[bi].b1 ||
                               start[bi].b2 != tgt[bi].b2 || start[bi].a1 != tgt[bi].a1 ||
                               start[bi].a2 != tgt[bi].a2);
    }

    const float inv_frames = 1.0f / (float)std::max(1, frames);
    int nc = std::min(n_ch, 2);
    for (int c = 0; c < nc; ++c) {
        for (int f = 0; f < frames; ++f) {
            float s = buf[f*n_ch+c];
            float t = (float)(f + 1) * inv_frames;
            for (int bi = 0; bi < 7; ++bi) {
                EqBandState& b = eq.bands[bi];
                if (!run[bi]) {
                    // Identity filter: keep the delay line current (w == x) so
                    // a band that is switched on later starts from a consistent state.
                    b.z2[c] = b.z1[c]; b.z1[c] = s;
                    continue;
                }
                BiquadCoeffs co = tgt[bi];
                if (ramp[bi]) {
                    co.b0 = start[bi].b0 + (tgt[bi].b0 - start[bi].b0) * t;
                    co.b1 = start[bi].b1 + (tgt[bi].b1 - start[bi].b1) * t;
                    co.b2 = start[bi].b2 + (tgt[bi].b2 - start[bi].b2) * t;
                    co.a1 = start[bi].a1 + (tgt[bi].a1 - start[bi].a1) * t;
                    co.a2 = start[bi].a2 + (tgt[bi].a2 - start[bi].a2) * t;
                }
                s = biquad_step(s, co, b.z1[c], b.z2[c]);
            }
            buf[f*n_ch+c] = eq_output_stage(s);
        }
    }
    for (int bi = 0; bi < 7; ++bi) {
        float* pc = eq.bands[bi].pc;
        pc[0]=tgt[bi].b0; pc[1]=tgt[bi].b1; pc[2]=tgt[bi].b2;
        pc[3]=tgt[bi].a1; pc[4]=tgt[bi].a2;
    }
}

// ===========================================================================
// Freeverb
// ===========================================================================
static inline float comb_step(CombState& st, float in,
                                int len, float feedback, float damp)
{
    if (st.pos >= len) st.pos = 0;
    float out    = st.buf[st.pos];
    st.filter    = out * (1.0f - damp) + st.filter * damp;
    st.buf[st.pos] = in + st.filter * feedback;
    if (++st.pos >= len) st.pos = 0;
    return out;
}

static inline float allpass_step(AllpassState& st, float in, int len)
{
    if (st.pos >= len) st.pos = 0;
    float buf_out = st.buf[st.pos];
    float out     = -in + buf_out;
    st.buf[st.pos]= in + buf_out * 0.5f;
    if (++st.pos >= len) st.pos = 0;
    return out;
}

static void apply_reverb_param(int ch, aud::sample_t* buf, int frames, int n_ch,
                                const EffectSlot& fx, float sr)
{
    float room_sz   = 0.28f + fx.params[0] * 0.70f;
    float damp      = fx.params[1] * 0.95f;
    float wet       = fx.params[2];
    float dry       = 1.0f - wet * 0.7f;
    float pd_norm   = fx.params[3];
    float width     = fx.params[4];
    int   pd_samp   = (int)(pd_norm * 0.1f * sr);
    pd_samp = std::max(0, std::min(pd_samp, MAX_PREDELAY_SAMP - 1));

    ReverbChannelState& rv = g_state.reverb_state[ch];
    rv.sample_rate = sr;
    float sr_scale = sr / 44100.0f;

    // Wet-path level trim. The summed comb filters amplify the input by roughly
    // sqrt(N / (1 - g^2)) (g = comb feedback), which made the wet signal 21..35 dB
    // louder than the input and grew with room size. Dividing that out (and the
    // constant stereo/allpass gain, measured 11.7 dB) makes "wet = 1" about as
    // loud as the input at any room size, so the Wet knob behaves like a mix control.
    const float comb_gain = std::sqrt((float)PB_REVERB_COMB / (1.0f - room_sz * room_sz));
    const float wet_trim  = std::pow(10.0f, HJ_REVERB_WET_TRIM_DB / 20.0f) / (3.86f * comb_gain);

    // Delay-line lengths depend only on the sample rate: compute them once per
    // buffer instead of once per sample.
    int comb_len_l[PB_REVERB_COMB], comb_len_r[PB_REVERB_COMB];
    int ap_len_l[PB_REVERB_AP],     ap_len_r[PB_REVERB_AP];
    for (int i = 0; i < PB_REVERB_COMB; ++i) {
        comb_len_l[i] = std::max(1, std::min((int)(COMB_LENGTHS_L[i] * sr_scale), MAX_COMB_LEN-1));
        comb_len_r[i] = std::max(1, std::min((int)(COMB_LENGTHS_R[i] * sr_scale), MAX_COMB_LEN-1));
    }
    for (int i = 0; i < PB_REVERB_AP; ++i) {
        ap_len_l[i] = std::max(1, std::min((int)(AP_LENGTHS_L[i] * sr_scale), MAX_AP_LEN-1));
        ap_len_r[i] = std::max(1, std::min((int)(AP_LENGTHS_R[i] * sr_scale), MAX_AP_LEN-1));
    }

    for (int f = 0; f < frames; ++f) {
        float in_l = buf[f*n_ch + 0];
        float in_r = (n_ch > 1) ? buf[f*n_ch + 1] : in_l;

        // Pre-delay: a fixed-size ring that is written every sample and read
        // `pd_samp` samples behind the write head. Changing the pre-delay time
        // therefore never reads stale or mismatched positions.
        int wp = rv.pd_write[0];
        rv.predelay[0][wp] = in_l;
        rv.predelay[1][wp] = in_r;
        float pd_in_l = in_l, pd_in_r = in_r;
        if (pd_samp > 0) {
            int rp = wp - pd_samp;
            if (rp < 0) rp += MAX_PREDELAY_SAMP;
            pd_in_l = rv.predelay[0][rp];
            pd_in_r = rv.predelay[1][rp];
        }
        if (++wp >= MAX_PREDELAY_SAMP) wp = 0;
        rv.pd_write[0] = rv.pd_write[1] = wp;
        float verb_in = (pd_in_l + pd_in_r) * 0.5f;

        float out_l = 0.0f, out_r = 0.0f;
        for (int i = 0; i < PB_REVERB_COMB; ++i) {
            out_l += comb_step(rv.comb[0][i], verb_in, comb_len_l[i], room_sz, damp);
            out_r += comb_step(rv.comb[1][i], verb_in, comb_len_r[i], room_sz, damp);
        }

        for (int i = 0; i < PB_REVERB_AP; ++i) {
            out_l = allpass_step(rv.ap[0][i], out_l, ap_len_l[i]);
            out_r = allpass_step(rv.ap[1][i], out_r, ap_len_r[i]);
        }

        float width_mix = width * 0.5f;
        float wet_l = out_l * (0.5f + width_mix) + out_r * (0.5f - width_mix);
        float wet_r = out_r * (0.5f + width_mix) + out_l * (0.5f - width_mix);

        buf[f*n_ch + 0] = in_l * dry + wet_l * wet * wet_trim;
        if (n_ch > 1)
            buf[f*n_ch + 1] = in_r * dry + wet_r * wet * wet_trim;
    }
}

// ===========================================================================
// Stereo Delay — with feedback, LP filter on tail, and ping-pong mode
//
// Design:
//   - One circular buffer per stereo channel (buf_l, buf_r in DelayChannelState)
//   - Feedback reads from the delay line, applies a one-pole LP filter to
//     darken each echo (simulates tape/air HF absorption), then writes back
//   - Ping-pong: when spread > 0.5, each echo bounces L→R→L instead of
//     repeating in the same channel — achieved by swapping which buffer
//     the feedback is written into
//   - Dry signal is always passed through at full level (mix = wet only)
//   - LP cutoff: 200 Hz (filter=0) → 20000 Hz (filter=1) on a log scale
//   - The delay time is slewed (~20 ms) and read with linear interpolation, so
//     turning the Time control glides like a tape delay instead of clearing the
//     buffer and clicking.
//
// State is NOT reset between process_buffer() batch calls — DelayChannelState
// lives in EngineState and persists across chunks so the tail rings through
// correctly.
// ===========================================================================
static inline int delay_buffer_size_for(float sr)
{
    return (int)std::ceil(2.0f * sr) + 68;     // max 2000 ms + interpolation/pad
}

// Delay lines that were replaced by a larger one. The audio thread may still be
// reading the old block at that moment, so it is kept (not freed) until the
// engine shuts down. This only happens when the sample rate asked for exceeds
// what the line was first sized for - at most a handful of times per session.
static std::mutex            g_delay_retired_mtx;
static std::vector<float*>   g_delay_retired;

void hj_ensure_delay_buffers(int ch, float sr)
{
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return;
    DelayChannelState& st = g_state.delay_state[ch];
    int need = delay_buffer_size_for(sr);
    if (st.buf_l && st.buf_r && st.buf_size >= need) return;
    float* nl = new float[need]();
    float* nr = new float[need]();
    float* ol = st.buf_l; float* orr = st.buf_r;
    st.cur_delay = -1.0f;
    st.filter_z_l = st.filter_z_r = 0.0f;
    st.write_l = st.write_r = 0;
    st.buf_l = nl; st.buf_r = nr; st.buf_size = need;
    if (ol || orr) {
        std::lock_guard<std::mutex> lk(g_delay_retired_mtx);
        if (ol)  g_delay_retired.push_back(ol);
        if (orr) g_delay_retired.push_back(orr);
    }
}

void hj_release_all_delay_buffers()
{
    for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch) {
        DelayChannelState& st = g_state.delay_state[ch];
        delete[] st.buf_l; delete[] st.buf_r;
        st = DelayChannelState{};
    }
    std::lock_guard<std::mutex> lk(g_delay_retired_mtx);
    for (float* p : g_delay_retired) delete[] p;
    g_delay_retired.clear();
}

static void apply_delay(int ch, aud::sample_t* buf, int frames, int n_ch,
                         const EffectSlot& fx, float sr)
{
    // Denormalise params
    float delay_ms  = 1.0f + fx.params[0] * 1999.0f;   // 1..2000 ms
    float feedback  = fx.params[1] * 0.92f;              // cap at 0.92
    float mix       = fx.params[2];
    float spread    = fx.params[3];
    bool  ping_pong = (spread > 0.5f);
    float filt_hz   = 200.0f * std::pow(100.0f, fx.params[4]);
    filt_hz = std::max(200.0f, std::min(filt_hz, sr * 0.49f));
    float lp_a = 1.0f - std::exp(-2.0f * PI * filt_hz / sr);

    int delay_samp = (int)(delay_ms * 0.001f * sr);
    delay_samp = std::max(1, delay_samp);
    const float target = (float)delay_samp;

    DelayChannelState& st = g_state.delay_state[ch];

    // Normally allocated when the slot is assigned (hj_ensure_delay_buffers).
    // This is only a safety net for the offline/batch path.
    int need = delay_buffer_size_for(sr);
    if (st.buf_l == nullptr || st.buf_r == nullptr || st.buf_size < need)
        hj_ensure_delay_buffers(ch, sr);

    if (st.cur_delay < 0.0f) st.cur_delay = target;

    const int   buf_sz = st.buf_size;
    const float slew   = 1.0f - std::exp(-1.0f / (0.020f * sr));
    float cur = st.cur_delay;

    for (int f = 0; f < frames; ++f) {
        float dry_l = buf[f * n_ch + 0];
        float dry_r = (n_ch > 1) ? buf[f * n_ch + 1] : dry_l;

        if (cur != target) {
            cur += (target - cur) * slew;
            if (std::abs(target - cur) < 0.02f) cur = target;
        }
        int   di = (int)cur;
        float fr = cur - (float)di;

        int ia = st.write_l - di;  if (ia < 0) ia += buf_sz;
        int ib = ia - 1;           if (ib < 0) ib += buf_sz;
        int ja = st.write_r - di;  if (ja < 0) ja += buf_sz;
        int jb = ja - 1;           if (jb < 0) jb += buf_sz;

        float echo_l, echo_r;
        if (fr == 0.0f) {
            echo_l = st.buf_l[ia];
            echo_r = st.buf_r[ja];
        } else {
            echo_l = st.buf_l[ia] + fr * (st.buf_l[ib] - st.buf_l[ia]);
            echo_r = st.buf_r[ja] + fr * (st.buf_r[jb] - st.buf_r[ja]);
        }

        st.filter_z_l = lp_a * echo_l + (1.0f - lp_a) * st.filter_z_l;
        st.filter_z_r = lp_a * echo_r + (1.0f - lp_a) * st.filter_z_r;
        float filt_l = st.filter_z_l;
        float filt_r = st.filter_z_r;

        if (ping_pong && n_ch > 1) {
            st.buf_l[st.write_l] = dry_l + filt_r * feedback;
            st.buf_r[st.write_r] = dry_r + filt_l * feedback;
        } else {
            st.buf_l[st.write_l] = dry_l + filt_l * feedback;
            st.buf_r[st.write_r] = dry_r + filt_r * feedback;
        }

        if (++st.write_l >= buf_sz) st.write_l = 0;
        if (++st.write_r >= buf_sz) st.write_r = 0;

        buf[f * n_ch + 0] = dry_l + echo_l * mix;
        if (n_ch > 1)
            buf[f * n_ch + 1] = dry_r + echo_r * mix;
    }
    st.cur_delay = cur;
    st.last_delay_samp = delay_samp;
}

// ===========================================================================
// Full-spectrum analyser — 128 log-spaced bins 20Hz-20kHz
// Called from apply_effect_chain_batch on the raw pre-EQ signal.
// ===========================================================================
// The audio thread only fills a ring buffer. The actual spectrum analysis
// (compute_spec_bins_all) runs from the Python meter timer (~10x/sec), which
// is OFF the audio thread.

// ===========================================================================
// FFT (per-band display)
//
// A direct 64-point DFT. The Hann window and the cos/sin twiddles are
// constants, so they are tabulated once instead of being recomputed with
// trigonometry for each of the 32 x 64 products on every call.
// ===========================================================================
namespace {
struct FFTTables {
    float win[PB_FFT_BINS * 2];
    float cs [PB_FFT_BINS][PB_FFT_BINS * 2];
    float sn [PB_FFT_BINS][PB_FFT_BINS * 2];
    FFTTables() {
        const int N = PB_FFT_BINS * 2;
        for (int n = 0; n < N; ++n)
            win[n] = 0.5f * (1.0f - std::cos(2.0f * PI * n / (N - 1)));
        for (int k = 0; k < PB_FFT_BINS; ++k)
            for (int n = 0; n < N; ++n) {
                float ang = 2.0f * PI * k * n / N;
                cs[k][n] = std::cos(ang);
                sn[k][n] = std::sin(ang);
            }
    }
};
// Built during static initialisation (module load), i.e. before any audio runs.
const FFTTables g_fft_tables;
}

static void update_fft(int ch, int band, const float* mono_buf, int frames)
{
    FFTBandState& fs = g_state.fft_state[ch][band];
    const int N = PB_FFT_BINS * 2;

    for (int f = 0; f < frames && f < N; ++f) {
        fs.window_buf[fs.write_pos] = mono_buf[f];
        fs.write_pos = (fs.write_pos + 1) % N;
    }

    float xw[PB_FFT_BINS * 2];
    for (int n = 0; n < N; ++n) {
        int idx = fs.write_pos + n; if (idx >= N) idx -= N;
        xw[n] = fs.window_buf[idx] * g_fft_tables.win[n];
    }

    for (int k = 0; k < PB_FFT_BINS; ++k) {
        float re = 0.0f, im = 0.0f;
        const float* cs = g_fft_tables.cs[k];
        const float* sn = g_fft_tables.sn[k];
        for (int n = 0; n < N; ++n) {
            re += xw[n] * cs[n];
            im -= xw[n] * sn[n];
        }
        float mag = std::sqrt(re*re + im*im) / (N * 0.5f);
        float normalised = std::max(0.0f, (lin2db(mag) + 80.0f) / 80.0f);
        const float sm = 0.6f;
        g_state.fft_bins[ch][band][k] = sm * g_state.fft_bins[ch][band][k]
                                       + (1.0f - sm) * normalised;
    }
}

// ===========================================================================
// Multiband compressor
// ===========================================================================
static const float CROSSOVER_HZ[3] = {120.0f, 800.0f, 5000.0f};

static void apply_comp_multi(int ch, aud::sample_t* buf,
                              int frames, int n_ch,
                              const EffectSlot& fx,
                              float sr)
{
    static const int MAX_F = 1024;

    // Longer buffers are processed in MAX_F-sized pieces (all filter and
    // envelope state carries over, so the result is identical).
    if (frames > MAX_F) {
        for (int off = 0; off < frames; off += MAX_F)
            apply_comp_multi(ch, buf + off * n_ch, std::min(MAX_F, frames - off),
                             n_ch, fx, sr);
        return;
    }

    float p[24];
    memcpy(p, fx.params, sizeof(p));

    BiquadCoeffs lp_co[3], hp_co[3], ap_co[3];
    for (int x = 0; x < 3; ++x) {
        lp_co[x] = butter_lp(CROSSOVER_HZ[x], sr);
        hp_co[x] = butter_hp(CROSSOVER_HZ[x], sr);
        ap_co[x] = butter_ap(CROSSOVER_HZ[x], sr);
    }

    // ---------------------------------------------------------------------------
    // STEREO-LINKED ARCHITECTURE
    //
    // Filter ALL audio channels into per-band buffers first, then run ONE
    // envelope follower per band using max(|L|, |R|) as the detector signal.
    // Apply the same GR to every audio channel simultaneously.
    //
    // CROSSOVER (4th-order Linkwitz-Riley, cascaded tree):
    //     x ─┬─ LP120 ──► AP800 ► AP5000 ─────────────────► band 0
    //        └─ HP120 ─┬─ LP800 ───────────► AP5000 ───────► band 1
    //                  └─ HP800 ─┬─ LP5000 ──────────────────► band 2
    //                            └─ HP5000 ──────────────────► band 3
    // The all-pass sections give the bands that skip a later split the same
    // phase shift as the bands that go through it, so with the compressors
    // idle the four bands sum back to an all-pass (flat magnitude) response.
    // ---------------------------------------------------------------------------
    static float band_bufs[PB_MAX_CHANNELS][2][PB_MB_BANDS][MAX_F];
    float mono[MAX_F];
    float rest[MAX_F];
    float rest2[MAX_F];

    CompressorChannelState& cs = g_state.comp_state[ch];

    for (int c = 0; c < n_ch && c < 2; ++c) {
        for (int f = 0; f < frames; ++f) mono[f] = buf[f * n_ch + c];

        float* b0 = band_bufs[ch][c][0];
        float* b1 = band_bufs[ch][c][1];
        float* b2 = band_bufs[ch][c][2];
        float* b3 = band_bufs[ch][c][3];

        // split 1 @ 120 Hz
        memcpy(b0,   mono, frames * sizeof(float));
        lr_filter(b0,   frames, lp_co[0], &cs.lp_z[0][0][c][0], &cs.lp_z[0][1][c][0]);
        memcpy(rest, mono, frames * sizeof(float));
        lr_filter(rest, frames, hp_co[0], &cs.hp_z[0][0][c][0], &cs.hp_z[0][1][c][0]);

        // split 2 @ 800 Hz
        memcpy(b1,    rest, frames * sizeof(float));
        lr_filter(b1,    frames, lp_co[1], &cs.lp_z[1][0][c][0], &cs.lp_z[1][1][c][0]);
        memcpy(rest2, rest, frames * sizeof(float));
        lr_filter(rest2, frames, hp_co[1], &cs.hp_z[1][0][c][0], &cs.hp_z[1][1][c][0]);

        // split 3 @ 5 kHz
        memcpy(b2, rest2, frames * sizeof(float));
        lr_filter(b2, frames, lp_co[2], &cs.lp_z[2][0][c][0], &cs.lp_z[2][1][c][0]);
        memcpy(b3, rest2, frames * sizeof(float));
        lr_filter(b3, frames, hp_co[2], &cs.hp_z[2][0][c][0], &cs.hp_z[2][1][c][0]);

        // phase compensation for the bands that skip later splits
        ap_filter(b0, frames, ap_co[1], &cs.lp_z[3][0][c][0]);   // band 0 through 800 Hz AP
        ap_filter(b0, frames, ap_co[2], &cs.lp_z[3][1][c][0]);   // band 0 through 5 kHz AP
        ap_filter(b1, frames, ap_co[2], &cs.hp_z[3][0][c][0]);   // band 1 through 5 kHz AP
    }
    // If mono, copy channel 0 to channel 1 slot so the GR loop is uniform
    if (n_ch == 1) {
        for (int b = 0; b < PB_MB_BANDS; ++b)
            memcpy(band_bufs[ch][1][b], band_bufs[ch][0][b], frames * sizeof(float));
    }

    // Per-band stereo-linked compression
    float band_sum[PB_MB_BANDS][MAX_F];  // post-GR ch0 for display
    for (int b = 0; b < PB_MB_BANDS; ++b) {
        float thr_db  = denorm_threshold(p[b]);
        float ratio   = denorm_ratio    (p[b + 4]);
        float atk_ms  = denorm_attack   (p[b + 8]);
        float rel_ms  = denorm_release  (p[b + 12]);
        float gain_lin= db2lin(denorm_gain(p[b + 16]));
        float knee_db = denorm_knee     (p[b + 20]);

        float atk_c = time_coeff(atk_ms, sr);
        float rel_c = time_coeff(rel_ms, sr);
        float floor_lin = knee_floor_lin(thr_db, knee_db);
        CompressorBandState& st = cs.bands[b];
        float min_gr = 1.0f;

        float* l = band_bufs[ch][0][b];
        float* r = band_bufs[ch][1][b];
        const bool two = (n_ch > 1);

        for (int f = 0; f < frames; ++f) {
            // Stereo-linked detector: max absolute value across all channels
            float peak = std::abs(l[f]);
            if (two) peak = std::max(peak, std::abs(r[f]));

            float coeff = (peak > st.envelope) ? atk_c : rel_c;
            st.envelope = coeff * st.envelope + (1.0f - coeff) * peak;

            float gr_db = soft_knee_gr_db(st.envelope, floor_lin, thr_db, ratio, knee_db);
            float gr    = (gr_db < 0.0f) ? db2lin(gr_db) : 1.0f;
            if (gr < min_gr) min_gr = gr;

            // Apply identical GR to all channels — preserves stereo image
            float g = gr * gain_lin;
            l[f] *= g;
            if (two) r[f] *= g;
        }

        float max_gr_db = (min_gr < 1.0f) ? lin2db(min_gr) : 0.0f;
        const float sm = 0.85f;
        g_state.gr_levels[ch][b] = sm * g_state.gr_levels[ch][b]
                                 + (1.0f - sm) * (-max_gr_db);
        update_fft(ch, b, band_bufs[ch][0][b], frames);
        memcpy(band_sum[b], band_bufs[ch][0][b], frames * sizeof(float));
    }

    // Reconstruct — sum the (phase-aligned) bands back per channel.
    for (int c = 0; c < n_ch && c < 2; ++c) {
        const float* b0 = band_bufs[ch][c][0];
        const float* b1 = band_bufs[ch][c][1];
        const float* b2 = band_bufs[ch][c][2];
        const float* b3 = band_bufs[ch][c][3];
        for (int f = 0; f < frames; ++f)
            buf[f * n_ch + c] = b0[f] + b1[f] + b2[f] + b3[f];
    }

    // Band RMS display
    const float sm = 0.85f;
    for (int b = 0; b < PB_MB_BANDS; ++b) {
        float rms = 0.0f;
        for (int f = 0; f < frames; ++f) rms += band_sum[b][f] * band_sum[b][f];
        rms = std::sqrt(rms / (frames + 1e-9f));
        g_state.band_levels[ch][b] = sm * g_state.band_levels[ch][b]
                                    + (1.0f - sm) * rms;
    }
}

// ===========================================================================
// Noise Gate
// ===========================================================================
static inline float gate_thr_db  (float n){ return -60.0f + n * 60.0f;  }
static inline float gate_atk_ms  (float n){ return   0.1f + n * 99.9f;  }
static inline float gate_hold_ms (float n){ return   0.0f + n * 500.0f; }
static inline float gate_rel_ms  (float n){ return  10.0f + n * 990.0f; }
static inline float gate_range_db(float n){ return -90.0f + n * 90.0f;  }

static void apply_noise_gate(int ch, aud::sample_t* buf,
                              int frames, int n_ch,
                              const EffectSlot& fx, float sr)
{
    float thr_lin    = db2lin(gate_thr_db  (fx.params[0]));
    float atk_c      = time_coeff(gate_atk_ms (fx.params[1]), sr);
    float hold_total = gate_hold_ms(fx.params[2]) * 0.001f * sr;
    float rel_c      = time_coeff(gate_rel_ms (fx.params[3]), sr);
    float range_lin  = db2lin(gate_range_db(fx.params[4]));

    GateChannelState& st = g_state.gate_state[ch];
    float min_gain = 1.0f;

    for (int f = 0; f < frames; ++f) {
        float peak = 0.0f;
        for (int c = 0; c < n_ch; ++c)
            peak = std::max(peak, std::abs(buf[f * n_ch + c]));

        float env_c  = (peak > st.envelope) ? atk_c : rel_c;
        st.envelope  = env_c * st.envelope + (1.0f - env_c) * peak;

        if (st.envelope >= thr_lin) {
            st.is_open   = true;
            st.hold_samp = hold_total;
        } else if (st.is_open) {
            if (st.hold_samp > 0.0f)
                st.hold_samp -= 1.0f;
            else
                st.is_open = false;
        }

        // Gain is smoothed directly in the linear domain (the previous
        // version converted to dB and back with pow/log10 every sample).
        float target   = st.is_open ? 1.0f : range_lin;
        float cur_lin  = st.gain_lin;
        float gain_c   = (target > cur_lin) ? atk_c : rel_c;
        float smooth   = gain_c * cur_lin + (1.0f - gain_c) * target;
        smooth         = std::max(smooth, 1e-9f);
        st.gain_lin    = smooth;

        if (smooth < min_gain) min_gain = smooth;

        for (int c = 0; c < n_ch; ++c)
            buf[f * n_ch + c] *= smooth;
    }

    float max_gr_db = (min_gain < 1.0f) ? lin2db(min_gain) : 0.0f;
    const float sm = 0.85f;
    g_state.gr_levels[ch][1] = sm * g_state.gr_levels[ch][1]
                              + (1.0f - sm) * (-max_gr_db);
    g_state.gr_levels[ch][2] = st.is_open ? 1.0f : 0.0f;
}

// ===========================================================================
// State helpers
// ===========================================================================
void hj_snapshot_slot(int ch, int slot, EffectSlot& out)
{
    const EffectSlot& src = g_state.effect_chain[ch][slot];
    std::atomic<uint32_t>& seq = g_state.effect_seq[ch][slot];
    for (int tries = 0; tries < 4; ++tries) {
        uint32_t s1 = seq.load(std::memory_order_acquire);
        if (s1 & 1u) continue;                       // writer in progress
        memcpy(&out, &src, sizeof(EffectSlot));
        std::atomic_thread_fence(std::memory_order_acquire);
        uint32_t s2 = seq.load(std::memory_order_relaxed);
        if (s1 == s2) return;                        // consistent copy
    }
    memcpy(&out, &src, sizeof(EffectSlot));          // give up: best effort
}

void hj_reset_channel_dsp(int ch)
{
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return;
    g_state.comp_state[ch] = CompressorChannelState{};
    for (int bi = 0; bi < 7; ++bi) {
        EqBandState& b = g_state.eq_state[ch].bands[bi];
        b.z1[0] = b.z1[1] = b.z2[0] = b.z2[1] = 0.0f;
        // Forget the coefficients of the previous run too: they were computed for that run's sample
        // rate and would otherwise be cross-faded into the first block of the next one.
        b.pc[0] = 1.0f; b.pc[1] = b.pc[2] = b.pc[3] = b.pc[4] = 0.0f;
    }
    ReverbChannelState& rv = g_state.reverb_state[ch];
    float sr = rv.sample_rate;
    memset((void*)&rv, 0, sizeof(rv));
    rv.sample_rate = sr;
    g_state.gate_state[ch] = GateChannelState{};
    DelayChannelState& d = g_state.delay_state[ch];
    if (d.buf_l) memset(d.buf_l, 0, sizeof(float) * d.buf_size);
    if (d.buf_r) memset(d.buf_r, 0, sizeof(float) * d.buf_size);
    d.write_l = d.write_r = 0;
    d.filter_z_l = d.filter_z_r = 0.0f;
}

bool hj_channel_has_fx(int ch)
{
    if (ch < 0 || ch >= PB_MAX_CHANNELS) return false;
    EffectSlot fx;
    for (int s = 0; s < PB_MAX_EFFECTS; ++s) {
        hj_snapshot_slot(ch, s, fx);
        if (fx.enabled) return true;
    }
    return false;
}

void hj_reset_dsp_for_active_fx(int ch)
{
    if (hj_channel_has_fx(ch)) hj_reset_channel_dsp(ch);
}

// ===========================================================================
// apply_effect_chain_batch
// ===========================================================================
void apply_effect_chain_batch(int ch, aud::sample_t* buf,
                                int frames, int n_ch, float sr)
{
    // Mono mix of the raw signal into the spectrum-display ring buffer.
    // (Only a ring write here — the analysis itself runs on the Python timer.)
    {
        const int N = PB_SPEC_WIN;
        int wpos = g_state.spec_wpos[ch];
        const float inv = 1.0f / (float)n_ch;
        for (int f = 0; f < frames; ++f) {
            float s = 0.0f;
            for (int c = 0; c < n_ch; ++c) s += buf[f * n_ch + c];
            g_state.spec_window[ch][wpos] = s * inv;
            if (++wpos >= N) wpos = 0;
        }
        g_state.spec_wpos[ch] = wpos;
    }

    for (int slot = 0; slot < PB_MAX_EFFECTS; ++slot) {
        EffectSlot fx;                                // consistent snapshot
        hj_snapshot_slot(ch, slot, fx);
        if (!fx.enabled) continue;
        switch (fx.type) {
        case EffectType::GAIN:
            for (int f = 0; f < frames; ++f)
                for (int c = 0; c < n_ch; ++c)
                    buf[f*n_ch+c] *= fx.params[0];
            break;
        case EffectType::COMP_SINGLE:
            apply_comp_single(ch, buf, frames, n_ch, fx, sr); break;
        case EffectType::COMP_MULTI:
            apply_comp_multi (ch, buf, frames, n_ch, fx, sr); break;
        case EffectType::EQ_PARAM:
            apply_eq_param   (ch, buf, frames, n_ch, fx, sr); break;
        case EffectType::REVERB_PARAM:
            apply_reverb_param(ch, buf, frames, n_ch, fx, sr); break;
        case EffectType::GATE_PARAM:
            apply_noise_gate (ch, buf, frames, n_ch, fx, sr); break;
        case EffectType::DELAY_PARAM:
            apply_delay      (ch, buf, frames, n_ch, fx, sr); break;
        case EffectType::REVERB: break;
        default: break;
        }
    }
}

// ===========================================================================
// C-linkage — spectrum analysis called from Python meter timer
// ===========================================================================
extern "C" {

// Called from Python meter timer (NOT audio thread).
// Analyses the most recent 512 samples of each channel's ring buffer with a
// Hann-windowed DFT at 128 log-spaced frequencies (20 Hz - 20 kHz).
// Each bin is a complex oscillator stepped by a rotation (no trigonometry in
// the inner loop) in double precision: far cheaper than before, and accurate
// at high frequencies where the old single-precision angle (~1e6 rad)
// had lost all its fractional bits.
void compute_spec_bins_all(float sample_rate)
{
    const int WIN = 512;
    const float log_min = std::log10(20.0f);
    const float log_max = std::log10(20000.0f);
    const float sm = 0.35f;

    static float hann[WIN];
    static bool  hann_ready = false;
    if (!hann_ready) {
        for (int n = 0; n < WIN; ++n)
            hann[n] = 0.5f * (1.0f - std::cos(2.0f * PI * n / (WIN - 1)));
        hann_ready = true;
    }

    for (int ch = 0; ch < PB_MAX_CHANNELS; ++ch) {
        bool has_signal = false;
        int wpos = g_state.spec_wpos[ch];
        for (int i = 0; i < WIN; i += 32) {
            int idx = (wpos - WIN + i + PB_SPEC_WIN) % PB_SPEC_WIN;
            if (g_state.spec_window[ch][idx] != 0.0f) { has_signal = true; break; }
        }
        if (!has_signal) continue;

        double xw[WIN];
        for (int n = 0; n < WIN; ++n) {
            int idx = (wpos - WIN + n + PB_SPEC_WIN) % PB_SPEC_WIN;
            xw[n] = (double)g_state.spec_window[ch][idx] * hann[n];
        }

        for (int k = 0; k < PB_SPEC_BINS; ++k) {
            float t    = (float)k / (PB_SPEC_BINS - 1);
            float f_hz = std::pow(10.0f, log_min + t * (log_max - log_min));
            double theta = 2.0 * 3.14159265358979323846 * (double)f_hz / (double)sample_rate;
            double rc = std::cos(theta), rs = std::sin(theta);
            double zc = 1.0, zs = 0.0;
            double re = 0.0, im = 0.0;
            for (int n = 0; n < WIN; ++n) {
                re += xw[n] * zc;
                im -= xw[n] * zs;
                double nc = zc * rc - zs * rs;
                double ns = zc * rs + zs * rc;
                zc = nc; zs = ns;
            }
            float mag        = (float)std::sqrt(re * re + im * im) / (WIN * 0.5f);
            float normalised = std::max(0.0f, (lin2db(mag) + 80.0f) / 80.0f);
            g_state.spec_bins[ch][k] = sm * g_state.spec_bins[ch][k]
                                     + (1.0f - sm) * normalised;
        }
    }
}

} // extern "C"
