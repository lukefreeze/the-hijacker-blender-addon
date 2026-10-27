"""Volume automation on segments: a segment can carry a straight-line gain change
(volume -> volume_end). A keyframed strip is sent to the engine as a chain of these.
Checks the gain follows the line exactly, chains join without clicks or dips, it works
through the sample-rate converter, in live playback as well as offline, and that
segments without volume_end behave exactly as before."""
import sys, os, numpy as np, tempfile, ctypes
sys.path.insert(0, os.path.dirname(__file__)); from hj import *
lib = sys.argv[1]; FR = 480
tmp = tempfile.mkdtemp(); ok = True
def check(name, cond, detail=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail else "")); ok &= bool(cond)

def tone_file(name, sr, secs, amp=0.5, f=441.0):
    t = np.arange(int(secs * sr)) / sr; s = amp * np.sin(2 * np.pi * f * t); p = os.path.join(tmp, name)
    write_wav(p, np.stack([s, s], 1).astype(np.float32), sr, 'f32'); return p, s
def put(hj, ch, segs):
    n = len(segs); L = hj.l
    L.t_playlist_ramp.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double),
                                  ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float)]
    L.t_playlist_ramp(ch, n, (ctypes.c_char_p * n)(*[s['path'].encode() for s in segs]), (ctypes.c_double * n)(*[s['off'] for s in segs]),
                      (ctypes.c_double * n)(*[s['dur'] for s in segs]), (ctypes.c_double * n)(*[s['pos'] for s in segs]),
                      (ctypes.c_float * n)(*[s['v0'] for s in segs]), (ctypes.c_float * n)(*[s.get('v1', -1.0) for s in segs]))
def offline(hj, rate, secs):
    L = hj.l
    L.t_off_begin.argtypes = [ctypes.c_double, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_int]; L.t_off_render.argtypes = [ctypes.c_void_p, ctypes.c_int]
    assert L.t_off_begin(0.0, rate, 1, 1, 1) == 1
    n = int(secs * rate); out = np.zeros((n, 2), dtype=np.float32); L.t_off_render(out.ctypes.data, n); L.t_off_end(); return out
def new(rate=48000):
    hj = HJ(lib); assert hj.init(rate) == 1; hj.l.t_vol(0, 1.0); hj.l.t_pan(0, 0.5); return hj

SR = 48000
p48, s48 = tone_file("t48.wav", 48000, 6.0)
# 1. one ramp, 0.2 -> 1.0 over 4 s ---------------------------------------------------------------------
hj = new(); put(hj, 0, [dict(path=p48, off=0, dur=4.0, pos=0.5, v0=0.2, v1=1.0)])
out = offline(hj, SR, 5.0); hj.l.t_shutdown()
n0 = int(0.5 * SR); L_ = int(4.0 * SR); rel = np.arange(L_)
g = 0.2 + 0.8 * rel / L_; src = s48[:L_]
exp = src * g
got = out[n0:n0 + L_, 0]
m = slice(300, L_ - 300)             # skip the declick / edge fades at the ends
check("linear ramp: output = tone x straight-line gain", np.abs(got[m] - exp[m]).max() < 2e-6, f"{np.abs(got[m] - exp[m]).max():.1e}")
check("ramp: starts at 0.2 and ends near 1.0", abs(np.abs(got[300:900]).max() / 0.5 - (0.2 + 0.8 * 600 / L_)) < 0.01 and abs(np.abs(got[-900:-300]).max() / 0.5 - 1.0) < 0.02)
check("outside the segment: silence", np.abs(out[:n0 - 5]).max() == 0 and np.abs(out[n0 + L_ + 5:]).max() == 0)

# 2. a chain of ramps (what a keyframed strip becomes): up, hold, down, to silence -------------------
knots = [(0.0, 0.0), (1.0, 1.0), (2.5, 1.0), (3.0, 0.25), (4.5, 0.0)]      # (time s, gain)
segs = [dict(path=p48, off=a, dur=b - a, pos=a, v0=ga, v1=gb) for (a, ga), (b, gb) in zip(knots, knots[1:])]
hj = new(); put(hj, 0, segs); out = offline(hj, SR, 5.0); hj.l.t_shutdown()
t = np.arange(int(4.5 * SR)) / SR; gain = np.interp(t, [k[0] for k in knots], [k[1] for k in knots]); exp = s48[:len(t)] * gain
m = slice(300, len(t) - 300)
check("chained ramps follow the polyline everywhere (no dip or click at the joins)", np.abs(out[:len(t), 0][m] - exp[m]).max() < 3e-6, f"{np.abs(out[:len(t), 0][m] - exp[m]).max():.1e}")
for j in (1.0, 2.5, 3.0):
    k = int(j * SR); w = out[k - 3:k + 3, 0]; e = exp[k - 3:k + 3]
    check(f"join at {j}s is seamless", np.abs(w - e).max() < 3e-6, f"{np.abs(w - e).max():.1e}")

# 3. a join that does NOT continue the gain keeps its short fade (no click on a step) -------------------
p437, s437 = tone_file("t437.wav", 48000, 3.0, f=437.3)       # not zero at the join, so a click would show
segs = [dict(path=p437, off=0, dur=1.0, pos=0.0, v0=1.0, v1=1.0), dict(path=p437, off=1.0, dur=1.0, pos=1.0, v0=0.2, v1=0.2)]
hj = new(); put(hj, 0, segs); out = offline(hj, SR, 2.0); hj.l.t_shutdown()
k = SR
check("a gain step is faded at the join, not clicked", abs(s437[k]) > 0.3 and abs(out[k - 1, 0]) < 0.02 and abs(out[k, 0]) < 0.02, f"tone {s437[k]:.3f}, out {out[k-1,0]:.3f} {out[k,0]:.3f}")

# 4. through the sample-rate converter (44.1 kHz file on a 48 kHz engine) ------------------------------
p44, _ = tone_file("t44.wav", 44100, 6.0)
hj = new(); put(hj, 0, [dict(path=p44, off=0, dur=4.0, pos=0.0, v0=1.0, v1=0.25)])
out = offline(hj, SR, 4.0); hj.l.t_shutdown()
worst = 0.0
for c in (0.5, 1.5, 2.5, 3.5):
    a = int((c - 0.05) * SR); b = int((c + 0.05) * SR)
    rms = np.sqrt((out[a:b, 0].astype(np.float64) ** 2).mean()) / (0.5 / np.sqrt(2)); want = 1.0 - 0.75 * c / 4.0
    worst = max(worst, abs(rms - want))
check("resampled file follows the ramp", worst < 0.01, f"worst {worst:.4f}")

# 5. live playback gives the same result as the offline render -----------------------------------------
segs = [dict(path=p48, off=0, dur=2.0, pos=0.0, v0=0.0, v1=1.0), dict(path=p48, off=2.0, dur=2.0, pos=2.0, v0=1.0, v1=0.5)]
hj = new(); put(hj, 0, segs); off = offline(hj, SR, 4.0)
hj.l.t_play(0.0); live, _ = hj.run(int(4.2 * SR / FR), FR, sleep_us=300); hj.l.t_stop(); hj.run(10, FR); hj.l.t_shutdown()
nz = np.nonzero(np.abs(live).max(1) > 1e-7)[0][0]; nzo = np.nonzero(np.abs(off).max(1) > 1e-7)[0][0]
a = live[nz + 2000:nz + 150000]; b = off[nzo + 2000:nzo + 150000]
check("live playback of a ramp matches the offline render", np.abs(a - b).max() < 1e-6, f"{np.abs(a - b).max():.1e}")

# 6. no volume_end (or a bad one) = constant gain, exactly as before ------------------------------------
for label, v1 in (("unset", -1.0), ("NaN", float('nan')), ("negative", -3.0)):
    hj = new(); put(hj, 0, [dict(path=p48, off=0, dur=2.0, pos=0.0, v0=0.6, v1=v1)]); out = offline(hj, SR, 2.0); hj.l.t_shutdown()
    exp = s48[:2 * SR] * 0.6; m = slice(300, 2 * SR - 300)
    check(f"volume_end {label}: constant gain", np.abs(out[m, 0] - exp[m]).max() < 2e-6)

print("RESULT", "PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
