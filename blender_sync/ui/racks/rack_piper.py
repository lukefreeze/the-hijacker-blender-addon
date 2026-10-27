# =============================================================================
# ui/racks/rack_piper.py
# Piper TTS rack body — called by _draw_ai_rack_expanded in Racks.py.
#
# Layout (body only — rail drawn by Racks.py):
#
#  ┌─────────────────────────────────────────────────────────────────────┐
#  │ [TOP RAIL — collapse▲  badge#  PIPER TTS  ◄ VoiceName ►  ON/OFF X]│
#  ├──────────────────────────────────┬──────────────────────────────────┤
#  │  SCRIPT (text input area)        │  VOICE SELECTOR (card list)      │
#  │                                  │                                  │
#  │  [GENERATE]  [CLEAR]             │  en_US-lessac-medium  ← selected │
#  │  char count                      │  en_US-ryan-high                 │
#  ├──────────────────────────────────┴──────────────────────────────────┤
#  │  OUTPUT WAVEFORM (after generation)                                  │
#  ├─────────────────────────┬───────────────────────────────────────────┤
#  │  PLACE ON CHANNEL  CH1… │  SPEED  NOISE  NOISE_W knobs              │
#  ├─────────────────────────┴───────────────────────────────────────────┤
#  │  ENGINE: piper.exe  |  MODEL: voice  |  OFFLINE              ●      │
#  └─────────────────────────────────────────────────────────────────────┘
#
# rx, ry = bottom-left of FULL rack.  rw, rh = full dimensions.
# Body = ry → ry + rh - RACK_RAIL_H*scale
# =============================================================================

import os
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

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

# ---------------------------------------------------------------------------
# Colour palette — pink/magenta theme to distinguish from DNF red.
# Brightened (Sept 2026 pass) for a neon-glow look — Luke reported the
# original values read as dim/washed-out against the dark panels, especially
# _TEXT_DIM and _BORDER. Backgrounds (_BG/_PANEL) stay dark on purpose —
# that's what makes the text/borders pop — everything drawn ON TOP of them
# (text, box edges, selection states) got a real brightness bump.
# ---------------------------------------------------------------------------
_BG         = (0.05,  0.02,  0.04,  1.0)
_BORDER     = (0.42,  0.10,  0.24,  1.0)   # was (0.22,0.06,0.14) — box edges
_PANEL      = (0.03,  0.01,  0.02,  1.0)
_PANEL_SEL  = (0.16,  0.04,  0.10,  1.0)   # selected voice card bg — was (0.12,0.03,0.07)
_TEXT       = (1.00,  0.28,  0.60,  1.0)   # neon pink label — was (0.85,0.20,0.45)
_TEXT_DIM   = (0.62,  0.16,  0.32,  1.0)   # "dim" text, still readable — was (0.38,0.08,0.18)
_TEXT_VOICE = (0.95,  0.30,  0.55,  1.0)   # voice name text — was (0.70,0.16,0.35)
_ACCENT     = (1.00,  0.22,  0.50,  1.0)   # borders, highlights — was (0.75,0.12,0.32)
_WAVE_COL   = (1.00,  0.40,  0.75)          # (r,g,b) no alpha — was (0.80,0.25,0.55)
_GREEN      = (0.20,  0.95,  0.45,  1.0)   # was (0.05,0.80,0.30)
_GRID       = (0.30,  0.09,  0.18,  0.5)   # was (0.18,0.05,0.10,0.5)
_CH_NUM_OFF = (0.75,  0.30,  0.48,  1.0)   # channel number text, unassigned state — was (0.55,0.20,0.32)

RACK_RAIL_H = 32

# =============================================================================
# GENERATE / PREVIEW / CLEAR BUTTON TUNING
# All values are UNSCALED px, applied at draw time (multiplied by scale).
# Each button's final position/size is its own natural flow position (the
# same left-to-right layout as before, computed from sp_x/sp_w only — never
# from another button's tuned position) PLUS its own offset/scale below.
# That means nudging GENERATE never moves PREVIEW or CLEAR and vice versa —
# each of the three is fully independent.
# Mirrored in Racks.py's hit_test_ai_racks() Piper block, which imports
# these same constants — change here and clicks stay aligned with the art.
# =============================================================================
PIPER_GEN_BTN_X_OFFSET = 6.5   # GENERATE: left(-)/right(+)
PIPER_GEN_BTN_Y_OFFSET = 1.5    # GENERATE: down(-)/up(+)
PIPER_GEN_BTN_W_SCALE  = 0.96    # GENERATE width multiplier
PIPER_GEN_BTN_H_SCALE  = 1.18    # height multiplier — shared by all three boxes

PIPER_PV_BTN_X_OFFSET  = 6.5    # PREVIEW: left(-)/right(+)
PIPER_PV_BTN_Y_OFFSET  = 1.5    # PREVIEW: down(-)/up(+)
PIPER_PV_BTN_W_SCALE   = 1.125    # PREVIEW width multiplier

PIPER_CL_BTN_X_OFFSET  = 16    # CLEAR: left(-)/right(+)
PIPER_CL_BTN_Y_OFFSET  = 1.5    # CLEAR: down(-)/up(+)
PIPER_CL_BTN_W_SCALE   = 1.225    # CLEAR width multiplier

# =============================================================================
# VOICE PANEL POSITION / WIDTH TUNING
# X_OFFSET is unscaled px (+ = right/-  = left), applied on top of the panel's
# natural left edge (split_x). W_SCALE is a multiplier on the computed panel
# width — width still grows/shrinks from the right edge regardless of X_OFFSET.
# Mirrored in Racks.py's hit_test_ai_racks() Piper block (_vp_x_p) so the
# voice-card and scroll-arrow click zones stay aligned with the art.
# =============================================================================
PIPER_VOICE_PANEL_X_OFFSET = 5.0
PIPER_VOICE_PANEL_W_SCALE  = 0.98

# "+ ADD VOICE" / "← BACK TO VOICES" button — unscaled px, down(-)/up(+).
# It sits flush against the top of the voice panel at 0.0; there's a fair
# amount of dead space already reserved below it (before the scroll arrows/
# card list), so nudging this down a bit is safe without clipping anything.
# Mirrored in Racks.py's hit_test_ai_racks() Piper block (_addy_p) so clicks
# stay aligned with the art.
PIPER_ADD_VOICE_BTN_Y_OFFSET = -6.0

# ---------------------------------------------------------------------------
# Voice panel browse mode — per-rack UI state (not persisted; purely a draw
# concern, so it lives here rather than as a bpy.props field). "+ ADD VOICE"
# swaps the same card-list area over to Piper's public voice catalog
# (core/ai_piper.py: fetch_voice_catalog/download_voice) instead of the
# installed-voices list; "← BACK" swaps it back. See hit_test_ai_racks()'s
# PIPER_TTS block in Racks.py, which reads _browse_mode directly (same
# pattern ui.mixer.interaction uses for _active_text_field) so a click
# routed through there stays in sync with what's actually drawn here.
# ---------------------------------------------------------------------------
_browse_mode   = {}   # ai_idx -> bool
_browse_scroll = {}   # ai_idx -> int (separate from the installed-voice
                       # scroll in rack.p5, so switching modes doesn't
                       # jump either list to a confusing position)

# =============================================================================
# KNOB TUNING
# SPEED / NOISE / NOISE W labels are now baked into the background art, so
# the knob label text itself is suppressed below — only the value readout
# (e.g. "1.0×") still draws. Each knob's value has its own independent X/Y
# offset (unscaled px, + = right/up) since the three don't necessarily need
# identical nudges against the baked art.
# =============================================================================
PIPER_SPEED_VALUE_X_OFFSET  = -2.0
PIPER_SPEED_VALUE_Y_OFFSET  = -4.0
PIPER_NOISE_VALUE_X_OFFSET  = -2.0
PIPER_NOISE_VALUE_Y_OFFSET  = -4.0
PIPER_NOISEW_VALUE_X_OFFSET = -2.0
PIPER_NOISEW_VALUE_Y_OFFSET = -4.0

# =============================================================================
# OUTPUT WAVEFORM Y NUDGE
# Purely visual — offsets where the waveform panel is drawn without touching
# the layout math the script/voice panels above it are derived from.
# =============================================================================
PIPER_WAVE_Y_OFFSET = -15.0

# =============================================================================
# BOTTOM-LEFT "PLACE ON CHANNEL" NUMBER LABEL TUNING
# The channel buttons themselves are baked into the background art, so only
# the number text draws in code. Unscaled px, + = right/up. Numbers were
# sitting too high against the baked art, hence the negative Y default.
# =============================================================================
PIPER_CH_BTN_LABEL_X_OFFSET = 0.0
PIPER_CH_BTN_LABEL_Y_OFFSET = 3.0

# =============================================================================
# SCRIPT TEXT AREA TUNING
# Controls the typed script text itself — not the panel/box, which is baked
# into the skin art and untouched by these. FONT_SIZE is unscaled px (same
# baseline-before-scale convention as the rest of the file) for the typed
# lines. X_OFFSET shifts where each line starts (+ = right, - = left), and
# RIGHT_INSET pulls the wrap boundary in from the right edge — use either or
# both together to narrow the effective typing column from one or both
# sides without touching sp_x/sp_w, which GENERATE/PREVIEW/CLEAR and the
# voice panel are still measured from.
# =============================================================================
PIPER_SCRIPT_FONT_SIZE        = 9.0   # base px size of typed text (was fixed at 7)
PIPER_SCRIPT_TEXT_X_OFFSET    = 6.0   # shifts the typed text right(+)/left(-)
PIPER_SCRIPT_TEXT_RIGHT_INSET = 6.0   # pulls the wrap edge in from the right


# ---------------------------------------------------------------------------
# Click/drag → character index in the script text.
#
# Reuses the EXACT SAME word-wrap layout as the script panel draw code
# below (_draw_piper_body), so a click always lands on the character it
# visually appears next to. Shared by Racks.py's handle_ai_rack_click()
# (click-to-position-cursor) and ui.mixer.interaction's drag-select
# handling — both import this rather than keeping their own copy, so the
# two can never drift out of sync with what's drawn on screen.
#
# sp_x/sp_y/sp_w/sp_h are the SAME script-panel geometry _draw_piper_body
# uses (sp_x/sp_y/sp_w/sp_h there); mouse_x/mouse_y are region-space,
# scroll-adjusted coordinates — the same space rack_x/rack_y hit-testing
# already works in throughout Racks.py.
# ---------------------------------------------------------------------------
def cursor_index_from_xy(text, sp_x, sp_y, sp_w, sp_h, scale, mouse_x, mouse_y):
    if piper_pp_active():          # packApunch layout: sp_* is the script area's screen rect
        return _pp_cursor_index(text, sp_x, sp_y, sp_w, sp_h, mouse_x, mouse_y)
    fs_lbl  = max(1, int(8*scale))
    fs_txt  = max(1, int(PIPER_SCRIPT_FONT_SIZE*scale))
    line_h  = fs_txt * 1.7
    txt_x0  = sp_x + 6*scale + PIPER_SCRIPT_TEXT_X_OFFSET*scale
    txt_w   = sp_w - 8*scale - (PIPER_SCRIPT_TEXT_X_OFFSET + PIPER_SCRIPT_TEXT_RIGHT_INSET)*scale
    max_chars  = max(1, int(txt_w / max(1, fs_txt*0.62)))
    text_y     = sp_y + sp_h - fs_lbl - 8*scale - line_h
    lines_area = sp_h - fs_lbl - 8*scale - 28*scale
    max_lines  = max(1, int(lines_area / line_h))

    if not text:
        return 0

    # Word-wrap — identical algorithm to _draw_piper_body below.
    display_lines = []
    current = ""
    for word in text.split():
        test = (current + " " + word).strip() if current else word
        if len(test) <= max_chars:
            current = test
        else:
            if current:
                display_lines.append(current)
            current = word
    if current:
        display_lines.append(current)
    if not display_lines:
        return 0

    visible_lines = display_lines[-max_lines:]
    offset        = max(0, len(display_lines) - max_lines)

    # Cumulative char-start per display line — identical to _draw_piper_body.
    line_char_starts = []
    acc = 0
    for ln in display_lines:
        line_char_starts.append(acc)
        acc += len(ln) + 1

    # Closest visible line to mouse_y (same top-down layout as the draw loop).
    best_i, best_dist = 0, None
    for i in range(len(visible_lines)):
        ly = text_y - i * line_h
        if ly < sp_y + 28*scale:
            break
        dist = abs(mouse_y - ly)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_i    = i
    li_abs = min(best_i + offset, len(display_lines) - 1)
    line   = display_lines[li_abs]

    # Closest character boundary within that line to mouse_x.
    rel_x = mouse_x - txt_x0
    if rel_x <= 0:
        col = 0
    else:
        col = len(line)
        for c in range(len(line) + 1):
            w = _text_width(line[:c], fs_txt)
            if w >= rel_x:
                if c > 0:
                    w_prev = _text_width(line[:c-1], fs_txt)
                    col = c-1 if (rel_x - w_prev) < (w - rel_x) else c
                else:
                    col = 0
                break

    return line_char_starts[li_abs] + col


# ---------------------------------------------------------------------------
# Waveform panel (reused from deepfilternet style)
# ---------------------------------------------------------------------------
def _draw_wave(px, py, pw, ph, scale, data, rgb, label, placeholder=None, has_skin=False):
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    if not has_skin:
        _draw_rect(px, py, pw, ph, _PANEL)
        bv = [(px,py),(px+pw,py),(px+pw,py+ph),(px,py+ph),(px,py)]
        b  = batch_for_shader(sh, "LINE_STRIP", {"pos": bv})
        sh.bind(); sh.uniform_float("color", _BORDER); b.draw(sh)
    fs = max(1, int(8*scale))
    if label:
        _draw_text(label, px+5*scale, py+ph-fs-3*scale, fs, _TEXT_DIM)
    cy  = py + ph*0.5
    amp = ph*0.38
    if not has_skin:
        # Idle centre-line reference — only needed against the flat GPU
        # panel; the skin art already has its own baked-in graph baseline.
        _draw_line(px+2*scale, cy, px+pw-2*scale, cy, _GRID, max(0.5, scale*0.5))
    if data and len(data) > 1:
        n    = len(data)
        step = (pw - 4*scale) / max(1, n-1)
        ox   = px + 2*scale
        top  = [(ox+i*step, cy+data[i]*amp) for i in range(n)]
        bot  = [(ox+i*step, cy-data[i]*amp) for i in range(n)]
        bt = batch_for_shader(sh, "LINE_STRIP", {"pos": top})
        sh.bind(); sh.uniform_float("color", rgb+(0.88,)); bt.draw(sh)
        bb = batch_for_shader(sh, "LINE_STRIP", {"pos": bot})
        sh.bind(); sh.uniform_float("color", (rgb[0]*0.6, rgb[1]*0.6, rgb[2]*0.6, 0.55)); bb.draw(sh)
    else:
        if not has_skin:
            flat = (rgb[0]*0.25, rgb[1]*0.25, rgb[2]*0.25, 0.4)
            _draw_line(px+2*scale, cy, px+pw-2*scale, cy, flat, max(0.8, scale*0.8))
        if placeholder:
            fs_ph = max(1, int(7*scale))
            tw    = _text_width(placeholder, fs_ph)
            _draw_text(placeholder, px+pw/2-tw/2, cy-fs_ph/2, fs_ph, _TEXT_DIM)


# ---------------------------------------------------------------------------
# Main body draw
# ---------------------------------------------------------------------------
# ── Engine-missing setup screen (same pattern as the Whisper/Demucs racks) ────
_WARN_BG     = (0.06, 0.04, 0.00, 1.0)
_WARN_BORDER = (0.55, 0.33, 0.00, 1.0)
_WARN_TEXT   = (0.86, 0.53, 0.00, 1.0)
_WARN_DIM    = (0.50, 0.28, 0.00, 1.0)


def _fill(x, y, w, h, col):
    _draw_rect(x, y, w, h, col)


def _outline(x, y, w, h, col):
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    b = batch_for_shader(sh, "LINE_STRIP",
                         {"pos": [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]})
    sh.bind(); sh.uniform_float("color", col); b.draw(sh)


def _draw_engine_warning(rx, ry, rw, rh, scale):
    """Setup card shown while the Piper engine isn't installed. Returns the
    (x, y, w, h) of the INSTALL / CANCEL button for hit-testing."""
    from core import ai_piper as _ap
    mg = 8 * scale
    bw = rw - mg * 4
    bh = min(rh * 0.88, 190 * scale)
    bx = rx + (rw - bw) / 2
    by = ry + (rh - bh) / 2
    _fill(bx, by, bw, bh, _WARN_BG)
    _fill(bx, by, 4 * scale, bh, _WARN_TEXT)
    _outline(bx, by, bw, bh, _WARN_BORDER)

    tx   = bx + 14 * scale
    fs_h = max(1, int(9 * scale))
    fs_b = max(1, int(8 * scale))
    fs_s = max(1, int(7 * scale))
    lh   = fs_b + 5 * scale
    ty   = by + bh - fs_h - 8 * scale

    _draw_text("PIPER ENGINE NOT INSTALLED", tx, ty, fs_h, _WARN_TEXT)
    ty -= lh * 1.5
    _draw_text("Manual install - download the release for your system:", tx, ty, fs_b, _WARN_DIM)
    ty -= lh * 1.2
    cw = bw - 28 * scale; ch = lh * 1.6 + 6 * scale; cx2 = tx; cy2 = ty - ch
    _fill(cx2, cy2, cw, ch, (0.02, 0.01, 0.00, 1.0))
    _outline(cx2, cy2, cw, ch, (0.44, 0.24, 0.00, 1.0))
    _draw_text("github.com/rhasspy/piper/releases/tag/" + _ap._ENGINE_VERSION,
               cx2 + 6 * scale, cy2 + ch / 2 - fs_b / 2, fs_b, _WARN_TEXT)
    ty = cy2 - lh * 1.2
    sub = _ap.engine_target()[1]
    _draw_text("then unzip it into  Blender user scripts/hijacker_piper/" + sub + "/", tx, ty, fs_s, _WARN_DIM)
    ty -= lh
    _draw_text("Or press the button: it downloads Piper and a starter voice (~80 MB).",
               tx, ty, fs_s, _WARN_DIM)
    ty -= lh
    _draw_text("Works fully offline afterwards.", tx, ty, fs_s, _WARN_DIM)

    lines, btn_lbl, kind = _ap.get_engine_progress_display()

    dw = min(170 * scale, bw * 0.44); dh = max(16 * scale, fs_s + 8 * scale)
    dx2 = bx + bw - dw - 12 * scale; dy2 = by + 8 * scale

    if kind == "busy":
        import time as _t
        pct = max(0.0, min(100.0, _ap.get_engine_state()["pct"])) / 100.0
        bar_w = bw - 28 * scale; bar_h = max(3 * scale, 3)
        bar_y = dy2 + dh + 5 * scale
        _fill(tx, bar_y, bar_w, bar_h, (0.10, 0.07, 0.0, 1.0))
        _fill(tx, bar_y, bar_w * pct, bar_h, _WARN_TEXT)
        if lines:
            _draw_text(lines[0], tx, dy2 + dh / 2 - fs_s / 2, fs_s, _WARN_TEXT)
        d_bg, d_edge = (0.06, 0.04, 0.00, 1.0), _WARN_DIM
    elif kind == "error":
        if lines:
            _draw_text(lines[0][:60], tx, dy2 + dh / 2 - fs_s / 2, fs_s, (1.0, 0.45, 0.4, 1.0))
        d_bg, d_edge = (0.10, 0.02, 0.02, 1.0), (1.0, 0.35, 0.3, 1.0)
    else:
        d_bg, d_edge = (0.10, 0.06, 0.00, 1.0), _WARN_BORDER

    _fill(dx2, dy2, dw, dh, d_bg)
    _outline(dx2, dy2, dw, dh, d_edge)
    tw_d = _text_width(btn_lbl, fs_s)
    _draw_text(btn_lbl, dx2 + dw / 2 - tw_d / 2, dy2 + dh / 2 - fs_s / 2, fs_s, _WARN_TEXT)
    return dx2, dy2, dw, dh


def _start_piper_engine_install():
    """Button handler: install, or cancel if an install is already running."""
    from core import ai_piper as _ap
    if _ap.is_engine_installing():
        _ap.cancel_engine_install()
    else:
        _ap.start_engine_install()


def _draw_piper_body(rx, ry, rw, rh, rack, ai_idx, scale):
    """Draw the Piper TTS rack body below the rail."""

    status     = getattr(rack, 'ai_status', 'READY')
    script_txt = getattr(rack, 'ai_text', '') or ''

    # ── Geometry ────────────────────────────────────────────────────────────
    rail_h   = RACK_RAIL_H * scale
    body_bot = ry
    body_top = ry + rh - rail_h
    body_h   = body_top - body_bot

    # Full-rack photoreal skin — when present, Racks.py's _draw_ai_rack_expanded
    # has already blit the whole unit (rail + body) before calling this
    # function, so the flat panel fills below are skipped entirely and only
    # dynamic content (text, waveform, knob state, highlights) draws on top.
    # Falls back to the old flat panel look if the PNG isn't found.
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_pp
        _has_skin = _gtc_pp("rack_piper_bg") is not None
    except Exception:
        _has_skin = False

    # Engine not installed (the add-on ships without Piper): show the setup
    # card with the manual instructions + auto-install button instead of the UI.
    try:
        from core import ai_piper as _ap_chk
        _engine_ok = _ap_chk.piper_engine_present()
    except Exception:
        _engine_ok = True
    if not _engine_ok:
        _draw_rect(rx, body_bot, rw, body_h, _BG)
        bv0 = [(rx,body_bot),(rx+rw,body_bot),(rx+rw,body_top),(rx,body_top),(rx,body_bot)]
        sh0 = gpu.shader.from_builtin("UNIFORM_COLOR")
        b0  = batch_for_shader(sh0, "LINE_STRIP", {"pos": bv0})
        sh0.bind(); sh0.uniform_float("color", _BORDER); b0.draw(sh0)
        try:
            rack['piper_setup_btn'] = _draw_engine_warning(rx, body_bot, rw, body_h, scale)
        except Exception as _e_w:
            print(f"[PIPER] setup card error: {_e_w}")
        return

    sbar_h   = max(16*scale, body_h * 0.065)
    sbar_y   = body_bot

    # Bottom strip: channel placement + knobs
    ctrl_h   = body_h * 0.22
    ctrl_bot = sbar_y + sbar_h + 2*scale
    ctrl_top = ctrl_bot + ctrl_h

    # Wave strip
    wave_h   = body_h * 0.18
    wave_y   = ctrl_top + 2*scale
    wave_top = wave_y + wave_h

    # Upper zone: script | voices
    upper_h  = body_top - wave_top - 4*scale
    upper_y  = wave_top + 2*scale

    split_x  = rx + rw * 0.52   # script left | voices right

    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    if not _has_skin:
        # ── Background ───────────────────────────────────────────────────
        _draw_rect(rx, body_bot, rw, body_h, _BG)
        bv  = [(rx,body_bot),(rx+rw,body_bot),(rx+rw,body_top),(rx,body_top),(rx,body_bot)]
        b   = batch_for_shader(sh, "LINE_STRIP", {"pos": bv})
        sh.bind(); sh.uniform_float("color", _BORDER); b.draw(sh)

    margin = 8*scale

    # ── SCRIPT PANEL (left) ──────────────────────────────────────────────────
    sp_x = rx + margin
    sp_y = upper_y
    sp_w = split_x - rx - margin*2
    sp_h = upper_h

    # Check if this rack's text field is active
    import time as _time
    text_active = False
    cursor_pos  = len(script_txt)
    try:
        from ui.mixer.interaction import _active_text_field
        if _active_text_field and _active_text_field.get('ai_idx') == ai_idx:
            text_active = True
            cursor_pos  = _active_text_field.get('cursor', len(script_txt))
    except Exception:
        pass

    # Panel fill/border — flat-GPU fallback only. The focus glow (red/pink
    # border box on click) has been removed per request: the baked skin art
    # already reads clearly enough without it, so the border is now only
    # ever drawn for the no-skin fallback, never as an active-state cue.
    panel_bg = (0.06, 0.02, 0.04, 1.0) if text_active else _PANEL

    if not _has_skin:
        _draw_rect(sp_x, sp_y, sp_w, sp_h, panel_bg)
        bvs = [(sp_x,sp_y),(sp_x+sp_w,sp_y),(sp_x+sp_w,sp_y+sp_h),(sp_x,sp_y+sp_h),(sp_x,sp_y)]
        bbs = batch_for_shader(sh, "LINE_STRIP", {"pos": bvs})
        sh.bind()
        sh.uniform_float("color", _ACCENT if text_active else _BORDER)
        bbs.draw(sh)

    # Typed-text left start and wrap-width, independent of the panel's own
    # sp_x/sp_w (which the buttons/voice panel below are measured from) —
    # see SCRIPT TEXT AREA TUNING above.
    _txt_x0 = sp_x + 6*scale + PIPER_SCRIPT_TEXT_X_OFFSET*scale

    # "Click to type" hint when empty and inactive
    if not script_txt and not text_active:
        hint_fs = max(1, int(PIPER_SCRIPT_FONT_SIZE*scale))
        _draw_text("Click to type script…",
                   _txt_x0, sp_y+sp_h*0.52, hint_fs, _TEXT_DIM)

    # "SCRIPT" label removed — baked into the skin art now.
    fs_lbl = max(1, int(8*scale))

    # Draw text lines
    fs_txt    = max(1, int(PIPER_SCRIPT_FONT_SIZE*scale))
    line_h    = fs_txt * 1.7
    _txt_w    = sp_w - 8*scale - (PIPER_SCRIPT_TEXT_X_OFFSET + PIPER_SCRIPT_TEXT_RIGHT_INSET)*scale
    max_chars = max(1, int(_txt_w / max(1, fs_txt*0.62)))
    text_y    = sp_y + sp_h - fs_lbl - 8*scale - line_h
    lines_area = sp_h - fs_lbl - 8*scale - 28*scale   # leave room for buttons
    max_lines  = max(1, int(lines_area / line_h))

    # Wrap script text into display lines
    display_lines = []
    if script_txt:
        words = script_txt.split()
        current = ""
        for word in words:
            test = (current + " " + word).strip() if current else word
            if len(test) <= max_chars:
                current = test
            else:
                if current:
                    display_lines.append(current)
                current = word
        if current:
            display_lines.append(current)
    else:
        display_lines = []

    if display_lines:
        # Work out cursor line/col from cursor_pos
        char_count_so_far = 0
        cursor_line = len(display_lines) - 1
        cursor_col  = len(display_lines[-1]) if display_lines else 0
        if text_active:
            for li, ln in enumerate(display_lines):
                end = char_count_so_far + len(ln)
                if cursor_pos <= end:
                    cursor_line = li
                    cursor_col  = cursor_pos - char_count_so_far
                    break
                char_count_so_far = end + 1   # +1 for space between words

        visible_lines = display_lines[-max_lines:]
        offset        = max(0, len(display_lines) - max_lines)

        # Get selection range for highlight
        sel_start_g = -1
        sel_end_g   = -1
        if text_active:
            try:
                from ui.mixer.interaction import _active_text_field as _atf
                if _atf and _atf.get('ai_idx') == ai_idx:
                    ss = _atf.get('sel_start', -1)
                    se = _atf.get('sel_end',   -1)
                    if ss >= 0 and se >= 0 and ss != se:
                        sel_start_g = min(ss, se)
                        sel_end_g   = max(ss, se)
            except Exception:
                pass

        # Build cumulative char positions per display line
        line_char_starts = []
        acc = 0
        for ln in display_lines:
            line_char_starts.append(acc)
            acc += len(ln) + 1  # +1 for space between words

        for i, line in enumerate(visible_lines):
            ly     = text_y - i * line_h
            li_abs = i + offset
            if ly < sp_y + 28*scale:
                break

            # Selection highlight
            if sel_start_g >= 0 and li_abs < len(line_char_starts):
                ls = line_char_starts[li_abs]
                le = ls + len(line)
                ov_s = max(sel_start_g, ls) - ls
                ov_e = min(sel_end_g,   le) - ls
                if ov_s < ov_e:
                    pre_w  = _text_width(line[:ov_s], fs_txt)
                    sel_w  = _text_width(line[ov_s:ov_e], fs_txt)
                    sx     = _txt_x0 + pre_w
                    _draw_rect(sx, ly - 1*scale, max(sel_w, 2*scale),
                               fs_txt + 2*scale, (0.50, 0.10, 0.25, 0.45))

            _draw_text(line, _txt_x0, ly, fs_txt, _TEXT)

            # Cursor blink on the active line
            if text_active and li_abs == cursor_line:
                blink = int(_time.time() * 2) % 2 == 0
                if blink:
                    pre   = line[:cursor_col]
                    cur_x = _txt_x0 + _text_width(pre, fs_txt)
                    _draw_line(cur_x, ly - 1*scale, cur_x, ly + fs_txt + 1*scale,
                               _TEXT, max(1.0, scale))

    # Char counter
    char_count = len(script_txt)
    cc_fs = max(1, int(6*scale))
    cc_txt = f"{char_count} / 4096"
    cc_tw  = _text_width(cc_txt, cc_fs)
    _draw_text(cc_txt, sp_x+sp_w-cc_tw-4*scale, sp_y+3*scale, cc_fs, _TEXT_DIM)

    # Natural flow base positions — the same left-to-right layout as before,
    # computed only from sp_x/sp_w/sp_y (never from another button's tuned
    # position), so each button's own offset/scale below is fully independent.
    _gen_w_base = min(80*scale, sp_w*0.48)
    _pv_w_base  = min(60*scale, sp_w*0.36)
    _cl_w_base  = min(38*scale, sp_w*0.22)
    _gen_x_base = sp_x + 4*scale
    _pv_x_base  = _gen_x_base + _gen_w_base + 4*scale
    _cl_x_base  = _pv_x_base + _pv_w_base + 4*scale
    _row_y_base = sp_y + 4*scale

    # GENERATE button
    gen_w = _gen_w_base * PIPER_GEN_BTN_W_SCALE
    gen_h = max(16*scale, 20*scale) * PIPER_GEN_BTN_H_SCALE
    gen_x = _gen_x_base + PIPER_GEN_BTN_X_OFFSET*scale
    gen_y = _row_y_base + PIPER_GEN_BTN_Y_OFFSET*scale

    if status == "PROCESSING":
        g_bg  = (0.16, 0.04, 0.08, 1.0)
        g_col = (1.00, 0.35, 0.65, 1.0)   # was (0.80,0.25,0.50) — brighter neon pink
        g_lbl = "GENERATING…"
    elif status == "ERROR":
        # Previously ERROR rendered identically to READY — a failed click
        # (no script text, missing voice models, piper.exe not found, a
        # subprocess crash) was completely invisible in the UI, so a click
        # that silently failed looked exactly like a click that did nothing.
        # See rack.ai_error_msg (set alongside ai_status in core/ai_piper.py)
        # for the actual reason, also always printed to the console.
        g_bg  = (0.22, 0.03, 0.03, 1.0)
        g_col = (1.00, 0.25, 0.20, 1.0)
        g_lbl = "⚠ ERROR — RETRY"
    else:
        g_bg  = (0.14, 0.03, 0.07, 1.0)
        g_col = _TEXT
        g_lbl = "GENERATE"

    _draw_rect(gen_x, gen_y, gen_w, gen_h, g_bg)
    gv = [(gen_x,gen_y),(gen_x+gen_w,gen_y),(gen_x+gen_w,gen_y+gen_h),
          (gen_x,gen_y+gen_h),(gen_x,gen_y)]
    gb = batch_for_shader(sh, "LINE_STRIP", {"pos": gv})
    sh.bind(); sh.uniform_float("color", g_col); gb.draw(sh)
    fs_g  = max(1, int(8*scale))
    tw_g  = _text_width(g_lbl, fs_g)
    _draw_text(g_lbl, gen_x+gen_w/2-tw_g/2, gen_y+gen_h/2-fs_g/2, fs_g, g_col)

    # PREVIEW button (independent — see natural flow base positions above)
    pv_w = _pv_w_base * PIPER_PV_BTN_W_SCALE
    pv_x = _pv_x_base + PIPER_PV_BTN_X_OFFSET*scale
    pv_y = _row_y_base + PIPER_PV_BTN_Y_OFFSET*scale
    if status == "PREVIEWING":
        pv_bg  = (0.12, 0.04, 0.08, 1.0)
        pv_col = (1.00, 0.35, 0.65, 1.0)   # was (0.80,0.25,0.50)
        pv_lbl = "▶ …"
    elif status == "ERROR":
        pv_bg  = (0.18, 0.03, 0.03, 1.0)
        pv_col = (1.00, 0.25, 0.20, 1.0)
        pv_lbl = "⚠ ERROR"
    else:
        pv_bg  = (0.08, 0.02, 0.06, 1.0)
        pv_col = (0.85, 0.28, 0.50, 1.0)   # was (0.60,0.15,0.35) — was too dim to read
        pv_lbl = "▶ PREVIEW"
    _draw_rect(pv_x, pv_y, pv_w, gen_h, pv_bg)
    pvvs = [(pv_x,pv_y),(pv_x+pv_w,pv_y),(pv_x+pv_w,pv_y+gen_h),
            (pv_x,pv_y+gen_h),(pv_x,pv_y)]
    pvb = batch_for_shader(sh, "LINE_STRIP", {"pos": pvvs})
    sh.bind(); sh.uniform_float("color", pv_col); pvb.draw(sh)
    fs_pv = max(1, int(7*scale))
    tw_pv = _text_width(pv_lbl, fs_pv)
    _draw_text(pv_lbl, pv_x+pv_w/2-tw_pv/2, pv_y+gen_h/2-fs_pv/2, fs_pv, pv_col)

    # CLEAR button (independent — see natural flow base positions above)
    cl_w = _cl_w_base * PIPER_CL_BTN_W_SCALE
    cl_x = _cl_x_base + PIPER_CL_BTN_X_OFFSET*scale
    cl_y = _row_y_base + PIPER_CL_BTN_Y_OFFSET*scale
    _draw_rect(cl_x, cl_y, cl_w, gen_h, (0.08, 0.02, 0.04, 1.0))
    cv = [(cl_x,cl_y),(cl_x+cl_w,cl_y),(cl_x+cl_w,cl_y+gen_h),
          (cl_x,cl_y+gen_h),(cl_x,cl_y)]
    cb = batch_for_shader(sh, "LINE_STRIP", {"pos": cv})
    sh.bind(); sh.uniform_float("color", _TEXT_DIM); cb.draw(sh)
    fs_cl = max(1, int(7*scale))
    tw_cl = _text_width("CLEAR", fs_cl)
    _draw_text("CLEAR", cl_x+cl_w/2-tw_cl/2, cl_y+gen_h/2-fs_cl/2, fs_cl, _TEXT_DIM)

    # ── VOICE SELECTOR (right) ───────────────────────────────────────────────
    try:
        from core.ai_piper import get_voices
        voices = get_voices()
    except Exception:
        voices = []

    vp_x = split_x + margin*0.5 + PIPER_VOICE_PANEL_X_OFFSET*scale
    vp_y = upper_y
    vp_w = (rx + rw - split_x - margin*1.5) * PIPER_VOICE_PANEL_W_SCALE
    vp_h = upper_h

    if not _has_skin:
        _draw_rect(vp_x, vp_y, vp_w, vp_h, _PANEL)
        bvv = [(vp_x,vp_y),(vp_x+vp_w,vp_y),(vp_x+vp_w,vp_y+vp_h),(vp_x,vp_y+vp_h),(vp_x,vp_y)]
        bvb = batch_for_shader(sh, "LINE_STRIP", {"pos": bvv})
        sh.bind(); sh.uniform_float("color", _BORDER); bvb.draw(sh)

    # "VOICE" label removed — baked into the skin art now.

    # Voice index stored in p4 (float), separate from preset_idx (knob preset)
    selected_idx  = int(getattr(rack, 'p4', 0.0)) % max(1, len(voices)) if voices else 0
    voice_scroll  = int(getattr(rack, 'p5', 0.0))   # scroll offset stored in p5
    card_h   = max(22*scale, vp_h * 0.20)
    card_gap = 2*scale
    fs_vn    = max(1, int(8*scale))
    fs_vs    = max(1, int(7*scale))
    no_fs    = max(1, int(7*scale))

    browsing = _browse_mode.get(ai_idx, False)

    # ── "+ ADD VOICE" / "← BACK TO VOICES" button — always the topmost
    # element of this panel, in both modes. Click routed via Racks.py's
    # 'ai_piper_add_voice_toggle' zone, which flips _browse_mode and (when
    # entering browse mode) kicks off fetch_voice_catalog().
    add_btn_h   = 14*scale
    add_btn_gap = 3*scale
    add_btn_x   = vp_x + 4*scale
    add_btn_w   = vp_w - 8*scale
    add_btn_y   = vp_y + vp_h - add_btn_h + PIPER_ADD_VOICE_BTN_Y_OFFSET*scale
    btn_label   = "← BACK TO VOICES" if browsing else "+ ADD VOICE"
    _draw_rect(add_btn_x, add_btn_y, add_btn_w, add_btn_h, (0.10, 0.03, 0.06, 1.0))
    _abv = [(add_btn_x,add_btn_y),(add_btn_x+add_btn_w,add_btn_y),
            (add_btn_x+add_btn_w,add_btn_y+add_btn_h),
            (add_btn_x,add_btn_y+add_btn_h),(add_btn_x,add_btn_y)]
    _abb = batch_for_shader(sh, "LINE_STRIP", {"pos": _abv})
    sh.bind(); sh.uniform_float("color", _ACCENT); _abb.draw(sh)
    fs_ab = max(1, int(7*scale))
    tw_ab = _text_width(btn_label, fs_ab)
    _draw_text(btn_label, add_btn_x+add_btn_w/2-tw_ab/2,
               add_btn_y+add_btn_h/2-fs_ab/2, fs_ab, _ACCENT)

    # How many cards fit vertically (reserve space for the add-voice button,
    # label, and scroll arrows) — shared by both modes below.
    arrow_h      = 14*scale
    _top_reserve = add_btn_h + add_btn_gap + fs_lbl + 8*scale
    cards_area   = vp_h - _top_reserve - arrow_h*2 - 4*scale
    max_visible  = max(1, int(cards_area / (card_h + card_gap)))
    arr_top_y    = vp_y + vp_h - _top_reserve - arrow_h

    if browsing:
        # ── CATALOG BROWSE MODE ─────────────────────────────────────────
        try:
            from core.ai_piper import (get_voice_catalog, get_catalog_status,
                                        is_voice_installed, get_download_state)
            catalog            = get_voice_catalog()
            cat_status, cat_err = get_catalog_status()
        except Exception as _ce:
            catalog, cat_status, cat_err = [], "ERROR", str(_ce)

        if cat_status == "LOADING":
            _draw_text("Loading voice catalog…", vp_x+6*scale, vp_y+vp_h*0.5, no_fs, _TEXT_DIM)
        elif cat_status == "BLOCKED":
            _draw_text("Online access is disabled.", vp_x+6*scale, vp_y+vp_h*0.58,
                       no_fs, (1.0, 0.35, 0.3, 1.0))
            _draw_text("Enable it in Preferences >", vp_x+6*scale, vp_y+vp_h*0.48, no_fs, _TEXT_DIM)
            _draw_text("Get Extensions > Allow Online Access.",
                       vp_x+6*scale, vp_y+vp_h*0.40, no_fs, _TEXT_DIM)
        elif cat_status == "ERROR":
            _draw_text("Could not load catalog:", vp_x+6*scale, vp_y+vp_h*0.55,
                       no_fs, (1.0, 0.35, 0.3, 1.0))
            _draw_text((cat_err or "unknown error")[:44], vp_x+6*scale, vp_y+vp_h*0.45,
                       no_fs, _TEXT_DIM)
        elif not catalog:
            _draw_text("Catalog is empty.", vp_x+6*scale, vp_y+vp_h*0.5, no_fs, _TEXT_DIM)
        else:
            n_cat   = len(catalog)
            b_scroll = _browse_scroll.get(ai_idx, 0)
            b_scroll = max(0, min(b_scroll, max(0, n_cat - max_visible)))
            _browse_scroll[ai_idx] = b_scroll

            # ▲ up arrow
            sh.bind(); sh.uniform_float("color", _TEXT_DIM if b_scroll > 0 else _BORDER)
            b_arr_up = [(vp_x + vp_w/2, arr_top_y+arrow_h-2*scale),
                        (vp_x + vp_w/2 - 8*scale, arr_top_y+2*scale),
                        (vp_x + vp_w/2 + 8*scale, arr_top_y+2*scale)]
            batch_for_shader(sh, "TRIS", {"pos": b_arr_up}).draw(sh)

            v_start_y = arr_top_y - card_gap
            for slot in range(max_visible):
                vi = b_scroll + slot
                if vi >= n_cat:
                    break
                entry   = catalog[vi]
                cy_card = v_start_y - slot*(card_h+card_gap) - card_h
                if cy_card < vp_y + arrow_h + 2*scale:
                    break

                try:
                    installed = is_voice_installed(entry['key'])
                    dl_state  = get_download_state(entry['key'])
                except Exception:
                    installed, dl_state = False, None

                downloading = bool(dl_state and dl_state.get('status') == 'DOWNLOADING')
                is_error    = bool(dl_state and dl_state.get('status') == 'ERROR')

                if installed:
                    bg, bc, tc, status_txt = _PANEL_SEL, _GREEN, _GREEN, "✓ INSTALLED"
                elif downloading:
                    bg, bc, tc = _PANEL, _ACCENT, _ACCENT
                    status_txt = f"DOWNLOADING {dl_state.get('pct', 0):.0f}%"
                elif is_error:
                    bg, bc, tc = _PANEL, (1.0, 0.25, 0.2, 1.0), (1.0, 0.4, 0.35, 1.0)
                    status_txt = "ERROR"
                else:
                    bg, bc, tc = _PANEL, _BORDER, _TEXT_VOICE
                    status_txt = ""

                _draw_rect(vp_x+4*scale, cy_card, vp_w-8*scale, card_h, bg)
                cvb = [(vp_x+4*scale,cy_card),(vp_x+vp_w-4*scale,cy_card),
                       (vp_x+vp_w-4*scale,cy_card+card_h),
                       (vp_x+4*scale,cy_card+card_h),(vp_x+4*scale,cy_card)]
                cbb = batch_for_shader(sh, "LINE_STRIP", {"pos": cvb})
                sh.bind(); sh.uniform_float("color", bc); cbb.draw(sh)

                # A dedicated DOWNLOAD/RETRY button — NOT the whole card —
                # is the only clickable target for starting a download.
                # Luke flagged that "click anywhere on the card" made it too
                # easy to accidentally start a download you didn't want and
                # eat disk space; an explicit button fixes that. Installed/
                # downloading entries get no button (nothing to click).
                # Racks.py's hit_test_ai_racks() mirrors this exact rect.
                show_dl_btn = not installed and not downloading
                if show_dl_btn:
                    dl_btn_w = min(50*scale, vp_w*0.34)
                    dl_btn_h = min(card_h - 6*scale, 14*scale)
                    dl_btn_x = vp_x + vp_w - 4*scale - dl_btn_w
                    dl_btn_y = cy_card + (card_h - dl_btn_h) / 2
                    dl_label = "RETRY" if is_error else "DOWNLOAD"
                    dl_bg    = (0.22, 0.03, 0.03, 1.0) if is_error else (0.04, 0.16, 0.09, 1.0)
                    dl_edge  = (1.0, 0.35, 0.3, 1.0)    if is_error else _GREEN
                    _draw_rect(dl_btn_x, dl_btn_y, dl_btn_w, dl_btn_h, dl_bg)
                    _dlv = [(dl_btn_x,dl_btn_y),(dl_btn_x+dl_btn_w,dl_btn_y),
                            (dl_btn_x+dl_btn_w,dl_btn_y+dl_btn_h),
                            (dl_btn_x,dl_btn_y+dl_btn_h),(dl_btn_x,dl_btn_y)]
                    _dlb = batch_for_shader(sh, "LINE_STRIP", {"pos": _dlv})
                    sh.bind(); sh.uniform_float("color", dl_edge); _dlb.draw(sh)
                    fs_dl = max(1, int(6.5*scale))
                    tw_dl = _text_width(dl_label, fs_dl)
                    _draw_text(dl_label, dl_btn_x+dl_btn_w/2-tw_dl/2,
                               dl_btn_y+dl_btn_h/2-fs_dl/2, fs_dl, dl_edge)
                    name_w_max = dl_btn_x - (vp_x+8*scale) - 4*scale
                else:
                    name_w_max = vp_w - 16*scale

                name_line = f"{entry.get('lang_name','')} — {entry.get('name','')} ({entry.get('quality','')})"
                if show_dl_btn:
                    # Keep the name from running under the DOWNLOAD/RETRY
                    # button — trim to fit the space actually left for it.
                    while len(name_line) > 4 and _text_width(name_line, fs_vn) > name_w_max:
                        name_line = name_line[:-1]
                    if name_line != f"{entry.get('lang_name','')} — {entry.get('name','')} ({entry.get('quality','')})":
                        name_line = name_line[:-1] + "…"
                _draw_text(name_line, vp_x+8*scale, cy_card+card_h-fs_vn-3*scale, fs_vn, tc)
                size_mb  = (entry.get('onnx_size', 0) or 0) / (1024*1024)
                sub_line = f"{size_mb:.0f} MB" + (f"   {status_txt}" if status_txt else "")
                _draw_text(sub_line, vp_x+8*scale, cy_card+3*scale, fs_vs, _TEXT_DIM)

            # ▼ down arrow
            arr_bot_y = vp_y + 2*scale
            can_scroll_down = (b_scroll + max_visible) < n_cat
            sh.bind(); sh.uniform_float("color", _TEXT_DIM if can_scroll_down else _BORDER)
            b_arr_dn = [(vp_x + vp_w/2, arr_bot_y+2*scale),
                        (vp_x + vp_w/2 - 8*scale, arr_bot_y+arrow_h-2*scale),
                        (vp_x + vp_w/2 + 8*scale, arr_bot_y+arrow_h-2*scale)]
            batch_for_shader(sh, "TRIS", {"pos": b_arr_dn}).draw(sh)

            if n_cat > max_visible:
                pg_fs  = max(1, int(6*scale))
                pg_txt = f"{b_scroll+1}-{min(b_scroll+max_visible, n_cat)} / {n_cat}"
                pg_tw  = _text_width(pg_txt, pg_fs)
                _draw_text(pg_txt, vp_x+vp_w/2-pg_tw/2, arr_bot_y+arrow_h+1*scale,
                           pg_fs, _TEXT_DIM)

    elif voices:
        # ── INSTALLED VOICES (unchanged from before, just shifted down to
        # make room for the add-voice button above) ─────────────────────
        n_voices     = len(voices)
        voice_scroll = max(0, min(voice_scroll, max(0, n_voices - max_visible)))

        # ▲ up arrow (scroll up)
        sh.bind(); sh.uniform_float("color", _TEXT_DIM if voice_scroll > 0 else _BORDER)
        arr_up = [(vp_x + vp_w/2, arr_top_y+arrow_h-2*scale),
                  (vp_x + vp_w/2 - 8*scale, arr_top_y+2*scale),
                  (vp_x + vp_w/2 + 8*scale, arr_top_y+2*scale)]
        ab_up = batch_for_shader(sh, "TRIS", {"pos": arr_up})
        ab_up.draw(sh)

        # Voice cards
        v_start_y = arr_top_y - card_gap
        for slot in range(max_visible):
            vi = voice_scroll + slot
            if vi >= n_voices:
                break
            name, onnx_path, _ = voices[vi]
            cy_card = v_start_y - slot*(card_h+card_gap) - card_h
            if cy_card < vp_y + arrow_h + 2*scale:
                break
            is_sel = (vi == selected_idx)
            bg = _PANEL_SEL if is_sel else _PANEL
            bc = _ACCENT    if is_sel else _BORDER
            _draw_rect(vp_x+4*scale, cy_card, vp_w-8*scale, card_h, bg)
            cv2 = [(vp_x+4*scale,cy_card),(vp_x+vp_w-4*scale,cy_card),
                   (vp_x+vp_w-4*scale,cy_card+card_h),
                   (vp_x+4*scale,cy_card+card_h),(vp_x+4*scale,cy_card)]
            cb2 = batch_for_shader(sh, "LINE_STRIP", {"pos": cv2})
            sh.bind(); sh.uniform_float("color", bc); cb2.draw(sh)
            tc = _TEXT if is_sel else _TEXT_VOICE
            _draw_text(name, vp_x+8*scale, cy_card+card_h-fs_vn-3*scale, fs_vn, tc)
            fn = os.path.basename(onnx_path).replace(".onnx", "")
            _draw_text(fn, vp_x+8*scale, cy_card+3*scale, fs_vs, _TEXT_DIM)

        # ▼ down arrow
        arr_bot_y = vp_y + 2*scale
        can_scroll_down = (voice_scroll + max_visible) < n_voices
        sh.bind(); sh.uniform_float("color", _TEXT_DIM if can_scroll_down else _BORDER)
        arr_dn = [(vp_x + vp_w/2, arr_bot_y+2*scale),
                  (vp_x + vp_w/2 - 8*scale, arr_bot_y+arrow_h-2*scale),
                  (vp_x + vp_w/2 + 8*scale, arr_bot_y+arrow_h-2*scale)]
        ab_dn = batch_for_shader(sh, "TRIS", {"pos": arr_dn})
        ab_dn.draw(sh)

        # Page indicator e.g. "4-6 / 8"
        if n_voices > max_visible:
            pg_fs = max(1, int(6*scale))
            pg_txt = f"{voice_scroll+1}-{min(voice_scroll+max_visible, n_voices)} / {n_voices}"
            pg_tw  = _text_width(pg_txt, pg_fs)
            _draw_text(pg_txt, vp_x+vp_w/2-pg_tw/2, arr_bot_y+arrow_h+1*scale,
                       pg_fs, _TEXT_DIM)
    else:
        _draw_text("No voices found in", vp_x+6*scale, vp_y+vp_h*0.6, no_fs, _TEXT_DIM)
        _draw_text("the Piper voices folder.", vp_x+6*scale, vp_y+vp_h*0.45, no_fs, _TEXT_DIM)
        _draw_text("Click + ADD VOICE above.", vp_x+6*scale, vp_y+vp_h*0.35, no_fs, _TEXT_DIM)

    # ── OUTPUT WAVEFORM ───────────────────────────────────────────────────────
    try:
        from core.ai_piper import get_output_wave
        wave_data = get_output_wave(ai_idx)
    except Exception:
        wave_data = []

    ph_wave = None if status == "DONE" else "CLICK GENERATE TO SYNTHESISE"
    # Label removed — "OUTPUT — GENERATED SPEECH" is baked into the skin art now.
    _draw_wave(rx+margin, wave_y + PIPER_WAVE_Y_OFFSET*scale, rw-margin*2, wave_h, scale,
               wave_data, _WAVE_COL, "",
               placeholder=ph_wave, has_skin=_has_skin)

    # ── CONTROLS STRIP (left: channel, right: knobs) ──────────────────────────
    ctrl_split = rx + rw * 0.45

    # "PLACE ON CHANNEL" label removed — baked into the skin art now.

    ch_s    = 18*scale
    ch_gap  = 3*scale
    ch_y    = ctrl_bot + 2*scale
    from core import vse_compat as _vse
    scene_c = bpy.context.scene
    high_c  = 0
    if scene_c and scene_c.sequence_editor:
        for _s in _vse.get_all_strips(scene_c.sequence_editor):
            if _s.type == "SOUND" and _s.sound:
                high_c = max(high_c, _s.channel - 1)
    num_ch = max(9, high_c + 1)
    max_fit = max(1, int((ctrl_split - rx - margin*2) / (ch_s + ch_gap)))
    num_ch  = min(num_ch, max_fit)

    for ci in range(num_ch):
        bx = rx + margin + ci*(ch_s+ch_gap)
        by = ch_y
        assigned_c = getattr(rack, f'ch{ci}', False)
        bg = (0.0, 0.18, 0.10, 1.0) if assigned_c else (0.08, 0.02, 0.04, 1.0)
        bc = (0.0, 0.75, 0.45, 1.0) if assigned_c else _CH_NUM_OFF
        if not _has_skin:
            _draw_rect(bx, by, ch_s, ch_s, bg)
            cv3 = [(bx,by),(bx+ch_s,by),(bx+ch_s,by+ch_s),(bx,by+ch_s),(bx,by)]
            cb3 = batch_for_shader(sh, "LINE_STRIP", {"pos": cv3})
            sh.bind(); sh.uniform_float("color", bc); cb3.draw(sh)
        fs_ci = max(1, int(7*scale))
        lbl_c = str(ci+1)
        tw_c  = _text_width(lbl_c, fs_ci)
        _draw_text(lbl_c, bx+ch_s/2-tw_c/2 + PIPER_CH_BTN_LABEL_X_OFFSET*scale,
                   by+ch_s/2-fs_ci/2 + PIPER_CH_BTN_LABEL_Y_OFFSET*scale, fs_ci, bc)

    # Speed, Noise, Noise_W knobs
    knob_defs = [
        ('p0', 'SPEED',   0.5,  '1.0×'),
        ('p1', 'NOISE',   0.667, '0.67'),
        ('p2', 'NOISE W', 0.8,  '0.80'),
    ]
    n_knobs  = len(knob_defs)
    knob_zone_w = rx + rw - ctrl_split - margin
    knob_w   = knob_zone_w / n_knobs
    knob_r   = min(12*scale, ctrl_h*0.38)
    knob_y_c = ctrl_bot + ctrl_h * 0.58

    # Per-knob value text offsets, in knob_defs order (SPEED, NOISE, NOISE W)
    _knob_value_offsets = [
        (PIPER_SPEED_VALUE_X_OFFSET,  PIPER_SPEED_VALUE_Y_OFFSET),
        (PIPER_NOISE_VALUE_X_OFFSET,  PIPER_NOISE_VALUE_Y_OFFSET),
        (PIPER_NOISEW_VALUE_X_OFFSET, PIPER_NOISEW_VALUE_Y_OFFSET),
    ]

    for i, (attr, lbl, pdef, _fmt) in enumerate(knob_defs):
        kx   = ctrl_split + knob_w*(i+0.5)
        norm = getattr(rack, attr, pdef)
        # Format value
        if attr == 'p0':
            val = 2.0 - norm*1.5
            vs  = f"{val:.1f}×"
        else:
            vs = f"{norm:.2f}"
        _vx_off, _vy_off = _knob_value_offsets[i]
        _draw_knob(kx, knob_y_c, knob_r, norm,
                   (_TEXT[0], _TEXT[1], _TEXT[2]),
                   "", vs, scale,
                   value_x_offset=_vx_off, value_y_offset=_vy_off)

    # ── STATUS BAR ────────────────────────────────────────────────────────────
    if not _has_skin:
        _draw_rect(rx+6*scale, sbar_y, rw-12*scale, sbar_h, (0.04, 0.01, 0.02, 1.0))
        svs = [(rx+6*scale,sbar_y),(rx+rw-6*scale,sbar_y),
               (rx+rw-6*scale,sbar_y+sbar_h),(rx+6*scale,sbar_y+sbar_h),(rx+6*scale,sbar_y)]
        sb = batch_for_shader(sh, "LINE_STRIP", {"pos": svs})
        sh.bind(); sh.uniform_float("color", _BORDER); sb.draw(sh)

    # Voice name in status bar
    voice_name = "no voice loaded"
    if voices:
        vi = int(getattr(rack, 'p4', 0.0)) % max(1, len(voices))
        voice_name = os.path.basename(voices[vi][1]).replace(".onnx", "")
    fs_sb = max(1, int(7*scale))
    _draw_text(
        f"ENGINE: piper.exe  |  MODEL: {voice_name}  |  OFFLINE",
        rx+14*scale, sbar_y+sbar_h/2-fs_sb/2, fs_sb, _TEXT_DIM)

    # Status LED — only drawn for the flat-GPU fallback now; the skin art has
    # its own baked-in LED in the bottom-right corner of the status bar.
    if not _has_skin:
        dot_cols = {
            'READY':      _GREEN,
            'PROCESSING': (0.90, 0.50, 0.10, 1.0),
            'PREVIEWING': (0.60, 0.20, 0.80, 1.0),
            'DONE':       _GREEN,
            'ERROR':      (0.90, 0.10, 0.05, 1.0),
        }
        _draw_circle(rx+rw-14*scale, sbar_y+sbar_h/2,
                     3.5*scale, dot_cols.get(status, (0.4, 0.4, 0.4, 1.0)))

# =============================================================================
# PACKAPUNCH SKIN  -  "HAL 9000"  (rack_piper_bg.png, 1760 x 476 art px)
# Everything below is laid out in ART pixels (top-left origin) and scaled to the rack at draw time, so
# drawing and click zones always agree. Used only while the packApunch skin is active.
# On-screen content (script, voice list, waveform) fades out when the rack is switched off; labels that are
# painted on metal (buttons, keys) stay visible. The eye has its own, much slower, fade.
# =============================================================================
import math as _pm
import re as _pre
import time as _ptime

PP_ART_W = 1760.0
PP_ART_H = 476.0
PP_SKIN  = "packapunch"

PP_COLLAPSE = (10.5, 17.3, 40.0, 44.5)
PP_BADGE    = (49.0, 17.3, 80.0, 44.5)
PP_ON       = (1612.0, 17.4, 1670.0, 40.8)
PP_CLOSE    = (1679.0, 17.4, 1703.0, 40.8)
PP_TOPKEY_X0, PP_TOPKEY_PITCH, PP_TOPKEY_W, PP_TOPKEY_Y = 730.0, 30.3, 24.2, (17.4, 40.8)
PP_BOTKEY_X0, PP_BOTKEY_PITCH, PP_BOTKEY_W, PP_BOTKEY_Y = 10.0, 31.0, 23.0, (415.5, 439.0)

PP_SCRIPT_GLASS = (81.0, 66.0, 601.0, 277.0)
PP_SCRIPT       = (98.0, 80.0, 585.0, 262.0)      # typed-text area (inside the left CRT)
PP_VOICE_GLASS  = (1139.0, 66.0, 1697.0, 277.0)
PP_PRESET_BOX   = (375.0, 17.4, 635.0, 40.8)       # preset selector in the top rail (name box)
PP_PRESET_PREV  = (343.0, 17.4, 373.0, 40.8)       # < hit/draw area
PP_PRESET_NEXT  = (637.0, 17.4, 667.0, 40.8)       # > hit/draw area
PP_ADD          = (1160.0, 84.0, 1676.0, 106.0)   # + ADD VOICE bar (top of the right CRT)
PP_CARD_X       = (1160.0, 1630.0)
PP_CARD_Y0, PP_CARD_PITCH, PP_CARD_H, PP_CARD_ROWS = 112.0, 37.0, 34.0, 4
PP_ARROW_UP     = (1636.0, 112.0, 1676.0, 172.0)
PP_ARROW_DN     = (1636.0, 200.0, 1676.0, 260.0)
PP_DL_W, PP_DL_H = 104.0, 24.0                    # DOWNLOAD button inside a catalog card

PP_GENERATE = (103.0, 310.0, 251.0, 353.0)
PP_PREVIEW  = (272.0, 310.0, 425.0, 353.0)
PP_CLEAR    = (442.0, 310.0, 583.0, 353.0)
PP_WAVE     = (1126.0, 309.0, 1691.0, 345.0)       # output read-out bar
PP_WAVE_BAR = (1118.0, 304.0, 1698.0, 350.0)
PP_KNOBS    = ((1468.8, 396.8), (1570.6, 397.2), (1661.8, 397.0))   # SPEED, NOISE, NOISE W
PP_KNOB_HIT_R   = 30.0
PP_KNOB_PTR_LEN = 15.5
PP_KNOB_PTR_W   = 2.7
PP_KNOB_START_DEG = 225.0
PP_KNOB_SWEEP     = 270.0
PP_STATUS   = (8.0, 447.0, 1752.0, 470.0)
PP_LED      = (1733.0, 461.0)
PP_EYE_C    = (868.0, 169.0)
PP_EYE_PATCH = (756.0, 50.0, 979.0, 289.0)
PP_EYE_GLOW_SIZE = 640.0
PP_EYE_GLOW_GAIN = 0.85
PP_EYE_IDLE_DIP = 0.40                                # how far the idle breath dims the lens (0..1)
PP_SCREEN_TINT = 0.07                                 # phosphor wash strength on the matte-shaped CRTs
PP_EYE_FADE_ON, PP_EYE_FADE_OFF = 0.8, 4.0        # seconds: HAL wakes quickly and dies slowly

PP_FS_BIG, PP_FS_BTN, PP_FS_TEXT, PP_FS_CARD, PP_FS_SUB, PP_FS_SMALL = 24.0, 21.0, 17.0, 16.0, 12.5, 13.0
PP_LINE_H = 1.5
PP_TXT      = (0.86, 0.94, 1.00, 1.00)     # cold phosphor white
PP_TXT_DIM  = (0.50, 0.58, 0.68, 0.90)
PP_TXT_HOT  = (1.00, 1.00, 1.00, 1.00)
PP_METAL    = (0.93, 0.95, 0.97, 1.00)     # lettering painted on metal
PP_METAL_DIM = (0.52, 0.55, 0.60, 0.95)
PP_RED      = (1.00, 0.30, 0.20, 1.00)
PP_GLOW_COL = (0.45, 0.70, 1.00)
PP_GLOW     = 0.55
PP_ICE      = (0.50, 0.72, 1.00)           # light colour of lit keys / outlines

_PP_EYE = {}


def _pp_skin_on():
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas, get_texture as _gt
        return _gas() == PP_SKIN and _gt("rack_piper_bg") is not None
    except Exception:
        return False


def piper_pp_active():
    return _pp_skin_on()


class _PpGeom:
    def __init__(self, rx, ry, rw, rh):
        self.rx, self.ry, self.rw, self.rh = rx, ry, rw, rh
        self.sx, self.sy = rw / PP_ART_W, rh / PP_ART_H
        self.s = min(self.sx, self.sy)

    def X(self, ax):
        return self.rx + ax * self.sx

    def Y(self, ay):
        return self.ry + (1.0 - ay / PP_ART_H) * self.rh

    def rect(self, r):
        return (self.X(r[0]), self.Y(r[3]), self.X(r[2]), self.Y(r[1]))

    def top_key(self, i):
        x0 = PP_TOPKEY_X0 + PP_TOPKEY_PITCH * i
        return (x0, PP_TOPKEY_Y[0], x0 + PP_TOPKEY_W, PP_TOPKEY_Y[1])

    def bot_key(self, i):
        x0 = PP_BOTKEY_X0 + PP_BOTKEY_PITCH * i
        return (x0, PP_BOTKEY_Y[0], x0 + PP_BOTKEY_W, PP_BOTKEY_Y[1])

    def card(self, k):
        y0 = PP_CARD_Y0 + PP_CARD_PITCH * k
        return (PP_CARD_X[0], y0, PP_CARD_X[1], y0 + PP_CARD_H)

    def dl_btn(self, k):
        c = self.card(k)
        cy = (c[1] + c[3]) / 2
        return (c[2] - 8.0 - PP_DL_W, cy - PP_DL_H / 2, c[2] - 8.0, cy + PP_DL_H / 2)


def _pp_in(r, x, y):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


# -- script text layout (shared by drawing, click-to-cursor and drag-select) ------------------------------------
def _pp_wrap(text, fs_px, max_w):
    """Greedy word wrap that keeps real character indices: a list of (start, string); line k owns characters
    start_k .. start_(k+1)-1 (the space a line broke on belongs to the line before it)."""
    if not text:
        return []
    lines = []
    ln_start = None
    ln_end = 0
    for mo in _pre.finditer(r"\S+", text):
        if ln_start is None:
            ln_start, ln_end = mo.start(), mo.end()
            continue
        cand = text[ln_start:mo.end()].replace("\n", " ")
        if _pp_tw(cand, fs_px) <= max_w:
            ln_end = mo.end()
        else:
            lines.append((ln_start, text[ln_start:ln_end].replace("\n", " ")))
            ln_start, ln_end = mo.start(), mo.end()
    if ln_start is None:
        return [(0, text.replace("\n", " "))]
    lines.append((ln_start, text[ln_start:].replace("\n", " ")))
    return lines


def _pp_tw(txt, fs_px):
    try:
        from ui.racks.pp_text import text_width as _ptw
        return _ptw(txt, fs_px)
    except Exception:
        return _text_width(txt, fs_px)


def _pp_text_metrics(sp_w, sp_h):
    """(scale, font px, line height px, max chars-width px, max lines) of the script area from its screen size."""
    sx = sp_w / (PP_SCRIPT[2] - PP_SCRIPT[0])
    sy = sp_h / (PP_SCRIPT[3] - PP_SCRIPT[1])
    s = min(sx, sy)
    fs = max(1, int(round(PP_FS_TEXT * s)))
    lh = PP_FS_TEXT * PP_LINE_H * s
    max_lines = max(1, int((sp_h - 6.0 * s) // lh))
    return s, fs, lh, sp_w - 12.0 * s, max_lines


def _pp_cursor_index(text, sp_x, sp_y, sp_w, sp_h, mouse_x, mouse_y):
    """Character index under the mouse for the packApunch script area (sp_* = its screen rect, y = bottom)."""
    s, fs, lh, wrap_w, max_lines = _pp_text_metrics(sp_w, sp_h)
    if not text:
        return 0
    lines = _pp_wrap(text, fs, wrap_w)
    if not lines:
        return 0
    first = max(0, len(lines) - max_lines)
    top = sp_y + sp_h - 4.0 * s
    row = int((top - mouse_y) // lh)
    row = max(0, min(row, min(max_lines, len(lines) - first) - 1))
    li = first + row
    start, line = lines[li]
    end = lines[li + 1][0] if li + 1 < len(lines) else len(text)
    rel = mouse_x - (sp_x + 6.0 * s)
    if rel <= 0:
        return start
    col = len(line)
    for c in range(len(line) + 1):
        w = _pp_tw(line[:c], fs)
        if w >= rel:
            if c > 0:
                wp = _pp_tw(line[:c - 1], fs)
                col = c - 1 if (rel - wp) < (w - rel) else c
            else:
                col = 0
            break
    return min(start + col, end if li + 1 < len(lines) else len(text))


# -- hit testing ---------------------------------------------------------------------------------------------------
def _pp_voice_state(rack, ai_idx):
    try:
        from core.ai_piper import get_voices
        voices = get_voices()
    except Exception:
        voices = []
    scroll = int(getattr(rack, 'p5', 0.0))
    scroll = max(0, min(scroll, max(0, len(voices) - PP_CARD_ROWS)))
    return voices, scroll


def piper_pp_hit(mx, my, rack, ai_idx, rack_x, rack_y, rw, rh):
    g = _PpGeom(rack_x, rack_y, rw, rh)
    R = g.rect
    if _pp_in(R(PP_COLLAPSE), mx, my):
        return {'zone': 'ai_collapse', 'ai_idx': ai_idx}
    if _pp_in(R(PP_CLOSE), mx, my):
        return {'zone': 'ai_delete', 'ai_idx': ai_idx}
    if _pp_in(R(PP_ON), mx, my):
        return {'zone': 'ai_on_off', 'ai_idx': ai_idx}
    if _pp_in(R(PP_PRESET_PREV), mx, my):
        return {'zone': 'ai_preset_prev', 'ai_idx': ai_idx}
    if _pp_in(R(PP_PRESET_NEXT), mx, my):
        return {'zone': 'ai_preset_next', 'ai_idx': ai_idx}
    for ci in range(9):
        if _pp_in(R(g.top_key(ci)), mx, my) or _pp_in(R(g.bot_key(ci)), mx, my):
            return {'zone': 'ai_channel_btn', 'ai_idx': ai_idx, 'ch_idx': ci}
    for ki, (kx, ky) in enumerate(PP_KNOBS):
        if _pm.hypot(mx - g.X(kx), my - g.Y(ky)) <= PP_KNOB_HIT_R * g.s:
            return {'zone': 'ai_piper_knob', 'ai_idx': ai_idx, 'knob_idx': ki}
    if _pp_in(R(PP_GENERATE), mx, my):
        return {'zone': 'ai_process', 'ai_idx': ai_idx, 'ai_type': 'PIPER_TTS'}
    if _pp_in(R(PP_PREVIEW), mx, my):
        return {'zone': 'ai_piper_preview', 'ai_idx': ai_idx}
    if _pp_in(R(PP_CLEAR), mx, my):
        return {'zone': 'ai_piper_clear', 'ai_idx': ai_idx}
    sr = R(PP_SCRIPT)
    if _pp_in(sr, mx, my):
        return {'zone': 'ai_piper_text', 'ai_idx': ai_idx,
                'sp_x': sr[0], 'sp_y': sr[1], 'sp_w': sr[2] - sr[0], 'sp_h': sr[3] - sr[1],
                'scale': g.s, 'mouse_x': mx, 'mouse_y': my}
    browsing = _browse_mode.get(ai_idx, False)
    if _pp_in(R(PP_ADD), mx, my):
        return {'zone': 'ai_piper_add_voice_toggle', 'ai_idx': ai_idx}
    scroll_zone = 'ai_piper_catalog_scroll' if browsing else 'ai_piper_scroll'
    if _pp_in(R(PP_ARROW_UP), mx, my):
        return {'zone': scroll_zone, 'ai_idx': ai_idx, 'dir': -1}
    if _pp_in(R(PP_ARROW_DN), mx, my):
        return {'zone': scroll_zone, 'ai_idx': ai_idx, 'dir': 1}
    if browsing:
        try:
            from core.ai_piper import (get_voice_catalog, is_voice_installed, get_download_state)
            cat = get_voice_catalog()
        except Exception:
            return None
        n = len(cat)
        b_scroll = max(0, min(int(_browse_scroll.get(ai_idx, 0)), max(0, n - PP_CARD_ROWS)))
        for slot in range(PP_CARD_ROWS):
            vi = b_scroll + slot
            if vi >= n:
                break
            try:
                inst = is_voice_installed(cat[vi]['key'])
                dls = get_download_state(cat[vi]['key'])
            except Exception:
                inst, dls = False, None
            if inst or (dls and dls.get('status') == 'DOWNLOADING'):
                continue
            if _pp_in(R(g.dl_btn(slot)), mx, my):
                return {'zone': 'ai_piper_catalog_click', 'ai_idx': ai_idx, 'voice_key': cat[vi]['key']}
        return None
    voices, scroll = _pp_voice_state(rack, ai_idx)
    for slot in range(PP_CARD_ROWS):
        vi = scroll + slot
        if vi >= len(voices):
            break
        if _pp_in(R(g.card(slot)), mx, my):
            return {'zone': 'ai_piper_voice', 'ai_idx': ai_idx, 'voice_idx': vi}
    return None


# -- drawing helpers -----------------------------------------------------------------------------------------------
def _pp_text(text, x, cy, fs_art, g, col, a=1.0, align="l", glow=PP_GLOW):
    """Glowing text; x = left / centre / right by align, cy = vertical centre in screen px."""
    try:
        from ui.racks.pp_text import draw_text as _pdt, text_width as _ptw
    except Exception:
        _pdt = _ptw = None
    if a <= 0.01:
        return 0.0
    fs = max(1, int(round(fs_art * g.s)))
    c = (col[0], col[1], col[2], col[3] * a)
    tw = _ptw(text, fs) if _ptw else _text_width(text, fs)
    if align == "c":
        x -= tw / 2
    elif align == "r":
        x -= tw
    y = cy - fs * 0.36
    if _pdt:
        _pdt(text, x, y, fs, c, glow=glow * a, glow_col=PP_GLOW_COL)
    else:
        _draw_text(text, x, y, fs, c)
    return tw


def _pp_quad(sh, x0, y0, x1, y1, col, blend="ALPHA"):
    gpu.state.blend_set(blend)
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _pp_tri(sh, pts, col, blend="ALPHA"):
    gpu.state.blend_set(blend)
    sh.bind()
    sh.uniform_float("color", col)
    batch_for_shader(sh, "TRIS", {"pos": pts}).draw(sh)
    gpu.state.blend_set("ALPHA")


def _pp_outline(sh, r, w, col, blend="ALPHA"):
    x0, y0, x1, y1 = r
    _pp_quad(sh, x0, y1 - w, x1, y1, col, blend)
    _pp_quad(sh, x0, y0, x1, y0 + w, col, blend)
    _pp_quad(sh, x0, y0, x0 + w, y1, col, blend)
    _pp_quad(sh, x1 - w, y0, x1, y1, col, blend)


def _pp_glow_box(sh, r, lv, g, strong=True):
    t = max(1.0, 1.6 * g.s)
    for grow, wid, a in ((3.0 * g.s, 3.0 * g.s, 0.08), (1.5 * g.s, 2.2 * g.s, 0.15), (0.0, t, 0.85 if strong else 0.35)):
        rr = (r[0] - grow, r[1] - grow, r[2] + grow, r[3] + grow)
        _pp_outline(sh, rr, wid, (PP_ICE[0], PP_ICE[1], PP_ICE[2], a * lv), "ADDITIVE" if grow > 0 else "ALPHA")
    _pp_quad(sh, r[0], r[1], r[2], r[3], (PP_ICE[0], PP_ICE[1], PP_ICE[2], 0.07 * lv), "ADDITIVE")


def _pp_blit(key, r, blend="ALPHA_PREMULT", k=1.0):
    try:
        from ui.mixer.texture_cache import (get_texture as _gt, blit_texture as _bt, blit_texture_tinted as _btt)
        tex = _gt(key)
        if tex:
            if k >= 0.999:
                _bt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], key=key, blend=blend)
            elif k > 0.003:
                _btt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], color=(k, k, k, k), blend=blend, key=key)
            return True
    except Exception as e:
        print(f"[PIPER] blit {key}: {e}")
    return False


def _pp_eye_level(ai_idx, enabled, now):
    """0..1 life of the eye: wakes in ~0.8 s, dies over ~4 s."""
    st = _PP_EYE.get(ai_idx)
    if st is None:
        st = {"l": 1.0 if enabled else 0.0, "t": now}
        _PP_EYE[ai_idx] = st
    dt = max(0.0, min(0.25, now - st["t"]))
    st["t"] = now
    if enabled:
        st["l"] = min(1.0, st["l"] + dt / PP_EYE_FADE_ON)
    else:
        st["l"] = max(0.0, st["l"] - dt / PP_EYE_FADE_OFF)
    v = st["l"]
    return v * v * (3.0 - 2.0 * v)


def _pp_eye_pulse(status, now):
    if status == "PROCESSING":
        return 0.55 + 0.45 * _pm.sin(now * 6.9)
    if status == "PREVIEWING":
        return 0.25 + 0.75 * min(1.0, 0.65 * abs(_pm.sin(now * 7.3)) + 0.45 * abs(_pm.sin(now * 3.1 + 1.0)))
    if status == "ERROR":
        return 0.35 + 0.45 * (1.0 if _pm.sin(now * 23.0) > 0 else 0.0)
    return _pp_eye_idle(now)


def _pp_eye_idle(now):
    """HAL at rest: slow breathing (~4 s) with a faint shimmer, 0.28 .. 0.80."""
    br = 0.5 + 0.5 * _pm.sin(now * 1.55)
    sh = 0.5 + 0.5 * _pm.sin(now * 5.3 + 1.3) * _pm.sin(now * 0.7)
    return 0.28 + 0.46 * br + 0.06 * sh


def _pp_x_glyph(sh, r, col, g):
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    d = 5.2 * g.s
    hw = 1.25 * g.s
    for sgn in (1, -1):
        dx, dy = d, d * sgn
        nx, ny = -dy / _pm.hypot(dx, dy) * hw, dx / _pm.hypot(dx, dy) * hw
        pts = [(cx - dx + nx, cy - dy + ny), (cx - dx - nx, cy - dy - ny), (cx + dx + nx, cy + dy + ny),
               (cx - dx - nx, cy - dy - ny), (cx + dx - nx, cy + dy - ny), (cx + dx + nx, cy + dy + ny)]
        _pp_tri(sh, pts, col)


def _pp_pointer(sh, cx, cy, length, width, ang_deg):
    a = _pm.radians(ang_deg)
    ca, sa = _pm.cos(a), _pm.sin(a)
    x0, y0 = cx + ca * length * 0.10, cy + sa * length * 0.10
    x1, y1 = cx + ca * length, cy + sa * length
    for wmul, col, bl in ((2.4, (0.0, 0.0, 0.0, 0.30), "ALPHA"),
                          (1.0, (0.97, 0.98, 1.0, 1.0), "ALPHA")):
        hw = width * wmul * 0.5
        nx, ny = -sa * hw, ca * hw
        q = [(x0 + nx, y0 + ny), (x0 - nx, y0 - ny), (x1 + nx, y1 + ny),
             (x0 - nx, y0 - ny), (x1 - nx, y1 - ny), (x1 + nx, y1 + ny)]
        _pp_tri(sh, q, col, bl)


def piper_pp_draw(rx, ry, rw, rh, rack, ai_idx, scale):
    """packApunch Piper rack: everything that moves or changes, drawn over the baked art."""
    g = _PpGeom(rx, ry, rw, rh)
    R = g.rect
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    enabled = bool(getattr(rack, "enabled", True))
    now_m = _ptime.monotonic()
    now = _ptime.time()
    try:
        from ui.racks.rack_base import _pp_preset_level as _ppl
        lv = _ppl(("piper", ai_idx), enabled, now_m)
    except Exception:
        lv = 1.0 if enabled else 0.0
    eye_lv = _pp_eye_level(ai_idx, enabled, now_m)
    status = getattr(rack, 'ai_status', 'READY')

    # ── power: dead screens fade in over the chassis; the eye dims on its own, slower clock ───────────
    if lv < 0.997:
        _pp_blit("rack_piper_bg_off", (rx, ry, rx + rw, ry + rh), k=1.0 - lv)
    k_eye = 1.0 - eye_lv
    if status not in ("PROCESSING", "PREVIEWING", "ERROR"):
        # idle: the lens itself breathes (dips toward the dark patch), not just its bloom
        k_eye += eye_lv * (1.0 - _pp_eye_idle(now) / 0.80) * PP_EYE_IDLE_DIP
    if k_eye > 0.003:
        _pp_blit("rack_piper_eye_off", R(PP_EYE_PATCH), k=min(1.0, k_eye))
    if eye_lv > 0.01:
        gx, gy = g.X(PP_EYE_C[0]), g.Y(PP_EYE_C[1])
        half = PP_EYE_GLOW_SIZE * 0.5
        kk = _pp_eye_pulse(status, now) * eye_lv * PP_EYE_GLOW_GAIN
        try:
            from ui.mixer.texture_cache import get_texture as _gt, blit_texture_tinted as _btt
            tex = _gt("rack_piper_eye_glow")
            if tex is not None and kk > 0.003:
                _btt(tex, gx - half * g.sx, gy - half * g.sy, 2 * half * g.sx, 2 * half * g.sy,
                     color=(kk, kk, kk, kk), blend="ADDITIVE_PREMULT", key="rack_piper_eye_glow")
        except Exception as _e:
            print(f"[PIPER] eye glow error: {_e}")

    # ── faint phosphor light on the two CRT faces (shaped by the screen matte) ──────────────────────
    if lv > 0.01:
        try:
            from ui.mixer.texture_cache import get_texture as _gt, blit_texture_tinted as _btt
            tex = _gt("rack_piper_screens")
            if tex is not None:
                k_ = PP_SCREEN_TINT * lv
                _btt(tex, rx, ry, rw, rh, color=(0.45 * k_, 0.62 * k_, 0.85 * k_, k_),
                     blend="ADDITIVE_PREMULT", key="rack_piper_screens")
        except Exception as _e:
            print(f"[PIPER] screen tint error: {_e}")

    # ── rack number, ON key, close key ───────────────────────────────────────────────────────────────
    br = R(PP_BADGE)
    _pp_text(str(ai_idx + 1), (br[0] + br[2]) / 2, (br[1] + br[3]) / 2, 22.0, g, PP_METAL, 1.0, "c", glow=0.0)
    onr = R(PP_ON)
    if lv > 0.01:
        _pp_quad(sh, onr[0], onr[1], onr[2], onr[3], (PP_ICE[0], PP_ICE[1], PP_ICE[2], 0.10 * lv), "ADDITIVE")
    _pp_text("ON", (onr[0] + onr[2]) / 2, (onr[1] + onr[3]) / 2, PP_FS_BTN, g,
             (PP_METAL_DIM[0] + (PP_TXT_HOT[0] - PP_METAL_DIM[0]) * lv,
              PP_METAL_DIM[1] + (PP_TXT_HOT[1] - PP_METAL_DIM[1]) * lv,
              PP_METAL_DIM[2] + (PP_TXT_HOT[2] - PP_METAL_DIM[2]) * lv, 1.0), 1.0, "c", glow=0.8 * lv)
    _pp_x_glyph(sh, R(PP_CLOSE), (0.85, 0.20, 0.15, 0.95), g)

    # ── preset selector (top rail): < name > — same list/zones as the stock Piper rack ─────────────
    try:
        import Racks as _rk
        _presets = _rk.AI_PRESETS.get("PIPER_TTS", [])
    except Exception:
        _presets = []
    if _presets:
        p_name = _presets[int(getattr(rack, 'preset_idx', 0)) % len(_presets)]
        pb = R(PP_PRESET_BOX)
        if lv > 0.01:
            _pp_quad(sh, pb[0], pb[1], pb[2], pb[3], (0.0, 0.0, 0.0, 0.45 * lv))
            _pp_outline(sh, pb, max(1.0, 1.2 * g.s), (PP_ICE[0], PP_ICE[1], PP_ICE[2], 0.55 * lv))
            _pp_quad(sh, pb[0], pb[1], pb[2], pb[3], (PP_ICE[0], PP_ICE[1], PP_ICE[2], 0.06 * lv), "ADDITIVE")
        _pp_text(p_name.upper(), (pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2, PP_FS_SMALL + 2.0, g, PP_TXT, lv, "c", glow=0.5)
        for rr_, sgn in ((R(PP_PRESET_PREV), -1), (R(PP_PRESET_NEXT), 1)):
            ax, ay = (rr_[0] + rr_[2]) / 2, (rr_[1] + rr_[3]) / 2
            hw, hh = 5.0 * g.s, 6.5 * g.s
            tip, base = ax + sgn * hw, ax - sgn * hw
            _pp_tri(sh, [(tip, ay), (base, ay + hh), (base, ay - hh)],
                    (PP_METAL[0], PP_METAL[1], PP_METAL[2], 0.35 + 0.55 * lv))

    # ── channel keys (rail row above, and the PLACE ON CHANNEL row below) ───────────────────────────
    group_off = getattr(rack, 'group_idx', 0) * 9
    for ci in range(9):
        assigned = bool(getattr(rack, f"ch{ci}", False))
        for kr in (R(g.top_key(ci)), R(g.bot_key(ci))):
            if assigned and lv > 0.01:
                _pp_quad(sh, kr[0] - 2 * g.s, kr[1] - 2 * g.s, kr[2] + 2 * g.s, kr[3] + 2 * g.s,
                         (PP_ICE[0], PP_ICE[1], PP_ICE[2], 0.10 * lv), "ADDITIVE")
                _pp_quad(sh, kr[0], kr[1], kr[2], kr[3], (PP_ICE[0], PP_ICE[1], PP_ICE[2], 0.34 * lv), "ADDITIVE")
            _pp_text(str(group_off + ci + 1), (kr[0] + kr[2]) / 2, (kr[1] + kr[3]) / 2, PP_FS_SMALL + 1.0, g,
                     PP_TXT_HOT if (assigned and lv > 0.5) else PP_METAL_DIM, 1.0, "c",
                     glow=0.7 * lv if assigned else 0.0)

    # ── script (left CRT) ────────────────────────────────────────────────────────────────────────────
    script = getattr(rack, 'ai_text', '') or ''
    text_active, cursor_pos, sel = False, len(script), (-1, -1)
    try:
        from ui.mixer.interaction import _active_text_field as _atf
        if _atf and _atf.get('ai_idx') == ai_idx:
            text_active = True
            cursor_pos = _atf.get('cursor', len(script))
            ss, se = _atf.get('sel_start', -1), _atf.get('sel_end', -1)
            if ss >= 0 and se >= 0 and ss != se:
                sel = (min(ss, se), max(ss, se))
    except Exception:
        pass
    sr = R(PP_SCRIPT)
    s_, fs_px, lh, wrap_w, max_lines = _pp_text_metrics(sr[2] - sr[0], sr[3] - sr[1])
    tx0 = sr[0] + 6.0 * s_
    if lv > 0.01:
        if not script and not text_active:
            _pp_text("Click to type script...", tx0, sr[3] - 4.0 * s_ - lh * 0.5, PP_FS_TEXT, g, PP_TXT_DIM, lv)
        lines = _pp_wrap(script, fs_px, wrap_w)
        first = max(0, len(lines) - max_lines)
        cur_line = None
        for li in range(first, len(lines)):
            row = li - first
            if row >= max_lines:
                break
            start, line = lines[li]
            end = lines[li + 1][0] if li + 1 < len(lines) else len(script) + 1
            cy = sr[3] - 4.0 * s_ - lh * (row + 0.5)
            if sel[0] >= 0:
                ov0, ov1 = max(sel[0], start) - start, min(sel[1], start + len(line)) - start
                if ov0 < ov1:
                    pre_w = _pp_tw(line[:ov0], fs_px)
                    sel_w = _pp_tw(line[ov0:ov1], fs_px)
                    _pp_quad(sh, tx0 + pre_w, cy - lh * 0.5, tx0 + pre_w + max(sel_w, 2 * s_), cy + lh * 0.5,
                             (0.35, 0.55, 0.95, 0.38 * lv), "ALPHA")
            _pp_text(line, tx0, cy, PP_FS_TEXT, g, PP_TXT, lv)
            if text_active and cur_line is None and start <= cursor_pos < end:
                cur_line = (row, cy, line[:max(0, min(cursor_pos - start, len(line)))], line)
        if text_active and script and cur_line is None and lines:
            row = min(len(lines) - 1 - first, max_lines - 1)
            cur_line = (row, sr[3] - 4.0 * s_ - lh * (row + 0.5), lines[-1][1], lines[-1][1])
        if text_active and int(now * 2) % 2 == 0:
            # terminal-style block cursor: a solid phosphor rectangle one character wide, covering the
            # character it sits on (which is redrawn dark on top so it stays readable)
            if cur_line is None:
                cx_, cy_, pre_, full_ = tx0, sr[3] - 4.0 * s_ - lh * 0.5, "", ""
            else:
                pre_, full_ = cur_line[2], cur_line[3]
                cx_, cy_ = tx0 + _pp_tw(pre_, fs_px), cur_line[1]
            ch_ = full_[len(pre_)] if len(pre_) < len(full_) else ""
            bw_ = _pp_tw(ch_, fs_px) if ch_ and ch_ != " " else 0.0
            if bw_ <= 0.0:
                bw_ = _pp_tw(" ", fs_px) if ch_ == " " else _pp_tw("M", fs_px) * 0.82
            bw_ = max(bw_, 5.0 * g.s)
            _pp_quad(sh, cx_, cy_ - fs_px * 0.50, cx_ + bw_, cy_ + fs_px * 0.46, (0.82, 0.92, 1.0, 0.85 * lv))
            _pp_quad(sh, cx_ - 2 * g.s, cy_ - fs_px * 0.62, cx_ + bw_ + 2 * g.s, cy_ + fs_px * 0.58,
                     (PP_GLOW_COL[0], PP_GLOW_COL[1], PP_GLOW_COL[2], 0.10 * lv), "ADDITIVE")
            if ch_ and ch_ != " ":
                _pp_text(ch_, cx_, cy_, PP_FS_TEXT, g, (0.02, 0.04, 0.07, 1.0), lv)
        cc = f"{len(script)} / 4096"
        _pp_text(cc, sr[2] - 4.0 * g.s, sr[1] + 8.0 * g.s, PP_FS_SMALL - 1.5, g, PP_TXT_DIM, lv * 0.8, "r", glow=0.3)

    # ── status message, bottom-left of the script screen (button lettering is baked in the art) ─────
    if lv > 0.01 and status in ("PROCESSING", "ERROR", "PREVIEWING"):
        if status == "PROCESSING":
            msg, mcol, ma = "GENERATING...", PP_TXT_HOT, 0.65 + 0.35 * _pm.sin(now * 6.0)
        elif status == "ERROR":
            msg, mcol, ma = "ERROR - CLICK GENERATE TO RETRY", PP_RED, 1.0
        else:
            msg, mcol, ma = "PLAYING...", PP_TXT, 0.9
        _pp_text(msg, sr[0] + 6.0 * g.s, sr[1] + 8.0 * g.s, PP_FS_SMALL + 1.0, g, mcol, lv * ma, "l", glow=0.6)

    # ── voices (right CRT) ───────────────────────────────────────────────────────────────────────────
    browsing = _browse_mode.get(ai_idx, False)
    ar_ = R(PP_ADD)
    if lv > 0.01:
        _pp_outline(sh, ar_, max(1.0, 1.2 * g.s), (PP_ICE[0], PP_ICE[1], PP_ICE[2], 0.55 * lv))
        _pp_text("< BACK TO VOICES" if browsing else "+ ADD VOICE", (ar_[0] + ar_[2]) / 2, (ar_[1] + ar_[3]) / 2,
                 PP_FS_SMALL + 2.0, g, PP_TXT, lv, "c")
        up, dn = R(PP_ARROW_UP), R(PP_ARROW_DN)
        cxa = (up[0] + up[2]) / 2
    def _arrows(can_up, can_dn, label):
        if lv <= 0.01:
            return
        col_u = (PP_TXT[0], PP_TXT[1], PP_TXT[2], (0.9 if can_up else 0.22) * lv)
        col_d = (PP_TXT[0], PP_TXT[1], PP_TXT[2], (0.9 if can_dn else 0.22) * lv)
        ut = up[3] - 10.0 * g.s
        _pp_tri(sh, [(cxa, ut), (cxa - 9 * g.s, ut - 13 * g.s), (cxa + 9 * g.s, ut - 13 * g.s)], col_u)
        db = dn[1] + 10.0 * g.s
        _pp_tri(sh, [(cxa, db), (cxa - 9 * g.s, db + 13 * g.s), (cxa + 9 * g.s, db + 13 * g.s)], col_d)
        if label:
            _pp_text(label, cxa, (up[1] + dn[3]) / 2, PP_FS_SMALL - 1.5, g, PP_TXT_DIM, lv, "c", glow=0.3)

    if browsing:
        try:
            from core.ai_piper import (get_voice_catalog, get_catalog_status, is_voice_installed, get_download_state)
            catalog = get_voice_catalog()
            cat_status, cat_err = get_catalog_status()
        except Exception as _ce:
            catalog, cat_status, cat_err = [], "ERROR", str(_ce)
        c0 = R(g.card(0))
        if cat_status == "LOADING":
            _pp_text("Loading voice catalog...", c0[0] + 14 * g.s, g.Y(150), PP_FS_CARD, g, PP_TXT_DIM, lv)
        elif cat_status == "BLOCKED":
            _pp_text("Online access is disabled.", c0[0] + 14 * g.s, g.Y(140), PP_FS_CARD, g, PP_RED, lv)
            _pp_text("Enable it in Preferences > Get Extensions > Allow Online Access.", c0[0] + 14 * g.s, g.Y(166),
                     PP_FS_SUB, g, PP_TXT_DIM, lv)
        elif cat_status == "ERROR":
            _pp_text("Could not load catalog:", c0[0] + 14 * g.s, g.Y(140), PP_FS_CARD, g, PP_RED, lv)
            _pp_text((cat_err or "unknown error")[:70], c0[0] + 14 * g.s, g.Y(166), PP_FS_SUB, g, PP_TXT_DIM, lv)
        elif not catalog:
            _pp_text("Catalog is empty.", c0[0] + 14 * g.s, g.Y(150), PP_FS_CARD, g, PP_TXT_DIM, lv)
        else:
            n_cat = len(catalog)
            b_scroll = max(0, min(int(_browse_scroll.get(ai_idx, 0)), max(0, n_cat - PP_CARD_ROWS)))
            _browse_scroll[ai_idx] = b_scroll
            for slot in range(PP_CARD_ROWS):
                vi = b_scroll + slot
                if vi >= n_cat:
                    break
                entry = catalog[vi]
                cr_ = R(g.card(slot))
                try:
                    installed = is_voice_installed(entry['key'])
                    dls = get_download_state(entry['key'])
                except Exception:
                    installed, dls = False, None
                downloading = bool(dls and dls.get('status') == 'DOWNLOADING')
                is_err = bool(dls and dls.get('status') == 'ERROR')
                if lv > 0.01:
                    _pp_outline(sh, cr_, max(1.0, g.s), (PP_ICE[0], PP_ICE[1], PP_ICE[2], (0.7 if installed else 0.22) * lv))
                cy = (cr_[1] + cr_[3]) / 2
                name_line = f"{entry.get('lang_name', '')} - {entry.get('name', '')} ({entry.get('quality', '')})"
                show_btn = not installed and not downloading
                max_w = (R(g.dl_btn(slot))[0] - cr_[0] - 22 * g.s) if show_btn else (cr_[2] - cr_[0] - 22 * g.s)
                fs_px2 = max(1, int(round(PP_FS_CARD * g.s)))
                while len(name_line) > 4 and _pp_tw(name_line, fs_px2) > max_w:
                    name_line = name_line[:-1]
                _pp_text(name_line, cr_[0] + 12 * g.s, cr_[3] - 11 * g.s, PP_FS_CARD, g,
                         PP_TXT_HOT if installed else PP_TXT, lv)
                size_mb = (entry.get('onnx_size', 0) or 0) / (1024 * 1024)
                st_txt = "INSTALLED" if installed else (f"DOWNLOADING {dls.get('pct', 0):.0f}%" if downloading else ("ERROR" if is_err else ""))
                _pp_text(f"{size_mb:.0f} MB" + (f"   {st_txt}" if st_txt else ""), cr_[0] + 12 * g.s, cr_[1] + 9 * g.s,
                         PP_FS_SUB, g, PP_RED if is_err else PP_TXT_DIM, lv)
                if show_btn and lv > 0.01:
                    db_ = R(g.dl_btn(slot))
                    _pp_outline(sh, db_, max(1.0, 1.2 * g.s), ((PP_RED[0], PP_RED[1], PP_RED[2]) if is_err else PP_ICE) + (0.8 * lv,))
                    _pp_text("RETRY" if is_err else "DOWNLOAD", (db_[0] + db_[2]) / 2, (db_[1] + db_[3]) / 2,
                             PP_FS_SMALL, g, PP_RED if is_err else PP_TXT, lv, "c")
            lbl = f"{b_scroll + 1}-{min(b_scroll + PP_CARD_ROWS, n_cat)}/{n_cat}" if n_cat > PP_CARD_ROWS else ""
            _arrows(b_scroll > 0, b_scroll + PP_CARD_ROWS < n_cat, lbl)
    else:
        voices, scroll = _pp_voice_state(rack, ai_idx)
        sel_idx = int(getattr(rack, 'p4', 0.0)) % max(1, len(voices)) if voices else 0
        if voices:
            for slot in range(PP_CARD_ROWS):
                vi = scroll + slot
                if vi >= len(voices):
                    break
                name, onnx_path, _x = voices[vi]
                cr_ = R(g.card(slot))
                issel = (vi == sel_idx)
                if lv > 0.01:
                    if issel:
                        _pp_glow_box(sh, cr_, lv, g)
                    else:
                        _pp_outline(sh, cr_, max(1.0, g.s), (PP_ICE[0], PP_ICE[1], PP_ICE[2], 0.20 * lv))
                _pp_text(name, cr_[0] + 12 * g.s, cr_[3] - 11 * g.s, PP_FS_CARD, g, PP_TXT_HOT if issel else PP_TXT, lv)
                fn = os.path.basename(onnx_path).replace(".onnx", "")
                _pp_text(fn, cr_[0] + 12 * g.s, cr_[1] + 9 * g.s, PP_FS_SUB, g, PP_TXT_DIM, lv)
            lbl = f"{scroll + 1}-{min(scroll + PP_CARD_ROWS, len(voices))}/{len(voices)}" if len(voices) > PP_CARD_ROWS else ""
            _arrows(scroll > 0, scroll + PP_CARD_ROWS < len(voices), lbl)
        else:
            c0 = R(g.card(0))
            _pp_text("No voices found - use + ADD VOICE", c0[0] + 14 * g.s, g.Y(140), PP_FS_CARD, g, PP_TXT_DIM, lv)
            _pp_text("Click + ADD VOICE above.", c0[0] + 14 * g.s, g.Y(166), PP_FS_CARD, g, PP_TXT_DIM, lv)

    # ── output read-out ──────────────────────────────────────────────────────────────────────────────
    wr = R(PP_WAVE)
    try:
        from core.ai_piper import get_output_wave
        wave = get_output_wave(ai_idx)
    except Exception:
        wave = []
    if lv > 0.01:
        cyw = (wr[1] + wr[3]) / 2
        amp = (wr[3] - wr[1]) * 0.46
        if wave and len(wave) > 1:
            n = len(wave)
            nb = min(n, 520)
            step = (wr[2] - wr[0]) / nb
            for b in range(nb):
                i0, i1 = int(b * n / nb), max(int(b * n / nb) + 1, int((b + 1) * n / nb))
                a_ = max(abs(v) for v in wave[i0:i1])
                h_ = max(1.0 * g.s, a_ * amp)
                x0_ = wr[0] + b * step
                _pp_quad(sh, x0_, cyw - h_, x0_ + step * 0.72, cyw + h_, (0.55, 0.78, 1.0, 0.80 * lv))
            _pp_quad(sh, wr[0], cyw - 0.5 * g.s, wr[2], cyw + 0.5 * g.s, (0.8, 0.92, 1.0, 0.35 * lv))
        else:
            _pp_quad(sh, wr[0], cyw - 0.6 * g.s, wr[2], cyw + 0.6 * g.s, (0.5, 0.7, 1.0, 0.18 * lv), "ADDITIVE")
            if status != "DONE":
                _pp_text("CLICK GENERATE TO SYNTHESISE", (wr[0] + wr[2]) / 2, cyw, PP_FS_SMALL, g, PP_TXT_DIM, lv, "c", glow=0.3)

    # ── knobs ────────────────────────────────────────────────────────────────────────────────────────
    for ki, (kx, ky) in enumerate(PP_KNOBS):
        v = max(0.0, min(1.0, float(getattr(rack, f"p{ki}", (0.5, 0.667, 0.8)[ki]))))
        cx, cy = g.X(kx), g.Y(ky)
        _pp_pointer(sh, cx, cy, PP_KNOB_PTR_LEN * g.s, PP_KNOB_PTR_W * g.s, PP_KNOB_START_DEG - PP_KNOB_SWEEP * v)
        vs = f"{2.0 - v * 1.5:.1f}x" if ki == 0 else f"{v:.2f}"
        _pp_text(vs, cx, g.Y(ky - 33.0), PP_FS_SMALL + 2.0, g, PP_METAL, 1.0, "c", glow=0.3)

    # ── status bar + LED ─────────────────────────────────────────────────────────────────────────────
    voices_all = []
    try:
        from core.ai_piper import get_voices
        voices_all = get_voices()
    except Exception:
        pass
    voice_name = "no voice loaded"
    if voices_all:
        vi = int(getattr(rack, 'p4', 0.0)) % max(1, len(voices_all))
        voice_name = os.path.basename(voices_all[vi][1]).replace(".onnx", "")
    sb = R(PP_STATUS)
    _pp_text(f"ENGINE: piper.exe  |  MODEL: {voice_name}  |  OFFLINE", sb[0] + 14 * g.s, (sb[1] + sb[3]) / 2,
             PP_FS_SMALL, g, PP_TXT, lv * 0.85, glow=0.4)
    led_cols = {'READY': (0.20, 0.95, 0.45), 'PROCESSING': (1.0, 0.65, 0.15), 'PREVIEWING': (0.65, 0.4, 1.0),
                'DONE': (0.20, 0.95, 0.45), 'ERROR': (1.0, 0.15, 0.1)}
    lc = led_cols.get(status, led_cols['READY'])
    lx, ly = g.X(PP_LED[0]), g.Y(PP_LED[1])
    for rad, a in ((11.0, 0.10), (7.0, 0.22), (4.4, 0.95)):
        rr = rad * g.s
        pts = []
        for k in range(24):
            a0, a1 = 2 * _pm.pi * k / 24, 2 * _pm.pi * (k + 1) / 24
            pts += [(lx, ly), (lx + rr * _pm.cos(a0), ly + rr * _pm.sin(a0)), (lx + rr * _pm.cos(a1), ly + rr * _pm.sin(a1))]
        _pp_tri(sh, pts, (lc[0], lc[1], lc[2], a * lv), "ADDITIVE" if rad > 5 else "ALPHA")
