# =============================================================================
# ui/racks/rack_demucs_pp.py
# packApunch layout for the Demucs stem-splitter rack ("The Matrix / Zion" skin).
#
# The art (rack_demucs_bg.png, 2054x512) carries every key, pad, slab, screen, strip and CRT at fixed
# positions. This module draws the live content on top (text, lit lens in the stem pads, lit model
# slab, key glows, lamp) and provides the click zones in ART PIXELS, the same approach as
# rack_voicefixer_pp.py / rack_knnvc.py / rack_piper.py.
#
# All coordinates are art pixels, y measured from the TOP of the art: (x0, y0, x1, y1).
# If something sits a little off in Blender, nudge its rectangle here.
#
# Lit art: rack_demucs_bg_lit.png (same size as the art, premultiplied RGBA) holds the lit states -
# the green lens for each stem pad, the glow on each model slab, the ON-key lettering, the SPLIT STEMS
# key rim and the channel-key glow. Each is blitted as a sub-rectangle at an eased 0..1 level.
# =============================================================================

import math
import os
import time as _time

import gpu
from gpu_extras.batch import batch_for_shader

try:
    from ui.mixer.draw_utils import draw_text as _draw_text, text_width as _text_width
except Exception:                                   # pragma: no cover
    _draw_text = _text_width = None

try:
    from ui.racks.rack_demucs import (_check_demucs, _get_free_channels, MODELS, MODEL_DESC, MODEL_STEMS,
                                      STEM_ICONS, STEM_BITS, STEM_CH_PROPS, ALL_STEMS)
except Exception:                                   # pragma: no cover
    _check_demucs = lambda: True
    _get_free_channels = lambda exclude=None: []
    MODELS = ["htdemucs", "htdemucs_ft", "htdemucs_6s", "mdx_extra"]
    MODEL_DESC = {"htdemucs": "4 stems · fast", "htdemucs_ft": "4 stems · best",
                  "htdemucs_6s": "6 stems · fine", "mdx_extra": "4 stems · alt"}
    MODEL_STEMS = {"htdemucs": ["drums", "bass", "vocals", "other"],
                   "htdemucs_ft": ["drums", "bass", "vocals", "other"],
                   "htdemucs_6s": ["drums", "bass", "vocals", "other", "piano", "guitar"],
                   "mdx_extra": ["drums", "bass", "vocals", "other"]}
    STEM_ICONS = {"drums": "DRUMS", "bass": "BASS", "vocals": "VOX", "other": "OTHER", "piano": "PIANO", "guitar": "GTR"}
    STEM_BITS = {"drums": 0, "bass": 1, "vocals": 2, "other": 3, "piano": 4, "guitar": 5}
    STEM_CH_PROPS = {"drums": "p4", "bass": "p5", "vocals": "p6", "other": "p7", "piano": "p8", "guitar": "p9"}
    ALL_STEMS = ["drums", "bass", "vocals", "other", "piano", "guitar"]

DM_PP_SKIN = "packapunch"
DM_ART_W = 2054.0
DM_ART_H = 512.0

# ── layout (art px) ──────────────────────────────────────────────────────────
DM_COLLAPSE = (20.0, 22.0, 58.0, 59.0)
DM_NUMBER_AT = (100.0, 47.0)                    # blank plate next to the collapse key: rack number
DM_ON = (1921.0, 10.0, 1999.0, 58.0)
DM_CLOSE = (2001.0, 10.0, 2047.0, 58.0)

DM_CHKEY_X0 = 907.0                             # nine channel keys across the top centre
DM_CHKEY_PITCH = 39.1
DM_CHKEY_W = 34.0
DM_CHKEY_Y = (26.0, 61.0)

DM_SLABS = ((82.0, 119.0, 428.0, 162.0), (82.0, 201.0, 428.0, 245.0),
            (82.0, 280.0, 428.0, 324.0), (87.0, 357.0, 430.0, 402.0))          # slab faces (text area)
DM_SLABS_HIT = ((68.0, 107.0, 448.0, 173.0), (68.0, 189.0, 448.0, 254.0),
                (68.0, 268.0, 448.0, 336.0), (70.0, 342.0, 448.0, 411.0))      # slab frames (click area)

DM_PADS = ((538.0, 304.0, 656.0, 362.0), (720.0, 304.0, 837.0, 362.0), (900.0, 304.0, 1020.0, 362.0),
           (1081.0, 304.0, 1200.0, 362.0), (1265.0, 304.0, 1387.0, 362.0), (1440.0, 304.0, 1560.0, 362.0))
DM_PAD_PAD = 15.0                               # click area = face grown by this much (the whole frame)
DM_MINUS = tuple((x, 389.0, x + 29.0, 420.0) for x in (515.0, 696.0, 877.0, 1059.0, 1238.0, 1418.0))
DM_PLUS = tuple((x, 389.0, x + 28.0, 420.0) for x in (659.0, 840.0, 1020.0, 1200.0, 1382.0, 1561.0))
DM_READ = ((560.0, 383.0, 642.0, 428.0), (741.0, 383.0, 824.0, 428.0), (920.0, 383.0, 1004.0, 428.0),
           (1101.0, 383.0, 1184.0, 428.0), (1282.0, 383.0, 1364.0, 428.0), (1461.0, 383.0, 1543.0, 428.0))

DM_SCR_L = (532.0, 98.0, 1056.0, 240.0)         # STEMS screen, left half (text area)
DM_SCR_R = (1072.0, 98.0, 1572.0, 240.0)        # STEMS screen, right half
DM_FREE = (516.0, 455.0, 1545.0, 484.0)
DM_CRT = (1672.0, 186.0, 1960.0, 335.0)         # OPTIONS CRT text area (art px, before the barrel warp)
DM_CRT_GLASS = (1632.0, 110.0, 2004.0, 393.0)   # the curved glass (from the screen matte)
DM_CRT_K = 0.065                                # barrel distortion strength
DM_LAMP = (1622.0, 448.0, 7.0)                  # status lamp: centre x, centre y, radius
DM_STATUS_X = 1636.0
DM_SPLIT = (1607.0, 462.0, 2045.0, 500.0)

# right-screen rows (art y of each row centre)
DM_R_MODEL_Y = 109.0
DM_R_INPUT_Y = 128.0
DM_R_STEM_Y = (147.0, 166.0, 185.0)
DM_R_FREE_Y = 204.0
DM_R_MUTE_Y = 223.0

# ── text sizes (art px) and colours ──────────────────────────────────────────
DM_FS_SCR = 16.0
DM_FS_SCR_BIG = 20.0
DM_FS_SLAB = 21.0
DM_FS_SLAB_SM = 14.0
DM_FS_PAD = 25.0
DM_FS_READ = 17.0
DM_FS_KEY = 17.0
DM_FS_FREE = 16.0
DM_FS_CRT = 15.0
DM_FS_SPLIT = 22.0

DM_GREEN = (0.46, 1.00, 0.58, 1.00)             # phosphor text
DM_GREEN_DIM = (0.28, 0.66, 0.38, 0.95)
DM_GREEN_GLOW = (0.20, 1.00, 0.35)
DM_GLOW = 0.9
DM_GLOW_SCALE = 0.5
DM_AMBER = (1.00, 0.62, 0.12, 1.00)
DM_RED = (1.00, 0.30, 0.15, 1.00)
DM_STEEL = (0.66, 0.70, 0.64, 0.92)             # stamped lettering on the dark steel
DM_STEEL_DIM = (0.50, 0.54, 0.49, 0.62)
DM_PAD_INK = (0.02, 0.13, 0.04, 0.96)           # dark text on the lit lens

# ── code rain (procedural Matrix rain on the two screens) ────────────────────
DM_RAIN_KEY = "rack_demucs_rain"                # glyph atlas: 8x8 cells, sharp on top, blurred copy below
DM_RAIN_MAIN = (532.0, 92.0, 1578.0, 248.0)     # STEMS screen glass (art px) the rain may fill
DM_DIVIDER_X = 1062.0                           # divider between the two halves of the STEMS screen
DM_RAIN_PX = 14.0                               # column pitch (art px)
DM_RAIN_PY = 16.0                               # row pitch (art px)
DM_RAIN_GLYPH = (11.0, 14.0)                    # glyph quad size (art px)
DM_RAIN_SPEED = 1.5                             # rows per second, normal ("slow")
DM_RAIN_FAST = 5.5                              # speed multiplier while splitting
DM_RAIN_GAIN = 1.05                             # peak brightness at rest ...
DM_RAIN_GAIN_FAST = 1.20                        # ... and while splitting
DM_RAIN_DIM = 0.28                              # rain brightness behind live text (factor)
DM_RAIN_COL = (0.28, 1.0, 0.42)
DM_RAIN_HEAD = (0.80, 1.0, 0.86)

# opaque patches stored in the lit atlas (dest rect on the art, source rect inside the atlas) - they cover baked art
DM_P_SPLIT_CLEAN = ((1752.0, 466.0, 1902.0, 498.0), (40.0, 440.0, 190.0, 472.0))     # SPLIT STEMS key, label wiped
DM_P_READY_CLEAN = ((1630.0, 434.0, 1690.0, 462.0), (240.0, 440.0, 300.0, 468.0))    # READY lettering wiped, lamp kept
DM_P_LAMP_OFF = ((1598.0, 428.0, 1694.0, 461.0), (340.0, 440.0, 436.0, 473.0))       # blank glass bulb, no lettering
DM_LOGO = (136.0, 0.0, 432.0, 104.0)                                                  # DEMUCS plaque: logo + traces glow

DM_LIT_FADE = 14.0                              # light fade speed (1/s)

_lit_state = {}


# ── helpers ──────────────────────────────────────────────────────────────────
def _pp_skin_on():
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas, get_texture as _gt
        return _gas() == DM_PP_SKIN and _gt("rack_demucs_bg") is not None
    except Exception:
        return False


def dm_pp_active():
    """True when the packApunch Demucs layout is in use (skin loaded AND demucs found, since the skin is
    withheld behind the setup warning until then)."""
    return _pp_skin_on() and _check_demucs()


class _G:
    """Art px -> screen px for one rack."""
    def __init__(self, rx, ry, rw, rh):
        self.rx, self.ry, self.rw, self.rh = rx, ry, rw, rh
        self.sx, self.sy = rw / DM_ART_W, rh / DM_ART_H
        self.s = min(self.sx, self.sy)

    def X(self, ax):
        return self.rx + ax * self.sx

    def Y(self, ay):
        return self.ry + (1.0 - ay / DM_ART_H) * self.rh

    def rect(self, r):
        """(x0, y0, x1, y1) screen rect, y0 = bottom."""
        return (self.X(r[0]), self.Y(r[3]), self.X(r[2]), self.Y(r[1]))


def _ch_key(i):
    x0 = DM_CHKEY_X0 + DM_CHKEY_PITCH * i
    return (x0, DM_CHKEY_Y[0], x0 + DM_CHKEY_W, DM_CHKEY_Y[1])


def _pad_hit(i):
    r = DM_PADS[i]
    return (r[0] - DM_PAD_PAD, r[1] - DM_PAD_PAD, r[2] + DM_PAD_PAD, r[3] + DM_PAD_PAD + 2)


def _grow(r, d):
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def _mute_zone():
    return (DM_SCR_R[0], DM_R_MUTE_Y - 11.0, DM_SCR_R[2], DM_R_MUTE_Y + 11.0)


def _in(r, x, y):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def dm_pp_hit(mx, my, rack, ai_idx, rack_x, rack_y, rw, rh):
    """Click zones for the packApunch layout (same zone names the old layout used). Only call while dm_pp_active()."""
    g = _G(rack_x, rack_y, rw, rh)
    R = g.rect
    if _in(R(DM_COLLAPSE), mx, my):
        return {'zone': 'ai_collapse', 'ai_idx': ai_idx}
    if _in(R(DM_CLOSE), mx, my):
        return {'zone': 'ai_delete', 'ai_idx': ai_idx}
    if _in(R(DM_ON), mx, my):
        return {'zone': 'ai_on_off', 'ai_idx': ai_idx}
    for ci in range(9):
        if _in(R(_ch_key(ci)), mx, my):
            return {'zone': 'ai_channel_btn', 'ai_idx': ai_idx, 'ch_idx': ci}
    for mi in range(len(MODELS)):
        if _in(R(DM_SLABS_HIT[mi]), mx, my):
            return {'zone': 'demucs_model', 'ai_idx': ai_idx, 'model_idx': mi}
    for si, stem in enumerate(ALL_STEMS):
        if _in(R(_pad_hit(si)), mx, my):
            if stem not in MODEL_STEMS[MODELS[max(0, min(int(getattr(rack, "p0", 1.0)), len(MODELS) - 1))]]:
                return {'zone': 'ai_rack_body', 'ai_idx': ai_idx}      # stem not in this model: nothing to toggle
            return {'zone': 'demucs_stem_toggle', 'ai_idx': ai_idx, 'stem': stem, 'bit': STEM_BITS[stem]}
        if _in(R(_grow(DM_MINUS[si], 2)), mx, my):
            return {'zone': 'demucs_stem_ch_minus', 'ai_idx': ai_idx, 'stem': stem}
        if _in(R(_grow(DM_PLUS[si], 2)), mx, my):
            return {'zone': 'demucs_stem_ch_plus', 'ai_idx': ai_idx, 'stem': stem}
    if _in(R(_mute_zone()), mx, my):
        return {'zone': 'demucs_mute_toggle', 'ai_idx': ai_idx}
    if _in(R(DM_SPLIT), mx, my):
        return {'zone': 'demucs_split', 'ai_idx': ai_idx}
    return {'zone': 'ai_rack_body', 'ai_idx': ai_idx}


def _text(text, x, cy, fs_art, g, col, lv=1.0, align="l", glow=DM_GLOW, glow_col=DM_GREEN_GLOW, max_w=None):
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
    _rec_rect(g, x, cy, tw, fs)
    if _pdt:
        _pdt(text, x, y, fs, c, glow=glow * lv * DM_GLOW_SCALE, glow_col=glow_col)
    elif _draw_text:
        _draw_text(text, x, y, fs, c)
    return tw


def _crt_warp(ax, ay):
    """Barrel-warp an art-space point on the CRT glass; also returns a size factor (shrinks toward the rim)."""
    x0, y0, x1, y1 = DM_CRT_GLASS
    cx, cy, hw, hh = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0) / 2.0, (y1 - y0) / 2.0
    u, v = (ax - cx) / hw, (ay - cy) / hh
    k = DM_CRT_K
    wx = cx + hw * u * (1.0 - k * v * v)
    wy = cy + hh * v * (1.0 - k * u * u)
    return wx, wy, 1.0 - 0.5 * k * (u * u + v * v)


def _text_crt(text, ax, ay, fs_art, g, col, lv=1.0, glow=0.7, glow_col=DM_GREEN_GLOW):
    """Text on the CRT: each glyph is placed through the barrel warp so lines bow with the glass.
    ax = left edge, ay = vertical centre (art px)."""
    if lv <= 0.01 or not text:
        return
    try:
        from ui.racks.pp_text import draw_text as _pdt, text_width as _ptw
    except Exception:
        _pdt = _ptw = None
    fs0 = max(1, int(round(fs_art * g.s)))
    c = (col[0], col[1], col[2], col[3] * lv)
    _tw_all = _ptw(text, fs0) if _ptw else fs0 * 0.55 * len(text)
    _rec_rect(g, g.X(ax), g.Y(ay), _tw_all, fs0)
    for i, ch in enumerate(text):
        if ch == " ":
            continue
        pre = _ptw(text[:i], fs0) if (_ptw and i) else (fs0 * 0.55 * i)
        w = _ptw(ch, fs0) if _ptw else fs0 * 0.55
        mid_art = ax + (pre + w / 2.0) / g.sx
        wx, wy, sc = _crt_warp(mid_art, ay)
        fs = max(1, int(round(fs0 * sc)))
        w2 = _ptw(ch, fs) if _ptw else fs * 0.55
        x = g.X(wx) - w2 / 2.0
        y = g.Y(wy) - fs * 0.36
        if _pdt:
            _pdt(ch, x, y, fs, c, glow=glow * lv * DM_GLOW_SCALE, glow_col=glow_col)
        elif _draw_text:
            _draw_text(ch, x, y, fs, c)


def _text_shadowed(text, x, cy, fs_art, g, col, lv=1.0, align="l", glow=0.0, glow_col=DM_GREEN_GLOW,
                   max_w=None, shadow=(0.0, 0.0, 0.0, 0.55), off=1.2):
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


def _tri(sh, pts, col):
    gpu.state.blend_set("ALPHA")
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": pts}).draw(sh)


def _lit_patch(g, tex, art_rect, k, src=None):
    """Blit the lit art (tex, premultiplied) over art_rect (art px) at strength k. src = where that patch lives in the
    atlas (art px) when it is not at the same place as the destination."""
    try:
        from ui.mixer.texture_cache import blit_texture_tinted as _btt
        if tex is None or k <= 0.003:
            return
        r = g.rect(art_rect)
        sr = src if src is not None else art_rect
        u0, u1 = sr[0] / DM_ART_W, sr[2] / DM_ART_W
        v0, v1 = 1.0 - sr[3] / DM_ART_H, 1.0 - sr[1] / DM_ART_H
        _btt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], color=(k, k, k, k), blend="ALPHA_PREMULT",
             uv=(u0, v0, u1, v1), key="rack_demucs_bg_lit")
    except Exception as e:
        print(f"[DEMUCS] lit patch error: {e}")


def _level(key, target, now):
    """Smoothly eased 0..1 light level."""
    st = _lit_state.get(key)
    if st is None:
        _lit_state[key] = (float(target), now)
        return float(target)
    lv, t0 = st
    dt = max(0.0, min(0.25, now - t0))
    lv += (target - lv) * min(1.0, dt * DM_LIT_FADE)
    if abs(target - lv) < 0.004:
        lv = float(target)
    _lit_state[key] = (lv, now)
    return lv


# ── rain engine ──────────────────────────────────────────────────────────────
_rain_state = {}
_rain_mask = {}
_REC = [None]                                   # records text rectangles (art px) so the rain can dim behind them
_img_shader = [None]


def _hash3(a, b, c):
    x = (a * 374761393 + b * 668265263 + c * 2147483647) & 0xFFFFFFFF
    x = ((x ^ (x >> 13)) * 1274126177) & 0xFFFFFFFF
    return x ^ (x >> 16)


def _hf(a, b, c):
    return (_hash3(a, b, c) & 0xFFFF) / 65535.0


def _smooth(e0, e1, x):
    if x <= e0:
        return 0.0
    if x >= e1:
        return 1.0
    t = (x - e0) / (e1 - e0)
    return t * t * (3.0 - 2.0 * t)


def _rain_time(ai_idx, status, enabled, now):
    """Rain clock: advances only while the rack is on; the speed multiplier eases up while splitting."""
    st = _rain_state.get(ai_idx)
    if st is None:
        st = {"t": float(_hash3(ai_idx, 7, 3) % 1000), "mult": 1.0, "last": now}
        _rain_state[ai_idx] = st
    dt = max(0.0, min(0.1, now - st["last"]))
    st["last"] = now
    target = DM_RAIN_FAST if status == "PROCESSING" else 1.0
    st["mult"] += (target - st["mult"]) * min(1.0, dt * 2.2)
    if enabled:
        st["t"] += dt * st["mult"]
    frac = max(0.0, min(1.0, (st["mult"] - 1.0) / (DM_RAIN_FAST - 1.0)))
    return st["t"], DM_RAIN_GAIN + (DM_RAIN_GAIN_FAST - DM_RAIN_GAIN) * frac


def _row_intervals(rects, y0, py, nrows, region):
    """Per-row x intervals of text rects (art px) that fall inside the screen region."""
    rows = [[] for _ in range(nrows)]
    rx0, ry0, rx1, ry1 = region
    for (a, b, c, d) in rects:
        if c < rx0 or a > rx1 or d < ry0 or b > ry1:
            continue
        lo = max(0, int((b - 4.0 - y0) // py))
        hi = min(nrows - 1, int((d + 4.0 - y0) // py))
        for r in range(lo, hi + 1):
            rows[r].append((a - 8.0, c + 8.0))
    return rows


_LV_EDGES = (0.05, 0.13, 0.22, 0.33, 0.46, 0.62, 2.0)
_LV_VAL = (0.09, 0.17, 0.27, 0.39, 0.54, 0.72)


def _rain_grid(g, tex, name, ai_idx, region, ncols, nrows, x0, y0, t, gain, lv, rects, edge_fn, warp=None):
    """Draw one rain field. Grid cell (c, r) sits at art (x0 + c*pitch, y0 + r*pitch). edge_fn(ax, ay) -> 0..1 fade
    (screen edge / divider / glass shape). warp(ax, ay) -> (wx, wy, size_factor) bends the quads (CRT)."""
    px_, py_ = DM_RAIN_PX, DM_RAIN_PY
    gw, gh = DM_RAIN_GLYPH
    intervals = _row_intervals(rects, y0, py_, nrows, region)
    cells = {}                                  # level -> [verts], [uvs]
    heads_v, heads_u = [], []
    glow_v, glow_u = [], []
    seed = _hash3(ai_idx, ncols, 11)
    for c in range(ncols):
        sp = 0.65 + 0.7 * _hf(seed, c, 1)
        trail = 5 + int(_hf(seed, c, 2) * 8.0)
        gap = 1 + int(_hf(seed, c, 3) * 6.0)
        period = nrows + trail + gap
        head = (t * DM_RAIN_SPEED * sp + _hf(seed, c, 4) * period) % period
        hr = int(math.floor(head))
        ax = x0 + c * px_ + px_ * 0.5
        for k in range(trail + 1):
            r = hr - k
            if r < 0 or r >= nrows:
                continue
            d = head - r
            b = 1.0 - d / (trail + 1.0)
            if b <= 0.0:
                continue
            ay = y0 + r * py_ + py_ * 0.5
            f = edge_fn(ax, ay)
            if f <= 0.02:
                continue
            for (i0, i1) in intervals[r]:
                if i0 <= ax <= i1:
                    f *= DM_RAIN_DIM
                    break
            is_head = d < 1.0
            if is_head:
                v = f * gain * 1.15
                gi = _hash3(c, r, int(t * 7.0)) & 63
            else:
                v = f * gain * (b ** 0.9) * 0.95
                gi = _hash3(c, r, int(t * 1.6 + ((c * 5 + r * 3) % 7) * 0.31)) & 63
            if v < 0.05:
                continue
            gc, gr_ = gi & 7, gi >> 3
            u0, u1 = gc / 8.0, (gc + 1) / 8.0
            # sharp band = top half of the 512x1024 atlas: rows gr_ of 16 -> v from top
            vt = 1.0 - gr_ / 16.0
            vb = 1.0 - (gr_ + 1) / 16.0
            if warp is None:
                hw_, hh_ = gw * 0.5, gh * 0.5
                corners = ((ax - hw_, ay + hh_), (ax + hw_, ay + hh_), (ax + hw_, ay - hh_), (ax - hw_, ay - hh_))
                pts = [(g.X(px), g.Y(py)) for (px, py) in corners]
            else:
                wx, wy, sc = warp(ax, ay)
                hw_, hh_ = gw * 0.5 * sc, gh * 0.5 * sc
                pts = []
                for (px, py) in ((ax - gw * 0.5, ay + gh * 0.5), (ax + gw * 0.5, ay + gh * 0.5),
                                 (ax + gw * 0.5, ay - gh * 0.5), (ax - gw * 0.5, ay - gh * 0.5)):
                    qx, qy, _s = warp(px, py)
                    pts.append((g.X(qx), g.Y(qy)))
            uvq = ((u0, vt), (u1, vt), (u1, vb), (u0, vb))
            quad = (pts[0], pts[1], pts[2], pts[0], pts[2], pts[3])
            uq = (uvq[0], uvq[1], uvq[2], uvq[0], uvq[2], uvq[3])
            if is_head:
                heads_v.extend(quad)
                heads_u.extend(uq)
                # glow: blurred copy (band 8..15) over a larger quad
                cxs = sum(p[0] for p in pts) / 4.0
                cys = sum(p[1] for p in pts) / 4.0
                gv0 = 1.0 - (gr_ + 8) / 16.0
                gv1 = 1.0 - (gr_ + 9) / 16.0
                big = [(cxs + (p[0] - cxs) * 2.0, cys + (p[1] - cys) * 2.0) for p in pts]
                glow_v.extend((big[0], big[1], big[2], big[0], big[2], big[3]))
                glow_u.extend(((u0, gv0), (u1, gv0), (u1, gv1), (u0, gv0), (u1, gv1), (u0, gv1)))
            else:
                li = 0
                while li < 5 and v > _LV_EDGES[li + 1]:
                    li += 1
                ent = cells.get(li)
                if ent is None:
                    ent = cells[li] = ([], [])
                ent[0].extend(quad)
                ent[1].extend(uq)
    try:
        if _img_shader[0] is None:
            _img_shader[0] = gpu.shader.from_builtin("IMAGE_COLOR")
        sh = _img_shader[0]
        gpu.state.blend_set("ADDITIVE_PREMULT")
        cr, cg, cb = DM_RAIN_COL
        for li, (vs, us) in cells.items():
            val = _LV_VAL[li] * lv
            _draw_quads(sh, tex, vs, us, (cr * val, cg * val, cb * val, val))
        if glow_v:
            val = 0.55 * lv
            _draw_quads(sh, tex, glow_v, glow_u, (0.15 * val, 0.9 * val, 0.3 * val, val))
        if heads_v:
            hr_, hg_, hb_ = DM_RAIN_HEAD
            val = lv
            _draw_quads(sh, tex, heads_v, heads_u, (hr_ * val, hg_ * val, hb_ * val, val))
    except Exception as e:
        print(f"[DEMUCS] rain draw error: {e}")
    finally:
        try:
            gpu.state.blend_set("ALPHA")
        except Exception:
            pass


def _draw_quads(sh, tex, verts, uvs, color):
    batch = batch_for_shader(sh, "TRIS", {"pos": verts, "texCoord": uvs})
    sh.bind()
    sh.uniform_sampler("image", tex)
    sh.uniform_float("color", color)
    batch.draw(sh)


def _rain_edge_main(ax, ay):
    x0, y0, x1, y1 = DM_RAIN_MAIN
    f = min(1.0, (ax - x0) / 14.0, (x1 - ax) / 14.0, (ay - y0) / 10.0, (y1 - ay) / 10.0)
    if f <= 0.0:
        return 0.0
    dv = abs(ax - DM_DIVIDER_X)
    if dv < 14.0:
        f *= dv / 14.0
    return f


def _rain_edge_crt(ax, ay):
    wx, wy, _s = _crt_warp(ax, ay)
    x0, y0, x1, y1 = DM_CRT_GLASS
    u = (wx - (x0 + x1) / 2.0) / ((x1 - x0) / 2.0)
    v = (wy - (y0 + y1) / 2.0) / ((y1 - y0) / 2.0)
    s_ = (abs(u) ** 3 + abs(v) ** 3) ** (1.0 / 3.0)
    return 1.0 - _smooth(0.80, 0.94, s_)


def _draw_rain(g, ai_idx, status, enabled, lv, now):
    try:
        from ui.mixer.texture_cache import get_texture as _gt
        tex = _gt(DM_RAIN_KEY)
    except Exception:
        tex = None
    if tex is None or lv <= 0.01:
        return
    t, gain = _rain_time(ai_idx, status, enabled, now)
    rects = _rain_mask.get(ai_idx, [])
    # STEMS screen
    mx0, my0, mx1, my1 = DM_RAIN_MAIN
    ncols = int((mx1 - mx0) // DM_RAIN_PX)
    nrows = int((my1 - my0) // DM_RAIN_PY)
    ox = mx0 + ((mx1 - mx0) - ncols * DM_RAIN_PX) / 2.0
    oy = my0 + ((my1 - my0) - nrows * DM_RAIN_PY) / 2.0
    _rain_grid(g, tex, "main", ai_idx, DM_RAIN_MAIN, ncols, nrows, ox, oy, t, gain, lv, rects, _rain_edge_main)
    # CRT: same rain, bent by the glass
    cx0, cy0, cx1, cy1 = DM_CRT_GLASS
    ncols = int((cx1 - cx0) // DM_RAIN_PX)
    nrows = int((cy1 - cy0) // DM_RAIN_PY)
    ox = cx0 + ((cx1 - cx0) - ncols * DM_RAIN_PX) / 2.0
    oy = cy0 + ((cy1 - cy0) - nrows * DM_RAIN_PY) / 2.0
    _rain_grid(g, tex, "crt", ai_idx + 1000, DM_CRT_GLASS, ncols, nrows, ox, oy, t * 0.9 + 17.0, gain, lv, rects,
               _rain_edge_crt, warp=_crt_warp)


def _rec_rect(g, x, cy, tw, fs):
    """Remember where a piece of text sits (art px) so the rain can dim behind it."""
    rec = _REC[0]
    if rec is None:
        return
    ax0 = (x - g.rx) / g.sx
    ax1 = ax0 + tw / g.sx
    ay = (1.0 - (cy - g.ry) / g.rh) * DM_ART_H
    hh = fs / g.sy * 0.75
    rec.append((ax0, ay - hh, ax1, ay + hh))


def _wrap(text, max_chars):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def dm_pp_draw(rx, ry, rw, rh, rack, ai_idx, scale):
    """packApunch Demucs rack: everything that changes, drawn over the baked art."""
    g = _G(rx, ry, rw, rh)
    R = g.rect
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    now = _time.time()
    enabled = bool(getattr(rack, "enabled", True))
    try:
        from ui.racks.rack_base import _pp_preset_level as _ppl
        lv = _ppl(("dm", ai_idx), enabled, _time.monotonic())
    except Exception:
        lv = 1.0 if enabled else 0.0

    # One-time init guard: PB_AIRackSettings shares FloatProperty slots across AI rack types, so a new Demucs
    # rack inherits stray values in p4..p9 (they would read as channel numbers). Zero them once per rack.
    try:
        if not rack.get('dm_ch_init'):
            for _prop in ("p4", "p5", "p6", "p7", "p8", "p9"):
                try:
                    setattr(rack, _prop, 0.0)
                except Exception:
                    pass
            rack['dm_ch_init'] = True
    except Exception:
        pass

    try:
        from ui.mixer.texture_cache import get_texture as _gt_lit
        lit_tex = _gt_lit("rack_demucs_bg_lit")
    except Exception:
        lit_tex = None

    # (no whole-unit darkening on power-off: the metal isn't lit by the rack; only the lights and screen text fade)

    status = getattr(rack, "ai_status", "READY")
    if status not in ("READY", "PROCESSING", "DONE", "ERROR"):
        status = "READY"
    _REC[0] = []
    model_idx = max(0, min(int(getattr(rack, "p0", 1.0)), len(MODELS) - 1))
    model = MODELS[model_idx]
    all_stems = MODEL_STEMS[model]
    mute_orig = float(getattr(rack, "p2", 1.0)) > 0.5
    stem_bits = int(getattr(rack, "p3", 15.0))
    group_off = getattr(rack, "group_idx", 0) * 9
    active_chs = [ci + 1 for ci in range(9) if getattr(rack, f"ch{ci}", False)]
    in_ch_num = (group_off + active_chs[0]) if active_chs else None
    enabled_stems = [s for s in ALL_STEMS if s in all_stems and bool((stem_bits >> STEM_BITS[s]) & 1)]
    n_active = len(enabled_stems)
    try:
        free = _get_free_channels()
    except Exception:
        free = []
    timing_lbl = {"htdemucs": "~1-2 min"}.get(model, "~2-4 min")
    pulse = 0.5 + 0.5 * math.sin(now * 5.0)

    # ── rack number on the blank plate by the collapse key ───────────────────
    nx, ny = g.X(DM_NUMBER_AT[0]), g.Y(DM_NUMBER_AT[1])
    _text_shadowed(str(ai_idx + 1), nx, ny, 46.0, g, (0.80, 0.82, 0.78, 0.92), 1.0, "c",
                   shadow=(0.0, 0.0, 0.0, 0.7), off=1.6)

    # ── ON key (lettering lights green) ──────────────────────────────────────
    k_on = _level(("dm_on", ai_idx), 1.0 if enabled else 0.0, now)
    _lit_patch(g, lit_tex, (1919.0, 7.0, 2003.0, 61.0), k_on)

    # ── channel keys: digit on each, lit when assigned ───────────────────────
    for ci in range(9):
        kr = _ch_key(ci)
        assigned = bool(getattr(rack, f"ch{ci}", False))
        k = _level(("dm_ch", ai_idx, ci), 1.0 if assigned else 0.0, now)
        _lit_patch(g, lit_tex, (kr[0] + 3 - 8, kr[1] + 2 - 8, kr[2] - 3 + 8, kr[3] - 5 + 8), k)
        sr = R(kr)
        kcx, kcy = (sr[0] + sr[2]) / 2, g.Y((kr[1] + kr[3]) / 2 - 1.5)
        if assigned:
            _text_shadowed(str(group_off + ci + 1), kcx, kcy, DM_FS_KEY, g, (0.86, 1.0, 0.88, 1.0), 1.0, "c",
                           glow=0.7, shadow=(0.0, 0.10, 0.02, 0.6))
        else:
            _text_shadowed(str(group_off + ci + 1), kcx, kcy, DM_FS_KEY, g, DM_STEEL, 1.0, "c",
                           shadow=(0.0, 0.0, 0.0, 0.5), off=1.0)

    # ── model slabs ──────────────────────────────────────────────────────────
    for mi, m in enumerate(MODELS):
        sel = (mi == model_idx) and enabled
        k = _level(("dm_slab", ai_idx, mi), 1.0 if sel else 0.0, now)
        face = DM_SLABS[mi]
        _lit_patch(g, lit_tex, _grow(face, 12.0), k)
        fr = R(face)
        cy = (fr[1] + fr[3]) / 2
        tx = fr[0] + 20 * g.s
        name_col = DM_GREEN if sel else DM_STEEL_DIM
        _text_shadowed(m.upper(), tx, cy, DM_FS_SLAB, g, name_col, 1.0, glow=0.8 if sel else 0.0,
                       max_w=(fr[2] - fr[0]) * 0.58)
        desc = MODEL_DESC.get(m, "").replace("·", "|").replace("·", "|")
        _text_shadowed(desc, fr[2] - 18 * g.s, cy, DM_FS_SLAB_SM, g,
                       (0.62, 1.0, 0.70, 0.95) if sel else DM_STEEL_DIM, 1.0, "r", glow=0.5 if sel else 0.0,
                       max_w=(fr[2] - fr[0]) * 0.34)

    # ── stem pads (lit lens when on), channel steppers ───────────────────────
    for si, stem in enumerate(ALL_STEMS):
        avail = stem in all_stems
        on = bool((stem_bits >> STEM_BITS[stem]) & 1)
        k = _level(("dm_pad", ai_idx, si), 1.0 if (avail and on and enabled) else 0.0, now)
        pad = DM_PADS[si]
        _lit_patch(g, lit_tex, _grow(pad, 12.0), k)
        # (no ON/OFF/NA lettering: the lit groove around the pad is the state)

        prop = STEM_CH_PROPS.get(stem)
        ch_val = int(getattr(rack, prop, 0.0)) if prop else 0
        ch_str = f"ch{ch_val}" if ch_val > 0 else "auto"
        rr = R(DM_READ[si])
        rcx, rcy = (rr[0] + rr[2]) / 2, (rr[1] + rr[3]) / 2
        if not avail:
            _text("--", rcx, rcy, DM_FS_READ, g, (0.20, 0.38, 0.24, 0.55), lv, "c", glow=0.0)
        else:
            _text(ch_str, rcx, rcy, DM_FS_READ, g, DM_GREEN if on else DM_GREEN_DIM, lv, "c",
                  glow=0.8 if on else 0.3, max_w=(rr[2] - rr[0]) * 0.66)

    # ── code rain on both screens (behind the live text) ─────────────────────
    _draw_rain(g, ai_idx, status, enabled, lv, now)

    # ── STEMS screen: left half talks to the user, right half reads out the settings ────────
    sl = R(DM_SCR_L)
    lx = sl[0] + 4 * g.s
    lmax = min((sl[2] - sl[0]) - 14 * g.s, 360 * g.sx)
    if status == "PROCESSING":
        from core import ai_status as _ais
        _text(_ais.headline("demucs", ai_idx, "ANALYZING..."), lx, g.Y(112.0), DM_FS_SCR_BIG, g, DM_GREEN,
              lv * (0.6 + 0.4 * pulse), max_w=lmax)
        _mc = max(8, int(lmax / max(1.0, DM_FS_SCR * g.s * 0.56)))
        _text(_ais.pick("demucs", ai_idx, _mc), lx, g.Y(143.0), DM_FS_SCR, g, DM_GREEN, lv, max_w=lmax)
        bx0, bx1 = sl[0] + 4 * g.s, sl[0] + 4 * g.s + min(lmax, 360 * g.sx)
        by0, by1 = g.Y(176.0), g.Y(170.0)
        _quad(sh, bx0, by0, bx1, by1, (0.05, 0.22, 0.09, 0.85 * lv))
        fill = max(0.02, _ais.get_fraction("demucs", ai_idx))
        _quad(sh, bx0, by0, bx0 + (bx1 - bx0) * fill, by1, (0.40, 1.0, 0.52, 0.95 * lv))
        _quad(sh, bx0, by0 - 2 * g.s, bx0 + (bx1 - bx0) * fill, by1 + 2 * g.s, (0.20, 1.0, 0.35, 0.18 * lv), "ADDITIVE")
    elif status == "DONE":
        _text("DEMUCS COMPLETE", lx, g.Y(112.0), DM_FS_SCR_BIG, g, DM_GREEN, lv, max_w=lmax)
        _text("Stems placed on the timeline.", lx, g.Y(143.0), DM_FS_SCR, g, DM_GREEN, lv, max_w=lmax)
    elif status == "ERROR":
        _text("SOMETHING WENT WRONG", lx, g.Y(112.0), DM_FS_SCR_BIG, g, DM_RED, lv, glow=0.6,
              glow_col=(1.0, 0.25, 0.15), max_w=lmax)
        _text("Check the console, then retry.", lx, g.Y(143.0), DM_FS_SCR, g, DM_GREEN, lv, max_w=lmax)
    else:
        s1 = in_ch_num is not None
        s3 = n_active > 0
        steps = (("1. Select input channel from the rack inputs.", s1),
                 ("2. Pick a model.", s1),
                 ("3. Pick which stems to split.", s3))
        for i, (txt, done) in enumerate(steps):
            _text(txt, lx, g.Y(112.0 + 21.0 * i), DM_FS_SCR + (1.0 if done else 0.0), g, DM_GREEN, lv,
                  glow=DM_GLOW if done else 0.4, max_w=lmax)
            if not done:
                pass
        # sub-hint under the steps once everything is set
        if s1 and s3:
            _text(f"Ready: CH {in_ch_num}  |  {n_active} stem{'s' if n_active != 1 else ''}  |  {model.upper()}",
                  lx, g.Y(188.0), DM_FS_SCR, g, DM_GREEN_DIM, lv, glow=0.4, max_w=lmax)

    sr_ = R(DM_SCR_R)
    rx0 = sr_[0] + 4 * g.s
    rmax = (sr_[2] - sr_[0]) - 14 * g.s
    _text(f"MODEL: {model.upper()}", rx0, g.Y(DM_R_MODEL_Y), DM_FS_SCR, g, DM_GREEN, lv, max_w=rmax)
    _text(f"INPUT: {('CH ' + str(in_ch_num)) if in_ch_num is not None else 'NOT SET'}", rx0, g.Y(DM_R_INPUT_Y),
          DM_FS_SCR, g, DM_GREEN, lv, max_w=rmax)
    if enabled_stems:
        cells = []
        for s in enabled_stems:
            sprop = STEM_CH_PROPS.get(s)
            sval = int(getattr(rack, sprop, 0.0)) if sprop else 0
            cells.append(f"{STEM_ICONS[s]}: " + (f"CH {sval}" if sval > 0 else "AUTO"))
        col_w = rmax / 2.0
        for i, c in enumerate(cells):
            row, col = divmod(i, 2)
            if row >= len(DM_R_STEM_Y):
                break
            _text(c, rx0 + col * col_w, g.Y(DM_R_STEM_Y[row]), DM_FS_SCR, g, DM_GREEN, lv, max_w=col_w - 10 * g.s)
    else:
        _text("No stems selected yet", rx0, g.Y(DM_R_STEM_Y[0]), DM_FS_SCR, g, DM_GREEN, lv, max_w=rmax)
    free_str_s = ", ".join(str(c) for c in free[:6]) if free else "NONE"
    _text(f"CHANNELS AVAILABLE: {free_str_s}", rx0, g.Y(DM_R_FREE_Y), DM_FS_SCR, g, DM_GREEN, lv, max_w=rmax)
    mute_col = DM_GREEN if mute_orig else DM_GREEN_DIM
    _text(f"MUTE ORIGINAL: {'ON' if mute_orig else 'OFF'}", rx0, g.Y(DM_R_MUTE_Y), DM_FS_SCR, g, mute_col, lv,
          glow=0.9 if mute_orig else 0.3, max_w=rmax * 0.7)
    # tick box at the right end of the mute row: filled when muting
    mz = R(_mute_zone())
    bsz = 11.0 * g.s
    bcx, bcy = mz[0] + rmax * 0.78, (mz[1] + mz[3]) / 2
    _quad(sh, bcx - bsz, bcy - bsz, bcx + bsz, bcy + bsz, (0.05, 0.20, 0.08, 0.85 * lv))
    for (a, b, c, d) in ((bcx - bsz, bcy - bsz, bcx + bsz, bcy - bsz + 1.2 * g.s),
                         (bcx - bsz, bcy + bsz - 1.2 * g.s, bcx + bsz, bcy + bsz),
                         (bcx - bsz, bcy - bsz, bcx - bsz + 1.2 * g.s, bcy + bsz),
                         (bcx + bsz - 1.2 * g.s, bcy - bsz, bcx + bsz, bcy + bsz)):
        _quad(sh, a, b, c, d, (0.40, 0.95, 0.52, 0.85 * lv))
    if mute_orig:
        _quad(sh, bcx - bsz * 0.55, bcy - bsz * 0.55, bcx + bsz * 0.55, bcy + bsz * 0.55,
              (0.46, 1.0, 0.58, 0.95 * lv))
        _quad(sh, bcx - bsz * 0.9, bcy - bsz * 0.9, bcx + bsz * 0.9, bcy + bsz * 0.9, (0.2, 1.0, 0.35, 0.18 * lv),
              "ADDITIVE")

    # ── FREE CH strip ────────────────────────────────────────────────────────
    fr_ = R(DM_FREE)
    _text("FREE CH:  " + ("  ".join(str(c) for c in free[:6]) if free else "NONE"), fr_[0] + 12 * g.s,
          (fr_[1] + fr_[3]) / 2, DM_FS_FREE, g, DM_GREEN, lv, glow=0.8, max_w=(fr_[2] - fr_[0]) - 24 * g.s)

    # ── OPTIONS CRT: model notes + last run (text follows the glass: barrel warp) ─────────
    mdesc = MODEL_DESC.get(model, "").replace("·", "|").replace("\u00b7", "|")
    crt_lines = [(mdesc, DM_GREEN)]
    for ln in _wrap("Runs locally - no audio leaves this machine.", 28):
        crt_lines.append((ln, DM_GREEN_DIM))
    crt_lines.append((f"Typically {timing_lbl}.", DM_GREEN_DIM))
    crt_lines.append(("", DM_GREEN))
    if status == "DONE":
        crt_lines.append(("LAST RUN: stems split", DM_GREEN))
        crt_lines.append(("to timeline", DM_GREEN))
    elif status == "ERROR":
        crt_lines.append(("LAST RUN: failed -", DM_RED))
        crt_lines.append(("check console", DM_RED))
    elif status == "PROCESSING":
        crt_lines.append(("LAST RUN: working...", DM_GREEN))
    else:
        crt_lines.append(("LAST RUN: no output yet", DM_GREEN_DIM))
    y = DM_CRT[1] + 8.0
    for txt, col in crt_lines:
        if txt:
            _text_crt(txt, DM_CRT[0], y, DM_FS_CRT, g, col, lv, glow=0.0)
        y += 21.0

    # ── status lamp + word ───────────────────────────────────────────────────
    # READY + the green lamp are baked into the art. READY draws nothing live; other states cover the baked word and
    # print their own. Power off: the lamp fades to a blank bulb and the lettering goes (patch from the lit atlas).
    lcx, lcy, lr = g.X(DM_LAMP[0]), g.Y(DM_LAMP[1]), DM_LAMP[2] * g.s
    k_rt = _level(("dm_rtxt", ai_idx), 1.0 if status != "READY" else 0.0, now)
    _lit_patch(g, lit_tex, DM_P_READY_CLEAN[0], k_rt, DM_P_READY_CLEAN[1])
    _lit_patch(g, lit_tex, DM_P_LAMP_OFF[0], 1.0 - lv, DM_P_LAMP_OFF[1])
    if lv > 0.01:
        if status == "PROCESSING":
            lamp_col, word, wcol = (1.0, 0.55, 0.08), "BUSY", DM_AMBER
            _disc(sh, lcx, lcy, lr * 1.05, (0.10, 0.05, 0.02, 0.97 * lv))
            _disc(sh, lcx, lcy, lr * 0.95, (lamp_col[0], lamp_col[1], lamp_col[2], (0.55 + 0.45 * pulse) * lv))
            _disc(sh, lcx, lcy, lr * 2.2, (lamp_col[0], lamp_col[1], lamp_col[2], 0.20 * (0.4 + 0.6 * pulse) * lv), "ADDITIVE")
        elif status == "ERROR":
            lamp_col, word, wcol = (1.0, 0.18, 0.10), "ERROR", DM_RED
            _disc(sh, lcx, lcy, lr * 1.05, (0.10, 0.02, 0.02, 0.97 * lv))
            _disc(sh, lcx, lcy, lr * 0.95, (lamp_col[0], lamp_col[1], lamp_col[2], 0.95 * lv))
            _disc(sh, lcx, lcy, lr * 2.2, (lamp_col[0], lamp_col[1], lamp_col[2], 0.20 * lv), "ADDITIVE")
        elif status == "DONE":
            lamp_col, word, wcol = (0.30, 1.0, 0.42), "DONE", DM_GREEN
        else:
            lamp_col, word, wcol = (0.30, 1.0, 0.42), "", DM_GREEN
        if word:
            _text(word, g.X(DM_STATUS_X), lcy, 18.0, g, wcol, lv, glow=0.5, glow_col=lamp_col)

    # ── SPLIT STEMS key ──────────────────────────────────────────────────────
    # The key and its "SPLIT STEMS" label are baked into the art. Run states cover the baked label with the wiped key
    # and print their own (dark ink, like the baked one); the rim glow shows when ready / running.
    k_lbl = _level(("dm_split_lbl", ai_idx), 1.0 if status != "READY" else 0.0, now)
    _lit_patch(g, lit_tex, DM_P_SPLIT_CLEAN[0], k_lbl, DM_P_SPLIT_CLEAN[1])
    ready_to_go = (in_ch_num is not None and n_active > 0 and enabled and status == "READY")
    if status == "PROCESSING":
        k_sp = (0.55 + 0.45 * pulse)
    elif status in ("DONE", "ERROR"):
        k_sp = 0.7 if enabled else 0.0
    else:
        k_sp = 1.0 if ready_to_go else 0.0
    k_sp = _level(("dm_split", ai_idx), k_sp, now) if status != "PROCESSING" else k_sp
    if status != "ERROR":
        _lit_patch(g, lit_tex, (1600.0, 455.0, 2052.0, 507.0), k_sp)
    elif k_sp > 0.01:
        sp_ = R(DM_SPLIT)
        _quad(sh, sp_[0], sp_[1], sp_[2], sp_[3], (1.0, 0.15, 0.08, 0.14 * k_sp * lv), "ADDITIVE")
    if status != "READY":
        spr = R(DM_SPLIT)
        scx, scy = (spr[0] + spr[2]) / 2, (spr[1] + spr[3]) / 2
        if status == "PROCESSING":
            sp_lbl, sp_col = "SPLITTING...", (0.05, 0.32 + 0.20 * pulse, 0.10, 1.0)
        elif status == "DONE":
            sp_lbl, sp_col = "DONE  (run again)", (0.04, 0.30, 0.09, 1.0)
        else:
            sp_lbl, sp_col = "ERROR - RETRY", (0.55, 0.06, 0.04, 1.0)
        _text(sp_lbl, scx, scy, DM_FS_SPLIT, g, sp_col, 1.0, "c", glow=0.0, max_w=(spr[2] - spr[0]) * 0.8)

    # ── DEMUCS plaque: logo + circuit traces glow green while the rack is on ─────────
    k_logo = _level(("dm_logo", ai_idx), 1.0 if enabled else 0.0, now)
    _lit_patch(g, lit_tex, DM_LOGO, k_logo)
    _rain_mask[ai_idx] = _REC[0] or []
    _REC[0] = None
    gpu.state.blend_set("ALPHA")
