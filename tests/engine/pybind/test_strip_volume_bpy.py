"""Keyframed strip volume, end to end: real Blender Python API (the `bpy` module), the add-on's real
playlist builder and fader code, and the real compiled engine module.

  * keyframes on a strip's volume are heard (offline render == tone x F-curve),
  * the desk fader scales the keyframes too and gives the user's curve back exactly when it returns,
  * the fader never destroys the user's strip values (quiet strips, zero keyframes, silence),
  * plain strips are unaffected.

Needs `pip install bpy` (Blender as a Python module, Python 3.11); the test is skipped without it."""
import sys, os, glob, types, tempfile
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
try:
    import bpy
except Exception as e:
    print("SKIP  (bpy not installed: pip install bpy)"); print("RESULT PASS"); sys.exit(0)
sys.path.insert(0, os.path.join(HERE, '..')); from hj import write_wav
modpath = glob.glob(os.path.join(sys.argv[1], 'hijacker_engine*'))[0]; sys.path.insert(0, os.path.dirname(modpath))
import hijacker_engine as engine
FAILS = []
def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond: FAILS.append(name)

sys.modules["aud"] = types.ModuleType("aud")                       # only used to decode non-WAV files
sys.path.insert(0, os.path.join(ROOT, "blender_sync"))
import core
eng_stub = types.ModuleType("core.engine"); eng_stub.get_engine = lambda: engine; sys.modules["core.engine"] = eng_stub
import core.properties as props
props.register_properties()
import core.audio as audio
import core.strip_volume as sv

SR = 48000; FPS = 24
tmp = tempfile.mkdtemp()
t = np.arange(SR * 10) / SR; tone = 0.5 * np.sin(2 * np.pi * 441.0 * t)
wav = os.path.join(tmp, "tone.wav"); write_wav(wav, np.stack([tone, tone], 1).astype(np.float32), SR, 'f32')

scene = bpy.context.scene
scene.render.fps = FPS; scene.render.fps_base = 1.0
scene.frame_start = 1; scene.frame_end = 240
scene.sequence_editor_create()
seq = scene.sequence_editor
for _ in range(4): scene.pb_sync_tracks.add()
from core import vse_compat
strips = vse_compat.get_strips_collection(seq)
A = strips.new_sound("A", wav, 1, 1)       # channel 1: keyframed (hold, then fade out over 2 s)
B = strips.new_sound("B", wav, 2, 1)       # channel 2: plain, quiet value
C = strips.new_sound("C", wav, 3, 1)       # channel 3: plain, exactly zero
for f, v in ((1, 1.0), (49, 1.0), (97, 0.0), (121, 0.6), (180, 0.6)):
    A.volume = v; A.keyframe_insert("volume", frame=f)
A.volume = 1.0
B.volume = 0.0004
C.volume = 0.0

fc = sv.volume_fcurve(A)
check("the keyframed volume is found (and plain strips are not)", fc is not None and sv.volume_fcurve(B) is None and not sv.is_animated(C))
orig_keys = [(k.co[0], k.co[1]) for k in fc.keyframe_points]

def playlist(ch):
    return audio._hj_build_segment_playlist(ch, scene)
def curve(f):                      # the user's curve (before fader / gain), evaluated by Blender
    return max(0.0, sv.volume_fcurve(A).evaluate(f))
def render_channel(ch, secs):
    hj = engine.get_engine()
    hj.clear_all_channels()
    hj.set_channel_playlist(ch, audio._make_engine_segments(engine, playlist(ch)))
    hj.set_volume(ch, audio._pb_channel_volume(ch)); hj.set_pan(ch, 0.5); hj.set_mute(ch, False); hj.set_solo(ch, False)
    hj.set_master_gain(1.0)
    assert hj.offline_begin(0.0, SR, [ch], True, False)
    out = hj.offline_render(int(secs * SR)); hj.offline_end(); return out

assert engine.engine_init(SR); engine.get_engine().set_latency_compensation(0.0)

# ---- 1. playlist: a chain of ramps that follows the curve ------------------------------------------------
segs = playlist(0)
check("keyframed strip becomes a chain of ramp segments", len(segs) > 3 and all('volume_end' in s for s in segs), f"{len(segs)} segments")
contig = all(abs(a['timeline_pos_s'] + a['duration_s'] - b['timeline_pos_s']) < 1e-6 and
             abs(a['file_offset_s'] + a['duration_s'] - b['file_offset_s']) < 1e-6 and abs(a['volume_end'] - b['volume']) < 1e-6
             for a, b in zip(segs, segs[1:]))
check("pieces are contiguous in time and file, and the gain carries on across each join", contig)
check("they cover the whole strip", abs(segs[0]['timeline_pos_s'] - 1 / FPS) < 1e-6 and abs(segs[-1]['timeline_pos_s'] + segs[-1]['duration_s'] - (A.frame_final_end) / FPS) < 1e-6,
      f"{segs[0]['timeline_pos_s']:.4f}..{segs[-1]['timeline_pos_s'] + segs[-1]['duration_s']:.4f}")
check("plain strips stay a single constant segment", len(playlist(1)) == 1 and 'volume_end' not in playlist(1)[0])

# ---- 2. the engine plays the curve -------------------------------------------------------------------------
out = render_channel(0, 8.0)
n = len(out)
# strip A starts at frame 1 (t = 1/24 s); gain at sample i is the curve at frame 1 + (i / SR - 1/24) * 24 ... but the strip itself starts there
t0 = int(round(1 / FPS * SR))
fr = 1 + (np.arange(n - t0) / SR) * FPS
step = 16; xs = fr[::step]; ys = np.array([curve(f) for f in xs]); g = np.interp(fr, xs, ys)
exp = np.zeros(n); exp[t0:] = tone[:n - t0] * g
m = slice(t0 + 400, n - 400)
err = np.abs(out[m, 0] - exp[m]).max()
check("playback = tone x the keyframed curve (within the 1 % simplification)", err < 0.012, f"max error {err:.4f} (full scale {0.5})")
z = int(97 / FPS * SR); check("the curve reaches silence at its zero keyframe (frame 97)", np.abs(out[z - 100:z + 100, 0]).max() < 0.01)

# ---- 3. the fader multiplies the keyframes and gives the user's values back --------------------------------------
tr = scene.pb_sync_tracks
tr[0].volume = 1.0
def move_fader(ch, new):
    old = tr[ch].volume; audio.apply_fader_to_channel(ch, old, new); tr[ch].volume = new
move_fader(0, 0.5)
fc = sv.volume_fcurve(A)
check("fader 0.5 halves every keyframe", all(abs(k.co[1] - o[1] * 0.5) < 1e-6 for k, o in zip(fc.keyframe_points, orig_keys)))
out_half = render_channel(0, 8.0)
err = np.abs(out_half[m, 0] - 0.5 * exp[m]).max()
check("and playback is the user's curve x 0.5 (not doubled, not lost)", err < 0.008, f"{err:.4f}")
move_fader(0, 0.2); move_fader(0, 1.0)
fc = sv.volume_fcurve(A)
check("fader back to 1.0: the user's keyframes are restored", all(abs(k.co[1] - o[1]) < 1e-5 for k, o in zip(fc.keyframe_points, orig_keys)),
      str([round(k.co[1], 6) for k in fc.keyframe_points]))
move_fader(0, 0.001); move_fader(0, 0.7); move_fader(0, 1.0)         # all the way to the fader floor and back
fc = sv.volume_fcurve(A)
check("even after a trip to the fader floor (0.001) nothing is lost", all(abs(k.co[1] - o[1]) < 1e-5 for k, o in zip(fc.keyframe_points, orig_keys)))
check("a zero keyframe stays exactly zero", [k.co[1] for k in fc.keyframe_points][2] == 0.0)
gain_before = [k.co[1] for k in fc.keyframe_points]
audio.apply_gain_to_channel(0, 1.0, 2.0); tr[0].gain = 2.0
check("gain knob scales the keyframes too", all(abs(k.co[1] - o[1] * 2.0) < 1e-5 for k, o in zip(sv.volume_fcurve(A).keyframe_points, orig_keys)))
out_g = render_channel(0, 8.0)         # engine = seg(curve*2/ (fader*gain)) x (fader*gain) -> curve x 2
check("gain 2.0 plays 2x the curve", np.abs(out_g[m, 0] - 2.0 * exp[m]).max() < 0.02)
audio.apply_gain_to_channel(0, 2.0, 1.0); tr[0].gain = 1.0

# ---- 4. the fader does not destroy plain strips either ---------------------------------------------------------------
move_fader(1, 0.1); move_fader(1, 1.0)
check("a very quiet strip (0.0004) survives fader moves", abs(B.volume - 0.0004) < 1e-8, f"{B.volume:.8f}")
move_fader(2, 0.3); move_fader(2, 1.0)
check("a strip set to exactly 0 stays 0", C.volume == 0.0, f"{C.volume}")
move_fader(1, 0.5)
check("plain strips scale by the fader", abs(B.volume - 0.0002) < 1e-8)
move_fader(1, 1.0)

# ---- 5. a strip trimmed by the sequence range only uses the audible part of the curve --------------------------------
scene.frame_start = 60; scene.frame_end = 150
segs = playlist(0)
check("sequence range clamps the chain", abs(segs[0]['timeline_pos_s'] - 60 / FPS) < 1e-6 and abs(segs[-1]['timeline_pos_s'] + segs[-1]['duration_s'] - 151 / FPS) < 1e-6,
      f"{segs[0]['timeline_pos_s'] * FPS:.2f}..{(segs[-1]['timeline_pos_s'] + segs[-1]['duration_s']) * FPS:.2f}")
check("and starts at the curve's value at that frame", abs(segs[0]['volume'] - curve(60)) < 1e-4, f"{segs[0]['volume']:.4f} vs {curve(60):.4f}")
scene.frame_start = 1; scene.frame_end = 240

# ---- 6. mute/removed animation falls back to the static volume ------------------------------------------------------------
fc = sv.volume_fcurve(A); fc.mute = True
check("a muted F-curve is ignored", sv.volume_fcurve(A) is None)
fc.mute = False

engine.engine_shutdown()
print("RESULT", "PASS" if not FAILS else "FAIL " + str(FAILS)); sys.exit(0 if not FAILS else 1)
