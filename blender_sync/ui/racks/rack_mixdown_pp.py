# =============================================================================
# ui/racks/rack_mixdown_pp.py
# packApunch layout for the Mixdown rack ("something came back from hell and ate the crew").
#
# The art (rack_mixdown_bg.png, 2000x528) carries every key, slot, screen and the porthole at fixed
# positions. This module draws what changes on top of it (text in the slots, selected-key lights,
# the render bar, the porthole's gimbal rings) and provides the click zones in ART PIXELS - the same
# approach as rack_whisper_pp.py / rack_voicefixer_pp.py.
#
# All coordinates are art pixels, y measured from the TOP of the art: (x0, y0, x1, y1).
# If something sits a little off in Blender, nudge its rectangle here.
#
# The rack header (collapse, rack number, ON / close, the preset display with its two arrows, and the 3x3
# channel keys) is positioned by rack_base.PACKAPUNCH_BTN_HITBOXES["MIXDOWN"] and drawn by the generic rack
# code; the numbers on the channel keys come from there too. This module draws the lit key faces under them.
#
# Extra art (same size as the base):
#   rack_mixdown_bg.png      base: keys unlit (steel), everything else as painted
#   rack_mixdown_bg_lit.png  the fully lit painting; a key's patch of it crossfades in when the key is selected
#   rack_mixdown_bg_off.png  lights OFF (dark screens, dark ON / arrow / save keys); blitted at (1 - power) over
#                            the listed regions so the metal itself never dims
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

MX_PP_SKIN = "packapunch"
MX_FONT = "mixdown.ttf"                        # ui/assets/skins/packapunch/fonts/
MX_PLAIN_SCALE = 0.84                         # normal-font text (keys, middle screens) is drawn at this fraction of the art sizes
MX_FONT_SCALE = 1.30                          # the font is small-caps with a low cap height: this brings it up to the old sizes
MX_ART_W = 2000.0
MX_ART_H = 528.0
MX_LIT_KEY = "rack_mixdown_bg_lit"
MX_OFF_KEY = "rack_mixdown_bg_off"

# ── layout (art px) ──────────────────────────────────────────────────────────
MX_BAR = ((62.0, 186.0, 250.0, 235.0), (62.0, 258.0, 250.0, 306.0))      # render mode: mix to single file / bake per channel
MX_FMT = ((403.0, 215.0, 521.0, 257.0), (539.0, 215.0, 655.0, 258.0))    # WAV, FLAC
MX_SR = ((706.0, 214.0, 757.0, 255.0), (774.0, 215.0, 825.0, 255.0), (842.0, 215.0, 893.0, 255.0))
MX_BD = ((946.0, 214.0, 997.0, 255.0), (1015.0, 214.0, 1066.0, 255.0), (1083.0, 214.0, 1134.0, 255.0))
MX_RNG = ((402.0, 297.0, 517.0, 342.0), (532.0, 297.0, 642.0, 342.0))    # full timeline, custom frames
MX_FSTART = (662.0, 300.0, 887.0, 334.0)
MX_FEND = (912.0, 300.0, 1140.0, 334.0)
MX_PATH = (529.0, 127.0, 1073.0, 167.0)                                  # output path slot
MX_SAVE = (1093.0, 131.0, 1135.0, 169.0)                                 # save as / choose folder key
MX_PLACE = (425.0, 376.0, 750.0, 406.0)                                  # import slots (steppers)
MX_AFTER = (797.0, 376.0, 1120.0, 406.0)
MX_RENDER = (432.0, 442.0, 1117.0, 480.0)                                # the guarded bar
MX_CARDS = ((1486.0, 128.0, 1732.0, 197.0),                              # format / duration / est. size / selected
            (1486.0, 222.0, 1732.0, 291.0),
            (1486.0, 316.0, 1732.0, 385.0),
            (1486.0, 410.0, 1732.0, 479.0))
MX_NOTE = (1486.0, 484.0, 1732.0, 518.0)                                 # printed-line spot under the readouts
MX_BLEND_NOTE = (425.0, 409.0, 750.0, 423.0)                             # small notes under the import slots
MX_HINT_NOTE = (797.0, 409.0, 1120.0, 423.0)

MX_ON = (1781.0, 20.0, 1838.0, 78.0)
MX_ARROWS = ((844.0, 31.0, 880.0, 68.0), (1136.0, 31.0, 1172.0, 68.0))
MX_CLOSE = (1864.0, 21.0, 1918.0, 78.0)
MX_COLLAPSE = (105.0, 29.0, 145.0, 70.0)
MX_TITLE = (412.0, 26.0, 724.0, 76.0)                                    # the blank nameplate top left
MX_FS_TITLE = 38.0
MX_TITLE_INK = (0.0, 0.0, 0.0, 1.0)

MX_CH_CX = (1799.0, 1866.5, 1934.0)                                      # channel keys (centres), 3x3
MX_CH_CY = (203.5, 269.0, 336.0)
MX_CH_W, MX_CH_H = 43.0, 44.0

MX_PORT = (1326.0, 249.0, 66.0)                                          # porthole lens: centre x, y, usable radius

# regions of rack_mixdown_bg_off.png that fade in over the lit art when the rack is off (art px)
MX_OFF_REGIONS = (
    (178.0, 16.0, 246.0, 84.0),                                          # rack number window
    (889.0, 10.0, 1128.0, 86.0),                                         # preset window
    (838.0, 25.0, 886.0, 74.0), (1130.0, 25.0, 1178.0, 74.0),            # preset arrows
    (523.0, 121.0, 1079.0, 173.0),                                       # output path slot
    (1086.0, 124.0, 1142.0, 176.0),                                      # save key
    (656.0, 294.0, 893.0, 340.0), (906.0, 294.0, 1146.0, 340.0),         # start / end slots
    (419.0, 370.0, 756.0, 412.0), (791.0, 370.0, 1126.0, 412.0),         # import slots
    (1478.0, 122.0, 1740.0, 203.0), (1480.0, 216.0, 1740.0, 297.0),      # readouts
    (1480.0, 310.0, 1740.0, 391.0), (1480.0, 404.0, 1740.0, 485.0),
    (1774.0, 14.0, 1844.0, 84.0),                                        # ON key
    (1858.0, 18.0, 1924.0, 84.0),                                        # close key
    (98.0, 22.0, 152.0, 76.0),                                           # collapse key
)

# ── text sizes (art px) and colours ──────────────────────────────────────────
MX_FS_BIG = 30.0
MX_FS_VAL = 20.0
MX_FS_SLOT = 19.0
MX_FS_SMALL = 16.5
MX_FS_NOTE = 11.5
MX_FS_BAR = 23.0

MX_RED = (1.00, 0.43, 0.40, 1.00)               # phosphor text
MX_RED_DIM = (0.78, 0.26, 0.26, 0.92)
MX_RED_FAINT = (0.62, 0.20, 0.20, 0.62)
MX_CORE = (1.00, 0.84, 0.82, 1.00)
MX_GLOW = (1.00, 0.16, 0.16)
MX_GLOW_AMT = 0.8
MX_GLOW_SCALE = 0.5
MX_INK = (0.07, 0.07, 0.09, 0.92)               # dark lettering on the steel bar
MX_NOTE_COL = (0.90, 0.40, 0.38, 0.80)
MX_BAR_TEXT = (1.00, 0.95, 0.92, 1.00)          # render bar lettering: bright, with a dark outline
MX_KEY_INK = (0.07, 0.08, 0.11, 0.95)           # lettering on an unlit steel key
MX_KEY_HOT = (1.00, 0.96, 0.93, 1.00)           # lettering on a lit key

MX_LIT_FADE = 14.0
MX_LIT_GROW = 7.0                               # how far a key's patch extends past the key (to carry its glow)

_lit_state = {}
_port_state = {}                                # rack_idx -> porthole animation state


# ── helpers ──────────────────────────────────────────────────────────────────
def _pp_skin_on():
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas, get_texture as _gt
        return (_gas() == MX_PP_SKIN and _gt("rack_mixdown_bg") is not None
                and _gt(MX_LIT_KEY) is not None)
    except Exception:
        return False


def mx_pp_active():
    """True when the packApunch Mixdown layout is in use (skin loaded and the new art's lit image is present)."""
    return _pp_skin_on()


class _G:
    """Art px -> screen px for one rack."""
    def __init__(self, rx, ry, rw, rh):
        self.rx, self.ry, self.rw, self.rh = rx, ry, rw, rh
        self.sx, self.sy = rw / MX_ART_W, rh / MX_ART_H
        self.s = min(self.sx, self.sy)

    def X(self, ax):
        return self.rx + ax * self.sx

    def Y(self, ay):
        return self.ry + (1.0 - ay / MX_ART_H) * self.rh

    def rect(self, r):
        """(x0, y0, x1, y1) screen rect, y0 = bottom."""
        return (self.X(r[0]), self.Y(r[3]), self.X(r[2]), self.Y(r[1]))


def _in(r, x, y):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def _grow(r, d):
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def _ch_key(i):
    r, c = divmod(i, 3)
    cx, cy = MX_CH_CX[c], MX_CH_CY[r]
    return (cx - MX_CH_W / 2, cy - MX_CH_H / 2, cx + MX_CH_W / 2, cy + MX_CH_H / 2)


def _stepper_zones(r):
    """(minus rect, plus rect) of a stepper slot: the outer fifth at each end."""
    w = r[2] - r[0]
    return ((r[0], r[1], r[0] + w * 0.20, r[3]), (r[2] - w * 0.20, r[1], r[2], r[3]))


# ── click zones ──────────────────────────────────────────────────────────────
def mx_pp_hit(mx, my, rack, rack_idx, rack_x, rack_y, rw, rh):
    """Body click zones in the same form the old layout produced (mixdown_set / mixdown_browse / ...).
    Returns a hit dict (without rack_idx filled by the caller if it is missing) or None for 'nothing here'."""
    g = _G(rack_x, rack_y, rw, rh)
    R = g.rect
    i = rack_idx
    is_bake = float(getattr(rack, "p0", 0.0)) > 0.5
    is_custom = float(getattr(rack, "p4", 0.0)) > 0.5

    for k, r in enumerate(MX_BAR):
        if _in(R(r), mx, my):
            return {'zone': 'mixdown_set', 'rack_idx': i, 'param': 'p0', 'value': float(k)}
    if _in(R(MX_PATH), mx, my) or _in(R(_grow(MX_SAVE, 2.0)), mx, my):
        return {'zone': 'mixdown_browse', 'rack_idx': i}
    for k, r in enumerate(MX_FMT):
        if _in(R(r), mx, my):
            return {'zone': 'mixdown_set', 'rack_idx': i, 'param': 'p1', 'value': float(k)}
    for k, r in enumerate(MX_SR):
        if _in(R(r), mx, my):
            return {'zone': 'mixdown_set', 'rack_idx': i, 'param': 'p2', 'value': (0.0, 0.5, 1.0)[k]}
    for k, r in enumerate(MX_BD):
        if _in(R(r), mx, my):
            return {'zone': 'mixdown_set', 'rack_idx': i, 'param': 'p3', 'value': (0.0, 0.5, 1.0)[k]}
    for k, r in enumerate(MX_RNG):
        if _in(R(r), mx, my):
            return {'zone': 'mixdown_set', 'rack_idx': i, 'param': 'p4', 'value': float(k)}

    # place on channel (mix) / free-vs-replace (bake)
    if _in(R(MX_PLACE), mx, my):
        if not is_bake:
            mn, pl = _stepper_zones(R(MX_PLACE))
            if _in(mn, mx, my):
                return {'zone': 'mixdown_place_ch_minus', 'rack_idx': i}
            if _in(pl, mx, my):
                return {'zone': 'mixdown_place_ch_plus', 'rack_idx': i}
        else:
            cur = float(getattr(rack, "p7", 0.0))
            return {'zone': 'mixdown_set', 'rack_idx': i, 'param': 'p7', 'value': 0.0 if cur > 0.5 else 1.0}

    # after render (mix only): mute -> keep -> remove
    if not is_bake and _in(R(MX_AFTER), mx, my):
        mn, pl = _stepper_zones(R(MX_AFTER))
        cur = float(getattr(rack, "p7", 0.0))
        if _in(mn, mx, my):
            nxt = (0.0 if cur > 0.75 else 0.5 if cur < 0.25 else 1.0)
        elif _in(pl, mx, my):
            nxt = (1.0 if cur < 0.25 else 0.0 if cur > 0.75 else 1.0)
        else:
            nxt = cur
        return {'zone': 'mixdown_set', 'rack_idx': i, 'param': 'p7', 'value': nxt}

    # frame boxes (typing) when custom frames is on
    if is_custom:
        for param, r in (('p5', MX_FSTART), ('p6', MX_FEND)):
            if _in(R(r), mx, my):
                try:
                    from ui.racks.rack_mixdown import _frame_from_norm as _ffn
                    cur = _ffn(float(getattr(rack, param, 0.0)))
                except Exception:
                    cur = int(float(getattr(rack, param, 0.0)))
                return {'zone': 'mixdown_frame_focus', 'rack_idx': i, 'param': param, 'current': cur}
        try:
            import ui.mixer.interaction as _inter
            atf = _inter._active_text_field
            if atf and atf.get('mx_frame') and atf.get('rack_idx') == i:
                return {'zone': 'mixdown_frame_commit', 'rack_idx': i}
        except Exception:
            pass

    if _in(R(MX_RENDER), mx, my):
        return {'zone': 'mixdown_render', 'rack_idx': i}
    return None


# ── drawing helpers ──────────────────────────────────────────────────────────
_font_cache = {}


def _font_id():
    """blf id of the rack font (fonts/mixdown.ttf) or 0 when it is missing (then the normal font is used)."""
    if "id" not in _font_cache:
        fid = 0
        try:
            from ui.racks.pp_text import font_id as _fid
            fid = _fid(MX_FONT, "MIXDOWN") or 0
        except Exception:
            fid = 0
        _font_cache["id"] = fid
    return _font_cache["id"]


def _text(text, x, cy, fs_art, g, col, lv=1.0, align="l", glow=MX_GLOW_AMT, glow_col=MX_GLOW, max_w=None, plain=True):
    """Text with optional soft glow; x = left / centre / right edge by align, cy = vertical centre (screen px)."""
    if lv <= 0.01 or not text:
        return 0.0
    try:
        from ui.racks.pp_text import draw_text as _pdt, text_width as _ptw
    except Exception:
        _pdt = _ptw = None
    fid = _font_id() if (_pdt and not plain) else 0      # plain = the normal Blender font (buttons)
    fs = max(1, int(round(fs_art * (MX_FONT_SCALE if fid else MX_PLAIN_SCALE) * g.s)))
    c = (col[0], col[1], col[2], col[3] * lv)

    def _w(t, f):
        if _ptw:
            return _ptw(t, f, fid)
        return _text_width(t, f) if _text_width else f * 0.6 * len(t)

    tw = _w(text, fs)
    if max_w is not None and tw > max_w > 0:
        fs = max(1, int(fs * max_w / tw))
        tw = _w(text, fs)
    if align == "c":
        x -= tw / 2
    elif align == "r":
        x -= tw
    y = (cy - fs / 2.0 + 1.0) if fid else (cy - fs * 0.36)      # pp_text centres the themed font itself
    if _pdt:
        _pdt(text, x, y, fs, c, glow=glow * lv * MX_GLOW_SCALE, glow_col=glow_col, fid=fid)
    elif _draw_text:
        _draw_text(text, x, y, fs, c)
    return tw


def _text_bold(text, x, cy, fs_art, g, col, lv=1.0, align="l", glow=0.0, glow_col=MX_GLOW, max_w=None, bold=0.8,
               outline=None, plain=False):
    """Heavier text: the same line drawn at a few sub-pixel offsets (the font has no bold), optionally with a dark
    outline behind it. bold = spread in screen px."""
    if lv <= 0.01 or not text:
        return 0.0
    b = max(0.5, bold * max(1.0, g.s * 1.6))
    if outline is not None:
        o = b * 1.6
        for dx, dy in ((-o, 0), (o, 0), (0, -o), (0, o), (-o, -o), (o, o), (-o, o), (o, -o)):
            _text(text, x + dx, cy + dy, fs_art, g, outline, lv, align, glow=0.0, max_w=max_w, plain=plain)
    tw = 0.0
    for dx, dy in ((-b, 0), (b, 0), (0, b * 0.6), (0, -b * 0.6)):
        tw = _text(text, x + dx, cy + dy, fs_art, g, col, lv, align, glow=0.0, max_w=max_w, plain=plain)
    return _text(text, x, cy, fs_art, g, col, lv, align, glow=glow, glow_col=glow_col, max_w=max_w, plain=plain) or tw


def _quad(sh, x0, y0, x1, y1, col, blend="ALPHA"):
    gpu.state.blend_set(blend)
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _tris(sh, pts, col, blend="ADDITIVE"):
    if not pts:
        return
    gpu.state.blend_set(blend)
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": pts}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _disc(sh, cx, cy, r, col, blend="ALPHA", seg=32):
    pts = []
    for i in range(seg):
        a0 = 2 * math.pi * i / seg
        a1 = 2 * math.pi * (i + 1) / seg
        pts += [(cx, cy), (cx + r * math.cos(a0), cy + r * math.sin(a0)), (cx + r * math.cos(a1), cy + r * math.sin(a1))]
    _tris(sh, pts, col, blend)


def _patch(g, tex, key, art_rect, k):
    """Crossfade part of an extra art image (same size as the base) over art_rect (art px) at strength k."""
    try:
        from ui.mixer.texture_cache import blit_texture_tinted as _btt
        if tex is None or k <= 0.003:
            return
        r = g.rect(art_rect)
        u0, u1 = art_rect[0] / MX_ART_W, art_rect[2] / MX_ART_W
        v0, v1 = 1.0 - art_rect[3] / MX_ART_H, 1.0 - art_rect[1] / MX_ART_H
        _btt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], color=(k, k, k, k), blend="ALPHA_PREMULT",
             uv=(u0, v0, u1, v1), key=key)
    except Exception as e:
        print(f"[MIXDOWN] patch error: {e}")


def _level(key, target, now):
    """Smoothly eased 0..1 light level."""
    st = _lit_state.get(key)
    if st is None:
        _lit_state[key] = (float(target), now)
        return float(target)
    lv, t0 = st
    dt = max(0.0, min(0.25, now - t0))
    lv += (target - lv) * min(1.0, dt * MX_LIT_FADE)
    if abs(target - lv) < 0.004:
        lv = float(target)
    _lit_state[key] = (lv, now)
    return lv


def _fit_tail(text, max_chars):
    if len(text) <= max_chars:
        return text
    return "..." + text[-(max_chars - 3):]


# ── screen glitches: the right-hand readouts occasionally fall apart, as if the ship is not quite right ──
MX_GLITCH_CHARS = "#%&@/\\|?!<>=+*01"
MX_GHOST_LINES = ("LIBERATE ME", "NOT HERE", "STILL HERE", "WHERE WE'RE GOING", "SEE")


def _hash(n):
    v = math.sin(n * 12.9898 + 78.233) * 43758.5453
    return v - math.floor(v)


def _glitch(idx, now, force=0.0):
    """(intensity 0..1, frame seed) for readout screen idx. Bursts are rare and short; every so often the whole
    bank goes at once. force (0..1) holds a minimum level (e.g. while in error)."""
    t = now + idx * 3.7
    P = 8.0 + idx * 1.3
    slot = int(t // P)
    loc = t - slot * P
    k = 0.0
    if _hash(slot * 7.1 + idx) < 0.45:
        s0 = _hash(slot * 3.3 + idx * 1.7) * (P - 1.2)
        d = 0.20 + 0.55 * _hash(slot * 13.7 + idx + 5)
        if s0 <= loc < s0 + d:
            k = math.sin(math.pi * (loc - s0) / d) ** 0.6
    # all four together now and then
    GP = 31.0
    gs = int(now // GP)
    gl = now - gs * GP
    if _hash(gs * 5.7 + 2.0) < 0.65:
        g0 = 4.0 + _hash(gs * 1.9) * (GP - 9.0)
        gd = 0.5 + 0.7 * _hash(gs * 4.1)
        if g0 + idx * 0.05 <= gl < g0 + gd:
            k = max(k, math.sin(math.pi * (gl - g0) / gd) ** 0.5 * (0.7 + 0.3 * _hash(gs + idx)))
    return max(k, force), int(now * 24.0) + idx * 977


def _scramble(text, k, seed):
    out = []
    for n, ch in enumerate(text):
        if ch != " " and _hash(seed * 0.37 + n * 5.1) < 0.55 * k:
            out.append(MX_GLITCH_CHARS[int(_hash(seed * 1.7 + n * 3.3) * len(MX_GLITCH_CHARS))])
        else:
            out.append(ch)
    return "".join(out)


# ── porthole: nested gimbal rings that idle cold and spin up red while rendering ──
def _ring_pts(cx, cy, rad, tilt, rot, th, a0, a1, n=40):
    """Triangle list for an elliptical ring (semi-axes rad, rad*|cos tilt|, turned by rot) from angle a0 to a1,
    thickness th (screen px)."""
    ex, ey = rad, rad * max(0.06, abs(math.cos(tilt)))
    cr, sr = math.cos(rot), math.sin(rot)

    def P(a, off):
        x, y = (ex + off) * math.cos(a), (ey + off * max(0.06, abs(math.cos(tilt)))) * math.sin(a)
        return (cx + x * cr - y * sr, cy + x * sr + y * cr)

    pts = []
    for i in range(n):
        t0 = a0 + (a1 - a0) * i / n
        t1 = a0 + (a1 - a0) * (i + 1) / n
        o, p = P(t0, -th / 2), P(t0, th / 2)
        o2, p2 = P(t1, -th / 2), P(t1, th / 2)
        pts += [o, p, p2, o, p2, o2]
    return pts


def _porthole(g, sh, rack_idx, now, lv, running, progress, done_t):
    cx, cy, rr = g.X(MX_PORT[0]), g.Y(MX_PORT[1]), MX_PORT[2] * g.s
    st = _port_state.get(rack_idx)
    if st is None:
        st = {"t": now, "hot": 0.0, "spd": 1.0, "ang": [0.0, 1.7, 3.1, 4.4], "tilt": [0.3, 1.1, 2.0, 0.7]}
        _port_state[rack_idx] = st
    dt = max(0.0, min(0.1, now - st["t"]))
    st["t"] = now
    want_hot = 1.0 if running else 0.0
    st["hot"] += (want_hot - st["hot"]) * min(1.0, dt * (2.2 if running else 1.1))
    want_spd = (3.2 + 4.0 * max(0.0, min(1.0, progress))) if running else 1.0
    st["spd"] += (want_spd - st["spd"]) * min(1.0, dt * 1.6)
    spd = st["spd"]
    rates = (0.55, -0.80, 1.15, -0.35)
    trates = (0.50, 0.38, 0.62, 0.30)
    for i in range(4):
        st["ang"][i] += rates[i] * spd * dt
        st["tilt"][i] += trates[i] * spd * dt
    hot = st["hot"]
    flash = 0.0
    if done_t is not None:                        # finished a moment ago: one cool white pulse
        flash = max(0.0, 1.0 - (now - done_t) / 1.4)
    cold = (0.55, 0.82, 1.00)
    red = (1.00, 0.20, 0.12)
    col = tuple(cold[k] * (1.0 - hot) + red[k] * hot for k in range(3))
    a = lv * (0.55 + 0.35 * hot)

    # soft core
    pulse = 0.5 + 0.5 * math.sin(now * (1.6 + 5.0 * hot))
    for k, (f, al) in enumerate(((0.62, 0.05), (0.46, 0.07), (0.32, 0.09), (0.20, 0.12), (0.10, 0.18))):
        _disc(sh, cx, cy, rr * f * (0.92 + 0.10 * pulse), (col[0], col[1], col[2], al * lv * (0.7 + 0.6 * hot + 0.5 * flash)),
              "ADDITIVE")
    if flash > 0.01:
        _disc(sh, cx, cy, rr * 0.5, (0.85, 0.95, 1.0, 0.12 * flash * lv), "ADDITIVE")

    # rings
    radii = (0.94, 0.74, 0.54, 0.34)
    th = max(1.0, 2.2 * g.s)
    for i in range(4):
        rad = rr * radii[i]
        ang, tilt = st["ang"][i], st["tilt"][i]
        if i == 0:                                # outer ring: three long arcs
            segs = [(0.15 + k * 2.09, 0.15 + k * 2.09 + 1.55) for k in range(3)]
        elif i == 1:                              # second: full ring
            segs = [(0.0, 2 * math.pi)]
        elif i == 2:                              # third: two arcs
            segs = [(0.3, 2.6), (3.45, 5.75)]
        else:                                     # inner: dashes
            segs = [(k * 0.9, k * 0.9 + 0.45) for k in range(7)]
        pts, gpts = [], []
        for (a0, a1) in segs:
            pts += _ring_pts(cx, cy, rad, tilt, ang, th, a0, a1, 28)
            gpts += _ring_pts(cx, cy, rad, tilt, ang, th * 3.2, a0, a1, 28)
        _tris(sh, gpts, (col[0], col[1], col[2], 0.10 * a), "ADDITIVE")
        _tris(sh, pts, (col[0], col[1], col[2], min(1.0, 0.85 * a)), "ADDITIVE")
    # bright node on the outer ring
    nx = cx + rr * 0.94 * math.cos(st["ang"][0] * 1.7)
    ny = cy + rr * 0.94 * 0.9 * math.sin(st["ang"][0] * 1.7)
    _disc(sh, nx, ny, max(1.5, 2.6 * g.s), (min(1.0, col[0] + 0.3), min(1.0, col[1] + 0.3), min(1.0, col[2] + 0.3), 0.8 * lv),
          "ADDITIVE", 12)
    gpu.state.blend_set("ALPHA")


# ── the draw ─────────────────────────────────────────────────────────────────
def mx_pp_draw(rx, ry, rw, rh, rack, rack_idx, scale):
    """packApunch Mixdown rack: everything that changes, drawn over the baked art (the art itself is blitted by
    the generic rack code before / after this, like the other FX racks)."""
    try:
        from ui.racks import rack_mixdown as _mxm
    except Exception as e:                           # pragma: no cover
        print(f"[MIXDOWN] pp: {e}")
        return
    g = _G(rx, ry, rw, rh)
    R = g.rect
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    now = _time.time()
    enabled = bool(getattr(rack, "enabled", True))
    try:
        from ui.racks.rack_base import _pp_preset_level as _ppl
        lv = _ppl(("mx", rack_idx), enabled, _time.monotonic())
    except Exception:
        lv = 1.0 if enabled else 0.0

    # the base art is blitted by the generic rack code (rack_mixdown_bg); this module only fetches the extra images
    try:
        from ui.mixer.texture_cache import get_texture as _gt
        lit_tex = _gt(MX_LIT_KEY)
        off_tex = _gt(MX_OFF_KEY)
    except Exception:
        lit_tex = off_tex = None

    # ── power: the unlit art (dark screens, dark ON / arrow / save keys) fades in over the lights ──
    if lv < 0.995 and off_tex is not None:
        for reg in MX_OFF_REGIONS:
            _patch(g, off_tex, MX_OFF_KEY, reg, 1.0 - lv)

    # ── state ───────────────────────────────────────────────────────────────
    is_bake = float(getattr(rack, "p0", 0.0)) > 0.5
    fmt = _mxm._fmt_from_norm(rack.p1)
    sr = _mxm._sr_from_norm(rack.p2)
    bd = _mxm._bd_from_norm(rack.p3)
    is_custom = float(getattr(rack, "p4", 0.0)) > 0.5
    import_mode = float(getattr(rack, "p7", 0.0))
    try:
        import bpy
        scene = bpy.context.scene
    except Exception:
        scene = None
    fps = (scene.render.fps / scene.render.fps_base) if scene else 24.0
    try:
        f_start, f_end = _mxm._get_timeline_frames(rack, scene) if scene else (1, 250)
    except Exception:
        f_start, f_end = 1, 250
    frames = max(1, f_end - f_start)
    dur_str = _mxm._duration_str(frames, fps)
    try:
        from Racks import get_rack_channels
        assigned = list(get_rack_channels(rack))
    except Exception:
        assigned = []
    n_sel = len(assigned)
    est_mb = _mxm._est_size_mb(frames, fps, sr, bd, max(1, n_sel) if is_bake else 2)
    out_path = getattr(rack, "mixdown_output_path", "")
    ms = _mxm._mx_state
    running = bool(ms.get('running')) and ms.get('rack_idx') == rack_idx
    status = str(ms.get('status_msg', ''))
    done = (not running) and status == 'done ✓' and ms.get('rack_idx') == rack_idx
    has_error = (not running) and ('error' in status.lower()) and ms.get('rack_idx') == rack_idx
    progress = float(ms.get('progress', 0.0)) if running else (1.0 if done else 0.0)
    done_t = None
    if done:
        st0 = _port_state.setdefault(("done", rack_idx), {"t0": now})
        done_t = st0["t0"]
    else:
        _port_state.pop(("done", rack_idx), None)

    # ── selected-key lights: the lit painting's patch fades in over the unlit key ──
    def lit(key_id, rect, on, power=True):
        k = _level(("mx", rack_idx, key_id), 1.0 if on else 0.0, now)
        if power:
            k *= lv
        _patch(g, lit_tex, MX_LIT_KEY, _grow(rect, MX_LIT_GROW), k)
        return k

    def key_label(text, rect, k, fs, max_frac=0.84):
        """Engraved text on a key: dark ink on steel, white-hot when the key is lit (k = how lit, 0..1)."""
        r = R(rect)
        col = tuple(MX_KEY_INK[c] + (MX_KEY_HOT[c] - MX_KEY_INK[c]) * k for c in range(4))
        _text_bold(text, (r[0] + r[2]) / 2, (r[1] + r[3]) / 2, fs, g, col, 1.0, "c", glow=0.9 * k,
                   max_w=(r[2] - r[0]) * max_frac, bold=0.18, plain=True)

    key_label("MIX", MX_BAR[0], lit("mode0", MX_BAR[0], not is_bake), 22.0, 0.80)
    key_label("BAKE", MX_BAR[1], lit("mode1", MX_BAR[1], is_bake), 22.0, 0.80)
    key_label("WAV", MX_FMT[0], lit("fmt0", MX_FMT[0], not (rack.p1 > 0.5)), 24.0)
    key_label("FLAC", MX_FMT[1], lit("fmt1", MX_FMT[1], rack.p1 > 0.5), 24.0)
    for k, (r, t) in enumerate(zip(MX_SR, ("44.1K", "48K", "96K"))):
        key_label(t, r, lit(f"sr{k}", r, abs(rack.p2 - (0.0, 0.5, 1.0)[k]) < 0.2), 18.0, 0.86)
    for k, (r, t) in enumerate(zip(MX_BD, ("16", "24", "32"))):
        key_label(t, r, lit(f"bd{k}", r, abs(rack.p3 - (0.0, 0.5, 1.0)[k]) < 0.2), 22.0)
    key_label("FULL", MX_RNG[0], lit("rng0", MX_RNG[0], not is_custom), 20.0)
    key_label("CUSTOM", MX_RNG[1], lit("rng1", MX_RNG[1], is_custom), 20.0, 0.80)
    for ci in range(9):                              # channel keys: unaffected by power (numbers come from rack_base)
        on = bool(getattr(rack, f"ch{ci}", False))
        lit(f"ch{ci}", _ch_key(ci), on, power=False)
    # power / close / collapse keys: lit with the rack, dark ink when it is off
    key_label("ON", MX_ON, lv, 24.0, 0.80)
    key_label("X", MX_CLOSE, lv, 26.0, 0.60)
    cr = R(MX_COLLAPSE)
    ccx, ccy = (cr[0] + cr[2]) / 2, (cr[1] + cr[3]) / 2
    ch = (cr[3] - cr[1]) * 0.20
    ccol = tuple(MX_KEY_INK[c] + (MX_KEY_HOT[c] - MX_KEY_INK[c]) * lv for c in range(4))
    for off_, sc_ in ((0.0, 1.0),):
        _tris(sh, [(ccx - ch * 1.25, ccy - ch * 0.55), (ccx + ch * 1.25, ccy - ch * 0.55), (ccx, ccy + ch * 0.85)],
              ccol, "ALPHA")

    # ── output path ─────────────────────────────────────────────────────────
    pr = R(MX_PATH)
    pcy = (pr[1] + pr[3]) / 2
    if out_path:
        _text(_fit_tail(out_path, 70), pr[0] + 14 * g.s, pcy, MX_FS_SLOT, g, MX_CORE, lv, glow=0.5,
              max_w=(pr[2] - pr[0]) - 28 * g.s)
    else:
        hint = "(click the key at the right to choose the output file)" if not is_bake else "(click the key at the right to choose a folder)"
        _text(hint, pr[0] + 14 * g.s, pcy, MX_FS_SLOT, g, MX_RED_DIM, lv, glow=0.0, max_w=(pr[2] - pr[0]) - 28 * g.s)
    sv = R(MX_SAVE)
    _text("DIR" if is_bake else "SAVE", (sv[0] + sv[2]) / 2, (sv[1] + sv[3]) / 2, 13.0, g, MX_CORE, lv, "c", glow=0.5,
          max_w=(sv[2] - sv[0]) * 0.78)

    # ── custom frame boxes ──────────────────────────────────────────────────
    def frame_box(rect, label, param, shown):
        r = R(rect)
        cy = (r[1] + r[3]) / 2
        try:
            from ui.mixer.interaction import _active_text_field as _atf
            focused = bool(is_custom and _atf is not None and _atf.get('mx_frame') and _atf.get('param') == param
                           and _atf.get('rack_idx') == rack_idx)
            ftext = _atf['text'] if focused else None
        except Exception:
            focused, ftext = False, None
        col_l = MX_RED_DIM if is_custom else MX_RED_FAINT
        col_v = MX_CORE if focused else (MX_RED if is_custom else MX_RED_FAINT)
        if focused:
            t = max(1.0, 1.4 * g.s)
            _quad(sh, r[0], r[1], r[2], r[1] + t, (1.0, 0.45, 0.42, 0.9))
            _quad(sh, r[0], r[3] - t, r[2], r[3], (1.0, 0.45, 0.42, 0.9))
            _quad(sh, r[0], r[1], r[0] + t, r[3], (1.0, 0.45, 0.42, 0.9))
            _quad(sh, r[2] - t, r[1], r[2], r[3], (1.0, 0.45, 0.42, 0.9))
        _text(label, r[0] + 14 * g.s, cy, MX_FS_SMALL, g, col_l, lv, glow=0.0)
        disp = ftext if focused else str(shown)
        cur = "|" if focused and int(now * 2) % 2 == 0 else ""
        _text(disp + cur, r[2] - 14 * g.s, cy, MX_FS_SLOT, g, col_v, lv, "r", glow=0.6 if is_custom else 0.0)

    frame_box(MX_FSTART, "start", "p5", f_start)
    frame_box(MX_FEND, "end", "p6", f_end)

    # ── import steppers ─────────────────────────────────────────────────────
    def stepper(rect, label, value, active=True):
        r = R(rect)
        cy = (r[1] + r[3]) / 2
        w = r[2] - r[0]
        _text(label, r[0] + w * 0.16, cy, MX_FS_SMALL, g, MX_RED_DIM if active else MX_RED_FAINT, lv, glow=0.0, max_w=w * 0.34)
        if active:
            _text("-", r[0] + w * 0.08, cy, MX_FS_VAL, g, MX_RED, lv, "c", glow=0.6)
            _text("+", r[2] - w * 0.08, cy, MX_FS_VAL, g, MX_RED, lv, "c", glow=0.6)
        _text(value, r[2] - w * 0.16, cy, MX_FS_SLOT, g, MX_CORE if active else MX_RED_FAINT, lv, "r", glow=0.5 if active else 0.0,
              max_w=w * 0.38)

    if is_bake:
        stepper(MX_PLACE, "place on", "replace in-place" if import_mode > 0.5 else "free channels")
        stepper(MX_AFTER, "after render", "mute originals", active=False)
    else:
        place_ch = int(getattr(rack, "mixdown_place_ch", 0))
        stepper(MX_PLACE, "place on", f"ch {place_ch}" if place_ch > 0 else "auto")
        mode_labels = {0.0: "mute originals", 0.5: "keep active", 1.0: "remove originals"}
        closest = min(mode_labels, key=lambda kk: abs(kk - import_mode))
        stepper(MX_AFTER, "after render", mode_labels[closest])

    # ── render bar ──────────────────────────────────────────────────────────
    rb = R(MX_RENDER)
    rcy = (rb[1] + rb[3]) / 2
    rwid = rb[2] - rb[0]
    if running or done or has_error:
        pw = rwid * max(0.0, min(1.0, progress))
        if has_error:
            _quad(sh, rb[0], rb[1], rb[2], rb[3], (1.0, 0.12, 0.08, 0.18 + 0.12 * (0.5 + 0.5 * math.sin(now * 9.0))), "ADDITIVE")
        elif pw > 1.0:
            pulse = 0.5 + 0.5 * math.sin(now * 5.0)
            _quad(sh, rb[0], rb[1], rb[0] + pw, rb[3], (1.0, 0.16, 0.10, 0.34 + 0.10 * pulse if running else 0.22), "ADDITIVE")
            _quad(sh, rb[0] + pw - 5 * g.s, rb[1], rb[0] + pw, rb[3], (1.0, 0.55, 0.45, 0.45 if running else 0.0), "ADDITIVE")
    if running:
        label = f"RENDERING  {int(progress * 100)}%"
        lcol = (0.96, 0.92, 0.90, 1.0)
        glow = 0.6
    elif has_error:
        label = "ERROR  " + status[:60]
        lcol = (1.0, 0.85, 0.82, 1.0)
        glow = 0.5
    elif done:
        label = "DONE - RENDER AGAIN"
        lcol = MX_INK
        glow = 0.0
    else:
        label = "BAKE CHANNELS & REPLACE STRIPS" if is_bake else "RENDER & IMPORT MIXDOWN"
        lcol = MX_INK
        glow = 0.0
    idle_bar = not (running or has_error)
    if idle_bar and not done:
        lcol = MX_BAR_TEXT
        glow = 0.9 * lv
    elif done:
        lcol = MX_BAR_TEXT
        glow = 0.9 * lv
    _text_bold(label, (rb[0] + rb[2]) / 2, rcy, MX_FS_BAR, g, lcol, 1.0, "c", glow=glow, glow_col=MX_GLOW,
               max_w=rwid - 40 * g.s, bold=0.3, plain=True)

    # ── readouts ────────────────────────────────────────────────────────────
    def card(i, big, label):
        r = R(MX_CARDS[i])
        cx = (r[0] + r[2]) / 2
        h = r[3] - r[1]
        w = r[2] - r[0]
        k, seed = _glitch(i, now, force=0.55 if has_error else 0.0)
        if lv < 0.5:
            k = 0.0
        if k < 0.02:
            _text(big, cx, r[1] + h * 0.60, MX_FS_BIG, g, MX_CORE, lv, "c", glow=0.7, max_w=w - 26 * g.s, plain=False)
            _text(label, cx, r[1] + h * 0.24, MX_FS_SMALL, g, MX_RED_DIM, lv, "c", glow=0.0, max_w=w - 26 * g.s, plain=False)
            return
        fr = seed
        ghost = k > 0.55 and _hash(int(now / 7.0) * 3.1 + i) < 0.18       # rarely a different message shows through
        if ghost:
            big_t = MX_GHOST_LINES[int(_hash(int(now / 7.0) + i * 2.0) * len(MX_GHOST_LINES))]
            lab_t = ""
        else:
            big_t = _scramble(big, k, fr) if _hash(fr * 0.9) < 0.75 * k else big
            lab_t = _scramble(label, k * 0.6, fr + 5) if _hash(fr * 1.3) < 0.5 * k else label
        flick = 1.0 - 0.55 * k * _hash(fr * 2.3)                            # the screen stutters
        dx = (_hash(fr * 3.1) - 0.5) * 2.0 * 16.0 * k * g.s                 # sideways tearing
        dy = (_hash(fr * 4.7) - 0.5) * 2.0 * 3.0 * k * g.s
        sp = 3.5 * k * g.s                                                  # colour split
        cy1, cy2 = r[1] + h * 0.60 + dy, r[1] + h * 0.24 + dy
        mw = w - 26 * g.s
        col_big = (MX_CORE[0], MX_CORE[1], MX_CORE[2], 1.0)
        # red / cyan ghosts either side of the text
        _text(big_t, cx + dx - sp, cy1, MX_FS_BIG, g, (1.0, 0.05, 0.05, 0.60), lv * flick, "c", glow=0.0, max_w=mw, plain=False)
        _text(big_t, cx + dx + sp, cy1, MX_FS_BIG, g, (0.15, 0.85, 1.0, 0.45), lv * flick, "c", glow=0.0, max_w=mw, plain=False)
        _text(big_t, cx + dx, cy1, MX_FS_BIG, g, col_big, lv * flick, "c", glow=0.7 * flick, max_w=mw, plain=False)
        _text(lab_t, cx + dx * 0.6, cy2, MX_FS_SMALL, g, MX_RED_DIM, lv * flick, "c", glow=0.0, max_w=mw, plain=False)
        # blacked-out scanlines and bright tear bands
        for n in range(1 + int(3 * k)):
            by = r[1] + _hash(fr * 5.3 + n * 7.7) * (h - 4 * g.s)
            bh = (1.5 + 5.0 * _hash(fr * 6.1 + n)) * g.s
            _quad(sh, r[0] + 3 * g.s, by, r[2] - 3 * g.s, by + bh, (0.06, 0.012, 0.012, 0.92))
            if _hash(fr * 8.9 + n * 2.0) < 0.6:
                oy = by + bh + 1.0 * g.s
                shift = (_hash(fr * 9.3 + n) - 0.5) * 40.0 * k * g.s
                _quad(sh, r[0] + 3 * g.s + max(0.0, shift), oy, r[2] - 3 * g.s + min(0.0, shift), oy + max(1.0, 1.4 * g.s),
                      (1.0, 0.30, 0.25, 0.30 * k), "ADDITIVE")
        # a faint sweep line and static
        sy = r[1] + ((now * 0.9 + i * 0.3) % 1.0) * h
        _quad(sh, r[0] + 3 * g.s, sy, r[2] - 3 * g.s, sy + 2.0 * g.s, (1.0, 0.25, 0.2, 0.10 * k), "ADDITIVE")
        if k > 0.35:
            for n in range(int(14 * k)):
                px = r[0] + 4 * g.s + _hash(fr * 11.1 + n * 1.3) * (w - 8 * g.s)
                py = r[1] + 3 * g.s + _hash(fr * 12.7 + n * 2.9) * (h - 6 * g.s)
                sz = (1.0 + 2.0 * _hash(fr + n)) * g.s
                _quad(sh, px, py, px + sz * 2.4, py + sz * 0.7, (1.0, 0.5, 0.45, 0.35 * k), "ADDITIVE")

    card(0, fmt, f"{bd}-BIT / {sr // 1000}K")
    card(1, dur_str, f"DURATION / FRAMES {f_start}-{f_end}")
    size_v = f"~{round(est_mb * max(1, n_sel), 1)} MB" if is_bake else f"~{round(est_mb, 1)} MB"
    card(2, size_v, "est. size")
    sel_chs = [getattr(rack, 'group_idx', 0) * 9 + j + 1 for j in range(9) if getattr(rack, f'ch{j}', False)]
    if sel_chs:
        ch_str = ", ".join(str(c) for c in sel_chs)
        if len(ch_str) > 11:
            ch_str = ", ".join(str(c) for c in sel_chs[:3]) + f" +{len(sel_chs) - 3}"
    else:
        ch_str = "0 ch"
    card(3, ch_str, "selected")

    nt = R(MX_NOTE)
    try:
        import bpy
        blend_base = (os.path.splitext(os.path.basename(bpy.data.filepath))[0] if bpy.data.filepath else "untitled")
    except Exception:
        blend_base = "untitled"
    ncx = (nt[0] + nt[2]) / 2
    nh = nt[3] - nt[1]
    _text("BLEND SAVED BEFORE RENDER", ncx, nt[1] + nh * 0.72, MX_FS_NOTE, g,
          MX_NOTE_COL, lv, "c", glow=0.0, max_w=(nt[2] - nt[0]), plain=False)
    _text("RENDERS OFFLINE", ncx, nt[1] + nh * 0.26, MX_FS_NOTE, g,
          MX_NOTE_COL, lv, "c", glow=0.0, max_w=(nt[2] - nt[0]), plain=False)

    # ── nameplate title ──────────────────────────────────────────────────────
    tr = R(MX_TITLE)
    _text_bold("MIXDOWN", (tr[0] + tr[2]) / 2, (tr[1] + tr[3]) / 2, MX_FS_TITLE, g, MX_TITLE_INK, 1.0, "c", glow=0.0,
               max_w=(tr[2] - tr[0]) * 0.86, bold=0.28, plain=False)

    # ── porthole ────────────────────────────────────────────────────────────
    try:
        _porthole(g, sh, rack_idx, now, lv, running, progress, done_t)
    except Exception as e:
        print(f"[MIXDOWN] porthole error: {e}")
    gpu.state.blend_set("ALPHA")
