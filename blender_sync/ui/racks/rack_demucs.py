# =============================================================================
# ui/racks/rack_demucs.py
# Demucs — Stem Splitter rack UI
#
# p0 = model index     (0=htdemucs 1=htdemucs_ft 2=htdemucs_6s 3=mdx_extra)
# p2 = mute original   (>0.5 = mute source after split)
# p3 = stem toggle bits (bit0=drums bit1=bass bit2=vocals bit3=other
#                        bit4=piano bit5=guitar)
# p4 = drums out ch    (0=auto)
# p5 = bass out ch     (0=auto)
# p6 = vocals out ch   (0=auto)
# p7 = other out ch    (0=auto)
# p8 = piano out ch    (0=auto) — htdemucs_6s only
# p9 = guitar out ch   (0=auto) — htdemucs_6s only
# =============================================================================

import math
import time
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
    )
except ImportError:
    pass

RACK_RAIL_H = 32

_BG         = (0.02, 0.06, 0.03, 1.0)
_BORDER     = (0.08, 0.25, 0.10, 1.0)
_GREEN      = (0.15, 0.85, 0.35, 1.0)
_GREEN_DIM  = (0.06, 0.35, 0.14, 1.0)
_GREEN_MID  = (0.08, 0.55, 0.22, 1.0)
_RED        = (0.85, 0.15, 0.10, 1.0)
_AMBER      = (0.85, 0.55, 0.05, 1.0)
_TEXT_DIM   = (0.18, 0.45, 0.22, 1.0)
_TEXT_MUTED = (0.08, 0.22, 0.10, 1.0)
_PANEL      = (0.01, 0.04, 0.02, 1.0)

MODELS = ["htdemucs", "htdemucs_ft", "htdemucs_6s", "mdx_extra"]
MODEL_DESC = {
    "htdemucs":    "4 stems · fast",
    "htdemucs_ft": "4 stems · best",
    "htdemucs_6s": "6 stems · fine",
    "mdx_extra":   "4 stems · alt",
}
MODEL_STEMS = {
    "htdemucs":    ["drums", "bass", "vocals", "other"],
    "htdemucs_ft": ["drums", "bass", "vocals", "other"],
    "htdemucs_6s": ["drums", "bass", "vocals", "other", "piano", "guitar"],
    "mdx_extra":   ["drums", "bass", "vocals", "other"],
}
STEM_ICONS = {
    "drums":  "DRUMS",
    "bass":   "BASS",
    "vocals": "VOX",
    "other":  "OTHER",
    "piano":  "PIANO",
    "guitar": "GTR",
}
STEM_BITS = {"drums": 0, "bass": 1, "vocals": 2, "other": 3, "piano": 4, "guitar": 5}
STEM_CH_PROPS = {"drums": "p4", "bass": "p5", "vocals": "p6", "other": "p7",
                 "piano": "p8", "guitar": "p9"}

# Fixed draw/hit-test order for all 6 possible stems — used so all 6 slots
# are always visible (unavailable ones shown greyed with an "NA" pad label)
# instead of the stem list reflowing/narrowing when the model changes.
ALL_STEMS = ["drums", "bass", "vocals", "other", "piano", "guitar"]

# Per-stem ON/OFF pad fine-tuning — nudges each of the six selection
# buttons independently (position + size) so they can be lined up exactly
# with the baked button art in the skin, without touching the shared
# layout math the rest of the STEMS column is built from. Index order
# matches ALL_STEMS (drums, bass, vocals, other, piano, guitar); units are
# px at scale 1.0. Racks.py's hit-test imports and applies these same four
# lists so clicks always land on what's actually drawn.
STEM_PAD_X_OFFSET = [-3, -2, -1, -2, -3, -3]              # shifts the pad left/right
STEM_PAD_Y_OFFSET = [3.0, 3.0, 3.0, 3.0, 3.0, 3.0]              # shifts the pad up/down
STEM_PAD_W_SCALE  = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]  # multiplies pad width
STEM_PAD_H_SCALE  = [0.99, 0.99, 0.99, 0.99, 0.99, 0.99]  # multiplies pad height

# Same fine-tuning for the four MODEL selector buttons — index order matches
# MODELS (htdemucs, htdemucs_ft, htdemucs_6s, mdx_extra); units are px at
# scale 1.0. Racks.py's hit-test imports and applies these same four lists.
MODEL_BTN_X_OFFSET = [-3, -3, -3, -3]
MODEL_BTN_Y_OFFSET = [1, 1.5, 2, 4]
MODEL_BTN_W_SCALE  = [1.0, 1.0, 1.0, 1.0]
MODEL_BTN_H_SCALE  = [0.9, 0.9, 0.9, 0.9]

# ---------------------------------------------------------------------------
# Dependency check — runs once in background, cached for session
# ---------------------------------------------------------------------------
_dm_dep_cache     = {"ok": False, "checked": False, "checking": False}
_dm_check_running = False

_WARN_BG     = (0.02, 0.05, 0.02, 1.0)
_WARN_BORDER = (0.15, 0.55, 0.15, 1.0)
_WARN_TEXT   = (0.30, 0.90, 0.35, 1.0)
_WARN_DIM    = (0.15, 0.50, 0.20, 1.0)


def _run_dep_check():
    global _dm_check_running
    ok = False
    try:
        import subprocess, platform
        from core.ai_python_finder import (
            _win_python_paths, _mac_python_paths, _linux_python_paths)
        sys_name = platform.system()
        candidates = (_win_python_paths() if sys_name == "Windows"
                      else _mac_python_paths() if sys_name == "Darwin"
                      else _linux_python_paths())
        for cmd in candidates:
            try:
                r = subprocess.run(
                    cmd + ["-m", "pip", "show", "demucs"],
                    capture_output=True, timeout=5)
                if r.returncode == 0:
                    ok = True
                    break
            except Exception:
                continue
    except Exception as e:
        print(f"[DEMUCS] dep check error: {e}")
    _dm_dep_cache["ok"]       = ok
    _dm_dep_cache["checked"]  = True
    _dm_dep_cache["checking"] = False
    _dm_check_running             = False
    print(f"[DEMUCS] dep check complete: {'found' if ok else 'not found'}")
    try:
        import bpy
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ('NODE_EDITOR', 'SEQUENCE_EDITOR'):
                    area.tag_redraw()
    except Exception:
        pass

def _check_demucs():
    global _dm_check_running
    if not _dm_dep_cache["checked"] and not _dm_check_running:
        _dm_check_running = True
        _dm_dep_cache["checking"] = True
        import threading
        threading.Thread(target=_run_dep_check, daemon=True).start()
    return _dm_dep_cache["ok"]


def _draw_setup_warning(rx, ry, rw, rh, scale):
    """Draw the 'demucs not installed' warning panel."""
    sh       = gpu.shader.from_builtin("UNIFORM_COLOR")
    rail_h   = RACK_RAIL_H * scale
    body_bot = ry
    body_top = ry + rh - rail_h
    body_h   = body_top - body_bot
    margin   = 8 * scale

    _draw_rect(rx, body_bot, rw, body_h, _BG)
    bvs = [(rx, body_bot), (rx+rw, body_bot),
           (rx+rw, body_top), (rx, body_top), (rx, body_bot)]
    bb = batch_for_shader(sh, "LINE_STRIP", {"pos": bvs})
    sh.bind(); sh.uniform_float("color", _BORDER); bb.draw(sh)

    warn_w = rw - margin * 4
    warn_h = min(body_h * 0.82, 165 * scale)
    warn_x = rx + (rw - warn_w) / 2
    warn_y = body_bot + (body_h - warn_h) / 2

    _draw_rect(warn_x, warn_y, warn_w, warn_h, _WARN_BG)
    _draw_rect(warn_x, warn_y, 4 * scale, warn_h, _WARN_TEXT)
    wvs = [(warn_x, warn_y), (warn_x+warn_w, warn_y),
           (warn_x+warn_w, warn_y+warn_h), (warn_x, warn_y+warn_h), (warn_x, warn_y)]
    wb = batch_for_shader(sh, "LINE_STRIP", {"pos": wvs})
    sh.bind(); sh.uniform_float("color", _WARN_BORDER); wb.draw(sh)

    tx   = warn_x + 12 * scale
    fs_h = max(1, int(9 * scale))
    fs_b = max(1, int(8 * scale))
    fs_s = max(1, int(7 * scale))
    lh   = fs_b + 5 * scale
    ty   = warn_y + warn_h - fs_h - 8 * scale

    _draw_text("DEMUCS NOT FOUND — REQUIRES SETUP", tx, ty, fs_h, _WARN_TEXT)
    ty -= lh * 1.4
    _draw_text("Stem separation needs Demucs + ffmpeg in your system Python.", tx, ty, fs_b, _WARN_DIM)
    ty -= lh
    _draw_text("Open a terminal and run:", tx, ty, fs_b, _WARN_DIM)
    ty -= lh * 1.2

    cmd_w = warn_w - 24 * scale
    cmd_h = lh * 3.2 + 6 * scale
    cmd_x = tx
    cmd_y = ty - cmd_h
    _draw_rect(cmd_x, cmd_y, cmd_w, cmd_h, (0.01, 0.04, 0.01, 1.0))
    cvs = [(cmd_x, cmd_y), (cmd_x+cmd_w, cmd_y),
           (cmd_x+cmd_w, cmd_y+cmd_h), (cmd_x, cmd_y+cmd_h), (cmd_x, cmd_y)]
    cb = batch_for_shader(sh, "LINE_STRIP", {"pos": cvs})
    sh.bind(); sh.uniform_float("color", (0.10, 0.40, 0.12, 1.0)); cb.draw(sh)
    _draw_text("py -3.12 -m pip install demucs",
               cmd_x + 6 * scale, cmd_y + cmd_h - lh*1.2, fs_b, _WARN_TEXT)
    _draw_text("winget install Gyan.FFmpeg",
               cmd_x + 6 * scale, cmd_y + cmd_h - lh*2.4, fs_b, _WARN_TEXT)
    _draw_text("(then copy ffprobe.exe to your Python folder if ffmpeg alias is broken)",
               cmd_x + 6 * scale, cmd_y + 4*scale, max(1, int(6*scale)), _WARN_DIM)

    ty = cmd_y - lh * 1.2
    _draw_text("Models (~80-400MB) auto-download on first run. Restart Blender after install.",
               tx, ty, fs_s, _WARN_DIM)
    ty -= lh
    _draw_text("Requires ffprobe accessible from Python. CUDA recommended for large files.",
               tx, ty, fs_s, _WARN_DIM)

    btn_w = min(140 * scale, warn_w * 0.38)
    btn_h = max(16 * scale, fs_s + 8 * scale)
    btn_x = warn_x + warn_w - btn_w - 12 * scale
    btn_y = warn_y + 8 * scale
    _draw_rect(btn_x, btn_y, btn_w, btn_h, (0.02, 0.08, 0.03, 1.0))
    bvs2 = [(btn_x, btn_y), (btn_x+btn_w, btn_y),
            (btn_x+btn_w, btn_y+btn_h), (btn_x, btn_y+btn_h), (btn_x, btn_y)]
    bb2 = batch_for_shader(sh, "LINE_STRIP", {"pos": bvs2})
    sh.bind(); sh.uniform_float("color", _WARN_BORDER); bb2.draw(sh)
    fs_btn = max(1, int(7 * scale))
    lbl    = "OPEN SETUP GUIDE  >"
    tw_btn = _text_width(lbl, fs_btn)
    _draw_text(lbl, btn_x + btn_w/2 - tw_btn/2,
               btn_y + btn_h/2 - fs_btn/2, fs_btn, _WARN_TEXT)
    return btn_x, btn_y, btn_w, btn_h


def _get_free_channels(exclude=None):
    try:
        scene = bpy.context.scene
        if not scene or not scene.sequence_editor:
            return []
        used = {s.channel for s in scene.sequence_editor.sequences_all
                if s.type == "SOUND" and s.sound}
        if exclude:
            used.discard(exclude)
        return [ch for ch in range(1, 33) if ch not in used][:6]
    except Exception:
        return []


def _box(shader, bx, by, bw, bh, col):
    bvs = [(bx, by), (bx+bw, by), (bx+bw, by+bh), (bx, by+bh), (bx, by)]
    bb = batch_for_shader(shader, "LINE_STRIP", {"pos": bvs})
    shader.bind(); shader.uniform_float("color", col); bb.draw(shader)


def _draw_demucs_body(rx, ry, rw, rh, rack, ai_idx, scale):
    # Full-rack photoreal skin — when present, Racks.py's _draw_ai_rack_expanded
    # has already blit the whole unit (rail + body) before calling this
    # function, so the flat panel fills/borders/static labels below are
    # skipped entirely and only dynamic content (selection highlights, live
    # values, status text) draws on top at the same coordinates. Falls back
    # to the old flat panel look if the PNG isn't found yet — same convention
    # as rack_whisper.py's / rack_knnvc.py's / rack_voicefixer.py's
    # _has_skin gating.
    # (Replaces the old body-only draw_element() skin attempt, which predated
    # the full-unit blit Racks.py now does before dispatching here.)
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_dm
        _has_skin = _gtc_dm("rack_demucs_bg") is not None
    except Exception:
        _has_skin = False

    # Stem ON glow overlay — same convention as Racks.py's RackOn.png /
    # RackOff.png handling in _draw_ai_rack_expanded: the base skin already
    # bakes in the off/unlit look for every pad, and this small texture is
    # blit on top only for whichever stems are actually on. Looked up once
    # here rather than per-stem in the loop below.
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_dmon, blit_texture as _blt_dmon
        _stem_on_tex = _gtc_dmon("rack_demucs_stem_on")
    except Exception:
        _stem_on_tex, _blt_dmon = None, None

    # Dependency check — show warning panel if demucs not installed. Racks.py
    # withholds the full-unit skin blit while _check_demucs() hasn't passed
    # (mirrors the RVC/VoiceFixer/Whisper dep-check gating), so this always
    # draws its own flat chrome regardless of _has_skin.
    if not _check_demucs():
        bx, by, bw, bh = _draw_setup_warning(rx, ry, rw, rh, scale)
        try: rack['dm_setup_btn'] = (bx, by, bw, bh)
        except Exception: pass
        return

    # One-time init guard — runs exactly once per rack instance.
    # PB_AIRackSettings shares FloatProperty slots across all AI rack types,
    # so a new Demucs rack inherits whatever defaults Whisper left behind:
    #   p4=40 (font size), p6=2 (position), p7=1 (VAD on)
    # These look like valid channel numbers 1 and 2, so a simple range check
    # isn't enough. Instead we use a custom property flag 'dm_ch_init' that
    # is set to True once we've explicitly zeroed all channel output props.
    # After that the user's manual channel choices are never touched.
    if not rack.get('dm_ch_init'):
        for _prop in ("p4", "p5", "p6", "p7", "p8", "p9"):
            try:
                setattr(rack, _prop, 0.0)
            except Exception:
                pass
        try:
            rack['dm_ch_init'] = True
        except Exception:
            pass
    model_idx  = max(0, min(int(getattr(rack, "p0", 1.0)), len(MODELS) - 1))
    model      = MODELS[model_idx]
    mute_orig  = float(getattr(rack, "p2", 1.0)) > 0.5
    stem_bits  = int(getattr(rack, "p3", 15.0))
    n_active   = bin(stem_bits).count("1")
    free       = _get_free_channels()   # used by both the screen's SETTINGS
                                         # column and the strip under the stems

    # Which rack input channel this instance is attached to — read once here
    # so both the MODEL column's "IN CH n" echo and the screen's SETTINGS
    # column INPUT line can use the same value.
    in_ch_num = None
    try:
        active_chs = [ci for ci in range(9) if getattr(rack, f"ch{ci}", False)]
        if active_chs:
            in_ch_num = active_chs[0] + 1
    except Exception:
        pass

    status     = getattr(rack, "ai_status", "READY")
    if status not in ("READY", "PROCESSING", "DONE", "ERROR"):
        status = "READY"

    all_stems  = MODEL_STEMS[model]

    rail_h   = RACK_RAIL_H * scale
    body_bot = ry
    body_top = ry + rh - rail_h
    body_h   = body_top - body_bot
    shader   = gpu.shader.from_builtin("UNIFORM_COLOR")

    # Background + border — baked into the skin art once present.
    if not _has_skin:
        _draw_rect(rx, body_bot, rw, body_h, _BG)
        bv = [(rx, body_bot), (rx+rw, body_bot),
              (rx+rw, body_top), (rx, body_top), (rx, body_bot)]
        b  = batch_for_shader(shader, "LINE_STRIP", {"pos": bv})
        shader.bind(); shader.uniform_float("color", _BORDER); b.draw(shader)

    # Column layout — the rail's channel-assignment buttons (the numbered
    # 1-9 row) live in their own strip on the RAIL, a separate row above the
    # body (see Racks.py's _draw_ai_rack_expanded / _draw_ai_channel_buttons,
    # ch_area_x = rx+rw-100*scale there) — that's a different y-range from
    # the body entirely, so the body doesn't also need to reserve a matching
    # 100*scale gap on its own right edge. That reservation was leaving a
    # permanent unused strip the full height of the body (the border/bg
    # already spans the full rw, so the gap just sat there empty). Use the
    # full width.
    content_w = rw
    left_w    = content_w * 0.24
    right_w   = content_w * 0.22
    centre_w  = content_w - left_w - right_w
    centre_x  = rx + left_w
    right_x   = centre_x + centre_w

    # Dividers — baked into the skin art once present.
    if not _has_skin:
        for dx in (centre_x, right_x):
            dv = [(dx, body_bot+4*scale), (dx, body_top-4*scale)]
            db = batch_for_shader(shader, "LINES", {"pos": dv})
            shader.bind(); shader.uniform_float("color", (0.06, 0.18, 0.08, 1.0)); db.draw(shader)

    fs_lbl = max(1, int(7*scale))
    fs_sm  = max(1, int(8*scale))
    fs_med = max(1, int(9*scale))

    # ── LEFT: Model selector ──────────────────────────────────────────────────
    mx = rx + 5*scale
    mw = left_w - 10*scale
    if not _has_skin:
        _draw_text("MODEL", mx, body_top - fs_lbl - 4*scale, fs_lbl, _TEXT_DIM)

    # Read-only echo of which channel this rack is attached to — same
    # informational convention as Whisper's col1 INPUT CHANNEL row, just
    # compressed to one line since Demucs' left column has less room. Kept
    # unconditional — this is live per-rack state, not baked art.
    if in_ch_num is not None:
        in_ch_lbl = f"IN CH {in_ch_num}"
        tw_ic = _text_width(in_ch_lbl, fs_lbl)
        _draw_text(in_ch_lbl, mx + mw - tw_ic, body_top - fs_lbl - 4*scale,
                   fs_lbl, _TEXT_MUTED)

    model_btn_h = max(13*scale, (body_h * 0.60 / len(MODELS)) - 3*scale)
    models_top  = body_top - fs_lbl - 8*scale
    for mi, m in enumerate(MODELS):
        by_m   = models_top - (mi+1) * (model_btn_h + 3*scale)
        active = (mi == model_idx)
        bg     = (0.04, 0.14, 0.06, 1.0) if active else _PANEL
        col    = _GREEN if active else _GREEN_DIM
        # Per-model fine-tune — see MODEL_BTN_* constants near ALL_STEMS
        # above. Only this button's own box moves/scales.
        mbx = mx + MODEL_BTN_X_OFFSET[mi] * scale
        mby = by_m + MODEL_BTN_Y_OFFSET[mi] * scale
        mbw = mw * MODEL_BTN_W_SCALE[mi]
        mbh = model_btn_h * MODEL_BTN_H_SCALE[mi]
        # Fill suppressed once skinned — the button shapes are baked into
        # the art. The active row's border still draws on top so the
        # current selection stays visible live. The model name text is NOT
        # baked into this skin, so it always draws regardless.
        if not _has_skin:
            _draw_rect(mbx, mby, mbw, mbh, bg)
        if not _has_skin or active:
            _box(shader, mbx, mby, mbw, mbh, col)
        lbl_fs = max(1, int(7*scale))
        _draw_text(m, mbx+3*scale, mby+mbh/2-lbl_fs/2, lbl_fs, col)

    # Timing hint — replaces the removed PREVIEW/FULL toggle
    timing_lbl = {"htdemucs": "~1-2 min", "htdemucs_ft": "~2-4 min",
                  "htdemucs_6s": "~2-4 min", "mdx_extra": "~2-4 min"}.get(model, "~2-4 min")
    _draw_text(timing_lbl, mx, body_bot + 4*scale, max(1, int(6*scale)), _TEXT_MUTED)

    # ── CENTRE: status screen + per-stem blocks ───────────────────────────────
    # Layout (top to bottom): STEMS label, a fixed-ish-height status screen
    # (live summary / ANALYZING / DEMUCS COMPLETE — mirrors the design pass
    # that replaced the old inline stem rows), one block per stem sized from
    # whatever room is left (name, big ON pad, channel stepper directly below
    # it), then a small FREE CHANNELS digital readout sitting right under the
    # stems it describes, then the 6s hint. Capping the screen's height is the
    # fix for the earlier pass where it grew to fill the column and crushed
    # the stem blocks down to nothing.
    cx = centre_x + 5*scale
    cw = centre_w - 10*scale

    if not _has_skin:
        _draw_text("STEMS", cx, body_top - fs_lbl - 4*scale, fs_lbl, _TEXT_DIM)

    scr_y_top = body_top - fs_lbl - 8*scale
    # Sized to match the approved design pass (screen ~40% of the column,
    # stem blocks getting a comparable share below) rather than the earlier,
    # much-too-small cap that let the stem blocks balloon to fill the rest.
    scr_h     = min(140*scale, body_h * 0.40)
    scr_y     = scr_y_top - scr_h
    # Screen container suppressed once skinned — baked into the art as the
    # glass "screen" cutout; the live text drawn into it below still draws
    # on top either way.
    if not _has_skin:
        _draw_rect(cx, scr_y, cw, scr_h, (0.012, 0.05, 0.025, 1.0))
        _box(shader, cx, scr_y, cw, scr_h, _GREEN_DIM)

    # Screen is split in two: LEFT talks to the user (what to do next / what's
    # happening right now), RIGHT is a live readout of the actual settings
    # (model, per-stem channel, free channels, mute) — so the screen reads as
    # an interface giving feedback rather than a static caption.
    pad_scr   = 8*scale
    div_x     = cx + cw/2
    left_x0   = cx + pad_scr
    left_w_s  = div_x - left_x0 - 6*scale
    right_x0  = div_x + 6*scale

    if not _has_skin:
        dvv = [(div_x, scr_y + 4*scale), (div_x, scr_y + scr_h - 4*scale)]
        dvb = batch_for_shader(shader, "LINES", {"pos": dvv})
        shader.bind(); shader.uniform_float("color", (0.05, 0.20, 0.09, 1.0)); dvb.draw(shader)

    fs_head  = max(1, int(8*scale))
    fs_body  = fs_lbl
    line_h_l = fs_body + 5*scale
    ly       = scr_y + scr_h - pad_scr - fs_head

    # All text drawn into the screen (both halves) uses the same bright
    # green as the MODEL/CHANNELS AVAILABLE lines — the dimmer variants read
    # fine against the old flat black background but disappear against the
    # skin's screen glass, so nothing on the screen uses _TEXT_DIM/_TEXT_MUTED
    # any more. Progress on the 3-step list below is now shown by font size
    # (done steps are drawn larger) rather than by dimming, since colour is
    # no longer available as a second channel for that.
    if status == "PROCESSING":
        t_now    = time.time()
        pulse    = 0.5 + 0.5 * math.sin(t_now * 4.0)
        head_col = (_GREEN[0], _GREEN[1] * 0.5 + 0.5 * pulse, _GREEN[2], 1.0)
        _draw_text("ANALYZING…", left_x0, ly, fs_head, head_col)
        ly -= line_h_l
        _draw_text("This can take a minute or two.", left_x0, ly, fs_body, _GREEN)
        ly -= line_h_l + 4*scale
        bar_h = 3*scale
        _draw_rect(left_x0, ly, left_w_s, bar_h, (0.04, 0.15, 0.06, 1.0))
        fill_pulse = 0.4 + 0.6 * abs(math.sin(t_now * 1.5))
        _draw_rect(left_x0, ly, left_w_s * fill_pulse, bar_h, _GREEN)
    elif status == "DONE":
        _draw_text("DEMUCS COMPLETE", left_x0, ly, fs_head, _GREEN)
        ly -= line_h_l
        _draw_text("Stems placed on the timeline.", left_x0, ly, fs_body, _GREEN)
    elif status == "ERROR":
        _draw_text("SOMETHING WENT WRONG", left_x0, ly, fs_head, _RED)
        ly -= line_h_l
        _draw_text("Check the console, then retry.", left_x0, ly, fs_body, _GREEN)
    else:
        # Fixed 3-step walkthrough, in the order the user actually needs to
        # work through the rack.
        #   step 1 — an input channel has been picked
        #   step 2 — a model is always selected (there's a default, no
        #            separate "unset" state to track) so this is considered
        #            done as soon as step 1 is, i.e. once you've gotten this
        #            far there's nothing left outstanding on this step
        #   step 3 — at least one stem has been turned on
        step1_done = in_ch_num is not None
        step2_done = step1_done
        step3_done = n_active > 0

        _draw_text("1. Select input channel from the rack inputs.",
                   left_x0, ly, fs_head if step1_done else fs_body, _GREEN)
        ly -= line_h_l
        _draw_text("2. Pick a model.", left_x0, ly,
                   fs_head if step2_done else fs_body, _GREEN)
        ly -= line_h_l
        _draw_text("3. Pick which stems to split.", left_x0, ly,
                   fs_head if step3_done else fs_body, _GREEN)

    # RIGHT half — live settings readout. Line count varies with how many
    # stems are on, so the line height auto-shrinks to always fit rather
    # than risking overflow past the screen's bottom edge.
    sy = scr_y + scr_h - pad_scr - fs_lbl
    if not _has_skin:
        _draw_text("SETTINGS", right_x0, sy, fs_lbl, _TEXT_DIM)
    sy -= fs_lbl + 4*scale

    # Same bright green throughout — see the note above the LEFT-half text;
    # _TEXT_DIM/_TEXT_MUTED disappear against the skin's screen glass.
    input_str = f"CH {in_ch_num}" if in_ch_num is not None else "NOT SET"
    settings_lines = [
        (f"MODEL   {model.upper()}", _GREEN),
        (f"INPUT   {input_str}", _GREEN),
    ]
    enabled_stems = [s for s in ALL_STEMS
                      if s in all_stems and bool((stem_bits >> STEM_BITS[s]) & 1)]
    if enabled_stems:
        for s in enabled_stems:
            sprop = STEM_CH_PROPS.get(s)
            sval  = int(getattr(rack, sprop, 0.0)) if sprop else 0
            sdisp = f"CH {sval}" if sval > 0 else "AUTO"
            settings_lines.append((f"{STEM_ICONS[s]}  →  {sdisp}", _GREEN))
    else:
        settings_lines.append(("No stems selected yet", _GREEN))
    free_str_s = ", ".join(str(c) for c in free[:6]) if free else "NONE"
    settings_lines.append((f"CHANNELS AVAILABLE   {free_str_s}", _GREEN))
    settings_lines.append((f"MUTE    {'ON' if mute_orig else 'OFF'}", _GREEN))

    avail_h_s = max(1.0, sy - (scr_y + pad_scr))
    line_h_r  = min(fs_lbl + 4*scale, avail_h_s / len(settings_lines))
    fs_r      = max(1, min(fs_lbl, int(line_h_r - 2*scale)))
    ry = sy
    for text, col in settings_lines:
        _draw_text(text, right_x0, ry - fs_r, fs_r, col)
        ry -= line_h_r

    # Budget: screen, stem blocks, free-channels strip and the 6s hint all
    # have to fit between the STEMS label and the bottom of the body — the
    # stem blocks get whatever is left after the other three are reserved.
    free_h = max(14*scale, min(body_h * 0.075, 30*scale))
    hint_h = max(1, int(6*scale)) + 6*scale
    gap_a  = gap_b = gap_c = 4*scale

    stems_block_top = scr_y - gap_a
    stems_block_h   = max(30*scale, stems_block_top -
                           (body_bot + hint_h + gap_c + free_h))
    stems_block_bot = stems_block_top - stems_block_h

    # Always lay out all 6 stem slots, regardless of what the current model
    # supports — unavailable ones just show greyed out with an "NA" pad
    # label below, instead of the row reflowing to 4 wider blocks.
    n_slots  = len(ALL_STEMS)
    gap_stem = 4*scale
    block_w  = (cw - (n_slots - 1) * gap_stem) / n_slots
    name_h   = fs_lbl + 2*scale
    step_h   = max(14*scale, fs_lbl + 8*scale)
    # Capped so the pad can't balloon to fill whatever's left of the column
    # (that's what made it look oversized before) — the screen above now
    # carries most of the extra room instead.
    pad_h    = max(20*scale, min(stems_block_h - name_h - step_h - 6*scale, 60*scale))

    for si, stem in enumerate(ALL_STEMS):
        bx2        = cx + si * (block_w + gap_stem)
        enabled    = bool((stem_bits >> STEM_BITS[stem]) & 1)
        available  = stem in all_stems

        if not available:
            name_col, pad_bg, pad_col, on_lbl = _TEXT_MUTED, _PANEL, _TEXT_MUTED, "NA"
        elif enabled:
            name_col, pad_bg, pad_col, on_lbl = _GREEN, (0.04, 0.18, 0.07, 1.0), _GREEN, "ON"
        else:
            name_col, pad_bg, pad_col, on_lbl = _GREEN_DIM, _PANEL, _GREEN_DIM, "OFF"

        # Stem name is a fixed caption (same 6, same order, always) — baked
        # into the skin art once present. The ON/OFF/NA pad label is the one
        # thing that actually carries live state a static image can't show,
        # so it always draws. The pad's fill is suppressed once skinned
        # (don't paint over the art), and now that the off/unlit look is
        # baked into the skin too, the placeholder border is only needed as
        # an unskinned fallback — the ON glow overlay plus the text label
        # carry the state once skinned, so the border is suppressed there.
        name_str = STEM_ICONS[stem]
        tw_n     = _text_width(name_str, fs_lbl)
        name_y   = stems_block_top - name_h + 1*scale
        if not _has_skin:
            _draw_text(name_str, bx2 + block_w/2 - tw_n/2, name_y, fs_lbl, name_col)

        pad_y = name_y - 2*scale - pad_h
        # Per-stem fine-tune — see STEM_PAD_* constants near ALL_STEMS above.
        # Only the pad box itself moves/scales; the stepper below it stays
        # anchored off the un-offset pad_y so nudging one pad's alignment
        # doesn't drag its stepper out of place.
        pad_bx = bx2 + STEM_PAD_X_OFFSET[si] * scale
        pad_by = pad_y + STEM_PAD_Y_OFFSET[si] * scale
        pad_bw = block_w * STEM_PAD_W_SCALE[si]
        pad_bh = pad_h * STEM_PAD_H_SCALE[si]
        if not _has_skin:
            _draw_rect(pad_bx, pad_by, pad_bw, pad_bh, pad_bg)
        # Glowing ON art takes over for available+enabled pads now that it
        # exists — blit on top of the base (unlit) skin at the same tuned
        # rect as the placeholder box, instead of drawing the box. OFF/NA
        # pads need no overlay at all — that look is baked into the skin —
        # so the placeholder border only draws as an unskinned fallback.
        show_on_tex = _stem_on_tex is not None and available and enabled
        if show_on_tex:
            _blt_dmon(_stem_on_tex, pad_bx, pad_by, pad_bw, pad_bh,
                      key="rack_demucs_stem_on")
        elif not _has_skin:
            _box(shader, pad_bx, pad_by, pad_bw, pad_bh, pad_col)
        tw_on = _text_width(on_lbl, fs_med)
        _draw_text(on_lbl, pad_bx+pad_bw/2-tw_on/2, pad_by+pad_bh/2-fs_med/2, fs_med, pad_col)

        # Channel stepper — directly below the pad now (was to the right of
        # a level bar in the old single-row layout). Stays visible, just
        # dimmed, while the stem is off so a channel can be pre-picked
        # before switching it on; Racks.py's click handlers don't gate on
        # `enabled` so this pre-picking actually works.
        prop    = STEM_CH_PROPS.get(stem)
        ch_val  = int(getattr(rack, prop, 0.0)) if prop else 0
        ch_str  = f"ch{ch_val}" if ch_val > 0 else "auto"
        st_y    = pad_y - 2*scale - step_h
        st_col  = _TEXT_MUTED if not available else (_GREEN_MID if enabled else _GREEN_DIM)
        # The channel value itself needs to stay legible even while the stem
        # is off (that's the whole point of letting it be pre-picked before
        # switching on) — _GREEN_DIM read as too dim once the skin was in
        # place, so it's brightened to the same green used everywhere else.
        val_col = _TEXT_MUTED if not available else _GREEN
        arr_w   = min(16*scale, block_w * 0.28)
        val_w   = block_w - arr_w * 2

        # Stepper chrome (the three boxes) AND the +/− glyphs themselves are
        # baked into the art once skinned — only the live channel value in
        # the middle can't be baked (it changes per-rack), so that's the
        # only piece of this row that still draws unconditionally.
        if not _has_skin:
            _draw_rect(bx2, st_y, arr_w, step_h, _PANEL)
            _box(shader, bx2, st_y, arr_w, step_h, st_col)
            _draw_text("−", bx2+arr_w/2-_text_width("−", fs_sm)/2,
                       st_y+step_h/2-fs_sm/2, fs_sm, st_col)

        if not _has_skin:
            _draw_rect(bx2+arr_w, st_y, val_w, step_h, _PANEL)
            _box(shader, bx2+arr_w, st_y, val_w, step_h, st_col)
        tw_ch = _text_width(ch_str, fs_lbl)
        _draw_text(ch_str, bx2+arr_w+val_w/2-tw_ch/2, st_y+step_h/2-fs_lbl/2, fs_lbl, val_col)

        if not _has_skin:
            _draw_rect(bx2+arr_w+val_w, st_y, arr_w, step_h, _PANEL)
            _box(shader, bx2+arr_w+val_w, st_y, arr_w, step_h, st_col)
            _draw_text("+", bx2+arr_w+val_w+arr_w/2-_text_width("+", fs_sm)/2,
                       st_y+step_h/2-fs_sm/2, fs_sm, st_col)

    # Free channels — its own small digital readout, sitting right under the
    # stem blocks it's relevant to (moved out of the OPTIONS column, which
    # was only ever about mute/run, not about which channels are free).
    # `free` was already computed near the top of the function.
    free_y = stems_block_bot - gap_b - free_h
    if not _has_skin:
        _draw_rect(cx, free_y, cw, free_h, (0.012, 0.05, 0.025, 1.0))
        _box(shader, cx, free_y, cw, free_h, (0.05, 0.20, 0.09, 1.0))
    fs_free  = max(1, int(7*scale))
    free_str = "FREE CH:  " + ("  ".join(str(c) for c in free[:6]) if free else "NONE")
    _draw_text(free_str, cx + 6*scale, free_y + free_h/2 - fs_free/2, fs_free, _GREEN)

    # ── RIGHT: Options + status + run button ──────────────────────────────────
    rx2 = right_x + 4*scale
    rw2 = right_w - 8*scale

    if not _has_skin:
        _draw_text("OPTIONS", rx2, body_top - fs_lbl - 4*scale, fs_lbl, _TEXT_DIM)

    # Mute original toggle — fill + caption suppressed once skinned (baked
    # into the art); the border still lights up live when actually on, and
    # the description line below it is likewise baked static copy.
    opt_y = body_top - fs_lbl - 10*scale - 14*scale
    opt_h = 13*scale
    mute_bg  = (0.04, 0.14, 0.06, 1.0) if mute_orig else _PANEL
    mute_col = _GREEN if mute_orig else _GREEN_DIM
    if not _has_skin:
        _draw_rect(rx2, opt_y, rw2, opt_h, mute_bg)
    if not _has_skin or mute_orig:
        _box(shader, rx2, opt_y, rw2, opt_h, mute_col)
    if not _has_skin:
        _draw_text("MUTE ORIGINAL", rx2+3*scale, opt_y+opt_h/2-max(1,int(6*scale))/2,
                   max(1, int(6*scale)), mute_col)
    desc_fs = max(1, int(6*scale))
    desc_y  = opt_y - desc_fs - 4*scale
    if not _has_skin:
        _draw_text("Silences the source once stems are split", rx2, desc_y, desc_fs, _TEXT_MUTED)

    # Run button — anchored to the bottom. Status / LAST RUN / info lines
    # below stack UPWARD from here (rather than down from a fixed offset
    # like before), so they can never end up drawn underneath it again.
    run_h = min(22*scale, body_h * 0.16)
    run_y = body_bot + 4*scale

    micro_fs = max(1, int(6*scale))
    cursor   = run_y + run_h + 8*scale

    # READY's LED dot is baked into the skin art now, so it's suppressed
    # once skinned (same "only draw the live states art can't show" pattern
    # as the SPLIT STEMS button below); PROCESSING/DONE/ERROR still need
    # their own live dot since the baked art can't show those. The status
    # word itself always draws — READY's colour was _GREEN_DIM, which read
    # too dim once skinned, so it's brightened to the same green used
    # everywhere else on the rack.
    dot_cols = {"READY": _GREEN, "PROCESSING": _AMBER,
                "DONE": _GREEN, "ERROR": _RED}
    status_y = cursor
    if not _has_skin or status != "READY":
        _draw_circle(rx2 + 4*scale, status_y + 4*scale, 4*scale,
                     dot_cols.get(status, _GREEN))
    _draw_text(status, rx2 + 12*scale, status_y, fs_sm,
               dot_cols.get(status, _GREEN))
    cursor = status_y + fs_sm + 6*scale

    if not _has_skin:
        _draw_text("LAST RUN", rx2, cursor, micro_fs, _TEXT_MUTED)
    cursor += micro_fs + 2*scale
    if status == "DONE":
        _draw_text("✓ Stems split to timeline", rx2, cursor, micro_fs, _GREEN)
    elif status == "ERROR":
        _draw_text("✗ Failed — check console", rx2, cursor, micro_fs, _RED)
    else:
        _draw_text("No output yet", rx2, cursor, micro_fs, _TEXT_MUTED)
    cursor += micro_fs + 6*scale

    # "n stems on" moved into the screen's SETTINGS column (it's listed
    # there per-stem now, which is more useful than a bare count) — kept
    # here is just the one line that doesn't belong anywhere else. Purely
    # static copy, so it's suppressed once baked into the skin art.
    if not _has_skin:
        _draw_text("CPU/GPU auto", rx2, cursor, micro_fs, _TEXT_MUTED)
    status_cluster_top = cursor + micro_fs

    # ── ABOUT panel — fills the room between the mute description and the
    # status cluster above, instead of leaving it blank (that gap was the
    # "big chunk of unused space" on this side). Its height is whatever's
    # actually left, so it can't under- or over-shoot.
    about_top = desc_y - 10*scale
    about_bot = status_cluster_top + 10*scale
    about_h   = about_top - about_bot
    if about_h > 20*scale:
        # Panel chrome suppressed once skinned — baked in as the "screen"
        # cutout; the lines inside are model-dependent (MODEL_DESC, timing)
        # so they stay live either way.
        if not _has_skin:
            _draw_rect(rx2, about_bot, rw2, about_h, (0.012, 0.05, 0.025, 1.0))
            _box(shader, rx2, about_bot, rw2, about_h, (0.05, 0.20, 0.09, 1.0))
        about_fs    = max(1, int(6*scale))
        about_lh    = about_fs + 4*scale
        about_lines = [
            MODEL_DESC.get(model, ""),
            "Runs locally — no audio leaves this machine.",
            f"Typically {timing_lbl}.",
        ]
        about_block_h = len(about_lines) * about_lh
        about_y = about_bot + about_h/2 + about_block_h/2 - about_fs
        for line in about_lines:
            if line:
                _draw_text(line, rx2 + 6*scale, about_y, about_fs, _TEXT_DIM)
            about_y -= about_lh

    if status == "PROCESSING":
        pulse   = 0.5 + 0.5 * math.sin(time.time() * 4.0)
        run_bg  = (0.04, 0.14, 0.06, 1.0)
        run_col = (_GREEN[0], _GREEN[1] * 0.5 + 0.5 * pulse, _GREEN[2], 1.0)
        run_lbl = "SPLITTING..."
    elif status == "DONE":
        run_bg  = (0.02, 0.10, 0.04, 1.0)
        run_col = _GREEN
        run_lbl = "DONE  (run again)"
    elif status == "ERROR":
        run_bg  = (0.14, 0.02, 0.02, 1.0)
        run_col = _RED
        run_lbl = "ERROR — RETRY"
    else:
        run_bg  = (0.03, 0.12, 0.05, 1.0)
        run_col = _GREEN
        run_lbl = "▶  SPLIT STEMS"

    # Fill suppressed once skinned — the idle "SPLIT STEMS" look, label
    # included, is baked into the art. Once status moves off READY the
    # border re-appears on top in the status colour, and the label draws
    # too, since the baked art can't show SPLITTING.../DONE/ERROR text.
    if not _has_skin:
        _draw_rect(rx2, run_y, rw2, run_h, run_bg)
    if not _has_skin or status != "READY":
        _box(shader, rx2, run_y, rw2, run_h, run_col)
    if not _has_skin or status != "READY":
        fs_run = max(1, int(8*scale))
        tw_run = _text_width(run_lbl, fs_run)
        _draw_text(run_lbl, rx2+rw2/2-tw_run/2, run_y+run_h/2-fs_run/2, fs_run, run_col)