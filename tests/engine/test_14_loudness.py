"""Loudness metering (ITU-R BS.1770-4 / EBU R128): momentary, short-term, gated integrated,
loudness range, sample peak and true peak. Checked three ways:
  * against the standard's calibration (a 997 Hz sine at -23 dBFS in both channels reads -23.0 LUFS),
  * against an independent numpy/scipy implementation that uses the standard's published 48 kHz
    filter coefficients and exact (un-binned) gating,
  * true peak against an 8x band-limited resampling of the same signal.
Both paths are covered: the offline renderer (Mixdown) and the live master meter."""
import sys, os, numpy as np, tempfile, ctypes
from scipy.signal import lfilter, resample_poly
sys.path.insert(0, os.path.dirname(__file__)); from hj import *
lib = sys.argv[1]; FR = 480
tmp = tempfile.mkdtemp(); ok = True
def check(name, cond, detail=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail else "")); ok &= bool(cond)

# ---- independent reference -----------------------------------------------------------------------
PUB_SHELF_B = [1.53512485958697, -2.69169618940638, 1.19839281085285]; PUB_SHELF_A = [1.0, -1.69065929318241, 0.73248077421585]
PUB_HP_B = [1.0, -2.0, 1.0];                                           PUB_HP_A = [1.0, -1.99004745483398, 0.99007225036621]
def kcoef(fs):
    if fs == 48000: return PUB_SHELF_B, PUB_SHELF_A, PUB_HP_B, PUB_HP_A
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = np.tan(np.pi * f0 / fs); Vh = 10 ** (G / 20); Vb = Vh ** 0.4996667741545416; a0 = 1 + K / Q + K * K
    sb = [(Vh + Vb * K / Q + K * K) / a0, 2 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0]; sa = [1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = np.tan(np.pi * f0 / fs); a0 = 1 + K / Q + K * K
    return sb, sa, [1.0, -2.0, 1.0], [1.0, 2 * (K * K - 1) / a0, (1 - K / Q + K * K) / a0]
def ref_loudness(x, fs):
    sb, sa, hb, ha = kcoef(fs)
    z = lfilter(hb, ha, lfilter(sb, sa, x.astype(np.float64), axis=0), axis=0)
    e = (z ** 2).sum(1); sub = int(round(0.1 * fs)); nsub = len(e) // sub
    se = e[:nsub * sub].reshape(nsub, sub).mean(1)
    L = lambda v: -0.691 + 10 * np.log10(v)
    blk = np.array([se[i:i + 4].mean() for i in range(nsub - 3)]); lb = L(blk)
    st = np.array([se[i:i + 30].mean() for i in range(nsub - 29)]); ls = L(st)
    g = blk[lb > -70]
    integ = np.nan
    if len(g):
        rel = L(g.mean()) - 10; g2 = blk[lb > max(-70, rel)]
        integ = L(g2.mean()) if len(g2) else np.nan
    lra = 0.0
    s = st[ls > -70]
    if len(s) > 1:
        s2 = ls[ls > max(-70, L(s.mean()) - 20)]
        if len(s2) > 1: lra = np.percentile(s2, 95) - np.percentile(s2, 10)
    return dict(integ=integ, lra=lra, mom_max=lb.max() if len(lb) else np.nan, st_max=ls.max() if len(ls) else np.nan,
                mom_last=lb[-1] if len(lb) else np.nan, st_last=ls[-1] if len(ls) else np.nan)
def true_peak_ref(x): return 20 * np.log10(np.abs(resample_poly(x.astype(np.float64), 8, 1, axis=0, padtype='line')).max())

# ---- engine access ---------------------------------------------------------------------------------
def offline_loud(x, fs, secs=None):
    """Plays x (stereo float array at rate fs) through channel 0 and renders it offline at fs.
    Returns (rendered output, loudness readings)."""
    p = os.path.join(tmp, f"sig_{abs(hash(x.tobytes())) % 10**9}_{fs}.wav"); write_wav(p, x, fs, 'f32')
    hj = HJ(lib); assert hj.init(48000) == 1; L = hj.l
    hj.playlist(0, [dict(path=p, off=0, dur=len(x) / fs, pos=0.0)]); L.t_vol(0, 1.0); L.t_pan(0, 0.5)
    L.t_off_begin.argtypes = [ctypes.c_double, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_int]
    L.t_off_render.argtypes = [ctypes.c_void_p, ctypes.c_int]; L.t_off_loud.argtypes = [ctypes.c_void_p]
    assert L.t_off_begin(0.0, fs, 1, 1, 1) == 1
    n = len(x); out = np.zeros((n, 2), dtype=np.float32); pos = 0
    for step in (333, 5000, 10 ** 9):
        m = min(step, n - pos)
        if m > 0: L.t_off_render(out[pos:].ctypes.data, m); pos += m
    o = np.zeros(9); L.t_off_loud(o.ctypes.data); L.t_off_end(); L.t_shutdown()
    return out, dict(mom=o[0], st=o[1], integ=o[2], lra=o[3], mom_max=o[4], st_max=o[5], tp=o[6], sp=o[7], dur=o[8])

def sine(f, fs, secs, amp, phase=0.0):
    t = np.arange(int(secs * fs)) / fs; s = amp * np.sin(2 * np.pi * f * t + phase); return np.stack([s, s], 1)
db = lambda v: 20 * np.log10(v)

# 1. calibration: 997 Hz, -23 dBFS peak, both channels -> -23.0 LUFS at every rate -----------------
for fs in (44100, 48000, 96000):
    amp = 10 ** (-23 / 20)
    out, r = offline_loud(sine(997, fs, 20, amp), fs)
    check(f"-23 dBFS sine reads -23.0 LUFS integrated @{fs}", abs(r['integ'] + 23.0) < 0.06, f"{r['integ']:.3f}")
    check(f"  momentary / short-term agree @{fs}", abs(r['mom'] + 23.0) < 0.06 and abs(r['st'] + 23.0) < 0.06, f"{r['mom']:.3f} {r['st']:.3f}")
    check(f"  sample peak -23 dBFS @{fs}", abs(r['sp'] + 23.0) < 0.05, f"{r['sp']:.3f}")
    check(f"  LRA of a steady tone is ~0 @{fs}", r['lra'] < 0.1, f"{r['lra']:.3f}")
    check(f"  measured duration @{fs}", abs(r['dur'] - 20.0) < 0.2, f"{r['dur']:.2f}")
# a mono-looking signal in one channel only is 3 dB quieter than the same in both
x = sine(997, 48000, 10, 10 ** (-23 / 20)); x[:, 1] = 0
_, r = offline_loud(x, 48000)
check("one channel only reads 3 dB lower", abs(r['integ'] + 26.0) < 0.06, f"{r['integ']:.3f}")

# 2. programme material: noise bursts at several levels with silence, gated + range vs the reference --
rng = np.random.default_rng(7)
def seg(secs, level_db, fs=48000):
    n = int(secs * fs); w = rng.standard_normal((n, 2)); w = lfilter([1, -0.6], [1], w, axis=0)   # a little tilt, like music
    return w / np.sqrt((w ** 2).mean()) * 10 ** (level_db / 20)
prog = np.concatenate([seg(8, -22), seg(3, -90), seg(10, -16), seg(6, -34), seg(5, -80), seg(9, -26), seg(5, -12)])
prog = np.clip(prog, -0.95, 0.95)
out, r = offline_loud(prog.astype(np.float32), 48000)
ref = ref_loudness(out, 48000)
check("programme: integrated (gated) loudness matches the reference", abs(r['integ'] - ref['integ']) < 0.03, f"{r['integ']:.3f} vs {ref['integ']:.3f}")
check("programme: loudness range matches", abs(r['lra'] - ref['lra']) < 0.15, f"{r['lra']:.2f} vs {ref['lra']:.2f}")
check("programme: momentary max matches", abs(r['mom_max'] - ref['mom_max']) < 0.02, f"{r['mom_max']:.3f} vs {ref['mom_max']:.3f}")
check("programme: short-term max matches", abs(r['st_max'] - ref['st_max']) < 0.02, f"{r['st_max']:.3f} vs {ref['st_max']:.3f}")
check("programme: last momentary / short-term match", abs(r['mom'] - ref['mom_last']) < 0.02 and abs(r['st'] - ref['st_last']) < 0.02, f"{r['mom']:.3f} {r['st']:.3f}")
# gating really ignores silence: the same material without the silent parts reads the same integrated loudness
prog2 = np.concatenate([seg(8, -22), seg(10, -16), seg(6, -34), seg(9, -26), seg(5, -12)])
# (different random noise, so allow a little spread) and a silence-only file has no integrated reading
_, rs = offline_loud(np.zeros((48000 * 5, 2), dtype=np.float32), 48000)
check("silence gives no integrated reading", rs['integ'] <= -150 and rs['tp'] <= -150, f"{rs['integ']} {rs['tp']}")
_, rq = offline_loud(sine(997, 48000, 15, 10 ** (-80 / 20)).astype(np.float32), 48000)
check("a signal under the -70 LUFS gate gives no integrated reading", rq['integ'] <= -150, f"{rq['integ']}")
# quiet intro (-45 LUFS, below the relative gate) does not drag the integrated figure down
loud = sine(997, 48000, 12, 10 ** (-20 / 20)); quiet = sine(997, 48000, 12, 10 ** (-45 / 20))
_, rg = offline_loud(np.concatenate([quiet, loud]).astype(np.float32), 48000)
check("relative gate: quiet passage is ignored", abs(rg['integ'] + 20.0) < 0.1, f"{rg['integ']:.3f}")

# 3. true peak ---------------------------------------------------------------------------------------
# a sine at fs/4 whose samples never reach the crest: sample peak 0.9*cos(45deg), true peak 0.9
for fs in (48000, 44100):
    x = sine(fs / 4, fs, 3, 0.9, phase=np.pi / 4).astype(np.float32)
    _, r = offline_loud(x, fs)
    check(f"fs/4 sine: sample peak {db(0.9 * np.cos(np.pi / 4)):.2f} dBFS @{fs}", abs(r['sp'] - db(0.9 * np.cos(np.pi / 4))) < 0.05, f"{r['sp']:.3f}")
    check(f"fs/4 sine: true peak {db(0.9):.2f} dBTP @{fs}", -0.05 < r['tp'] - db(0.9) < 0.08, f"{r['tp']:.3f}")
# noisy programme: against 8x resampling
nz = lfilter([1, 0.9], [1], rng.standard_normal((48000 * 4, 2)), axis=0); nz = (nz / np.abs(nz).max() * 0.8).astype(np.float32)
out, r = offline_loud(nz, 48000); ref_tp = true_peak_ref(out)
check("noise: true peak matches an 8x oracle", abs(r['tp'] - ref_tp) < 0.1 and r['tp'] >= r['sp'] - 1e-6, f"{r['tp']:.3f} vs {ref_tp:.3f} (sample {r['sp']:.3f})")
out, r = offline_loud(sine(24000.0, 96000, 3, 0.9, phase=np.pi / 4).astype(np.float32), 96000)
check("96 kHz: the inter-sample peak is found too", abs(r['tp'] - db(0.9)) < 0.1 and r['sp'] < db(0.9) - 2.5, f"{r['tp']:.3f} / sample {r['sp']:.3f}")

# 4. the limiter's output is what is measured: an over-full-scale mix is bent below 0 dBFS --------------
hot = (sine(997, 48000, 5, 1.6)).astype(np.float32)        # 1.6 is clipped by nothing but the master limiter
out, r = offline_loud(hot, 48000)
check("measured after the master limiter (peak < 0 dBFS)", r['sp'] < 0.0 and abs(r['sp'] - db(np.abs(out).max())) < 0.01, f"{r['sp']:.3f} {db(np.abs(out).max()):.3f}")

# 5. live master meter ---------------------------------------------------------------------------------
hj = HJ(lib); assert hj.init(48000) == 1; L = hj.l
L.t_loud_get.argtypes = [ctypes.c_void_p]; L.t_loud_reset.argtypes = []
def live_loud():
    o = np.zeros(9); L.t_loud_get(o.ctypes.data); return o
o = live_loud(); check("live: nothing measured yet", o[2] <= -150 and o[6] <= -150, str(o[:3]))
p = os.path.join(tmp, "live.wav"); write_wav(p, prog.astype(np.float32), 48000, 'f32')
hj.playlist(0, [dict(path=p, off=0, dur=len(prog) / 48000, pos=0.0)]); L.t_vol(0, 1.0); L.t_pan(0, 0.5)
L.t_play(0.0); live, _ = hj.run(int(30 * 48000 / FR), FR, sleep_us=250)
o = live_loud(); ref = ref_loudness(live, 48000)
check("live: integrated loudness matches the reference on the captured output", abs(o[2] - ref['integ']) < 0.05, f"{o[2]:.3f} vs {ref['integ']:.3f}")
check("live: true peak never below the sample peak, and matches the oracle", o[6] >= o[7] - 1e-6 and abs(o[6] - true_peak_ref(live)) < 0.1, f"{o[6]:.3f} vs {true_peak_ref(live):.3f}")
check("live: momentary max matches", abs(o[4] - ref['mom_max']) < 0.05, f"{o[4]:.3f} vs {ref['mom_max']:.3f}")
L.t_loud_reset(); hj.run(2, FR)
o = live_loud(); check("reset clears the readings", o[2] <= -150 and o[6] <= -150 and o[8] < 0.1, str(o))
L.t_seek.argtypes = [ctypes.c_double]; L.t_seek(18.0)
seg_live, _ = hj.run(int(8 * 48000 / FR), FR, sleep_us=250)
o = live_loud(); ref = ref_loudness(seg_live, 48000)
check("after a reset only the new material is measured", abs(o[2] - ref['integ']) < 0.25 and 5 < o[8] < 12, f"{o[2]:.3f} vs {ref['integ']:.3f}, {o[8]:.1f}s")
L.t_stop(); hj.run(10, FR); L.t_shutdown()

print("RESULT", "PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
