"""The offline renderer (used for mixdowns and stems) must produce what live playback
produces: same fader, pan, effects, mute/solo, resampling, master gain and limiter.
Live output is captured through the fake audio device and compared sample by sample."""
import sys, os, numpy as np, tempfile, ctypes
sys.path.insert(0, os.path.dirname(__file__)); from hj import *
lib = sys.argv[1]; SR = 48000; FR = 480
FX_EQ, FX_REVERB, FX_DELAY = 6, 7, 9
tmp = tempfile.mkdtemp(); ok = True
def check(name, cond, detail=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail else "")); ok &= bool(cond)

def tone(f, sr, secs, amp, lead=0.2, tail=0.3, stereo_off=0.0):
    n = int(secs * sr); t = np.arange(n) / sr
    x = amp * np.sin(2 * np.pi * f * t) * (np.sin(np.pi * t / secs) ** 0.5)
    y = amp * np.sin(2 * np.pi * (f * 1.5) * t + stereo_off) * (np.sin(np.pi * t / secs) ** 0.5)
    sig = np.stack([x, y], 1)
    return np.concatenate([np.zeros((int(lead * sr), 2)), sig, np.zeros((int(tail * sr), 2))])
files = {}
for name, (f, sr, amp) in {'a': (220, 44100, 0.5), 'b': (330, 48000, 0.6), 'c': (440, 96000, 0.7), 'd': (550, 48000, 0.9)}.items():
    p = os.path.join(tmp, name + '.wav'); write_wav(p, tone(f, sr, 2.0, amp), sr, 'f32'); files[name] = p

def setup(hj, solo_ch=None):
    L = hj.l
    # 4 channels with different rates, volume, pan, effects; one muted
    hj.playlist(0, [dict(path=files['a'], off=0, dur=2.7, pos=0.0, vol=0.8)])        # 44.1 kHz -> 48 kHz
    hj.playlist(1, [dict(path=files['b'], off=0, dur=1.2, pos=0.1), dict(path=files['b'], off=0.5, dur=1.0, pos=1.6)])
    hj.playlist(2, [dict(path=files['c'], off=0, dur=2.7, pos=0.0)])                  # 96 kHz -> 48 kHz
    hj.playlist(3, [dict(path=files['d'], off=0, dur=2.7, pos=0.0)])
    L.t_vol(0, 0.9); L.t_pan(0, 0.3)
    L.t_vol(1, 1.0); L.t_pan(1, 0.5)
    L.t_vol(2, 0.7); L.t_pan(2, 0.9)
    L.t_vol(3, 1.0); L.t_pan(3, 0.5); L.t_mute(3, 1)
    hj.fx(0, 0, FX_EQ, [0.5, 0.8, 0.5, 0.3, 0.5, 0.5, 0.5, 0.1, 0.3, 0.45, 0.6, 0.7, 0.8, 0.9, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0, 0, 0])
    hj.fx(1, 0, FX_REVERB, [0.5, 0.4, 0.35, 0.02, 0.8] + [0.0] * 19)
    hj.fx(2, 0, FX_DELAY, [0.2, 0.4, 0.3, 0.5, 0.7] + [0.0] * 19)
    if solo_ch is not None: L.t_solo(solo_ch, 1)

def live_capture(solo_ch=None, master=1.0, secs=3.0):
    hj = HJ(lib); assert hj.init(SR) == 1
    hj.l.t_master.argtypes = [ctypes.c_float]; hj.l.t_master(master)
    setup(hj, solo_ch)
    hj.l.t_play(0.0)
    out, _ = hj.run(int(secs * SR / FR), FR, sleep_us=300)
    hj.l.t_stop(); hj.run(10, FR)
    return hj, out

def offline(hj, secs, channels, rate=SR, respect=1, master=1):
    L = hj.l
    L.t_off_begin.argtypes = [ctypes.c_double, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_int]
    L.t_off_render.argtypes = [ctypes.c_void_p, ctypes.c_int]
    mask = sum(1 << c for c in channels)
    assert L.t_off_begin(0.0, rate, mask, respect, master) == 1
    n = int(secs * rate); out = np.zeros((n, 2), dtype=np.float32)
    # render in odd-sized pieces on purpose: block boundaries must not matter
    pos = 0
    for step in (1, 777, 4096, 1500, 10 ** 9):
        m = min(step, n - pos)
        if m <= 0: break
        L.t_off_render(out[pos:].ctypes.data, m); pos += m
    L.t_off_end(); return out

def compare(name, live, off):
    nzl = np.nonzero(np.abs(live).max(1) > 1e-7)[0]; nzo = np.nonzero(np.abs(off).max(1) > 1e-7)[0]
    a = live[nzl[0]:]; b = off[nzo[0]:]; n = min(len(a), len(b), int(2.5 * SR))
    a = a[:n]; b = b[:n]
    err = np.abs(a - b).max(); sig = np.abs(a).max()
    db = 20 * np.log10(max(err, 1e-12) / sig)
    check(name, err < 1e-6, f"largest difference {err:.2e} ({db:.0f} dB re peak {sig:.2f})")
    return a, b

# 1. everything on, default master
hj, live = live_capture()
check("live capture has audio", np.abs(live).max() > 0.1)
off = offline(hj, 3.0, [0, 1, 2, 3])
compare("mix with fader, pan, EQ, reverb, delay, 44.1/96 kHz sources, a muted channel", live, off)
# offline left the live engine usable and in a clean state
check("offline_active is false afterwards", hj.l.t_off_active() == 0)
hj.l.t_shutdown()

# 2. solo on channel 1
hj, live = live_capture(solo_ch=1)
off = offline(hj, 3.0, [0, 1, 2, 3])
compare("solo respected like live", live, off)
hj.l.t_shutdown()

# 3. master gain + limiter engaged (hot signal)
hj, live = live_capture(master=2.0)
off = offline(hj, 3.0, [0, 1, 2, 3])
a, b = compare("master gain 2.0 and the limiter", live, off)
check("limiter was actually engaged in that test", np.abs(live).max() > 0.95 and np.abs(live).max() < 1.0, f"peak {np.abs(live).max():.4f}")
hj.l.t_shutdown()

# 4. stems: single channel, no master, ignoring mute/solo
hj = HJ(lib); assert hj.init(SR) == 1; setup(hj)
stem3 = offline(hj, 3.0, [3], respect=0, master=0)
check("stem of a muted channel still renders (mute ignored for stems)", np.abs(stem3).max() > 0.5)
stem0 = offline(hj, 3.0, [0], respect=0, master=0)
mix03 = offline(hj, 3.0, [0, 3], respect=0, master=0)
check("stems add up to the mix (no master)", np.abs(stem0 + stem3 - mix03).max() < 1e-5, f"{np.abs(stem0 + stem3 - mix03).max():.1e}")
# a channel with no playlist, or nothing selected, renders silence and does not crash
none = offline(hj, 1.0, [20]); check("empty selection renders silence", np.abs(none).max() == 0.0)

# 5. other sample rates keep pitch and level
r48 = offline(hj, 2.0, [1], respect=0, master=0)
r44 = offline(hj, 2.0 * 44100 / 48000, [1], rate=44100, respect=0, master=0)
r96 = offline(hj, 2.0 * 96000 / 48000, [1], rate=96000, respect=0, master=0)
def rms(x): return np.sqrt(np.mean(x[: int(0.9 * len(x))] ** 2))
def peakfreq(x, rate):
    seg = x[int(0.3 * rate):int(0.9 * rate), 0]; spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))); return np.argmax(spec) * rate / len(seg)
check("44.1 kHz render: same pitch and level", abs(peakfreq(r44, 44100) - peakfreq(r48, 48000)) < 8 and abs(20 * np.log10(rms(r44) / rms(r48))) < 0.2,
      f"{peakfreq(r44, 44100):.0f} Hz vs {peakfreq(r48, 48000):.0f} Hz")
check("96 kHz render: same pitch and level", abs(peakfreq(r96, 96000) - peakfreq(r48, 48000)) < 8 and abs(20 * np.log10(rms(r96) / rms(r48))) < 0.2)

# 6. playback is refused during an offline render, and offline during playback
L = hj.l
L.t_off_begin.argtypes = [ctypes.c_double, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_int]
assert L.t_off_begin(0.0, SR, 1, 1, 1) == 1
check("second offline_begin refused while one is running", L.t_off_begin(0.0, SR, 1, 1, 1) == 0)
L.t_off_end()
L.t_play(0.0); hj.run(5, FR, sleep_us=300)
check("offline_begin refused while playing", L.t_off_begin(0.0, SR, 1, 1, 1) == 0)
L.t_stop(); hj.run(10, FR)
L.t_shutdown(); print("RESULT", "PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
