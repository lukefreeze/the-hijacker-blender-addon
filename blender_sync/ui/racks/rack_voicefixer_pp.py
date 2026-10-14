# =============================================================================
# ui/racks/rack_voicefixer_pp.py
# packApunch layout for the Voice Fixer rack.
#
# The art (rack_voicefixer_bg.png, 1758x497) carries every key, window, screen and lamp at fixed
# positions, so this module draws the live content (text, lit windows, key glows, lamp colour) and
# provides the click zones in ART PIXELS, the same approach as rack_knnvc.py / rack_piper.py.
#
# All coordinates are art pixels, y measured from the TOP of the art: (x0, y0, x1, y1).
# If something sits a little off in Blender, nudge its rectangle here.
#
# Lit art: if  rack_voicefixer_bg_lit.png  (same size, backlit windows + lit keys) is in the skin
# folder, the selected mode's window and key crossfade to it. Without it, a warm-white glow is
# drawn over the paper window and key instead.
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
    from ui.racks.rack_voicefixer import (_check_voicefixer, _MODE_LABELS, _MODE_DESCS,
                                          _READY, _PROCESSING, _DONE, _ERROR)
except Exception:                                   # pragma: no cover
    _check_voicefixer = lambda: True
    _MODE_LABELS = ["STANDARD", "SMOOTH", "AGGRESSIVE"]
    _MODE_DESCS = ["Best for most recordings", "Softer - may alter voice slightly", "Maximum restoration power"]
    _READY, _PROCESSING, _DONE, _ERROR = "READY", "PROCESSING", "DONE", "ERROR"

VF_PP_SKIN = "packapunch"
VF_ART_W = 1758.0
VF_ART_H = 497.0

# ── layout (art px) ──────────────────────────────────────────────────────────
VF_COLLAPSE = (97.0, 8.0, 127.0, 32.0)          # chip drawn over the left end of the hazard band
VF_BADGE = (131.0, 8.0, 161.0, 32.0)            # rack number chip next to it
VF_ON = (1586.0, 9.0, 1633.0, 57.0)
VF_CLOSE = (1647.0, 9.0, 1695.0, 57.0)

VF_CHKEY_X0 = 774.3                             # nine channel keys across the top centre
VF_CHKEY_PITCH = 37.8
VF_CHKEY_W = 36.0
VF_CHKEY_Y = (13.0, 51.0)

VF_SRC_SCREEN = (103.0, 67.0, 527.0, 103.0)     # small source screen (glass)

VF_MODE_WIN_X = (91.0, 453.0)                   # mode window frames (click area)
VF_MODE_WIN_Y = ((169.0, 238.0), (241.0, 308.0), (315.0, 383.0))
VF_MODE_PAPER_X = (115.0, 428.0)                # the paper itself (lit patch + text area)
VF_MODE_PAPER_Y = ((182.0, 228.0), (255.0, 297.0), (328.0, 370.0))
VF_MODE_KEY_X = (477.0, 534.0)                  # clear square keys
VF_MODE_KEY_Y = ((176.0, 233.0), (247.0, 305.0), (322.0, 379.0))

VF_SCREEN = (662.0, 107.0, 1165.0, 230.0)       # centre screen: largest box inside the screen matte (text area)
VF_ENHANCE = (656.0, 276.0, 893.0, 330.0)       # wide bars (frame = click area)
VF_PREVIEW = (943.0, 276.0, 1168.0, 330.0)
VF_ENH_BAR = (667.0, 291.0, 878.0, 315.0)       # the steel bar inside each frame (label area)
VF_PRV_BAR = (954.0, 291.0, 1157.0, 315.0)
VF_OUT_DEC = (702.0, 344.0, 727.0, 376.0)
VF_OUT_BOX = (728.0, 344.0, 766.0, 376.0)
VF_OUT_INC = (768.0, 344.0, 794.0, 376.0)
VF_OUT_LABEL_X = 808.0
VF_INFO = (1314.0, 112.0, 1662.0, 342.0)        # right screen: largest box inside the screen matte (text area)
VF_STATUS = (662.0, 436.0, 1196.0, 466.0)       # status slot
VF_LAMP = (1693.0, 445.0, 36.0)                 # amber lamp: centre x, centre y, radius

# ── text sizes (art px) and colours ──────────────────────────────────────────
VF_FS_BIG = 26.0
VF_FS_MED = 18.0
VF_FS_SMALL = 15.0
VF_FS_TINY = 14.0
VF_FS_LABEL = 21.0                              # mode names on the paper
VF_FS_DESC = 13.0                               # mode descriptions on the paper

VF_GREEN = (0.46, 1.00, 0.58, 1.00)             # phosphor text
VF_GREEN_DIM = (0.28, 0.62, 0.36, 0.95)
VF_GREEN_GLOW = (0.20, 1.00, 0.35)
VF_GLOW = 0.9
VF_GLOW_SCALE = 0.5
VF_AMBER = (1.00, 0.62, 0.12, 1.00)
VF_RED = (1.00, 0.30, 0.15, 1.00)
VF_INK = (0.14, 0.09, 0.05, 0.96)               # printed on the paper labels
VF_INK_DIM = (0.14, 0.09, 0.05, 0.80)
VF_INK_METAL = (0.07, 0.06, 0.05, 0.95)         # text on the steel bars
VF_CREAM = (0.93, 0.88, 0.74, 1.00)             # chips over the hazard band

VF_LIT_FADE = 14.0                              # window light fade speed (1/s)

# areas of rack_voicefixer_bg_lit.png that crossfade in (art px; slightly larger than the frames so the glow shows)
VF_LIT_WIN = ((89.0, 167.0, 455.0, 240.0), (89.0, 239.0, 455.0, 310.0), (89.0, 313.0, 455.0, 385.0))
VF_LIT_KEY = ((471.0, 170.0, 540.0, 239.0), (471.0, 241.0, 540.0, 311.0), (471.0, 316.0, 540.0, 385.0))

# glass-frame overlay: the bezel around the two big screens, cut by the screen matte. Drawn over the text so
# anything that reaches past the glass is hidden behind the bezel.
VF_FRAME_ZONES = ((600.0, 70.0, 1215.0, 268.0), (1262.0, 70.0, 1710.0, 385.0))

_lit_state = {}


# ── helpers ──────────────────────────────────────────────────────────────────
def _pp_skin_on():
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas, get_texture as _gt
        return _gas() == VF_PP_SKIN and _gt("rack_voicefixer_bg") is not None
    except Exception:
        return False


def vf_pp_active():
    """True when the packApunch Voice Fixer layout is in use (skin loaded AND VoiceFixer found, since the skin
    is withheld behind the setup warning until then)."""
    return _pp_skin_on() and _check_voicefixer()


class _G:
    """Art px -> screen px for one rack."""
    def __init__(self, rx, ry, rw, rh):
        self.rx, self.ry, self.rw, self.rh = rx, ry, rw, rh
        self.sx, self.sy = rw / VF_ART_W, rh / VF_ART_H
        self.s = min(self.sx, self.sy)

    def X(self, ax):
        return self.rx + ax * self.sx

    def Y(self, ay):
        return self.ry + (1.0 - ay / VF_ART_H) * self.rh

    def rect(self, r):
        """(x0, y0, x1, y1) screen rect, y0 = bottom."""
        return (self.X(r[0]), self.Y(r[3]), self.X(r[2]), self.Y(r[1]))

    def ch_key(self, i):
        x0 = VF_CHKEY_X0 + VF_CHKEY_PITCH * i
        return (x0, VF_CHKEY_Y[0], x0 + VF_CHKEY_W, VF_CHKEY_Y[1])


def _in(r, x, y):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def _mode_win(i):
    return (VF_MODE_WIN_X[0], VF_MODE_WIN_Y[i][0], VF_MODE_WIN_X[1], VF_MODE_WIN_Y[i][1])


def _mode_paper(i):
    return (VF_MODE_PAPER_X[0], VF_MODE_PAPER_Y[i][0], VF_MODE_PAPER_X[1], VF_MODE_PAPER_Y[i][1])


def _mode_key(i):
    return (VF_MODE_KEY_X[0], VF_MODE_KEY_Y[i][0], VF_MODE_KEY_X[1], VF_MODE_KEY_Y[i][1])


def vf_pp_hit(mx, my, rack, ai_idx, rack_x, rack_y, rw, rh):
    """Click zones for the packApunch layout (same zone names the old layout used). Only call while vf_pp_active()."""
    g = _G(rack_x, rack_y, rw, rh)
    R = g.rect
    if _in(R(VF_COLLAPSE), mx, my):
        return {'zone': 'ai_collapse', 'ai_idx': ai_idx}
    if _in(R(VF_CLOSE), mx, my):
        return {'zone': 'ai_delete', 'ai_idx': ai_idx}
    if _in(R(VF_ON), mx, my):
        return {'zone': 'ai_on_off', 'ai_idx': ai_idx}
    for ci in range(9):
        if _in(R(g.ch_key(ci)), mx, my):
            return {'zone': 'ai_channel_btn', 'ai_idx': ai_idx, 'ch_idx': ci}
    for mi in range(3):
        if _in(R(_mode_key(mi)), mx, my) or _in(R(_mode_win(mi)), mx, my):
            return {'zone': 'ai_vf_mode', 'ai_idx': ai_idx, 'mode_idx': mi}
    if _in(R(VF_ENHANCE), mx, my):
        return {'zone': 'ai_vf_enhance', 'ai_idx': ai_idx}
    if _in(R(VF_PREVIEW), mx, my):
        return {'zone': 'ai_vf_preview', 'ai_idx': ai_idx}
    if _in(R(VF_OUT_DEC), mx, my):
        return {'zone': 'ai_vf_outch_dec', 'ai_idx': ai_idx}
    if _in(R(VF_OUT_INC), mx, my):
        return {'zone': 'ai_vf_outch_inc', 'ai_idx': ai_idx}
    return None


def _text(text, x, cy, fs_art, g, col, lv=1.0, align="l", glow=VF_GLOW, glow_col=VF_GREEN_GLOW, max_w=None):
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
        _pdt(text, x, y, fs, c, glow=glow * lv * VF_GLOW_SCALE, glow_col=glow_col)
    elif _draw_text:
        _draw_text(text, x, y, fs, c)
    return tw


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


def _lit_patch(g, tex, art_rect, k):
    """Crossfade the backlit art (tex) over art_rect (art px) at strength k. Returns False if there is no lit art."""
    try:
        from ui.mixer.texture_cache import blit_texture_tinted as _btt
        if tex is None:
            return False
        if k <= 0.003:
            return True
        r = g.rect(art_rect)
        u0, u1 = art_rect[0] / VF_ART_W, art_rect[2] / VF_ART_W
        v0, v1 = 1.0 - art_rect[3] / VF_ART_H, 1.0 - art_rect[1] / VF_ART_H
        _btt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], color=(k, k, k, k), blend="ALPHA_PREMULT",
             uv=(u0, v0, u1, v1), key="rack_voicefixer_bg_lit")
        return True
    except Exception as e:
        print(f"[VOICEFIXER] lit patch error: {e}")
        return False


def _level(key, target, now):
    """Smoothly eased 0..1 light level."""
    st = _lit_state.get(key)
    if st is None:
        _lit_state[key] = (float(target), now)
        return float(target)
    lv, t0 = st
    dt = max(0.0, min(0.25, now - t0))
    lv += (target - lv) * min(1.0, dt * VF_LIT_FADE)
    if abs(target - lv) < 0.004:
        lv = float(target)
    _lit_state[key] = (lv, now)
    return lv


def _screen_frames(g):
    """Bezel overlay over the screen text, clipped by the screen matte (skipped if the PNG is missing)."""
    try:
        from ui.mixer.texture_cache import get_texture as _gt, blit_texture as _bt
        tex = _gt("rack_voicefixer_screen_frame")
        if tex is None:
            return
        for z in VF_FRAME_ZONES:
            r = g.rect(z)
            uv = (z[0] / VF_ART_W, 1.0 - z[3] / VF_ART_H, z[2] / VF_ART_W, 1.0 - z[1] / VF_ART_H)
            _bt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], key="rack_voicefixer_screen_frame",
                blend="ALPHA_PREMULT", uv=uv)
    except Exception as e:
        print(f"[VOICEFIXER] screen frame error: {e}")


def vf_pp_draw(rx, ry, rw, rh, rack, ai_idx, scale):
    """packApunch Voice Fixer rack: everything that changes, drawn over the baked art."""
    g = _G(rx, ry, rw, rh)
    R = g.rect
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    now = _time.time()
    enabled = bool(getattr(rack, "enabled", True))
    try:
        from ui.racks.rack_base import _pp_preset_level as _ppl
        lv = _ppl(("vf", ai_idx), enabled, _time.monotonic())
    except Exception:
        lv = 1.0 if enabled else 0.0

    # ── power: darken the whole unit as the rack goes off ────────────────────
    if lv < 0.997:
        _quad(sh, rx, ry, rx + rw, ry + rh, (0.0, 0.0, 0.0, 0.55 * (1.0 - lv)))

    status = getattr(rack, "ai_status", _READY)
    mode_idx = max(0, min(2, int(getattr(rack, "p0", 0.0))))
    active_chs = [ci + 1 for ci in range(9) if getattr(rack, f"ch{ci}", False)]
    group_off = getattr(rack, "group_idx", 0) * 9

    # ── collapse chip + rack number over the hazard band ─────────────────────
    cr = R(VF_COLLAPSE)
    _quad(sh, cr[0], cr[1], cr[2], cr[3], (0.09, 0.08, 0.06, 0.94))
    cx_, cy_ = (cr[0] + cr[2]) / 2, (cr[1] + cr[3]) / 2
    aw, ah = (cr[2] - cr[0]) * 0.28, (cr[3] - cr[1]) * 0.22
    _tri(sh, [(cx_ - aw, cy_ - ah), (cx_ + aw, cy_ - ah), (cx_, cy_ + ah * 1.3)], VF_CREAM)
    br = R(VF_BADGE)
    _quad(sh, br[0], br[1], br[2], br[3], (0.09, 0.08, 0.06, 0.94))
    _text(str(ai_idx + 1), (br[0] + br[2]) / 2, (br[1] + br[3]) / 2, 19.0, g, VF_CREAM, 1.0, "c", glow=0.0)

    # ── ON / close keys ──────────────────────────────────────────────────────
    onr = R(VF_ON)
    if enabled:
        _ocx, _ocy = (onr[0] + onr[2]) / 2, (onr[1] + onr[3]) / 2
        _disc(sh, _ocx, _ocy, (onr[2] - onr[0]) * 0.46, (0.30, 1.0, 0.40, 0.16), "ADDITIVE")
        _disc(sh, _ocx, _ocy, (onr[2] - onr[0]) * 0.26, (0.55, 1.0, 0.60, 0.14), "ADDITIVE")
    _text("ON", (onr[0] + onr[2]) / 2, (onr[1] + onr[3]) / 2, 15.0, g, (0.04, 0.12, 0.04, 0.9) if enabled else (0.05, 0.10, 0.05, 0.6),
          1.0, "c", glow=0.0)
    clr = R(VF_CLOSE)
    _text("X", (clr[0] + clr[2]) / 2, (clr[1] + clr[3]) / 2, 19.0, g, (0.98, 0.86, 0.80, 0.95), 1.0, "c", glow=0.0)

    # ── channel keys (digits + glow when assigned) ───────────────────────────
    for ci in range(9):
        kr = R(g.ch_key(ci))
        kcx, kcy = (kr[0] + kr[2]) / 2, (kr[1] + kr[3]) / 2
        assigned = bool(getattr(rack, f"ch{ci}", False))
        if assigned:
            _disc(sh, kcx, kcy, (kr[2] - kr[0]) * 0.34, (1.0, 0.72, 0.22, 0.30 * lv), "ADDITIVE")
            _disc(sh, kcx, kcy, (kr[2] - kr[0]) * 0.20, (1.0, 0.86, 0.50, 0.35 * lv), "ADDITIVE")
        _text(str(group_off + ci + 1), kcx, kcy, 14.0, g,
              (0.10, 0.06, 0.02, 1.0) if assigned else (0.20, 0.15, 0.08, 0.88), 1.0, "c", glow=0.0)

    # ── SOURCE screen ────────────────────────────────────────────────────────
    sr = R(VF_SRC_SCREEN)
    scy = (sr[1] + sr[3]) / 2
    src_disp = f"SRC: CH {active_chs[0]}" if active_chs else "SRC: USE RAIL"
    _text(src_disp, sr[0] + 14 * g.s, scy, VF_FS_MED, g, VF_GREEN if active_chs else VF_GREEN_DIM, lv,
          max_w=(sr[2] - sr[0]) - 28 * g.s)

    # ── mode windows: printed labels, lit selection ──────────────────────────
    try:
        from ui.mixer.texture_cache import get_texture as _gt_lit
        lit_tex = _gt_lit("rack_voicefixer_bg_lit")
    except Exception:
        lit_tex = None
    for mi in range(3):
        sel = (mi == mode_idx) and enabled
        k = _level(("vf_mode", ai_idx, mi), 1.0 if sel else 0.0, now)
        pr, kr = R(_mode_paper(mi)), R(_mode_key(mi))
        art_has = _lit_patch(g, lit_tex, VF_LIT_WIN[mi], k) and _lit_patch(g, lit_tex, VF_LIT_KEY[mi], k)
        if not art_has and k > 0.003:
            # no lit art yet: warm-white glow over the paper and a lamp in the key
            _quad(sh, pr[0], pr[1], pr[2], pr[3], (1.0, 0.95, 0.78, 0.50 * k), "ADDITIVE")
            _quad(sh, pr[0] + 6 * g.s, pr[1] + 4 * g.s, pr[2] - 6 * g.s, pr[3] - 4 * g.s, (1.0, 0.97, 0.88, 0.20 * k), "ADDITIVE")
            kcx, kcy = (kr[0] + kr[2]) / 2, (kr[1] + kr[3]) / 2
            _quad(sh, kr[0] + 8 * g.s, kr[1] + 8 * g.s, kr[2] - 8 * g.s, kr[3] - 8 * g.s, (1.0, 0.98, 0.9, 0.62 * k), "ADDITIVE")
            _disc(sh, kcx, kcy, (kr[2] - kr[0]) * 0.55, (1.0, 0.95, 0.8, 0.10 * k), "ADDITIVE")
        ptop = _mode_paper(mi)
        pcy_big = g.Y(ptop[1] + (ptop[3] - ptop[1]) * 0.33)
        pcy_sm = g.Y(ptop[1] + (ptop[3] - ptop[1]) * 0.75)
        tx = pr[0] + 14 * g.s
        ink = VF_INK if sel else VF_INK_DIM
        _mw = (pr[2] - pr[0]) - 28 * g.s
        _text(_MODE_LABELS[mi], tx, pcy_big, VF_FS_LABEL, g, ink, lv, glow=0.0, max_w=_mw)
        _text(_MODE_DESCS[mi], tx, pcy_sm, VF_FS_DESC, g, ink, lv, glow=0.0, max_w=_mw)

    # ── centre screen: state read-out ────────────────────────────────────────
    scr = R(VF_SCREEN)
    cxs = (scr[0] + scr[2]) / 2

    def _line(txt, art_y, fs, col, glow=VF_GLOW, a=1.0):
        c = (col[0], col[1], col[2], col[3] * a)
        _text(txt, cxs, g.Y(art_y), fs, g, c, lv, "c", glow=glow, max_w=(scr[2] - scr[0]) - 40 * g.s)

    if status == _PROCESSING:
        pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(now * 5.0))
        _line("RESTORING...", 150, VF_FS_BIG, VF_GREEN, a=pulse)
        _line("Neural vocoder rebuilding speech", 188, VF_FS_SMALL, VF_GREEN_DIM, 0.5)
        _line("This may take 30-90 seconds", 212, VF_FS_SMALL, VF_GREEN_DIM, 0.4)
    elif status == _DONE:
        _line("RESTORATION DONE", 140, VF_FS_BIG, VF_GREEN)
        info = "Output placed in VSE"
        try:
            import glob, tempfile, wave
            matches = sorted(glob.glob(os.path.join(tempfile.gettempdir(), f"pb_vf_{ai_idx}_*.wav")))
            if matches:
                wav = matches[-1]
                sz = os.path.getsize(wav)
                sz_str = f"{sz // 1024}KB" if sz < 1024 * 1024 else f"{sz // (1024 * 1024)}MB"
                with wave.open(wav, "r") as wf:
                    dur = wf.getnframes() / wf.getframerate()
                    sr_hz = wf.getframerate()
                info = f"{dur:.1f}s   |   {sr_hz}Hz   |   {sz_str}"
        except Exception:
            pass
        _line(info, 186, VF_FS_SMALL, VF_GREEN_DIM, 0.5)
    elif status == _ERROR:
        _line("ERROR - CHECK CONSOLE", 150, VF_FS_BIG, VF_RED)
        _line("Assign a source channel and try again", 190, VF_FS_SMALL, VF_GREEN_DIM, 0.5)
    else:
        if not active_chs:
            _line("ASSIGN A SOURCE CHANNEL", 150, VF_FS_BIG, VF_GREEN_DIM, 0.5)
            _line("use the channel keys above the screen", 190, VF_FS_SMALL, VF_GREEN_DIM, 0.4)
        else:
            _line("READY TO RESTORE", 140, VF_FS_BIG, VF_GREEN)
            _line(f"Src: CH{active_chs[0]}   |   Mode: {_MODE_LABELS[mode_idx]}", 178, VF_FS_SMALL, VF_GREEN_DIM, 0.6)
            _line("VoiceFixer - neural vocoder restoration", 204, VF_FS_TINY, VF_GREEN_DIM, 0.4)

    # ── ENHANCE / PREVIEW (printed on the steel bars) ────────────────────────
    _has_out = _is_playing = False
    try:
        from core.ai_voicefixer import has_rack_output, is_rack_previewing
        _has_out = has_rack_output(ai_idx)
        _is_playing = is_rack_previewing(ai_idx)
    except Exception:
        pass
    eb, pb = R(VF_ENH_BAR), R(VF_PRV_BAR)
    if status == _PROCESSING:
        _quad(sh, eb[0], eb[1], eb[2], eb[3], (0.25, 1.0, 0.40, 0.14 + 0.10 * (0.5 + 0.5 * math.sin(now * 5.0))), "ADDITIVE")
    if _is_playing:
        _quad(sh, pb[0], pb[1], pb[2], pb[3], (0.25, 1.0, 0.40, 0.18), "ADDITIVE")
    e_lbl = "RESTORING..." if status == _PROCESSING else "ENHANCE"
    _text(e_lbl, (eb[0] + eb[2]) / 2, (eb[1] + eb[3]) / 2, VF_FS_MED, g, VF_INK_METAL, lv, "c", glow=0.0)
    p_col = VF_INK_METAL if (_has_out or _is_playing) else (VF_INK_METAL[0], VF_INK_METAL[1], VF_INK_METAL[2], 0.45)
    _text("[] STOP" if _is_playing else "> PREVIEW", (pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2, VF_FS_MED, g, p_col, lv, "c", glow=0.0)

    # ── OUT CH selector ──────────────────────────────────────────────────────
    out_ch_val = int(getattr(rack, "p3", 0.0)) or (active_chs[0] + 1 if active_chs else 2)
    out_ch_val = max(1, min(9, out_ch_val))
    ob = R(VF_OUT_BOX)
    _text(str(out_ch_val), (ob[0] + ob[2]) / 2, (ob[1] + ob[3]) / 2, 22.0, g, VF_GREEN, lv, "c")
    _text("OUT CH", g.X(VF_OUT_LABEL_X), (ob[1] + ob[3]) / 2, VF_FS_SMALL, g, VF_INK, lv, glow=0.0)

    # ── right screen: about notes ────────────────────────────────────────────
    ir = R(VF_INFO)
    notes = [
        "VoiceFixer", "Neural vocoder", "speech restoration", "",
        "Handles: noise,", "reverb, low-res,", "clipping - in", "one pass", "",
        "Output: 44.1kHz", "CUDA / MPS / CPU", "", "~625MB models", "auto-downloaded",
    ]
    ny = VF_INFO[1] + 14.0
    _step = min(18.5, (VF_INFO[3] - VF_INFO[1] - 24.0) / (len(notes) - 1))
    for note in notes:
        if note:
            _text(note, ir[0] + 16 * g.s, g.Y(ny), VF_FS_TINY + 1, g, VF_GREEN_DIM, lv, glow=0.5,
                  max_w=(ir[2] - ir[0]) - 32 * g.s)
        ny += _step

    # ── bezel over the two big screens (hides any text that reaches past the glass) ─────
    _screen_frames(g)

    # ── status slot ──────────────────────────────────────────────────────────
    sb = R(VF_STATUS)
    src_str = f"SRC:CH{active_chs[0]}" if active_chs else "SRC:unset"
    _text(f"ENGINE: VoiceFixer  |  {src_str}  |  OUT:CH{out_ch_val}  |  MODE:{_MODE_LABELS[mode_idx]}  |  OFFLINE",
          sb[0] + 12 * g.s, (sb[1] + sb[3]) / 2, VF_FS_TINY, g, VF_GREEN_DIM, lv, glow=0.6,
          max_w=(sb[2] - sb[0]) - 24 * g.s)

    # ── status lamp ──────────────────────────────────────────────────────────
    lcx, lcy, lr = g.X(VF_LAMP[0]), g.Y(VF_LAMP[1]), VF_LAMP[2] * g.s
    if status == _PROCESSING:
        pul = 0.5 + 0.5 * math.sin(now * 6.0)
        _disc(sh, lcx, lcy, lr * 0.78, (1.0, 0.45, 0.05, (0.35 + 0.35 * pul) * lv), "ADDITIVE")
    elif status == _DONE:
        _disc(sh, lcx, lcy, lr * 0.80, (0.10, 0.85, 0.25, 0.62 * lv), "ALPHA")
        _disc(sh, lcx, lcy, lr * 0.80, (0.30, 1.0, 0.45, 0.25 * lv), "ADDITIVE")
    elif status == _ERROR:
        _disc(sh, lcx, lcy, lr * 0.80, (0.85, 0.08, 0.04, 0.65 * lv), "ALPHA")
        _disc(sh, lcx, lcy, lr * 0.80, (1.0, 0.25, 0.15, 0.25 * lv), "ADDITIVE")
    else:
        _disc(sh, lcx, lcy, lr * 0.78, (1.0, 0.60, 0.10, 0.22 * lv), "ADDITIVE")
    gpu.state.blend_set("ALPHA")
