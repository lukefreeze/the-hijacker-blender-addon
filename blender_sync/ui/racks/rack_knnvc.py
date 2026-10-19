# =============================================================================
# ui/racks/rack_rvc.py
# kNN-VC Voice Conversion rack — system Python / PyTorch dependent (BETA).
#
# New features:
#   - Reference voice cards have ▶ preview button each
#   - Centre panel shows useful state info instead of empty waveform box
#   - "ADD FROM TIMELINE" section with separate channel selector + name input
#   - All audio preview via core.audio.play_oneshot (Hijacker engine device)
# =============================================================================

import os
import math
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

_BG          = (0.04,  0.02,  0.06,  1.0)
_BORDER      = (0.22,  0.08,  0.30,  1.0)
_PANEL       = (0.03,  0.01,  0.04,  1.0)
_PANEL_SEL   = (0.12,  0.04,  0.18,  1.0)
_ACCENT      = (0.67,  0.27,  0.87,  1.0)
_ACCENT_DIM  = (0.34,  0.10,  0.44,  1.0)
_TEXT        = (0.80,  0.55,  0.95,  1.0)
_TEXT_DIM    = (0.38,  0.16,  0.50,  1.0)
_TEXT_LABEL  = (0.25,  0.08,  0.32,  1.0)
_WARN_BG     = (0.08,  0.05,  0.00,  1.0)
_WARN_BORDER = (0.55,  0.33,  0.00,  1.0)
_WARN_TEXT   = (0.86,  0.53,  0.00,  1.0)
_WARN_DIM    = (0.50,  0.28,  0.00,  1.0)
_GREEN       = (0.05,  0.80,  0.30,  1.0)
_AMBER       = (0.86,  0.53,  0.00,  1.0)

RACK_RAIL_H = 32

# =============================================================================
# CONTROLS PANEL KNOB TUNING
# The drawn knob (arc + pointer) is computed from the panel geometry, then
# scaled by these multipliers — independent per knob so TOPK and REF SECS
# can each be grown/shrunk to match their respective graphic in the skin
# art. 1.0 = original computed size (min(18*scale, right_w*0.28, work_h*0.18)).
# Mirrors rack_base.py's SB_KNOB_SCALE / rack_piper.py's *_W_SCALE pattern.
# =============================================================================
KNNVC_TOPK_KNOB_SCALE    = 1.7
KNNVC_REFSECS_KNOB_SCALE = 1.7

# Per-knob centre position nudge — unscaled px, + = right/up. Needed because
# growing a knob via the *_SCALE constants above only grows the arc/pointer
# around its existing centre; it doesn't re-centre it against the (larger)
# knob graphic baked into the skin art, so each knob needs its own nudge to
# land back on top of its artwork. Same convention as CH_BTN_LABEL_X_OFFSET
# in rack_base.py / PIPER_*_VALUE_X_OFFSET in rack_piper.py.
KNNVC_TOPK_KNOB_X_OFFSET    = -2.0
KNNVC_TOPK_KNOB_Y_OFFSET    = -1.0
KNNVC_REFSECS_KNOB_X_OFFSET = -4.0
KNNVC_REFSECS_KNOB_Y_OFFSET = 0.5

# Left-panel text nudges — unscaled px, + = right. "USE RAIL"/"CH X" is the
# SOURCE box value (src_disp, below); the per-channel digits are the small
# 1-9 buttons in the ADD FROM TIMELINE selector row — NOT the "CH1Sample"
# name-field text, which has its own KNNVC_ADD_NAME_X_OFFSET further down.
KNNVC_SOURCE_VALUE_X_OFFSET = 6.0   # "CH X" / "USE RAIL" readout, top of left panel
KNNVC_SRC_CH_LABEL_X_OFFSET = 0.0   # per-channel number, ADD FROM TIMELINE selector row

# Add-voice name field text nudge — unscaled px, + = right. Moves both the
# live value ("CH1Sample" default, or whatever's been typed) and the "enter
# name..." placeholder; the blinking cursor position is offset by the same
# amount so it stays aligned with the text it's drawn against.
KNNVC_ADD_NAME_X_OFFSET = 4.0

# ADD TO VOICES button label nudge — unscaled px, + = up/- = down.
KNNVC_ADD_BTN_LABEL_Y_OFFSET = -1.5

_READY      = "READY"
_NO_REF     = "NO_REF"
_PROCESSING = "PROCESSING"
_DONE       = "DONE"
_ERROR      = "ERROR"

# Dep check — background thread only, never on draw thread
#   checked  : a result exists (the draw code shows the warning, not the "checking" card, while False)
#   when     : time of the last result; a NEGATIVE result is re-checked after DEP_RECHECK_S so that a
#              check that failed for a transient reason (cold first import of torch is slow, antivirus
#              scanning, the user installing PyTorch meanwhile) cannot leave the warning up for good
_dep_cache         = {"ok": False, "checked": False, "checking": False, "when": 0.0}
_dep_check_running = False
DEP_TIMEOUT_S      = 60.0     # `import torch, torchaudio` can take well over 4 s on a cold start
DEP_RECHECK_S      = 15.0


def _redraw_editors():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ('NODE_EDITOR', 'SEQUENCE_EDITOR'):
                    area.tag_redraw()
    except Exception:
        pass


def _redraw_poll():
    """Main-thread timer: the check runs on a background thread, and Blender only reliably redraws from
    the main thread, so poll until the check has finished and then redraw once more."""
    _redraw_editors()
    return 0.3 if _dep_check_running else None


def _run_dep_check():
    global _dep_check_running
    ok = False
    try:
        # Single source of truth, shared with the actual runtime Python
        # selection in core/ai_knnvc.py's _find_system_python() — both
        # require torch AND torchaudio importable in the SAME interpreter.
        # This used to check "torch" alone (via a separate pip-show-based
        # scan duplicated here), which meant any Python on the system PATH
        # with torch installed for some unrelated reason — common on a
        # machine used for other AI/VFX tools — would make this rack claim
        # "ready" even though torchaudio was never installed and
        # processing would fail the moment it actually ran.
        from core.ai_python_finder import find_python_with as _fpw
        ok = _fpw(["torch", "torchaudio"], timeout=DEP_TIMEOUT_S) is not None
    except Exception as e:
        print(f"[KNNVC] dep check error: {e}")
    import time as _time
    changed = (not _dep_cache["checked"]) or (_dep_cache["ok"] != ok)
    _dep_cache["ok"]       = ok
    _dep_cache["when"]     = _time.time()
    _dep_cache["checked"]  = True
    _dep_cache["checking"] = False
    _dep_check_running     = False
    if changed:
        print(f"[KNNVC] dep check complete: {'found' if ok else 'not found'}")
    _redraw_editors()      # (the main-thread poll timer redraws again as soon as it sees the flag drop)

def _check_deps():
    global _dep_check_running
    import time as _time
    stale_negative = (_dep_cache["checked"] and not _dep_cache["ok"]
                      and _time.time() - _dep_cache["when"] > DEP_RECHECK_S)
    if (not _dep_cache["checked"] or stale_negative) and not _dep_check_running:
        _dep_check_running = True
        _dep_cache["checking"] = True
        import threading
        t = threading.Thread(target=_run_dep_check, daemon=True)
        t.start()
        try:
            if not bpy.app.timers.is_registered(_redraw_poll):
                bpy.app.timers.register(_redraw_poll, first_interval=0.3)
        except Exception:
            pass
    return _dep_cache["ok"]


def _get_voices_dir():
    addon_dir = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    return os.path.join(addon_dir, "ai_engines", "knnvc", "voices")


def _discover_ref_voices(ai_idx):
    """Return list of (display_name, wav_path) from ai_engines/knnvc/voices/."""
    try:
        voices_dir = _get_voices_dir()
        if not os.path.isdir(voices_dir):
            return []
        voices = []
        for f in sorted(os.listdir(voices_dir)):
            if f.lower().endswith(".wav"):
                name = os.path.splitext(f)[0].replace("_", " ").title()
                voices.append((name, os.path.join(voices_dir, f)))
        return voices
    except Exception:
        return []


def _draw_setup_warning(rx, ry, rw, rh, scale, has_skin=False):
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    rail_h   = RACK_RAIL_H * scale
    body_bot = ry
    body_top = ry + rh - rail_h
    body_h   = body_top - body_bot
    margin   = 8 * scale

    # Outer body fill/border — suppressed when the full-unit skin PNG is
    # already blit underneath by _draw_ai_rack_expanded (Racks.py). The
    # warning card itself always draws its own box below regardless of skin,
    # since it needs to read clearly as an alert no matter what art sits
    # behind it (same convention as rack_piper.py's has_skin gating).
    if not has_skin:
        _draw_rect(rx, body_bot, rw, body_h, _BG)
        bvs = [(rx,body_bot),(rx+rw,body_bot),(rx+rw,body_top),(rx,body_top),(rx,body_bot)]
        bb = batch_for_shader(sh,"LINE_STRIP",{"pos":bvs})
        sh.bind(); sh.uniform_float("color",_BORDER); bb.draw(sh)

    warn_w = rw - margin * 4
    warn_h = min(body_h * 0.78, 160 * scale)
    warn_x = rx + (rw - warn_w) / 2
    warn_y = body_bot + (body_h - warn_h) / 2

    _draw_rect(warn_x, warn_y, warn_w, warn_h, _WARN_BG)
    _draw_rect(warn_x, warn_y, 4*scale, warn_h, _WARN_TEXT)
    wvs = [(warn_x,warn_y),(warn_x+warn_w,warn_y),
           (warn_x+warn_w,warn_y+warn_h),(warn_x,warn_y+warn_h),(warn_x,warn_y)]
    wb = batch_for_shader(sh,"LINE_STRIP",{"pos":wvs})
    sh.bind(); sh.uniform_float("color",_WARN_BORDER); wb.draw(sh)

    tx   = warn_x + 12*scale
    fs_h = max(1, int(9*scale))
    fs_b = max(1, int(8*scale))
    fs_s = max(1, int(7*scale))
    lh   = fs_b + 5*scale
    ty   = warn_y + warn_h - fs_h - 8*scale

    _draw_text("PYTORCH NOT FOUND - BETA RACK REQUIRES SETUP", tx, ty, fs_h, _WARN_TEXT)
    ty -= lh * 1.4
    _draw_text("Voice conversion needs PyTorch + torchaudio in your system Python.", tx, ty, fs_b, _WARN_DIM)
    ty -= lh
    _draw_text("Open a terminal and run:", tx, ty, fs_b, _WARN_DIM)
    ty -= lh * 1.2

    cmd_w = warn_w - 24*scale
    cmd_h = lh * 1.6 + 6*scale
    cmd_x = tx; cmd_y = ty - cmd_h
    _draw_rect(cmd_x, cmd_y, cmd_w, cmd_h, (0.02,0.01,0.00,1.0))
    cvs = [(cmd_x,cmd_y),(cmd_x+cmd_w,cmd_y),(cmd_x+cmd_w,cmd_y+cmd_h),(cmd_x,cmd_y+cmd_h),(cmd_x,cmd_y)]
    cb = batch_for_shader(sh,"LINE_STRIP",{"pos":cvs})
    sh.bind(); sh.uniform_float("color",(0.44,0.24,0.00,1.0)); cb.draw(sh)
    _draw_text("pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126",
               cmd_x+6*scale, cmd_y+cmd_h/2-fs_b/2, fs_b, _WARN_TEXT)

    ty = cmd_y - lh * 1.2
    _draw_text("Models (~1.3GB) auto-download on first use. Restart Blender after install.", tx, ty, fs_s, _WARN_DIM)
    ty -= lh
    _draw_text("Use clean speech clips (10-30s) as reference. Avoid music/background noise.", tx, ty, fs_s, _WARN_DIM)

    try:
        from core import ai_pydeps as _pydeps
        _rv_lines, _rv_btn_lbl, _rv_kind = _pydeps.get_progress_display("knnvc")
    except Exception:
        _rv_lines, _rv_btn_lbl, _rv_kind = [], "INSTALL AUTOMATICALLY  >", "idle"

    _rv_status_y = cmd_y + 10 * scale
    if _rv_kind == "busy":
        for _i, _line in enumerate(_rv_lines):
            _draw_text(_line, tx, _rv_status_y + _i * (fs_s + 3*scale), fs_s, _WARN_TEXT)
        import time as _rv_time
        _bar_w = warn_w - 28 * scale
        _bar_h = max(3 * scale, 3)
        _bar_x = tx
        _bar_y = _rv_status_y + len(_rv_lines) * (fs_s + 3*scale) + 3*scale
        _draw_rect(_bar_x, _bar_y, _bar_w, _bar_h, (0.10, 0.07, 0.0, 1.0))
        _seg_w = _bar_w * 0.28
        _t     = (_rv_time.time() * 0.35) % 1.0
        _pos   = _t * (_bar_w + _seg_w) - _seg_w
        _seg_x = max(_bar_x, min(_bar_x + _bar_w - _seg_w, _bar_x + _pos))
        _draw_rect(_seg_x, _bar_y, min(_seg_w, _bar_x + _bar_w - _seg_x), _bar_h, _WARN_TEXT)
    elif _rv_kind == "error" and _rv_lines:
        _draw_text(_rv_lines[0], tx, _rv_status_y, fs_s, _WARN_TEXT)

    btn_w = min(170*scale, warn_w*0.44)
    btn_h = max(16*scale, fs_s+8*scale)
    btn_x = warn_x + warn_w - btn_w - 12*scale
    btn_y = warn_y + 8*scale
    if _rv_kind == "error":
        btn_bg, btn_edge = (0.10, 0.02, 0.02, 1.0), (1.0, 0.35, 0.3, 1.0)
    elif _rv_kind == "busy":
        btn_bg, btn_edge = (0.06, 0.04, 0.00, 1.0), _WARN_DIM
    else:
        btn_bg, btn_edge = (0.10,0.06,0.00,1.0), _WARN_BORDER
    lbl = _rv_btn_lbl
    _draw_rect(btn_x, btn_y, btn_w, btn_h, btn_bg)
    bvs2 = [(btn_x,btn_y),(btn_x+btn_w,btn_y),(btn_x+btn_w,btn_y+btn_h),(btn_x,btn_y+btn_h),(btn_x,btn_y)]
    bb2 = batch_for_shader(sh,"LINE_STRIP",{"pos":bvs2})
    sh.bind(); sh.uniform_float("color",btn_edge); bb2.draw(sh)
    fs_btn = max(1,int(7*scale))
    tw_btn = _text_width(lbl,fs_btn)
    _draw_text(lbl, btn_x+btn_w/2-tw_btn/2, btn_y+btn_h/2-fs_btn/2, fs_btn, _WARN_TEXT)
    if _rv_kind == "error" and _rv_lines:
        _draw_text(_rv_lines[0][:70], tx, btn_y + btn_h + 4*scale,
                    max(1, int(6*scale)), (1.0, 0.45, 0.4, 1.0))
    return btn_x, btn_y, btn_w, btn_h


def _start_knnvc_install():
    """CPU-compatible torch/torchaudio wheels — works everywhere without
    needing to detect the user's GPU/CUDA situation. The pinned-CUDA
    command shown above stays as a reference for anyone who wants to
    install a GPU build manually afterward."""
    from core import ai_pydeps as _pydeps

    if _pydeps.is_installing("knnvc"):
        _pydeps.cancel_install("knnvc")
        return

    def _on_done(success):
        _dep_cache["checked"] = False

    _pydeps.start_install(
        "knnvc",
        pip_specs=["torch", "torchaudio"],
        check_module=["torch", "torchaudio"],
        on_done=_on_done,
    )


def _draw_rvc_body(rx, ry, rw, rh, rack, ai_idx, scale):
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")

    # Full-rack photoreal skin — when present, Racks.py's _draw_ai_rack_expanded
    # has already blit the whole unit (rail + body) before calling this
    # function, so the flat panel fills/borders below are skipped entirely and
    # only dynamic content (state text, selection highlights, cursor, knob
    # values) draws on top. Falls back to the old flat panel look if the PNG
    # isn't found yet — same convention as rack_piper.py's _has_skin gating.
    # (Replaces the old body-only draw_element() skin attempt, which predates
    # the full-unit blit Racks.py now does before dispatching here.)
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_rvc
        _has_skin = _gtc_rvc("rack_knnvc_bg") is not None
    except Exception:
        _has_skin = False

    # Deps not ready yet — Racks.py's _draw_ai_rack_expanded withholds the
    # full-unit skin blit in this state (see its RVC-specific check right
    # before the blit), so even though the skin texture may already be
    # loaded in cache, nothing has actually been drawn behind us. Force
    # has_skin=False here so the warning card draws its own flat black body
    # chrome instead of assuming art is already sitting underneath it.
    if not _check_deps():
        if not _dep_cache["checked"]:
            # First check still running: say so, instead of flashing the "PYTORCH NOT FOUND" warning
            rail_h = RACK_RAIL_H * scale
            body_h = rh - rail_h
            _draw_rect(rx, ry, rw, body_h, _BG)
            fs = max(1, int(8 * scale))
            msg = "Checking PyTorch installation..."
            _draw_text(msg, rx + rw / 2 - _text_width(msg, fs) / 2, ry + body_h / 2 - fs / 2, fs, _TEXT_DIM)
            return
        bx,by,bw,bh = _draw_setup_warning(rx, ry, rw, rh, scale, has_skin=False)
        try: rack['rvc_setup_btn'] = (bx,by,bw,bh)
        except Exception: pass
        return

    rail_h   = RACK_RAIL_H * scale
    body_bot = ry
    body_top = ry + rh - rail_h
    body_h   = body_top - body_bot
    margin   = 8 * scale

    if not _has_skin:
        _draw_rect(rx, body_bot, rw, body_h, _BG)
        bvs = [(rx,body_bot),(rx+rw,body_bot),(rx+rw,body_top),(rx,body_top),(rx,body_bot)]
        bb = batch_for_shader(sh,"LINE_STRIP",{"pos":bvs})
        sh.bind(); sh.uniform_float("color",_BORDER); bb.draw(sh)

    sbar_h   = max(16*scale, body_h*0.07)
    sbar_y   = body_bot
    sbar_x   = rx + margin
    sbar_w   = rw - margin*2

    left_w   = rw * 0.30
    right_w  = rw * 0.24
    centre_w = rw - left_w - right_w - margin*4
    left_x   = rx + margin
    centre_x = left_x + left_w + margin
    right_x  = centre_x + centre_w + margin
    work_bot = sbar_y + sbar_h + 2*scale
    work_top = body_top - 2*scale
    work_h   = work_top - work_bot
    fs_lbl   = max(1, int(7*scale))
    fs_sm    = max(1, int(7*scale))
    status   = getattr(rack, "ai_status", _READY)

    # ── LEFT PANEL: source info + voice cards + add from timeline ─────────────
    lx = left_x; ly = work_bot; lh = work_h
    if not _has_skin:
        _draw_rect(lx, ly, left_w, lh, _PANEL)
        lvs = [(lx,ly),(lx+left_w,ly),(lx+left_w,ly+lh),(lx,ly+lh),(lx,ly)]
        lb = batch_for_shader(sh,"LINE_STRIP",{"pos":lvs})
        sh.bind(); sh.uniform_float("color",_BORDER); lb.draw(sh)

    # Source channel display (assigned from rail)
    # "SOURCE" label suppressed once skinned — expected to be baked into
    # rack_knnvc_bg.png like Piper's "SCRIPT"/"VOICE" labels were.
    if not _has_skin:
        _draw_text("SOURCE", lx+5*scale, ly+lh-fs_lbl-4*scale, fs_lbl, _TEXT_LABEL)
    active_chs = [ci+1 for ci in range(9) if getattr(rack, f"ch{ci}", False)]
    src_disp   = f"CH {active_chs[0]}" if active_chs else "USE RAIL"
    fs_ch      = max(1, int(7*scale))
    _draw_text(src_disp, lx+5*scale + KNNVC_SOURCE_VALUE_X_OFFSET*scale,
               ly+lh-fs_lbl-fs_ch-10*scale, fs_ch,
               _ACCENT if active_chs else _TEXT_DIM)

    # ── ADD FROM TIMELINE section (bottom of left panel) ──────────────────
    add_section_h = min(work_h * 0.38, 90*scale)
    add_y         = ly
    add_sep_y     = add_y + add_section_h

    # Separator line — suppressed once skinned, baked into the art.
    if not _has_skin:
        sep_verts = [(lx+4*scale, add_sep_y), (lx+left_w-4*scale, add_sep_y)]
        sep_b = batch_for_shader(sh,"LINES",{"pos":sep_verts})
        sh.bind(); sh.uniform_float("color",_BORDER); sep_b.draw(sh)

    if not _has_skin:
        _draw_text("ADD FROM TIMELINE", lx+5*scale, add_sep_y+fs_lbl+3*scale, fs_lbl, _TEXT_LABEL)

    # Channel selector for timeline extraction (stored in p5 as int 0-8)
    add_ch = int(getattr(rack, "p5", 0.0))  # 0-based
    ch_btn_s = min(16*scale, (left_w-10*scale)/9)
    ch_btn_y = add_y + add_section_h - fs_lbl - ch_btn_s - 4*scale
    if not _has_skin:
        _draw_text("SRC", lx+5*scale, ch_btn_y+ch_btn_s/2-fs_lbl/2, max(1,int(6*scale)), _TEXT_LABEL)
    for ci in range(9):
        bx2 = lx + 5*scale + 16*scale + ci*(ch_btn_s+1*scale)
        by2 = ch_btn_y
        issel = (ci == add_ch)
        bc = _ACCENT if issel else _ACCENT_DIM
        bg = _PANEL_SEL if issel else _PANEL
        # Per-slot button chrome suppressed once skinned — selection state is
        # conveyed by the number colour below instead (mirrors rack_base.py's
        # _draw_channel_buttons number_only fallback treatment).
        if not _has_skin:
            _draw_rect(bx2, by2, ch_btn_s, ch_btn_s, bg)
            cbvs = [(bx2,by2),(bx2+ch_btn_s,by2),(bx2+ch_btn_s,by2+ch_btn_s),(bx2,by2+ch_btn_s),(bx2,by2)]
            cbat = batch_for_shader(sh,"LINE_STRIP",{"pos":cbvs})
            sh.bind(); sh.uniform_float("color",bc); cbat.draw(sh)
        fs_ci = max(1,int(6*scale))
        lc = str(ci+1)
        tw_c = _text_width(lc,fs_ci)
        _draw_text(lc, bx2+ch_btn_s/2-tw_c/2 + KNNVC_SRC_CH_LABEL_X_OFFSET*scale,
                   by2+ch_btn_s/2-fs_ci/2, fs_ci, bc)

    # Name input field (stored in rack['add_voice_name'])
    # Check if this field is active for text input
    import time as _time
    _name_active = False
    _name_cursor = 0
    try:
        from ui.mixer.interaction import _active_text_field as _atf
        if (_atf and _atf.get('ai_idx') == ai_idx
                and _atf.get('field') == 'add_voice_name'):
            _name_active = True
            _name_cursor = _atf.get('cursor', 0)
    except ImportError:
        pass

    # Don't auto-populate — only use stored value, show placeholder if empty
    name_val     = rack.get('add_voice_name', None)
    if name_val is None:
        name_val = f"CH{add_ch+1}Sample"
        rack['add_voice_name'] = name_val

    name_field_h = max(14*scale, fs_sm+4*scale)
    name_field_y = ch_btn_y - name_field_h - 3*scale
    name_field_w = left_w - 10*scale

    # Background — brighter when active. Flat-fallback only; once skinned the
    # baked art reads clearly enough on its own (same call made for Piper's
    # script-panel focus glow — see rack_piper.py's panel_bg comment).
    _nf_bg  = (0.08,0.03,0.12,1.0) if _name_active else _BG
    _nf_col = _ACCENT if _name_active else _ACCENT_DIM
    if not _has_skin:
        _draw_rect(lx+5*scale, name_field_y, name_field_w, name_field_h, _nf_bg)
        nfvs = [(lx+5*scale, name_field_y), (lx+5*scale+name_field_w, name_field_y),
                (lx+5*scale+name_field_w, name_field_y+name_field_h),
                (lx+5*scale, name_field_y+name_field_h), (lx+5*scale, name_field_y)]
        nfb = batch_for_shader(sh,"LINE_STRIP",{"pos":nfvs})
        sh.bind(); sh.uniform_float("color",_nf_col); nfb.draw(sh)

    # Text display
    _name_text_x = lx + 8*scale + KNNVC_ADD_NAME_X_OFFSET*scale
    if name_val:
        disp_name = name_val[:16] if len(name_val) <= 16 else name_val[:15]+"..."
        _draw_text(disp_name, _name_text_x, name_field_y+name_field_h/2-fs_sm/2, fs_sm, _TEXT)
    else:
        _draw_text("enter name...", _name_text_x,
                   name_field_y+name_field_h/2-fs_sm/2, fs_sm, _TEXT_LABEL)

    # Blinking cursor when active
    if _name_active:
        _blink = int(_time.time() * 2) % 2 == 0
        if _blink:
            _pre_cur = name_val[:min(_name_cursor, len(name_val))]
            _cur_x   = _name_text_x + _text_width(_pre_cur, fs_sm)
            _draw_line(_cur_x, name_field_y + 2*scale,
                       _cur_x, name_field_y + name_field_h - 2*scale,
                       _TEXT, max(1.0, scale))

    # ADD button
    add_btn_h = max(16*scale, fs_sm+4*scale)
    add_btn_y = name_field_y - add_btn_h - 3*scale
    add_btn_w = left_w - 10*scale
    add_busy  = rack.get('add_voice_busy', False)
    add_col   = _AMBER if not add_busy else (0.60,0.35,0.00,1.0)
    add_lbl   = "ADDING..." if add_busy else "ADD TO VOICES >"
    # Button chrome suppressed once skinned — baked into the art; only the
    # state-dependent label (ADD TO VOICES/ADDING...) below still draws live.
    if not _has_skin:
        _draw_rect(lx+5*scale, add_btn_y, add_btn_w, add_btn_h, (0.06,0.03,0.00,1.0))
        abvs = [(lx+5*scale,add_btn_y),(lx+5*scale+add_btn_w,add_btn_y),
                (lx+5*scale+add_btn_w,add_btn_y+add_btn_h),
                (lx+5*scale,add_btn_y+add_btn_h),(lx+5*scale,add_btn_y)]
        abb = batch_for_shader(sh,"LINE_STRIP",{"pos":abvs})
        sh.bind(); sh.uniform_float("color",add_col); abb.draw(sh)
    fs_add = max(1,int(7*scale))
    tw_add = _text_width(add_lbl, fs_add)
    _draw_text(add_lbl, lx+5*scale+add_btn_w/2-tw_add/2,
               add_btn_y+add_btn_h/2-fs_add/2 + KNNVC_ADD_BTN_LABEL_Y_OFFSET*scale,
               fs_add, add_col)

    # ── VOICE CARDS (above add section) ────────────────────────────────────
    voices   = _discover_ref_voices(ai_idx)
    sel_idx  = int(getattr(rack, "p4", 0.0)) % max(1, len(voices)) if voices else 0
    card_h   = max(20*scale, work_h * 0.11)
    card_gap = 2*scale
    cards_top  = add_sep_y
    cards_bot  = ly + lh - fs_lbl - fs_ch*2 - 14*scale
    cards_h    = cards_bot - (add_sep_y + 2*scale)

    # Hint text above cards — suppressed once skinned, baked into the art.
    hint_y = cards_bot - fs_lbl - 2*scale
    if not _has_skin:
        _draw_text("REFERENCE VOICES", lx+5*scale, hint_y, fs_lbl, _TEXT_LABEL)

    prev_btn_w = max(16*scale, card_h*0.7)
    card_name_w = left_w - 8*scale - prev_btn_w - 4*scale
    max_vis  = max(1, int((hint_y - add_sep_y - 6*scale) / (card_h + card_gap)))

    # Scroll offset
    scroll_ofs = int(rack.get('rvc_scroll', 0))
    scroll_ofs = max(0, min(scroll_ofs, max(0, len(voices) - 1)))

    # ▲/▼ scroll arrows
    arr_sz = max(12*scale, fs_lbl + 4*scale)
    arr_x  = lx + left_w - arr_sz*2 - 6*scale
    arr_y  = hint_y - 1*scale
    can_up = scroll_ofs > 0
    can_dn = len(voices) > 0 and (scroll_ofs + max_vis) < len(voices)

    # ▲ up arrow
    up_col = _ACCENT if can_up else _ACCENT_DIM
    if not _has_skin:
        _draw_rect(arr_x, arr_y, arr_sz, arr_sz, _PANEL)
    _up_tri = [
        (arr_x + arr_sz*0.50, arr_y + arr_sz*0.72),
        (arr_x + arr_sz*0.20, arr_y + arr_sz*0.28),
        (arr_x + arr_sz*0.80, arr_y + arr_sz*0.28),
    ]
    _ub = batch_for_shader(sh, "TRI_FAN", {"pos": _up_tri})
    sh.bind(); sh.uniform_float("color", up_col); _ub.draw(sh)

    # ▼ down arrow
    dn_x   = arr_x + arr_sz + 2*scale
    dn_col = _ACCENT if can_dn else _ACCENT_DIM
    if not _has_skin:
        _draw_rect(dn_x, arr_y, arr_sz, arr_sz, _PANEL)
    _dn_tri = [
        (dn_x + arr_sz*0.50, arr_y + arr_sz*0.28),
        (dn_x + arr_sz*0.20, arr_y + arr_sz*0.72),
        (dn_x + arr_sz*0.80, arr_y + arr_sz*0.72),
    ]
    _db = batch_for_shader(sh, "TRI_FAN", {"pos": _dn_tri})
    sh.bind(); sh.uniform_float("color", dn_col); _db.draw(sh)

    # Stash arrow hit boxes and max_vis for hit test
    try:
        rack['rvc_arr_up']  = (arr_x, arr_y, arr_sz, arr_sz)
        rack['rvc_arr_dn']  = (dn_x,  arr_y, arr_sz, arr_sz)
        rack['rvc_max_vis'] = max_vis
    except Exception:
        pass

    if voices:
        for slot in range(max_vis):
            vi = slot + scroll_ofs
            if vi >= len(voices): break
            name, wav_path = voices[vi]
            cy_card = hint_y - fs_lbl - 4*scale - slot*(card_h+card_gap) - card_h
            if cy_card < add_sep_y + 2*scale: break
            issel = (vi == sel_idx)
            bg = _PANEL_SEL if issel else _PANEL
            bc = _ACCENT    if issel else _BORDER

            # Card background — fill+border suppressed once skinned (each
            # slot's box is baked into rack_knnvc_bg.png); a thin accent
            # border still draws over the selected slot as a highlight,
            # since which voice is selected/scrolled-to is dynamic and can't
            # be pre-baked into the art.
            cvs2 = [(lx+4*scale,cy_card),(lx+left_w-4*scale,cy_card),
                    (lx+left_w-4*scale,cy_card+card_h),
                    (lx+4*scale,cy_card+card_h),(lx+4*scale,cy_card)]
            if not _has_skin:
                _draw_rect(lx+4*scale, cy_card, left_w-8*scale, card_h, bg)
            if not _has_skin or issel:
                cb2 = batch_for_shader(sh,"LINE_STRIP",{"pos":cvs2})
                sh.bind(); sh.uniform_float("color",bc); cb2.draw(sh)

            # Voice name
            tc = _TEXT if issel else _TEXT_DIM
            max_chars = int(card_name_w / max(1, fs_sm*0.6))
            disp = name[:max_chars]
            _draw_text(disp, lx+8*scale, cy_card+card_h/2-fs_sm/2, fs_sm, tc)

            # ▶ / ■ preview button — toggles play/stop
            pb_x = lx + left_w - 4*scale - prev_btn_w
            pb_y = cy_card + card_h*0.1
            pb_h = card_h * 0.8
            _is_prev = False
            try:
                from core.ai_knnvc import is_voice_previewing as _ivp
                _is_prev = _ivp(ai_idx, vi)
            except Exception:
                pass
            _pb_bg  = (0.18,0.04,0.24,1.0) if _is_prev else (0.10,0.03,0.15,1.0)
            # Idle glyph used to sit on its own contrast box; now that the
            # box is suppressed when skinned, _ACCENT_DIM reads as invisible
            # against the art. Keep it bright whenever skinned so the ">" is
            # visible in both play and stop states; unskinned fallback keeps
            # the original dim/bright distinction since its box still gives contrast.
            _pb_col = _ACCENT if (_is_prev or _has_skin) else _ACCENT_DIM
            _pb_ico = "■" if _is_prev else ">"
            # Button chrome suppressed once skinned — baked into the art;
            # only the ▶/■ glyph (drawn below) still needs to swap live.
            if not _has_skin:
                _draw_rect(pb_x, pb_y, prev_btn_w, pb_h, _pb_bg)
                pbvs = [(pb_x,pb_y),(pb_x+prev_btn_w,pb_y),
                        (pb_x+prev_btn_w,pb_y+pb_h),(pb_x,pb_y+pb_h),(pb_x,pb_y)]
                pbb = batch_for_shader(sh,"LINE_STRIP",{"pos":pbvs})
                sh.bind(); sh.uniform_float("color",_pb_col); pbb.draw(sh)
            fs_pv = max(1,int(6*scale))
            tw_pv = _text_width(_pb_ico,fs_pv)
            _draw_text(_pb_ico, pb_x+prev_btn_w/2-tw_pv/2,
                       pb_y+pb_h/2-fs_pv/2, fs_pv, _pb_col)

        if len(voices) > max_vis:
            pg = f"{min(max_vis,len(voices))}/{len(voices)}"
            _draw_text(pg, lx+5*scale, add_sep_y+3*scale, max(1,int(6*scale)), _TEXT_LABEL)
    else:
        _draw_text("No voices found", lx+5*scale,
                   add_sep_y + cards_h*0.6, fs_sm, _TEXT_DIM)
        _draw_text("Use ADD FROM TIMELINE", lx+5*scale,
                   add_sep_y + cards_h*0.4, max(1,int(6*scale)), _TEXT_LABEL)
        _draw_text("or drop WAVs into voices/", lx+5*scale,
                   add_sep_y + cards_h*0.25, max(1,int(6*scale)), _TEXT_LABEL)

    # ── CENTRE PANEL: state display + convert/preview + output ch ─────────────
    cx = centre_x; cy2 = work_bot; ch2 = work_h
    if not _has_skin:
        _draw_rect(cx, cy2, centre_w, ch2, _PANEL)
        cvs3 = [(cx,cy2),(cx+centre_w,cy2),(cx+centre_w,cy2+ch2),(cx,cy2+ch2),(cx,cy2)]
        cb3 = batch_for_shader(sh,"LINE_STRIP",{"pos":cvs3})
        sh.bind(); sh.uniform_float("color",_BORDER); cb3.draw(sh)
        _draw_text("CONVERSION", cx+5*scale, cy2+ch2-fs_lbl-4*scale, fs_lbl, _TEXT_LABEL)

    # State display area (replaces the empty waveform box). Fill suppressed
    # once skinned; the status-coloured border below still draws every time
    # since it's a live state indicator, not static chrome.
    state_h = ch2 * 0.44
    state_y = cy2 + ch2 - fs_lbl - 10*scale - state_h
    if not _has_skin:
        _draw_rect(cx+4*scale, state_y, centre_w-8*scale, state_h, _BG)
    stvs = [(cx+4*scale,state_y),(cx+centre_w-4*scale,state_y),
            (cx+centre_w-4*scale,state_y+state_h),
            (cx+4*scale,state_y+state_h),(cx+4*scale,state_y)]
    stb = batch_for_shader(sh,"LINE_STRIP",{"pos":stvs})

    if status == _PROCESSING:
        sh.bind(); sh.uniform_float("color",(0.30,0.10,0.40,1.0)); stb.draw(sh)
        fs_st = max(1,int(8*scale))
        lbl_p = "CONVERTING..."
        tw_p  = _text_width(lbl_p, fs_st)
        _draw_text(lbl_p, cx+centre_w/2-tw_p/2,
                   state_y+state_h*0.6-fs_st/2, fs_st, _ACCENT)
        _draw_text("This may take 30-60 seconds",
                   cx+centre_w/2-_text_width("This may take 30-60 seconds",max(1,int(6*scale)))/2,
                   state_y+state_h*0.35, max(1,int(6*scale)), _TEXT_DIM)

    elif status == _DONE:
        sh.bind(); sh.uniform_float("color",(0.02,0.08,0.04,1.0)); stb.draw(sh)
        # Show output info
        import glob, tempfile
        tmp_dir = tempfile.gettempdir()
        matches = sorted(glob.glob(os.path.join(tmp_dir, f"pb_knnvc_{ai_idx}_*.wav")))
        fs_st = max(1,int(8*scale))
        _draw_text("CONVERSION DONE", cx+8*scale,
                   state_y+state_h*0.78-fs_st/2, fs_st, _GREEN)
        fs_info = max(1,int(7*scale))
        if matches:
            wav = matches[-1]
            try:
                sz  = os.path.getsize(wav)
                sz_str = f"{sz//1024}KB" if sz < 1024*1024 else f"{sz//(1024*1024)}MB"
                import wave
                with wave.open(wav,'r') as wf:
                    dur = wf.getnframes()/wf.getframerate()
                    sr  = wf.getframerate()
                _draw_text(f"Duration: {dur:.1f}s", cx+8*scale,
                           state_y+state_h*0.55-fs_info/2, fs_info, _TEXT_DIM)
                _draw_text(f"Sample rate: {sr}Hz", cx+8*scale,
                           state_y+state_h*0.38-fs_info/2, fs_info, _TEXT_DIM)
                _draw_text(f"File size: {sz_str}", cx+8*scale,
                           state_y+state_h*0.21-fs_info/2, fs_info, _TEXT_DIM)
            except Exception:
                _draw_text("Output placed in VSE", cx+8*scale,
                           state_y+state_h*0.45-fs_info/2, fs_info, _TEXT_DIM)
        else:
            _draw_text("Output placed in VSE", cx+8*scale,
                       state_y+state_h*0.45-fs_info/2, fs_info, _TEXT_DIM)

    elif status == _ERROR:
        sh.bind(); sh.uniform_float("color",(0.08,0.01,0.01,1.0)); stb.draw(sh)
        fs_st = max(1,int(8*scale))
        _draw_text("ERROR — check console", cx+8*scale,
                   state_y+state_h*0.6-fs_st/2, fs_st, (0.90,0.20,0.10,1.0))
        _draw_text("Assign source channel in rail",
                   cx+8*scale, state_y+state_h*0.38,
                   max(1,int(6*scale)), _TEXT_DIM)
        _draw_text("and select a reference voice",
                   cx+8*scale, state_y+state_h*0.22,
                   max(1,int(6*scale)), _TEXT_DIM)

    else:
        # READY / NO_REF
        sh.bind(); sh.uniform_float("color",_BORDER); stb.draw(sh)
        fs_st = max(1,int(8*scale))
        ref_sel = voices[sel_idx][0] if voices else None
        src_rdy = bool(active_chs)

        if not src_rdy:
            _draw_text("Assign source channel", cx+8*scale,
                       state_y+state_h*0.65, fs_st, _TEXT_DIM)
            _draw_text("in the rack rail above", cx+8*scale,
                       state_y+state_h*0.45, max(1,int(7*scale)), _TEXT_LABEL)
        elif not voices:
            _draw_text("Add a reference voice", cx+8*scale,
                       state_y+state_h*0.65, fs_st, _TEXT_DIM)
            _draw_text("using ADD FROM TIMELINE", cx+8*scale,
                       state_y+state_h*0.45, max(1,int(7*scale)), _TEXT_LABEL)
        else:
            _draw_text("Ready to convert", cx+8*scale,
                       state_y+state_h*0.65, fs_st, _ACCENT)
            ref_disp = ref_sel[:20] if ref_sel else "none"
            _draw_text(f"Ref: {ref_disp}", cx+8*scale,
                       state_y+state_h*0.45, max(1,int(7*scale)), _TEXT_DIM)
            src_disp2 = f"Src: CH{active_chs[0]}"
            _draw_text(src_disp2, cx+8*scale,
                       state_y+state_h*0.26, max(1,int(7*scale)), _TEXT_DIM)

    # CONVERT + PREVIEW buttons
    btn_h2  = max(22*scale, ch2*0.11)
    btn_y2  = state_y - 2*scale - btn_h2
    conv_w  = centre_w * 0.52
    prev_w  = centre_w * 0.38
    conv_x  = cx + 4*scale
    prev_x  = conv_x + conv_w + 4*scale

    # Label reflects state: GENERATING during convert, GENERATE when ready,
    # or PLACE (if preview output exists and just needs placing on timeline)
    _has_out_gen = False
    try:
        from core.ai_knnvc import has_rack_output as _hro
        _has_out_gen = _hro(ai_idx)
    except Exception:
        pass
    if status == _PROCESSING:
        c_lbl = "GENERATING..."
    elif _has_out_gen:
        c_lbl = "PLACE ON TRACK"
    else:
        c_lbl = "GENERATE"
    c_bg  = (0.08,0.02,0.12,1.0) if status == _PROCESSING else (0.10,0.03,0.15,1.0)
    # Button chrome suppressed once skinned — baked into the art; only the
    # state-dependent label (GENERATE/GENERATING.../PLACE ON TRACK) below
    # still needs to draw live.
    if not _has_skin:
        _draw_rect(conv_x, btn_y2, conv_w, btn_h2, c_bg)
        evs = [(conv_x,btn_y2),(conv_x+conv_w,btn_y2),(conv_x+conv_w,btn_y2+btn_h2),
               (conv_x,btn_y2+btn_h2),(conv_x,btn_y2)]
        eb = batch_for_shader(sh,"LINE_STRIP",{"pos":evs})
        sh.bind(); sh.uniform_float("color",_ACCENT); eb.draw(sh)
    fs_btn = max(1,int(8*scale))
    tw_c2  = _text_width(c_lbl, fs_btn)
    _draw_text(c_lbl, conv_x+conv_w/2-tw_c2/2,
               btn_y2+btn_h2/2-fs_btn/2, fs_btn, _ACCENT)

    # Preview button — shows ■ STOP when playing, > PREVIEW when not
    # Only active when this rack has a fresh conversion output
    _has_out   = False
    _is_playing = False
    try:
        from core.ai_knnvc import has_rack_output, is_rack_previewing
        _has_out    = has_rack_output(ai_idx)
        _is_playing = is_rack_previewing(ai_idx)
    except Exception:
        pass
    _prev_bg  = (0.18,0.04,0.24,1.0) if _is_playing else (0.04,0.02,0.06,1.0)
    prev_col  = _ACCENT if (_has_out or _is_playing) else _ACCENT_DIM
    pv_lbl    = "■ STOP" if _is_playing else "> PREVIEW"
    # Button chrome suppressed once skinned — baked into the art; only the
    # state-dependent label (PREVIEW/STOP) below still needs to draw live.
    if not _has_skin:
        _draw_rect(prev_x, btn_y2, prev_w, btn_h2, _prev_bg)
        pvs2 = [(prev_x,btn_y2),(prev_x+prev_w,btn_y2),(prev_x+prev_w,btn_y2+btn_h2),
                (prev_x,btn_y2+btn_h2),(prev_x,btn_y2)]
        pb2 = batch_for_shader(sh,"LINE_STRIP",{"pos":pvs2})
        sh.bind(); sh.uniform_float("color",prev_col); pb2.draw(sh)
    tw_pv  = _text_width(pv_lbl, fs_btn)
    _draw_text(pv_lbl, prev_x+prev_w/2-tw_pv/2,
               btn_y2+btn_h2/2-fs_btn/2, fs_btn, prev_col)

    # Output channel with < > arrows — stored in p3
    row2_y = btn_y2 - 2*scale - max(16*scale, ch2*0.08)
    row2_h = max(16*scale, ch2*0.08)
    out_ch_val = int(getattr(rack, "p3", 0.0)) or (active_chs[0]+1 if active_chs else 2)
    out_ch_val = max(1, min(9, out_ch_val))

    if not _has_skin:
        _draw_text("OUT CH", cx+5*scale, row2_y+row2_h/2-fs_lbl/2, fs_lbl, _TEXT_LABEL)
    lbl_tw = _text_width("OUT CH ", fs_lbl)
    arr_w  = max(14*scale, row2_h)
    oc_s   = max(22*scale, row2_h)
    oc_x   = cx + 5*scale + lbl_tw + arr_w + 2*scale

    if not _has_skin:
        _draw_rect(cx+5*scale+lbl_tw, row2_y, arr_w, row2_h, _PANEL)
    _ax = cx+5*scale+lbl_tw
    # Left arrow — suppressed once skinned (baked into the art either side
    # of the output-channel box); the box's hit test is unaffected, this
    # only turns off the drawn triangle.
    if not _has_skin:
        mv_l = [(_ax+arr_w*0.65, row2_y+row2_h*0.18),
                (_ax+arr_w*0.28, row2_y+row2_h*0.50),
                (_ax+arr_w*0.65, row2_y+row2_h*0.82)]
        al = batch_for_shader(sh,"TRI_FAN",{"pos":mv_l})
        sh.bind(); sh.uniform_float("color",_ACCENT); al.draw(sh)

    if not _has_skin:
        _draw_rect(oc_x, row2_y, oc_s, row2_h, _PANEL)
        ocvs = [(oc_x,row2_y),(oc_x+oc_s,row2_y),(oc_x+oc_s,row2_y+row2_h),
                (oc_x,row2_y+row2_h),(oc_x,row2_y)]
        ocb = batch_for_shader(sh,"LINE_STRIP",{"pos":ocvs})
        sh.bind(); sh.uniform_float("color",_BORDER); ocb.draw(sh)
    oc_str = str(out_ch_val)
    tw_oc  = _text_width(oc_str, fs_lbl)
    _draw_text(oc_str, oc_x+oc_s/2-tw_oc/2, row2_y+row2_h/2-fs_lbl/2, fs_lbl, _ACCENT)

    plus_x = oc_x + oc_s + 2*scale
    if not _has_skin:
        _draw_rect(plus_x, row2_y, arr_w, row2_h, _PANEL)
    # Right arrow — suppressed once skinned, same reasoning as the left
    # arrow above.
    if not _has_skin:
        mv_r = [(plus_x+arr_w*0.35, row2_y+row2_h*0.18),
                (plus_x+arr_w*0.72, row2_y+row2_h*0.50),
                (plus_x+arr_w*0.35, row2_y+row2_h*0.82)]
        ar = batch_for_shader(sh,"TRI_FAN",{"pos":mv_r})
        sh.bind(); sh.uniform_float("color",_ACCENT); ar.draw(sh)

    # ── RIGHT PANEL: knobs ────────────────────────────────────────────────────
    rx2 = right_x; ry2 = work_bot; rh2 = work_h
    if not _has_skin:
        _draw_rect(rx2, ry2, right_w, rh2, _PANEL)
        rvs = [(rx2,ry2),(rx2+right_w,ry2),(rx2+right_w,ry2+rh2),(rx2,ry2+rh2),(rx2,ry2)]
        rb = batch_for_shader(sh,"LINE_STRIP",{"pos":rvs})
        sh.bind(); sh.uniform_float("color",_BORDER); rb.draw(sh)
        _draw_text("CONTROLS", rx2+5*scale, ry2+rh2-fs_lbl-4*scale, fs_lbl, _TEXT_LABEL)

    knob_r_base = min(18*scale, right_w*0.28, work_h*0.18)
    ky0         = ry2 + rh2 * 0.72

    # Knob names ("TOPK"/"REF SECS") suppressed once skinned — same treatment
    # as Piper's SPEED/NOISE/NOISE W knob labels; the live value string
    # (topk_val / ref_secs) always keeps drawing since it's dynamic. Knob
    # radius is scaled per-knob via KNNVC_TOPK_KNOB_SCALE / _REFSECS_ above;
    # centre position is nudged per-knob via the *_X_OFFSET/_Y_OFFSET
    # constants so each can be matched to its background art independently.
    norm0    = float(getattr(rack,"p0",0.5))
    topk_val = int(2 + norm0*6)
    _draw_knob(rx2+right_w*0.30 + KNNVC_TOPK_KNOB_X_OFFSET*scale,
               ky0 + KNNVC_TOPK_KNOB_Y_OFFSET*scale,
               knob_r_base * KNNVC_TOPK_KNOB_SCALE, norm0,
               (_ACCENT[0],_ACCENT[1],_ACCENT[2]),
               "" if _has_skin else "TOPK", str(topk_val), scale)

    norm1    = float(getattr(rack,"p1",0.5))
    ref_secs = int(10 + norm1*50)
    _draw_knob(rx2+right_w*0.72 + KNNVC_REFSECS_KNOB_X_OFFSET*scale,
               ky0 + KNNVC_REFSECS_KNOB_Y_OFFSET*scale,
               knob_r_base * KNNVC_REFSECS_KNOB_SCALE, norm1,
               (_ACCENT[0],_ACCENT[1],_ACCENT[2]),
               "" if _has_skin else "REF SECS", str(ref_secs)+"s", scale)

    # Help-text column — kept even when skinned. Unlike the section headers
    # above, this is six lines of tooltip-style copy that's unlikely to be
    # baked into a control-panel graphic — flag to confirm once the PNG
    # exists; easy to wrap in `if not _has_skin:` if it turns out to be baked.
    fs_n   = max(1,int(6*scale))
    note_y = ry2 + rh2*0.30
    for note in ["TOPK: neighbours","(2=sharp,8=smooth)","","REF SECS: max ref","clip length","(30s recommended)"]:
        _draw_text(note, rx2+6*scale, note_y, fs_n, _TEXT_LABEL)
        note_y -= (fs_n+2*scale)

    # ── STATUS BAR ───────────────────────────────────────────────────────────
    if not _has_skin:
        _draw_rect(sbar_x, sbar_y, sbar_w, sbar_h, (0.03,0.01,0.04,1.0))
        svs = [(sbar_x,sbar_y),(sbar_x+sbar_w,sbar_y),(sbar_x+sbar_w,sbar_y+sbar_h),
               (sbar_x,sbar_y+sbar_h),(sbar_x,sbar_y)]
        sb2 = batch_for_shader(sh,"LINE_STRIP",{"pos":svs})
        sh.bind(); sh.uniform_float("color",_BORDER); sb2.draw(sh)

    voices_now  = _discover_ref_voices(ai_idx)
    ref_name    = "none"
    if voices_now:
        si = int(getattr(rack,"p4",0.0)) % max(1,len(voices_now))
        ref_name = os.path.splitext(os.path.basename(voices_now[si][1]))[0] if si < len(voices_now) else "none"
    topk_disp   = int(2 + float(getattr(rack,"p0",0.5))*6)
    src_str     = f"SRC:CH{active_chs[0]}" if active_chs else "SRC:unset"
    out_ch_sb   = int(getattr(rack,"p3",0.0)) or (active_chs[0]+1 if active_chs else 2)

    fs_sb = max(1,int(7*scale))
    _draw_text(
        f"ENGINE: kNN-VC  |  {src_str}  |  OUT:CH{out_ch_sb}  |  REF:{ref_name}  |  TOPK:{topk_disp}  |  OFFLINE",
        sbar_x+7*scale, sbar_y+sbar_h/2-fs_sb/2, fs_sb, _TEXT_DIM)

    # Status LED — suppressed once skinned; baked into the background art in
    # the bottom-right corner of the rack, same as the other static chrome.
    if not _has_skin:
        dot_cols = {
            _READY:      _AMBER,
            _NO_REF:     _AMBER,
            _PROCESSING: (0.90,0.50,0.10,1.0),
            _DONE:       _GREEN,
            _ERROR:      (0.90,0.10,0.05,1.0),
        }
        _draw_circle(sbar_x+sbar_w-9*scale, sbar_y+sbar_h/2,
                     3.5*scale, dot_cols.get(status,_AMBER))

# =============================================================================
# PACKAPUNCH SKIN  (rack_knnvc_bg.png, 1759 x 526 art px)
# Everything below is laid out in ART pixels (top-left origin) and scaled to the
# rack at draw time, so drawing and click zones always agree and a tweak here
# moves both. Used only while the packApunch skin is active and PyTorch is found.
# =============================================================================
KN_PP_SKIN  = "packapunch"
KN_ART_W    = 1759.0
KN_ART_H    = 526.0

# rects are (x0, y0, x1, y1) in art px
KN_COLLAPSE   = (115.0, 15.5, 142.5, 45.0)       # the dark button with the red triangle
KN_BADGE      = (151.0, 15.5, 179.0, 45.0)       # the lit red square (rack number)
KN_ON         = (1568.0, 13.5, 1623.0, 43.0)     # green ON glass
KN_CLOSE      = (1625.5, 13.5, 1653.0, 43.0)     # red power button (removes the rack)
KN_OFF_PATCH  = (1563, 9, 1627, 53)              # dimmed ON glass image is laid here while the rack is off
KN_TOPKEY_X0  = 868.0                            # channel keys above the screen
KN_TOPKEY_PITCH = 27.75
KN_TOPKEY_W   = 24.0
KN_TOPKEY_Y   = (21.5, 48.5)
KN_SOURCE     = (130.0, 71.0, 361.0, 90.5)       # SOURCE read-out bar
KN_VOICE_X    = (127.0, 552.0)                   # reference-voice rows
KN_VOICE_Y0   = 122.7
KN_VOICE_PITCH = 49.4
KN_VOICE_H    = 47.3
KN_VOICE_ROWS = 4
KN_VOICE_BTN_X = (516.0, 546.7)                  # the small preview buttons
KN_VOICE_BTN_DY = (8.3, 39.0)                    # button top / bottom relative to its row top
KN_ARROW_UP   = (512.0, 92.0, 532.0, 112.0)
KN_ARROW_DN   = (532.0, 92.0, 552.0, 112.0)
KN_SCREEN     = (574.0, 72.0, 1232.0, 257.0)     # the big black screen
KN_GENERATE   = (571.0, 261.0, 949.0, 310.0)
KN_PREVIEW    = (951.0, 262.0, 1182.0, 311.0)
KN_OUT_DEC    = (616.0, 312.0, 644.0, 350.0)     # < > around the output-channel box
KN_OUT_BOX    = (646.0, 313.0, 687.0, 348.0)
KN_OUT_INC    = (690.0, 312.0, 716.0, 350.0)
KN_SRCKEY_X0  = 156.5                            # SRC keys (add-from-timeline channel)
KN_SRCKEY_PITCH = 24.5
KN_SRCKEY_W   = 21.5
KN_SRCKEY_Y   = (359.5, 382.5)
KN_NAME       = (134.0, 387.7, 551.0, 405.0)     # name field
KN_ADD        = (134.0, 410.7, 550.0, 441.7)     # ADD TO VOICES
KN_KNOBS      = ((1373.0, 168.0), (1538.0, 165.0))   # TOPK, REF SECS centres
KN_KNOB_HIT_R = 52.0
KN_KNOB_PTR_LEN = 30.0                           # pointer length (art px)
KN_KNOB_PTR_W   = 4.6                            # pointer width (art px)
KN_KNOB_START_DEG = 225.0                        # pointer angle at minimum (lower left; turns clockwise)
KN_KNOB_SWEEP     = 270.0
KN_KNOB_VAL_Y     = 234.0                        # value read-out under each knob
KN_STATUS     = (120.0, 478.0, 1640.0, 505.0)    # status bar

# red glow travelling along the veins: pre-rendered additive loop (see KnnvcVeins_frames)
try:
    from core.constants import ASSETS_DIR as _KN_ASSETS_DIR
except Exception:
    _KN_ASSETS_DIR = ""
KN_VEIN_DIR  = os.path.join(_KN_ASSETS_DIR, "packapunch", "KnnvcVeins_frames")
KN_VEIN_FPS  = 20.0      # 48 frames = 2.4 s loop
KN_VEIN_GAIN = 1.0       # brightness of the travelling light

# text (art px sizes) and colours: glowing red
KN_FS_BIG     = 20.0
KN_FS_LABEL   = 17.0     # button labels / voice names
KN_FS_MED     = 15.0
KN_FS_SMALL   = 13.0
KN_TEXT       = (1.00, 0.24, 0.17, 1.00)    # bright glowing red
KN_TEXT_DIM   = (0.66, 0.14, 0.11, 0.92)    # unavailable / unselected
KN_TEXT_HOT   = (1.00, 0.56, 0.48, 1.00)    # text on a lit key
KN_TEXT_DARK  = (0.10, 0.00, 0.00, 0.95)    # on the lit red number plate
KN_GLOW       = 0.95
KN_GLOW_SCALE = 0.5      # overall text-glow strength multiplier
KN_GLOW_COL   = (1.00, 0.10, 0.06)


def _kn_pp_skin_on():
    try:
        from ui.mixer.texture_cache import get_active_skin as _gas_kn, get_texture as _gt_kn
        return _gas_kn() == KN_PP_SKIN and _gt_kn("rack_knnvc_bg") is not None
    except Exception:
        return False


def knnvc_pp_active():
    """True when the packApunch kNN-VC layout is in use (skin loaded AND PyTorch found, since the skin
    is withheld behind the setup warning until then)."""
    return _kn_pp_skin_on() and _check_deps()


class _KnGeom:
    """Art px -> screen px for one rack."""
    def __init__(self, rx, ry, rw, rh):
        self.rx, self.ry, self.rw, self.rh = rx, ry, rw, rh
        self.sx, self.sy = rw / KN_ART_W, rh / KN_ART_H
        self.s = min(self.sx, self.sy)

    def X(self, ax):
        return self.rx + ax * self.sx

    def Y(self, ay):
        return self.ry + (1.0 - ay / KN_ART_H) * self.rh

    def rect(self, r):
        """(x0, y0, x1, y1) screen rect, y0 = bottom."""
        return (self.X(r[0]), self.Y(r[3]), self.X(r[2]), self.Y(r[1]))

    def top_key(self, i):
        x0 = KN_TOPKEY_X0 + KN_TOPKEY_PITCH * i
        return (x0, KN_TOPKEY_Y[0], x0 + KN_TOPKEY_W, KN_TOPKEY_Y[1])

    def src_key(self, i):
        x0 = KN_SRCKEY_X0 + KN_SRCKEY_PITCH * i
        return (x0, KN_SRCKEY_Y[0], x0 + KN_SRCKEY_W, KN_SRCKEY_Y[1])

    def voice_row(self, k):
        y0 = KN_VOICE_Y0 + KN_VOICE_PITCH * k
        return (KN_VOICE_X[0], y0, KN_VOICE_X[1], y0 + KN_VOICE_H)

    def voice_btn(self, k):
        y0 = KN_VOICE_Y0 + KN_VOICE_PITCH * k
        return (KN_VOICE_BTN_X[0], y0 + KN_VOICE_BTN_DY[0], KN_VOICE_BTN_X[1], y0 + KN_VOICE_BTN_DY[1])


def _kn_in(r, x, y):
    return r[0] <= x <= r[2] and r[1] <= y <= r[3]


def knnvc_pp_hit(mx, my, rack, ai_idx, rack_x, rack_y, rw, rh):
    """Click zones for the packApunch layout. Returns a hit dict (same zones the old layout used),
    or None. Only call while knnvc_pp_active()."""
    g = _KnGeom(rack_x, rack_y, rw, rh)
    R = g.rect
    if _kn_in(R(KN_COLLAPSE), mx, my):
        return {'zone': 'ai_collapse', 'ai_idx': ai_idx}
    if _kn_in(R(KN_CLOSE), mx, my):
        return {'zone': 'ai_delete', 'ai_idx': ai_idx}
    if _kn_in(R(KN_ON), mx, my):
        return {'zone': 'ai_on_off', 'ai_idx': ai_idx}
    for ci in range(9):
        if _kn_in(R(g.top_key(ci)), mx, my):
            return {'zone': 'ai_channel_btn', 'ai_idx': ai_idx, 'ch_idx': ci}
    for ki, (kx, ky) in enumerate(KN_KNOBS):
        if math.hypot(mx - g.X(kx), my - g.Y(ky)) <= KN_KNOB_HIT_R * g.s:
            return {'zone': 'ai_rvc_knob', 'ai_idx': ai_idx, 'knob_idx': ki}
    if _kn_in(R(KN_GENERATE), mx, my):
        return {'zone': 'ai_rvc_convert', 'ai_idx': ai_idx}
    if _kn_in(R(KN_PREVIEW), mx, my):
        return {'zone': 'ai_rvc_preview', 'ai_idx': ai_idx}
    if _kn_in(R(KN_OUT_DEC), mx, my):
        return {'zone': 'ai_rvc_outch_dec', 'ai_idx': ai_idx}
    if _kn_in(R(KN_OUT_INC), mx, my):
        return {'zone': 'ai_rvc_outch_inc', 'ai_idx': ai_idx}
    for ci in range(9):
        if _kn_in(R(g.src_key(ci)), mx, my):
            return {'zone': 'ai_rvc_add_ch', 'ai_idx': ai_idx, 'ch_idx': ci}
    if _kn_in(R(KN_NAME), mx, my):
        return {'zone': 'ai_rvc_name_field', 'ai_idx': ai_idx}
    if _kn_in(R(KN_ADD), mx, my):
        return {'zone': 'ai_rvc_add_voice', 'ai_idx': ai_idx}
    if _kn_in(R(KN_ARROW_UP), mx, my):
        return {'zone': 'ai_rvc_scroll', 'ai_idx': ai_idx, 'dir': -1}
    if _kn_in(R(KN_ARROW_DN), mx, my):
        return {'zone': 'ai_rvc_scroll', 'ai_idx': ai_idx, 'dir': 1}
    voices = _discover_ref_voices(ai_idx)
    try:
        scroll = int(rack.get('rvc_scroll', 0))
    except Exception:
        scroll = 0
    for slot in range(KN_VOICE_ROWS):
        vi = slot + scroll
        if vi >= len(voices):
            break
        if _kn_in(R(g.voice_btn(slot)), mx, my):
            return {'zone': 'ai_rvc_voice_preview', 'ai_idx': ai_idx, 'voice_idx': vi}
        if _kn_in(R(g.voice_row(slot)), mx, my):
            return {'zone': 'ai_rvc_voice', 'ai_idx': ai_idx, 'voice_idx': vi}
    return None


# ---------------------------------------------------------------------------
# drawing helpers
# ---------------------------------------------------------------------------
def _kn_text(text, x, cy, fs_art, g, col, lv=1.0, align="l", glow=KN_GLOW):
    """Glowing text; x is the left edge / centre / right edge by align, cy the vertical centre (screen px)."""
    try:
        from ui.racks.pp_text import draw_text as _pdt, text_width as _ptw
    except Exception:
        _pdt = _ptw = None
    if lv <= 0.01:
        return 0.0
    fs = max(1, int(round(fs_art * g.s)))
    a = col[3] * lv
    c = (col[0], col[1], col[2], a)
    if _ptw:
        tw = _ptw(text, fs)
    else:
        tw = _text_width(text, fs)
    if align == "c":
        x = x - tw / 2
    elif align == "r":
        x = x - tw
    y = cy - fs * 0.36
    if _pdt:
        _pdt(text, x, y, fs, c, glow=glow * lv * KN_GLOW_SCALE, glow_col=KN_GLOW_COL)
    else:
        _draw_text(text, x, y, fs, c)
    return tw


def _kn_quad(shader, x0, y0, x1, y1, col, blend="ALPHA"):
    gpu.state.blend_set(blend)
    shader.bind()
    shader.uniform_float("color", col)
    batch_for_shader(shader, "TRIS", {"pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]}).draw(shader)
    gpu.state.blend_set("ALPHA")


def _kn_glow_box(shader, r, lv, g):
    """Bright red outline + soft halo around a rect (the selected voice row)."""
    x0, y0, x1, y1 = r
    t = max(1.0, 1.7 * g.s)
    for grow, wid, a in ((3.2 * g.s, 3.0 * g.s, 0.10), (1.6 * g.s, 2.2 * g.s, 0.18), (0.0, t, 0.85)):
        ax0, ay0, ax1, ay1 = x0 - grow, y0 - grow, x1 + grow, y1 + grow
        col = (1.0, 0.16, 0.10, a * lv)
        bl = "ADDITIVE" if grow > 0 else "ALPHA"
        _kn_quad(shader, ax0, ay1 - wid, ax1, ay1, col, bl)
        _kn_quad(shader, ax0, ay0, ax1, ay0 + wid, col, bl)
        _kn_quad(shader, ax0, ay0, ax0 + wid, ay1, col, bl)
        _kn_quad(shader, ax1 - wid, ay0, ax1, ay1, col, bl)
    _kn_quad(shader, x0, y0, x1, y1, (1.0, 0.1, 0.06, 0.07 * lv), "ADDITIVE")


def _kn_pointer(shader, cx, cy, length, width, ang_deg, lv):
    a = math.radians(ang_deg)
    ca, sa = math.cos(a), math.sin(a)
    x0, y0 = cx + ca * length * 0.12, cy + sa * length * 0.12
    x1, y1 = cx + ca * length, cy + sa * length
    for wmul, col, bl in ((3.0, (1.0, 0.10, 0.06, 0.22 * lv), "ADDITIVE"),
                          (1.8, (1.0, 0.12, 0.08, 0.35 * lv), "ADDITIVE"),
                          (1.0, (1.0, 0.30, 0.22, lv), "ALPHA")):
        hw = width * wmul * 0.5
        nx, ny = -sa * hw, ca * hw
        q = [(x0 + nx, y0 + ny), (x0 - nx, y0 - ny), (x1 + nx, y1 + ny),
             (x0 - nx, y0 - ny), (x1 - nx, y1 - ny), (x1 + nx, y1 + ny)]
        gpu.state.blend_set(bl)
        shader.bind()
        shader.uniform_float("color", col)
        batch_for_shader(shader, "TRIS", {"pos": q}).draw(shader)
    gpu.state.blend_set("ALPHA")


def _kn_blit(key, r, blend="ALPHA", k=1.0):
    """Blit a skin image into a screen rect; k < 1 fades it (premultiplied by k)."""
    try:
        from ui.mixer.texture_cache import (get_texture as _gt, blit_texture as _bt,
                                            blit_texture_tinted as _btt)
        tex = _gt(key)
        if tex:
            if k >= 0.999:
                _bt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], key=key, blend=blend)
            elif k > 0.003:
                _btt(tex, r[0], r[1], r[2] - r[0], r[3] - r[1], color=(k, k, k, k), blend=blend, key=key)
            return True
    except Exception as e:
        print(f"[KNNVC] blit {key}: {e}")
    return False


# knob LED arc meters: 6 lit segments per side (bottom->top), art-pixel boxes of the sprites
KN_ARC_BOXES = {0: {'L': [[1304, 189, 1352, 242], [1291, 180, 1339, 218], [1287, 168, 1334, 196], [1287, 144, 1334, 171], [1291, 122, 1339, 159], [1302, 96, 1352, 150]],
                    'R': [[1402, 197, 1437, 244], [1409, 184, 1449, 219], [1417, 168, 1454, 197], [1417, 145, 1454, 171], [1411, 122, 1450, 156], [1402, 94, 1439, 143]]},
                1: {'L': [[1467, 189, 1516, 242], [1455, 179, 1503, 218], [1450, 169, 1498, 196], [1450, 144, 1497, 171], [1454, 122, 1502, 159], [1466, 96, 1516, 149]],
                    'R': [[1566, 197, 1600, 244], [1573, 184, 1612, 219], [1580, 168, 1617, 197], [1581, 145, 1617, 171], [1574, 122, 1613, 156], [1566, 94, 1602, 143]]}}
KN_ARC_SEGS = 12


def _kn_arc_meter(g, ki, v, lv):
    """Light the knob's LED arc: left side bottom->top, then right side top->bottom."""
    if lv <= 0.003:
        return
    n = 1.0 + v * (KN_ARC_SEGS - 1)
    boxes = KN_ARC_BOXES.get(ki)
    if not boxes:
        return
    for s in range(KN_ARC_SEGS):
        k = max(0.0, min(1.0, n - s)) * lv
        if k <= 0.003:
            continue
        side, i = ("L", s) if s < 6 else ("R", 5 - (s - 6))
        _kn_blit(f"knnvc_arc_{ki}_{side}{i}", g.rect(boxes[side][i]), "ALPHA_PREMULT", k)



def knnvc_pp_draw(rx, ry, rw, rh, rack, ai_idx, scale):
    """packApunch kNN-VC rack: everything that moves or changes, drawn over the baked art."""
    import time as _time
    g = _KnGeom(rx, ry, rw, rh)
    R = g.rect
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    enabled = bool(getattr(rack, "enabled", True))
    try:
        from ui.racks.rack_base import _pp_preset_level as _ppl
        lv = _ppl(("knnvc", ai_idx), enabled, _time.monotonic())
    except Exception:
        lv = 1.0 if enabled else 0.0
    now = _time.time()

    # ── power: lights-off art fades in over the chassis (dark character, eyes out, veins down) ─────
    full = (rx, ry, rx + rw, ry + rh)
    if lv < 0.997:
        _kn_blit("rack_knnvc_bg_off", full, blend="ALPHA_PREMULT", k=1.0 - lv)
    # ── red glow travelling along the veins in the metal (fades out with the power) ───────────────
    if lv > 0.01:
        try:
            from core.rack_screen_sequence import get_sequence_frame as _gsf
            from ui.mixer.texture_cache import blit_texture_tinted as _btt_v
            _vt = _gsf("knnvc_packapunch_veins", KN_VEIN_DIR, fps=KN_VEIN_FPS, speed=1.0, phase_key=ai_idx)
            if _vt is not None:
                k = lv * KN_VEIN_GAIN
                _btt_v(_vt, rx, ry, rw, rh, color=(k, k, k, k), blend="ADDITIVE_PREMULT",
                       key="knnvc_packapunch_veins")
        except Exception as _e_vn:
            print(f"[KNNVC] vein animation error: {_e_vn}")
    # ── dimmed ON glass, crossfaded in as the rack goes off ───────────────────────────────────────
    if lv < 0.997:
        _kn_blit("rack_knnvc_off_btn", R(KN_OFF_PATCH), k=1.0 - lv)

    # ── rack number on the lit square ─────────────────────────────────────────
    br = R(KN_BADGE)
    _kn_text(str(ai_idx + 1), (br[0] + br[2]) / 2, (br[1] + br[3]) / 2, 22.0, g, KN_TEXT_DARK, 1.0, "c", glow=0.0)

    # ── channel keys above the screen ─────────────────────────────────────────
    group_off = getattr(rack, 'group_idx', 0) * 9
    for ci in range(9):
        kr = R(g.top_key(ci))
        assigned = bool(getattr(rack, f"ch{ci}", False))
        if assigned:
            _kn_blit("rack_knnvc_key_lit", kr, k=lv)
        _kn_text(str(group_off + ci + 1), (kr[0] + kr[2]) / 2, (kr[1] + kr[3]) / 2, KN_FS_MED, g,
                 KN_TEXT_HOT if assigned else KN_TEXT_DIM, lv, "c",
                 glow=KN_GLOW if assigned else 0.35)

    status = getattr(rack, "ai_status", _READY)
    active_chs = [ci + 1 for ci in range(9) if getattr(rack, f"ch{ci}", False)]

    # ── SOURCE read-out ───────────────────────────────────────────────────────
    sr = R(KN_SOURCE)
    src_disp = f"CH {active_chs[0]}" if active_chs else "USE RAIL"
    _kn_text(src_disp, sr[0] + 7 * g.s, (sr[1] + sr[3]) / 2, KN_FS_MED, g,
             KN_TEXT if active_chs else KN_TEXT_DIM, lv)

    # ── REFERENCE VOICES ──────────────────────────────────────────────────────
    voices = _discover_ref_voices(ai_idx)
    sel_idx = int(getattr(rack, "p4", 0.0)) % max(1, len(voices)) if voices else 0
    try:
        scroll = int(rack.get('rvc_scroll', 0))
    except Exception:
        scroll = 0
    scroll = max(0, min(scroll, max(0, len(voices) - 1)))
    try:
        rack['rvc_max_vis'] = KN_VOICE_ROWS
    except Exception:
        pass
    if voices:
        for slot in range(KN_VOICE_ROWS):
            vi = slot + scroll
            if vi >= len(voices):
                break
            name, _wav = voices[vi]
            rr = R(g.voice_row(slot))
            issel = (vi == sel_idx)
            if issel:
                _kn_glow_box(sh, rr, lv, g)
            cyr = (rr[1] + rr[3]) / 2
            maxw = (R(g.voice_btn(slot))[0] - rr[0]) - 26 * g.s
            nm = name
            fs_px = max(1, int(round(KN_FS_LABEL * g.s)))
            while len(nm) > 1 and _text_width(nm, fs_px) > maxw:
                nm = nm[:-1]
            _kn_text(nm, rr[0] + 14 * g.s, cyr, KN_FS_LABEL, g, KN_TEXT if issel else KN_TEXT_DIM, lv)
            br2 = R(g.voice_btn(slot))
            _is_prev = False
            try:
                from core.ai_knnvc import is_voice_previewing as _ivp
                _is_prev = _ivp(ai_idx, vi)
            except Exception:
                pass
            _kn_text("■" if _is_prev else ">", (br2[0] + br2[2]) / 2, (br2[1] + br2[3]) / 2,
                     KN_FS_LABEL, g, KN_TEXT, lv, "c")
        if len(voices) > KN_VOICE_ROWS:
            pg = f"{scroll + 1}-{min(scroll + KN_VOICE_ROWS, len(voices))}/{len(voices)}"
            ar = R(KN_ARROW_UP)
            _kn_text(pg, ar[0] - 6 * g.s, (ar[1] + ar[3]) / 2, KN_FS_SMALL, g, KN_TEXT_DIM, lv, "r", glow=0.5)
    else:
        rr = R(g.voice_row(0))
        _kn_text("No reference voices yet", rr[0] + 14 * g.s, (rr[1] + rr[3]) / 2, KN_FS_LABEL, g, KN_TEXT_DIM, lv)
        rr2 = R(g.voice_row(1))
        _kn_text("Use ADD TO VOICES, or drop WAVs into voices/", rr2[0] + 14 * g.s, (rr2[1] + rr2[3]) / 2,
                 KN_FS_MED, g, KN_TEXT_DIM, lv, glow=0.5)

    # ── screen: state read-out under the eyes ─────────────────────────────────
    scr = R(KN_SCREEN)
    cxs = (scr[0] + scr[2]) / 2
    def _line(txt, art_y, fs, col, glow=KN_GLOW, a=1.0):
        c = (col[0], col[1], col[2], col[3] * a)
        _kn_text(txt, cxs, g.Y(art_y), fs, g, c, lv, "c", glow=glow)
    ref_sel = voices[sel_idx][0] if voices else None
    if status == _PROCESSING:
        pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(now * 5.0))
        _line("CONVERTING...", 214, KN_FS_BIG, KN_TEXT, a=pulse)
        _line("This may take 30-60 seconds", 236, KN_FS_SMALL, KN_TEXT_DIM, 0.5)
    elif status == _DONE:
        _line("CONVERSION DONE", 214, KN_FS_BIG, KN_TEXT)
        info = "Output placed in VSE"
        try:
            import glob, tempfile, wave
            matches = sorted(glob.glob(os.path.join(tempfile.gettempdir(), f"pb_knnvc_{ai_idx}_*.wav")))
            if matches:
                wav = matches[-1]
                sz = os.path.getsize(wav)
                sz_str = f"{sz // 1024}KB" if sz < 1024 * 1024 else f"{sz // (1024 * 1024)}MB"
                with wave.open(wav, 'r') as wf:
                    dur = wf.getnframes() / wf.getframerate()
                    sr_hz = wf.getframerate()
                info = f"{dur:.1f}s   |   {sr_hz}Hz   |   {sz_str}"
        except Exception:
            pass
        _line(info, 236, KN_FS_SMALL, KN_TEXT_DIM, 0.5)
    elif status == _ERROR:
        _line("ERROR - CHECK CONSOLE", 214, KN_FS_BIG, (1.0, 0.30, 0.12, 1.0))
        _line("Assign a source channel and select a reference voice", 236, KN_FS_SMALL, KN_TEXT_DIM, 0.5)
    else:
        if not active_chs:
            _line("ASSIGN A SOURCE CHANNEL", 214, KN_FS_BIG, KN_TEXT_DIM, 0.5)
            _line("use the channel keys above the screen", 236, KN_FS_SMALL, KN_TEXT_DIM, 0.4)
        elif not voices:
            _line("ADD A REFERENCE VOICE", 214, KN_FS_BIG, KN_TEXT_DIM, 0.5)
            _line("use ADD TO VOICES (bottom left)", 236, KN_FS_SMALL, KN_TEXT_DIM, 0.4)
        else:
            _line("READY TO CONVERT", 214, KN_FS_BIG, KN_TEXT)
            _line(f"Ref: {(ref_sel or 'none')[:24]}   |   Src: CH{active_chs[0]}", 236, KN_FS_SMALL, KN_TEXT_DIM, 0.6)

    # ── GENERATE / PREVIEW ────────────────────────────────────────────────────
    _has_out_gen = False
    try:
        from core.ai_knnvc import has_rack_output as _hro
        _has_out_gen = _hro(ai_idx)
    except Exception:
        pass
    if status == _PROCESSING:
        c_lbl = "GENERATING..."
    elif _has_out_gen:
        c_lbl = "PLACE ON TRACK"
    else:
        c_lbl = "GENERATE"
    gr = R(KN_GENERATE)
    _kn_text(c_lbl, (gr[0] + gr[2]) / 2, (gr[1] + gr[3]) / 2, KN_FS_LABEL, g, KN_TEXT, lv, "c")
    _has_out = _is_playing = False
    try:
        from core.ai_knnvc import has_rack_output, is_rack_previewing
        _has_out = has_rack_output(ai_idx)
        _is_playing = is_rack_previewing(ai_idx)
    except Exception:
        pass
    pr = R(KN_PREVIEW)
    _kn_text("■ STOP" if _is_playing else "> PREVIEW", (pr[0] + pr[2]) / 2, (pr[1] + pr[3]) / 2,
             KN_FS_LABEL, g, KN_TEXT if (_has_out or _is_playing) else KN_TEXT_DIM, 1.0, "c")   # painted on metal: stays lit

    # ── OUT CH ────────────────────────────────────────────────────────────────
    out_ch_val = int(getattr(rack, "p3", 0.0)) or (active_chs[0] + 1 if active_chs else 2)
    out_ch_val = max(1, min(9, out_ch_val))
    ob = R(KN_OUT_BOX)
    _kn_text(str(out_ch_val), (ob[0] + ob[2]) / 2, (ob[1] + ob[3]) / 2, 22.0, g, KN_TEXT, lv, "c")

    # ── SRC keys (add-from-timeline channel) ──────────────────────────────────
    add_ch = int(getattr(rack, "p5", 0.0))
    for ci in range(9):
        kr = R(g.src_key(ci))
        issel = (ci == add_ch)
        if issel:
            _kn_blit("rack_knnvc_key_lit", kr, k=lv)
        _kn_text(str(ci + 1), (kr[0] + kr[2]) / 2, (kr[1] + kr[3]) / 2, KN_FS_SMALL, g,
                 KN_TEXT_HOT if issel else KN_TEXT_DIM, lv, "c", glow=KN_GLOW if issel else 0.35)

    # ── name field ────────────────────────────────────────────────────────────
    _name_active = False
    _name_cursor = 0
    try:
        from ui.mixer.interaction import _active_text_field as _atf
        if _atf and _atf.get('ai_idx') == ai_idx and _atf.get('field') == 'add_voice_name':
            _name_active = True
            _name_cursor = _atf.get('cursor', 0)
    except ImportError:
        pass
    name_val = rack.get('add_voice_name', None)
    if name_val is None:
        name_val = f"CH{add_ch + 1}Sample"
        rack['add_voice_name'] = name_val
    nr = R(KN_NAME)
    ncy = (nr[1] + nr[3]) / 2
    nx = nr[0] + 8 * g.s
    if name_val:
        disp_name = name_val[:28] if len(name_val) <= 28 else name_val[:27] + "..."
        _kn_text(disp_name, nx, ncy, KN_FS_MED, g, KN_TEXT, lv)
    else:
        _kn_text("enter name...", nx, ncy, KN_FS_MED, g, KN_TEXT_DIM, lv, glow=0.4)
    if _name_active and int(now * 2) % 2 == 0:
        fs_px = max(1, int(round(KN_FS_MED * g.s)))
        pre = name_val[:min(_name_cursor, len(name_val))]
        try:
            from ui.racks.pp_text import text_width as _ptw2
            cw = _ptw2(pre, fs_px)
        except Exception:
            cw = _text_width(pre, fs_px)
        cxp = nx + cw
        _kn_quad(sh, cxp, nr[1] + 2 * g.s, cxp + max(1.0, 1.4 * g.s), nr[3] - 2 * g.s, (1.0, 0.35, 0.28, 1.0))

    # ── ADD TO VOICES ─────────────────────────────────────────────────────────
    ar_ = R(KN_ADD)
    add_busy = rack.get('add_voice_busy', False)
    _kn_text("ADDING..." if add_busy else "ADD TO VOICES >", (ar_[0] + ar_[2]) / 2, (ar_[1] + ar_[3]) / 2,
             KN_FS_LABEL, g, KN_TEXT_DIM if add_busy else KN_TEXT, 1.0, "c")   # painted on metal: stays lit

    # ── knobs ─────────────────────────────────────────────────────────────────
    vals = (int(2 + float(getattr(rack, "p0", 0.5)) * 6), str(int(10 + float(getattr(rack, "p1", 0.5)) * 50)) + "s")
    for ki, (kx, ky) in enumerate(KN_KNOBS):
        v = max(0.0, min(1.0, float(getattr(rack, f"p{ki}", 0.5))))
        cx, cy = g.X(kx), g.Y(ky)
        _kn_arc_meter(g, ki, v, lv)
        _kn_pointer(sh, cx, cy, KN_KNOB_PTR_LEN * g.s, KN_KNOB_PTR_W * g.s,
                    KN_KNOB_START_DEG - KN_KNOB_SWEEP * v, lv)
        _kn_text(str(vals[ki]), cx, g.Y(KN_KNOB_VAL_Y), KN_FS_MED, g, KN_TEXT, lv, "c")

    # ── status bar ────────────────────────────────────────────────────────────
    ref_name = "none"
    if voices:
        si = int(getattr(rack, "p4", 0.0)) % max(1, len(voices))
        ref_name = os.path.splitext(os.path.basename(voices[si][1]))[0] if si < len(voices) else "none"
    topk_disp = int(2 + float(getattr(rack, "p0", 0.5)) * 6)
    src_str = f"SRC:CH{active_chs[0]}" if active_chs else "SRC:unset"
    out_ch_sb = int(getattr(rack, "p3", 0.0)) or (active_chs[0] + 1 if active_chs else 2)
    sb = R(KN_STATUS)
    _kn_text(f"ENGINE: kNN-VC  |  {src_str}  |  OUT:CH{out_ch_sb}  |  REF:{ref_name}  |  TOPK:{topk_disp}  |  OFFLINE",
             sb[0] + 14 * g.s, (sb[1] + sb[3]) / 2, KN_FS_SMALL, g, KN_TEXT_DIM, lv, glow=0.6)
