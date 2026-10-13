# =============================================================================
# rack_noisegate.py
# Noise gate rack UI
# ┌─ LAYOUT CONSTANTS ─────────────────────────────────────────────────────┐
# │ RACK_EXPANDED_H_NG — change height here
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
# NOISE GATE KNOB TUNING
# All values are unscaled px — multiplied by scale at draw time.
#
# NG_KNOB_SCALE : size multiplier for all 5 knobs (1.0 = no change)
# NG_KNOB_X     : per-knob X nudge [Threshold, Attack, Hold, Release, Range]
# NG_KNOB_Y     : per-knob Y nudge [Threshold, Attack, Hold, Release, Range]
# Positive X = right, Negative X = left.
# Positive Y = up,    Negative Y = down.
# =============================================================================
NG_KNOB_SCALE = 0.95
#                Threshold  Attack    Hold  Release   Range
NG_KNOB_X = [      14.25,    12.75,     11.5,    7.25,    -0.5]
NG_KNOB_Y = [      -12.0,    -12.0,     -12.0,    -12.0,    -12.0]

# Value text nudge — shifts all 5 value readouts independently of the knobs.
# Negative = move down, positive = move up.
NG_VALUE_Y_OFFSET = -10.0

# ---------------------------------------------------------------------------
# Module-level rolling RMS buffer — one per channel, updated every draw call.
# Gives a scrolling waveform without needing _fft_timeline_full (dead code).
# 80 samples = same window width as the old timeline approach.
# ---------------------------------------------------------------------------
import collections as _coll
# Ring buffer per channel — stores (frame, rms) pairs from live engine meter.
# Updated every draw call. Holds N_WIN samples = ~5s of history at 24fps.
_NG_RMS_HISTORY  = {}   # ch_idx -> deque of (frame, rms_value)
_NG_GATE_HISTORY = {}   # ch_idx -> dict {frame: bool} gate open state
_NG_HISTORY_LEN  = 120  # frames of history — matches N_WIN display window


# =============================================================================
# PACKAPUNCH SKIN
# With the packApunch skin the rack art has the screen cut out as transparent
# glass, so the screen is drawn BEHIND the art and the art frames it.
# Rects are in the skin's own pixels (top-left origin).
# =============================================================================
NG_PP_SKIN   = "packapunch"
NG_ART_W, NG_ART_H = 1864, 503
NG_PP_HOLE   = (67, 58, 1675, 296)            # the transparent screen (from the alpha matte)
NG_PP_INSET  = (8, 12, 8, 14)                 # keep drawing this far inside the hole: left, top, right, bottom
NG_PP_BACK   = (0.030, 0.008, 0.010, 1.0)     # what sits behind the glass

NG_PP_WAVE_CORE  = (1.00, 0.16, 0.12)         # waveform: red
NG_PP_WAVE_GLOW  = (1.00, 0.05, 0.03)         # ... and its glow
NG_PP_WAVE_W     = 1.6                        # waveform line width (unscaled px)
NG_PP_WAVE_FILL  = 0.20                       # translucent red fill inside the waveform
NG_PP_THR_CORE   = (1.00, 0.22, 0.16)         # dashed threshold line: red
NG_PP_THR_GLOW   = (1.00, 0.06, 0.03)         # ... and its glow
NG_PP_THR_W      = 1.8
NG_PP_GLOW_GAIN  = 1.0                        # glow strength for both (0 = no glow)
NG_PP_GATE_LINE  = (0.42, 0.025, 0.035, 1.0)  # gate open/close line: dark blood red
NG_PP_GATE_FILL  = (0.20, 0.010, 0.020, 0.55) # ... and the fill under it

# --- knobs: the skull knob image is turned to show the value (no sprite sheet) ---
NG_PP_KNOB_CX     = (256.0, 578.0, 899.0, 1217.0, 1528.0)   # knob centres in the skin (art px)
NG_PP_KNOB_CYS    = (389.0, 388.0, 388.0, 389.0, 389.0)     # per-knob centre y (art px)
NG_PP_KNOB_CY     = 389.0
NG_PP_KNOB_KEYS   = ("rack_noisegate_knob", "rack_noisegate_knob_2", "rack_noisegate_knob_3",
                     "rack_noisegate_knob_4", "rack_noisegate_knob_5")   # one skull image per knob (own perspective)
NG_PP_KNOB_R      = 34.5     # radius of the knob image (art px)
NG_PP_KNOB_SCALE  = 1.0      # draw the live knob slightly bigger (e.g. 1.05) if the baked knob peeks out
NG_PP_KNOB_HIT_R  = 44.0     # click radius (art px)
NG_PP_KNOB_SWEEP  = 270.0    # degrees of turn from minimum to maximum
NG_PP_KNOB_OFFSET = 0.0      # extra turn in degrees (+ = anticlockwise) if the skull should sit differently
NG_PP_VALUE_CY    = 463.0    # where the value readout sits, under each knob label (art px)
NG_PP_VALUE_PX    = 15.0     # value text height (art px)
NG_PP_VALUE_COL   = (1.00, 0.36, 0.30, 0.90)
NG_PP_VALUE_GLOW    = 0.9                      # glow strength of the knob value text (0 = none)

# ── packApunch: ambient "targeting" animation on the screen (purely decorative, ignores the audio) ──
NG_PP_TGT_ON        = True
NG_PP_TGT_CORE      = (1.00, 0.20, 0.15)       # the three dots / beams
NG_PP_TGT_GLOW      = (1.00, 0.05, 0.03)
NG_PP_TGT_EMITTER   = (0.50, 1.00)             # beam source: (x as a fraction of the screen width, height above the
                                               # screen's top edge as a multiple of its height) - converges up and off-screen
NG_PP_TGT_PERIOD    = 4.0                      # seconds per "glide to a new spot, then hold" step
NG_PP_TGT_MOVE      = (0.55, 0.10)             # share of the step spent gliding: 0.55 + up to 0.10 (so ~2.2-2.6 s gliding,
                                               # ~1.4-1.8 s holding still), varied per step
NG_PP_TGT_DOT_R     = 3.4                      # dot radius (unscaled px)
NG_PP_TGT_BEAM_W    = 1.3                      # beam width (unscaled px)
NG_PP_TGT_BEAM_A    = (0.28, 0.80)             # beam brightness while searching / locked
NG_PP_TGT_SIZE      = 0.13                     # size of the (rigid) triangle: distance from its centre to each dot, as a fraction of the screen height


def noisegate_pp_knob_geom(rack_x, rack_y, rw, rh):
    """Screen-space layout of the 5 packApunch knobs: list of
    (cx, cy, draw_radius, hit_radius, value_cx, value_cy, value_font_size),
    or None when the packApunch skin is not active."""
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas_kg
        if _gas_kg() != NG_PP_SKIN:
            return None
    except Exception:
        return None
    sx, sy = rw / NG_ART_W, rh / NG_ART_H
    sr = min(sx, sy)
    vy = rack_y + (1.0 - NG_PP_VALUE_CY / NG_ART_H) * rh
    fs = max(1, int(NG_PP_VALUE_PX * sy))
    return [(rack_x + ax * sx, rack_y + (1.0 - ay / NG_ART_H) * rh,
             NG_PP_KNOB_R * NG_PP_KNOB_SCALE * sr, NG_PP_KNOB_HIT_R * sr,
             rack_x + ax * sx, vy, fs) for ax, ay in zip(NG_PP_KNOB_CX, NG_PP_KNOB_CYS)]


def _ng_thick_segs(segs, width, color, additive=False):
    """Draw many separate 2-point segments as thick quads in ONE batch."""
    hw = width * 0.5
    verts = []
    for i in range(0, len(segs) - 1, 2):
        (x0, y0), (x1, y1) = segs[i], segs[i + 1]
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L * hw, dx / L * hw
        a, b = (x0 + nx, y0 + ny), (x0 - nx, y0 - ny)
        c, d = (x1 + nx, y1 + ny), (x1 - nx, y1 - ny)
        verts += [a, b, c, b, d, c]
    if not verts:
        return
    sh = _get_shader()
    gpu.state.blend_set("ADDITIVE" if additive else "ALPHA")
    sh.bind(); sh.uniform_float("color", color)
    batch_for_shader(sh, "TRIS", {"pos": verts}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _ng_thick(pts, width, color, additive=False):
    """One thick polyline as a single triangle strip (averaged normals)."""
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
    sh = _get_shader()
    gpu.state.blend_set("ADDITIVE" if additive else "ALPHA")
    sh.bind(); sh.uniform_float("color", color)
    batch_for_shader(sh, "TRI_STRIP", {"pos": verts}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _ng_glow_line(pts, width, core, glow):
    """A line with a soft glow around it."""
    g = NG_PP_GLOW_GAIN
    if g > 0:
        _ng_thick(pts, width * 5.0, (*glow, 0.07 * g), additive=True)
        _ng_thick(pts, width * 2.6, (*glow, 0.17 * g), additive=True)
    _ng_thick(pts, width, (*core, 0.95))


def _ng_hash(n, k=0):
    """Repeatable pseudo-random 0..1 from an integer."""
    v = math.sin(n * 12.9898 + k * 78.233) * 43758.5453
    return v - math.floor(v)


def _ng_smooth(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def _ng_disc(cx, cy, r, color, additive=False, seg=18):
    sh = _get_shader()
    verts = []
    for i in range(seg):
        a0 = 2 * math.pi * i / seg
        a1 = 2 * math.pi * (i + 1) / seg
        verts += [(cx, cy), (cx + r * math.cos(a0), cy + r * math.sin(a0)),
                  (cx + r * math.cos(a1), cy + r * math.sin(a1))]
    gpu.state.blend_set("ADDITIVE" if additive else "ALPHA")
    sh.bind(); sh.uniform_float("color", color)
    batch_for_shader(sh, "TRIS", {"pos": verts}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _ng_clip(p0, p1, x0, y0, x1, y1):
    """Liang-Barsky: the part of segment p0-p1 inside the rectangle, or None."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p0[0] - x0), (dx, x1 - p0[0]), (-dy, p0[1] - y0), (dy, y1 - p0[1])):
        if pp == 0:
            if qq < 0:
                return None
        else:
            r = qq / pp
            if pp < 0:
                if r > t1:
                    return None
                t0 = max(t0, r)
            else:
                if r < t0:
                    return None
                t1 = min(t1, r)
    return ((p0[0] + t0 * dx, p0[1] + t0 * dy), (p0[0] + t1 * dx, p0[1] + t1 * dy))


def _ng_targeting_state(t, w, h):
    """The three dots at time t (seconds): a rigid triangle that glides smoothly to a spot, eases to a
    stop, holds for a moment, then glides to the next spot. Returns (dots, hold, since_arrival):
    dots = [(x, y)] in pixels from the screen's bottom-left, hold = 0 (moving) .. 1 (held still),
    since_arrival = seconds since the last stop (or -1 while moving)."""
    def _fr(v):
        return v - math.floor(v)
    # well-spread spots (golden-ratio steps with a little randomness): never two close together
    wp = lambda i: (0.12 + 0.76 * _fr(0.37 + i * 0.6180339 + 0.12 * (_ng_hash(i, 1) - 0.5)),
                    0.30 + 0.40 * _fr(0.20 + i * 0.7548777 + 0.15 * (_ng_hash(i, 2) - 0.5)))
    P = NG_PP_TGT_PERIOD
    n = int(t // P)
    p = t - n * P
    move = P * (NG_PP_TGT_MOVE[0] + NG_PP_TGT_MOVE[1] * _ng_hash(n, 3))
    (ax_, ay_), (bx_, by_) = wp(n), wp(n + 1)
    if p < move:
        u = p / move
        f = u * u * u * (u * (u * 6.0 - 15.0) + 10.0)            # smootherstep: slow start, slow stop
        hold = _ng_smooth(min(u / 0.12, (1.0 - u) / 0.40))       # drops fast as it sets off, rises as it settles
        since = -1.0
    else:
        f, hold, since = 1.0, 1.0, p - move
    cx = (ax_ + (bx_ - ax_) * f) * w
    cy = (ay_ + (by_ - ay_) * f) * h
    R = NG_PP_TGT_SIZE * h
    dots = []
    for i in range(3):                                            # fixed shape: apex up, never changes
        ang = math.pi / 2 + i * 2 * math.pi / 3
        dots.append((cx + R * math.cos(ang), cy + R * math.sin(ang)))
    return dots, hold, since


def _ng_draw_targeting(x0, y0, w, h, level, ui):
    """Three red target dots with laser beams from above, drifting over the screen, periodically locking
    on into a tight triangle. Fixed loop, independent of audio. (x0, y0) = bottom-left of the screen."""
    if level <= 0.01 or w < 20 or h < 20:
        return
    import time as _t
    now = _t.monotonic()
    dots, L, since = _ng_targeting_state(now, w, h)
    X0, Y0, X1, Y1 = x0, y0, x0 + w, y0 + h
    core, glow = NG_PP_TGT_CORE, NG_PP_TGT_GLOW
    flick = 0.90 + 0.10 * math.sin(now * 23.0) * math.sin(now * 7.3)
    ba = NG_PP_TGT_BEAM_A[0] + (NG_PP_TGT_BEAM_A[1] - NG_PP_TGT_BEAM_A[0]) * L
    ex = x0 + w * (NG_PP_TGT_EMITTER[0] + 0.10 * math.sin(now * 0.31))
    ey = y0 + h * (1.0 + NG_PP_TGT_EMITTER[1])
    bw = max(1.0, NG_PP_TGT_BEAM_W * ui)
    g = NG_PP_GLOW_GAIN
    # beams: emitter (above the screen) -> each dot, clipped to the glass
    segs = []
    pulses = []
    for i, (px, py) in enumerate(dots):
        d = (x0 + px, y0 + py)
        seg = _ng_clip((ex, ey), d, X0, Y0, X1, Y1)
        if seg:
            segs += [seg[0], seg[1]]
            u = (now * 0.8 + i * 0.31) % 1.0                     # a bright pulse running down the beam
            q0 = (seg[0][0] + (seg[1][0] - seg[0][0]) * u, seg[0][1] + (seg[1][1] - seg[0][1]) * u)
            u2 = min(1.0, u + 0.10)
            q1 = (seg[0][0] + (seg[1][0] - seg[0][0]) * u2, seg[0][1] + (seg[1][1] - seg[0][1]) * u2)
            pulses += [q0, q1]
    a = ba * level * flick
    if segs:
        if g > 0:
            _ng_thick_segs(segs, bw * 4.5, (*glow, 0.05 * g * a / 0.5), additive=True)
            _ng_thick_segs(segs, bw * 2.4, (*glow, 0.12 * g * a / 0.5), additive=True)
        _ng_thick_segs(segs, bw, (*core, min(1.0, a)))
        _ng_thick_segs(pulses, bw * 1.6, (1.0, 0.55, 0.45, min(1.0, 0.55 * a)), additive=True)
    # faint triangle joining the dots once they lock
    if L > 0.02:
        tri = [(x0 + px, y0 + py) for px, py in dots]
        tri.append(tri[0])
        _ng_thick(tri, max(1.0, 0.8 * ui), (*core, 0.35 * L * level))
    # the dots
    r = NG_PP_TGT_DOT_R * ui * (1.0 + 0.25 * L)
    for i, (px, py) in enumerate(dots):
        cx, cy = x0 + px, y0 + py
        b = (0.55 + 0.45 * L) * (0.88 + 0.12 * math.sin(now * (5.0 + i) + i)) * level
        if g > 0:
            _ng_disc(cx, cy, r * 4.6, (*glow, 0.07 * g * b), additive=True)
            _ng_disc(cx, cy, r * 2.4, (*glow, 0.20 * g * b), additive=True)
        _ng_disc(cx, cy, r, (*core, min(1.0, 0.95 * b)))
        _ng_disc(cx, cy, r * 0.45, (1.0, 0.85, 0.80, min(1.0, 0.8 * b)))



def _draw_noisegate_body(rx, ry, rw, rh, rack, rack_idx, scale):
    """Noise gate display.

    Draws exactly the mockup design:
    - Grey waveform from _fft_timeline_full (real audio, mirrored top+bottom)
    - Green gate envelope line drawn from knob values (attack slope, hold flat,
      release slope) — triggered wherever waveform crosses threshold
    - Amber dashed threshold line
    - Colour-coded atk/hold/rel bracket annotations over first event
    - Gate open/closed readout top-right
    - Knob strip with colour-coded arcs matching bracket colours
    """
    # Lazy imports — avoids circular import at module load time
    import Racks as _racks_mod
    get_rack_channels = _racks_mod.get_rack_channels
    import math as _mg
    ui     = scale
    rail_h = RACK_RAIL_H * ui
    body_h = rh - rail_h
    # PackAPunch: the screen sits BEHIND the skin art (art has a transparent screen hole)
    _ng_pp = False
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas_ng
        _ng_pp = _gas_ng() == NG_PP_SKIN
    except Exception:
        pass

    _krx, _kry, _krw, _krh = rx, ry, rw, rh      # rx is reused further down, keep the rack rect
    def _ng_draw_skin(rx=rx, ry=ry, rw=rw, rh=rh, body_h=body_h):
        # ── Skin background — full rack height (rail/title now baked into PNG)
        try:
            from ui.mixer.texture_cache import get_texture as _gtc_ng
            from ui.mixer.texture_cache import blit_texture as _blt_ng
            _ng_tex = _gtc_ng("rack_noisegate_bg")
            if _ng_tex:
                _blt_ng(_ng_tex, rx, ry, rw, rh, key="rack_noisegate_bg")
            else:
                _draw_rect(rx, ry, rw, body_h, (0.07, 0.07, 0.07, 1.0))
        except Exception:
            _draw_rect(rx, ry, rw, body_h, (0.07, 0.07, 0.07, 1.0))

    if not _ng_pp:
        _ng_draw_skin()

    # Display area (left of channel buttons)
    disp_x = rx + 42 * ui
    disp_w = rw - 42 * ui - 108 * ui - 8 * ui
    disp_h = body_h * 0.56 - 4 * ui
    disp_y = ry + body_h - disp_h - 2 * ui
    knob_disp_x, knob_disp_w = disp_x, disp_w      # knob strip keeps its own layout
    if _ng_pp:
        _ax = lambda a: rx + a / NG_ART_W * rw
        _ay = lambda a: ry + (1.0 - a / NG_ART_H) * rh
        _hx0, _hy0, _hx1, _hy1 = NG_PP_HOLE
        # backing behind the glass (the art's hole shows it; the art covers the ragged edges)
        _draw_rect(_ax(_hx0 - 3), _ay(_hy1 + 3), _ax(_hx1 + 3) - _ax(_hx0 - 3),
                   _ay(_hy0 - 3) - _ay(_hy1 + 3), NG_PP_BACK)
        _il, _it, _ir, _ib = NG_PP_INSET
        disp_x = _ax(_hx0 + _il)
        disp_w = _ax(_hx1 - _ir) - disp_x
        disp_y = _ay(_hy1 - _ib)
        disp_h = _ay(_hy0 + _it) - disp_y

    # Knob area (above display)
    knob_h = body_h * 0.42 - 4 * ui
    knob_y = ry + 2 * ui

    # Gate state Y positions
    open_y   = disp_y + disp_h * 0.92   # gate open = high signal = near bottom of display
    closed_y = disp_y + disp_h * 0.10   # gate closed = muted = near top of display
    centre_y = disp_y + disp_h * 0.50   # waveform centre

    # Knob values
    thr_norm  = getattr(rack, 'p0', 0.50)
    atk_norm  = getattr(rack, 'p1', 0.05)
    hold_norm = getattr(rack, 'p2', 0.16)
    rel_norm  = getattr(rack, 'p3', 0.14)
    rng_norm  = getattr(rack, 'p4', 0.00)
    thr_db    = -60.0 + thr_norm * 60.0
    atk_ms    = 0.1   + atk_norm  * 99.9
    hold_ms   = hold_norm * 500.0
    rel_ms    = 10.0  + rel_norm  * 990.0
    rng_db    = -90.0 + rng_norm  * 90.0

    # Threshold in linear amplitude (for RMS comparison)
    thr_lin = 10.0 ** (thr_db / 20.0)

    shader = _get_shader()

    # ── Display background ────────────────────────────────────────────────────
    _draw_rect(disp_x, disp_y, disp_w, disp_h, NG_PP_BACK if _ng_pp else (0.035, 0.042, 0.050, 1.0))

    # ── Grey waveform from real audio ─────────────────────────────────────────
    rms_vals = None
    gate_events = []   # list of (close_x, attack_x, hold_x, release_x) in pixels
    gate_open_now = True
    gr_db_cur = 0.0

    # Pull live gate state from engine and store per-frame history
    # gr_levels[ch][1]=GR dB, [ch][2]=1.0 open / 0.0 closed
    try:
        import bpy as _bpy_ng
        from Loader import get_engine as _get_eng_ng
        _eng_ng = _get_eng_ng()
        if _eng_ng:
            _hj_ng = _eng_ng.get_engine()
            if _hj_ng:
                assigned_ng = get_rack_channels(rack)
                if assigned_ng:
                    _ch_ng  = list(assigned_ng)[0]
                    _st_ng  = _hj_ng.get_state()
                    _gr_ng  = _st_ng.get_gr_levels(_ch_ng)
                    if _gr_ng and len(_gr_ng) >= 3:
                        gr_db_cur     = float(_gr_ng[1])
                        gate_open_now = float(_gr_ng[2]) > 0.5
                        # Store current frame's gate state so the waveform
                        # display can draw a historically accurate envelope
                        _cur_f_ng = (_bpy_ng.context.scene.frame_current
                                     if _bpy_ng.context.scene else 0)
                        if _ch_ng not in _NG_GATE_HISTORY:
                            _NG_GATE_HISTORY[_ch_ng] = {}
                        _NG_GATE_HISTORY[_ch_ng][_cur_f_ng] = gate_open_now
                        # Trim old frames to avoid unbounded growth
                        if len(_NG_GATE_HISTORY[_ch_ng]) > 500:
                            oldest = min(_NG_GATE_HISTORY[_ch_ng])
                            del _NG_GATE_HISTORY[_ch_ng][oldest]
    except Exception:
        pass

    try:
        import bpy as _bpy
        assigned = get_rack_channels(rack)
        if not assigned:
            raise ValueError("no ch")
        ch = list(assigned)[0]

        scene = _bpy.context.scene
        if not scene:
            raise ValueError("no scene")

        fps   = scene.render.fps / scene.render.fps_base
        cur_f = scene.frame_current

        try:
            from Loader import get_engine as _get_eng_ng2
            _eng2 = _get_eng_ng2()
            if _eng2:
                _hj2 = _eng2.get_engine()
                if _hj2:
                    live_rms = float(_hj2.get_meter_rms(ch))
                    if ch not in _NG_RMS_HISTORY:
                        _NG_RMS_HISTORY[ch] = _coll.deque(maxlen=_NG_HISTORY_LEN)
                    _NG_RMS_HISTORY[ch].append((cur_f, live_rms))
        except Exception:
            pass

        # Build frame→rms lookup from ring buffer
        N_WIN  = 120
        half   = N_WIN // 2
        f_start = cur_f - half
        f_end   = f_start + N_WIN

        rms_by_frame = {}
        buf = _NG_RMS_HISTORY.get(ch)
        if buf:
            for frame_i, rms_i in buf:
                rms_by_frame[frame_i] = rms_i

        rms_vals      = [rms_by_frame.get(f_start + i, 0.0) for i in range(N_WIN)]
        playhead_slot = half

        # Threshold in same linear scale as RMS (0..1)
        thr_lin_disp = 10.0 ** (thr_db / 20.0)
        thr_lin      = thr_lin_disp

        rms_cur = rms_by_frame.get(cur_f, 0.0)
        if gr_db_cur == 0.0:
            gate_open_now = rms_cur >= thr_lin_disp

        # px widths based on fps
        ms_per_frame = 1000.0 / fps
        px_per_frame = disp_w / max(N_WIN - 1, 1)
        atk_px  = max(2.0, (atk_ms  / ms_per_frame) * px_per_frame)
        hold_px = max(2.0, (hold_ms / ms_per_frame) * px_per_frame)
        rel_px  = max(2.0, (rel_ms  / ms_per_frame) * px_per_frame)

    except Exception:
        rms_vals     = None
        thr_lin_disp = thr_lin
        atk_px  = max(2.0, disp_w * 0.025)
        hold_px = max(2.0, disp_w * 0.15)
        rel_px  = max(2.0, disp_w * 0.055)

    # Draw waveform if we have data
    if rms_vals:
        # Normalise to peak of visible window; floor at thr_lin so the
        # threshold line always has context even on quiet signals
        # Normalise to the peak of the visible window only.
        # Do NOT include thr_lin — threshold has nothing to do with waveform scale.
        # The waveform should always fill the same height regardless of threshold.
        max_rms = max(max(rms_vals), 0.001)
        half_h  = disp_h * 0.34
        wf_top  = []
        wf_bot  = []
        _N = N_WIN - 1  # spacing constant matches N_WIN
        for i, rms in enumerate(rms_vals):
            bx  = disp_x + (i / _N) * disp_w
            amp = (rms / max_rms) * half_h
            wf_top.append((bx, centre_y - amp))
            wf_bot.append((bx, centre_y + amp))

        # Fill between top and bottom
        fill_verts = []
        for (bx, ty), (_, by) in zip(wf_top, wf_bot):
            fill_verts += [(bx, ty), (bx, by)]
        if len(fill_verts) >= 4:
            bf = batch_for_shader(shader, "TRI_STRIP", {"pos": fill_verts})
            gpu.state.blend_set("ALPHA")
            shader.bind()
            shader.uniform_float("color", (*NG_PP_WAVE_GLOW, NG_PP_WAVE_FILL) if _ng_pp
                                 else (0.15, 0.16, 0.20, 0.85))
            bf.draw(shader)
        for pts in [wf_top, wf_bot]:
            if len(pts) >= 2:
                if _ng_pp:
                    _ng_glow_line(pts, max(1.5, NG_PP_WAVE_W * ui), NG_PP_WAVE_CORE, NG_PP_WAVE_GLOW)
                    continue
                bl = batch_for_shader(shader, "LINE_STRIP", {"pos": pts})
                gpu.state.line_width_set(max(1.0, ui * 0.8))
                shader.bind(); shader.uniform_float("color", (0.24, 0.26, 0.32, 0.70))
                bl.draw(shader)
        gpu.state.line_width_set(1.0)

    # ── Playhead cursor line ─────────────────────────────────────────────────
    # White vertical line showing current frame position in the waveform window
    if rms_vals:
        try:
            _ph_x = disp_x + (playhead_slot / max(N_WIN - 1, 1)) * disp_w
            _ph_verts = [(_ph_x, disp_y + 2*ui), (_ph_x, disp_y + disp_h - 2*ui)]
            _ph_batch = batch_for_shader(shader, "LINES", {"pos": _ph_verts})
            gpu.state.line_width_set(max(1.5, ui))
            shader.bind()
            shader.uniform_float("color", (0.85, 0.85, 0.90, 0.60))
            _ph_batch.draw(shader)
            gpu.state.line_width_set(1.0)
        except Exception:
            pass

    # ── Gate envelope line ────────────────────────────────────────────────────
    # Use recorded engine gate state per frame (from _NG_GATE_HISTORY) so the
    # envelope matches what the engine actually did — not a prediction from
    # comparing waveform to threshold (which has scale/calibration errors).
    rng_y = closed_y - rng_norm * (closed_y - open_y)

    gate_pts = []
    first_event = None

    if rms_vals:
        count    = len(rms_vals)
        px_per_i = disp_w / max(count - 1, 1)
        # f_start was set in the try block above; fall back if exception occurred
        try:
            _f0 = f_start
            _ch = ch
            gate_hist = _NG_GATE_HISTORY.get(_ch, {})
        except Exception:
            _f0 = 0; gate_hist = {}

        in_gate = False
        hold_remaining = 0.0

        for i in range(count):
            bx       = disp_x + i * px_per_i
            frame_no = _f0 + i
            # Use recorded engine state if available, else fall back to waveform
            if frame_no in gate_hist:
                is_above = gate_hist[frame_no]
            else:
                rms      = rms_vals[i]
                is_above = rms >= thr_lin

            if not in_gate and is_above:
                close_x = bx
                atk_end = min(bx + atk_px, disp_x + disp_w)
                gate_pts.append((bx, rng_y))
                gate_pts.append((atk_end, open_y))
                in_gate = True
                hold_remaining = hold_px
                if first_event is None:
                    first_event = (close_x, atk_end, None, None)
            elif in_gate and is_above:
                gate_pts.append((bx, open_y))
                hold_remaining -= px_per_i
                if first_event and first_event[2] is None:
                    first_event = (first_event[0], first_event[1], bx, None)
            elif in_gate and not is_above:
                if hold_remaining > 0:
                    gate_pts.append((bx, open_y))
                    hold_remaining -= px_per_i
                    if first_event and first_event[2] is None:
                        first_event = (first_event[0], first_event[1], bx, None)
                else:
                    rel_end = min(bx + rel_px, disp_x + disp_w)
                    gate_pts.append((bx, open_y))
                    gate_pts.append((rel_end, rng_y))
                    in_gate = False
                    if first_event and first_event[3] is None:
                        hx2 = first_event[2] if first_event[2] else bx
                        first_event = (first_event[0], first_event[1], hx2, rel_end)
            else:
                gate_pts.append((bx, rng_y))
    else:
        # No data — draw flat at open
        gate_pts = [(disp_x, open_y), (disp_x + disp_w, open_y)]

    # Fill under gate line
    if len(gate_pts) >= 2:
        fv2 = []
        for gx, gy in gate_pts:
            fv2 += [(gx, disp_y + disp_h - 2), (gx, gy)]
        if len(fv2) >= 4:
            bf2 = batch_for_shader(shader, "TRI_STRIP", {"pos": fv2})
            gpu.state.blend_set("ALPHA")
            shader.bind(); shader.uniform_float("color", NG_PP_GATE_FILL if _ng_pp
                                                else (0.04, 0.18, 0.07, 0.45))
            bf2.draw(shader)
        if _ng_pp:
            _ng_thick(gate_pts, max(2.0, ui * 2.0), NG_PP_GATE_LINE)
        else:
            bg = batch_for_shader(shader, "LINE_STRIP", {"pos": gate_pts})
            gpu.state.line_width_set(max(2.0, ui * 1.5))
            shader.bind(); shader.uniform_float("color", (0.20, 0.88, 0.38, 1.0))
            bg.draw(shader); gpu.state.line_width_set(1.0)

    # ── Amber dashed threshold line ───────────────────────────────────────────
    thr_y = rng_y - thr_norm * (rng_y - open_y)
    dash = 7*ui; gap = 4*ui; x = disp_x; tog = True; segs = []
    while x < disp_x + disp_w:
        xe = min(x + (dash if tog else gap), disp_x + disp_w)
        if tog: segs += [(x, thr_y), (xe, thr_y)]
        x = xe; tog = not tog
    if segs and _ng_pp:
        _tw = max(1.8, NG_PP_THR_W * ui)
        if NG_PP_GLOW_GAIN > 0:
            _ng_thick_segs(segs, _tw * 5.0, (*NG_PP_THR_GLOW, 0.07 * NG_PP_GLOW_GAIN), additive=True)
            _ng_thick_segs(segs, _tw * 2.6, (*NG_PP_THR_GLOW, 0.17 * NG_PP_GLOW_GAIN), additive=True)
        _ng_thick_segs(segs, _tw, (*NG_PP_THR_CORE, 0.95))
    elif segs:
        bd = batch_for_shader(shader, "LINES", {"pos": segs})
        gpu.state.line_width_set(max(1.8, ui * 1.2))
        shader.bind(); shader.uniform_float("color", (0.96, 0.62, 0.08, 1.0))
        bd.draw(shader); gpu.state.line_width_set(1.0)
    _draw_text(f"thr {thr_db:.0f}dB", disp_x + 4*ui, thr_y - 2*ui - 9*ui,
               max(1, int(8*ui)), (*NG_PP_THR_CORE, 1.0) if _ng_pp else (0.96, 0.62, 0.08, 1.0))

    # ── open/closed labels ────────────────────────────────────────────────────
    fs7 = max(1, int(7*ui))
    _draw_text("open",   disp_x + 4*ui, open_y + 2*ui,       fs7,
               (0.62, 0.10, 0.12, 0.80) if _ng_pp else (0.28, 0.55, 0.28, 0.70))
    _draw_text("closed", disp_x + 4*ui, closed_y - fs7 - 2*ui, fs7, (0.55, 0.28, 0.28, 0.70))

    # ── Bracket annotations (atk/hold/rel) on first gate event ───────────────
    ann_y = disp_y + 8*ui
    fs8   = max(1, int(8*ui))
    if first_event:
        cx, ax, hx, rx = first_event
        hx = hx or ax
        rx = rx or min(hx + rel_px, disp_x + disp_w)
        # attack bracket (blue)
        if ax > cx + 2:
            _draw_rect(cx, ann_y - 1, ax - cx, 1, (0.42, 0.55, 1.0, 0.9))
            _draw_rect(cx, ann_y - 3, 1, 5, (0.42, 0.55, 1.0, 0.9))
            _draw_rect(ax, ann_y - 3, 1, 5, (0.42, 0.55, 1.0, 0.9))
            _draw_text("atk", cx + 2, ann_y - fs8 - 3, fs8, (0.42, 0.55, 1.0, 1.0))
        # hold bracket (purple)
        if hx > ax + 2:
            _draw_rect(ax, ann_y - 1, hx - ax, 1, (0.65, 0.55, 0.98, 0.9))
            _draw_rect(ax, ann_y - 3, 1, 5, (0.65, 0.55, 0.98, 0.9))
            _draw_rect(hx, ann_y - 3, 1, 5, (0.65, 0.55, 0.98, 0.9))
            mid = ax + (hx - ax) / 2
            _draw_text("hold", mid - 12*ui, ann_y - fs8 - 3, fs8, (0.65, 0.55, 0.98, 1.0))
        # release bracket (orange)
        if rx > hx + 2:
            _draw_rect(hx, ann_y - 1, rx - hx, 1, (0.98, 0.45, 0.08, 0.9))
            _draw_rect(hx, ann_y - 3, 1, 5, (0.98, 0.45, 0.08, 0.9))
            _draw_rect(rx, ann_y - 3, 1, 5, (0.98, 0.45, 0.08, 0.9))
            _draw_text("rel", hx + 2, ann_y - fs8 - 3, fs8, (0.98, 0.45, 0.08, 1.0))

    # ── Gate open/closed readout ──────────────────────────────────────────────
    try:
        if _ng_pp:
            col = (0.85, 0.14, 0.14, 1.0) if gate_open_now else (0.50, 0.44, 0.42, 1.0)
        else:
            col = (0.20, 0.88, 0.32, 1.0) if gate_open_now else (0.88, 0.18, 0.18, 1.0)
        lbl = "gate open" if gate_open_now else "gate closed"
        fs9 = max(1, int(9*ui)); fs8b = max(1, int(8*ui)); pad = 4*ui
        tw  = _text_width(lbl, fs9)
        bw  = tw + pad*2; bh = fs9 + fs8b + pad*2 + 2*ui
        bx2 = disp_x + disp_w - bw - 4*ui
        by2 = disp_y + 4*ui
        if _ng_pp:
            bg_col = (0.20, 0.02, 0.03, 0.90) if gate_open_now else (0.07, 0.04, 0.04, 0.90)
        else:
            bg_col = (0.05, 0.18, 0.07, 1.0) if gate_open_now else (0.18, 0.05, 0.05, 1.0)
        _draw_rect(bx2, by2, bw, bh, bg_col)
        _draw_text(lbl, bx2 + pad, by2 + bh - fs9 - pad, fs9, col)
        _draw_text(f"GR  {gr_db_cur:.1f}dB", bx2 + pad, by2 + pad, fs8b,
                   (0.70, 0.58, 0.55, 1.0) if _ng_pp else (0.55, 0.65, 0.55, 1.0))
    except Exception:
        pass

    # PackAPunch: ambient red targeting dots + beams over the graph (decoration only, not tied to the audio)
    if _ng_pp and NG_PP_TGT_ON:
        try:
            from ui.racks.rack_base import _pp_preset_level as _ppl_ng
            import time as _tm_ng
            _lv_ng = _ppl_ng(rack_idx, rack.enabled, _tm_ng.monotonic())      # fades out when the rack is off
            _ng_draw_targeting(disp_x, disp_y, disp_w, disp_h, _lv_ng, ui)
        except Exception:
            pass

    # PackAPunch: the skin art goes over the screen (its transparent hole frames the graph)
    if _ng_pp:
        _ng_draw_skin()

    # ── Knob strip ────────────────────────────────────────────────────────────
    N  = 5
    cw = knob_disp_w / N
    rk = knob_y + knob_h * 0.68
    kr = min(max(13*ui, cw * 0.16), 20*ui) * NG_KNOB_SCALE
    kr = min(kr, knob_h * 0.42 * 0.42)

    knob_defs = [
        (thr_norm,  f"{thr_db:.0f}dB",  (0.88, 0.30, 0.30)),
        (atk_norm,  f"{atk_ms:.0f}ms",  (0.42, 0.55, 1.00)),
        (hold_norm, f"{hold_ms:.0f}ms", (0.65, 0.55, 0.98)),
        (rel_norm,  f"{rel_ms:.0f}ms",  (0.98, 0.45, 0.08)),
        (rng_norm,  f"{rng_db:.0f}dB",  (0.50, 0.50, 0.55)),
    ]
    _pp_kg = noisegate_pp_knob_geom(_krx, _kry, _krw, _krh) if _ng_pp else None
    _pp_ktex = None
    _pp_ktexs = []
    if _pp_kg:
        try:
            from ui.mixer.texture_cache import get_texture as _gtc_nk
            _pp_ktexs = [_gtc_nk(k) for k in NG_PP_KNOB_KEYS]
            _pp_ktex = _pp_ktexs[0]
            # fall back to knob 1's image for any knob whose own image is missing
            _pp_ktexs = [t or _pp_ktex for t in _pp_ktexs]
        except Exception:
            _pp_ktex = None
    for ki, (val, vstr, col_k) in enumerate(knob_defs):
        if _pp_kg and _pp_ktex:
            # packApunch: the skull knob image, turned to the value; value text under the baked label
            kcx, kcy, krad, _hit, vcx, vcy, vfs = _pp_kg[ki]
            ang = math.radians((0.5 - max(0.0, min(1.0, val))) * NG_PP_KNOB_SWEEP + NG_PP_KNOB_OFFSET)
            from ui.mixer.texture_cache import blit_texture_rotated as _blt_rot
            _blt_rot(_pp_ktexs[ki], kcx, kcy, krad * 2, krad * 2, ang, blend="ALPHA_PREMULT",
                     key=NG_PP_KNOB_KEYS[ki])
            try:
                from ui.racks.pp_text import (draw_text as _pdt, text_width as _ptw,
                                               font_id as _pfid, font_profile as _pprof)
                from ui.racks.rack_base import PACKAPUNCH_RACK_THEME as _ppt
                _fs = _ppt.get("NOISE_GATE", {}).get("font")                       # this rack's own font
                _vf, _vp = _pfid(_fs, "NOISE_GATE"), _pprof(_fs, "NOISE_GATE")
                _vsz = int(vfs * (_vp.get("value_scale", 1.0) if _vp else 1.0))
                vtw = _ptw(vstr, _vsz, _vf, _vp)
                _pdt(vstr, vcx - vtw / 2, vcy - _vsz / 2, _vsz, NG_PP_VALUE_COL,
                     glow=NG_PP_VALUE_GLOW, fid=_vf, prof=_vp)
            except Exception:
                vtw = _text_width(vstr, vfs)
                _draw_text(vstr, vcx - vtw / 2, vcy - vfs / 2, vfs, NG_PP_VALUE_COL)
            continue
        cx = knob_disp_x + (ki + 0.5) * cw + NG_KNOB_X[ki] * ui
        cy = rk                         + NG_KNOB_Y[ki] * ui
        _draw_knob(cx, cy, kr, val, col_k, "", vstr, ui,
                   value_y_offset=NG_VALUE_Y_OFFSET)  # label suppressed — baked into background PNG



