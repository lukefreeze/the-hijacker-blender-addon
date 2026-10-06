# =============================================================================
# rack_reverb.py
# Reverb rack UI
# ┌─ LAYOUT CONSTANTS ─────────────────────────────────────────────────────┐
# │ RACK_EXPANDED_H_RV — change height here
# └────────────────────────────────────────────────────────────────────────┘
# =============================================================================

import math
import gpu
from gpu_extras.batch import batch_for_shader
# ---------------------------------------------------------------------------
# Shader singleton — gpu.shader.from_builtin() is expensive; reuse one instance.
# ---------------------------------------------------------------------------
_shader = None

def _get_shader():
    global _shader
    if _shader is None:
        _shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    return _shader



# These drawing helpers are imported from draw_utils so the PNG bridge
# (draw_element) can replace them with texture blits when PNGs are loaded.
# Until then they call GPU primitives directly.
try:
    from ui.mixer.draw_utils import (
        draw_rect as _draw_rect,
        draw_line as _draw_line,
        draw_circle as _draw_circle,
        draw_text as _draw_text,
        text_width as _text_width,
        draw_knob as _draw_knob,
    )
except ImportError:
    # Fallback when loaded standalone — Racks.py re-exports these
    pass

RACK_RAIL_H = 32  # duplicated from Racks.py to avoid circular import

# =============================================================================
# REVERB KNOB TUNING
# All values are unscaled px — multiplied by scale at draw time.
#
# RV_KNOB_SCALE  : size multiplier for all 5 knobs (1.0 = no change)
# RV_KNOB_X      : per-knob X nudge [Room, Damp, Wet, Pre-dly, Width]
# RV_KNOB_Y      : per-knob Y nudge [Room, Damp, Wet, Pre-dly, Width]
# Positive X = right, Negative X = left.
# Positive Y = up,    Negative Y = down.
# =============================================================================
RV_KNOB_SCALE = 1.0
#                    Room   Damp    Wet  Pre-dly  Width
RV_KNOB_X    = [    0.0,   0.0,   0.0,    0.0,   0.0]
RV_KNOB_Y    = [    0.0,   0.0,   0.0,    0.0,   0.0]

# Value text nudge — shifts all 5 knob value readouts vertically.
# Negative = move down, positive = move up.
RV_VALUE_Y_OFFSET = -2.0

# =============================================================================
# GRAPH CROP — unscaled px inset from each edge of the computed display rect.
# Increase to pull the waveform/tail drawing away from background elements.
# RV_CROP_LEFT / RIGHT crop the horizontal extent.
# RV_CROP_TOP  / BOTTOM crop the vertical extent.
# =============================================================================
RV_CROP_LEFT   = 2.0
RV_CROP_RIGHT  = 2.0
RV_CROP_TOP    = 2.0
RV_CROP_BOTTOM = 2.0


# =============================================================================
# PACKAPUNCH SCREEN — the CRT glass in rack_reverb_bg.png (packapunch skin)
# The graph is drawn in a unit square (u across 0..1, v up 0..1) and every point
# is bent onto the real, slightly bulged screen outline, so grid lines, the
# waveform and the reverb tail follow the glass like a CRT. The outline was
# traced from the screen's alpha matte (1908x544 art px, top-left origin):
# four curves, each stored left->right (top / bottom) or bottom->top (left / right).
# =============================================================================
RV_PP_SKIN       = "packapunch"
RV_ART_W, RV_ART_H = 1908, 544
RV_PP_INSET_U    = 0.010     # keep drawing this far in from the screen edge (fraction of width)
RV_PP_INSET_V    = 0.045     # ... and of height
RV_PP_CORE       = (1.00, 0.76, 0.22)   # waveform colour: orangey yellow
RV_PP_GLOW       = (1.00, 0.52, 0.06)   # its soft glow
RV_PP_LINE_W     = 1.7                  # waveform line width (unscaled px)
RV_PP_GLOW_GAIN  = 1.0                  # glow strength (0 = no glow)
RV_PP_FILL_ALPHA = 0.16                 # translucent fill under the waveform / tail
RV_PP_GRID_ALPHA = 0.11                 # faint grid lines

RV_SCREEN_TOP = [(241,118), (272,117), (303,116), (334,115), (365,114), (396,114), (428,113), (459,113), (490,112), (521,112), (553,112), (584,111), (615,111), (647,111), (678,111), (710,111), (741,111), (772,110), (803,110), (835,110), (866,111), (897,111), (929,111), (960,111), (992,111), (1023,111), (1054,111), (1086,112), (1117,112), (1148,112), (1179,113), (1211,113), (1242,113), (1273,114), (1304,114), (1336,115), (1367,116), (1398,116), (1428,118), (1458,121)]
RV_SCREEN_BOTTOM = [(236,323), (266,327), (296,329), (327,330), (359,330), (390,331), (421,331), (453,332), (484,332), (516,333), (547,333), (578,334), (610,334), (641,334), (673,335), (704,335), (736,334), (767,334), (799,334), (830,334), (862,334), (894,334), (925,335), (956,335), (988,334), (1019,334), (1051,334), (1082,334), (1114,334), (1146,334), (1177,333), (1208,333), (1240,333), (1271,332), (1303,332), (1334,332), (1366,331), (1397,330), (1428,329), (1458,327)]
RV_SCREEN_LEFT = [(236,323), (234,318), (233,313), (233,308), (233,302), (233,297), (233,292), (232,286), (232,281), (232,276), (231,271), (230,266), (230,260), (230,255), (230,249), (230,244), (230,238), (230,233), (230,228), (230,222), (230,217), (230,211), (230,206), (230,200), (230,195), (230,190), (231,184), (231,179), (231,174), (231,168), (232,163), (232,158), (232,152), (232,147), (233,142), (233,137), (234,132), (236,127), (238,122), (241,118)]
RV_SCREEN_RIGHT = [(1458,327), (1460,322), (1462,318), (1463,313), (1464,308), (1465,302), (1465,297), (1465,292), (1465,286), (1466,281), (1466,276), (1466,270), (1466,265), (1467,259), (1467,254), (1467,249), (1467,243), (1467,238), (1467,232), (1467,227), (1467,221), (1467,216), (1467,210), (1467,205), (1467,199), (1467,194), (1466,189), (1466,183), (1466,178), (1465,173), (1465,167), (1465,162), (1464,157), (1464,151), (1464,146), (1463,141), (1463,136), (1462,130), (1460,126), (1458,121)]


def _rv_curve(c, t):
    n = len(c) - 1
    x = min(max(t, 0.0), 1.0) * n
    i = min(int(x), n - 1)
    f = x - i
    return (c[i][0] + (c[i + 1][0] - c[i][0]) * f,
            c[i][1] + (c[i + 1][1] - c[i][1]) * f)


def _rv_screen_xy(u, v, rx, ry, rw, rh):
    """Unit-square point (u, v; v up) -> rack pixel position on the curved CRT glass.
    Coons patch between the four traced outline curves."""
    u = RV_PP_INSET_U + u * (1.0 - 2.0 * RV_PP_INSET_U)
    v = RV_PP_INSET_V + v * (1.0 - 2.0 * RV_PP_INSET_V)
    bx, by = _rv_curve(RV_SCREEN_BOTTOM, u)
    tx, ty = _rv_curve(RV_SCREEN_TOP, u)
    lx, ly = _rv_curve(RV_SCREEN_LEFT, v)
    qx, qy = _rv_curve(RV_SCREEN_RIGHT, v)
    BL, BR = RV_SCREEN_BOTTOM[0], RV_SCREEN_BOTTOM[-1]
    TL, TR = RV_SCREEN_TOP[0], RV_SCREEN_TOP[-1]
    ax = ((1 - v) * bx + v * tx + (1 - u) * lx + u * qx
          - ((1 - u) * (1 - v) * BL[0] + u * (1 - v) * BR[0] + (1 - u) * v * TL[0] + u * v * TR[0]))
    ay = ((1 - v) * by + v * ty + (1 - u) * ly + u * qy
          - ((1 - u) * (1 - v) * BL[1] + u * (1 - v) * BR[1] + (1 - u) * v * TL[1] + u * v * TR[1]))
    return rx + ax / RV_ART_W * rw, ry + (1.0 - ay / RV_ART_H) * rh


def _rv_thick(pts, width, color, additive=False):
    """One-batch thick polyline (triangle strip on averaged normals, no overlaps)."""
    n = len(pts)
    if n < 2:
        return
    hw = width * 0.5
    verts = []
    for i in range(n):
        if i == 0:
            dx, dy = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
        elif i == n - 1:
            dx, dy = pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1]
        else:
            dx, dy = pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1]
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L * hw, dx / L * hw
        verts.append((pts[i][0] + nx, pts[i][1] + ny))
        verts.append((pts[i][0] - nx, pts[i][1] - ny))
    shader = _get_shader()
    gpu.state.blend_set("ADDITIVE" if additive else "ALPHA")
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, "TRI_STRIP", {"pos": verts}).draw(shader)
    gpu.state.blend_set("ALPHA")


def _rv_glow_line(pts, scale, strength=1.0):
    """Orangey-yellow line with a soft glow around it."""
    w = RV_PP_LINE_W * scale
    g = RV_PP_GLOW_GAIN * strength
    if g > 0:
        _rv_thick(pts, w * 5.0, (*RV_PP_GLOW, 0.07 * g), additive=True)
        _rv_thick(pts, w * 2.6, (*RV_PP_GLOW, 0.17 * g), additive=True)
    _rv_thick(pts, w, (*RV_PP_CORE, 0.95 * strength))


def _rv_fill(top_pts, bot_pts, color):
    """Translucent fill between two matching polylines."""
    verts = []
    for a, b in zip(top_pts, bot_pts):
        verts += [a, b]
    if len(verts) >= 4:
        shader = _get_shader()
        gpu.state.blend_set("ALPHA")
        shader.bind()
        shader.uniform_float("color", color)
        batch_for_shader(shader, "TRI_STRIP", {"pos": verts}).draw(shader)


def _rv_dry_envelope(rack, get_rack_channels):
    """Scrolling window of peak amplitude around the playhead, normalised 0..1
    (same data the standard skin's dry waveform uses). Returns (values, N_WIN) or None."""
    try:
        import bpy as _bpy_rv
        from core.meters import _envelope_cache, get_envelope as _get_env_rv
        from core import vse_compat as _vse
        assigned_rv = get_rack_channels(rack)
        if not assigned_rv:
            return None
        ch_rv = list(assigned_rv)[0]
        scene_rv = _bpy_rv.context.scene
        if not (scene_rv and scene_rv.sequence_editor):
            return None
        fps_rv = scene_rv.render.fps / scene_rv.render.fps_base
        cur_f = scene_rv.frame_current
        N_WIN = 80
        f_start = cur_f - N_WIN // 2
        f_end = f_start + N_WIN
        strips_rv = [s for s in _vse.get_all_strips(scene_rv.sequence_editor)
                     if s.type == "SOUND" and s.sound and (s.channel - 1) == ch_rv]
        peak_by_frame = {}
        for strip in strips_rv:
            fp = _bpy_rv.path.abspath(strip.sound.filepath)
            if fp not in _envelope_cache:
                _get_env_rv(fp, fps_rv)
            env = _envelope_cache.get(fp)
            if env is None or len(env) < 2:
                continue
            peak_list = env[1]
            fs = int(strip.frame_start)
            fo = int(getattr(strip, "frame_offset_start", 0))
            for fi in range(f_start, f_end):
                file_f = fi - fs + fo
                if 0 <= file_f < len(peak_list):
                    peak_by_frame[fi] = float(peak_list[file_f])
        vals = [peak_by_frame.get(f_start + i, 0.0) for i in range(N_WIN)]
        mx = max(max(vals), 0.001)
        return [v / mx for v in vals], N_WIN
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PACKAPUNCH KNOBS
# Positions read off the skin art (art px, top-left origin). The live knob is
# drawn over the baked knob; the value goes in the baked amber readout box under it.
# ---------------------------------------------------------------------------
RV_PP_KNOB_CX    = (337.5, 594.5, 851.0, 1108.0, 1368.0)   # centre of each knob's TOP FACE, x
RV_PP_KNOB_CY    = (429.5, 428.0, 429.0, 428.0, 430.0)      # ... and y (the baked knobs are seen in perspective)
RV_PP_KNOB_R     = 26.5     # live knob radius (art px) = the flat top of the baked knob, so its 3D skirt stays visible
RV_PP_KNOB_HIT_R = 50.0     # click radius (art px)
RV_PP_VALUE_CX   = (351.5, 602.5, 854.5, 1105.5, 1358.5)   # centres of the baked readout boxes, x
RV_PP_VALUE_CY   = 497.0    # ... and y
RV_PP_VALUE_BOX_H = 28.0    # height of a readout box (sets the text size)
RV_PP_VALUE_COL  = (1.00, 0.72, 0.22, 0.95)


RV_PP_POINTER_COL   = (*RV_PP_CORE, 0.95)       # the pointer line colour (same orangey yellow as the screen)
RV_PP_POINTER_W     = 4.5     # pointer thickness (art px)
RV_PP_POINTER_START = 0.30    # where the line starts, as a fraction of the knob radius (0 = centre)
RV_PP_POINTER_END   = 0.92    # ... and where it ends (1 = edge of the knob top)
RV_PP_POINTER_GLOW  = 0.0     # soft amber glow behind the line (0 = off, try 0.5)


RV_PP_KNOB_FADE_SEC = 0.9     # seconds for the readouts to fade out / the pointers to dim when the rack is switched off
RV_PP_POINTER_DIM   = 0.22    # how bright the pointer lines stay when the rack is off (0 = invisible, 1 = no dimming)

_rv_knob_pow = {}             # rack_idx -> {"en", "t0", "l0", "l"}


def _rv_knob_level(rack_idx, enabled, now):
    """0..1 'power' of the knob lights: fades down after switch-off, snaps to 1 on switch-on."""
    st = _rv_knob_pow.get(rack_idx)
    if st is None:                                    # first sight: no animation
        v = 1.0 if enabled else 0.0
        st = {"en": bool(enabled), "t0": now - 999.0, "l0": v, "l": v}
        _rv_knob_pow[rack_idx] = st
    if bool(enabled) != st["en"]:
        st["en"], st["t0"], st["l0"] = bool(enabled), now, st["l"]
    if st["en"]:
        lv = 1.0                                      # instant light-up
    else:
        lv = st["l0"] * max(0.0, 1.0 - (now - st["t0"]) / max(0.01, RV_PP_KNOB_FADE_SEC))
    st["l"] = lv
    return lv


def _rv_pp_pointer(cx, cy, r, val, sr, level=1.0):
    """packApunch knob: just the pointer line (the knob itself is in the background art).
    Same angle convention as the standard knob: 225 deg at minimum, sweeping 270 deg clockwise."""
    a = math.radians(225.0 - max(0.0, min(1.0, val)) * 270.0)
    ca, sa = math.cos(a), math.sin(a)
    p0 = (cx + ca * r * RV_PP_POINTER_START, cy + sa * r * RV_PP_POINTER_START)
    p1 = (cx + ca * r * RV_PP_POINTER_END,   cy + sa * r * RV_PP_POINTER_END)
    w = max(1.5, RV_PP_POINTER_W * sr)
    k = RV_PP_POINTER_DIM + (1.0 - RV_PP_POINTER_DIM) * level      # 1 = lit, RV_PP_POINTER_DIM = rack off
    if RV_PP_POINTER_GLOW > 0 and level > 0.01:
        _rv_thick([p0, p1], w * 3.0, (*RV_PP_GLOW, 0.20 * RV_PP_POINTER_GLOW * level), additive=True)
    c = RV_PP_POINTER_COL
    _rv_thick([p0, p1], w, (c[0] * k, c[1] * k, c[2] * k, c[3]))


def reverb_pp_knob_geom(rack_x, rack_y, rw, rh):
    """Screen-space knob layout for the packApunch skin: list of 5
    (cx, cy, live_radius, hit_radius, value_cx, value_cy, value_box_h),
    or None when the packApunch skin is not active."""
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas_kg
        if _gas_kg() != RV_PP_SKIN:
            return None
    except Exception:
        return None
    sx, sy = rw / RV_ART_W, rh / RV_ART_H
    sr = min(sx, sy)
    vy = rack_y + (1.0 - RV_PP_VALUE_CY / RV_ART_H) * rh
    out = []
    for k in range(5):
        cx = rack_x + RV_PP_KNOB_CX[k] * sx
        cy = rack_y + (1.0 - RV_PP_KNOB_CY[k] / RV_ART_H) * rh
        vx = rack_x + RV_PP_VALUE_CX[k] * sx
        out.append((cx, cy, RV_PP_KNOB_R * sr, RV_PP_KNOB_HIT_R * sr, vx, vy, RV_PP_VALUE_BOX_H * sy))
    return out


# ---------------------------------------------------------------------------
# PACKAPUNCH POWER ANIMATION
# Switching the rack off wipes the waveform / reverb curve away from the RIGHT
# of the screen to the LEFT until it is gone, with a hot flickering edge where
# the signal is "losing power". Switching it back on redraws it left -> right.
# ---------------------------------------------------------------------------
RV_PP_POWER_DOWN_SEC = 1.6      # seconds for the curve to be wiped out
RV_PP_POWER_UP_SEC   = 1.1      # seconds for the curve to draw back in
RV_PP_EDGE_GAIN      = 1.0      # brightness of the wipe edge (0 = no edge effect)

_rv_power = {}                  # rack_idx -> {"en", "t0", "c0", "c"}


def _rv_power_cut(rack_idx, enabled, now):
    """How much of the screen (0..1, measured from the left) is currently drawn."""
    st = _rv_power.get(rack_idx)
    if st is None:                                   # first sight: no animation
        v = 1.0 if enabled else 0.0
        st = {"en": bool(enabled), "t0": now - 999.0, "c0": v, "c": v}
        _rv_power[rack_idx] = st
    if bool(enabled) != st["en"]:
        st["en"], st["t0"], st["c0"] = bool(enabled), now, st["c"]
    dt = now - st["t0"]
    if st["en"]:
        x = min(1.0, dt / max(0.01, RV_PP_POWER_UP_SEC))
        c = st["c0"] + (1.0 - st["c0"]) * x
    else:
        x = min(1.0, dt / max(0.01, RV_PP_POWER_DOWN_SEC))
        c = st["c0"] * (1.0 - x)
    st["c"] = c
    return c


def _rv_clip_uv(pts, cut):
    """Keep the part of a (u, v) polyline with u <= cut (last point interpolated)."""
    out = []
    for k, (u, v) in enumerate(pts):
        if u <= cut:
            out.append((u, v))
        else:
            if out and k > 0:
                u0, v0 = pts[k - 1]
                f = (cut - u0) / max(u - u0, 1e-9)
                out.append((cut, v0 + (v - v0) * f))
            break
    return out if len(out) >= 2 else []


def _draw_reverb_screen_pp(rx, ry, rw, rh, rack, rack_idx, scale, get_rack_channels,
                           room_sz, damping, wet, pre_d):
    """PackAPunch reverb screen: dry waveform + reverb tail, bent onto the CRT glass."""
    import time as _time
    import random as _random
    P = lambda u, v: _rv_screen_xy(u, v, rx, ry, rw, rh)
    PL = lambda pts: [P(u, v) for (u, v) in pts]
    fs = max(1, int(9 * scale))
    pre_frac = pre_d * 0.20
    cut = _rv_power_cut(rack_idx, rack.enabled, _time.monotonic())
    animating = 0.002 < cut < 0.998

    # Faint grid (the screen's own art stays visible - no background is drawn)
    for v in (0.25, 0.5, 0.75):
        _rv_thick([P(i / 40.0, v) for i in range(41)], max(1.0, scale * 0.8),
                  (*RV_PP_GLOW, RV_PP_GRID_ALPHA))
    for k in range(1, 8):
        _rv_thick([P(k / 8.0, j / 16.0) for j in range(17)], max(1.0, scale * 0.8),
                  (*RV_PP_GLOW, RV_PP_GRID_ALPHA * 0.6))

    if cut <= 0.002:
        return                                       # fully powered down: just the glass

    # LEFT ZONE: dry waveform
    env = _rv_dry_envelope(rack, get_rack_channels)
    if env and pre_frac > 0.002:
        vals, n_win = env
        den = max(n_win - 1, 1)
        top = _rv_clip_uv([(pre_frac * i / den, 0.5 + 0.34 * a) for i, a in enumerate(vals)], cut)
        bot = _rv_clip_uv([(pre_frac * i / den, 0.5 - 0.34 * a) for i, a in enumerate(vals)], cut)
        if top and bot:
            _rv_fill(PL(top), PL(bot), (*RV_PP_GLOW, RV_PP_FILL_ALPHA))
            _rv_glow_line(PL(top), scale)
            _rv_glow_line(PL(bot), scale)
        ph_u = pre_frac * (n_win // 2) / den
        if ph_u <= cut:
            _rv_thick([P(ph_u, 0.03), P(ph_u, 0.97)], max(1.2, scale * 1.2), (1.0, 0.9, 0.6, 0.45))

    # Pre-delay divider + label
    if 0.005 < pre_frac <= cut:
        _rv_thick([P(pre_frac, j / 12.0) for j in range(13)], max(1.0, scale), (*RV_PP_CORE, 0.35))
        dx, dy = P(pre_frac + 0.004, 0.93)
        _draw_text(f"{int(pre_d * 100)}ms", dx, dy - fs, max(1, int(8 * scale)), (*RV_PP_CORE, 0.8))

    # RIGHT ZONE: reverb tail silhouette (shape from room size + damping, like the standard skin)
    feedback = 0.28 + room_sz * 0.70
    if feedback < 0.9999:
        rt60_frac = -0.05 / math.log10(max(feedback, 1e-9))
    else:
        rt60_frac = 2.0
    rt60_frac = min(rt60_frac, 2.0)
    hf_rt60_frac = rt60_frac * (1.0 - damping * 0.7)
    N_TAIL = 96
    peak_h = 0.45 * wet
    tail_w = 1.0 - pre_frac
    up, dn, hf_up, hf_dn = [], [], [], []
    for i in range(N_TAIL + 1):
        t = i / N_TAIL
        u = pre_frac + t * tail_w
        amp = math.exp(-t * 3.0 / max(rt60_frac, 0.01)) if rt60_frac > 0 else 0.0
        hf = math.exp(-t * 3.0 / max(hf_rt60_frac, 0.01)) if hf_rt60_frac > 0 else 0.0
        up.append((u, 0.5 + amp * peak_h))
        dn.append((u, 0.5 - amp * peak_h))
        hf_up.append((u, 0.5 + hf * peak_h * 0.65))
        hf_dn.append((u, 0.5 - hf * peak_h * 0.65))
    up, dn, hf_up, hf_dn = (_rv_clip_uv(c, cut) for c in (up, dn, hf_up, hf_dn))
    if up and dn:
        _rv_fill(PL(up), PL(dn), (*RV_PP_GLOW, RV_PP_FILL_ALPHA * 0.85))
        _rv_glow_line(PL(up), scale, 0.9)
        _rv_glow_line(PL(dn), scale, 0.9)
    for pts in (hf_up, hf_dn):                       # damping curves: paler yellow, thinner
        if pts:
            _rv_thick(PL(pts), max(1.0, scale * 1.1), (1.0, 0.92, 0.55, 0.65))

    # Labels disappear as the wipe passes them
    if rt60_frac > 0 and cut > 0.93:
        rt60_ms = rt60_frac * 1000
        rt60_str = f"{rt60_ms:.0f}ms" if rt60_ms < 1000 else f"{rt60_ms / 1000:.1f}s"
        lx, ly = P(0.90, 0.07)
        _draw_text(f"RT60 {rt60_str}", lx, ly, fs, (*RV_PP_CORE, 0.85))
    if cut > 0.05:
        lx, ly = P(0.012, 0.07)
        _draw_text("dry", lx, ly, max(1, int(8 * scale)), (*RV_PP_CORE, 0.70))
    if cut > pre_frac + 0.06:
        lx, ly = P(pre_frac + 0.01, 0.07)
        _draw_text("tail", lx, ly, max(1, int(8 * scale)), (*RV_PP_CORE, 0.70))

    # Hot, flickering edge where the signal is being wiped / redrawn
    if animating and RV_PP_EDGE_GAIN > 0:
        fl = (0.55 + 0.45 * _random.random()) * RV_PP_EDGE_GAIN
        edge = [P(cut, 0.04 + 0.92 * j / 14.0) for j in range(15)]
        _rv_thick(edge, max(6.0, scale * 9.0), (*RV_PP_GLOW, 0.10 * fl), additive=True)
        _rv_thick(edge, max(3.0, scale * 4.0), (*RV_PP_GLOW, 0.22 * fl), additive=True)
        _rv_thick(edge, max(1.2, scale * 1.4), (1.0, 0.92, 0.6, 0.65 * fl), additive=True)


def _draw_reverb_body(rx, ry, rw, rh, rack, rack_idx, scale):
    """Draw the reverb rack body — Option C style.

    Display area (upper 60% of body):
      Left zone  — dry waveform from _fft_timeline (same as EQ pre-EQ layer)
      Divider    — dashed vertical line at the pre-delay position
      Right zone — computed reverb tail silhouette, exponentially decaying,
                   shape driven entirely by room_size and damping knobs

    Knob strip (lower 40%):
      Room | Damp | Wet | Pre-dly | Width
    """
    # Lazy imports — avoids circular import at module load time
    import Racks as _racks_mod
    get_rack_channels = _racks_mod.get_rack_channels
    import math as _mr
    ui_scale    = scale
    rail_h      = RACK_RAIL_H * ui_scale
    body_h      = rh - rail_h

    # ── Skin background — full rack height (rail/title now baked into PNG)
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_rv
        from ui.mixer.texture_cache import blit_texture as _blt_rv
        _rv_tex = _gtc_rv("rack_reverb_bg")
        if _rv_tex:
            _blt_rv(_rv_tex, rx, ry, rw, rh, key="rack_reverb_bg")
        else:
            _draw_rect(rx, ry, rw, body_h, (0.07, 0.07, 0.07, 1.0))
    except Exception:
        _draw_rect(rx, ry, rw, body_h, (0.07, 0.07, 0.07, 1.0))

    # --- Display geometry: display at TOP of body, knobs at BOTTOM ---
    margin_l    = 42 * ui_scale
    ch_btn_w    = 108 * ui_scale
    margin_r    = ch_btn_w + 8 * ui_scale
    disp_x      = rx + margin_l        + RV_CROP_LEFT   * ui_scale
    disp_w      = rw - margin_l - margin_r - (RV_CROP_LEFT + RV_CROP_RIGHT) * ui_scale
    disp_prop   = 0.56          # display takes 56% of body height
    disp_h      = body_h * disp_prop - 4 * ui_scale - (RV_CROP_TOP + RV_CROP_BOTTOM) * ui_scale
    disp_y      = ry + body_h - disp_h - 2 * ui_scale - RV_CROP_TOP * ui_scale  # top of body

    # Knob strip sits at the bottom of the body
    knob_h      = body_h * 0.42 - 4 * ui_scale
    knob_y      = ry + 2 * ui_scale                     # bottom of body

    shader = _get_shader()

    # --- Read knob params ---
    room_sz  = getattr(rack, 'p0', 0.5)
    damping  = getattr(rack, 'p1', 0.5)
    wet      = getattr(rack, 'p2', 0.3)
    pre_d    = getattr(rack, 'p3', 0.0)
    width    = getattr(rack, 'p4', 1.0)

    # Pre-delay position as fraction of display width (0–20% of display)
    pre_frac = pre_d * 0.20
    div_x    = disp_x + pre_frac * disp_w

    # PackAPunch: the screen is the skin's own CRT glass (no background drawn),
    # and the graph is bent onto its curved outline in amber with a soft glow.
    _rv_pp = False
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas_rv
        _rv_pp = _gas_rv() == RV_PP_SKIN
    except Exception:
        pass

    if _rv_pp:
        try:
            _draw_reverb_screen_pp(rx, ry, rw, rh, rack, rack_idx, ui_scale, get_rack_channels,
                                   room_sz, damping, wet, pre_d)
        except Exception as _e_rvpp:
            print(f"[REVERB] packapunch screen error: {_e_rvpp}")
    else:
        # Display background
        _draw_rect(disp_x, disp_y, disp_w, disp_h, (0.07, 0.07, 0.09, 1.0))

        # Grid lines
        for db_frac in [0.25, 0.5, 0.75]:
            ly = disp_y + db_frac * disp_h
            gl = batch_for_shader(shader, "LINES",
                                   {"pos": [(disp_x, ly), (disp_x + disp_w, ly)]})
            shader.bind()
            shader.uniform_float("color", (0.18, 0.18, 0.20, 1.0))
            gl.draw(shader)

        # --- LEFT ZONE: dry waveform from envelope cache ---
        # Uses _envelope_cache (pre-built at play start) keyed by filepath.
        # Shows a scrolling N_WIN-frame window of peak amplitude centred on
        # the playhead — identical pattern to the noise gate waveform.
        if rack.enabled:
            try:
                import bpy as _bpy_rv
                from core.meters import _envelope_cache, get_envelope as _get_env_rv
                from core import vse_compat as _vse
                assigned_rv = get_rack_channels(rack)
                if assigned_rv:
                    ch_rv   = list(assigned_rv)[0]
                    scene_rv = _bpy_rv.context.scene
                    if scene_rv and scene_rv.sequence_editor:
                        fps_rv  = scene_rv.render.fps / scene_rv.render.fps_base
                        cur_f   = scene_rv.frame_current
                        N_WIN   = 80
                        half    = N_WIN // 2
                        f_start = cur_f - half
                        f_end   = f_start + N_WIN

                        strips_rv = [
                            s for s in _vse.get_all_strips(scene_rv.sequence_editor)
                            if s.type == "SOUND" and s.sound
                            and (s.channel - 1) == ch_rv
                        ]

                        peak_by_frame = {}
                        for strip in strips_rv:
                            fp = _bpy_rv.path.abspath(strip.sound.filepath)
                            if fp not in _envelope_cache:
                                _get_env_rv(fp, fps_rv)
                            env = _envelope_cache.get(fp)
                            if env is None or len(env) < 2:
                                continue
                            peak_list = env[1]
                            fs = int(strip.frame_start)
                            fo = int(getattr(strip, "frame_offset_start", 0))
                            for fi in range(f_start, f_end):
                                file_f = fi - fs + fo
                                if 0 <= file_f < len(peak_list):
                                    peak_by_frame[fi] = float(peak_list[file_f])

                        rms_vals_rv = [peak_by_frame.get(f_start + i, 0.0)
                                       for i in range(N_WIN)]
                        max_rv = max(max(rms_vals_rv), 0.001)

                        # Draw waveform clipped to left zone (up to pre-delay divider)
                        left_w   = div_x - disp_x
                        half_h_rv = disp_h * 0.34
                        centre_rv = disp_y + disp_h * 0.5
                        wf_top_rv = []; wf_bot_rv = []
                        for i, amp in enumerate(rms_vals_rv):
                            bx  = disp_x + (i / max(N_WIN - 1, 1)) * left_w
                            h   = (amp / max_rv) * half_h_rv
                            wf_top_rv.append((bx, centre_rv - h))
                            wf_bot_rv.append((bx, centre_rv + h))

                        fill_rv = []
                        for (bx, ty), (_, by) in zip(wf_top_rv, wf_bot_rv):
                            fill_rv += [(bx, ty), (bx, by)]
                        if len(fill_rv) >= 4:
                            bf = batch_for_shader(shader, "TRI_STRIP", {"pos": fill_rv})
                            shader.bind()
                            shader.uniform_float("color", (0.17, 0.17, 0.22, 0.82))
                            bf.draw(shader)
                        for pts_rv in [wf_top_rv, wf_bot_rv]:
                            if len(pts_rv) >= 2:
                                be = batch_for_shader(shader, "LINE_STRIP", {"pos": pts_rv})
                                gpu.state.line_width_set(max(1.0, ui_scale * 0.7))
                                shader.bind()
                                shader.uniform_float("color", (0.32, 0.32, 0.40, 0.60))
                                be.draw(shader)
                                gpu.state.line_width_set(1.0)

                        # Playhead cursor in left zone
                        ph_x = disp_x + (half / max(N_WIN - 1, 1)) * left_w
                        ph_b = batch_for_shader(shader, "LINES",
                                                {"pos": [(ph_x, disp_y + 2*ui_scale),
                                                         (ph_x, disp_y + disp_h - 2*ui_scale)]})
                        gpu.state.line_width_set(max(1.5, ui_scale))
                        shader.bind()
                        shader.uniform_float("color", (0.85, 0.85, 0.90, 0.50))
                        ph_b.draw(shader)
                        gpu.state.line_width_set(1.0)

            except Exception:
                pass  # waveform is decorative — never crash

        # --- Pre-delay divider ---
        if pre_frac > 0.005:
            div_verts = [(div_x, disp_y), (div_x, disp_y + disp_h)]
            div_batch = batch_for_shader(shader, "LINES", {"pos": div_verts})
            shader.bind()
            shader.uniform_float("color", (0.55, 0.55, 0.60, 0.50))
            div_batch.draw(shader)
            # Label
            fs_pd = max(1, int(8*ui_scale))
            _draw_text(f"{int(pre_d*100)}ms", div_x + 2*ui_scale,
                       disp_y + disp_h - fs_pd - 2*ui_scale, fs_pd, (0.55, 0.55, 0.60, 0.80))

        # --- RIGHT ZONE: reverb tail silhouette ---
        # Exponential decay: y(t) = exp(-t * decay_rate)
        # decay_rate is derived from room_size and damping
        # RT60 (60dB decay time) = -60 / (20*log10(e) * decay_rate)
        # We map room_size → feedback (0.28-0.98), damping → HF rolloff
        feedback     = 0.28 + room_sz * 0.70
        # Approximate RT60 in display-space: larger room = longer tail
        if feedback < 0.9999:
            rt60_frac = -0.05 / _mr.log10(max(feedback, 1e-9))  # in display width units
        else:
            rt60_frac = 2.0
        rt60_frac = min(rt60_frac, 2.0)

        # HF curve decays faster by damping factor
        hf_rt60_frac = rt60_frac * (1.0 - damping * 0.7)

        tail_start_x = div_x
        tail_w       = disp_x + disp_w - tail_start_x
        N_TAIL       = 128
        centre_y     = disp_y + disp_h * 0.5
        peak_h       = disp_h * 0.45 * wet  # taller tail = more wet

        # Full-band tail (grey)
        tail_verts = []
        for i in range(N_TAIL + 1):
            t = i / N_TAIL
            x = tail_start_x + t * tail_w
            if rt60_frac > 0:
                amp = _mr.exp(-t * 3.0 / max(rt60_frac, 0.01))
            else:
                amp = 0.0
            h = amp * peak_h
            tail_verts.append((x, centre_y))
            tail_verts.append((x, centre_y + h))

        if len(tail_verts) >= 4:
            bt = batch_for_shader(shader, "TRI_STRIP", {"pos": tail_verts})
            shader.bind()
            shader.uniform_float("color", (0.28, 0.32, 0.38, 0.65))
            bt.draw(shader)

        # Mirror lower half
        tail_lower = []
        for i in range(N_TAIL + 1):
            t = i / N_TAIL
            x = tail_start_x + t * tail_w
            amp = _mr.exp(-t * 3.0 / max(rt60_frac, 0.01)) if rt60_frac > 0 else 0.0
            h = amp * peak_h
            tail_lower.append((x, centre_y))
            tail_lower.append((x, centre_y - h))

        if len(tail_lower) >= 4:
            bl = batch_for_shader(shader, "TRI_STRIP", {"pos": tail_lower})
            shader.bind()
            shader.uniform_float("color", (0.28, 0.32, 0.38, 0.65))
            bl.draw(shader)

        # HF tail overlay (lighter, decays faster — shows damping effect)
        hf_verts_top = []; hf_verts_bot = []
        for i in range(N_TAIL + 1):
            t = i / N_TAIL
            x = tail_start_x + t * tail_w
            amp = _mr.exp(-t * 3.0 / max(hf_rt60_frac, 0.01)) if hf_rt60_frac > 0 else 0.0
            h = amp * peak_h * 0.65
            hf_verts_top.append((x, centre_y + h))
            hf_verts_bot.append((x, centre_y - h))

        for hf_verts in [hf_verts_top, hf_verts_bot]:
            if len(hf_verts) >= 2:
                bh = batch_for_shader(shader, "LINE_STRIP", {"pos": hf_verts})
                gpu.state.line_width_set(max(1.0, ui_scale * 0.7))
                shader.bind()
                shader.uniform_float("color", (0.50, 0.60, 0.72, 0.70))
                bh.draw(shader)
                gpu.state.line_width_set(1.0)

        # RT60 label
        if rt60_frac > 0:
            rt60_ms = rt60_frac * 1000
            rt60_str = f"{rt60_ms:.0f}ms" if rt60_ms < 1000 else f"{rt60_ms/1000:.1f}s"
            fs_rt = max(1, int(9*ui_scale))
            _draw_text(f"RT60 {rt60_str}", disp_x + disp_w - 60*ui_scale,
                       disp_y + 6*ui_scale, fs_rt, (0.50, 0.60, 0.72, 0.85))

        # Labels
        fs_lbl = max(1, int(8*ui_scale))
        _draw_text("dry", disp_x + 3*ui_scale, disp_y + 5*ui_scale,
                   fs_lbl, (0.45, 0.45, 0.48, 0.80))
        _draw_text("tail", tail_start_x + 4*ui_scale, disp_y + 5*ui_scale,
                   fs_lbl, (0.50, 0.60, 0.72, 0.80))

    # --- KNOB STRIP (5 knobs: Room, Damp, Wet, Pre-dly, Width) ---
    N_KNOBS  = 5
    col_w    = disp_w / N_KNOBS
    row_slot = knob_h / 3.0
    row_knob = knob_y + knob_h - row_slot * 1.3
    kr       = min(max(13*ui_scale, col_w*0.16), 20*ui_scale) * RV_KNOB_SCALE
    kr       = min(kr, row_slot * 0.42)

    RV_KNOB_PARAMS = ["Room", "Damp", "Wet", "Pre-dly", "Width"]
    rv_vals = [room_sz, damping, wet, pre_d, width]
    rv_col  = (0.35, 0.65, 0.90)

    _pp_knobs = reverb_pp_knob_geom(rx, ry, rw, rh) if _rv_pp else None
    _pp_lvl = 1.0
    if _pp_knobs:
        import time as _time_kp
        _pp_lvl = _rv_knob_level(rack_idx, rack.enabled, _time_kp.monotonic())
    for ki in range(N_KNOBS):
        val = rv_vals[ki]
        pct_str = f"{int(val*100)}%"
        if _pp_knobs:
            # packApunch: knob sits on the baked knob, value shown in the baked readout box
            kcx, kcy, kkr, _hit, vcx, vcy, vbh = _pp_knobs[ki]
            _rv_pp_pointer(kcx, kcy, kkr, val, min(rw / RV_ART_W, rh / RV_ART_H), _pp_lvl)
            if _pp_lvl > 0.01:
                vfs = max(1, int(vbh * 0.5))
                vtw = _text_width(pct_str, vfs)
                _draw_text(pct_str, vcx - vtw / 2, vcy - vfs / 2 + 1, vfs,
                           (RV_PP_VALUE_COL[0], RV_PP_VALUE_COL[1], RV_PP_VALUE_COL[2],
                            RV_PP_VALUE_COL[3] * _pp_lvl))
            continue
        cx = disp_x + (ki + 0.5) * col_w + RV_KNOB_X[ki] * ui_scale
        cy = row_knob + RV_KNOB_Y[ki] * ui_scale
        _draw_knob(cx, cy, kr, val, rv_col,
                   "", pct_str, ui_scale,  # label suppressed - baked into background PNG
                   value_y_offset=RV_VALUE_Y_OFFSET)
