# =============================================================================
# ui/racks/rack_booster.py
# THE BOOSTER!! — Volume amplification beyond Blender's strip.volume=100 cap.
#
# p0 = boost_norm   (0.0 → 0dB,  1.0 → 40dB)
# p1 = limiter_on   (> 0.5 = on)
# p2 = target_ch    (0 = auto, 1–32 = specific VSE channel)
#
# Column layout (same widths as original):
#   Left   (25%):  big boost knob — unchanged geometry
#   Centre (50%):  IN meter / OUT preview meter / preset grid / apply+limiter
#   Right  (25%):  top section: clear (channel assignment buttons drawn by rack_base)
#                  bottom section: output channel stepper + status dot
#
# Channel assignment buttons are drawn by rack_base.py at rx+rw-100*scale.
# They occupy the TOP-RIGHT of the body (approx top 110*scale of body height).
# Our right column content sits in the lower portion, clear of that overlap.
# =============================================================================

import math
import time
import bpy
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



try:
    from ui.mixer.draw_utils import (
        draw_rect   as _draw_rect,
        draw_line   as _draw_line,
        draw_circle as _draw_circle,
        draw_text   as _draw_text,
        text_width  as _text_width,
        draw_knob   as _draw_knob,
    )
except ImportError:
    pass

RACK_RAIL_H = 32

# =============================================================================
# PACKAPUNCH SKIN — relit knob sprites (12 frames, pointer angle measured per frame)
# Art coordinates are in rack_booster_bg.png space (1988 x 528).
# =============================================================================
BST_PP_SKIN       = "packapunch"
BST_PP_ART_W      = 1988.0
BST_PP_ART_H      = 528.0
BST_PP_KNOB_POS   = (313.5, 277.5)    # centre of the knob cap
BST_PP_KNOB_SIZE  = (219.0, 219.0)   # the rotating cap, drawn 1:1 over the cap baked into the art
BST_PP_HIT_R      = 150.0            # hit radius (art px)
BST_PP_SWEEP      = 270.0            # degrees of travel, min = lower-left (-135), max = lower-right (+135)
BST_PP_KNOB_KEY   = "rack_booster_knob"
BST_PP_KNOB_LINE  = 0.0              # angle of the red pointer in the sprite, degrees clockwise from up
BST_PP_VALUE_POS  = (314.0, 504.5)   # centre of the "+6.0 dB" readout (art px)
BST_PP_VALUE_PX   = 21.0             # readout font size (art px)
BST_PP_VALUE_COL  = (0.80, 0.79, 0.76, 0.95)


def _bst_pp_active():
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas_bs
        return _gas_bs() == BST_PP_SKIN
    except Exception:
        return False


def booster_pp_knob_geom(rack_x, rack_y, rw, rh):
    """(cx, cy, radius, hit_radius) of the packApunch booster knob in screen space, or None
    when the packApunch skin is not active."""
    if not _bst_pp_active():
        return None
    sx, sy = rw / BST_PP_ART_W, rh / BST_PP_ART_H
    sr = min(sx, sy)
    return (rack_x + BST_PP_KNOB_POS[0] * sx,
            rack_y + (1.0 - BST_PP_KNOB_POS[1] / BST_PP_ART_H) * rh,
            BST_PP_KNOB_SIZE[0] * 0.5 * sr, BST_PP_HIT_R * sr)


def _bst_pp_draw_knob(rx, ry, rw, rh, boost_norm, boost_str, power=1.0):
    """packApunch knob: lit sprite turned to the value, plus the live readout. Returns True if drawn."""
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_k, blit_texture_rotated as _blt_rot
        g = booster_pp_knob_geom(rx, ry, rw, rh)
        if not g:
            return False
        key = BST_PP_KNOB_KEY
        tex = _gtc_k(key)
        if not tex:
            return False
        sx, sy = rw / BST_PP_ART_W, rh / BST_PP_ART_H
        v = max(0.0, min(1.0, boost_norm))
        ang = math.radians((0.5 - v) * BST_PP_SWEEP + BST_PP_KNOB_LINE)
        _blt_rot(tex, g[0], g[1], BST_PP_KNOB_SIZE[0] * sx, BST_PP_KNOB_SIZE[1] * sy,
                 ang, blend="ALPHA_PREMULT", key=key)
        fs = max(1, int(BST_PP_VALUE_PX * rh / BST_PP_ART_H))
        vx = rx + BST_PP_VALUE_POS[0] * sx
        vy = ry + (1.0 - BST_PP_VALUE_POS[1] / BST_PP_ART_H) * rh
        tw = _text_width(boost_str, fs)
        _draw_text(boost_str, vx - tw / 2, vy - fs / 2, fs,
                   (BST_PP_VALUE_COL[0], BST_PP_VALUE_COL[1], BST_PP_VALUE_COL[2], BST_PP_VALUE_COL[3] * power))
        return True
    except Exception as _e_bk:
        print(f"[BOOSTER] knob draw error: {_e_bk}")
        return False


# =============================================================================
# PACKAPUNCH SKIN (CHIMICHANGA / "The Booster") - live parts placed in ART pixels
# Everything below is in rack_booster_bg.png space (1988 x 528), y measured from the TOP.
# The art itself carries no live text: the meter cells, the preset name, the output channel,
# the free-channel list, the status word, the rack number and the keypad digits are all drawn here.
# =============================================================================
BST_PP_METER_X0    = 637.0                  # left edge of cell 0
BST_PP_METER_PITCH = (1057.0 - 637.0) / 18.0
BST_PP_METER_CELLS = 31
BST_PP_METER_CW    = 14.0                   # cell width
BST_PP_IN_Y        = (138.0, 180.0)         # IN meter cell row (y0, y1)
BST_PP_OUT_Y       = (218.0, 260.0)         # OUT meter cell row
BST_PP_DB_IN       = (1338.0, 131.0)        # centre of the little dB read-out (IN)
BST_PP_DB_OUT      = (1338.0, 212.0)
BST_PP_DB_PX       = 15.0
BST_PP_PRESET_BTNS = [                      # (rect x0,y0,x1,y1, boost_norm); top row +24 +30 +40, bottom row +6 +12 +18
    ((612.0, 295.0, 840.0, 357.0), 24 / 40.0),
    ((856.0, 295.0, 1144.0, 357.0), 30 / 40.0),
    ((1159.0, 295.0, 1397.0, 357.0), 1.0),
    ((612.0, 372.0, 840.0, 436.0), 6 / 40.0),
    ((856.0, 372.0, 1144.0, 436.0), 12 / 40.0),
    ((1159.0, 372.0, 1397.0, 436.0), 18 / 40.0),
]
BST_PP_APPLY       = (614.0, 452.0, 1274.0, 507.0)
BST_PP_LIMIT       = (1290.0, 459.0, 1394.0, 503.0)
BST_PP_LIMIT_BAR   = (1376.0, 458.0, 1396.0, 505.0)   # patch rects (match the PNGs)
BST_PP_LIMIT_X     = (1316.0, 461.0, 1362.0, 502.0)
BST_PP_STEPPER     = (1426.0, 406.0, 1905.0, 444.0)   # output channel box (the - and + are baked at its ends)
BST_PP_FREE_POS    = (1434.0, 455.0)        # left edge / centre line of the free-channel list
BST_PP_FREE_PX     = 12.0
BST_PP_STATUS_DOT  = (1440.0, 508.0, 7.0)
BST_PP_STATUS_POS  = (1456.0, 508.0)
BST_PP_STATUS_PX   = 15.0
BST_PP_CREAM       = (0.90, 0.87, 0.80, 1.0)
BST_PP_AMBER       = (0.96, 0.66, 0.26, 1.0)


# The bullet casings round the knob: (clock angle in degrees from straight up, x, y of its 36 x 36 cell in the art),
# in the order of the cells in rack_booster_bullets_lit.png (8 cells per row). The atlas holds ONLY the casings' own pixels
# (cut out of the art as a mask, in a lit colour), so lighting one lights exactly that casing and nothing around it.
BST_PP_BULLET_CELL = 36
BST_PP_BULLET_COLS = 8
BST_PP_BULLET_ATLAS = 288.0
BST_PP_BULLET_CELLS = [
    (-174.4, 280, 408),
    (-168.1, 264, 404),
    (-161.9, 248, 401),
    (-155.3, 233, 393),
    (-149.1, 219, 386),
    (-142.7, 204, 377),
    (-136.6, 191, 368),
    (-130.1, 180, 355),
    (-123.9, 171, 342),
    (-117.6, 164, 327),
    (-111.2, 157, 312),
    (-104.6, 153, 295),
    (-98.6, 151, 280),
    (-91.9, 150, 263),
    (-85.6, 151, 247),
    (-79.2, 153, 231),
    (-73.0, 157, 216),
    (-66.5, 163, 201),
    (-60.4, 170, 187),
    (-54.1, 178, 174),
    (-47.6, 188, 162),
    (-41.5, 199, 150),
    (-35.6, 211, 141),
    (-29.4, 224, 132),
    (-23.3, 238, 126),
    (-17.2, 252, 120),
    (-11.3, 266, 117),
    (-5.4, 281, 114),
    (0.6, 296, 114),
    (6.6, 311, 114),
    (12.6, 326, 117),
    (18.4, 341, 120),
    (24.3, 355, 126),
    (30.5, 369, 132),
    (36.5, 382, 141),
    (42.5, 394, 150),
    (48.6, 404, 162),
    (54.6, 414, 174),
    (60.8, 423, 187),
    (67.0, 430, 201),
    (73.3, 436, 216),
    (79.4, 440, 231),
    (85.6, 442, 247),
    (92.0, 443, 264),
    (98.3, 441, 280),
    (104.5, 439, 296),
    (110.7, 435, 312),
    (117.0, 429, 327),
    (123.0, 422, 341),
    (129.0, 413, 354),
    (135.5, 402, 368),
    (141.5, 390, 378),
    (147.8, 376, 388),
    (153.9, 362, 396),
    (159.9, 346, 399),
    (166.2, 330, 403),
    (172.4, 315, 407),
    (179.1, 297, 408),
]
# Preset-button glow layer: the six button faces + lamps cut out as masks, in the lit colour (added on top, so only
# the button shape lights up). Origin and size of that picture in art px:
BST_PP_GLOW_RECT = (600.0, 285.0, 1410.0, 445.0)
BST_PP_FADE = 14.0                                      # how fast lights fade in / out (per second)

_bst_levels = {}


def _bst_level(key, target, now=None):
    """Smoothly eased 0..1 light level for one lamp (so things switch on / off instead of snapping)."""
    now = time.monotonic() if now is None else now
    lv, t0 = _bst_levels.get(key, (target, now))
    dt = max(0.0, min(0.25, now - t0))
    lv += (target - lv) * min(1.0, dt * BST_PP_FADE)
    if abs(lv - target) < 0.004:
        lv = target
    _bst_levels[key] = (lv, now)
    return lv


class _BstG:
    """Art-pixel -> screen mapping for one rack draw."""
    def __init__(self, rx, ry, rw, rh):
        self.rx, self.ry, self.rw, self.rh = rx, ry, rw, rh
        self.sx, self.sy = rw / BST_PP_ART_W, rh / BST_PP_ART_H

    def X(self, ax):
        return self.rx + ax * self.sx

    def Y(self, ay):
        return self.ry + (1.0 - ay / BST_PP_ART_H) * self.rh

    def rect(self, r):
        return (self.X(r[0]), self.Y(r[3]), self.X(r[2]), self.Y(r[1]))   # x0, y0(bottom), x1, y1(top)


def booster_pp_active():
    return _bst_pp_active()


def _bst_in(r, mx, my):
    return r[0] <= mx <= r[2] and r[1] <= my <= r[3]


def booster_pp_hit(mx, my, rack_x, rack_y, rw, rh):
    """Click zones of the packApunch booster layout (same zone names as the old layout), or None."""
    g = _BstG(rack_x, rack_y, rw, rh)
    if _bst_in(g.rect(BST_PP_APPLY), mx, my):
        return {'zone': 'booster_apply'}
    if _bst_in(g.rect(BST_PP_LIMIT), mx, my):
        return {'zone': 'booster_limiter'}
    for r, norm in BST_PP_PRESET_BTNS:
        if _bst_in(g.rect(r), mx, my):
            return {'zone': 'booster_preset', 'boost_norm': norm}
    st = g.rect(BST_PP_STEPPER)
    if _bst_in(st, mx, my):
        third = (st[2] - st[0]) / 6.0
        if mx <= st[0] + third:
            return {'zone': 'booster_ch_minus'}
        if mx >= st[2] - third:
            return {'zone': 'booster_ch_plus'}
    return None


def _bst_text(text, x, cy, fs_art, g, col, align="l", glow=0.0, glow_col=None, max_w=None):
    """Text at x (left / centre / right by align), vertically centred on cy (screen px)."""
    if not text:
        return 0.0
    try:
        from ui.racks.pp_text import draw_text as _pdt, text_width as _ptw
    except Exception:
        _pdt = _ptw = None
    fs = max(1, int(round(fs_art * g.sy)))
    tw = _ptw(text, fs) if _ptw else _text_width(text, fs)
    if max_w is not None and tw > max_w > 0:
        fs = max(1, int(fs * max_w / tw))
        tw = _ptw(text, fs) if _ptw else _text_width(text, fs)
    if align == "c":
        x -= tw / 2
    elif align == "r":
        x -= tw
    y = cy - fs * 0.36
    if _pdt:
        _pdt(text, x, y, fs, col, glow=glow, glow_col=(glow_col or col))
    else:
        _draw_text(text, x, y, fs, col)
    return tw


def _bst_quad(sh, pts, col, blend="ALPHA"):
    gpu.state.blend_set(blend)
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": [pts[0], pts[1], pts[2], pts[0], pts[2], pts[3]]}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _bst_disc(sh, cx, cy, rx_, ry_, col, blend="ALPHA", seg=24):
    pts = []
    for i in range(seg):
        a0 = 2 * math.pi * i / seg
        a1 = 2 * math.pi * (i + 1) / seg
        pts += [(cx, cy), (cx + rx_ * math.cos(a0), cy + ry_ * math.sin(a0)),
                (cx + rx_ * math.cos(a1), cy + ry_ * math.sin(a1))]
    gpu.state.blend_set(blend)
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": pts}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _bst_patch(g, key, art_rect, alpha=1.0):
    try:
        from ui.mixer.texture_cache import get_texture as _gt, blit_texture as _bt
        tex = _gt(key)
        if tex is None:
            return False
        r = g.rect(art_rect)
        _bt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], alpha=alpha, key=key)
        return True
    except Exception:
        return False


def _bst_db_pos(v):
    """Meter position 0..1 for a linear level: a dB scale (-60 .. 0) moves like a real meter; linear would sit in the
    first few cells for speech."""
    if v <= 0.0001:
        return 0.0
    return max(0.0, min(1.0, (20.0 * math.log10(min(v, 1.0)) + 60.0) / 60.0))


BST_PP_METER_FALL = 0.40        # seconds for the cells to drain to zero when the rack is switched off
BST_PP_METER_UP   = 0.45        # seconds for the sweep up to full when the rack is switched on
BST_PP_METER_DOWN = 0.70        # seconds to settle back from full to the real level

_bst_meter_state = {}


def _bst_ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _bst_meter_disp(rack_idx, enabled, name, live, now):
    """Displayed meter position for one meter. Off: drains to zero. Switch-on: sweeps up to full, then falls back to
    the real level, then follows it live. Returns (position 0..1, phase) with phase 'off' / 'up' / 'down' / 'live'."""
    st = _bst_meter_state.get(rack_idx)
    if st is None:                                   # first sight: no animation
        st = {"en": bool(enabled), "t0": now - 999.0, "start": {}, "last": {}}
        _bst_meter_state[rack_idx] = st
    if bool(enabled) != st["en"]:
        st["en"], st["t0"] = bool(enabled), now
        st["start"] = dict(st["last"])               # where each meter was at the moment of the switch
    t = now - st["t0"]
    s0 = st["start"].get(name, 0.0)
    if not st["en"]:
        pos = s0 * max(0.0, 1.0 - t / BST_PP_METER_FALL)
        phase = "off"
    elif t < BST_PP_METER_UP:
        pos = s0 + (1.0 - s0) * _bst_ease(t / BST_PP_METER_UP)
        phase = "up"
    elif t < BST_PP_METER_UP + BST_PP_METER_DOWN:
        pos = 1.0 + (live - 1.0) * _bst_ease((t - BST_PP_METER_UP) / BST_PP_METER_DOWN)
        phase = "down"
    else:
        pos = live
        phase = "live"
    st["last"][name] = pos
    return pos, phase


def _bst_pp_meter(g, sh, y_rng, pos, peak_pos, is_out, db_pos, db_str, db_level):
    """Lit cells over the dimmed baked cell row (pos 0..1), plus the dB read-out (db_str '' = none)."""
    n = BST_PP_METER_CELLS
    lit = int(pos * n + 0.5)
    pk = int(peak_pos * n) if (peak_pos is not None and peak_pos > 0.0) else -1
    for i in range(min(lit, n)):
        f = i / float(n - 1)
        if f > 0.90:
            c = (1.00, 0.30, 0.22, 0.97)
        elif f > 0.70:
            c = (1.00, 0.62, 0.28, 0.97)
        else:
            c = (0.82, 0.46, 0.41, 0.97) if not is_out else (0.95, 0.58, 0.40, 0.97)
        x0 = BST_PP_METER_X0 + BST_PP_METER_PITCH * i
        r = g.rect((x0, y_rng[0], x0 + BST_PP_METER_CW, y_rng[1]))
        _bst_quad(sh, [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])], c)
        _bst_quad(sh, [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])], (0.35, 0.10, 0.06, 0.20), "ADDITIVE")
    if 0 <= pk < n and pk >= lit:
        x0 = BST_PP_METER_X0 + BST_PP_METER_PITCH * pk
        r = g.rect((x0, y_rng[0], x0 + BST_PP_METER_CW, y_rng[1]))
        _bst_quad(sh, [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])], (1.0, 0.85, 0.7, 0.55), "ADDITIVE")
    if db_str:
        col = _GREEN if db_level < 0.7 else (_AMBER if db_level < 0.9 else _RED)
        _bst_text(db_str, g.X(db_pos[0]), g.Y(db_pos[1]), BST_PP_DB_PX, g, (col[0], col[1], col[2], 0.95), "c")


def _bst_pp_bullets(g, sh, boost_norm, power=1.0):
    """Light the baked bullet casings (just their own shapes) from the knob's minimum up to its value."""
    try:
        from ui.mixer.texture_cache import get_texture as _gt, blit_texture_tinted as _btt
        tex = _gt("rack_booster_bullets_lit")
    except Exception:
        return
    if tex is None:
        return
    v = max(0.0, min(1.0, boost_norm))
    target = (v - 0.5) * BST_PP_SWEEP
    half = BST_PP_SWEEP / 2.0 + 2.0
    cell, cols, at = BST_PP_BULLET_CELL, BST_PP_BULLET_COLS, BST_PP_BULLET_ATLAS
    for i, (clk, ox, oy) in enumerate(BST_PP_BULLET_CELLS):
        if abs(clk) > half:
            continue
        k = max(0.0, min(1.0, (target - clk) / 3.0 + 0.5))
        if v <= 0.0:
            k = 0.0
        k *= power
        if k <= 0.004:
            continue
        row, col = divmod(i, cols)
        uv = (col * cell / at, 1.0 - (row + 1) * cell / at, (col + 1) * cell / at, 1.0 - row * cell / at)
        _btt(tex, g.X(ox), g.Y(oy + cell), cell * g.sx, cell * g.sy, color=(k, k, k, k),
             blend="ALPHA_PREMULT", uv=uv, key="rack_booster_bullets_lit")


def _bst_pp_btn_glow(g, rect, k):
    """Switch a preset button on: its own lit face + lamp (cut from the art) added over it at level k."""
    if k <= 0.004:
        return
    try:
        from ui.mixer.texture_cache import get_texture as _gt, blit_texture_tinted as _btt
        tex = _gt("rack_booster_btn_glow")
        if tex is None:
            return
        gx0, gy0, gx1, gy1 = BST_PP_GLOW_RECT
        gw, gh = gx1 - gx0, gy1 - gy0
        pad = 3.0
        x0, y0, x1, y1 = rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad
        uv = ((x0 - gx0) / gw, 1.0 - (y1 - gy0) / gh, (x1 - gx0) / gw, 1.0 - (y0 - gy0) / gh)
        r = g.rect((x0, y0, x1, y1))
        _btt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], color=(k, k, k, k), blend="ADDITIVE_PREMULT",
             uv=uv, key="rack_booster_btn_glow")
    except Exception:
        pass


def _bst_pp_draw_body(rx, ry, rw, rh, rack, rack_idx, scale):
    """Live parts of the packApunch booster. Returns True when drawn (the bg + knob + keypad + rail are drawn elsewhere)."""
    g = _BstG(rx, ry, rw, rh)
    sh = _get_shader()
    boost_norm = getattr(rack, 'p0', 0.30)
    limiter_on = getattr(rack, 'p1', 1.0) > 0.5
    target_ch = int(getattr(rack, 'p2', 0))
    boost_db = boost_norm * 40.0
    boost_str = f"+{boost_db:.1f} dB"
    gain_lin = 10.0 ** (boost_db / 20.0)
    status = getattr(rack, 'ai_status', 'READY')
    if status not in ('READY', 'PROCESSING', 'DONE', 'ERROR', 'NO_CHANNEL'):
        status = 'READY'
    enabled = getattr(rack, 'enabled', True)

    # skin background (full rack height - rail, title, rack-number plate and keypad frame are baked in)
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_bs, blit_texture as _blt_bs
        _tex = _gtc_bs("rack_booster_bg")
        if not _tex:
            return False
        _blt_bs(_tex, rx, ry, rw, rh, key="rack_booster_bg")
    except Exception:
        return False

    # knob + its value
    _now = time.monotonic()
    # the lights fade out with the rack, the same fade as the preset name (rack_base._pp_preset_level)
    try:
        from ui.racks.rack_base import _pp_preset_level as _ppl
        power = _ppl(rack_idx, enabled, _now)
    except Exception:
        power = 1.0 if enabled else 0.0
    _bst_pp_bullets(g, sh, boost_norm, power)
    _bst_pp_draw_knob(rx, ry, rw, rh, boost_norm, boost_str, power)

    # meters
    in_rms, in_peak = _get_live_levels(rack)
    out_rms = _tanh_limit(in_rms * gain_lin) if limiter_on else min(in_rms * gain_lin, 1.0)
    out_peak = _tanh_limit(in_peak * gain_lin) if limiter_on else min(in_peak * gain_lin, 1.0)
    pin, ph_in = _bst_meter_disp(rack_idx, enabled, "in", _bst_db_pos(in_rms), _now)
    pout, ph_out = _bst_meter_disp(rack_idx, enabled, "out", _bst_db_pos(out_rms), _now)
    pk_in = _bst_db_pos(in_peak) if (ph_in == "live" and in_peak > 0.001) else None
    pk_out = _bst_db_pos(out_peak) if (ph_out == "live" and out_peak > 0.001) else None
    _bst_pp_meter(g, sh, BST_PP_IN_Y, pin, pk_in, False, BST_PP_DB_IN,
                  _level_to_dbfs(in_rms) if ph_in == "live" else "", in_rms)
    _bst_pp_meter(g, sh, BST_PP_OUT_Y, pout, pk_out, True, BST_PP_DB_OUT,
                  _level_to_dbfs(out_rms) if ph_out == "live" else "", out_rms)

    # preset buttons: the one matching the current boost switches on (its own face and lamp light up, nothing else)
    for bi, (r, norm) in enumerate(BST_PP_PRESET_BTNS):
        _bst_pp_btn_glow(g, r, _bst_level((rack_idx, bi), 1.0 if abs(boost_norm - norm) < 0.015 else 0.0, _now) * power)

    # apply button: the baked art says APPLY BOOST; other states get a plate over the label
    ap = g.rect(BST_PP_APPLY)
    if status != 'READY':
        if status == 'PROCESSING':
            pulse = 0.5 + 0.5 * math.sin(time.time() * 5.0)
            plate, txt, col = (0.30, 0.05, 0.03, 1.0), "BOOSTING...", (1.0, 0.70 + 0.2 * pulse, 0.25, 1.0)
            _bst_quad(sh, [(ap[0], ap[1]), (ap[2], ap[1]), (ap[2], ap[3]), (ap[0], ap[3])], (1.0, 0.5, 0.2, 0.10 + 0.12 * pulse), "ADDITIVE")
        elif status == 'DONE':
            plate, txt, col = (0.05, 0.22, 0.09, 1.0), "DONE  (apply again)", _GREEN
        elif status == 'ERROR':
            plate, txt, col = (0.22, 0.03, 0.03, 1.0), "ERROR - RETRY", (1.0, 0.45, 0.35, 1.0)
        else:
            plate, txt, col = (0.20, 0.08, 0.04, 1.0), "NO CHANNEL", (0.85, 0.60, 0.30, 1.0)
        p = g.rect((BST_PP_APPLY[0] + 8.0, BST_PP_APPLY[1] + 7.0, BST_PP_APPLY[2] - 8.0, BST_PP_APPLY[3] - 7.0))
        _bst_quad(sh, [(p[0], p[1]), (p[2], p[1]), (p[2], p[3]), (p[0], p[3])], plate)
        _bst_text(txt, (ap[0] + ap[2]) / 2, (ap[1] + ap[3]) / 2, 26.0, g, col, "c")

    # limiter: green bar = limiter on, red cross = off (full send)
    if limiter_on:
        _bst_patch(g, "rack_booster_limit_bar", BST_PP_LIMIT_BAR)
    else:
        _bst_patch(g, "rack_booster_limit_x", BST_PP_LIMIT_X)

    # output channel + free channels + status
    st = g.rect(BST_PP_STEPPER)
    ch_display = f"ch {target_ch}" if target_ch > 0 else "auto"
    _bst_text(ch_display, (st[0] + st[2]) / 2, (st[1] + st[3]) / 2, 24.0, g, BST_PP_AMBER, "c", glow=0.25, glow_col=(1.0, 0.55, 0.15, 1.0))
    free = _get_free_channels()
    if free:
        _bst_text("free: " + " ".join(str(c) for c in free), g.X(BST_PP_FREE_POS[0]), g.Y(BST_PP_FREE_POS[1]),
                  BST_PP_FREE_PX, g, (0.92, 0.66, 0.36, 0.95), "l")
    dot_cols = {'READY': (0.96, 0.72, 0.22, 1.0), 'PROCESSING': (1.0, 0.80, 0.25, 1.0), 'DONE': (0.20, 0.92, 0.40, 1.0),
                'ERROR': (1.0, 0.25, 0.20, 1.0), 'NO_CHANNEL': (0.45, 0.40, 0.38, 1.0)}
    dc = dot_cols.get(status, dot_cols['READY'])
    dx_, dy_, dr_ = BST_PP_STATUS_DOT
    _bst_disc(sh, g.X(dx_), g.Y(dy_), dr_ * g.sx, dr_ * g.sy, dc)
    _bst_disc(sh, g.X(dx_), g.Y(dy_), dr_ * 1.9 * g.sx, dr_ * 1.9 * g.sy, (dc[0], dc[1], dc[2], 0.16), "ADDITIVE")
    _bst_text({'PROCESSING': 'BUSY', 'NO_CHANNEL': 'NO CH'}.get(status, status), g.X(BST_PP_STATUS_POS[0]), g.Y(BST_PP_STATUS_POS[1]), BST_PP_STATUS_PX, g,
              (dc[0], dc[1], dc[2], 0.95), "l")
    return True


# =============================================================================
# BOOSTER LAYOUT TUNING — all values unscaled px, multiplied by scale at draw time.
# Every section has fully INDEPENDENT position + size constants.
# Nothing derives from another section — change one without affecting others.
# Y values are measured from body_bot (bottom of rack body area).
# Positive Y = up. Positive X = right.
# Set any value to 0 to use the computed default.
# =============================================================================
# BOOSTER LAYOUT TUNING — all values unscaled px, same pattern as other racks.
# Positive = right/up/bigger. Negative = left/down/smaller.
# =============================================================================
BST_KNOB_SCALE = 0.75
BST_KNOB_X     = -12.5
BST_KNOB_Y     = 0.0
BST_KNOB_VAL_Y = -40.0   # move +6.0 dB text down (negative) or up (positive)
BST_KNOB_VAL_X = 10.0     # move +6.0 dB text left (negative) or right (positive)
BST_KNOB_FS    = 12.0    # font size for +6.0 dB text (0 = default 8pt)

# IN meter bar — position and size within the centre column
BST_IN_X       = 30.0   # nudge left/right
BST_IN_Y       = 3.0   # nudge up/down
BST_IN_W       = -62.0   # adjust width  (negative = narrower)
BST_IN_H       = -20.0   # adjust height (negative = shorter)

# OUT meter bar
BST_OUT_X      = 30.0
BST_OUT_Y      = 9.5
BST_OUT_W      = -62.0
BST_OUT_H      = -20.0

# LED meter constants (booster_Green__LED.png / booster_Orange__LED.png, 17x43px each)
# LEDs tile horizontally across the meter bar.
BST_LED_GAP      = 2.0    # gap between each LED in unscaled px
BST_LED_W        = 0.0    # LED width override in unscaled px (0 = auto-fit to bar height)
# IN meter LED position nudge
BST_IN_LED_X     = 0.0
BST_IN_LED_Y     = 0.0
# OUT meter LED position nudge
BST_OUT_LED_X    = 0.0
BST_OUT_LED_Y    = 0.0
BST_GRID_X     = 27.0   # nudge entire grid left/right
BST_GRID_Y     = -18.0   # nudge entire grid up/down
BST_GRID_W     = -50.0   # adjust total grid width
BST_GRID_H     = 15.0   # adjust total grid height
# Active preset button PNG (booster__button.png, 282x70px)
BST_BTN_ON_SCALE = 1.05   # width multiplier (1.0 = matches button area exactly)
BST_BTN_ON_SCALE_H = 1.2  # height multiplier — adjust independently of width
BST_BTN_ON_X     = 0.0   # nudge left/right (unscaled px)
BST_BTN_ON_Y     = 0.0   # nudge up/down   (unscaled px)
# Per-button nudges — [+6, +12, +18, +24, +30, +40]
BST_BTN_X      = [-3.0, 2.0, 7.0, -3.0, 2.0, 7.0]
BST_BTN_Y      = [0.0, 0.0, 0.0, 6.0, 6.0, 6.0]
BST_BTN_W      = [-7.0, -7.0, -7.0, -7.0, -7.0, -7.0]  # per-button width adjust
BST_BTN_H      = [-5.0, -5.0, -5.0, -5.0, -5.0, -5.0]  # per-button height adjust

# Apply + Limiter row
BST_APPLY_X    = 23.0
BST_APPLY_Y    = 8.0
BST_APPLY_W    = -55.0
BST_APPLY_H    = -8.0
BST_LIM_W      = 85.0  # limiter button width (unscaled px)
BST_LIM_X      = 9.0   # nudge limiter button left/right (relative to its auto position)
BST_LIM_Y      = -5.0   # nudge limiter button up/down
BST_LIM_H      = 8.0   # adjust limiter button height independently of apply button

# Output channel stepper
BST_STEPPER_X  = -5
BST_STEPPER_Y  = 6.0
BST_STEPPER_W  = 6.0   # adjust stepper width
BST_STEPPER_H  = -6.0     # adjust stepper height

# Font sizes — unscaled pt, multiplied by scale at draw time
BST_FS_GRID    = 12.0    # preset grid button labels (+6, +12 etc.)
BST_FS_APPLY   = 15.0    # apply button text (▶ APPLY BOOST etc.)
BST_FS_LIM     = 14.0    # limiter button text (LIMIT: ON / LIMIT: OFF)
BST_FS_STEPPER = 12.0    # channel stepper display (auto / ch N)
BST_FS_STEPPER_LBL = 7.0  # "Output ch" label above stepper

# Channel buttons drawn by rack_base occupy approx the top 110px of body (at scale=1)
# Our right column must stay below: body_top - 115*scale
_CH_CLEAR_TOP = 115   # px at scale=1 to keep clear from body_top downward

_BG        = (0.05,  0.04,  0.02,  1.0)
_BORDER    = (0.35,  0.22,  0.04,  1.0)
_AMBER     = (0.95,  0.60,  0.08,  1.0)
_AMBER_DIM = (0.40,  0.25,  0.04,  1.0)
_GREEN     = (0.15,  0.85,  0.35,  1.0)
_RED       = (0.90,  0.15,  0.10,  1.0)
_TEXT_DIM  = (0.45,  0.32,  0.08,  1.0)
_METER_BG  = (0.03,  0.02,  0.01,  1.0)


def _tanh_limit(x):
    if abs(x) <= 1.0:
        return x
    s = 1.0 if x >= 0 else -1.0
    return s * math.tanh(abs(x))


def _level_to_dbfs(level):
    if level <= 0.0001:
        return "-inf"
    db = 20.0 * math.log10(max(level, 0.0001))
    return f"{db:+.1f}"


def _draw_vu_bar(shader, bx, by, bw, bh, level, peak, is_output=False,
                 led_x_offset=0.0, led_y_offset=0.0):
    """Draw a horizontal LED meter bar using PNG LED tiles.

    LEDs tile left-to-right across the bar width. Unlit LEDs are skipped
    (transparent — the baked background shows through). Peak line kept.
    led_x_offset / led_y_offset: unscaled-px nudge already multiplied by scale.
    """
    # LED sizing — height fills the bar, width preserves the LED's 17:43 aspect ratio
    led_h = bh
    _led_w_auto = led_h * (17.0 / 43.0)   # 17x43 aspect ratio
    led_w = BST_LED_W if BST_LED_W > 0 else _led_w_auto
    led_gap = BST_LED_GAP   # already unscaled; caller multiplies by scale externally
    # Note: BST_LED_GAP is unscaled px — we need scale here, but _draw_vu_bar
    # doesn't receive scale. Use a module-level ref via the closure.
    # Instead, treat BST_LED_GAP as already-in-screen-px relative to current bh.
    # A simpler approach: gap is a fraction of led_w.
    stride = led_w + led_gap   # px per LED slot

    if stride <= 0:
        return

    n_leds = max(1, int(bw / stride))
    lit_count = int(min(level, 1.0) * n_leds)

    # Pick LED texture key based on meter type
    led_key = "rack_booster_led_orange" if is_output else "rack_booster_led_green"

    try:
        from ui.mixer.texture_cache import get_texture as _gtc_led
        from ui.mixer.texture_cache import blit_texture as _blt_led
        led_tex = _gtc_led(led_key)
    except Exception:
        led_tex = None

    _ox = bx + led_x_offset
    _oy = by + led_y_offset

    for i in range(lit_count):
        lx = _ox + i * stride
        if led_tex:
            _blt_led(led_tex, lx, _oy, led_w, led_h, key=led_key)
        else:
            # Fallback — solid colour rect
            col = (_RED if i / n_leds > 0.90 else
                   (_AMBER if i / n_leds > 0.70 else
                    ((0.90, 0.55, 0.05, 1.0) if is_output else (0.12, 0.70, 0.25, 1.0))))
            _draw_rect(lx, _oy, led_w, led_h, col)

    # Peak line — kept as-is
    if peak > 0.001:
        px = bx + min(peak, 1.0) * bw - 1
        peak_col = _RED if peak > 0.90 else (_AMBER if peak > 0.70 else _GREEN)
        pb = batch_for_shader(shader, "LINES",
                              {"pos": [(px, by+1), (px, by+bh-1)]})
        shader.bind(); shader.uniform_float("color", peak_col); pb.draw(shader)


def _get_live_levels(rack):
    try:
        import Racks as _rk
        assigned = list(_rk.get_rack_channels(rack))
        if not assigned:
            return 0.0, 0.0
        ch_idx = assigned[0]
        from core.meters import _engine_levels, _peak_hold
        rms  = _engine_levels[ch_idx] if ch_idx < len(_engine_levels) else 0.0
        peak = _peak_hold[ch_idx]     if ch_idx < len(_peak_hold)     else 0.0
        return rms, peak
    except Exception:
        return 0.0, 0.0


def _get_free_channels():
    try:
        from core import vse_compat as _vse
        scene = bpy.context.scene
        if not scene or not scene.sequence_editor:
            return []
        used = {s.channel for s in _vse.get_all_strips(scene.sequence_editor)
                if s.type == "SOUND" and s.sound}
        return [ch for ch in range(1, 33) if ch not in used][:5]
    except Exception:
        return []


def _draw_booster_body(rx, ry, rw, rh, rack, rack_idx, scale):
    if _bst_pp_active():
        try:
            if _bst_pp_draw_body(rx, ry, rw, rh, rack, rack_idx, scale):
                return
        except Exception as _e_bpp:
            print(f"[BOOSTER] packApunch draw error: {_e_bpp}")
    boost_norm = getattr(rack, 'p0', 0.30)
    limiter_on = getattr(rack, 'p1', 1.0) > 0.5
    target_ch  = int(getattr(rack, 'p2', 0))
    boost_db   = boost_norm * 40.0
    boost_str  = f"+{boost_db:.1f} dB"
    gain_lin   = 10.0 ** (boost_db / 20.0)

    status = getattr(rack, 'ai_status', 'READY')
    if status not in ('READY', 'PROCESSING', 'DONE', 'ERROR', 'NO_CHANNEL'):
        status = 'READY'

    in_rms, in_peak = _get_live_levels(rack)
    out_rms  = _tanh_limit(in_rms  * gain_lin) if limiter_on else min(in_rms  * gain_lin, 1.0)
    out_peak = _tanh_limit(in_peak * gain_lin) if limiter_on else min(in_peak * gain_lin, 1.0)

    rail_h   = RACK_RAIL_H * scale
    body_bot = ry
    body_top = ry + rh - rail_h
    body_h   = body_top - body_bot
    shader   = _get_shader()

    # ── Skin background — full rack height (rail/title now baked into PNG)
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_bs
        from ui.mixer.texture_cache import blit_texture as _blt_bs
        _bs_tex = _gtc_bs("rack_booster_bg")
        if _bs_tex:
            _blt_bs(_bs_tex, rx, ry, rw, rh, key="rack_booster_bg")
        else:
            _draw_rect(rx, body_bot, rw, body_h, _BG)
    except Exception:
        _draw_rect(rx, body_bot, rw, body_h, _BG)
    # Border suppressed — baked into background PNG

    # Original column widths — unchanged from first working version
    left_w   = rw * 0.25
    centre_w = rw * 0.50
    centre_x = rx + left_w
    right_x  = rx + left_w + centre_w

    # Dividers suppressed — baked into background PNG

    # ── LEFT: Big boost knob — original geometry ──────────────────────────────
    knob_cx = rx + left_w * 0.5 + BST_KNOB_X * scale
    knob_cy = body_bot + body_h * 0.52 + BST_KNOB_Y * scale
    knob_r  = min(left_w * 0.30, body_h * 0.35) * BST_KNOB_SCALE
    # "BOOST" label suppressed — baked into background PNG
    # Draw knob with empty label; value text drawn manually below for X control
    if not (_bst_pp_active() and _bst_pp_draw_knob(rx, ry, rw, rh, boost_norm, boost_str)):
      _draw_knob(knob_cx, knob_cy, knob_r,
               boost_norm, (_AMBER[0], _AMBER[1], _AMBER[2]),
               "", "", scale)
      # +6.0 dB value text — position and size controlled by BST_KNOB_VAL_* constants
      _fs_val = max(1, int((BST_KNOB_FS if BST_KNOB_FS > 0 else 8) * scale))
      _tw_val = _text_width(boost_str, _fs_val)
      _draw_text(boost_str,
               knob_cx - _tw_val/2 + BST_KNOB_VAL_X * scale,
               knob_cy - knob_r - _fs_val * 2 - 4*scale + BST_KNOB_VAL_Y * scale,
               _fs_val, (0.8, 0.8, 0.8, 1.0))

    # ── CENTRE + APPLY ────────────────────────────────────────────────────────
    # All elements baseline from centre_x/centre_w — same as other racks.
    # BST_* constants nudge/resize each element independently.
    _cx   = centre_x          # left edge of centre column
    _cw   = centre_w          # width of centre column
    _pad  = 8 * scale         # inner padding
    fs_lbl = max(1, int(7*scale))
    fs_db  = max(1, int(8*scale))

    # IN meter — sits in upper portion of body
    in_bx    = _cx + _pad            + BST_IN_X * scale
    in_bw    = _cw - _pad*2          + BST_IN_W * scale
    in_bh    = body_h * 0.15         + BST_IN_H * scale
    in_bar_y = body_bot + body_h*0.73 + BST_IN_Y * scale
    in_lbl_y = in_bar_y + in_bh + 2*scale

    # "IN" label suppressed — baked into background PNG
    in_db_str = _level_to_dbfs(in_rms)
    _draw_text(in_db_str, in_bx + in_bw - _text_width(in_db_str, fs_db),
               in_lbl_y, fs_db,
               _GREEN if in_rms < 0.7 else (_AMBER if in_rms < 0.9 else _RED))
    _draw_vu_bar(shader, in_bx, in_bar_y, in_bw, in_bh, in_rms, in_peak, False,
                 led_x_offset=BST_IN_LED_X*scale, led_y_offset=BST_IN_LED_Y*scale)

    # OUT meter — sits below IN meter
    out_bx    = _cx + _pad            + BST_OUT_X * scale
    out_bw    = _cw - _pad*2          + BST_OUT_W * scale
    out_bh    = body_h * 0.15         + BST_OUT_H * scale
    out_bar_y = body_bot + body_h*0.54 + BST_OUT_Y * scale
    out_lbl_y = out_bar_y + out_bh + 2*scale

    # "OUT (est.)" label suppressed — baked into background PNG
    out_db_str = _level_to_dbfs(out_rms)
    _draw_text(out_db_str, out_bx + out_bw - _text_width(out_db_str, fs_db),
               out_lbl_y, fs_db,
               _GREEN if out_rms < 0.7 else (_AMBER if out_rms < 0.9 else _RED))
    _draw_vu_bar(shader, out_bx, out_bar_y, out_bw, out_bh, out_rms, out_peak, True,
                 led_x_offset=BST_OUT_LED_X*scale, led_y_offset=BST_OUT_LED_Y*scale)

    # Preset grid — 6 buttons 3 cols x 2 rows, sits between meters and apply row
    grid_x   = _cx + _pad            + BST_GRID_X * scale
    grid_w   = _cw - _pad*2          + BST_GRID_W * scale
    grid_bot = body_bot + body_h*0.28 + BST_GRID_Y * scale
    grid_h   = body_h * 0.22         + BST_GRID_H * scale
    _btn_w_base = (grid_w - 2*3*scale) / 3
    _btn_h_base = (grid_h - 3*scale)   / 2
    fs_pre   = max(1, int(BST_FS_GRID*scale))
    presets  = [("+6",  6/40), ("+12", 12/40), ("+18", 18/40),
                ("+24", 24/40), ("+30", 30/40), ("+40", 1.0)]
    for pi, (lbl, norm) in enumerate(presets):
        col_i = pi % 3
        row_i = pi // 3
        btn_w = _btn_w_base + BST_BTN_W[pi] * scale
        btn_h = _btn_h_base + BST_BTN_H[pi] * scale
        bx_p  = grid_x + col_i * (_btn_w_base + 3*scale) + BST_BTN_X[pi] * scale
        by_p  = grid_bot + row_i * (_btn_h_base + 3*scale) + BST_BTN_Y[pi] * scale
        active = abs(boost_norm - norm) < 0.015
        col_p = _AMBER if active else _AMBER_DIM
        # Active state — blit PNG behind text
        if active:
            try:
                from ui.mixer.texture_cache import get_texture as _gtc_bb
                from ui.mixer.texture_cache import blit_texture as _blt_bb
                _bb_tex = _gtc_bb("rack_booster_btn_on")
                if _bb_tex:
                    _bb_w = btn_w * BST_BTN_ON_SCALE
                    _bb_h = btn_h * BST_BTN_ON_SCALE_H
                    _bb_x = bx_p + (btn_w - _bb_w)/2 + BST_BTN_ON_X * scale
                    _bb_y = by_p + (btn_h - _bb_h)/2 + BST_BTN_ON_Y * scale
                    _blt_bb(_bb_tex, _bb_x, _bb_y, _bb_w, _bb_h, key="rack_booster_btn_on")
            except Exception:
                pass
        # label text on top
        tw = _text_width(lbl, fs_pre)
        _draw_text(lbl, bx_p+btn_w/2-tw/2, by_p+btn_h/2-fs_pre/2, fs_pre, col_p)

    # Apply + Limiter row — sits at bottom of centre column
    apply_x  = _cx + _pad            + BST_APPLY_X * scale
    apply_y  = body_bot + body_h*0.06 + BST_APPLY_Y * scale
    ctrl_h   = body_h * 0.10         + BST_APPLY_H * scale
    lim_w    = BST_LIM_W * scale
    apply_w  = _cw - _pad*2          + BST_APPLY_W * scale - lim_w - 6*scale
    lim_x    = apply_x + apply_w + 6*scale + BST_LIM_X * scale
    lim_y    = apply_y                      + BST_LIM_Y * scale
    lim_h    = ctrl_h                       + BST_LIM_H * scale

    # Apply button
    if status == 'PROCESSING':
        pulse  = 0.5 + 0.5 * math.sin(time.time() * 5.0)
        ap_bg  = (0.18, 0.10, 0.01, 1.0)
        ap_col = (_AMBER[0], _AMBER[1]*0.5 + 0.5*pulse, _AMBER[2], 1.0)
        ap_lbl = "BOOSTING..."
    elif status == 'DONE':
        ap_bg  = (0.02, 0.12, 0.05, 1.0); ap_col = _GREEN; ap_lbl = "DONE  (apply again)"
    elif status == 'ERROR':
        ap_bg  = (0.14, 0.02, 0.02, 1.0); ap_col = _RED;   ap_lbl = "ERROR — RETRY"
    elif status == 'NO_CHANNEL':
        ap_bg  = (0.08, 0.06, 0.01, 1.0); ap_col = _AMBER_DIM; ap_lbl = "NO CHANNEL"
    else:
        ap_bg  = (0.15, 0.09, 0.01, 1.0); ap_col = _AMBER; ap_lbl = "▶  APPLY BOOST"

    # rect and border suppressed — baked into background PNG
    fs_ap = max(1, int(BST_FS_APPLY*scale))
    tw_ap = _text_width(ap_lbl, fs_ap)
    _draw_text(ap_lbl, apply_x+apply_w/2-tw_ap/2, apply_y+ctrl_h/2-fs_ap/2, fs_ap, ap_col)

    # Limiter toggle
    lim_bg  = (0.02, 0.10, 0.04, 1.0) if limiter_on else (0.08, 0.05, 0.01, 1.0)
    lim_col = _GREEN if limiter_on else _AMBER_DIM
    lim_lbl = "LIMIT: ON" if limiter_on else "LIMIT: OFF"
    # rect and border suppressed — baked into background PNG
    fs_lim = max(1, int(BST_FS_LIM*scale))
    tw_lim = _text_width(lim_lbl, fs_lim)
    _draw_text(lim_lbl, lim_x+lim_w/2-tw_lim/2, lim_y+lim_h/2-fs_lim/2, fs_lim, lim_col)

    # ── RIGHT: Output channel stepper ────────────────────────────────────────
    _rpad  = 6 * scale
    _rw_base = left_w - _rpad * 2
    rw2    = _rw_base             + BST_STEPPER_W * scale
    rx2    = right_x + _rpad      + BST_STEPPER_X * scale
    fs_r   = max(1, int(BST_FS_STEPPER_LBL*scale))
    fs_ch  = max(1, int(BST_FS_STEPPER*scale))
    stepper_h = body_h * 0.08 + BST_STEPPER_H * scale
    stepper_y = apply_y + ctrl_h + 6*scale + BST_STEPPER_Y * scale
    ch_lbl_y   = stepper_y + stepper_h + 3*scale

    # "Output ch" label suppressed — baked into background PNG

    # Stepper box, border and +/- suppressed — baked into background PNG
    ch_display = f"ch {target_ch}" if target_ch > 0 else "auto"
    _draw_text(ch_display,
               rx2 + rw2/2 - _text_width(ch_display, fs_ch)/2,
               stepper_y + stepper_h/2 - fs_ch/2, fs_ch, _AMBER)

    # Free channels hint below stepper
    free = _get_free_channels()
    if free:
        free_str = "free: " + " ".join(str(c) for c in free)
        _draw_text(free_str, rx2, stepper_y - fs_r - 3*scale, fs_r, _TEXT_DIM)

    # Status dot at bottom of right column
    dot_cols = {
        'READY':      _AMBER_DIM, 'PROCESSING': _AMBER,
        'DONE':       _GREEN,     'ERROR':      _RED,
        'NO_CHANNEL': (0.25, 0.25, 0.25, 1.0),
    }
    status_y = body_bot + 4*scale + 13*scale
    _draw_circle(rx2 + 4*scale, status_y, 4*scale, dot_cols.get(status, _AMBER_DIM))
    st_lbl = status.replace('_', ' ')
    _draw_text(st_lbl, rx2 + 12*scale, status_y - fs_r/2, fs_r,
               dot_cols.get(status, _AMBER_DIM))