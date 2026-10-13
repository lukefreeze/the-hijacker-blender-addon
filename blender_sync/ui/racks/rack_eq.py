# =============================================================================
# rack_eq.py
# Parametric EQ rack UI (7-band)
# ┌─ LAYOUT CONSTANTS ─────────────────────────────────────────────────────┐
# │ RACK_EXPANDED_H_EQ — change height here
# └────────────────────────────────────────────────────────────────────────┘
# =============================================================================

import math
import os
import gpu
from gpu_extras.batch import batch_for_shader
from core.constants import ASSETS_DIR
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
# PARAMETRIC EQ KNOB TUNING
# All values are unscaled px — multiplied by scale at draw time.
#
# EQ_KNOB_SCALE      : size multiplier for all knobs (1.0 = no change)
#                      affects kr_gain and kr_small proportionally
# EQ_KNOB_X_OFFSET   : shifts the entire knob strip left/right
#                      (moves all 7 columns together)
# EQ_KNOB_Y_LABEL    : Y nudge for the band label row  (L/1/2/3/4/5/H)
# EQ_KNOB_Y_GAIN     : Y nudge for the Gain knob row
# EQ_KNOB_Y_FREQ     : Y nudge for the Freq knob row
# EQ_KNOB_Y_Q        : Y nudge for the Q/shelf knob row
#
# Positive Y moves up, negative moves down.
# Tune EQ_KNOB_SCALE first, then use the Y nudges per row.
# =============================================================================
EQ_KNOB_SCALE    = 0.7
EQ_KNOB_X_OFFSET = 0.0

# =============================================================================
# PACKAPUNCH KNOBS
# The knobs are baked into rack_eq_bg.png; on that skin the live knobs are the exported knob-face images
# (rack_eq_knob_*.png) drawn at the exact spot of the baked ones and turned to show the value. Positions are the
# centres of the faces in the art (art px, y down), measured by matching each exported image against the background.
# =============================================================================
EQ_ART_W, EQ_ART_H = 1488.0, 719.0
EQ_PP_KNOB_SWEEP   = 270.0     # degrees of turn from minimum to maximum
EQ_PP_KNOB_OFFSET  = 0.0       # extra turn in degrees (+ = anticlockwise)
EQ_PP_HIT_GAIN     = 30.0      # click radius of the big (gain) knobs, art px
EQ_PP_HIT_SMALL    = 21.0      # click radius of the small (freq / Q) knobs, art px
# parameter index -> (texture key, centre x, centre y, image width, image height)  (art px)
EQ_PP_KNOBS = {
    0:  ("rack_eq_knob_g0",  145.0, 514.0, 50, 48),   1:  ("rack_eq_knob_g1",  332.0, 514.0, 50, 48),
    2:  ("rack_eq_knob_g2",  515.0, 514.0, 50, 48),   3:  ("rack_eq_knob_g3",  700.0, 516.0, 50, 48),
    4:  ("rack_eq_knob_g4",  884.0, 514.0, 50, 48),   5:  ("rack_eq_knob_g5", 1070.0, 515.0, 50, 48),
    6:  ("rack_eq_knob_g6", 1255.0, 515.0, 50, 48),
    7:  ("rack_eq_knob_f0",  146.0, 594.5, 32, 31),   8:  ("rack_eq_knob_f1",  332.0, 594.5, 32, 31),
    9:  ("rack_eq_knob_f2",  515.0, 594.5, 32, 31),   10: ("rack_eq_knob_f3",  700.0, 593.5, 32, 31),
    11: ("rack_eq_knob_f4",  884.0, 595.5, 32, 31),   12: ("rack_eq_knob_f5", 1070.0, 595.5, 32, 31),
    13: ("rack_eq_knob_f6", 1255.0, 595.5, 32, 31),
    15: ("rack_eq_knob_q1",  332.0, 671.5, 32, 31),   16: ("rack_eq_knob_q2",  516.0, 672.5, 32, 31),
    17: ("rack_eq_knob_q3",  700.0, 670.5, 32, 31),   18: ("rack_eq_knob_q4",  885.0, 672.5, 32, 31),
    19: ("rack_eq_knob_q5", 1070.0, 671.5, 32, 31),
}


def eq_pp_knob_geom(rack_x, rack_y, rw, rh):
    """Screen-space layout of the packApunch EQ knobs: {param_index: (cx, cy, w, h, hit_radius, texture_key)},
    or None when the packApunch skin is not active. (The shelves have no Q knob: params 14 and 20 are absent.)"""
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas_eqk
        if _gas_eqk() != EQ_SCREEN_SEQ_SKIN:
            return None
    except Exception:
        return None
    sx, sy = rw / EQ_ART_W, rh / EQ_ART_H
    sr = min(sx, sy)
    out = {}
    for pi, (key, ax, ay, aw, ah) in EQ_PP_KNOBS.items():
        hit = EQ_PP_HIT_GAIN if pi < 7 else EQ_PP_HIT_SMALL
        out[pi] = (rack_x + ax * sx, rack_y + (1.0 - ay / EQ_ART_H) * rh, aw * sx, ah * sy, hit * sr, key)
    return out

# =============================================================================
# PACKAPUNCH SCREEN SEQUENCE
# Animated vortex background drawn behind the frequency display, only while
# the "packapunch" skin is active (default skin is untouched — see the
# EQ_SCREEN_SEQ_SKIN check at the draw site below). These are the knobs to
# hand-tune for THIS rack; the player itself is generic and reused as-is by
# any other rack that gets one of these (core/rack_screen_sequence.py).
#
# EQ_SCREEN_SEQ_DIR      : folder of pre-scaled, pre-matted, pre-cropped PNG
#                          frames — lives in the skin's own asset folder
#                          (.../skins/packapunch/EQ_frames/) rather than
#                          "working" since these are a shipped asset, not
#                          scratch material, and "working" isn't pushed to
#                          git. Originally produced by the batch pipeline
#                          that still lives at .../working/parametricEQ/
#                          packApunch/process_frames.py, if this sequence
#                          ever needs regenerating from the raw source
#                          frames (same folder, frames/).
#                          119 frames (every other frame of the original
#                          238 dropped) run through pngquant to cut the
#                          shipped size — 238 raw frames @ 1261x354 RGBA
#                          was ~83MB; this is ~18MB. Re-run both steps
#                          together if the source footage changes: halving
#                          the frame count on its own would double actual
#                          playback speed unless EQ_SCREEN_SEQ_FPS below
#                          is halved to match, which it already is here.
# EQ_SCREEN_SEQ_FPS      : playback rate for the loop.
# EQ_SCREEN_SEQ_STRETCH  : True  = stretch the sequence to exactly fill the
#                                  display rect (disp_w x disp_h) — safe
#                                  default, always fully covers the screen.
#                          False = draw at the frame's own native pixel size
#                                  (x scale) instead, positioned purely by
#                                  the offsets below. Try this if stretching
#                                  looks visibly distorted.
# EQ_SCREEN_SEQ_WIDTH_SCALE  : fraction of disp_w to use when STRETCH is on.
#                               1.0 = fill the full display width. Our CRT-
#                               style PackAPunch screen reads narrower than
#                               the display box, hence < 1.0 below.
# EQ_SCREEN_SEQ_HEIGHT_SCALE : same, for disp_h.
#                               Both scales shrink the sequence toward the
#                               CENTRE of the display rect; EQ_SCREEN_SEQ_
#                               OFFSET_X/Y below still nudge it further from
#                               that centred position, same as before.
# EQ_SCREEN_SEQ_OFFSET_X : unscaled px, nudge from the centred position.
#                          Positive = right.
# EQ_SCREEN_SEQ_OFFSET_Y : unscaled px. Positive = up.
# =============================================================================
EQ_SCREEN_SEQ_SKIN          = "packapunch"
EQ_SCREEN_SEQ_DIR           = os.path.join(
    ASSETS_DIR, "packapunch", "EQ_frames")
EQ_SCREEN_SEQ_FPS           = 12.0  # halved alongside the frame count (238->119)
# Power on/off: the screen animation winds down to a stop when the rack is
# switched off, and winds back up to full speed when it's switched on again.
EQ_SEQ_RAMP_DOWN_SEC        = 2.0   # seconds from full speed to a standstill
EQ_SEQ_RAMP_UP_SEC          = 2.5   # seconds from standstill back to full speed
EQ_SCREEN_SEQ_STRETCH       = True
EQ_SCREEN_SEQ_WIDTH_SCALE   = 0.97
EQ_SCREEN_SEQ_HEIGHT_SCALE  = 0.98
EQ_SCREEN_SEQ_OFFSET_X      = 2
EQ_SCREEN_SEQ_OFFSET_Y      = 0

# =============================================================================
# PACKAPUNCH CURVE COLOUR
# The main combined-response curve reads fine as neon cyan against the old
# flat dark background, but disappears against the blue-heavy vortex screen
# above — swapped to neon red + a glow halo for packapunch only; every other
# skin keeps the original cyan untouched. The fill under the curve is
# dropped entirely for packapunch rather than recoloured — it doesn't follow
# the screen's matted glass shape, so a flat fill just paints over the
# vortex background underneath it.
# =============================================================================
EQ_PACKAPUNCH_CURVE_COLOR = (1.00, 0.08, 0.10)   # neon red, RGB 0-1
EQ_PACKAPUNCH_CURVE_GLOW  = True                 # halo passes under the core line

# =============================================================================
# PER-KNOB X/Y NUDGE TABLES
# Index order matches band order: [L, 1, 2, 3, 4, 5, H]
# All values unscaled px — multiplied by scale at draw time.
# Positive X = right, Negative X = left.
# Positive Y = up,    Negative Y = down.
#
# EQ_LABEL_X / EQ_LABEL_Y  : band label (L/1/2/3/4/5/H)
# EQ_GAIN_X  / EQ_GAIN_Y   : Gain knob
# EQ_FREQ_X  / EQ_FREQ_Y   : Freq knob
# EQ_Q_X     / EQ_Q_Y      : Q knob (shelf placeholder for L and H)
# =============================================================================
#                       L     1     2     3     4     5     H
EQ_LABEL_X = [        0.25,  0.25,  0.25,  0.25,  1.0,  1.5,  1.25]
EQ_LABEL_Y = [        -1.0,  -1.0,  -1.0,  -1.0,  -1.0,  -1.0,  -1.0]

EQ_GAIN_X  = [        -2.0,  -1.0,  -0.5,  0.0,  0.0,  1.0,  2.0]
EQ_GAIN_Y  = [        -2.0,  -2.0,  -2.0,  -2.0,  -2.0,  -2.0,  -2.0]

EQ_FREQ_X  = [        -0.35,  0.0,  0.0,  0.15,  0.25,  0.75,  1.5]
EQ_FREQ_Y  = [        -2.35,  -2.35,  -2.35,  -2.35,  -2.35,  -2.35,  -2.35]

EQ_Q_X     = [        -0.25,  0.0,  0.0,  0.0,  0.5,  1.5,  0.0]
EQ_Q_Y     = [        -2.0,  -2.0,  -2.0,  -2.0,  -2.0,  -2.0,  -2.0]

# EQ frequency/Q scale constants — used by _draw_eq_body helpers
EQ_FREQ_MIN_LOG = math.log10(20.0)
EQ_FREQ_MAX_LOG = math.log10(20000.0)
EQ_Q_MIN_LOG    = math.log10(0.1)
EQ_Q_MAX_LOG    = math.log10(10.0)


def _eq_freq_from_norm(norm):
    """Convert 0-1 norm to Hz (log scale)."""
    return 10.0 ** (EQ_FREQ_MIN_LOG + norm * (EQ_FREQ_MAX_LOG - EQ_FREQ_MIN_LOG))


def _eq_freq_to_norm(hz):
    return max(0.0, min(1.0,
        (math.log10(max(20.0, hz)) - EQ_FREQ_MIN_LOG) /
        (EQ_FREQ_MAX_LOG - EQ_FREQ_MIN_LOG)))


def _eq_q_from_norm(norm):
    return 10.0 ** (EQ_Q_MIN_LOG + norm * (EQ_Q_MAX_LOG - EQ_Q_MIN_LOG))


def _eq_q_to_norm(q):
    return max(0.0, min(1.0,
        (math.log10(max(0.1, q)) - EQ_Q_MIN_LOG) /
        (EQ_Q_MAX_LOG - EQ_Q_MIN_LOG)))

def _eq_biquad_response(freq_hz, gain_db, band_filter_type, q, f_test):
    """
    Compute magnitude response in dB at f_test Hz for one EQ band.
    Uses Audio EQ Cookbook biquad formulae (same as Loader.py).
    sample_rate assumed 48000 for display purposes.
    """
    sr = 48000.0
    w0 = 2.0 * math.pi * freq_hz / sr
    wt = 2.0 * math.pi * f_test  / sr
    cw0, sw0 = math.cos(w0), math.sin(w0)
    cwt       = math.cos(wt)
    swt       = math.sin(wt)
    alpha     = sw0 / (2.0 * q)
    A         = 10.0 ** (gain_db / 40.0)

    if band_filter_type == "low_shelf":
        sq = 2.0 * math.sqrt(A) * alpha
        b0 = A*((A+1) - (A-1)*cw0 + sq)
        b1 = 2*A*((A-1) - (A+1)*cw0)
        b2 = A*((A+1) - (A-1)*cw0 - sq)
        a0 = (A+1) + (A-1)*cw0 + sq
        a1 = -2*((A-1) + (A+1)*cw0)
        a2 = (A+1) + (A-1)*cw0 - sq
    elif band_filter_type == "high_shelf":
        sq = 2.0 * math.sqrt(A) * alpha
        b0 = A*((A+1) + (A-1)*cw0 + sq)
        b1 = -2*A*((A-1) + (A+1)*cw0)
        b2 = A*((A+1) + (A-1)*cw0 - sq)
        a0 = (A+1) - (A-1)*cw0 + sq
        a1 = 2*((A-1) - (A+1)*cw0)
        a2 = (A+1) - (A-1)*cw0 - sq
    else:  # peak
        alpha_a = sw0 / (2.0 * q)
        b0 = 1 + alpha_a * A
        b1 = -2 * cw0
        b2 = 1 - alpha_a * A
        a0 = 1 + alpha_a / A
        a1 = -2 * cw0
        a2 = 1 - alpha_a / A

    # Evaluate H(e^jwt) via the bilinear s→z substitution
    # |H(z)| at z=e^jwt:  num = b0 + b1*e^-jwt + b2*e^-2jwt
    #                      den = a0 + a1*e^-jwt + a2*e^-2jwt
    try:
        nr = b0/a0 + (b1/a0)*cwt + (b2/a0)*math.cos(2*wt)
        ni = -(b1/a0)*swt - (b2/a0)*math.sin(2*wt)
        dr = 1.0   + (a1/a0)*cwt + (a2/a0)*math.cos(2*wt)
        di = -(a1/a0)*swt - (a2/a0)*math.sin(2*wt)
        mag_sq = (nr*nr + ni*ni) / max(1e-30, dr*dr + di*di)
        return 10.0 * math.log10(max(1e-10, mag_sq))
    except Exception:
        return 0.0


_eq_seq_speed = {}      # rack_idx -> {"s": current speed 0..1, "t": last update time}


def _eq_seq_speed_now(rack_idx, enabled):
    """Playback speed of this rack's screen animation: 1 = normal, 0 = stopped.
    Eases toward 0 when the rack is off and back toward 1 when it's on."""
    import time as _t
    now = _t.perf_counter()
    st = _eq_seq_speed.get(rack_idx)
    if st is None:                                   # first sight: no ramp
        st = {"s": 1.0 if enabled else 0.0, "t": now}
        _eq_seq_speed[rack_idx] = st
    dt = min(0.25, max(0.0, now - st["t"]))
    st["t"] = now
    if enabled:
        st["s"] = min(1.0, st["s"] + dt / max(0.01, EQ_SEQ_RAMP_UP_SEC))
    else:
        st["s"] = max(0.0, st["s"] - dt / max(0.01, EQ_SEQ_RAMP_DOWN_SEC))
    return st["s"]


def _draw_eq_body(rx, ry, rw, rh, rack, rack_idx, scale):
    """
    Parametric EQ rack body — Adobe Premiere Pro style.

    Layout:
      Top 62% : frequency display
                - Audio waveform silhouette (mirrored, from RMS envelope)
                - dB grid +/-18dB, octave frequency grid
                - Per-band dim coloured response curves
                - Combined cyan response curve + filled teal area
                - 7 draggable band handle dots
      Bottom 38%: 7-column knob strip  L | 1 | 2 | 3 | 4 | 5 | H
                  Each column: band label, Gain knob, Freq knob, Q knob

    Parameter storage (7-band layout):
      p0-p6  : gain (0.5 = 0dB, range -24..+24dB)
      p7-p13 : freq (log-normalised 0-1)
      p14-p20: Q    (log-normalised 0-1)
    """
    # Lazy imports — avoids circular import at module load time
    import Racks as _racks_mod
    get_rack_channels = _racks_mod.get_rack_channels
    import math as _m
    rail_h = RACK_RAIL_H * scale
    body_h = rh - rail_h
    # ── Skin background — full rack height (rail/title now baked into PNG)
    # _eq_tex is reused further down (packapunch "frame" layer, drawn again
    # UV-sliced to just the display sub-rect) so it must stay a safely
    # referenceable name even if this try block fails before assignment.
    _eq_tex = None
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_eq
        from ui.mixer.texture_cache import blit_texture as _blt_eq
        _eq_tex = _gtc_eq("rack_eq_bg")
        if _eq_tex:
            _blt_eq(_eq_tex, rx, ry, rw, rh, key="rack_eq_bg")
        else:
            _draw_rect(rx, ry, rw, body_h, (0.07, 0.07, 0.07, 1.0))
    except Exception:
        _draw_rect(rx, ry, rw, body_h, (0.07, 0.07, 0.07, 1.0))

    EQ7_BANDS = [
        ("L",  (0.30, 0.60, 1.00), "low_shelf",   80.0,  0.7),
        ("1",  (0.25, 0.90, 0.55), "peak",        250.0,  1.0),
        ("2",  (0.50, 0.90, 0.20), "peak",        700.0,  1.0),
        ("3",  (0.95, 0.85, 0.10), "peak",       2000.0,  1.0),
        ("4",  (1.00, 0.55, 0.10), "peak",       5000.0,  1.0),
        ("5",  (0.95, 0.30, 0.55), "peak",      10000.0,  1.0),
        ("H",  (0.80, 0.30, 1.00), "high_shelf", 16000.0, 0.7),
    ]
    N_BANDS = 7

    def _get_b(bi):
        gn  = getattr(rack, f"p{bi}",      0.5)
        fn  = getattr(rack, f"p{bi + 7}", -1.0)
        qn  = getattr(rack, f"p{bi + 14}",-1.0)
        gdb = (gn - 0.5) * 48.0
        _, _, bft, df, dq = EQ7_BANDS[bi]
        if fn < 0.0: fn = _eq_freq_to_norm(df)
        if qn < 0.0: qn = _eq_q_to_norm(dq)
        return gdb, _eq_freq_from_norm(fn), _eq_q_from_norm(qn), gn, fn, qn

    # -----------------------------------------------------------------------
    # Geometry
    # -----------------------------------------------------------------------
    rail_h   = RACK_RAIL_H * scale
    body_h   = rh - rail_h
    ch_btn_w = 108 * scale
    margin_l = 42 * scale
    margin_r = ch_btn_w + 8 * scale

    disp_x = rx + margin_l
    disp_w = rw - margin_l - margin_r
    # 54% display / 44% knobs — more knob room for larger controls + visible labels
    disp_h = body_h * 0.54
    disp_y = ry + body_h - disp_h - 2 * scale   # top of body (display at top)

    knob_h = body_h * 0.44 - 6 * scale
    knob_y = ry + 2 * scale                      # knob strip at bottom of body

    # -----------------------------------------------------------------------
    # PLOT AREA — where all frequency-axis content (grid, spectrum, curves,
    # band dots) actually draws. Equals the full display box for every skin
    # except packapunch, where the CRT-style screen art is narrower than the
    # box itself — without this, the curve/grid/dots draw past the visible
    # screen edges into the surrounding rack art. Same width/height-scale +
    # centre + offset math as the background sequence below, computed once
    # here and shared so nothing drifts out of sync with where the vortex
    # background itself sits.
    # -----------------------------------------------------------------------
    plot_x, plot_w = disp_x, disp_w
    plot_y, plot_h = disp_y, disp_h
    # Computed once here and reused for every packapunch-specific choice
    # below (background sequence, plot-area narrowing, fill removal, curve
    # colour) instead of re-querying the active skin at each separate site.
    _is_packapunch = False
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas_plot
        _is_packapunch = _gas_plot() == EQ_SCREEN_SEQ_SKIN
        if _is_packapunch:
            plot_w = disp_w * EQ_SCREEN_SEQ_WIDTH_SCALE
            plot_h = disp_h * EQ_SCREEN_SEQ_HEIGHT_SCALE
            plot_x = disp_x + (disp_w - plot_w) * 0.5 + EQ_SCREEN_SEQ_OFFSET_X * scale
            plot_y = disp_y + (disp_h - plot_h) * 0.5 + EQ_SCREEN_SEQ_OFFSET_Y * scale
    except Exception:
        pass

    db_range  = 18.0
    zero_db_y = plot_y + plot_h * 0.5
    px_per_db = (plot_h * 0.5) / db_range

    shader = _get_shader()

    # -----------------------------------------------------------------------
    # DISPLAY BACKGROUND + GRID
    # -----------------------------------------------------------------------
    _drew_seq_bg = False
    try:
        if _is_packapunch:
            from ui.mixer.texture_cache import blit_texture as _blt_seq
            from core.rack_screen_sequence import get_sequence_frame as _gsf_eq
            _seq_tex = _gsf_eq("eq_packapunch_vortex", EQ_SCREEN_SEQ_DIR,
                                fps=EQ_SCREEN_SEQ_FPS,
                                speed=_eq_seq_speed_now(rack_idx, rack.enabled),
                                phase_key=rack_idx)
            if _seq_tex is not None:
                if EQ_SCREEN_SEQ_STRETCH:
                    # plot_x/plot_w/plot_y/plot_h already ARE this exact
                    # box (scale + centre + offset), computed once above so
                    # the curve/grid/dots line up with it automatically.
                    _seq_x, _seq_y, _seq_w, _seq_h = plot_x, plot_y, plot_w, plot_h
                else:
                    _seq_w = _seq_tex.width * scale
                    _seq_h = _seq_tex.height * scale
                    _seq_x = plot_x + (plot_w - _seq_w) * 0.5
                    _seq_y = plot_y + (plot_h - _seq_h) * 0.5
                # ALPHA, not the default ALPHA_PREMULT — these frames carry
                # straight (non-premultiplied) alpha from the GIMP/PIL
                # pipeline that made them, same convention as the mute/solo
                # glow overlays.
                _blt_seq(_seq_tex, _seq_x, _seq_y, _seq_w, _seq_h,
                         key="eq_packapunch_vortex", blend="ALPHA")
                _drew_seq_bg = True
    except Exception as e:
        print(f"[EQ] packapunch screen sequence draw failed: {e}")

    if not _drew_seq_bg:
        _draw_rect(disp_x, disp_y, disp_w, disp_h, (0.035, 0.035, 0.040, 1.0))

    for db_val in (18, 12, 6, 0, -6, -12, -18):
        gy = zero_db_y + db_val * px_per_db
        if not (plot_y <= gy <= plot_y + plot_h):
            continue
        bright = 0.22 if db_val == 0 else 0.09
        lw     = max(1.0, scale) if db_val == 0 else max(0.5, scale * 0.5)
        _draw_rect(plot_x, gy, plot_w, lw, (bright, bright, bright, 0.9))
        lbl   = "0dB" if db_val == 0 else f"{db_val:+d}"
        fs_db = max(1, int(7 * scale))
        tw_db = _text_width(lbl, fs_db)
        _draw_text(lbl, plot_x - tw_db - 4 * scale,
                   gy - fs_db * 0.5, fs_db, (0.30, 0.30, 0.30, 1.0))

    freq_marks = [
        (20, "20"), (50, "50"), (100, "100"), (200, "200"), (500, "500"),
        (1000, "1k"), (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k"),
    ]
    for fhz_m, lbl_m in freq_marks:
        t_m  = (_m.log10(fhz_m) - EQ_FREQ_MIN_LOG) / (EQ_FREQ_MAX_LOG - EQ_FREQ_MIN_LOG)
        gx_m = plot_x + t_m * plot_w
        if not (plot_x <= gx_m <= plot_x + plot_w):
            continue
        _draw_rect(gx_m, plot_y, max(0.5, scale * 0.5), plot_h,
                   (0.11, 0.11, 0.11, 1.0))
        fs_f = max(1, int(7 * scale))
        tw_f = _text_width(lbl_m, fs_f)
        _draw_text(lbl_m, gx_m - tw_f * 0.5,
                   plot_y - 11 * scale, fs_f, (0.28, 0.28, 0.28, 1.0))

    # -----------------------------------------------------------------------
    # SPECTRUM ANALYSER — FabFilter Pro-Q style
    # -----------------------------------------------------------------------
    # Real-time spectrum silhouette using full-spectrum analyser (128 bins,
    # 20Hz-20kHz log-spaced, computed from raw pre-EQ signal in C++).
    # Layer 1: dark grey pre-EQ shape. Layer 2: post-EQ with curve applied.
    freq_amp = []
    try:
        from Loader import get_engine as _get_eng_eq
        import math as _ms

        assigned_sp = get_rack_channels(rack)
        if assigned_sp:
            ch_sp   = list(assigned_sp)[0]
            _eng_eq = _get_eng_eq()
            if _eng_eq:
                _hj_eq = _eng_eq.get_engine()
                if _hj_eq and 0 <= ch_sp < 32:
                    _st_eq  = _hj_eq.get_state()
                    bins    = list(_st_eq.get_spec_bins(ch_sp))
                    N_SPEC  = len(bins)
                    LOG_MIN = _ms.log10(20.0)
                    LOG_RNG = _ms.log10(20000.0) - LOG_MIN

                    # Each bin is already log-spaced — just map to x_norm 0-1
                    freq_amp = []
                    for i, val in enumerate(bins):
                        t_x = i / (N_SPEC - 1)
                        freq_amp.append((t_x, max(0.0, min(1.0, float(val)))))

        def _spectrum_poly(pairs, color, h_scale=0.85):
            """Filled silhouette: each top point paired with floor point below it."""
            if len(pairs) < 2: return
            import gpu
            from gpu_extras.batch import batch_for_shader as _bfs
            _sh = _get_shader()
            # Interleave floor and top: (floor0, top0, floor1, top1, ...)
            # TRI_STRIP naturally fills between them with no cross-over
            verts = []
            for t_x, amp in pairs:
                px = plot_x + t_x * plot_w
                py = plot_y + amp * plot_h * h_scale
                verts.append((px, plot_y))   # floor point
                verts.append((px, py))        # top point
            _b = _bfs(_sh, "TRI_STRIP", {"pos": verts})
            _sh.bind(); _sh.uniform_float("color", color); _b.draw(_sh)

        def _spectrum_edge(pairs, color, h_scale=0.85):
            """Top edge line."""
            if len(pairs) < 2: return
            import gpu
            from gpu_extras.batch import batch_for_shader as _bfs
            _sh = _get_shader()
            verts = [(plot_x + t*plot_w, plot_y + a*plot_h*h_scale)
                     for t, a in pairs]
            _b = _bfs(_sh, "LINE_STRIP", {"pos": verts})
            gpu.state.line_width_set(max(1.0, scale * 1.2))
            _sh.bind(); _sh.uniform_float("color", color); _b.draw(_sh)
            gpu.state.line_width_set(1.0)

        if freq_amp:
            # The filled silhouette (not just its edge line) is what was
            # blocking the vortex background — same reasoning as dropping
            # the EQ-curve fill: it doesn't follow the screen's matted glass
            # shape, so a solid fill just paints over what's behind it.
            # Dropped for packapunch only; default skin keeps both layers.
            if not _is_packapunch:
                _spectrum_poly(freq_amp, (0.17, 0.17, 0.22, 0.85))
            _spectrum_edge(freq_amp, (0.35, 0.35, 0.42, 0.70))

            # Post-EQ layer — apply current EQ curve to each bin
            try:
                band_params_sp = [_get_b(bi) for bi in range(N_BANDS)]
                post_pairs = []
                for t_x, amp in freq_amp:
                    freq_hz = 20.0 * (10.0 ** (t_x * LOG_RNG))
                    eq_db   = 0.0
                    for bi in range(N_BANDS):
                        gdb_s, fhz_s, q_s, _, _, _ = band_params_sp[bi]
                        eq_db += _eq_biquad_response(
                            fhz_s, gdb_s, EQ7_BANDS[bi][2], q_s, freq_hz)
                    lin = 10.0 ** (eq_db / 20.0)
                    post_pairs.append((t_x, max(0.0, min(1.0, amp * lin))))
                if not _is_packapunch:
                    _spectrum_poly(post_pairs, (0.20, 0.28, 0.38, 0.65))
                _spectrum_edge(post_pairs, (0.45, 0.62, 0.85, 0.95))
            except Exception:
                pass

    except Exception:
        pass

    # -----------------------------------------------------------------------
    # EQ CURVES
    # -----------------------------------------------------------------------
    N_PTS = 300
    try:
        band_params = [_get_b(bi) for bi in range(N_BANDS)]

        # Combined response
        combined_db = []
        for pi in range(N_PTS):
            t      = pi / (N_PTS - 1)
            f_test = _eq_freq_from_norm(t)
            total  = 0.0
            for bi in range(N_BANDS):
                gdb_b, fhz_b, q_b, _, _, _ = band_params[bi]
                total += _eq_biquad_response(fhz_b, gdb_b, EQ7_BANDS[bi][2], q_b, f_test)
            combined_db.append(total)

        # Curve points (clamped)
        curve_pts = []
        for pi in range(N_PTS):
            t   = pi / (N_PTS - 1)
            db  = max(-db_range * 1.1, min(db_range * 1.1, combined_db[pi]))
            cpx = plot_x + t * plot_w
            cpy = zero_db_y + db * px_per_db
            cpy = max(plot_y + 1, min(plot_y + plot_h - 1, cpy))
            curve_pts.append((cpx, cpy))

        # Filled area between curve and 0dB — teal, for every skin except
        # packapunch: the vortex screen shows through the gap underneath the
        # curve there, a flat fill would just paint over it, and the fill's
        # own shape doesn't follow the screen's matted glass cutout anyway.
        if not _is_packapunch:
            y_zero  = max(plot_y, min(plot_y + plot_h, zero_db_y))
            fill_v2 = []
            for i in range(N_PTS):
                fill_v2.append(curve_pts[i])
                fill_v2.append((curve_pts[i][0], y_zero))
            if len(fill_v2) >= 4:
                bfill = batch_for_shader(shader, "TRI_STRIP", {"pos": fill_v2})
                shader.bind()
                shader.uniform_float("color", (0.05, 0.35, 0.52, 0.18))
                bfill.draw(shader)

        # Per-band dim curves
        for bi in range(N_BANDS):
            gdb_b, fhz_b, q_b, _, _, _ = band_params[bi]
            if abs(gdb_b) < 0.5:
                continue
            bcol_b = EQ7_BANDS[bi][1]
            bftype_b = EQ7_BANDS[bi][2]
            bpts = []
            for pi in range(N_PTS):
                t   = pi / (N_PTS - 1)
                db  = _eq_biquad_response(fhz_b, gdb_b, bftype_b, q_b,
                                          _eq_freq_from_norm(t))
                db  = max(-db_range * 1.1, min(db_range * 1.1, db))
                bpx  = plot_x + t * plot_w
                bpyv = zero_db_y + db * px_per_db
                bpyv = max(plot_y + 1, min(plot_y + plot_h - 1, bpyv))
                bpts.append((bpx, bpyv))
            if len(bpts) >= 2:
                bb_b = batch_for_shader(shader, "LINE_STRIP", {"pos": bpts})
                gpu.state.line_width_set(max(1.0, scale))
                shader.bind()
                shader.uniform_float("color", (*bcol_b, 0.35))
                bb_b.draw(shader)
                gpu.state.line_width_set(1.0)

        # Combined curve — bright cyan normally; neon red with a glow halo
        # for packapunch, where cyan disappears against the blue vortex
        # screen behind it.
        if len(curve_pts) >= 2:
            bc = batch_for_shader(shader, "LINE_STRIP", {"pos": curve_pts})
            shader.bind()
            if _is_packapunch:
                if EQ_PACKAPUNCH_CURVE_GLOW:
                    # Wide, translucent, additive passes first — overlapping
                    # alpha brightens where the curve bends back on itself,
                    # the same core+halo idea as the mute/solo glow PNGs
                    # earlier, just drawn as line passes instead of a texture.
                    gpu.state.blend_set('ADDITIVE')
                    for halo_w, halo_a in ((10.0, 0.05), (6.0, 0.09), (3.5, 0.16)):
                        gpu.state.line_width_set(max(1.0, halo_w * scale))
                        shader.uniform_float("color", (*EQ_PACKAPUNCH_CURVE_COLOR, halo_a))
                        bc.draw(shader)
                    gpu.state.blend_set('ALPHA')
                gpu.state.line_width_set(max(2.0, scale * 2.0))
                shader.uniform_float("color", (*EQ_PACKAPUNCH_CURVE_COLOR, 0.95))
                bc.draw(shader)
            else:
                gpu.state.line_width_set(max(2.0, scale * 2.0))
                shader.uniform_float("color", (0.15, 0.75, 1.00, 0.95))
                bc.draw(shader)
            gpu.state.line_width_set(1.0)

        # Band handle dots — positioned on the combined curve at each band freq
        for bi in range(N_BANDS):
            gdb_b, fhz_b, q_b, gn_b, fn_b, qn_b = band_params[bi]
            bname_b, bcol_b = EQ7_BANDS[bi][0], EQ7_BANDS[bi][1]
            dot_x = plot_x + fn_b * plot_w
            f_here = _eq_freq_from_norm(fn_b)
            dot_db = sum(
                _eq_biquad_response(band_params[b][1], band_params[b][0],
                                    EQ7_BANDS[b][2], band_params[b][2], f_here)
                for b in range(N_BANDS)
            )
            dot_db = max(-db_range, min(db_range, dot_db))
            dot_y  = zero_db_y + dot_db * px_per_db
            dot_y  = max(plot_y + 5*scale, min(plot_y + plot_h - 5*scale, dot_y))
            dot_r  = max(6*scale, 7*scale)
            _draw_circle(dot_x, dot_y, dot_r + 2*scale, (*bcol_b, 0.20))
            _draw_circle(dot_x, dot_y, dot_r,           (*bcol_b, 1.00))
            _draw_circle(dot_x, dot_y, dot_r * 0.35,    (1.0, 1.0, 1.0, 0.80))
            fs_dot = max(1, int(8 * scale))
            tw_dot = _text_width(bname_b, fs_dot)
            _draw_text(bname_b, dot_x - tw_dot * 0.5,
                       dot_y - dot_r - 11 * scale, fs_dot, (*bcol_b, 0.9))

    except Exception:
        import traceback; traceback.print_exc()

    # ── PackAPunch "frame" layer — garbage-matte the live content.
    # Luke cut the screen's glass area out of the rack's own background art
    # (ui/assets/skins/packapunch/rack_eq_bg.png) so it's transparent there,
    # bezel/tentacle art everywhere else. Redrawing that SAME texture here,
    # on top of the vortex sequence + grid/spectrum/curves/dots, means the
    # true rounded-CRT screen shape (not a rectangular approximation) is
    # what clips the live content — any overflow just lands under opaque
    # bezel pixels and disappears, no extra per-element coordinate math.
    #
    # UV-sliced to the display sub-rect only (not the full rx/ry/rw/rh rack
    # rect the first draw used) so this second pass doesn't re-cover the
    # knob strip below, which the rest of this function still draws using
    # that first, full-rack copy of the same art.
    if _is_packapunch and _eq_tex:
        try:
            from ui.mixer.texture_cache import blit_texture as _blt_frame
            u0 = (disp_x - rx) / rw
            u1 = (disp_x + disp_w - rx) / rw
            v0 = (disp_y - ry) / rh
            v1 = (disp_y + disp_h - ry) / rh
            # Same texture as the first (full-rack) draw above, which used
            # the default ALPHA_PREMULT blend — skin PNGs are pre-multiplied,
            # so this second pass must match it rather than use "ALPHA"
            # (that's for non-premultiplied overlays like glass layers) or
            # the cutout's soft edge would blend with the wrong darkness.
            _blt_frame(_eq_tex, disp_x, disp_y, disp_w, disp_h,
                       key="rack_eq_bg_frame", blend="ALPHA_PREMULT",
                       uv=(u0, v0, u1, v1))
        except Exception:
            pass

    # Display border
    bv3 = [(disp_x, disp_y), (disp_x + disp_w, disp_y),
           (disp_x + disp_w, disp_y + disp_h),
           (disp_x, disp_y + disp_h), (disp_x, disp_y)]
    bb3 = batch_for_shader(shader, "LINE_STRIP", {"pos": bv3})
    shader.bind(); shader.uniform_float("color", (0.20, 0.20, 0.20, 1.0))
    bb3.draw(shader)

    # -----------------------------------------------------------------------
    # KNOB STRIP  — 7 equal columns: L | 1 | 2 | 3 | 4 | 5 | H
    # Each column (top to bottom): label, Gain knob, Freq knob, Q knob
    # -----------------------------------------------------------------------
    col_w    = disp_w / N_BANDS
    kr_gain  = min(max(16 * scale, col_w * 0.18), 26 * scale) * EQ_KNOB_SCALE
    kr_small = min(max(11 * scale, col_w * 0.13), 18 * scale) * EQ_KNOB_SCALE

    # Row layout: place rows evenly within knob_h, top-to-bottom:
    # label → gain knob → freq knob → Q knob
    # Divide available height across 4 rows with equal spacing.
    row_slot = knob_h / 4.0
    row_lbl  = knob_y + knob_h - row_slot * 0.28
    row_gain = knob_y + knob_h - row_slot * 1.2
    row_freq = knob_y + knob_h - row_slot * 2.3
    row_q    = knob_y + knob_h - row_slot * 3.35
    # Clamp knob radii so they fit within a slot
    kr_gain  = min(kr_gain,  row_slot * 0.42)
    kr_small = min(kr_small, row_slot * 0.32)

    # X offset shifts all 7 columns together
    _eq_x_shift = EQ_KNOB_X_OFFSET * scale

    fs_lbl = max(1, int(11 * scale))

    _pp_kg = eq_pp_knob_geom(rx, ry, rw, rh) if _is_packapunch else None
    _pp_ktex_cache = {}

    for bi in range(N_BANDS):
        gdb_b, fhz_b, q_b, gn_b, fn_b, qn_b = band_params[bi]
        bname_b, bcol_b, bftype_b = EQ7_BANDS[bi][0], EQ7_BANDS[bi][1], EQ7_BANDS[bi][2]
        col_cx = disp_x + (bi + 0.5) * col_w + _eq_x_shift

        # Band label (L / 1 / 2 ... H) — baked into the packapunch art, so only
        # drawn for the other skins.
        if not _is_packapunch:
            tw_l = _text_width(bname_b, fs_lbl)
            _draw_text(bname_b,
                       col_cx - tw_l * 0.5 + EQ_LABEL_X[bi] * scale,
                       row_lbl - fs_lbl    + EQ_LABEL_Y[bi] * scale,
                       fs_lbl, (*bcol_b, 1.0))

        if _pp_kg:
            # packApunch: the knob-face images, turned to the value, sitting on the baked knobs
            for _pi, _nv in ((bi, gn_b), (bi + 7, fn_b), (bi + 14, qn_b)):
                _g = _pp_kg.get(_pi)
                if not _g:
                    continue
                _tex = _pp_ktex_cache.get(_g[5])
                if _tex is None:
                    try:
                        from ui.mixer.texture_cache import get_texture as _gtc_ek
                        _tex = _gtc_ek(_g[5])
                    except Exception:
                        _tex = None
                    _pp_ktex_cache[_g[5]] = _tex
                _ang = math.radians((0.5 - max(0.0, min(1.0, _nv))) * EQ_PP_KNOB_SWEEP + EQ_PP_KNOB_OFFSET)
                if _tex:
                    from ui.mixer.texture_cache import blit_texture_rotated as _blt_rot_eq
                    _blt_rot_eq(_tex, _g[0], _g[1], _g[2], _g[3], _ang, blend="ALPHA_PREMULT", key=_g[5])
            continue                 # baked column dividers / labels / shelf plates: nothing else to draw

        # Gain knob
        gain_str = f"{gdb_b:+.1f}dB"
        _draw_knob(col_cx + EQ_GAIN_X[bi] * scale,
                   row_gain + EQ_GAIN_Y[bi] * scale,
                   kr_gain, gn_b, bcol_b, "Gain", gain_str, scale)

        # Freq knob
        freq_str = (f"{fhz_b/1000:.2f}k" if fhz_b >= 1000
                    else f"{fhz_b:.0f}Hz")
        _draw_knob(col_cx + EQ_FREQ_X[bi] * scale,
                   row_freq + EQ_FREQ_Y[bi] * scale,
                   kr_small, fn_b, bcol_b, "Freq", freq_str, scale)

        # Q knob (peaks only)
        if bftype_b == "peak":
            q_str = f"{q_b:.2f}"
            _draw_knob(col_cx + EQ_Q_X[bi] * scale,
                       row_q   + EQ_Q_Y[bi] * scale,
                       kr_small, qn_b, bcol_b, "Q", q_str, scale)
        else:
            pass  # shelf placeholder suppressed — baked into background PNG

        # Column divider
        if bi < N_BANDS - 1:
            div_xd = disp_x + (bi + 1) * col_w
            _draw_rect(div_xd - max(0.5, scale * 0.5),
                       knob_y, max(0.5, scale * 0.5), knob_h,
                       (0.16, 0.16, 0.16, 1.0))

