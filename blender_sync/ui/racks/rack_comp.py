# =============================================================================
# rack_comp.py
# Single-band and multiband compressor rack UI
# ┌─ LAYOUT CONSTANTS ─────────────────────────────────────────────────────┐
# │ RACK_EXPANDED_H, RACK_EXPANDED_H_MB — change height here
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


# =============================================================================
# MULTIBAND COMPRESSOR KNOB TUNING
# All offsets are unscaled px — multiplied by UI_SCALE at draw time.
# MB_KNOB_SCALE multiplies the computed knob_r (radius).
# Per-knob x/y offsets nudge each knob from its computed grid position.
# Set MB_KNOB_SHOW_LABELS = False to suppress all knob labels and value text
# (use when they are baked into the background PNG).
# Knob order: Thr, Ratio, Knee (top row), Atk, Rel, Gain (bottom row)
# =============================================================================

MB_KNOB_SCALE       = 0.7    # multiplier on computed knob radius

MB_KNOB_SHOW_LABELS = False   # set False to suppress all knob label/value text
MB_BAND_SHOW_LABELS  = False   # set False to suppress Low/L-Mid/H-Mid/High text (baked into PNG)
MB_SHOW_FREQ_AXIS    = False   # set False to suppress 50/100/500/1k/2k/5k/10k axis labels
MB_SHOW_GAIN_TEXT   = False   # set False to suppress GAIN label + value on faders (baked into PNG)

# Fader handle PNG tuning — unscaled px, multiplied by scale at draw time
MB_FADER_HANDLE_SCALE    = 2.0   # size multiplier (1.0 = fills fdr_w x handle_h exactly)
MB_FADER_HANDLE_X_OFFSET = 0.0   # nudge left(−) / right(+)
MB_FADER_HANDLE_Y_OFFSET = 0.0   # nudge down(−) / up(+)

# Per-knob offsets [band_0..band_3] — each entry is (x_offset, y_offset) in unscaled px
# Thr, Ratio, Knee = top row;  Atk, Rel, Gain = bottom row
MB_KNOB_THR_OFFSETS  = [(0.0, 0.25), (-1.0, 2.0), (-0.5, 0.0), (-1.0, 0.25)]
MB_KNOB_RAT_OFFSETS  = [(0.0, 0.25), (-0.5, 1.0), (-0.5, 0.0), (-2.25, -0.25)]
MB_KNOB_KNE_OFFSETS  = [(0.0, 0.25), (-1.0, 1.0), (-0.5, 0.25), (-2.0, 0.25)]
MB_KNOB_ATK_OFFSETS  = [(0.0, 1.25), (0.0, 1.25), (-1.0, 0.0), (-1.0, 1.0)]
MB_KNOB_REL_OFFSETS  = [(0.0, 1.25), (-0.25, 2.5), (-1.0, 0.0), (-2.5, 0.0)]
MB_KNOB_GAN_OFFSETS  = [(0.0, 1.25), (-0.25, 1.5), (-1.0, 0.0), (-2.25, 1.75)]

# ---------------------------------------------------------------------------
# Module-level cache for multiband spectrum display.
# Weight arrays and hz-to-bin mapping are constant — computed once at import.
# Only vertex positions depend on live spec_bins and are rebuilt per frame.
# ---------------------------------------------------------------------------
import math as _math_mb

_MB_N_SPEC    = 128
_MB_HZ_BINS   = [20.0 * (1000.0 ** (i / (_MB_N_SPEC - 1))) for i in range(_MB_N_SPEC)]
_MB_LOG_HZ    = [_math_mb.log10(hz) for hz in _MB_HZ_BINS]
_MB_LOG_MIN   = _math_mb.log10(20.0)
_MB_LOG_RNG   = _math_mb.log10(20000.0) - _MB_LOG_MIN
_MB_CROSS_HZ  = [120.0, 800.0, 5000.0]
_MB_CROSS_LOG = [_math_mb.log10(f) for f in _MB_CROSS_HZ]

def _mb_band_weight_precomp(f_lo, f_hi, fade_oct=0.15):
    log_lo = _math_mb.log10(f_lo);  log_hi = _math_mb.log10(f_hi)
    out = []
    for lhz in _MB_LOG_HZ:
        if lhz < log_lo - fade_oct or lhz > log_hi + fade_oct:
            out.append(0.0); continue
        w_lo = 1.0
        if lhz < log_lo + fade_oct:
            t = (lhz - (log_lo - fade_oct)) / (2.0 * fade_oct)
            w_lo = 0.5 - 0.5 * _math_mb.cos(_math_mb.pi * t)
        w_hi = 1.0
        if lhz > log_hi - fade_oct:
            t = ((log_hi + fade_oct) - lhz) / (2.0 * fade_oct)
            w_hi = 0.5 - 0.5 * _math_mb.cos(_math_mb.pi * t)
        out.append(w_lo * w_hi)
    return out

def _mb_cross_weight_precomp(f_cross, half_oct=0.25):
    log_c = _math_mb.log10(f_cross)
    out = []
    for lhz in _MB_LOG_HZ:
        dist = abs(lhz - log_c)
        out.append(0.0 if dist >= half_oct else 0.5 + 0.5 * _math_mb.cos(_math_mb.pi * dist / half_oct))
    return out

# All 7 weight arrays computed ONCE at module load — never rebuilt
_MB_WEIGHTS = [
    _mb_band_weight_precomp(20.0,    120.0),
    _mb_cross_weight_precomp(120.0),
    _mb_band_weight_precomp(120.0,   800.0),
    _mb_cross_weight_precomp(800.0),
    _mb_band_weight_precomp(800.0,   5000.0),
    _mb_cross_weight_precomp(5000.0),
    _mb_band_weight_precomp(5000.0, 20000.0),
]
_MB_GR_BAND  = [0, None, 1, None, 2, None, 3]
_MB_SHADER   = None
def _get_mb_shader():
    global _MB_SHADER
    if _MB_SHADER is None:
        _MB_SHADER = _get_shader()
    return _MB_SHADER


def _rp(rack, idx, default=0.0):
    """Safely get rack param by index. Mirrors Racks._rp to avoid import."""
    try:
        return getattr(rack, f'p{idx}', default)
    except Exception:
        return default


RACK_RAIL_H = 32  # duplicated from Racks.py to avoid circular import

# Band display constants — duplicated from rack_base.py (can't import at module level)
BAND_COLORS = [
    (0.2, 0.5, 1.0, 0.85),   # Low      — blue
    (0.2, 0.9, 0.4, 0.85),   # Low-Mid  — green
    (1.0, 0.7, 0.1, 0.85),   # High-Mid — amber
    (1.0, 0.3, 0.3, 0.85),   # High     — red
]
BAND_NAMES = ["Low", "L-Mid", "H-Mid", "High"]
BAND_FREQS = ["<120Hz", "120-800Hz", "800Hz-5kHz", ">5kHz"]

# ---------------------------------------------------------------------------
# PackAPunch skin — one neon blue replaces the per-band red/green/blue/amber
# everywhere a band colour is used (knobs, faders, the response curve and
# its dots/labels). Colour sampled directly from Luke's new background art
# (the knob-ring glow + circuit-board trace colour) rather than guessed.
# The screen's own dark background rect is also skipped for this skin so
# that art shows through instead of being painted over — see
# _draw_multiband_body's _is_packapunch_mb check.
# ---------------------------------------------------------------------------
MB_PACKAPUNCH_SKIN  = "packapunch"
MB_PACKAPUNCH_COLOR = (0.15, 0.85, 1.0, 0.85)   # neon blue, same alpha as BAND_COLORS


def _mb_lighten(rgb, t):
    """Mix an (r,g,b) tuple toward white by fraction t (0=unchanged, 1=white).
    Used to turn the single packapunch neon blue into a set of lighter
    tints — same hue throughout, so the 4 bands (and the 3 crossover
    blend layers between them) stay readable as distinct bands in the
    audio-content spectrum display without reintroducing other hues."""
    return tuple(c + (1.0 - c) * t for c in rgb)


# One shade per band (0=base neon blue .. 3=lightest), plus the 3 in-between
# shades used for the crossover blend layers in the 7-layer spectrum below.
MB_PACKAPUNCH_BAND_SHADES  = [_mb_lighten(MB_PACKAPUNCH_COLOR[:3], t)
                               for t in (0.0, 0.18, 0.36, 0.54)]
MB_PACKAPUNCH_CROSS_SHADES = [_mb_lighten(MB_PACKAPUNCH_COLOR[:3], t)
                               for t in (0.09, 0.27, 0.45)]


#  PackAPunch meter colours — everything neon blue instead of the usual
#  green signal / red GR-cap, per Luke's request. Peak/GR stays a lighter
#  shade of the same blue rather than an identical flat colour to the
#  signal portion, so the meter still reads as two things (how loud vs.
#  how much is being pulled down) — same lighter-shade-per-element
#  convention as the spectrum bands above, just within one hue now.
MB_PACKAPUNCH_METER_SIGNAL    = (0.05, 0.45, 0.55, 0.85)
MB_PACKAPUNCH_METER_SIGNAL_HI = (0.15, 0.85, 1.00, 0.95)
MB_PACKAPUNCH_METER_PEAK      = (0.45, 0.80, 1.00, 0.90)
MB_PACKAPUNCH_METER_PEAK_HI   = (0.65, 0.92, 1.00, 1.00)


def _draw_gr_meter_band(bx, by, bw, bh, gr_db, scale, signal_norm=0.0, packapunch=False):
    """Premier Pro style: signal bar rising from bottom + GR cap on top.
    Default skin: green signal, red GR cap. packapunch=True: both neon
    blue — see the MB_PACKAPUNCH_METER_* constants above."""
    sig_col, sig_hi, gr_col, gr_hi = (
        (MB_PACKAPUNCH_METER_SIGNAL, MB_PACKAPUNCH_METER_SIGNAL_HI,
         MB_PACKAPUNCH_METER_PEAK, MB_PACKAPUNCH_METER_PEAK_HI) if packapunch else
        ((0.05, 0.55, 0.25, 0.85), (0.1, 0.9, 0.4, 0.95),
         (0.85, 0.15, 0.15, 0.9), (1.0, 0.35, 0.35, 1.0))
    )
    _draw_rect(bx, by, bw, bh, (0.04, 0.04, 0.04, 1.0))
    sig_h = max(0.0, min(1.0, signal_norm)) * bh
    if sig_h > 0.5:
        gr_h    = max(0.0, min(1.0, gr_db / 24.0)) * sig_h  # 24dB full scale
        green_h = sig_h - gr_h
        if green_h > 0.5:
            _draw_rect(bx, by, bw, green_h, sig_col)
            if green_h > 3*scale:
                _draw_rect(bx, by + green_h - 2*scale, bw, 2*scale, sig_hi)
        if gr_h > 0.5:
            _draw_rect(bx, by + green_h, bw, gr_h, gr_col)
            if gr_h > 2*scale:
                _draw_rect(bx, by + green_h + gr_h - 2*scale, bw, 2*scale, gr_hi)
    for t in [0.25, 0.5, 0.75]:
        _draw_rect(bx, by + t*bh, bw, max(0.5, scale*0.5), (0.2, 0.2, 0.2, 1.0))



# =============================================================================
# PACKAPUNCH MULTIBAND LIFE — standalone ambient animation (runs whether or
# not the timeline is playing, no audio needed):
#   1. BREATHING GRID — a lines-only copy of the screen grid (rack_comp_multi_
#      lines.png, same canvas as the background) is drawn ADDITIVELY over the
#      background and its brightness slowly rises and falls like breathing.
#   2. ELECTRIC BALL — a ball of light with a short fading trail runs along
#      the circuit-board lines on the rack chassis (MB_PULSE_PATHS, traced
#      once from the art), then pops up on a different line after a short rest.
# Everything here is tuned by editing these constants and reloading the addon.
# =============================================================================
# Screen interior in the 1758x589 background art (px, top-left origin) — the
# region the lines layer actually covers.
MB_ART_W, MB_ART_H = 1758, 589
MB_SCREEN_PX       = (46, 57, 1553, 276)       # x0, y0, x1, y1

# 1. Breathing grid
MB_GLOW_ENABLED = True
MB_GLOW_PERIOD  = 4.5     # seconds per full breath (in + out)
MB_GLOW_MIN     = 0.0     # glow brightness at the bottom of the breath (0 = off)
MB_GLOW_MAX     = 0.60    # glow brightness at the top of the breath (1.0 = very hot)
MB_GLOW_SPREAD  = 0.10    # phase lag left->right as a fraction of a breath; 0 = all lines breathe together
MB_GLOW_STRIPS  = 24      # vertical strips used to apply the lag (only matters if SPREAD > 0)
MB_GLOW_TINT    = (0.55, 0.95, 1.0)   # slight cyan push on the added light

# 2. Electric ball
MB_PULSE_ENABLED    = True
MB_PULSE_COUNT      = 1        # balls running at once per rack
MB_PULSE_SPEED      = 320.0    # art px per second along the line (1758px = rack width)
MB_PULSE_GAP_MIN    = 0.35     # rest between runs (seconds)
MB_PULSE_GAP_MAX    = 1.4
MB_PULSE_HEAD_SIZE  = 34.0     # unscaled px, diameter of the head glow
MB_PULSE_TRAIL_LEN  = 150.0    # art px of fading trail behind the head
MB_PULSE_TRAIL_STEP = 6.0      # art px between trail glows (smaller = smoother streak)
MB_PULSE_GAIN       = 2.0      # head brightness
MB_PULSE_TINT       = (0.70, 0.96, 1.0)
MB_PULSE_STRAIGHT   = 0.7      # chance of going straight on at a junction (else random branch)
MB_PULSE_ROUTE_MAX  = 2600.0   # safety cap on how far one run may wander (art px)
MB_PULSE_ROUTE_MIN  = 120.0    # ignore runs shorter than this

# Circuit network traced from the art. NODES are the junctions / line ends,
# EDGES are (node_a, node_b, [(x, y), ...]) polylines in art px (top-left
# origin). The ball does not follow fixed lines: each run it starts at a line
# end and walks the network, going straight on at junctions where it can, until
# it reaches another line end - so it always travels a line all the way.
MB_PULSE_NODES = [(1583,303), (1645,303), (1611,305), (1631,304), (1067,313), (1174,313), (320,314), (425,314), (693,314), (799,314), (1440,314), (1586,315), (1600,315), (1049,317), (1514,318), (1366,318), (1320,318), (1422,318), (1530,318), (1613,325), (1424,328), (1601,329), (1556,334), (1587,330), (1616,334), (1632,333), (1067,333), (215,335), (319,335), (589,335), (692,335), (963,335), (1440,335), (1337,336), (1587,346), (1600,346), (1067,411), (215,412), (589,412), (964,412), (1440,412), (1337,413), (319,414), (692,414), (1142,417), (393,418), (766,418), (309,424), (653,424), (682,424), (636,425), (646,425), (1533,428), (1422,431), (216,431), (1337,431), (1358,430), (1460,431), (1518,431), (589,432), (636,434), (650,434), (237,434), (303,434), (610,434), (674,434), (119,447), (1557,440), (1254,442), (1243,450), (104,453), (212,559), (708,559), (950,559), (1336,559), (1446,559), (572,560), (1085,560), (320,561), (987,564), (1569,564), (1586,564), (1048,563), (1,577), (1600,586), (435,587), (808,587), (1189,587)]
MB_PULSE_EDGES = [
    (0, 11, [(1583,303), (1587,307), (1586,315)]),
    (2, 12, [(1611,305), (1605,303), (1601,307), (1600,315)]),
    (1, 84, [(1645,303), (1648,306), (1647,415), (1601,463), (1600,586)]),
    (2, 19, [(1611,305), (1617,308), (1617,324), (1613,325)]),
    (3, 25, [(1631,304), (1633,306), (1632,333)]),
    (4, 13, [(1067,313), (1052,314), (1049,317)]),
    (5, 26, [(1174,313), (1170,314), (1165,320), (1082,322), (1074,331), (1067,333)]),
    (6, 66, [(320,314), (99,313), (96,316), (96,404), (114,424), (114,443), (119,447)]),
    (7, 28, [(425,314), (421,314), (414,321), (338,321), (333,323), (323,334), (319,335)]),
    (8, 85, [(693,314), (479,313), (476,316), (476,404), (494,424), (494,437), (476,457), (476,549), (441,585), (435,587)]),
    (9, 30, [(799,314), (795,314), (788,321), (710,321), (692,335)]),
    (10, 17, [(1440,314), (1426,314), (1422,318)]),
    (11, 12, [(1586,315), (1600,315)]),
    (11, 23, [(1586,315), (1587,330)]),
    (12, 21, [(1600,315), (1601,329)]),
    (13, 86, [(1049,317), (1044,313), (855,313), (851,316), (851,404), (869,424), (869,437), (851,457), (851,549), (819,581), (808,587)]),
    (13, 31, [(1049,317), (1045,322), (981,321), (966,335), (963,335)]),
    (13, 26, [(1049,317), (1060,330), (1067,333)]),
    (14, 18, [(1514,318), (1515,314), (1526,314), (1530,318)]),
    (14, 18, [(1514,318), (1530,318), (1526,322), (1515,322), (1514,318), (1530,318)]),
    (14, 32, [(1514,318), (1460,318), (1447,332), (1440,335)]),
    (15, 16, [(1366,318), (1363,314), (1324,314), (1320,318)]),
    (15, 33, [(1366,318), (1362,322), (1353,322), (1342,334), (1337,336)]),
    (15, 17, [(1366,318), (1422,318)]),
    (16, 68, [(1320,318), (1262,318), (1258,314), (1236,314), (1234,316), (1234,403), (1251,421), (1252,441), (1254,442)]),
    (16, 33, [(1320,318), (1331,333), (1337,336)]),
    (17, 20, [(1422,318), (1426,322), (1427,327), (1424,328)]),
    (18, 22, [(1530,318), (1542,320), (1556,334)]),
    (21, 19, [(1601,329), (1604,325), (1613,325)]),
    (21, 23, [(1601,329), (1587,326), (1602,326), (1587,330)]),
    (19, 24, [(1613,325), (1616,325), (1616,334)]),
    (20, 32, [(1424,328), (1427,327), (1440,335)]),
    (21, 24, [(1601,329), (1600,332), (1605,335), (1616,334)]),
    (21, 23, [(1601,329), (1587,333), (1600,332), (1587,330)]),
    (21, 35, [(1601,329), (1600,346)]),
    (22, 52, [(1556,334), (1561,346), (1560,366), (1565,373), (1565,392), (1537,421), (1533,428)]),
    (23, 34, [(1587,330), (1587,346)]),
    (24, 25, [(1616,334), (1632,333)]),
    (24, 80, [(1616,334), (1616,395), (1568,445), (1567,546), (1569,564)]),
    (25, 81, [(1632,333), (1631,409), (1585,457), (1586,564)]),
    (26, 36, [(1067,333), (1067,411)]),
    (27, 66, [(215,335), (198,321), (114,321), (109,324), (106,328), (106,399), (124,418), (124,437), (119,447)]),
    (27, 28, [(215,335), (218,335), (234,321), (302,321), (319,335)]),
    (27, 37, [(215,335), (215,412)]),
    (28, 42, [(319,335), (319,414)]),
    (29, 76, [(589,335), (571,321), (494,321), (486,327), (485,332), (486,399), (504,418), (504,442), (489,458), (489,550), (498,559), (572,560)]),
    (29, 30, [(589,335), (607,321), (675,321), (692,335)]),
    (29, 38, [(589,335), (589,412)]),
    (30, 43, [(692,335), (692,414)]),
    (31, 73, [(963,335), (946,321), (869,321), (861,328), (861,398), (880,419), (880,442), (864,459), (864,549), (873,559), (950,559)]),
    (31, 39, [(963,335), (964,412)]),
    (32, 40, [(1440,335), (1440,412)]),
    (33, 41, [(1337,336), (1337,413)]),
    (34, 35, [(1587,346), (1587,344), (1600,344), (1600,346)]),
    (34, 35, [(1587,346), (1587,349), (1600,349), (1600,346)]),
    (34, 52, [(1587,346), (1585,392), (1548,430), (1537,431), (1533,428)]),
    (35, 67, [(1600,346), (1599,398), (1557,440)]),
    (36, 39, [(1067,411), (1052,424), (979,424), (964,412)]),
    (36, 44, [(1067,411), (1069,411), (1082,424), (1135,424), (1142,417)]),
    (37, 62, [(215,412), (202,425), (203,440), (209,446), (224,446), (237,434)]),
    (37, 47, [(215,412), (219,413), (231,424), (309,424)]),
    (38, 64, [(589,412), (576,424), (576,440), (583,446), (597,446), (610,434)]),
    (38, 50, [(589,412), (604,424), (636,425)]),
    (39, 77, [(964,412), (961,413), (950,426), (950,439), (957,446), (972,446), (980,438), (1146,437), (1169,459), (1170,536), (1148,559), (1085,560)]),
    (40, 53, [(1440,412), (1429,421), (1422,431)]),
    (40, 57, [(1440,412), (1447,415), (1460,431)]),
    (41, 56, [(1337,413), (1335,413), (1323,426), (1323,438), (1329,445), (1344,446), (1360,431), (1358,430)]),
    (41, 56, [(1337,413), (1340,413), (1350,421), (1358,430)]),
    (42, 47, [(319,414), (309,424)]),
    (42, 45, [(319,414), (326,416), (334,424), (387,424), (393,418)]),
    (43, 49, [(692,414), (682,424)]),
    (43, 46, [(692,414), (699,416), (707,424), (760,424), (766,418)]),
    (47, 63, [(309,424), (308,429), (303,434)]),
    (48, 51, [(653,424), (646,425)]),
    (48, 49, [(653,424), (682,424)]),
    (48, 61, [(653,424), (653,434), (650,434)]),
    (49, 65, [(682,424), (680,430), (674,434)]),
    (50, 51, [(636,425), (646,425)]),
    (50, 60, [(636,425), (636,434)]),
    (51, 61, [(646,425), (646,434), (650,434)]),
    (52, 58, [(1533,428), (1521,428), (1518,431)]),
    (56, 55, [(1358,430), (1337,431)]),
    (53, 56, [(1422,431), (1358,430)]),
    (53, 57, [(1422,431), (1460,431), (1457,434), (1424,434), (1422,431), (1460,431)]),
    (54, 62, [(216,431), (234,431), (237,434)]),
    (57, 58, [(1460,431), (1518,431)]),
    (58, 75, [(1518,431), (1524,441), (1542,460), (1542,537), (1520,559), (1446,559)]),
    (59, 64, [(589,432), (607,431), (610,434)]),
    (60, 64, [(636,434), (610,434)]),
    (60, 61, [(636,434), (650,434)]),
    (61, 65, [(650,434), (674,434)]),
    (62, 63, [(237,434), (303,434)]),
    (63, 78, [(303,434), (305,437), (399,438), (420,460), (420,537), (398,559), (323,559), (320,561)]),
    (65, 72, [(674,434), (676,437), (773,438), (794,460), (794,537), (772,559), (708,559)]),
    (68, 69, [(1254,442), (1252,441), (1243,450)]),
    (66, 70, [(119,447), (107,449), (104,453)]),
    (66, 71, [(119,447), (115,456), (116,551), (125,559), (212,559)]),
    (69, 87, [(1243,450), (1239,449), (1233,455), (1233,547), (1193,586), (1189,587)]),
    (69, 74, [(1243,450), (1245,451), (1245,550), (1254,559), (1336,559)]),
    (70, 83, [(104,453), (101,451), (96,457), (96,538), (65,570), (3,570), (1,572), (1,577)]),
    (70, 83, [(104,453), (107,456), (107,542), (68,581), (2,582), (1,577)]),
]

import time as _time_mb
import random as _random_mb
import bisect as _bisect_mb

_mb_rng      = _random_mb.Random()
_mb_runs     = {}       # rack_idx -> [[pts, cum, start_time, speed], ...]
_mb_net      = None     # (nodes, edges, adjacency, endpoints), built on first use


def _mb_screen_uv():
    x0, y0, x1, y1 = MB_SCREEN_PX
    # v=0 is the BOTTOM of the image, so image-row y maps to 1 - y/H
    return x0 / MB_ART_W, 1.0 - y1 / MB_ART_H, x1 / MB_ART_W, 1.0 - y0 / MB_ART_H


# --- Power state (rack switched on / off) -----------------------------------
# OFF: the breathing glow and the ball stop, and the whole rack is dimmed.
# ON again: the rack "powers up" - the light stutters through MB_POWER_FLICKER
# (time in seconds since switch-on, brightness 0..1, linear in between) and then
# settles at full brightness. Also scales the glow and ball while it plays.
MB_POWER_ENABLED  = True
MB_POWER_OFF_DIM  = 0.62    # how dark the rack goes when OFF (0 = no dimming, 1 = black)
MB_POWER_DOWN_SEC = 0.55    # fade-out time when switched off
MB_POWER_FLOOR    = 0.45    # lowest brightness the power-up flicker dips to (0 = as dark as OFF, 1 = no flicker)
MB_POWER_FLICKER  = [
    (0.00, 0.00), (0.10, 0.00), (0.12, 0.80), (0.21, 0.80), (0.23, 0.08),
    (0.36, 0.08), (0.38, 0.95), (0.46, 0.95), (0.48, 0.20), (0.58, 0.20),
    (0.60, 1.00), (0.74, 1.00), (0.76, 0.45), (0.84, 0.45), (0.86, 1.00),
]
_mb_power = {}      # rack_idx -> {"en", "t0", "p0", "p"}


def _mb_flicker(dt):
    k = MB_POWER_FLICKER
    if dt <= k[0][0]:
        return k[0][1]
    for (ta, pa), (tb, pb) in zip(k, k[1:]):
        if dt <= tb:
            f = 0.0 if tb <= ta else (dt - ta) / (tb - ta)
            return pa + (pb - pa) * f
    return 1.0


def _mb_power_level(rack_idx, enabled, now):
    """Current brightness 0..1 of this rack (1 = fully powered)."""
    if not MB_POWER_ENABLED:
        return 1.0
    st = _mb_power.get(rack_idx)
    if st is None:                                   # first sight: no animation
        st = {"en": bool(enabled), "t0": now - 999.0,
              "p0": 1.0 if enabled else 0.0, "p": 1.0 if enabled else 0.0}
        _mb_power[rack_idx] = st
    if bool(enabled) != st["en"]:
        st["en"], st["t0"], st["p0"] = bool(enabled), now, st["p"]
    dt = now - st["t0"]
    if st["en"]:
        fl = MB_POWER_FLOOR + (1.0 - MB_POWER_FLOOR) * _mb_flicker(dt)   # dips to the floor, never to the OFF look
        p = max(fl, st["p0"] * max(0.0, 1.0 - dt / 0.15))
    else:
        p = st["p0"] * max(0.0, 1.0 - dt / max(0.01, MB_POWER_DOWN_SEC))
    st["p"] = p
    return p


def mb_draw_power_dim(rx, ry, rw, rh, rack_idx):
    """Dark overlay over the whole rack, strength = how powered-down it is.
    Called by rack_base after everything else is drawn so nothing escapes it."""
    try:
        st = _mb_power.get(rack_idx)
        if not MB_POWER_ENABLED or st is None:
            return
        a = (1.0 - st["p"]) * MB_POWER_OFF_DIM
        if a <= 0.004:
            return
        # draw_rect never sets a blend mode itself, so without this the overlay
        # was drawn with whatever state the last draw left behind - opaque
        # (a solid black rack) whenever nothing before it had set ALPHA.
        gpu.state.blend_set("ALPHA")
        _draw_rect(rx, ry, rw, rh, (0.0, 0.01, 0.015, a))
    except Exception as e:
        print(f"[MB] power dim error: {e}")


_mb_glow_warned = False

def _draw_mb_grid_pulse(rx, ry, rw, rh, power=1.0):
    """Breathing additive glow on the packapunch screen grid."""
    global _mb_glow_warned
    if power <= 0.01:
        return
    try:
        from ui.mixer.texture_cache import get_texture, blit_texture_tinted
        tex = get_texture("rack_comp_multi_glow")
        if not tex:
            if not _mb_glow_warned:
                _mb_glow_warned = True
                print("[MB] breathing glow: texture 'rack_comp_multi_glow' is not loaded "
                      "(check packapunch/rack_comp_multi_glow.png exists)")
            return
        if not _mb_glow_warned:
            _mb_glow_warned = True
            print("[MB] breathing glow: drawing (texture loaded)")
        u0, v0, u1, v1 = _mb_screen_uv()
        t   = _time_mb.perf_counter()
        n   = max(1, int(MB_GLOW_STRIPS)) if MB_GLOW_SPREAD > 0 else 1
        du  = (u1 - u0) / n
        y   = ry + v0 * rh
        h   = (v1 - v0) * rh
        tau = 2.0 * math.pi
        for i in range(n):
            f      = (i + 0.5) / n
            breath = 0.5 - 0.5 * math.cos(tau * (t / MB_GLOW_PERIOD - MB_GLOW_SPREAD * f))
            k      = max(0.0, MB_GLOW_MIN + (MB_GLOW_MAX - MB_GLOW_MIN) * breath) * power
            su0    = u0 + i * du
            blit_texture_tinted(tex, rx + su0 * rw, y, du * rw + 0.5, h,
                                color=(k * MB_GLOW_TINT[0], k * MB_GLOW_TINT[1],
                                       k * MB_GLOW_TINT[2], 1.0),
                                uv=(su0, v0, su0 + du, v1),
                                key="rack_comp_multi_glow")
    except Exception as e:
        print(f"[MB] grid breathe error: {e}")


def _mb_network():
    global _mb_net
    if _mb_net is None:
        adj = {}
        for ei, (u, v, pts) in enumerate(MB_PULSE_EDGES):
            adj.setdefault(u, []).append((ei, 1))      # walk the polyline forwards
            adj.setdefault(v, []).append((ei, -1))     # ... or backwards
        ends = [n for n, lst in adj.items() if len(lst) == 1]
        _mb_net = (MB_PULSE_NODES, MB_PULSE_EDGES, adj, ends or list(adj))
    return _mb_net


def _mb_heading(pts, i0, i1):
    dx, dy = pts[i1][0] - pts[i0][0], pts[i1][1] - pts[i0][1]
    d = math.hypot(dx, dy) or 1.0
    return dx / d, dy / d


def _mb_build_route():
    """Random walk over the circuit network: start at a line end, keep going
    (straight on where possible) until a dead end. Returns (pts, cum)."""
    nodes, edges, adj, ends = _mb_network()
    node = _mb_rng.choice(ends)
    route = [nodes[node]]
    heading = None
    used = set()
    total = 0.0
    while total < MB_PULSE_ROUTE_MAX:
        cands = [(ei, d) for ei, d in adj.get(node, ()) if ei not in used]
        if not cands:
            break                                      # reached the end of the line
        scored = []
        for ei, d in cands:
            u, v, pts = edges[ei]
            p = pts if d > 0 else pts[::-1]
            h = _mb_heading(p, 0, min(len(p) - 1, 1))
            align = 1.0 if heading is None else heading[0] * h[0] + heading[1] * h[1]
            scored.append((align, ei, d, p))
        scored.sort(key=lambda t: -t[0])
        pick = scored[0] if (len(scored) == 1 or _mb_rng.random() < MB_PULSE_STRAIGHT) \
            else _mb_rng.choice(scored)
        _, ei, d, p = pick
        used.add(ei)
        for q in p[1:]:
            total += math.hypot(q[0] - route[-1][0], q[1] - route[-1][1])
            route.append(q)
        heading = _mb_heading(route, max(0, len(route) - 2), len(route) - 1)
        u, v, _pts = edges[ei]
        node = v if d > 0 else u
    cum = [0.0]
    for (xa, ya), (xb, yb) in zip(route, route[1:]):
        cum.append(cum[-1] + math.hypot(xb - xa, yb - ya))
    return route, cum


def _mb_point_on_path(geo, s):
    """(x, y) in art px at arclength s along a route (clamped)."""
    pts, cum = geo
    s = max(0.0, min(cum[-1], s))
    i = min(len(pts) - 2, max(0, _bisect_mb.bisect_right(cum, s) - 1))
    seg = cum[i + 1] - cum[i]
    f = 0.0 if seg <= 0 else (s - cum[i]) / seg
    (xa, ya), (xb, yb) = pts[i], pts[i + 1]
    return xa + (xb - xa) * f, ya + (yb - ya) * f


def _mb_new_run(now, first=False):
    gap = _mb_rng.uniform(0.0, MB_PULSE_GAP_MAX) if first else \
          _mb_rng.uniform(MB_PULSE_GAP_MIN, MB_PULSE_GAP_MAX)
    for _ in range(6):                                 # skip degenerate (tiny) routes
        pts, cum = _mb_build_route()
        if cum[-1] >= MB_PULSE_ROUTE_MIN:
            break
    return [pts, cum, now + gap, MB_PULSE_SPEED * _mb_rng.uniform(0.85, 1.15)]


def _draw_mb_pulse(rx, ry, rw, rh, scale, rack_idx, power=1.0):
    """A ball of light + fading trail running along the circuit network."""
    if power <= 0.02:
        _mb_runs.pop(rack_idx, None)       # rack is off: forget the run, start fresh on power-up
        return
    try:
        from ui.mixer.texture_cache import get_texture, blit_texture_tinted
        tex = get_texture("rack_pulse_glow")
        if not tex or not MB_PULSE_EDGES:
            return
        now  = _time_mb.perf_counter()
        runs = _mb_runs.get(rack_idx)
        if runs is None or len(runs) != MB_PULSE_COUNT:
            runs = []
            for _ in range(MB_PULSE_COUNT):
                runs.append(_mb_new_run(now, first=True))
            _mb_runs[rack_idx] = runs
        for ri, run in enumerate(runs):
            pts, cum, t0, speed = run
            if now < t0:
                continue
            length  = cum[-1]
            d_head  = (now - t0) * speed                  # distance travelled so far
            if d_head > length + MB_PULSE_TRAIL_LEN:      # trail has fully left the line
                runs[ri] = _mb_new_run(now)
                continue
            n_tr = int(MB_PULSE_TRAIL_LEN / MB_PULSE_TRAIL_STEP)
            for k in range(n_tr, -1, -1):                 # tail first, head drawn last (on top)
                d = d_head - k * MB_PULSE_TRAIL_STEP
                if d < 0.0 or d > length:
                    continue
                ax, ay = _mb_point_on_path((pts, cum), d)
                f   = 1.0 - k * MB_PULSE_TRAIL_STEP / MB_PULSE_TRAIL_LEN
                env = f ** 1.8
                size = MB_PULSE_HEAD_SIZE * scale * (0.30 + 0.70 * f)
                if k == 0:
                    size *= 1.35
                    env = 1.0
                b = env * MB_PULSE_GAIN * (0.30 if k else 1.0) * power
                cx = rx + ax / MB_ART_W * rw
                cy = ry + (1.0 - ay / MB_ART_H) * rh
                blit_texture_tinted(tex, cx - size * 0.5, cy - size * 0.5, size, size,
                                    color=(b * MB_PULSE_TINT[0], b * MB_PULSE_TINT[1],
                                           b * MB_PULSE_TINT[2], 1.0),
                                    key="rack_pulse_glow")
    except Exception as e:
        print(f"[MB] electric ball error: {e}")


def _draw_multiband_body(rx, ry, rw, rh, rack, rack_idx, scale):
    """Draw multiband compressor body.
    Layout matches agreed sketch:
    - Top 48%: spectrum display with 4 equal-width band curves
    - Bottom 52%: 4 equal columns, each with gain fader left + 2x2 knobs right
    - No coloured backgrounds, only elements carry band colour
    - Equal margins on left and right of band area
    """
    # Lazy imports — avoids circular import at module load time
    import Racks as _racks_mod
    get_rack_channels = _racks_mod.get_rack_channels
    rail_h       = RACK_RAIL_H * scale
    body_h       = rh - rail_h

    # PackAPunch — checked once up front. Its own chassis art already has a
    # background baked into the screen area (no hole needed here, unlike the
    # singleband CRT rack — the screen isn't cut out, it's just art), so the
    # flat dark rect this function normally paints over the spectrum area
    # (further down) gets skipped for this skin so that art stays visible.
    # Every band-coloured element (knobs, faders, response curve + its dots
    # and labels) also collapses to one neon blue via _band_col() below
    # instead of the usual per-band red/green/blue/amber.
    _is_packapunch_mb = False
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas_mb0
        _is_packapunch_mb = _gas_mb0() == MB_PACKAPUNCH_SKIN
    except Exception:
        pass

    def _band_col(i):
        return MB_PACKAPUNCH_COLOR if _is_packapunch_mb else BAND_COLORS[i]

    # PNG background — blit first, everything draws on top
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_bg
        from ui.mixer.texture_cache import blit_texture as _blt_bg
        _tex_bg = _gtc_bg("rack_comp_multi_bg")
        if _tex_bg:
            _blt_bg(_tex_bg, rx, ry, rw, rh, key="rack_comp_multi_bg")
        else:
            _draw_rect(rx, ry, rw, body_h, (0.07, 0.07, 0.07, 1.0))
    except Exception:
        _draw_rect(rx, ry, rw, body_h, (0.07, 0.07, 0.07, 1.0))
    # PackAPunch ambient grid glow — behind the live graph, on top of the art
    _mb_pwr = 1.0
    if _is_packapunch_mb:
        _mb_pwr = _mb_power_level(rack_idx, rack.enabled, _time_mb.perf_counter())
    if _is_packapunch_mb and MB_GLOW_ENABLED:
        _draw_mb_grid_pulse(rx, ry, rw, rh, _mb_pwr)
    spec_zone_h  = body_h * 0.48
    fader_zone_h = body_h * 0.52

    # Channel buttons take 108px on right — equal margin on left
    ch_btn_w   = 108 * scale
    side_margin = ch_btn_w / 2   # 54px each side

    # Total content width available
    content_w   = rw - ch_btn_w - side_margin
    band_w      = content_w / 4
    content_x   = rx + side_margin / 2

    shader = _get_mb_shader()  # cached — never recreated per frame

    # ----------------------------------------------------------------
    # SPECTRUM DISPLAY — FabFilter style full-width GR curve
    # Background: grey FFT bars showing audio content
    # Foreground: smooth coloured GR curve dipping at compressed bands
    # ----------------------------------------------------------------
    spec_x = content_x
    spec_y = ry + fader_zone_h + 4*scale
    spec_w = content_w
    spec_h = spec_zone_h - 8*scale

    # Background — skipped for packapunch so its own baked-in screen art
    # (behind the chassis blit above) stays visible instead of being
    # painted over with a flat dark rect.
    if not _is_packapunch_mb:
        _draw_rect(spec_x, spec_y, spec_w, spec_h, (0.04, 0.04, 0.04, 1.0))
    bv = [(spec_x,spec_y),(spec_x+spec_w,spec_y),
          (spec_x+spec_w,spec_y+spec_h),(spec_x,spec_y+spec_h),(spec_x,spec_y)]
    bb = batch_for_shader(shader,"LINE_STRIP",{"pos":bv})
    shader.bind(); shader.uniform_float("color",(0.15,0.15,0.15,1.0)); bb.draw(shader)

    # (dB grid lines drawn later with correct _db_to_y mapping)

    # ----------------------------------------------------------------
    # 7-LAYER OVERLAPPING SPECTRUM — Premiere Pro multiband style
    #
    # Y AXIS CONVENTION (Blender GPU: y=0 at BOTTOM, increases upward):
    #   spec_y           = bottom of rect = -80dB
    #   spec_y + spec_h  = top of rect    =   0dB
    #   amp = (dB + 80) / 80  →  0.0 at bottom, 1.0 at top
    #   _amp_to_y(amp) = spec_y + amp * spec_h   ← bottom-origin
    #   floor of fill  = spec_y  (the bottom, not spec_y+spec_h)
    # ----------------------------------------------------------------
    import math as _mspec

    LOG_MIN  = _mspec.log10(20.0)
    LOG_RNG  = _mspec.log10(20000.0) - LOG_MIN
    CROSS_HZ = [120.0, 800.0, 5000.0]

    def _freq_to_x(hz):
        return spec_x + (_mspec.log10(max(hz, 20.0)) - LOG_MIN) / LOG_RNG * spec_w

    def _amp_to_y(amp):
        # amp 0.0 → spec_y (bottom = -80dB), amp 1.0 → spec_y+spec_h (top = 0dB)
        return spec_y + max(0.0, min(1.0, amp)) * spec_h

    def _db_to_y(db):
        return _amp_to_y((db + 80.0) / 80.0)

    # --- Fetch 128-bin full-spectrum + GR from engine ---
    # Average across ALL assigned channels so multi-channel racks show
    # a merged spectrum and the graph doesn't go blank.
    # Muted channels still have spec_bins data (the engine updates them
    # even when muted) so we include them for display — the graph should
    # show what's playing through the rack regardless of mute state.
    spec_bins = None
    gr_data   = [0.0, 0.0, 0.0, 0.0]
    if rack.enabled:
        try:
            from Loader import get_engine as _get_eng_mb
            _eng_mb  = _get_eng_mb()
            assigned = get_rack_channels(rack)
            if _eng_mb and assigned:
                _hj_mb = _eng_mb.get_engine()
                if _hj_mb:
                    _s_mb       = _hj_mb.get_state()
                    bin_acc     = [0.0] * 128
                    gr_acc      = [0.0, 0.0, 0.0, 0.0]
                    valid_count = 0
                    for ch in assigned:
                        if not (0 <= ch < 32): continue
                        _raw = list(_s_mb.get_spec_bins(ch))
                        # Include channel if it has any signal at all —
                        # use a very low threshold so muted-but-assigned
                        # channels don't block display from active channels.
                        if max(_raw) > 0.001:
                            for i in range(128):
                                bin_acc[i] += _raw[i]
                            gr_vals = _s_mb.get_gr_levels(ch)
                            if gr_vals:
                                for b in range(4):
                                    gr_acc[b] += gr_vals[b]
                            valid_count += 1
                    if valid_count > 0:
                        spec_bins = [v / valid_count for v in bin_acc]
                        gr_data   = [v / valid_count for v in gr_acc]
        except Exception:
            spec_bins = None

    # ----------------------------------------------------------------
    # 7-LAYER SPECTRUM — uses pre-computed module-level weight arrays.
    # No trig, no log10, no lambda, no shader construction per frame.
    # Only the vertex positions are recomputed (128 muls per layer).
    # ----------------------------------------------------------------
    if _is_packapunch_mb:
        # Same 4-band / 3-crossover layer order as the default skin below,
        # but every hue collapses to shades of the one neon blue — lighter
        # per band so the 4 bands stay distinguishable without the usual
        # red/green/amber/blue mix. fill/edge follow the same *0.5 (dim,
        # translucent) / *0.9+0.05 (bright, opaque) relationship as the
        # default skin's formula, just applied to a shade instead of a hue.
        bs, cs = MB_PACKAPUNCH_BAND_SHADES, MB_PACKAPUNCH_CROSS_SHADES
        def _mb_layer(shade):
            r, g, b_s = shade
            return ((r*0.5, g*0.5, b_s*0.5, 0.80),
                    (min(1.0, r*0.9+0.05), min(1.0, g*0.9+0.05), min(1.0, b_s*0.9+0.05), 1.0))
        layer_colors = [
            _mb_layer(bs[0]), _mb_layer(cs[0]),
            _mb_layer(bs[1]), _mb_layer(cs[1]),
            _mb_layer(bs[2]), _mb_layer(cs[2]),
            _mb_layer(bs[3]),
        ]
    else:
        bc = BAND_COLORS
        layer_colors = [
            ((bc[0][0]*0.5, bc[0][1]*0.5, bc[0][2]*0.5, 0.80),
             (bc[0][0]*0.9+0.05, bc[0][1]*0.9+0.05, bc[0][2]*0.9+0.05, 1.0)),
            ((0.05, 0.55, 0.65, 0.75), (0.1, 0.75, 0.85, 1.0)),
            ((bc[1][0]*0.5, bc[1][1]*0.5, bc[1][2]*0.5, 0.80),
             (bc[1][0]*0.9+0.05, bc[1][1]*0.9+0.05, bc[1][2]*0.9+0.05, 1.0)),
            ((0.65, 0.58, 0.05, 0.75), (0.85, 0.78, 0.1, 1.0)),
            ((bc[2][0]*0.5, bc[2][1]*0.5, bc[2][2]*0.5, 0.80),
             (bc[2][0]*0.9+0.05, bc[2][1]*0.9+0.05, bc[2][2]*0.9+0.05, 1.0)),
            ((0.55, 0.1, 0.65, 0.75), (0.75, 0.2, 0.85, 1.0)),
            ((bc[3][0]*0.5, bc[3][1]*0.5, bc[3][2]*0.5, 0.80),
             (bc[3][0]*0.9+0.05, bc[3][1]*0.9+0.05, bc[3][2]*0.9+0.05, 1.0)),
        ]

    # x positions are constant for a given spec_w — precompute once per draw
    x_positions = [spec_x + ((_MB_LOG_HZ[i] - _MB_LOG_MIN) / _MB_LOG_RNG) * spec_w
                   for i in range(_MB_N_SPEC)]
    floor_y = spec_y

    if spec_bins is not None:
        # GR linear multipliers — one per main band
        gr_muls = [10.0 ** (-max(0.0, float(gr_data[b])) / 20.0) for b in range(4)]

        _sh = _get_mb_shader()  # cached shader — never recreated
        _sh.bind()
        gpu.state.line_width_set(max(1.2, scale * 1.2))

        for layer_idx in range(7):
            weights  = _MB_WEIGHTS[layer_idx]
            gr_band  = _MB_GR_BAND[layer_idx]
            mul      = gr_muls[gr_band] if gr_band is not None else 1.0
            fill_col, edge_col = layer_colors[layer_idx]

            # Build vertex arrays — pure arithmetic, no trig
            verts_fill = []
            pts        = []
            for i in range(_MB_N_SPEC):
                w   = weights[i]
                amp = float(spec_bins[i]) * mul * w
                if amp > 1.0: amp = 1.0
                px  = x_positions[i]
                py  = spec_y + amp * spec_h
                verts_fill.append((px, floor_y))
                verts_fill.append((px, py))
                pts.append((px, py))

            _b = batch_for_shader(_sh, "TRI_STRIP", {"pos": verts_fill})
            _sh.uniform_float("color", fill_col)
            _b.draw(_sh)

            _be = batch_for_shader(_sh, "LINE_STRIP", {"pos": pts})
            _sh.uniform_float("color", edge_col)
            _be.draw(_sh)

        gpu.state.line_width_set(1.0)

    # ----------------------------------------------------------------
    # dB GRID + AXIS LABELS  (drawn over waveforms)
    # 0dB at top (spec_y+spec_h), -80dB at bottom (spec_y)
    # ----------------------------------------------------------------
    fs_db = max(1, int(7*scale))
    for db_val, label in [(0,"0dB"),(-10,"-10"),(-20,"-20"),(-40,"-40"),(-60,"-60"),(-80,"-80")]:
        gy = _db_to_y(db_val)
        if spec_y - 1 <= gy <= spec_y + spec_h + 1:
            bright = 0.22 if db_val == 0 else 0.10
            _draw_rect(spec_x, gy, spec_w, max(0.5, scale*0.5),
                       (bright, bright, bright, 1.0))
            tw = _text_width(label, fs_db)
            _draw_text(label, spec_x - tw - 3*scale, gy - fs_db*0.5,
                       fs_db, (0.35, 0.35, 0.35, 1.0))

    # ----------------------------------------------------------------
    # CROSSOVER DIVIDERS + Hz LABELS
    # ----------------------------------------------------------------
    cross_labels = ["120Hz", "800Hz", "5kHz"]
    for f_c, lbl in zip(CROSS_HZ, cross_labels):
        dx    = _freq_to_x(f_c)
        _draw_rect(dx, spec_y, max(0.5, scale*0.5), spec_h, (0.4, 0.4, 0.4, 0.5))
        fs_cr = max(1, int(7*scale))
        tw_cr = _text_width(lbl, fs_cr)
        _draw_text(lbl, dx - tw_cr/2, spec_y + spec_h + 2*scale,
                   fs_cr, (0.4, 0.4, 0.4, 1.0))

    if MB_SHOW_FREQ_AXIS:
        for hz_mark, lbl_mark in [(20,"20"),(50,"50"),(100,"100"),(200,"200"),
                                   (500,"500"),(1000,"1k"),(2000,"2k"),
                                   (5000,"5k"),(10000,"10k"),(20000,"20k")]:
            mx   = _freq_to_x(hz_mark)
            fs_m = max(1, int(6*scale))
            tw_m = _text_width(lbl_mark, fs_m)
            if spec_x + 4*scale <= mx <= spec_x + spec_w - 4*scale:
                _draw_text(lbl_mark, mx - tw_m/2, spec_y + spec_h + 10*scale,
                           fs_m, (0.28, 0.28, 0.28, 1.0))

    # ----------------------------------------------------------------
    # SETTINGS-DRIVEN RESPONSE CURVE OVERLAY
    #
    # Uses the SAME _db_to_y axis as the spectrum (0dB top, -80dB bottom).
    # The curve shows actual output level: REF_IN + gain_db + gr_db.
    # With defaults (0dB gain, threshold not hit) it sits at -18dB.
    # Heavy compression pushes it toward -60 or -70dB — a large visible drop
    # that maps directly to the spectrum scale the eye is already reading.
    # ----------------------------------------------------------------
    # REF_INPUT_DB: typical voice programme at -18dBFS.
    # PCM is normalised to -1..+1, same as the old engine — no sidechain offset needed.
    REF_INPUT_DB = -18.0

    def _band_output_db(b):
        """Actual output dB for this band at the reference input level."""
        thr_db  = -60.0 + _rp(rack, b)       * 40.0   # -60..-20dB, matches C++ denorm_threshold
        ratio   =  1.0  + _rp(rack, b + 4)   * 19.0   # 1..20
        knee_db =  0.5  + _rp(rack, b + 20)  * 23.5   # 0.5..24 dB
        gain_db = (_rp(rack, b + 16, 0.5) - 0.5) * 24.0  # -12..+12 dB
        half_k  = knee_db * 0.5
        in_db   = REF_INPUT_DB
        if in_db <= thr_db - half_k:
            gr_db = 0.0
        elif in_db <= thr_db + half_k and knee_db > 0.0:
            x     = in_db - thr_db + half_k
            gr_db = (1.0 / ratio - 1.0) * (x * x) / (2.0 * knee_db)
        else:
            gr_db = (in_db - thr_db) * (1.0 / ratio - 1.0)
        return REF_INPUT_DB + gain_db + gr_db  # output dB, same scale as spectrum

    band_out_db = [_band_output_db(b) for b in range(4)]

    # Reference line at -18dB (where curve sits with no processing)
    ref_y = _db_to_y(REF_INPUT_DB)
    _draw_rect(spec_x, ref_y, spec_w, max(0.8, scale * 0.6),
               (0.5, 0.5, 0.5, 0.55))

    # Map crossover Hz to fractional x positions
    cross_frac = [(_mspec.log10(f) - LOG_MIN) / LOG_RNG for f in CROSS_HZ]

    N_CURVE   = 200
    curve_pts = []
    for i in range(N_CURVE + 1):
        fx = i / N_CURVE
        if   fx <= cross_frac[0]:
            bi = 0; bt = fx / max(cross_frac[0], 1e-9)
        elif fx <= cross_frac[1]:
            bi = 1; bt = (fx - cross_frac[0]) / max(cross_frac[1] - cross_frac[0], 1e-9)
        elif fx <= cross_frac[2]:
            bi = 2; bt = (fx - cross_frac[1]) / max(cross_frac[2] - cross_frac[1], 1e-9)
        else:
            bi = 3; bt = (fx - cross_frac[2]) / max(1.0 - cross_frac[2], 1e-9)
        bt       = max(0.0, min(1.0, bt))
        s        = bt * bt * (3.0 - 2.0 * bt)
        db_here  = band_out_db[bi] + (band_out_db[min(3, bi+1)] - band_out_db[bi]) * s
        # Clamp to display range so curve never leaves the graph
        db_here  = max(-79.0, min(0.0, db_here))
        px = spec_x + fx * spec_w
        py = _db_to_y(db_here)
        curve_pts.append((px, py, bi))

    # Fill between curve and the -18dB reference line — dropped for
    # packapunch so the background art shows through the graph instead of
    # getting washed out by a translucent block sitting over it.
    if not _is_packapunch_mb:
        for i in range(len(curve_pts) - 1):
            px1, py1, bi = curve_pts[i]
            px2, py2, _  = curve_pts[i + 1]
            col           = _band_col(bi)
            r, g, b_c, _a = col
            y_lo = min(py1, ref_y);  y_hi = max(py1, ref_y)
            fh   = y_hi - y_lo
            if fh > 0.5:
                _draw_rect(px1, y_lo, max(px2 - px1, 0.5), fh,
                           (r * 0.45, g * 0.45, b_c * 0.45, 0.45))

    # Curve line — coloured by band
    for i in range(len(curve_pts) - 1):
        px1, py1, bi = curve_pts[i]
        px2, py2, _  = curve_pts[i + 1]
        col           = _band_col(bi)
        r, g, b_c, _a = col
        _draw_line(px1, py1, px2, py2,
                   (min(1.0, r*1.4), min(1.0, g*1.4), min(1.0, b_c*1.4), 1.0),
                   max(2.0, scale * 2.0))

    # Band-centre dots + output dB labels
    band_centres_hz = [60.0, 350.0, 2000.0, 12000.0]
    for band in range(4):
        bx_c  = _freq_to_x(band_centres_hz[band])
        py_c  = _db_to_y(band_out_db[band])
        py_c  = max(spec_y + 4*scale, min(spec_y + spec_h - 4*scale, py_c))
        col   = _band_col(band)
        r, g, b_c, _a = col
        _draw_circle(bx_c, py_c, 5*scale,
                     (min(1.0, r*1.5), min(1.0, g*1.5), min(1.0, b_c*1.5), 1.0))
        _draw_circle(bx_c, py_c, 5*scale, (0.08, 0.08, 0.08, 0.7), filled=False)
        fs_lbl = max(1, int(7*scale))
        lbl    = f"{band_out_db[band]:.1f}dB"
        tw_lbl = _text_width(lbl, fs_lbl)
        _draw_text(lbl, bx_c - tw_lbl/2, py_c + 7*scale,
                   fs_lbl, (r, g, b_c, 0.9))

    # Band name labels at bottom of spectrum
    for band in range(4):
        col  = _band_col(band)
        bx_c = _freq_to_x(band_centres_hz[band])
        fs_bn = max(1, int(8*scale))
        tw_bn = _text_width(BAND_NAMES[band], fs_bn)
        _draw_text(BAND_NAMES[band], bx_c - tw_bn/2,
                   spec_y + 2*scale,
                   fs_bn, (col[0]*0.8, col[1]*0.8, col[2]*0.8, 0.85))

    # ----------------------------------------------------------------
    # FADER + KNOB ZONE — bottom portion, 4 equal columns
    # Layout per column:
    #   Left ~30px: vertical gain fader + "GAIN" label
    #   Thin divider
    #   Right section: 2x2 knob grid (Thr, Ratio top row; Atk, Rel bottom row)
    #   Bottom: band name + freq range
    # ----------------------------------------------------------------
    fader_area_y = ry + 4*scale
    fader_area_h = fader_zone_h - 8*scale

    label_h  = 26*scale   # band name + freq at bottom
    ctrl_y   = fader_area_y + label_h
    ctrl_h   = fader_area_h - label_h

    # Knob layout: 2 rows, 2 cols within right section
    # Calculated so labels (22px below each knob) never overlap adjacent row:
    #   Bottom row centre: ctrl_y + 4 + 22 + r
    #   Top row centre:    bottom_row + r + 6 + 22 + r
    fader_strip_w = 38*scale
    knob_area_w   = band_w - fader_strip_w - 8*scale
    knob_r        = min(knob_area_w * 0.18, 18*scale)
    knob_r        = max(knob_r, 12*scale)
    knob_r        = knob_r * MB_KNOB_SCALE
    knob_col_gap  = knob_area_w / 2
    knob_label_h  = 22*scale   # space needed below each knob for labels
    knob_gap      = 6*scale    # gap between top-row label and bottom-row knob

    for band in range(4):
      try:
        bx  = content_x + band * band_w
        col = _band_col(band)
        col_rgb = col[:3]  # _draw_knob expects 3-tuple; col has alpha

        # Band name + freq label at bottom — suppressed when baked into PNG
        if MB_BAND_SHOW_LABELS:
            fs_bn = max(1, int(9*scale))
            tw_bn = _text_width(BAND_NAMES[band], fs_bn)
            _draw_text(BAND_NAMES[band],
                       bx + band_w/2 - tw_bn/2,
                       fader_area_y + 14*scale, fs_bn, col)
            fs_fr = max(1, int(7*scale))
            tw_fr = _text_width(BAND_FREQS[band], fs_fr)
            _draw_text(BAND_FREQS[band],
                       bx + band_w/2 - tw_fr/2,
                       fader_area_y + 3*scale, fs_fr,
                       (col[0]*0.55, col[1]*0.55, col[2]*0.55, 1.0))

        # --- GAIN FADER (left strip) ---
        fdr_x  = bx + 6*scale
        fdr_w  = 10*scale
        fdr_cx = fdr_x + fdr_w/2 - 2*scale   # rail centre
        gain_norm = _rp(rack, band+16, 0.5)
        # dB label above fader
        gain_db  = (gain_norm - 0.5) * 24.0  # -12 to +12 dB
        fs_g     = max(1, int(7*scale))
        gain_str = f"{gain_db:+.0f}"
        tw_g     = _text_width(gain_str, fs_g)
        if MB_SHOW_GAIN_TEXT:
            _draw_text(gain_str, bx+6*scale + fdr_w/2 - tw_g/2,
                       ctrl_y + ctrl_h - 12*scale, fs_g, (0.75,0.75,0.75,1.0))
            fs_gl = max(1, int(7*scale))
            _draw_text("GAIN", bx+6*scale, ctrl_y + ctrl_h - 22*scale,
                       fs_gl, (col[0]*0.7,col[1]*0.7,col[2]*0.7,1.0))

        fdr_h  = ctrl_h - 26*scale
        fdr_y  = ctrl_y + 2*scale
        # Rail
        _draw_rect(fdr_cx, fdr_y, 4*scale, fdr_h, (0.06,0.06,0.06,1.0))
        # 0dB mark
        unity_y = fdr_y + 0.5 * fdr_h
        _draw_rect(fdr_cx - 2*scale, unity_y, 8*scale,
                   max(0.5, scale*0.5), (0.3,0.3,0.3,1.0))
        # Handle
        handle_h = max(8*scale, fdr_h * 0.07)
        handle_y = fdr_y + gain_norm * (fdr_h - handle_h)

        # Fader handle — try band-specific PNG, fall back to coloured rect
        _mb_fader_keys = ["rack_mb_fader_low", "rack_mb_fader_lmid",
                          "rack_mb_fader_hmid", "rack_mb_fader_high"]
        try:
            from ui.mixer.texture_cache import get_texture as _gtc_fdr
            from ui.mixer.texture_cache import blit_texture as _blt_fdr
            _fdr_tex = _gtc_fdr(_mb_fader_keys[band])
        except Exception:
            _fdr_tex = None

        if _fdr_tex:
            _fdr_s_w = fdr_w  * MB_FADER_HANDLE_SCALE
            _fdr_s_h = handle_h * MB_FADER_HANDLE_SCALE
            _fdr_bx  = fdr_x   + (fdr_w   - _fdr_s_w) / 2 + MB_FADER_HANDLE_X_OFFSET * scale
            _fdr_by  = handle_y + (handle_h - _fdr_s_h) / 2 + MB_FADER_HANDLE_Y_OFFSET * scale
            _blt_fdr(_fdr_tex, _fdr_bx, _fdr_by, _fdr_s_w, _fdr_s_h,
                     key=_mb_fader_keys[band])
        else:
            _draw_rect(fdr_x, handle_y, fdr_w, handle_h,
                       (col[0]*0.85, col[1]*0.85, col[2]*0.85, 1.0))
            _draw_rect(fdr_x, handle_y + handle_h/2 - max(0.5,scale*0.5),
                       fdr_w, max(1.0, scale),
                       (min(1.0,col[0]*1.4), min(1.0,col[1]*1.4), min(1.0,col[2]*1.4), 1.0))

        # GR meter — slim bar to the right of the gain fader
        # Shows live gain reduction for this band, 0dB at top filling downward
        gr_meter_w = 6*scale
        gr_meter_x = fdr_x + fdr_w + 3*scale
        gr_meter_y = fdr_y
        gr_meter_h = fdr_h

        # Reuse gr_data already fetched at the top of this function —
        # avoids calling get_engine() 4 more times per frame per rack.
        gr_db_band = float(gr_data[band]) if band < len(gr_data) else 0.0
        sig_norm   = 0.0
        if spec_bins is not None and rack.enabled:
            try:
                from Loader import get_engine as _get_eng_gr
                _eng_gr = _get_eng_gr()
                assigned_gr2 = get_rack_channels(rack)
                if _eng_gr and assigned_gr2:
                    _hj_gr = _eng_gr.get_engine()
                    if _hj_gr:
                        # Average band level across assigned channels
                        lvl_acc = 0.0; lvl_cnt = 0
                        _s_gr = _hj_gr.get_state()
                        for ch_gr2 in assigned_gr2:
                            if 0 <= ch_gr2 < 32:
                                bl = _s_gr.get_band_levels(ch_gr2)
                                if bl and band < len(bl):
                                    lvl_acc += float(bl[band]); lvl_cnt += 1
                        if lvl_cnt > 0:
                            sig_norm = min(1.0, (lvl_acc / lvl_cnt) * 6.0)
            except Exception:
                pass

        _draw_gr_meter_band(gr_meter_x, gr_meter_y, gr_meter_w,
                            gr_meter_h, gr_db_band, scale,
                            signal_norm=sig_norm, packapunch=_is_packapunch_mb)

        # Thin divider after fader strip
        div_x = bx + fader_strip_w
        _draw_rect(div_x, ctrl_y, max(0.5,scale*0.5), ctrl_h, (0.18,0.18,0.18,1.0))

        # --- 3x2 KNOB GRID (Thr/Ratio/Knee top, Atk/Rel/Gain bottom) ---
        knob_base_x = div_x + 4*scale
        knob_col_gap3 = knob_area_w / 3
        kx0 = knob_base_x + knob_col_gap3 * 0.5
        kx1 = knob_base_x + knob_col_gap3 * 1.5
        kx2 = knob_base_x + knob_col_gap3 * 2.5
        ky1 = ctrl_y + ctrl_h * 0.25              # bottom row knob centre
        ky0 = ctrl_y + ctrl_h * 0.75              # top row knob centre

        # Per-knob offsets (unscaled px → scaled at draw time)
        _lbl = not MB_KNOB_SHOW_LABELS  # True = suppress (empty strings passed)

        # Threshold (p0-p3)
        thr_n   = _rp(rack, band)
        thr_db  = -60.0 + thr_n*40.0
        _ox, _oy = MB_KNOB_THR_OFFSETS[band]
        _draw_knob(kx0 + _ox*scale, ky0 + _oy*scale, knob_r, thr_n, col_rgb,
                   "" if _lbl else "Thr", "" if _lbl else f"{thr_db:.0f}dB", scale)

        # Ratio (p4-p7)
        rat_n  = _rp(rack, band+4)
        ratio  = 1.0 + rat_n*19.0
        _ox, _oy = MB_KNOB_RAT_OFFSETS[band]
        _draw_knob(kx1 + _ox*scale, ky0 + _oy*scale, knob_r, rat_n, col_rgb,
                   "" if _lbl else "Ratio", "" if _lbl else f"{ratio:.1f}:1", scale)

        # Knee (p20-p23)
        kne_n  = _rp(rack, band+20, 0.14)
        kne_db = 0.5 + kne_n*23.5
        _ox, _oy = MB_KNOB_KNE_OFFSETS[band]
        _draw_knob(kx2 + _ox*scale, ky0 + _oy*scale, knob_r, kne_n, col_rgb,
                   "" if _lbl else "Knee", "" if _lbl else f"{kne_db:.1f}dB", scale)

        # Attack (p8-p11)
        atk_n  = _rp(rack, band+8)
        atk_ms = 0.1 + atk_n*99.9
        _ox, _oy = MB_KNOB_ATK_OFFSETS[band]
        _draw_knob(kx0 + _ox*scale, ky1 + _oy*scale, knob_r, atk_n, col_rgb,
                   "" if _lbl else "Atk", "" if _lbl else f"{atk_ms:.0f}ms", scale)

        # Release (p12-p15)
        rel_n  = _rp(rack, band+12)
        rel_ms = 10.0 + rel_n*990.0
        _ox, _oy = MB_KNOB_REL_OFFSETS[band]
        _draw_knob(kx1 + _ox*scale, ky1 + _oy*scale, knob_r, rel_n, col_rgb,
                   "" if _lbl else "Rel", "" if _lbl else f"{rel_ms:.0f}ms", scale)

        # Gain (p16-p19)
        gain_n  = _rp(rack, band+16, 0.5)
        gain_db = (gain_n - 0.5) * 24.0
        _ox, _oy = MB_KNOB_GAN_OFFSETS[band]
        _draw_knob(kx2 + _ox*scale, ky1 + _oy*scale, knob_r, gain_n, col_rgb,
                   "" if _lbl else "Gain", "" if _lbl else f"{gain_db:+.0f}dB", scale)

        # Column divider (not after last band)
        if band < 3:
            _draw_rect(bx + band_w, fader_area_y,
                       max(0.5,scale*0.5), fader_area_h,
                       (0.18,0.18,0.18,1.0))
      except Exception as e:
        import traceback
        print(f"[MB] band {band} draw error: {e}")
        print(traceback.format_exc())

    # PackAPunch electric ball — on top of the art, graph and knobs
    if _is_packapunch_mb and MB_PULSE_ENABLED:
        _draw_mb_pulse(rx, ry, rw, rh, scale, rack_idx, _mb_pwr)

    # ── Full-rack PNG blit — drawn LAST so it paints over body content.
    # Glass overlay — composited on top of spectrum/knobs/faders, same rect as body PNG
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_gl
        from ui.mixer.texture_cache import blit_texture as _blt_gl
        _gl_tex = _gtc_gl("rack_comp_multi_glass")
        if _gl_tex:
            _blt_gl(_gl_tex, rx, ry, rw, rh, key="rack_comp_multi_glass", blend="ALPHA")
    except Exception:
        pass
