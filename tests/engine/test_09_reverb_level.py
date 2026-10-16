"""The reverb must not change the overall level much: a steady noise signal through
any of the factory presets should come out within a few dB of the dry level (the
original engine was 12-21 dB louder, which slammed into the limiter)."""
import sys, os, numpy as np, tempfile
sys.path.insert(0, os.path.dirname(__file__)); from hj import *
lib = sys.argv[1]; SR = 48000; FR = 480; FX_REVERB_PARAM = 7
rng = np.random.default_rng(5)
def pink(x):
    X = np.fft.rfft(x); f = np.arange(len(X)); f[0] = 1; X /= np.sqrt(f); y = np.fft.irfft(X, len(x)); return y / np.sqrt(np.mean(y ** 2))
n = SR * 8
sig = np.stack([pink(rng.standard_normal(n)), pink(rng.standard_normal(n))], 1) * 0.1   # about -20 dBFS rms
tmp = tempfile.mkdtemp(); p = os.path.join(tmp, 'noise.wav'); write_wav(p, sig, SR, 'f32')
presets = {'Bathroom Tiles': [0.2, 0.1, 0.3, 0.0, 0.6], 'Medium Room': [0.45, 0.35, 0.3, 0.02, 0.8],
           'Concert Hall': [0.78, 0.55, 0.38, 0.06, 0.9], 'Cathedral': [0.9, 0.3, 0.45, 0.1, 0.95],
           'Long Ambient': [0.92, 0.65, 0.5, 0.12, 1.0], 'Dry Ambience': [0.15, 0.7, 0.15, 0.0, 0.5]}
def measure(params):
    hj = HJ(lib); assert hj.init(SR) == 1
    hj.playlist(0, [dict(path=p, off=0, dur=7.5, pos=0.0)])
    if params is not None: hj.fx(0, 0, FX_REVERB_PARAM, params + [0.0] * 19)
    hj.l.t_play(0.0); out, _ = hj.run(int(6 * SR / FR), FR, sleep_us=300)
    seg = out[2 * SR:]; r = np.sqrt(np.mean(seg ** 2)); pk = np.abs(seg).max()
    hj.l.t_shutdown(); return r, pk
dry, _ = measure(None); ok = True
for name, pr in presets.items():
    r, pk = measure(pr); g = 20 * np.log10(r / dry)
    good = -4.0 < g < 2.0 and pk < 0.95
    ok &= good
    print(f"  {name:16s} level vs dry {g:+5.1f} dB   peak {20 * np.log10(pk):6.1f} dBFS   {'ok' if good else 'TOO LOUD/QUIET'}")
print("RESULT", "PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
