# =============================================================================
# ui/mixer/master_widget.py
# The Hijacker — the master bus fader + meter + clip light.
#
# Drawn in code (no artwork), pinned to the right-hand edge of the canvas in
# screen space. It does not scroll with the desks, so there is always exactly
# one master however many groups of 9 channels exist, and it does NOT follow
# the UI zoom either: it is always drawn at FIXED_SCALE, so it keeps the same
# on-screen size as you pan and zoom the desks. The small arrow button at the
# top right of the panel collapses it into a thin tab at the edge (click the
# tab to bring it back); the choice is saved in the .blend.
# Geometry lives in _layout() and is shared by draw() and the hit test, so what
# you see is always what you click.
#
# Controls:
#   drag the handle (or click the track)   set the level
#   double-click the fader or the readout  back to 0 dB
#   CLIP light                             flashes while the master is hitting 0 dBFS, off otherwise
#   click the loudness readout             restart the loudness measurement (also done at every Play)
#   click the arrow (top right)            collapse to a tab / click the tab to expand
# =============================================================================

import math
import time

from ui.mixer.draw_utils import (
    draw_rect, draw_rounded_rect, draw_rect_outline, draw_line,
    draw_circle, draw_text, text_width,
)
from core import master as _m
from core.constants import DOUBLE_CLICK_TIME

# The panel ignores the UI zoom — every public function below forces this scale.
FIXED_SCALE = 1.0

# Layout in screen pixels (times FIXED_SCALE)
PANEL_W   = 84
PANEL_H   = 400
LOUD_H    = 70      # loudness readout block (M / S / I / LRA / TP)
MARGIN_R  = 14      # gap to the canvas right edge (keeps the scrollbar clear)
TOP_OFF   = 16      # gap from the canvas top
HANDLE_W  = 28
HANDLE_H  = 14
METER_W   = 12
TAB_W     = 26      # collapsed tab
TAB_H     = 92
BTN_SZ    = 16      # collapse arrow button

TICKS = [(6, "+6"), (0, "0"), (-6, "-6"), (-12, "-12"), (-24, "-24"), (-48, "-48")]

C_PANEL   = (0.04, 0.04, 0.045, 0.90)
C_EDGE    = (0.28, 0.28, 0.31, 1.0)
C_GROOVE  = (0.0, 0.0, 0.0, 1.0)
C_TEXT    = (0.82, 0.82, 0.85, 1.0)
C_DIM     = (0.50, 0.50, 0.54, 1.0)
C_HANDLE  = (0.80, 0.80, 0.83, 1.0)
C_METER_BG = (0.02, 0.02, 0.02, 1.0)
METER_SEGS = [   # (from dB, to dB, colour)
    (-60.0, -18.0, (0.20, 0.80, 0.30, 1.0)),
    (-18.0,  -6.0, (0.90, 0.85, 0.20, 1.0)),
    ( -6.0,   0.0, (0.95, 0.50, 0.15, 1.0)),
    (  0.0,   6.0, (0.95, 0.15, 0.10, 1.0)),
]

_collapsed = None      # None = not read from the scene yet
_drag = False
_anchor_mouse_y = 0.0
_anchor_p = 0.0
_changed = False
_last_fader_click = 0.0


# ---------------------------------------------------------------------------
# Collapsed state (saved per .blend as scene.pb_master_collapsed)
# ---------------------------------------------------------------------------
def is_collapsed():
    global _collapsed
    try:
        import bpy
        scene = bpy.context.scene
        if scene is not None and hasattr(scene, "pb_master_collapsed"):
            return bool(scene.pb_master_collapsed)
    except Exception:
        pass
    # property not registered yet (add-on loaded before it existed): session-only state
    return bool(_collapsed)


def set_collapsed(v):
    global _collapsed, _drag
    _collapsed = bool(v)
    _drag = False
    try:
        import bpy
        scene = bpy.context.scene
        if scene is not None:
            scene.pb_master_collapsed = _collapsed
    except Exception:
        pass


def reserve_w():
    """Screen pixels the canvas should keep free at its right edge for this
    widget (used by mixer_hud.compute_autofit())."""
    if is_collapsed():
        return TAB_W + MARGIN_R + 6
    return PANEL_W + MARGIN_R + 2


def _tab_rect(width, height, s):
    tw, th = TAB_W * s, TAB_H * s
    return width - tw - MARGIN_R * s * 0.5, height - TOP_OFF * s - th, tw, th


def _btn_rect(L, s):
    b = BTN_SZ * s
    return L["px"] + L["pw"] - b - 4 * s, L["top"] - b - 4 * s, b, b


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def _layout(width, height, s):
    pw = PANEL_W * s
    px = width - pw - MARGIN_R * s
    top = height - TOP_OFF * s
    ph = max(150 * s, min(PANEL_H * s, top - 24 * s))
    py = top - ph
    track_top = top - 66 * s
    track_bot = py + (44 + LOUD_H) * s
    return dict(
        px=px, py=py, pw=pw, ph=ph, top=top,
        fx=px + 36 * s,                       # fader centre x
        mx=px + 58 * s, mw=METER_W * s,       # meter left x / width
        track_top=track_top, track_bot=track_bot,
        clip_cx=px + 14 * s, clip_cy=top - 42 * s,
        ro_x=px + 8 * s, ro_y=py + 8 * s, ro_w=pw - 16 * s, ro_h=22 * s,
        ld_x=px + 8 * s, ld_y=py + 36 * s, ld_w=pw - 16 * s, ld_h=LOUD_H * s,
    )


def _db_to_p(db):
    return max(0.0, min(1.0, (db - _m.DB_MIN) / (_m.DB_MAX - _m.DB_MIN)))


def _p_to_db(p):
    p = max(0.0, min(1.0, p))
    if p <= 0.002:
        return _m.DB_MIN
    db = _m.DB_MIN + p * (_m.DB_MAX - _m.DB_MIN)
    if abs(db) < 0.4:          # detent at unity
        return 0.0
    return round(db, 1)


def _y_of_db(L, db):
    return L["track_bot"] + _db_to_p(db) * (L["track_top"] - L["track_bot"])


def _db_text(db):
    return "-inf dB" if db <= _m.DB_MIN + 0.05 else f"{db:+.1f} dB"


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _draw_tab(width, height, s):
    tx, ty, tw, th = _tab_rect(width, height, s)
    draw_rounded_rect(tx, ty, tw, th, 6 * s, C_PANEL)
    draw_rect_outline(tx, ty, tw, th, C_EDGE, 1.0)
    fs = max(1, int(10 * s))
    draw_text("<", tx + (tw - text_width("<", fs)) / 2.0, ty + th - 20 * s, fs + 2, C_TEXT)
    draw_text("M", tx + (tw - text_width("M", fs)) / 2.0, ty + th / 2.0 - 4 * s, fs, C_DIM)
    # a clip is still visible while collapsed
    draw_circle(tx + tw / 2.0, ty + 14 * s, 5.0 * s,
                (1.0, 0.1, 0.05, 1.0) if _m.clip else (0.25, 0.04, 0.03, 1.0))


def draw(width, height, s):
    s = FIXED_SCALE
    if is_collapsed():
        _draw_tab(width, height, s)
        return
    L = _layout(width, height, s)
    db = _m.get_db()

    draw_rounded_rect(L["px"], L["py"], L["pw"], L["ph"], 6 * s, C_PANEL)
    draw_rect_outline(L["px"], L["py"], L["pw"], L["ph"], C_EDGE, 1.0)

    fs = max(1, int(11 * s))
    title = "MASTER"
    draw_text(title, L["px"] + (L["pw"] - text_width(title, fs)) / 2.0 - 6 * s,
              L["top"] - 22 * s, fs, C_TEXT)

    # collapse button (arrow pointing toward the edge it folds into)
    bx, by, bw, bh = _btn_rect(L, s)
    draw_rounded_rect(bx, by, bw, bh, 3 * s, (0.12, 0.12, 0.14, 1.0))
    draw_rect_outline(bx, by, bw, bh, C_EDGE, 1.0)
    draw_text(">", bx + (bw - text_width(">", fs)) / 2.0, by + 3 * s, fs, C_TEXT)

    # clip light
    on = _m.clip
    draw_circle(L["clip_cx"], L["clip_cy"], 5.5 * s,
                (1.0, 0.1, 0.05, 1.0) if on else (0.25, 0.04, 0.03, 1.0))
    draw_text("CLIP", L["clip_cx"] + 10 * s, L["clip_cy"] - 4 * s, max(1, int(8 * s)),
              (1.0, 0.45, 0.35, 1.0) if on else C_DIM)

    tb, tt = L["track_bot"], L["track_top"]

    # scale
    ts = max(1, int(7 * s))
    for tdb, label in TICKS:
        y = _y_of_db(L, tdb)
        draw_text(label, L["px"] + 4 * s, y - 3 * s, ts, C_DIM)
        draw_line(L["fx"] - 20 * s, y, L["fx"] - 16 * s, y, C_DIM, 1.0)

    # fader groove
    draw_rect(L["fx"] - 2 * s, tb, 4 * s, tt - tb, C_GROOVE)

    # meter
    mx, mw = L["mx"], L["mw"]
    draw_rect(mx, tb, mw, tt - tb, C_METER_BG)
    lvl_db = _m.gain_to_db(_m.level)
    if _m.level > 0.0:
        for a, b, col in METER_SEGS:
            top_db = min(lvl_db, b)
            if top_db > a:
                y0 = _y_of_db(L, a)
                y1 = _y_of_db(L, top_db)
                draw_rect(mx, y0, mw, y1 - y0, col)
    if _m.hold > 0.0:
        hy = _y_of_db(L, _m.gain_to_db(_m.hold))
        hc = (1.0, 0.2, 0.15, 1.0) if _m.hold >= 1.0 else (0.92, 0.92, 0.92, 1.0)
        draw_rect(mx, hy - 1 * s, mw, 2 * s, hc)
    draw_rect_outline(mx, tb, mw, tt - tb, C_EDGE, 1.0)

    # unity line across fader + meter
    uy = _y_of_db(L, 0.0)
    draw_line(L["fx"] - 17 * s, uy, mx + mw + 2 * s, uy, (0.7, 0.7, 0.75, 0.55), 1.0)

    # handle
    hy = _y_of_db(L, db)
    hw, hh = HANDLE_W * s, HANDLE_H * s
    draw_rounded_rect(L["fx"] - hw / 2.0, hy - hh / 2.0, hw, hh, 3 * s, C_HANDLE)
    draw_line(L["fx"] - hw / 2.0 + 3 * s, hy, L["fx"] + hw / 2.0 - 3 * s, hy,
              (0.1, 0.1, 0.12, 1.0), 2.0)

    _draw_loudness(L, s)

    # readout (or a warning if the compiled engine predates the master bus)
    draw_rounded_rect(L["ro_x"], L["ro_y"], L["ro_w"], L["ro_h"], 3 * s, (0.0, 0.0, 0.0, 0.85))
    if _m.engine_outdated():
        ws = max(1, int(7 * s))
        for i, line in enumerate(("ENGINE NEEDS", "REBUILD")):
            draw_text(line, L["ro_x"] + (L["ro_w"] - text_width(line, ws)) / 2.0,
                      L["ro_y"] + (12 - i * 9) * s, ws, (1.0, 0.35, 0.25, 1.0))
        return
    txt = _db_text(db)
    rs = max(1, int(10 * s))
    draw_text(txt, L["ro_x"] + (L["ro_w"] - text_width(txt, rs)) / 2.0,
              L["ro_y"] + 6 * s, rs, C_TEXT)


def _draw_loudness(L, s):
    """Momentary / short-term / integrated loudness (LUFS), loudness range (LU), true peak (dBTP)."""
    x, y, w, h = L["ld_x"], L["ld_y"], L["ld_w"], L["ld_h"]
    draw_rounded_rect(x, y, w, h, 3 * s, (0.0, 0.0, 0.0, 0.85))
    fs = max(1, int(8 * s))
    if not _m.loudness_available():
        draw_text("no loudness", x + 4 * s, y + h - 24 * s, fs, C_DIM)
        draw_text("(rebuild)", x + 4 * s, y + h - 36 * s, fs, C_DIM)
        return
    ld = _m.loudness
    tp = ld["true_peak_db"]
    tp_col = (1.0, 0.25, 0.15, 1.0) if tp >= 0.0 else ((1.0, 0.65, 0.2, 1.0) if tp >= -1.0 else C_TEXT)
    rows = (
        ("M",   _m.format_level(ld["momentary"]),   "LUFS", C_TEXT),
        ("S",   _m.format_level(ld["short_term"]),  "LUFS", C_TEXT),
        ("I",   _m.format_level(ld["integrated"]),  "LUFS", (0.55, 0.85, 1.0, 1.0)),
        ("LRA", _m.format_level(ld["lra"]) if ld["integrated"] > -150.0 else "--", "LU", C_TEXT),
        ("TP",  _m.format_level(tp),                "dBTP", tp_col),
    )
    row_h = h / 5.0
    for i, (lab, val, unit, col) in enumerate(rows):
        ry = y + h - (i + 1) * row_h + 3.5 * s
        draw_text(lab, x + 4 * s, ry, fs, C_DIM)
        draw_text(val, x + 24 * s + max(0.0, 26 * s - text_width(val, fs)), ry, fs, col)
        draw_text(unit, x + w - 4 * s - text_width(unit, max(1, int(6 * s))), ry, max(1, int(6 * s)), C_DIM)


# ---------------------------------------------------------------------------
# Hit testing + interaction (called from ui/mixer/interaction.py)
# ---------------------------------------------------------------------------
def hit_test(rx, ry, width, height, s):
    s = FIXED_SCALE
    if is_collapsed():
        tx, ty, tw, th = _tab_rect(width, height, s)
        return "expand" if (tx <= rx <= tx + tw and ty <= ry <= ty + th) else None
    L = _layout(width, height, s)
    if not (L["px"] <= rx <= L["px"] + L["pw"] and L["py"] <= ry <= L["py"] + L["ph"]):
        return None
    bx, by, bw, bh = _btn_rect(L, s)
    if bx - 2 * s <= rx <= bx + bw + 2 * s and by - 2 * s <= ry <= by + bh + 2 * s:
        return "collapse"
    if (abs(rx - L["clip_cx"]) <= 12 * s + 28 * s and abs(ry - L["clip_cy"]) <= 10 * s):
        return "clip"
    if L["ld_x"] <= rx <= L["ld_x"] + L["ld_w"] and L["ld_y"] <= ry <= L["ld_y"] + L["ld_h"]:
        return "loudness"
    if L["ro_x"] <= rx <= L["ro_x"] + L["ro_w"] and L["ro_y"] <= ry <= L["ro_y"] + L["ro_h"]:
        return "readout"
    if (abs(rx - L["fx"]) <= (HANDLE_W / 2.0 + 4) * s
            and L["track_bot"] - 12 * s <= ry <= L["track_top"] + 12 * s):
        return "fader"
    return "panel"


def is_dragging():
    return _drag


def handle_press(rx, ry, width, height, s, mouse_y):
    """Return True if the click belonged to the master widget."""
    global _drag, _anchor_mouse_y, _anchor_p, _changed, _last_fader_click
    s = FIXED_SCALE
    kind = hit_test(rx, ry, width, height, s)
    if kind is None:
        return False
    now = time.time()
    if kind in ("collapse", "expand"):
        set_collapsed(kind == "collapse")
        return True
    if kind == "clip":
        _m.clear_clip()
        return True
    if kind == "loudness":
        _m.reset_loudness()
        return True
    if kind in ("readout", "fader"):
        double = (now - _last_fader_click) <= DOUBLE_CLICK_TIME
        _last_fader_click = now
        if double:
            _m.set_db(0.0)
            _changed = True
            _drag = False
            return True
    if kind == "fader":
        L = _layout(width, height, s)
        db = _m.get_db()
        hy = _y_of_db(L, db)
        if abs(ry - hy) > (HANDLE_H / 2.0) * s:      # clicked the track: jump there
            span = max(1.0, L["track_top"] - L["track_bot"])
            db = _p_to_db((ry - L["track_bot"]) / span)
            _m.set_db(db)
            _changed = True
        _anchor_mouse_y = mouse_y
        _anchor_p = _db_to_p(_m.get_db())
        _drag = True
    return True


def handle_move(width, height, s, mouse_y):
    global _changed
    s = FIXED_SCALE
    if not _drag:
        return False
    L = _layout(width, height, s)
    span = max(1.0, L["track_top"] - L["track_bot"])
    p = _anchor_p + (mouse_y - _anchor_mouse_y) / span
    _m.set_db(_p_to_db(p))
    _changed = True
    return True


def handle_release():
    """Return True if a master-fader change just finished (so the caller can push an undo step)."""
    global _drag, _changed
    was = _changed
    _drag = False
    _changed = False
    return was
