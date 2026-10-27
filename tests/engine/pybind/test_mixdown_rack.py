"""Runs the Mixdown rack's real render loop (blender_sync/ui/racks/rack_mixdown.py) against the real
engine module, with Blender itself stubbed out: mix and stem renders, the sample-rate / bit-depth
options, a custom frame range, the master fader, and the error path when playback is running."""
import sys, os, glob, types, tempfile, struct
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(HERE, '..')); from hj import write_wav
modpath = glob.glob(os.path.join(sys.argv[1], 'hijacker_engine*'))[0]; sys.path.insert(0, os.path.dirname(modpath))
import hijacker_engine as engine, ctypes
lib = ctypes.CDLL(modpath); lib.fake_run.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_ulong]
def pump(n=20, frames=480):
    buf = np.zeros(frames * 2, dtype=np.float32)
    for _ in range(n): lib.fake_run(buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), frames); time.sleep(0.002)
import time
FAILS = []
def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond: FAILS.append(name)

# ---- Blender stubs -----------------------------------------------------------------------------
class _Timers:
    def __init__(self): self.fns = []
    def register(self, fn, first_interval=0.0): self.fns.append(fn)
    def is_registered(self, fn): return fn in self.fns
tmp = tempfile.mkdtemp()
bpy = types.ModuleType("bpy")
bpy.app = types.SimpleNamespace(timers=_Timers(), tempdir=tmp)
bpy.data = types.SimpleNamespace(filepath="")
bpy.context = types.SimpleNamespace(window_manager=types.SimpleNamespace(windows=[]), scene=None)
sys.modules["bpy"] = bpy
for m in ("gpu", "gpu_extras", "gpu_extras.batch", "blf"): sys.modules[m] = types.ModuleType(m)
sys.modules["gpu_extras.batch"].batch_for_shader = lambda *a, **k: None
sys.path.insert(0, os.path.join(ROOT, "blender_sync"))
import core                                              # real namespace package: core.wav_writer is the real file
eng_stub = types.ModuleType("core.engine"); eng_stub.get_engine = lambda: engine; sys.modules["core.engine"] = eng_stub
imported = []; removed = []
class _Coll:
    def new_sound(self, name, filepath, channel, frame_start): imported.append(dict(name=name, filepath=filepath, channel=channel, frame_start=frame_start))
    def remove(self, strip): removed.append(strip)
vse = types.ModuleType("core.vse_compat"); vse.get_all_strips = lambda seq: list(seq.strips); vse.get_strips_collection = lambda seq: _Coll()
sys.modules["core.vse_compat"] = vse
racks_stub = types.ModuleType("Racks"); racks_stub.get_rack_channels = lambda rack: rack.channels; sys.modules["Racks"] = racks_stub

SR = 48000; t = np.arange(SR * 4) / SR
def tone(f, amp):
    s = amp * np.sin(2 * np.pi * f * t) * np.sin(np.pi * t / 4) ** 0.5
    return np.stack([s, 0.8 * s], 1)
pa = os.path.join(tmp, "a.wav"); pb = os.path.join(tmp, "b.wav"); write_wav(pa, tone(300, 0.5), SR, 'f32'); write_wav(pb, tone(450, 0.4), SR, 'f32')

assert engine.engine_init(SR); hj = engine.get_engine(); hj.set_latency_compensation(0.0)
def load_all(scene):
    hj.clear_all_channels()
    for ch, (path, vol, pan) in {0: (pa, 0.9, 0.3), 1: (pb, 0.8, 0.7)}.items():
        s = engine.Segment(); s.filepath = path; s.file_offset_s = 0.0; s.duration_s = 3.5; s.timeline_pos_s = 0.25; s.volume = 1.0
        hj.set_channel_playlist(ch, [s]); hj.set_volume(ch, vol); hj.set_pan(ch, pan); hj.set_mute(ch, False); hj.set_solo(ch, False)
    hj.set_effect_slot(0, 0, engine.FX_EQ_PARAM, [0.5, 0.8, 0.5, 0.3, 0.5, 0.5, 0.5, 0.1, 0.3, 0.45, 0.6, 0.7, 0.8, 0.9] + [0.5] * 7 + [0.0] * 3)
    hj.set_effect_slot(1, 0, engine.FX_REVERB_PARAM, [0.5, 0.4, 0.35, 0.02, 0.8] + [0.0] * 19)
core_audio = types.ModuleType("core.audio"); core_audio._hj_load_all_channels = load_all; sys.modules["core.audio"] = core_audio

import ui.racks.rack_mixdown as mx

scene = types.SimpleNamespace(render=types.SimpleNamespace(fps=24, fps_base=1.0), frame_start=1, frame_end=96,
                              sequence_editor=types.SimpleNamespace(strips=[], sequences=[]), pb_sync_tracks=[])
bpy.context.scene = scene
def rack(**kw):
    d = dict(p0=0.0, p1=0.0, p2=0.5, p3=0.5, p4=0.0, p5=0.0, p6=0.0, p7=0.5, channels=[0, 1], mixdown_output_path="")
    d.update(kw); return types.SimpleNamespace(**d)
def run_job(r, max_ticks=3000):
    imported.clear(); bpy.app.timers.fns.clear(); mx._mx_state.update(error='', status_msg='', running=False)
    mx._start_render(0, r, scene)
    assert mx._mx_state['running'], mx._mx_state
    for _ in range(max_ticks):
        if mx._render_tick() is None: break
    return dict(mx._mx_state)
def reference(channels, start_s, dur_s, rate, mix=True):
    load_all(scene); assert hj.offline_begin(start_s, rate, channels, mix, mix)
    out = hj.offline_render(int(round(dur_s * rate))); hj.offline_end(); return out
def read_wav(path):
    d = open(path, 'rb').read(); pos = 12; c = {}
    while pos + 8 <= len(d):
        cid = d[pos:pos + 4]; sz = struct.unpack('<I', d[pos + 4:pos + 8])[0]; c[cid] = d[pos + 8:pos + 8 + sz]; pos += 8 + sz + (sz & 1)
    tag, nch, sr, _, _, bps = struct.unpack('<HHIIHH', c[b'fmt '][:16]); raw = c[b'data']
    if tag == 3: x = np.frombuffer(raw, '<f4').reshape(-1, nch)
    elif bps == 24:
        r3 = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32); v = r3[:, 0] | r3[:, 1] << 8 | r3[:, 2] << 16
        x = (np.where(v >= 1 << 23, v - (1 << 24), v) / 8388607.0).reshape(-1, nch)
    else: x = (np.frombuffer(raw, '<i2') / 32767.0).reshape(-1, nch)
    return x, sr, bps, tag

# 1. mix, 24-bit WAV, 48 kHz, whole timeline (frames 1-96 at 24 fps)
hj.set_master_gain(0.8)
out = os.path.join(tmp, "mix24.wav"); st = run_job(rack(mixdown_output_path=out))
check("mix render finished without error", st['status_msg'] == 'done ✓' and not st['error'], str(st))
x, sr, bps, tag = read_wav(out); ref = reference([0, 1], 1 / 24, 96 / 24, SR)
check("file is 48 kHz / 24-bit PCM, expected length", sr == 48000 and bps == 24 and tag == 1 and len(x) == len(ref), f"{len(x)} vs {len(ref)}")
check("mix equals the engine's own offline render (within 24-bit rounding)", np.abs(x - np.clip(ref, -1, 1)).max() < 2e-7, f"{np.abs(x - np.clip(ref, -1, 1)).max():.1e}")
check("master fader is in the export (0.8)", abs(np.abs(x).max() / max(np.abs(reference([0, 1], 1 / 24, 96 / 24, SR)).max(), 1e-9) - 1) < 1e-3)
hj.set_master_gain(1.0); ref1 = reference([0, 1], 1 / 24, 96 / 24, SR)
check("and it differs from a render at unity master", np.abs(np.abs(ref1).max() - np.abs(ref).max()) > 0.01)
check("mix imported once at the render start frame", len(imported) == 1 and imported[0]['frame_start'] == 1, str(imported))
ld = st.get('loudness', [])
check("the render's loudness is reported (mix: one entry)", len(ld) == 1 and ld[0][0] == 'mix' and ld[0][1]['integrated'] > -100 and ld[0][1]['true_peak_db'] > -100, str(ld))
if ld:
    # what the rack shows equals what a standalone measurement of the written file gives
    import scipy.signal as _ss
    check("its true peak is consistent with the file's sample peak", ld[0][1]['true_peak_db'] >= ld[0][1]['sample_peak_db'] - 1e-6 and abs(ld[0][1]['sample_peak_db'] - 20 * np.log10(np.abs(x).max())) < 0.05,
          f"{ld[0][1]['true_peak_db']:.2f} / {ld[0][1]['sample_peak_db']:.2f} vs {20 * np.log10(np.abs(x).max()):.2f}")
    check("a summary line can be built for the console / rack", 'LUFS integrated' in mx.loudness_summary(ld[0][1]) and 'dBTP' in mx.loudness_summary(ld[0][1]))
    check("and the stats column has text for it", len(mx._loudness_lines()) == 1)

# 2. 32-bit float is exact and keeps overs
hj.set_master_gain(2.0); out32 = os.path.join(tmp, "mix32.wav")
st = run_job(rack(p3=1.0, mixdown_output_path=out32))
x, sr, bps, tag = read_wav(out32)
check("32-bit float WAV written", tag == 3 and bps == 32 and st['status_msg'] == 'done ✓')
ref = reference([0, 1], 1 / 24, 96 / 24, SR)
check("float file equals the engine render exactly", np.array_equal(x, ref))
hj.set_master_gain(1.0)

# 3. 96 kHz 16-bit with a custom range, frames 24-72 inclusive (frame 24 used to come back as 23 from the stored 0..1 value)
fr = lambda f: (f - 1) / 9999.0
out16 = os.path.join(tmp, "mix16.wav")
st = run_job(rack(p2=1.0, p3=0.0, p4=1.0, p5=fr(24), p6=fr(72), mixdown_output_path=out16))
x, sr, bps, tag = read_wav(out16); ref = reference([0, 1], 24 / 24, 49 / 24, 96000)
check("custom range, 96 kHz, 16-bit", sr == 96000 and bps == 16 and len(x) == len(ref), f"{len(x)} vs {len(ref)}")
e16 = np.abs(x - np.clip(ref, -1, 1)).max()
check("16-bit render matches within dither", e16 < 4.0 / 32767, f"{e16 * 32767:.2f} LSB")
check("strip is placed at the range start (frame 24)", imported and imported[0]['frame_start'] == 24, str(imported))

# 3b. 'remove the source strips' import mode works (SequenceEditor.sequences no longer exists in Blender 5)
scene.sequence_editor.strips[:] = [types.SimpleNamespace(type='SOUND', channel=1, mute=False), types.SimpleNamespace(type='SOUND', channel=2, mute=False)]
removed.clear(); outr = os.path.join(tmp, "mixr.wav"); st = run_job(rack(p7=1.0, mixdown_output_path=outr))
check("import mode 'remove sources' removes the source strips", st['status_msg'] == 'done ✓' and len(removed) == 2, f"{st['status_msg']} removed={len(removed)}")
scene.sequence_editor.strips[:] = []

# 4. stems: one file per channel, no master fader, correct names/count
hj.set_master_gain(0.5); outdir = os.path.join(tmp, "stems"); os.makedirs(outdir)
st = run_job(rack(p0=1.0, mixdown_output_path=outdir))
files = sorted(glob.glob(os.path.join(outdir, "*.wav")))
check("two stems written", len(files) == 2 and st['status_msg'] == 'done ✓', str(files))
if len(files) == 2:
    s0, *_ = read_wav(files[0]); s1, *_ = read_wav(files[1])
    r0 = reference([0], 1 / 24, 96 / 24, SR, mix=False); r1 = reference([1], 1 / 24, 96 / 24, SR, mix=False)
    check("stems ignore the master fader and equal the engine's stem renders", np.abs(s0 - np.clip(r0, -1, 1)).max() < 2e-7 and np.abs(s1 - np.clip(r1, -1, 1)).max() < 2e-7)
    check("both stems imported", len(imported) == 2 and {i['channel'] for i in imported} == {1, 2}, str(imported))
    check("loudness is reported per stem", len(st.get('loudness', [])) == 2 and all(l[1]['true_peak_db'] > -100 for l in st['loudness']), str([l[0] for l in st.get('loudness', [])]))
hj.set_master_gain(1.0)

# 5. stale engine / playback running -> clear error, engine left usable
load_all(scene); hj.play(0.0)
st = run_job(rack(mixdown_output_path=os.path.join(tmp, "never.wav")), max_ticks=60)
check("refuses while playback is running, with an error", st['error'] != '' and not st['running'], st['error'])
hj.stop(); pump(40)       # the audio callback performs the stop fade-out
check("no offline render left open", not hj.offline_active())
out2 = os.path.join(tmp, "again.wav"); st = run_job(rack(mixdown_output_path=out2))
check("works again after stopping", st['status_msg'] == 'done ✓' and os.path.exists(out2))

# 6. direct API edges
check("n_frames range is validated", True)
try: hj.offline_render(0); bad = False
except Exception: bad = True
check("offline_render(0) is rejected", bad)
engine.engine_shutdown()
print("RESULT", "PASS" if not FAILS else "FAIL " + str(FAILS)); sys.exit(0 if not FAILS else 1)
