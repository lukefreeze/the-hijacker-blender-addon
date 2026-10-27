"""
packApunch mixer-strip knobs, taken from the art.

strip_top_bg.png / strip_bottom_bg.png have the knob caps painted in exactly as in the artwork, with each cap's
painted pointer line filled in.  strip_knob_faces.png is a small sheet with the 45 knob TOPS (gain, EQ high / mid /
low and pan, for each of the 9 strips) cut out of the original art, each with its painted pointer; the live knob
is that top turned to the track's value, drawn over the cap.  The side wall, rim and shadow stay still.

strip_knobs.json  (cell = sprite cell size in the sheet; for every knob: cx, cy = centre of the knob top in art px,
y measured from the top of the image; line = angle the painted pointer points at, degrees clockwise from up).

Tuning (module constants):
  SWEEP_DEG    knob travel (min = lower left, max = lower right)
  DX / DY      nudge per knob type (art px, +x right, +y down) if a row sits a little off
"""
import os, json, math

SWEEP_DEG = 270.0
DX = {"gain": 0.0, "high": 0.0, "mid": 0.0, "low": 0.0, "pan": 0.0}
DY = {"gain": 0.0, "high": 0.0, "mid": 0.0, "low": 0.0, "pan": 0.0}
TYPES = ("gain", "high", "mid", "low", "pan")

_DATA = None


def get_data():
    """The knob geometry (cached), or False when the file is missing."""
    global _DATA
    if _DATA is not None:
        return _DATA
    _DATA = False
    try:
        from ui.mixer import texture_cache as tc
        with open(os.path.join(tc._skin_dir, "strip_knobs.json"), "r", encoding="utf-8") as f:
            _DATA = json.load(f)
    except Exception as e:
        print(f"[KNOBS] data not available: {e}")
    return _DATA


def art_knobs_active():
    """True when the strip knobs come from the art (packApunch skin, data file and knob sheet present)."""
    try:
        from ui.mixer.texture_cache import get_active_skin, get_texture
        return get_active_skin() == "packapunch" and bool(get_data()) and get_texture("strip_knob_faces") is not None
    except Exception:
        return False


def draw_group_knobs(top_rect, bot_rect, tracks, first, count):
    """Draw the live knob tops of one group of strips.
    top_rect / bot_rect: (x, y_bottom, w, h) on screen of strip_top_bg / strip_bottom_bg.
    tracks[first : first + count] are the group's tracks."""
    d = get_data()
    if not d:
        return
    from ui.mixer.texture_cache import get_texture, blit_texture_rotated
    from core.constants import GAIN_MIN, GAIN_MAX
    tex = get_texture("strip_knob_faces")
    if tex is None:
        return
    CELL = d["cell"]
    AW, AH = d["atlas"]
    try:
        for ri, kind in enumerate(TYPES):
            rect, art = (top_rect, d["top_art"]) if kind == "gain" else (bot_rect, d["bottom_art"])
            x, y, w, h = rect
            W, H = art
            sx, sy = w / W, h / H
            size = CELL * sy
            for k in range(count):
                tr = tracks[first + k]
                if kind == "gain":
                    v = (tr.gain - GAIN_MIN) / (GAIN_MAX - GAIN_MIN)
                elif kind == "high":
                    v = (tr.eq_high + 24) / 48
                elif kind == "mid":
                    v = (tr.eq_mid + 24) / 48
                elif kind == "low":
                    v = (tr.eq_low + 24) / 48
                else:
                    v = getattr(tr, "pan", 0.5)
                v = max(0.0, min(1.0, float(v)))
                kn = d[kind]["knobs"][k]
                cx = x + (kn["cx"] + 0.5 + DX[kind]) * sx
                cy = y + (H - (kn["cy"] + 0.5 + DY[kind])) * sy
                # turn so the painted pointer points at -135 deg (lower left) at minimum and +135 deg at maximum
                ang = math.radians((0.5 - v) * SWEEP_DEG + kn["line"])
                u0, u1 = k * CELL / AW, (k + 1) * CELL / AW
                v1, v0 = 1.0 - ri * CELL / AH, 1.0 - (ri + 1) * CELL / AH
                blit_texture_rotated(tex, cx, cy, size, size, ang, blend="ALPHA_PREMULT",
                                     uv=(u0, v0, u1, v1), key="strip_knob_faces")
    except Exception as e:
        print(f"[KNOBS] draw error: {e}")
