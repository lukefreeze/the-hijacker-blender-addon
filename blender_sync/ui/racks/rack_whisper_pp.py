# =============================================================================
# ui/racks/rack_whisper_pp.py
# packApunch layout for the Whisper speech-to-text rack ("Cyberdyne" red skin).
#
# The art (rack_whisper_bg.png, 1895x446) carries every key, button, screen and lamp at fixed positions.
# This module draws the live content on top (screen text, lit key / button states, toggles, status) and
# provides the click zones in ART PIXELS - the same approach as rack_demucs_pp.py / rack_voicefixer_pp.py.
#
# All coordinates are art pixels, y measured from the TOP of the art: (x0, y0, x1, y1).
# If something sits a little off in Blender, nudge its rectangle here.
#
# Extra art:
#   rack_whisper_bg_lit.png  premultiplied RGBA, same size as the art. Additive glows (lit keys, selected
#                            buttons, lit toggle slots, TRANSCRIBE rim pulse) sit at the same place as the
#                            thing they light; one opaque patch (TRANSCRIBE button with its label wiped) is
#                            stored at the bottom-left corner and blitted over the button while it is busy.
#   rack_whisper_bg_off.png  the whole rack with its lights OFF (dark screens, dark lenses, unlit logo and
#                            lettering). Blitted over the lit art at (1 - power) while the rack is off; the metal
#                            itself is identical in both, so it never dims.
# =============================================================================

import math
import time as _time

import gpu
from gpu_extras.batch import batch_for_shader

try:
    from ui.racks.rack_whisper import (_check_dep, get_system_fonts, _font_scroll, _MODEL_LABELS, _MODEL_DESCS,
                                       _LANG_LABELS)
except Exception:                                   # pragma: no cover
    _check_dep = lambda: True
    get_system_fonts = lambda: []
    _font_scroll = {}
    _MODEL_LABELS = ["TINY", "BASE", "SMALL", "MEDIUM", "LARGE-V3"]
    _MODEL_DESCS = ["39M  fastest, English only", "74M  good for English (default)", "244M  good multilingual",
                    "769M  high accuracy", "1550M  best accuracy, slow"]
    _LANG_LABELS = ["Auto-detect", "English", "German", "French", "Spanish", "Italian", "Japanese", "Chinese",
                    "Russian", "Portuguese", "Dutch", "Korean", "Arabic", "Hindi", "Polish", "Swedish"]

try:
    from ui.mixer.draw_utils import draw_text as _draw_text, text_width as _text_width
except Exception:                                   # pragma: no cover
    _draw_text = _text_width = None

WH_PP_SKIN = "packapunch"
WH_ART_W = 1895.0
WH_ART_H = 446.0
WH_LIT_KEY = "rack_whisper_bg_lit"
WH_OFF_KEY = "rack_whisper_bg_off"

# ── layout (art px) ──────────────────────────────────────────────────────────
WH_COLLAPSE = (112.0, 9.0, 148.0, 46.0)
WH_NUMBER = (153.0, 10.0, 189.0, 47.0)          # the red box next to the collapse key: shows the rack number (not a button)
WH_POWER = (1392.0, 13.0, 1428.0, 49.0)         # top plate, right of the channel keys: on / off key ...
WH_CLOSE = (1433.0, 13.0, 1469.0, 49.0)         # ... and the close-rack key next to it

WH_TOP_X0, WH_TOP_P, WH_TOP_W, WH_TOP_Y = 815.0, 31.4, 22.0, (21.0, 44.0)      # nine channel keys across the top
WH_KEY_X0, WH_KEY_P, WH_KEY_W = 113.0, 36.0, 28.0                              # input / output channel keys
WH_KEY_IN_Y = (76.0, 104.0)
WH_KEY_OUT_Y = (140.0, 168.0)

WH_SCR = {
    'model': (160.0, 210.0, 598.0, 241.0),
    'chunk': (160.0, 290.0, 598.0, 322.0),
    'font': (660.0, 77.0, 1150.0, 171.0),
    'size': (702.0, 206.0, 1140.0, 236.0),
    'lang': (1234.0, 79.0, 1757.0, 110.0),
    'srt': (1196.0, 304.0, 1795.0, 324.0),
    'status': (627.0, 389.0, 993.0, 413.0),
}
WH_ARR = {
    'model_l': (115.0, 208.0, 148.0, 245.0), 'model_r': (606.0, 208.0, 641.0, 245.0),
    'chunk_l': (115.0, 288.0, 148.0, 325.0), 'chunk_r': (606.0, 288.0, 641.0, 325.0),
    'font_u': (1157.0, 76.0, 1183.0, 122.0), 'font_d': (1157.0, 127.0, 1183.0, 173.0),
    'size_m': (660.0, 205.0, 693.0, 238.0), 'size_p': (1148.0, 205.0, 1183.0, 238.0),
    'lang_l': (1195.0, 77.0, 1229.0, 111.0), 'lang_r': (1765.0, 77.0, 1798.0, 112.0),
}
WH_ARR_ZONE = {
    'model_l': 'ai_wsp_model_prev', 'model_r': 'ai_wsp_model_next',
    'chunk_l': 'ai_wsp_chunk_dec', 'chunk_r': 'ai_wsp_chunk_inc',
    'font_u': 'ai_wsp_font_scroll_up', 'font_d': 'ai_wsp_font_scroll_dn',
    'size_m': 'ai_wsp_size_dec', 'size_p': 'ai_wsp_size_inc',
    'lang_l': 'ai_wsp_lang_prev', 'lang_r': 'ai_wsp_lang_next',
}
WH_STYLE = ((660.0, 267.0, 770.0, 302.0), (779.0, 267.0, 889.0, 302.0), (899.0, 267.0, 993.0, 302.0),
            (1004.0, 267.0, 1087.0, 302.0), (1095.0, 267.0, 1182.0, 302.0))
WH_STYLE_FLAGS = (1, 2, 4, 8, 16)
WH_POS = ((662.0, 333.0, 858.0, 370.0), (866.0, 333.0, 1057.0, 370.0), (1065.0, 333.0, 1182.0, 370.0))
WH_MODE = ((1194.0, 159.0, 1504.0, 195.0), (1510.0, 159.0, 1800.0, 195.0))
WH_TOG_VAD = (1745.0, 227.0, 1796.0, 251.0)
WH_TOG_SRT = (1745.0, 275.0, 1796.0, 298.0)
WH_SRT_BROWSE = (1757.0, 303.0, 1794.0, 325.0)  # small "..." key at the right end of the SRT screen
WH_TRANS = (1594.0, 377.0, 1797.0, 419.0)
WH_EYES = ((557.0, 56.0), (485.0, 380.0), (1465.0, 381.0))
WH_LOGO = (196.0, 4.0, 392.0, 56.0)

WH_FONT_ROWS = 6
WH_FONT_ROW_H = 14.5
WH_FONT_TOP = 81.0
WH_FONT_X0, WH_FONT_X1 = 664.0, 1138.0

# opaque patch in the lit atlas: TRANSCRIBE face with its label wiped (dest rect on the art, source rect in the atlas)
WH_P_TRANS_CLEAN = ((1604.0, 387.0, 1787.0, 411.0), (0.0, 400.0, 183.0, 424.0))

# regions where the OFF art differs from the lit art (rects in art px) - blitted at (1 - power)
WH_OFF_REGIONS = (
    tuple((r[0] - 5, r[1] - 5, r[2] + 5, r[3] + 5) for r in WH_SCR.values())
    + ((WH_NUMBER[0] - 3, WH_NUMBER[1] - 3, WH_NUMBER[2] + 3, WH_NUMBER[3] + 3),)
    + tuple((r[0] - 5, r[1] - 5, r[2] + 5, r[3] + 5) for r in (WH_LOGO, WH_COLLAPSE, WH_CLOSE, WH_POWER, WH_TRANS))
    + tuple((r[0] - 2, r[1] - 2, r[2] + 2, r[3] + 2) for r in (list(WH_ARR.values()) + list(WH_STYLE) + list(WH_POS)
                                                                + list(WH_MODE)))
    + tuple((cx - 34, cy - 30, cx + 34, cy + 30) for (cx, cy) in WH_EYES)
)

# ── text sizes (art px) and colours ──────────────────────────────────────────
WH_FS_BIG = 21.0
WH_FS_VAL = 17.0                                # model / segment / language value text
WH_FS_SCR = 16.0
WH_FS_SMALL = 14.0
WH_FS_KEY = 17.0
WH_FS_KEY_TOP = 14.0
WH_FS_NUM = 34.0
WH_FS_LABEL = 19.0

WH_RED = (1.00, 0.43, 0.40, 1.00)               # phosphor text
WH_RED_DIM = (0.78, 0.24, 0.24, 0.95)
WH_CORE = (1.00, 0.84, 0.82, 1.00)              # hot white-pink
WH_GLOW = (1.00, 0.16, 0.16)
WH_GLOW_AMT = 0.9
WH_GLOW_SCALE = 0.5
WH_NOTE = (1.00, 0.52, 0.48, 1.00)              # hints printed in light on the metal
WH_PATH = (1.00, 0.76, 0.74, 1.00)              # SRT path: bright so it reads on the red glass
WH_AMBER = (1.00, 0.72, 0.22, 1.00)
WH_STEEL = (0.62, 0.62, 0.66, 0.95)             # stamped digits on the dark keys
WH_INK = (0.05, 0.05, 0.06, 0.92)               # printed labels (match the black lettering in the art)

WH_LIT_FADE = 14.0

_lit_state = {}


# ── helpers ──────────────────────────────────────────────────────────────────
def _pp_skin_on():
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas, get_texture as _gt
        return _gas() == WH_PP_SKIN and _gt("rack_whisper_bg") is not None
    except Exception:
        return False


def wh_pp_active():
    """True when the packApunch Whisper layout is in use (skin loaded AND faster-whisper found, since the skin is
    withheld behind the setup warning until then)."""
    return _pp_skin_on() and _check_dep()


class _G:
    """Art px -> screen px for one rack."""
    def __init__(self, rx, ry, rw, rh):
        self.rx, self.ry, self.rw, self.rh = rx, ry, rw, rh
        self.sx, self.sy = rw / WH_ART_W, rh / WH_ART_H
        self.s = min(self.sx, self.sy)

    def X(self, ax):
        return self.rx + ax * self.sx

    def Y(self, ay):
        return self.ry + (1.0 - ay / WH_ART_H) * self.rh

    def rect(self, r):
        """(x0, y0, x1, y1) screen rect, y0 = bottom."""
        return (self.X(r[0]), self.Y(r[3]), self.X(r[2]), self.Y(r[1]))


def _in(r, x, y):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def _grow(r, d):
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def _top_key(i):
    x0 = WH_TOP_X0 + WH_TOP_P * i
    return (x0 - 4.0, WH_TOP_Y[0] - 3.0, x0 + WH_TOP_W + 4.0, WH_TOP_Y[1] + 3.0)


def _key(row_y, i):
    x0 = WH_KEY_X0 + WH_KEY_P * i
    return (x0, row_y[0], x0 + WH_KEY_W, row_y[1])


def _font_rows_rect():
    return (WH_FONT_X0, WH_FONT_TOP, WH_FONT_X1, WH_FONT_TOP + WH_FONT_ROWS * WH_FONT_ROW_H)


def _srt_on(rack):
    try:
        return bool(rack.get('wsp_srt_enabled', True))
    except Exception:
        return True


def wh_pp_hit(mx, my, rack, ai_idx, rack_x, rack_y, rw, rh):
    """Click zones for the packApunch layout (same zone names the old layout used). Only call while wh_pp_active()."""
    g = _G(rack_x, rack_y, rw, rh)
    R = g.rect
    if _in(R(WH_COLLAPSE), mx, my):
        return {'zone': 'ai_collapse', 'ai_idx': ai_idx}
    if _in(R(WH_CLOSE), mx, my):
        return {'zone': 'ai_delete', 'ai_idx': ai_idx}
    if _in(R(WH_POWER), mx, my):
        return {'zone': 'ai_on_off', 'ai_idx': ai_idx}
    for ci in range(9):
        if _in(R(_top_key(ci)), mx, my):
            return {'zone': 'ai_channel_btn', 'ai_idx': ai_idx, 'ch_idx': ci}
    for ci in range(9):
        if _in(R(_grow(_key(WH_KEY_IN_Y, ci), 3)), mx, my):
            return {'zone': 'ai_wsp_inch_btn', 'ai_idx': ai_idx, 'ch_idx': ci}
        if _in(R(_grow(_key(WH_KEY_OUT_Y, ci), 3)), mx, my):
            return {'zone': 'ai_wsp_outch_btn', 'ai_idx': ai_idx, 'ch_idx': ci}
    for name, zone in WH_ARR_ZONE.items():
        if _in(R(_grow(WH_ARR[name], 1)), mx, my):
            return {'zone': zone, 'ai_idx': ai_idx}
    fr = _font_rows_rect()
    if _in(R(fr), mx, my):
        row = int((my_art(g, my) - WH_FONT_TOP) // WH_FONT_ROW_H)
        return {'zone': 'ai_wsp_font_pick', 'ai_idx': ai_idx, 'row': max(0, min(WH_FONT_ROWS - 1, row))}
    for si, r in enumerate(WH_STYLE):
        if _in(R(r), mx, my):
            return {'zone': 'ai_wsp_style_btn', 'ai_idx': ai_idx, 'style_idx': si}
    for pi, r in enumerate(WH_POS):
        if _in(R(r), mx, my):
            return {'zone': 'ai_wsp_pos_btn', 'ai_idx': ai_idx, 'pos_idx': pi}
    for mi, r in enumerate(WH_MODE):
        if _in(R(r), mx, my):
            return {'zone': 'ai_wsp_mode_btn', 'ai_idx': ai_idx, 'mode_idx': mi}
    if _in(R(_grow(WH_TOG_VAD, 2)), mx, my):
        return {'zone': 'ai_wsp_vad_toggle', 'ai_idx': ai_idx}
    if _in(R(_grow(WH_TOG_SRT, 2)), mx, my):
        return {'zone': 'ai_wsp_srt_toggle', 'ai_idx': ai_idx}
    if _srt_on(rack) and _in(R(WH_SRT_BROWSE), mx, my):
        return {'zone': 'ai_wsp_srt_browse', 'ai_idx': ai_idx}
    if _in(R(WH_TRANS), mx, my):
        return {'zone': 'ai_wsp_transcribe', 'ai_idx': ai_idx}
    return {'zone': 'ai_rack_body', 'ai_idx': ai_idx}


def my_art(g, sy):
    """Screen y -> art y (from the top)."""
    return (1.0 - (sy - g.ry) / g.rh) * WH_ART_H


# ── drawing helpers ──────────────────────────────────────────────────────────
def _text(text, x, cy, fs_art, g, col, lv=1.0, align="l", glow=WH_GLOW_AMT, glow_col=WH_GLOW, max_w=None):
    """Text with optional soft glow; x = left / centre / right edge by align, cy = vertical centre (screen px)."""
    if lv <= 0.01 or not text:
        return 0.0
    try:
        from ui.racks.pp_text import draw_text as _pdt, text_width as _ptw
    except Exception:
        _pdt = _ptw = None
    fs = max(1, int(round(fs_art * g.s)))
    c = (col[0], col[1], col[2], col[3] * lv)
    tw = _ptw(text, fs) if _ptw else (_text_width(text, fs) if _text_width else fs * 0.6 * len(text))
    if max_w is not None and tw > max_w > 0:          # shrink to fit the window / slot
        fs = max(1, int(fs * max_w / tw))
        tw = _ptw(text, fs) if _ptw else (_text_width(text, fs) if _text_width else fs * 0.6 * len(text))
    if align == "c":
        x -= tw / 2
    elif align == "r":
        x -= tw
    y = cy - fs * 0.36
    if _pdt:
        _pdt(text, x, y, fs, c, glow=glow * lv * WH_GLOW_SCALE, glow_col=glow_col)
    elif _draw_text:
        _draw_text(text, x, y, fs, c)
    return tw


def _text_shadowed(text, x, cy, fs_art, g, col, lv=1.0, align="l", glow=0.0, glow_col=WH_GLOW, max_w=None,
                   shadow=(0.0, 0.0, 0.0, 0.55), off=1.2):
    """Text with a 1px dark drop so it reads on a lit or patterned surface."""
    o = off * g.s
    _text(text, x + o, cy - o, fs_art, g, shadow, lv, align, glow=0.0, max_w=max_w)
    return _text(text, x, cy, fs_art, g, col, lv, align, glow=glow, glow_col=glow_col, max_w=max_w)


def _quad(sh, x0, y0, x1, y1, col, blend="ALPHA"):
    gpu.state.blend_set(blend)
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _disc(sh, cx, cy, r, col, blend="ALPHA", seg=28):
    pts = []
    for i in range(seg):
        a0 = 2 * math.pi * i / seg
        a1 = 2 * math.pi * (i + 1) / seg
        pts += [(cx, cy), (cx + r * math.cos(a0), cy + r * math.sin(a0)), (cx + r * math.cos(a1), cy + r * math.sin(a1))]
    gpu.state.blend_set(blend)
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": pts}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _patch(g, tex, key, art_rect, k, src=None):
    """Blit part of an extra art image (premultiplied) over art_rect (art px) at strength k. src = where that part
    lives in the image (art px) when it is not at the same place as the destination."""
    try:
        from ui.mixer.texture_cache import blit_texture_tinted as _btt
        if tex is None or k <= 0.003:
            return
        r = g.rect(art_rect)
        sr = src if src is not None else art_rect
        u0, u1 = sr[0] / WH_ART_W, sr[2] / WH_ART_W
        v0, v1 = 1.0 - sr[3] / WH_ART_H, 1.0 - sr[1] / WH_ART_H
        _btt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], color=(k, k, k, k), blend="ALPHA_PREMULT",
             uv=(u0, v0, u1, v1), key=key)
    except Exception as e:
        print(f"[WHISPER] patch error: {e}")


def _level(key, target, now):
    """Smoothly eased 0..1 light level."""
    st = _lit_state.get(key)
    if st is None:
        _lit_state[key] = (float(target), now)
        return float(target)
    lv, t0 = st
    dt = max(0.0, min(0.25, now - t0))
    lv += (target - lv) * min(1.0, dt * WH_LIT_FADE)
    if abs(target - lv) < 0.004:
        lv = float(target)
    _lit_state[key] = (lv, now)
    return lv


def _grow_xy(r, dx, dy):
    return (r[0] - dx, r[1] - dy, r[2] + dx, r[3] + dy)


def _fit_tail(text, max_chars):
    if len(text) <= max_chars:
        return text
    return "..." + text[-(max_chars - 3):]


# ── terminator-style targeting HUD, behind the font list ─────────────────────
WH_HUD_BOX = (664.0, 80.0, 1146.0, 168.0)       # clip box inside the glass (art px)
WH_HUD_X = (712.0, 1100.0)                      # where the reticle may roam (centre)
WH_HUD_Y = (100.0, 148.0)
WH_HUD_R = 28.0                                 # ring radius (art px)
WH_HUD_BR = 38.0                                # bracket half-side
WH_HUD_ALPHA = 0.60


_hud_clock = {}


def _hh(a, b):
    x = (a * 374761393 + b * 668265263) & 0xFFFFFFFF
    x = ((x ^ (x >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((x ^ (x >> 16)) & 0xFFFF) / 65535.0


def _ease(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _hud_waypoint(seed, n):
    return (WH_HUD_X[0] + (WH_HUD_X[1] - WH_HUD_X[0]) * _hh(seed, n * 2 + 1),
            WH_HUD_Y[0] + (WH_HUD_Y[1] - WH_HUD_Y[0]) * _hh(seed, n * 2 + 2))


WH_HUD_CYCLE = 13.0                             # one lock-on every cycle: it roams for 8.4-10 s first
WH_HUD_HOLD = 2.0                               # seconds held on target


def _hud_roam(seed, t):
    """Free roaming: a few slow sine sweeps, so it keeps moving and never stops."""
    ph = [_hh(seed, 40 + i) * 6.2832 for i in range(5)]
    mx, my = (WH_HUD_X[0] + WH_HUD_X[1]) / 2.0, (WH_HUD_Y[0] + WH_HUD_Y[1]) / 2.0
    x = mx + 112.0 * math.sin(0.50 * t + ph[0]) + 62.0 * math.sin(1.17 * t + ph[1]) + 20.0 * math.sin(2.3 * t + ph[2])
    y = my + 17.0 * math.sin(0.83 * t + ph[3]) + 7.0 * math.sin(1.9 * t + ph[4])
    return (max(WH_HUD_X[0], min(WH_HUD_X[1], x)), max(WH_HUD_Y[0], min(WH_HUD_Y[1], y)))


def _hud_state(seed, t):
    """(cx, cy, lock 0..1, blink 0..1) at clock t. It roams freely, then every WH_HUD_CYCLE seconds sweeps onto a
    target, locks (brackets close in, ring blinks) for WH_HUD_HOLD seconds and lets go again."""
    d = WH_HUD_CYCLE
    n = int(t // d)
    u = t - n * d
    h = 8.4 + 1.6 * _hh(seed, 700 + n)                 # when the hold starts in this cycle
    ta = h - 1.0                                       # start of the sweep onto the target
    rx, ry = _hud_roam(seed, t)
    tx, ty = _hud_waypoint(seed, n)
    if u < ta:
        return rx, ry, 0.0, 0.0
    if u < h:
        k = _ease(u - ta)
        lock = _ease((u - ta - 0.5) / 0.5)
        return rx + (tx - rx) * k, ry + (ty - ry) * k, lock, 0.0
    if u < h + WH_HUD_HOLD:
        blink = 0.5 + 0.5 * math.sin((u - h) * 26.0)
        return tx + 0.6 * math.sin(t * 9.0), ty + 0.6 * math.sin(t * 7.0), 1.0, blink
    k = 1.0 - _ease((u - h - WH_HUD_HOLD) / 0.6)
    return rx + (tx - rx) * k, ry + (ty - ry) * k, k, 0.0


def _clip_seg(x0, y0, x1, y1, box):
    """Liang-Barsky clip of a segment to box; None if outside."""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - box[0]), (dx, box[2] - x0), (-dy, y0 - box[1]), (dy, box[3] - y0)):
        if p == 0.0:
            if q < 0.0:
                return None
        else:
            r = q / p
            if p < 0.0:
                if r > t1:
                    return None
                t0 = max(t0, r)
            else:
                if r < t0:
                    return None
                t1 = min(t1, r)
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)


def _seg_quads(g, segs, th_art, box):
    """Segments (art px) -> triangle list in screen px, clipped to box, thickness th_art."""
    out = []
    half = max(0.5, th_art * g.s * 0.5)
    for (x0, y0, x1, y1) in segs:
        c = _clip_seg(x0, y0, x1, y1, box)
        if c is None:
            continue
        ax, ay, bx, by = g.X(c[0]), g.Y(c[1]), g.X(c[2]), g.Y(c[3])
        dx, dy = bx - ax, by - ay
        ln = math.hypot(dx, dy)
        if ln < 1e-4:
            continue
        nx, ny = -dy / ln * half, dx / ln * half
        a1, a2, b1, b2 = (ax + nx, ay + ny), (ax - nx, ay - ny), (bx + nx, by + ny), (bx - nx, by - ny)
        out += [a1, a2, b1, a2, b2, b1]
    return out


def _tris(sh, pts, col):
    if not pts:
        return
    gpu.state.blend_set("ADDITIVE")
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": pts}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _draw_hud(g, sh, ai_idx, now, lv, busy):
    """Targeting reticle (ring + corner brackets + ticks) roaming the font screen, with cross-hair lines and a slow
    scan sweep. Faint and additive so the font names stay readable on top."""
    if lv <= 0.01:
        return
    seed = 7 + ai_idx * 31
    st = _hud_clock.get(ai_idx)
    if st is None:
        st = _hud_clock[ai_idx] = [float(_hh(seed, 5) * 1000.0), now, 1.0]
    dt = max(0.0, min(0.1, now - st[1]))
    st[1] = now
    st[2] += ((1.8 if busy else 1.0) - st[2]) * min(1.0, dt * 2.0)
    st[0] += dt * st[2]
    t = st[0]
    cx, cy, lock, blink = _hud_state(seed, t)
    box = WH_HUD_BOX
    R_ = WH_HUD_R * (1.0 - 0.10 * lock)
    B_ = WH_HUD_BR * (1.0 - 0.30 * lock)
    arm = 12.0
    ring, inner, ticks, brk = [], [], [], []
    N = 44
    for i in range(N):
        a0, a1 = 2 * math.pi * i / N, 2 * math.pi * (i + 1) / N
        ring.append((cx + R_ * math.cos(a0), cy + R_ * math.sin(a0), cx + R_ * math.cos(a1), cy + R_ * math.sin(a1)))
    r2 = R_ * 0.30
    for i in range(20):
        a0, a1 = 2 * math.pi * i / 20, 2 * math.pi * (i + 1) / 20
        inner.append((cx + r2 * math.cos(a0), cy + r2 * math.sin(a0), cx + r2 * math.cos(a1), cy + r2 * math.sin(a1)))
    for (ux, uy) in ((1, 0), (-1, 0), (0, 1), (0, -1)):      # ticks: from inside the ring to just past it
        ticks.append((cx + ux * R_ * 0.52, cy + uy * R_ * 0.52, cx + ux * R_ * 1.22, cy + uy * R_ * 1.22))
    for sx_ in (-1, 1):                                       # corner brackets of the square
        for sy_ in (-1, 1):
            px, py = cx + sx_ * B_, cy + sy_ * B_
            brk.append((px, py, px - sx_ * arm, py))
            brk.append((px, py, px, py - sy_ * arm))
    axis = [(box[0], cy, cx - B_ - 6.0, cy), (cx + B_ + 6.0, cy, box[2], cy),
            (cx, box[1], cx, cy - B_ - 6.0), (cx, cy + B_ + 6.0, cx, box[3])]
    a = WH_HUD_ALPHA * lv
    hot = 0.55 * lock * (0.6 + 0.4 * blink)
    # long cross-hair lines (very faint)
    _tris(sh, _seg_quads(g, axis, 0.9, box), (1.0, 0.18, 0.22, 0.30 * a))
    # slow scan sweep
    sw = box[1] + (box[3] - box[1]) * ((now * 0.22) % 1.0)
    _tris(sh, _seg_quads(g, [(box[0], sw, box[2], sw)], 1.2, box), (1.0, 0.25, 0.28, 0.16 * a))
    # glow, then crisp lines
    shapes = ring + inner + ticks + brk
    _tris(sh, _seg_quads(g, shapes, 4.2, box), (1.0, 0.10, 0.14, (0.10 + 0.14 * lock) * a))
    _tris(sh, _seg_quads(g, ring + ticks, 1.5, box), (1.0, 0.42 + hot * 0.4, 0.44 + hot * 0.4, 0.80 * a))
    _tris(sh, _seg_quads(g, inner + brk, 1.5, box), (1.0, 0.36 + hot * 0.4, 0.38 + hot * 0.4, 0.85 * a))
    _tris(sh, _seg_quads(g, [(cx - 1.6, cy, cx + 1.6, cy)], 3.0, box), (1.0, 0.75, 0.75, 0.9 * a))


def wh_pp_draw(rx, ry, rw, rh, rack, ai_idx, scale):
    """packApunch Whisper rack: everything that changes, drawn over the baked art."""
    g = _G(rx, ry, rw, rh)
    R = g.rect
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    now = _time.time()
    enabled = bool(getattr(rack, "enabled", True))
    try:
        from ui.racks.rack_base import _pp_preset_level as _ppl
        lv = _ppl(("wh", ai_idx), enabled, _time.monotonic())
    except Exception:
        lv = 1.0 if enabled else 0.0

    try:
        from ui.mixer.texture_cache import get_texture as _gt
        lit_tex = _gt(WH_LIT_KEY)
        off_tex = _gt(WH_OFF_KEY)
    except Exception:
        lit_tex = off_tex = None

    # ── power: the lit art is the base; the unlit art fades in over the lights as the rack goes off ──
    if lv < 0.995 and off_tex is not None:
        for reg in WH_OFF_REGIONS:
            _patch(g, off_tex, WH_OFF_KEY, reg, 1.0 - lv)

    status = getattr(rack, "ai_status", "READY")
    if status not in ("READY", "PROCESSING", "DONE", "ERROR"):
        status = "READY"
    model_idx = max(0, min(int(getattr(rack, "p0", 1.0)), len(_MODEL_LABELS) - 1))
    lang_idx = max(0, min(int(getattr(rack, "p1", 0.0)), len(_LANG_LABELS) - 1))
    mode_idx = 1 if int(getattr(rack, "p2", 0.0)) == 1 else 0
    out_ch = int(getattr(rack, "p3", 0.0))
    font_size = int(getattr(rack, "p4", 40.0))
    style_flags = int(getattr(rack, "p5", 0.0))
    pos_idx = max(0, min(2, int(getattr(rack, "p6", 2.0))))
    vad_on = float(getattr(rack, "p7", 1.0)) > 0.5
    srt_on = _srt_on(rack)
    srt_path = getattr(rack, "wsp_srt_path", "")
    sel_font_path = getattr(rack, "wsp_font_path", "")
    chunk_len = int(getattr(rack, "wsp_chunk_length", 10))
    group_off = getattr(rack, "group_idx", 0) * 9
    active_chs = [ci for ci in range(9) if getattr(rack, f"ch{ci}", False)]
    in_ch = active_chs[0] if active_chs else -1
    pulse = 0.5 + 0.5 * math.sin(now * 5.0)

    # ── rack number in the red box beside the collapse key: hot pink on lit glass, dim maroon when the rack is off ──
    nr = R(WH_NUMBER)
    ncx, ncy = (nr[0] + nr[2]) / 2, g.Y((WH_NUMBER[1] + WH_NUMBER[3]) / 2 + 0.5)
    _text(str(ai_idx + 1), ncx, ncy, WH_FS_NUM, g, (0.46, 0.08, 0.10, 0.95), 1.0 - lv, "c", glow=0.0)
    _text(str(ai_idx + 1), ncx, ncy, WH_FS_NUM, g, WH_CORE, lv, "c", glow=1.0)

    # ── lit keys: top channel keys, input keys, output keys ──────────────────
    for ci in range(9):
        kr = _top_key(ci)
        assigned = bool(getattr(rack, f"ch{ci}", False))
        k = _level(("wh_top", ai_idx, ci), 1.0 if assigned else 0.0, now)
        _patch(g, lit_tex, WH_LIT_KEY, _grow(kr, 3.0), k * lv)
        sr = R(kr)
        kcx, kcy = (sr[0] + sr[2]) / 2, g.Y((WH_TOP_Y[0] + WH_TOP_Y[1]) / 2 - 1.0)
        col = WH_CORE if (assigned and lv > 0.5) else WH_STEEL
        _text_shadowed(str(group_off + ci + 1), kcx, kcy, WH_FS_KEY_TOP, g, col, 1.0, "c",
                       glow=0.7 if (assigned and lv > 0.5) else 0.0, shadow=(0.0, 0.0, 0.0, 0.6), off=1.0)
    for row_y, kind in ((WH_KEY_IN_Y, "in"), (WH_KEY_OUT_Y, "out")):
        for ci in range(9):
            kr = _key(row_y, ci)
            sel = (ci == in_ch) if kind == "in" else (ci + 1 == out_ch)
            k = _level(("wh_" + kind, ai_idx, ci), 1.0 if sel else 0.0, now)
            _patch(g, lit_tex, WH_LIT_KEY, (kr[0] - 3.0, kr[1] - 4.0, kr[2] + 3.0, kr[3] + 4.0), k * lv)
            sr = R(kr)
            kcx, kcy = (sr[0] + sr[2]) / 2, g.Y((kr[1] + kr[3]) / 2 - 1.0)
            lit_now = sel and lv > 0.5
            _text_shadowed(str(ci + 1), kcx, kcy, WH_FS_KEY, g, WH_CORE if lit_now else WH_STEEL, 1.0, "c",
                           glow=0.8 if lit_now else 0.0, shadow=(0.0, 0.0, 0.0, 0.6), off=1.0)

    # ── printed label for the second left selector (the art only labels the first) ──
    _text("MAX SEGMENT LENGTH", g.X(115.0), g.Y(277.0), WH_FS_LABEL, g, WH_INK, 1.0, glow=0.0)

    # ── selected buttons: STYLE flags, POSITION, MODE ────────────────────────
    for si, r in enumerate(WH_STYLE):
        on = bool(style_flags & WH_STYLE_FLAGS[si])
        k = _level(("wh_style", ai_idx, si), 1.0 if on else 0.0, now)
        _patch(g, lit_tex, WH_LIT_KEY, _grow_xy(r, 3.0, 14.0), k * lv)
    for pi, r in enumerate(WH_POS):
        k = _level(("wh_pos", ai_idx, pi), 1.0 if pi == pos_idx else 0.0, now)
        _patch(g, lit_tex, WH_LIT_KEY, _grow_xy(r, 3.0, 14.0), k * lv)
    for mi, r in enumerate(WH_MODE):
        k = _level(("wh_mode", ai_idx, mi), 1.0 if mi == mode_idx else 0.0, now)
        _patch(g, lit_tex, WH_LIT_KEY, _grow_xy(r, 3.0, 14.0), k * lv)

    # ── toggle slots ─────────────────────────────────────────────────────────
    for name, r, on in (("vad", WH_TOG_VAD, vad_on), ("srt", WH_TOG_SRT, srt_on)):
        k = _level(("wh_tog_" + name, ai_idx), 1.0 if on else 0.0, now)
        _patch(g, lit_tex, WH_LIT_KEY, _grow(r, 12.0), k * lv)

    # ── TRANSCRIBE: rim pulses while busy, label swaps to PROCESSING ─────────
    busy = status == "PROCESSING"
    k_busy = _level(("wh_busy", ai_idx), 1.0 if busy else 0.0, now)
    _patch(g, lit_tex, WH_LIT_KEY, WH_P_TRANS_CLEAN[0], k_busy * lv, WH_P_TRANS_CLEAN[1])
    _patch(g, lit_tex, WH_LIT_KEY, _grow(WH_TRANS, 14.0), k_busy * lv * (0.25 + 0.75 * pulse))
    if k_busy > 0.02 and lv > 0.01:
        tr = R(WH_TRANS)
        _text("PROCESSING...", (tr[0] + tr[2]) / 2, (tr[1] + tr[3]) / 2, 21.0, g, WH_CORE, k_busy * lv, "c",
              glow=0.9, max_w=(tr[2] - tr[0]) * 0.8)

    # ── lenses: pulse while busy ─────────────────────────────────────────────
    if lv > 0.01 and k_busy > 0.02:
        for (ex_, ey_) in WH_EYES:
            ecx, ecy = g.X(ex_), g.Y(ey_)
            er = 16.0 * g.s
            _disc(sh, ecx, ecy, er * 1.9, (1.0, 0.12, 0.12, 0.22 * (0.3 + 0.7 * pulse) * k_busy * lv), "ADDITIVE")
            _disc(sh, ecx, ecy, er * 0.8, (1.0, 0.55, 0.50, 0.30 * pulse * k_busy * lv), "ADDITIVE")

    # ── MODEL SIZE screen ────────────────────────────────────────────────────
    sm = R(WH_SCR['model'])
    scy = (sm[1] + sm[3]) / 2
    _text(_MODEL_LABELS[model_idx], sm[0] + 14 * g.s, scy, WH_FS_VAL, g, WH_CORE, lv, glow=1.0)
    _text(_MODEL_DESCS[model_idx], sm[2] - 14 * g.s, scy, WH_FS_SCR, g, WH_RED, lv, "r", glow=0.6,
          max_w=(sm[2] - sm[0]) * 0.62)

    # ── MAX SEGMENT screen ───────────────────────────────────────────────────
    sc = R(WH_SCR['chunk'])
    scy = (sc[1] + sc[3]) / 2
    _text(f"{chunk_len}s", sc[0] + 14 * g.s, scy, WH_FS_VAL, g, WH_CORE, lv, glow=1.0)
    _text("5s = more strips   30s = fewer strips", sc[2] - 14 * g.s, scy, WH_FS_SCR, g, WH_RED, lv, "r", glow=0.6,
          max_w=(sc[2] - sc[0]) * 0.62)

    # ── FONT screen: scrolling list ──────────────────────────────────────────
    fr = R(WH_SCR['font'])
    _draw_hud(g, sh, ai_idx, now, lv, busy)
    fonts = get_system_fonts()
    if fonts:
        vis = WH_FONT_ROWS
        if ai_idx in _font_scroll:
            scroll = max(0, min(_font_scroll[ai_idx], max(0, len(fonts) - vis)))
            _font_scroll[ai_idx] = scroll
        else:
            sel_fi = 0
            for fi, (fn, fp) in enumerate(fonts):
                if fp == sel_font_path:
                    sel_fi = fi
                    break
            scroll = max(0, min(sel_fi - vis // 2, max(0, len(fonts) - vis)))
            _font_scroll[ai_idx] = scroll
        for slot in range(vis):
            fi = scroll + slot
            if fi >= len(fonts):
                break
            fn, fp = fonts[fi]
            sel = (fp == sel_font_path)
            ry0, ry1 = WH_FONT_TOP + slot * WH_FONT_ROW_H, WH_FONT_TOP + (slot + 1) * WH_FONT_ROW_H
            if sel and lv > 0.01:
                bar = R((WH_FONT_X0, ry0, WH_FONT_X1, ry1))
                _quad(sh, bar[0], bar[1], bar[2], bar[3], (0.85, 0.08, 0.14, 0.50 * lv))
                _quad(sh, bar[0], bar[1], bar[2], bar[3], (1.0, 0.15, 0.20, 0.14 * lv), "ADDITIVE")
            disp = fn if len(fn) <= 52 else fn[:51] + "..."
            _text(disp, g.X(WH_FONT_X0 + 8.0), g.Y((ry0 + ry1) / 2), WH_FS_SMALL + 1.0, g,
                  WH_CORE if sel else WH_RED, lv, glow=0.9 if sel else 0.35,
                  max_w=(WH_FONT_X1 - WH_FONT_X0 - 20.0) * g.sx)
        if len(fonts) > vis and lv > 0.01:                    # slim scroll indicator on the right edge
            tr0, tr1 = WH_FONT_TOP, WH_FONT_TOP + vis * WH_FONT_ROW_H
            tb = R((1142.0, tr0, 1146.0, tr1))
            _quad(sh, tb[0], tb[1], tb[2], tb[3], (0.30, 0.04, 0.06, 0.8 * lv))
            frac0 = scroll / float(len(fonts))
            frac1 = min(1.0, (scroll + vis) / float(len(fonts)))
            th = R((1142.0, tr0 + (tr1 - tr0) * frac0, 1146.0, tr0 + (tr1 - tr0) * frac1))
            _quad(sh, th[0], th[1], th[2], th[3], (1.0, 0.45, 0.45, 0.9 * lv))
    else:
        _text("SCANNING FONTS...", (fr[0] + fr[2]) / 2, (fr[1] + fr[3]) / 2, WH_FS_SCR + 2.0, g, WH_RED, lv, "c",
              glow=0.6)

    # ── SIZE screen: bar + value ─────────────────────────────────────────────
    sz = WH_SCR['size']
    frac = max(0.0, min(1.0, (font_size - 12.0) / 188.0))
    bar0, bar1 = sz[0] + 5.0, sz[2] - 5.0
    bx_end = bar0 + (bar1 - bar0) * frac
    if lv > 0.01:
        b = R((bar0, sz[1] + 5.0, bx_end, sz[3] - 5.0))
        _quad(sh, b[0], b[1], b[2], b[3], (0.85, 0.07, 0.12, 0.42 * lv))
        _quad(sh, b[0], b[1], b[2], b[3], (1.0, 0.12, 0.16, 0.14 * lv), "ADDITIVE")
        cap = R((bx_end - 2.0, sz[1] + 3.0, bx_end + 1.0, sz[3] - 3.0))
        _quad(sh, cap[0], cap[1], cap[2], cap[3], (1.0, 0.86, 0.86, 0.95 * lv))
        _quad(sh, cap[0] - 4 * g.s, cap[1] - 2 * g.s, cap[2] + 4 * g.s, cap[3] + 2 * g.s, (1.0, 0.15, 0.2, 0.22 * lv),
              "ADDITIVE")
    szr = R(sz)
    _text(str(font_size), szr[0] + 22 * g.s, (szr[1] + szr[3]) / 2, WH_FS_BIG, g, WH_CORE, lv, glow=1.0)

    # ── AUDIO LANGUAGE screen ────────────────────────────────────────────────
    lr = R(WH_SCR['lang'])
    _text(_LANG_LABELS[lang_idx].upper(), (lr[0] + lr[2]) / 2, (lr[1] + lr[3]) / 2, WH_FS_VAL, g, WH_CORE, lv, "c",
          glow=1.0, max_w=(lr[2] - lr[0]) * 0.8)
    _text(f"{lang_idx + 1:02d}/{len(_LANG_LABELS):02d}", lr[2] - 14 * g.s, (lr[1] + lr[3]) / 2, WH_FS_SMALL, g,
          WH_RED_DIM, lv, "r", glow=0.3)

    # ── notes in the free metal beside the labels (glowing lettering, same as the buttons) ──
    if mode_idx == 1:
        _text("Any language -> English only (Whisper limit)", g.X(1262.0), g.Y(147.0), WH_FS_SMALL, g, WH_AMBER, lv,
              glow=0.7, glow_col=(1.0, 0.55, 0.1), max_w=(1795.0 - 1262.0) * g.sx)
    else:
        _text("Outputs text in source language", g.X(1262.0), g.Y(147.0), WH_FS_SMALL, g, WH_NOTE, lv, glow=0.5,
              max_w=(1795.0 - 1262.0) * g.sx)
    _text("Strips silence before transcribing", g.X(1300.0), g.Y(240.0), WH_FS_SMALL, g, WH_NOTE, lv, glow=0.5,
          max_w=(1735.0 - 1300.0) * g.sx)

    # ── SRT screen ───────────────────────────────────────────────────────────
    sr_ = R(WH_SCR['srt'])
    scy = (sr_[1] + sr_[3]) / 2
    if srt_on:
        path_disp = _fit_tail(srt_path, 62) if srt_path else "(no path set)"
        _text(path_disp, sr_[0] + 12 * g.s, scy, WH_FS_SCR + 1.0, g, WH_PATH, lv, glow=0.7,
              max_w=(WH_SRT_BROWSE[0] - 1196.0 - 24.0) * g.sx)
        if lv > 0.01:                                       # small browse key at the right end of the screen
            bb = R(WH_SRT_BROWSE)
            _quad(sh, bb[0], bb[1], bb[2], bb[3], (0.55, 0.05, 0.09, 0.55 * lv))
            for (a, b_, c, d) in ((bb[0], bb[1], bb[2], bb[1] + 1.2 * g.s), (bb[0], bb[3] - 1.2 * g.s, bb[2], bb[3]),
                                  (bb[0], bb[1], bb[0] + 1.2 * g.s, bb[3]), (bb[2] - 1.2 * g.s, bb[1], bb[2], bb[3])):
                _quad(sh, a, b_, c, d, (1.0, 0.45, 0.45, 0.9 * lv))
            _text("...", (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2 + 8 * g.s, WH_FS_BIG, g, WH_CORE, lv, "c", glow=0.8)
    else:
        _text("SRT EXPORT OFF", sr_[0] + 12 * g.s, scy, WH_FS_SCR, g, WH_RED, lv, glow=0.4)

    # ── status screen (bottom left) ──────────────────────────────────────────
    st = R(WH_SCR['status'])
    scol = {"READY": WH_RED, "PROCESSING": WH_AMBER, "DONE": WH_CORE, "ERROR": (1.0, 0.30, 0.20, 1.0)}[status]
    txt = (f"{status}  |  {_MODEL_LABELS[model_idx]}  |  {_LANG_LABELS[lang_idx].upper()}  |  "
           f"OUT {out_ch if out_ch else '?'}  |  {chunk_len}s  |  VAD {'ON' if vad_on else 'OFF'}")
    if busy:
        # progress shown ON the status screen, replacing its usual text
        from core import ai_status as _ais
        _fr = max(0.0, min(1.0, _ais.get_fraction("whisper", ai_idx)))
        _bh = max(2 * g.s, (st[3] - st[1]) * 0.16)
        _quad(sh, st[0] + 4 * g.s, st[1] + 3 * g.s, st[0] + 4 * g.s + ((st[2] - st[0]) - 8 * g.s) * _fr,
              st[1] + 3 * g.s + _bh, (1.0, 0.78, 0.30, 0.95 * lv))
        txt = _ais.pick("whisper", ai_idx, int(((st[2] - st[0]) - 28 * g.s) / max(1.0, WH_FS_SCR * g.s * 0.56)))
    _text(txt, st[0] + 14 * g.s, (st[1] + st[3]) / 2, WH_FS_SCR, g, scol, lv * (0.7 + 0.3 * pulse if busy else 1.0),
          glow=0.7, max_w=(st[2] - st[0]) - 28 * g.s)

    # ── LAST RUN result, next to its printed label ───────────────────────────
    if status == "DONE":
        _text("OK - strips placed on timeline", g.X(1300.0), g.Y(352.0), WH_FS_SMALL + 1.0, g, WH_CORE, lv, glow=0.8,
              max_w=(1560.0 - 1300.0) * g.sx)
    elif status == "ERROR":
        _text("FAILED - check console", g.X(1300.0), g.Y(352.0), WH_FS_SMALL + 1.0, g, (1.0, 0.35, 0.25, 1.0), lv,
              glow=0.8, glow_col=(1.0, 0.25, 0.1), max_w=(1560.0 - 1300.0) * g.sx)
    elif busy:
        from core import ai_status as _ais
        _lw = (1840.0 - 1300.0) * g.sx
        _text(_ais.pick("whisper", ai_idx, int(_lw / max(1.0, (WH_FS_SMALL + 1.0) * g.s * 0.56))),
              g.X(1300.0), g.Y(352.0), WH_FS_SMALL + 1.0, g, WH_AMBER, lv, glow=0.8, max_w=_lw)
    else:
        _text("No output yet", g.X(1300.0), g.Y(352.0), WH_FS_SMALL + 1.0, g, WH_NOTE, lv, glow=0.5)
