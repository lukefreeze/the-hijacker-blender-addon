# =============================================================================
# core/master.py
# The Hijacker — master bus state: fader value, meter ballistics, clip light.
#
# The engine does the actual work (a smoothed gain after the channel sum, a
# peak accumulator — see HijackerEngine::set_master_gain).
# This module holds the parts Python owns:
#   - the fader position, stored per .blend file as scene.pb_master_db
#   - converting dB <-> linear gain
#   - the meter's visual ballistics (instant attack, smooth fall, peak hold)
#   - the clip light: it FLASHES while the master is hitting 0 dBFS and goes
#     out by itself once it stops (fader lowered, audio stopped) — it is not
#     latched. The decision is made here from each poll's peak reading; the
#     engine's own latch (get_master_clip) is no longer used.
# It has no drawing code; ui/mixer/master_widget.py draws it.
# =============================================================================

import time
import bpy

from core.engine import get_engine

DB_MIN = -60.0           # fader bottom == silence (-inf)
DB_MAX = 6.0             # fader top (engine caps linear gain at 2.0 == +6.02 dB)

FALL_DB_PER_S      = 30.0    # meter bar fall rate
HOLD_TIME_S        = 1.5     # peak-hold tick stays put this long
HOLD_FALL_DB_PER_S = 20.0    # ...then falls at this rate
CLIP_HOLD_S        = 0.30    # keep flashing this long after the last clipped reading, so a single hit is visible
CLIP_FLASH_HZ      = 4.0     # flash rate while clipping (the HUD redraws at ~10-20 fps)

# Visual meter state (read by the widget)
level = 0.0              # linear, smoothed bar
hold  = 0.0              # linear, peak-hold tick
clip  = False            # clip light ON right now (flashes while clipping; never latched)

# Loudness (ITU-R BS.1770 / EBU R128), measured by the engine on exactly what leaves it.
# LUFS / LU / dBTP; NO_READING (-200) means nothing measured yet.
NO_READING = -200.0
loudness = {"momentary": NO_READING, "short_term": NO_READING, "integrated": NO_READING,
            "lra": 0.0, "true_peak_db": NO_READING, "sample_peak_db": NO_READING,
            "duration_s": 0.0}

_hold_t = 0.0
_last_t = None
_clip_start = None       # when the current run of clipping began (flash phase starts "on")
_clip_last  = 0.0        # time of the most recent clipped reading


def db_to_gain(db):
    if db <= DB_MIN + 0.05:
        return 0.0
    return 10.0 ** (db / 20.0)


def gain_to_db(g):
    import math
    return DB_MIN if g <= 1e-4 else max(DB_MIN, 20.0 * math.log10(g))


def get_db(scene=None):
    scene = scene or bpy.context.scene
    return float(getattr(scene, "pb_master_db", 0.0)) if scene else 0.0


def _hj():
    engine = get_engine()
    return engine.get_engine() if engine else None


def engine_outdated():
    """True if an engine is loaded but was compiled before the master bus existed."""
    hj = _hj()
    return bool(hj is not None and not hasattr(hj, "set_master_gain"))


_warned_old = False


def push_to_engine(db=None):
    """Send the fader value to the running engine (no-op if it has no master bus,
    i.e. a compiled engine older than this feature)."""
    global _warned_old
    hj = _hj()
    if hj is None:
        return
    if not hasattr(hj, "set_master_gain"):
        if not _warned_old:
            _warned_old = True
            print("[HIJACKER] the compiled audio engine has no master bus: run build.bat / "
                  "build_py313.bat again and restart Blender (the master fader does nothing until then)")
        return
    if db is None:
        db = get_db()
    try:
        hj.set_master_gain(db_to_gain(db))
    except Exception as e:
        print(f"[HIJACKER] could not set master gain: {e}")


def set_db(db, scene=None):
    scene = scene or bpy.context.scene
    db = max(DB_MIN, min(DB_MAX, float(db)))
    if scene is not None:
        try:
            scene.pb_master_db = db
        except Exception:
            pass
    push_to_engine(db)
    return db


def clear_clip():
    global clip, _clip_start
    clip = False
    _clip_start = None
    hj = _hj()
    if hj is not None and hasattr(hj, "clear_master_clip"):
        try:
            hj.clear_master_clip()
        except Exception:
            pass


def loudness_available():
    """False if the compiled engine predates the loudness meter."""
    hj = _hj()
    return bool(hj is not None and hasattr(hj, "get_loudness"))


def reset_loudness():
    """Start a fresh loudness measurement (integrated loudness, range and peaks)."""
    for k in loudness:
        loudness[k] = 0.0 if k in ("lra", "duration_s") else NO_READING
    hj = _hj()
    if hj is not None and hasattr(hj, "reset_loudness"):
        try:
            hj.reset_loudness()
        except Exception:
            pass


def format_level(v, places=1):
    """Loudness / peak value for display: '--' while there is no reading."""
    return "--" if v <= -150.0 else f"{v:.{places}f}"


def reset_meter():
    global level, hold, clip, _hold_t, _last_t, _clip_start
    level = hold = 0.0
    clip = False
    _clip_start = None
    _hold_t = 0.0
    _last_t = None


def poll(hj):
    """Called from the meter timer: read the engine, apply ballistics."""
    global level, hold, clip, _hold_t, _last_t, _clip_start, _clip_last
    now = time.perf_counter()
    dt = 0.05 if _last_t is None else max(0.0, min(0.5, now - _last_t))
    _last_t = now

    if hj is not None and hasattr(hj, "get_loudness"):
        try:
            loudness.update(hj.get_loudness())
        except Exception:
            pass

    peak = 0.0
    if hj is not None and hasattr(hj, "get_master_peak"):
        try:
            peak = float(hj.get_master_peak())
        except Exception:
            peak = 0.0

    # Clip light: peak is the loudest sample since the previous poll
    # (post-fader, pre-limiter). >= 1.0 means the master is at/over 0 dBFS
    # right now. Flash while that keeps happening, and go dark once it stops
    # (the fader came down, or playback ended and the peak fell to 0).
    if peak >= 1.0:
        if _clip_start is None or (now - _clip_last) > CLIP_HOLD_S:
            _clip_start = now
        _clip_last = now
    if _clip_start is not None and (now - _clip_last) <= CLIP_HOLD_S:
        phase = int((now - _clip_start) * CLIP_FLASH_HZ * 2.0)
        clip = (phase % 2 == 0)
    else:
        _clip_start = None
        clip = False

    if peak >= level:
        level = peak
    else:
        level *= 10.0 ** (-FALL_DB_PER_S * dt / 20.0)
        if level < 1e-4:
            level = 0.0

    if peak >= hold:
        hold = peak
        _hold_t = 0.0
    else:
        _hold_t += dt
        if _hold_t > HOLD_TIME_S:
            hold *= 10.0 ** (-HOLD_FALL_DB_PER_S * dt / 20.0)
            if hold < 1e-4:
                hold = 0.0
