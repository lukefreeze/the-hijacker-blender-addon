"""packApunch collapsed racks.

Every packApunch rack has a one-row "collapsed" strip (1888 x 71 art px) cut from its own header, plus a
lights-off twin. This module draws the live parts over the strip (rack number, ON / close glyphs where the art
has none, the nine channel LEDs) and gives the click zones, all in strip pixels (y from the top).

Skin files (ui/assets/skins/packapunch/):
    rack_<name>_collapsed_bg.png       the strip, rack powered
    rack_<name>_collapsed_bg_off.png   the same strip with the lights off (faded in over the first when the rack
                                       is switched off, so only lights change - the metal never dims)

Channel LEDs: the standard collapsed-rack strip (9 numbered LEDs, routed channels flash) sits just left of the
ON key, using rack_collapsed_ch_led_on/off.png from this skin folder. Keep that area of the art free.
"""
import math
import time as _time

ART_W, ART_H = 1888.0, 71.0

# colours
_AMBER = (1.00, 0.72, 0.22, 1.0)

CFG = {
    # ---- DSP racks (the channel numbers live in the display window) ----------------------------------------------
    "BOOSTER": dict(
        ink=(0.30, 0.05, 0.05, 0.75),
        art="booster", collapse=(15, 12, 48, 56), badge=(53, 12, 88, 57), on=(1777, 12, 1845, 54), close=(1849, 12, 1886, 54),
        disp=(866, 13, 1100, 58), bcol=(0.94, 0.94, 0.96, 1.0), bglow=0.0, dcol=(1.00, 0.55, 0.30, 1.0)),
    "COMP_MULTI": dict(
        ink=(0.25, 0.55, 0.62, 0.55),
        art="comp_multi", collapse=(9, 13, 43, 48), badge=(50, 13, 87, 48), on=(1764, 12, 1830, 46), close=(1832, 10, 1863, 47), led_right=1684,
        disp=(829, 21, 1121, 57), bcol=(0.50, 0.95, 1.00, 1.0), bglow=0.8, dcol=(0.50, 0.95, 1.00, 1.0)),
    "COMP_SINGLE": dict(
        ink=(0.28, 0.24, 0.18, 0.75),
        art="comp_single", collapse=(12, 9, 48, 48), badge=(55, 10, 90, 45), on=(1773, 7, 1833, 52), close=(1839, 10, 1873, 41),
        disp=(999, 16, 1279, 52), bcol=(0.35, 0.10, 0.05, 1.0), bglow=0.0, dcol=(0.08, 0.55, 0.15, 1.0)),
    "DELAY": dict(
        ink=(0.25, 0.22, 0.18, 0.75),
        art="delay", collapse=(14, 11, 54, 51), badge=(65, 10, 108, 54), on=(1763, 2, 1843, 70), close=(1848, 9, 1872, 48),
        disp=(755, 8, 1001, 26), bcol=(0.86, 0.86, 0.82, 1.0), bglow=0.0, dcol=(0.85, 0.38, 0.02, 1.0)),
    "EQ": dict(
        ink=(0.62, 0.58, 0.48, 0.55),
        art="eq", collapse=(11, 12, 47, 60), badge=(54, 13, 90, 57), on=(1773, 12, 1836, 52), close=(1843, 12, 1878, 52), led_right=1664,
        disp=(856, 17, 1108, 49), bcol=(0.80, 0.88, 1.00, 1.0), bglow=0.0, dcol=(0.92, 0.84, 0.62, 1.0)),
    "NOISE_GATE": dict(
        ink=(0.55, 0.18, 0.15, 0.65),
        art="noisegate", collapse=(17, 17, 56, 55), badge=(67, 17, 107, 56), on=(1780, 17, 1842, 53), close=(1847, 17, 1877, 52),
        disp=(846, 19, 1086, 51), bcol=(1.00, 0.45, 0.42, 1.0), bglow=0.7, dcol=(1.00, 0.30, 0.24, 1.0)),
    "REVERB": dict(
        ink=(0.75, 0.72, 0.62, 0.50),
        art="reverb", collapse=(14, 9, 67, 62), badge=(81, 17, 124, 57), on=(1704, 4, 1812, 70), close=(1822, 14, 1869, 62),
        disp=(490, 23, 1222, 43), bcol=(1.00, 0.72, 0.22, 1.0), bglow=0.5, dcol=(1.00, 0.72, 0.22, 1.0)),
    "MIXDOWN": dict(
        ink=(0.80, 0.86, 0.92, 0.45),
        art="mixdown", collapse=(17, 20, 77, 54), badge=(117, 15, 178, 59), on=(1690, 13, 1764, 63), close=(1789, 13, 1867, 63),
        disp=(814, 11, 1030, 57), bcol=(1.00, 0.45, 0.42, 1.0), bglow=0.5, dcol=(1.00, 0.35, 0.30, 1.0),
        title=("MIXDOWN", (335, 20, 602, 58), "mixdown.ttf", (0.0, 0.0, 0.0, 1.0)),
        on_text=("ON", (1.0, 0.92, 0.90, 1.0), (0.14, 0.15, 0.18, 0.80), 0.8, 0.30),
        close_text=("X", (1.0, 0.92, 0.90, 1.0), (0.14, 0.15, 0.18, 0.80), 0.8, 0.32)),
    # ---- AI racks (a row of nine keys in the art) ----------------------------------------------------------------
    "WHISPER": dict(
        ink=(0.12, 0.14, 0.18, 0.70),
        art="whisper", collapse=(111, 11, 148, 58), badge=(152, 15, 180, 58), on=(1311, 16, 1356, 62), close=(1363, 16, 1408, 62),
        keys=((765, 801, 838, 878, 915, 952, 991, 1030, 1070), 43, 15, 17, (1.00, 0.28, 0.16)),
        bcol=(0.18, 0.02, 0.02, 1.0), bglow=0.0),
    "RESEMBLE": dict(
        ink=(0.25, 0.18, 0.08, 0.70),
        art="voicefixer", collapse=(103, 3, 139, 40), badge=(146, 6, 180, 38), on=(1705, 9, 1754, 60), close=(1769, 9, 1820, 60),
        keys=((852, 892, 931, 971, 1011, 1051, 1090, 1128, 1168), 34, 19, 19, (0.95, 0.42, 0.02)),
        bcol=(0.22, 0.09, 0.01, 1.0), bglow=0.0,
        on_text=("ON", (0.82, 1.00, 0.82, 1.0), (0.14, 0.20, 0.14, 0.7), 0.85, 0.24, (0.30, 1.00, 0.30)),
        close_text=("X", (0.98, 0.86, 0.80, 1.0), (0.98, 0.86, 0.80, 1.0), 0.0, 0.26)),
    "DEMUCS": dict(
        ink=(0.18, 0.20, 0.15, 0.70),
        art="demucs", collapse=(16, 25, 46, 62), badge=(52, 22, 98, 62), on=(1776, 12, 1843, 69), close=(1844, 12, 1883, 69),
        keys=((923, 968, 1008, 1044, 1081, 1118, 1155, 1192, 1230), 50, 17, 19, (0.15, 0.55, 0.08)),
        bcol=(0.90, 0.92, 0.80, 1.0), bglow=0.0),
    "PIPER_TTS": dict(
        ink=(0.55, 0.58, 0.62, 0.55),
        art="piper", collapse=(11, 19, 43, 48), badge=(53, 19, 86, 48), on=(1729, 19, 1792, 44), close=(1801, 19, 1827, 44),
        keys=((795, 829, 862, 895, 929, 961, 995, 1027, 1061), 32, 13, 13, (0.45, 0.72, 1.00)),
        bcol=(0.62, 0.64, 0.68, 1.0), bglow=0.0,
        on_text=("ON", (0.75, 0.90, 1.00, 1.0), (0.35, 0.38, 0.42, 1.0), 0.8, 0.46, (0.45, 0.72, 1.00)),
        close_text=("X", (0.85, 0.20, 0.15, 0.95), (0.85, 0.20, 0.15, 0.95), 0.0, 0.42)),
    "RVC": dict(
        ink=(0.15, 0.15, 0.18, 0.70),
        art="knnvc", collapse=(123, 18, 151, 52), badge=(160, 18, 190, 52), on=(1691, 15, 1749, 49), close=(1751, 15, 1779, 49),
        keys=((949, 981, 1012, 1044, 1075, 1107, 1138, 1168, 1198), 40, 13, 15, (0.90, 0.10, 0.10)),
        bcol=(0.18, 0.02, 0.02, 1.0), bglow=0.0),
}

LED_GAP_TO_ON = 10.0    # UI px between the last LED and the ON key (a rack may set led_right = art x of the strip's right edge instead)
LED_PLATE = 0          # alpha of a dark backing plate behind the LEDs (0 = none, like the standard skin)
_LEVEL = {}


def _key(rack):
    t = getattr(rack, "ai_type", None)
    if t and t in CFG:
        return t
    t = getattr(rack, "effect_type", None)
    return t if t in CFG else None


def _bg_keys(cfg):
    return "rack_%s_collapsed_bg" % cfg["art"], "rack_%s_collapsed_bg_off" % cfg["art"]


def pp_collapsed_active(rack):
    """True when the packApunch skin is on and this rack has its collapsed strip."""
    k = _key(rack)
    if k is None:
        return False
    try:
        from ui.mixer.texture_cache import get_active_skin, get_texture
        if get_active_skin() != "packapunch":
            return False
        return get_texture(_bg_keys(CFG[k])[0]) is not None
    except Exception:
        return False


def _level(key, enabled, now):
    try:
        from ui.racks.rack_base import _pp_preset_level
        return _pp_preset_level(key, enabled, now)
    except Exception:
        return 1.0 if enabled else 0.0


class _Geo:
    def __init__(self, rx, ry, rw, rh):
        self.rx, self.ry, self.rw, self.rh = rx, ry, rw, rh

    def X(self, ax):
        return self.rx + ax / ART_W * self.rw

    def Y(self, ay):
        return self.ry + (1.0 - ay / ART_H) * self.rh

    def rect(self, r):
        # (x0, y0, x1, y1) in art px (y from top) -> screen (x0, y0 bottom, x1, y1 top)
        return (self.X(r[0]), self.Y(r[3]), self.X(r[2]), self.Y(r[1]))

    def sy(self, v):
        return v * self.rh / ART_H


def pp_collapsed_rects(rack, rx, ry, rw, rh):
    """Screen rects (x0, y0, x1, y1) of the click zones: {'collapse', 'badge', 'on', 'close'} or None."""
    k = _key(rack)
    if k is None:
        return None
    cfg = CFG[k]
    g = _Geo(rx, ry, rw, rh)
    return {n: g.rect(cfg[n]) for n in ("collapse", "badge", "on", "close")}


def pp_collapsed_hit(rack, rx, ry, rw, rh, mx, my):
    """'collapse' / 'badge' / 'on' / 'close' / 'body' for a click inside a collapsed packApunch rack, else None."""
    rects = pp_collapsed_rects(rack, rx, ry, rw, rh)
    if not rects:
        return None
    for name in ("close", "on", "collapse", "badge"):
        r = rects[name]
        if r[0] <= mx <= r[2] and r[1] <= my <= r[3]:
            return name
    return "body"


def _txt(text, cx, cy, size, col, th, glow=0.0, glow_col=None):
    from ui.racks.pp_text import draw_text, text_width
    fid = th["fid"] if th else 0
    prof = th["prof"] if th else None
    size = max(1, int(round(size)))
    w = text_width(text, size, fid, prof)
    draw_text(text, cx - w / 2.0, cy - size / 2.0 + 1.0, size, col, glow=glow, glow_col=glow_col, fid=fid, prof=prof)


def _fit(text, width, size, th):
    """Shrink size until the text fits width."""
    from ui.racks.pp_text import text_width
    fid = th["fid"] if th else 0
    prof = th["prof"] if th else None
    s = size
    while s > 6 and text_width(text, s, fid, prof) > width:
        s -= 1
    return s


def pp_collapsed_draw(rx, ry, rw, rh, rack, idx, scale):
    """Draw a collapsed packApunch rack. Returns True when it drew (the caller then draws nothing else)."""
    if not pp_collapsed_active(rack):
        return False
    from ui.mixer.texture_cache import get_texture, blit_texture, blit_texture_tinted
    k = _key(rack)
    cfg = CFG[k]
    g = _Geo(rx, ry, rw, rh)
    enabled = bool(getattr(rack, "enabled", True))
    now = _time.monotonic()
    lv = _level(("collapsed", k, idx), enabled, now)

    bg_key, off_key = _bg_keys(cfg)
    blit_texture(get_texture(bg_key), rx, ry, rw, rh, key=bg_key)
    if lv < 0.997:
        off = get_texture(off_key)
        if off is not None:
            a = 1.0 - lv
            blit_texture_tinted(off, rx, ry, rw, rh, color=(a, a, a, a), blend="ALPHA_PREMULT", key=off_key)

    # the rack's own text look (fonts / glow) where the rack has one
    th = None
    try:
        from ui.racks.rack_base import _pp_theme
        th = _pp_theme(rack)
        if not th["fid"]:
            th = None
    except Exception:
        th = None
    glow_k = th["glow"] if th else 0.0

    # rack number
    br = g.rect(cfg["badge"])
    bw, bh = br[2] - br[0], br[3] - br[1]
    label = str(idx + 1)
    bs = _fit(label, bw * 0.86, max(9, bh * 0.66), th)
    bc = cfg["bcol"]
    ba = 0.40 + 0.60 * lv if cfg.get("bglow", 0) <= 0 else lv * 0.9 + 0.1
    _txt(label, (br[0] + br[2]) / 2, (br[1] + br[3]) / 2, bs, (bc[0], bc[1], bc[2], bc[3] * ba), th,
         glow=cfg.get("bglow", 0.0) * lv)

    # ON / close glyphs the art leaves blank
    for nm, tk in (("on", "on_text"), ("close", "close_text")):
        t = cfg.get(tk)
        if not t:
            continue
        txt, hot, dim, glow, frac = t[0], t[1], t[2], t[3], t[4]
        gcol = t[5] if len(t) > 5 else None
        r = g.rect(cfg[nm])
        col = tuple(dim[i] + (hot[i] - dim[i]) * lv for i in range(4))
        sz = _fit(txt, (r[2] - r[0]) * 0.8, max(8, (r[3] - r[1]) * frac * 1.5), None)
        _txt(txt, (r[0] + r[2]) / 2, (r[1] + r[3]) / 2, sz, col, None,
             glow=glow * lv, glow_col=gcol)

    # title drawn live where the art has a blank name plate (Mixdown)
    tt = cfg.get("title")
    if tt:
        try:
            from ui.racks.pp_text import font_id as _fid, font_profile as _fprof, draw_text as _dt, text_width as _tw
            tfid = _fid(tt[2], "MIXDOWN")
            tprof = _fprof(tt[2], "MIXDOWN") if tfid else None
            tr = g.rect(tt[1])
            tsz = _fit(tt[0], (tr[2] - tr[0]) * 0.88, max(10, (tr[3] - tr[1]) * 0.62), {"fid": tfid, "prof": tprof})
            tw_ = _tw(tt[0], tsz, tfid, tprof)
            for ox in (-0.5, 0.0, 0.5):
                _dt(tt[0], (tr[0] + tr[2]) / 2 - tw_ / 2 + ox, (tr[1] + tr[3]) / 2 - tsz / 2 + 1.0, tsz, tt[3], fid=tfid, prof=tprof)
        except Exception:
            pass

    # channel LEDs - same as the standard collapsed rack: 9 numbered LEDs, just left of the ON key.
    # Channels routed to the rack flash; with the rack switched off every LED sits dark.
    try:
        from ui.racks.rack_base import (RACK_COLLAPSED_LED_W as _LW, RACK_COLLAPSED_LED_H as _LH,
                                        RACK_COLLAPSED_LED_GAP as _LG, RACK_COLLAPSED_LED_NUM_GAP as _LNG,
                                        RACK_COLLAPSED_LED_FLASH_HZ as _LHZ, _draw_text, _text_width, _draw_circle, _draw_rect)
    except Exception:
        return True
    flash_on = enabled and ((_time.time() * _LHZ) % 1.0 < 0.5)
    fs = max(1, int(9 * scale))
    led_w, led_h = _LW * scale, _LH * scale
    gap, ngap = _LG * scale, _LNG * scale
    unit_h = fs + ngap + led_h
    cy = ry + rh / 2.0
    led_y = cy - unit_h / 2.0
    num_y = led_y + led_h + ngap
    strip_w = 9 * led_w + 8 * gap
    onr = g.rect(cfg["on"])
    right = g.X(cfg["led_right"]) if cfg.get("led_right") else onr[0] - LED_GAP_TO_ON * scale
    x0 = right - strip_w
    if LED_PLATE:
        pad = 5 * scale
        try:
            _draw_rect(x0 - pad, led_y - pad * 0.6, strip_w + 2 * pad, unit_h + pad * 1.2, (0.0, 0.0, 0.0, LED_PLATE))
        except Exception:
            pass
    group_off = getattr(rack, "group_idx", 0) * 9
    tex_on, tex_off = get_texture("rack_collapsed_ch_led_on"), get_texture("rack_collapsed_ch_led_off")
    for i in range(9):
        lx = x0 + i * (led_w + gap)
        used = bool(getattr(rack, "ch%d" % i, False))
        lit = used and flash_on
        tex = tex_on if lit else tex_off
        if tex is not None:
            blit_texture(tex, lx, led_y, led_w, led_h, key="rack_collapsed_ch_led_on" if lit else "rack_collapsed_ch_led_off")
        else:
            _draw_circle(lx + led_w / 2, led_y + led_h / 2, led_w / 2,
                         (0.0, 0.9, 0.4, 1.0) if lit else (0.15, 0.15, 0.15, 1.0))
        label = str(group_off + i + 1)
        tw = _text_width(label, fs)
        _draw_text(label, lx + led_w / 2 - tw / 2, num_y, fs,
                   (0.75, 0.75, 0.75, 1.0) if used else (0.35, 0.35, 0.35, 1.0))
    return True
