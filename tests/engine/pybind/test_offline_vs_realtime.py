"""Offline processing (process_buffer, used by the AI racks and batch chains) must sound
exactly like realtime playback: the same effect chain run over the same audio has to
produce the same samples, whatever block size each path happens to use."""
import sys, os, glob, ctypes, tempfile, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from hj import write_wav
modpath = glob.glob(os.path.join(sys.argv[1], 'hijacker_engine*'))[0]
sys.path.insert(0, os.path.dirname(modpath))
import hijacker_engine as engine
lib = ctypes.CDLL(modpath); lib.fake_run.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_ulong]

SR = 48000; FR = 480; CH = 0
FX_GAIN, FX_COMP_SINGLE, FX_EQ_PARAM, FX_REVERB_PARAM, FX_GATE_PARAM, FX_DELAY_PARAM = (
    engine.FX_GAIN, engine.FX_COMP_SINGLE, engine.FX_EQ_PARAM, engine.FX_REVERB_PARAM, engine.FX_GATE_PARAM, engine.FX_DELAY_PARAM)
def pad(p): return list(p) + [0.0] * (24 - len(p))

# Test material: bursts of tones and noise with 0.2 s of silence at both ends, so the
# short fades the player puts on the first and last samples of a strip (which the
# offline path has no equivalent of) cannot make the two paths differ.
rng = np.random.default_rng(11)
N = SR * 4
t = np.arange(N) / SR
env = (0.35 + 0.65 * (np.sin(2 * np.pi * 1.7 * t) > 0)).astype(np.float64)
x = np.stack([0.10 * np.sin(2 * np.pi * 220 * t) + 0.05 * np.sin(2 * np.pi * 3300 * t) + 0.03 * rng.standard_normal(N),
              0.09 * np.sin(2 * np.pi * 330 * t) + 0.05 * np.sin(2 * np.pi * 2100 * t) + 0.03 * rng.standard_normal(N)], 1) * env[:, None]
x[:int(0.2 * SR)] = 0.0
x[-int(0.2 * SR):] = 0.0
x = x.astype(np.float32)
tmp = tempfile.mkdtemp(); wav = os.path.join(tmp, 'material.wav'); write_wav(wav, x, SR, 'f32')

assert engine.engine_init(SR)
hj = engine.get_engine(); hj.set_latency_compensation(0.0)
seg = engine.Segment(); seg.filepath = wav; seg.file_offset_s = 0.0; seg.duration_s = N / SR; seg.timeline_pos_s = 0.0; seg.volume = 1.0

def realtime():
    hj.clear_all_channels(); hj.set_channel_playlist(CH, [seg]); hj.set_volume(CH, 1.0); hj.set_pan(CH, 0.5)
    hj.play(0.0)
    cbs = N // FR + 40
    out = np.zeros(cbs * FR * 2, dtype=np.float32); ph = []
    for i in range(cbs):
        lib.fake_run(out[i * FR * 2:].ctypes.data_as(ctypes.POINTER(ctypes.c_float)), FR)
        ph.append(hj.get_playhead_s()); time.sleep(0.0006)
    hj.stop()
    for _ in range(20): lib.fake_run(out[:FR * 2].ctypes.data_as(ctypes.POINTER(ctypes.c_float)), FR)
    k0 = next(i for i, p in enumerate(ph) if p > 0.0) if ph[-1] > 0 else 0
    start = k0 * FR
    return out.reshape(-1, 2)[start:start + N]

def clear_fx():
    for s in range(8): hj.clear_effect_slot(CH, s)

chains = {
  'EQ':                 [(FX_EQ_PARAM, pad([0.70, 0.5, 0.35, 0.5, 0.60, 0.5, 0.40] + [0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 0.9] + [0.5] * 7))],
  'Compressor':         [(FX_COMP_SINGLE, pad([0.45, 0.35, 0.10, 0.30, 0.15, 0.30]))],
  'Noise gate':         [(FX_GATE_PARAM, pad([0.50, 0.05, 0.12, 0.09, 0.0]))],
  'Reverb':             [(FX_REVERB_PARAM, pad([0.5, 0.4, 0.3, 0.02, 0.8]))],
  'Delay':              [(FX_DELAY_PARAM, pad([0.10, 0.4, 0.3, 0.0, 0.8]))],
  'EQ+Comp+Reverb+Delay': [(FX_EQ_PARAM, pad([0.60, 0.5, 0.40, 0.5, 0.55, 0.5, 0.45] + [0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 0.9] + [0.5] * 7)),
                           (FX_COMP_SINGLE, pad([0.45, 0.35, 0.10, 0.30, 0.15, 0.30])),
                           (FX_REVERB_PARAM, pad([0.5, 0.4, 0.25, 0.02, 0.8])),
                           (FX_DELAY_PARAM, pad([0.10, 0.3, 0.2, 0.0, 0.8]))],
}
FAILS = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + extra)
    if not cond: FAILS.append(name)

clear_fx()
rt0 = realtime()
e0 = np.max(np.abs(rt0 - x))
check("no effects: realtime playback reproduces the file", e0 < 2e-5, f"  (max error {e0:.1e})")

for name, chain in chains.items():
    clear_fx()
    for slot, (typ, params) in enumerate(chain): hj.set_effect_slot(CH, slot, typ, params)
    rt = realtime()
    off = engine.process_buffer(CH, np.ascontiguousarray(x), SR)
    err = rt - off
    rms_sig = np.sqrt(np.mean(off ** 2)); rms_err = np.sqrt(np.mean(err ** 2))
    rel_db = 20 * np.log10(max(rms_err, 1e-12) / rms_sig)
    changed = np.sqrt(np.mean((off - x) ** 2)) / max(np.sqrt(np.mean(x ** 2)), 1e-12)
    check(f"{name:22s} offline == realtime", rel_db < -90 and changed > 0.01 and np.max(np.abs(rt)) < 0.95,
          f"  (difference {rel_db:6.1f} dB below the signal, effect changed the audio by {changed*100:.0f}%, peak {np.max(np.abs(rt)):.2f})")
clear_fx(); engine.engine_shutdown()
print("RESULT", "PASS" if not FAILS else "FAIL " + str(FAILS)); sys.exit(0 if not FAILS else 1)
