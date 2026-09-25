# =============================================================================
# ui/racks/rack_whisper.py
# Whisper speech-to-text rack — faster-whisper, system Python (BETA).
#
# 3-column layout:
#   LEFT   — input channel selector, output channel selector, model size
#   CENTRE — font picker (system fonts), font size, style flags, position
#   RIGHT  — audio language, transcribe/translate mode, VAD toggle, SRT toggle
#   FOOTER — status bar + TRANSCRIBE button
#
# Params on PB_AIRackSettings:
#   p0  model index     (0=tiny 1=base 2=small 3=medium 4=large-v3)
#   p1  language index  (0=auto, 1=en, 2=de, ...)
#   p2  mode            (0=transcribe, 1=translate to EN)
#   p3  output channel  (1-based int stored as float)
#   p4  font size       (12-200, default 48)
#   p5  style flags     (bit: 1=bold 2=italic 4=underline 8=shadow 16=box)
#   p6  position        (0=top 1=mid 2=bot)
#   p7  VAD filter      (0=off 1=on, default 1)
#   ch0..ch8 — input channel (single-select)
#   wsp_font_path  — path to selected .ttf/.otf
#   ai_text        — display name of selected font
#   rack['wsp_srt_enabled']  — bool custom prop (default True)
#   wsp_srt_path   — SRT output path
# =============================================================================

import os
import sys
import subprocess
import threading
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

# ── Colour palette ─────────────────────────────────────────────────────────────
_BG         = (0.01, 0.04, 0.02, 1.0)
_BORDER     = (0.06, 0.24, 0.12, 1.0)
_PANEL      = (0.01, 0.03, 0.02, 1.0)
_PANEL_SEL  = (0.04, 0.16, 0.08, 1.0)
_ACCENT     = (0.10, 0.90, 0.45, 1.0)
_ACCENT_DIM = (0.04, 0.30, 0.16, 1.0)
_TEXT       = (0.55, 0.95, 0.72, 1.0)
_TEXT_DIM   = (0.16, 0.42, 0.26, 1.0)
_TEXT_LABEL = (0.08, 0.22, 0.12, 1.0)
_GREEN      = (0.05, 0.80, 0.30, 1.0)
_AMBER      = (0.86, 0.53, 0.00, 1.0)
_RED        = (0.90, 0.12, 0.12, 1.0)
_WARN_BG    = (0.06, 0.04, 0.00, 1.0)
_WARN_BORDER= (0.55, 0.33, 0.00, 1.0)
_WARN_TEXT  = (0.86, 0.53, 0.00, 1.0)
_WARN_DIM   = (0.50, 0.28, 0.00, 1.0)

RACK_RAIL_H = 32
_READY      = "READY"
_PROCESSING = "PROCESSING"
_DONE       = "DONE"
_ERROR      = "ERROR"

_MODEL_LABELS = ["TINY", "BASE", "SMALL", "MEDIUM", "LARGE-V3"]
_MODEL_DESCS  = [
    "39M  fastest, English only",
    "74M  good for English (default)",
    "244M  good multilingual",
    "769M  high accuracy",
    "1550M  best accuracy, slow",
]

_LANG_LABELS = [
    "Auto-detect", "English", "German", "French", "Spanish",
    "Italian", "Japanese", "Chinese", "Russian", "Portuguese",
    "Dutch", "Korean", "Arabic", "Hindi", "Polish", "Swedish",
]

_STYLE_NAMES = ["B", "I", "U", "SH", "BOX"]
_STYLE_FLAGS = [1,   2,  4,   8,   16 ]

# =============================================================================
# STYLE BUTTON TUNING (B / I / U / SH / BOX)
# Exposes position and size of the 5 style-flag buttons independently of the
# column's auto-computed proportions, so they can be lined up with the
# baked-in button art.
#   WSP_STYLE_Y_OFFSET / WSP_STYLE_H_SCALE : shared across the whole row —
#       moves/resizes all 5 buttons together vertically (unscaled px / mult).
#   WSP_STYLE_X_OFFSETS / WSP_STYLE_W_SCALES : ONE ENTRY PER BUTTON, indexed
#       0-4 to match B / I / U / SH / BOX. Each button's baked-in art can be
#       a slightly different width, so each gets its own x nudge (unscaled
#       px, from its auto-computed evenly-spaced slot) and its own width
#       multiplier (1.0 = auto width). Nudging/resizing one button doesn't
#       shift the others — they're all positioned independently from the
#       same evenly-spaced baseline.
#   WSP_STYLE_GAP : unscaled px — only used to compute the auto baseline
#       spacing that the per-button offsets nudge from (was hardcoded 3.0).
# =============================================================================
WSP_STYLE_Y_OFFSET  = 3.75
WSP_STYLE_H_SCALE   = 0.8
WSP_STYLE_GAP       = 6.0
WSP_STYLE_X_OFFSETS = [3.0, 3.0, 2.5, 2.5, 2.0]   # B, I, U, SH, BOX
WSP_STYLE_W_SCALES  = [1.0, 0.97, 0.96, 0.95, 0.96]   # B, I, U, SH, BOX

# =============================================================================
# POSITION BUTTON TUNING (TOP / MID / BOT)
# Same knobs as the style buttons above, applied to the 3 caption-position
# buttons (WSP_POSITION_X_OFFSETS / WSP_POSITION_W_SCALES indexed 0-2 to
# match TOP / MID / BOT).
# =============================================================================
WSP_POSITION_Y_OFFSET  = 2.5
WSP_POSITION_H_SCALE   = 0.8
WSP_POSITION_GAP       = 6.0
WSP_POSITION_X_OFFSETS = [3.5, 7.5, 10.0]   # TOP, MID, BOT
WSP_POSITION_W_SCALES  = [1.02, 1.0, 0.9]   # TOP, MID, BOT

# =============================================================================
# MODE BUTTON TUNING (TRANSCRIBE / TRANSLATE→EN)
# Same knobs as above, applied to the 2 transcribe-mode buttons in the right
# column (WSP_MODE_X_OFFSETS / WSP_MODE_W_SCALES indexed 0-1 to match
# TRANSCRIBE / TRANSLATE→EN).
# =============================================================================
WSP_MODE_Y_OFFSET  = 1.5
WSP_MODE_H_SCALE   = 0.75
WSP_MODE_GAP       = 10.0
WSP_MODE_X_OFFSETS = [-1.0, 6.0]   # TRANSCRIBE, TRANSLATE→EN
WSP_MODE_W_SCALES  = [1.05, 0.98]   # TRANSCRIBE, TRANSLATE→EN

# =============================================================================
# FONT LIST TEXT NUDGE
# Unscaled px, + = right/up. Moves the font name text drawn inside the font
# picker list (and the "Scanning fonts..." placeholder) as one block — does
# not affect the list's selection highlight bounds or scroll behaviour.
# =============================================================================
WSP_FONT_LIST_X_OFFSET = 5.0
WSP_FONT_LIST_Y_OFFSET = 0.0

# Multiplier on the auto-computed font list box width (1.0 = unchanged).
# Shrinks the list box AND its per-row selection highlight together so the
# highlight stays flush with the box edge and matches the baked-in art's
# list boundary. Scroll buttons stay pinned to the (now narrower) box's
# right edge automatically.
WSP_FONT_LIST_W_SCALE = 0.95

# =============================================================================
# VAD / SRT TOGGLE Y NUDGE
# Unscaled px, + = up. Moves the whole toggle pill — housing, dot, AND its
# click zone together (Racks.py's hit-test applies this same offset) — so it
# can be aligned to the baked-in art's circle without the clickable area
# drifting away from the visible dot. (Previously nudged only the dot,
# which visually matched the art but left the hit zone behind — this is
# the fix for that mismatch.)
# =============================================================================
WSP_VAD_Y_OFFSET = -2.0
WSP_SRT_Y_OFFSET = -2.0

# =============================================================================
# SRT BROWSE BUTTON
# Unscaled px. Carves a small "..." button from the right edge of the SRT
# path box (only shown while SRT export is on) to open a file browser and
# set wsp_srt_path. X/Y offsets nudge it, + = right/up.
# =============================================================================
WSP_SRT_BROWSE_BTN_W    = 26.0
WSP_SRT_BROWSE_X_OFFSET = 0.0
WSP_SRT_BROWSE_Y_OFFSET = 0.0

# ── System font scanner ────────────────────────────────────────────────────────
_font_cache    = None
_font_scan_run = False
_font_lock     = threading.Lock()

# Per-rack font scroll offset (ai_idx -> int)
_font_scroll   = {}


def _scan_fonts_worker():
    global _font_cache, _font_scan_run
    dirs = []
    if sys.platform == "win32":
        dirs = [
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Microsoft", "Windows", "Fonts"),
        ]
    elif sys.platform == "darwin":
        dirs = ["/Library/Fonts", "/System/Library/Fonts",
                os.path.expanduser("~/Library/Fonts")]
    else:
        dirs = ["/usr/share/fonts", "/usr/local/share/fonts",
                os.path.expanduser("~/.fonts"),
                os.path.expanduser("~/.local/share/fonts")]
    fonts = []
    seen  = set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in sorted(files):
                if f.lower().endswith((".ttf", ".otf")):
                    full = os.path.join(root, f)
                    key  = f.lower()
                    if key not in seen:
                        seen.add(key)
                        fonts.append((os.path.splitext(f)[0], full))
    fonts.sort(key=lambda x: x[0].lower())
    with _font_lock:
        _font_cache    = fonts
        _font_scan_run = False


def get_system_fonts():
    global _font_scan_run
    with _font_lock:
        if _font_cache is not None:
            return _font_cache
        if not _font_scan_run:
            _font_scan_run = True
            threading.Thread(target=_scan_fonts_worker, daemon=True).start()
        return []


# ── Dependency check ───────────────────────────────────────────────────────────
_dep_cache = {"ok": False, "checked": False, "checking": False}
_dep_running = False


def _dep_worker():
    global _dep_running
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
                    cmd + ["-m", "pip", "show", "faster-whisper"],
                    capture_output=True, timeout=5)
                if r.returncode == 0:
                    ok = True
                    break
            except Exception:
                continue
    except Exception as e:
        print(f"[WHISPER] dep check error: {e}")
    _dep_cache["ok"]       = ok
    _dep_cache["checked"]  = True
    _dep_cache["checking"] = False
    _dep_running           = False
    print(f"[WHISPER] dep check complete: {'found' if ok else 'not found'}")
    try:
        import bpy
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ('NODE_EDITOR', 'SEQUENCE_EDITOR'):
                    area.tag_redraw()
    except Exception:
        pass
def _check_dep():
    global _dep_running
    if _dep_cache["checked"]:
        return _dep_cache["ok"]
    if not _dep_running:
        _dep_running           = True
        _dep_cache["checking"] = True
        threading.Thread(target=_dep_worker, daemon=True).start()
    return False


# ── GPU helpers ────────────────────────────────────────────────────────────────
def _r(rx, ry, rw, rh, col):
    _draw_rect(rx, ry, rw, rh, col)


def _box(rx, ry, rw, rh, col):
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    vs = [(rx, ry), (rx+rw, ry), (rx+rw, ry+rh), (rx, ry+rh), (rx, ry)]
    b  = batch_for_shader(sh, "LINE_STRIP", {"pos": vs})
    sh.bind(); sh.uniform_float("color", col); b.draw(sh)


def _pill(px, py, pw, ph, on, scale, has_skin=False):
    # Switch housing (fill + border) suppressed once skinned — expected to be
    # baked into the art as the toggle's physical slot; the coloured dot
    # itself still draws every time since its position/colour is the live
    # on/off state and can't be pre-baked. Callers nudge the WHOLE pill
    # (py itself) to align with the baked art — Racks.py's hit-test applies
    # the same nudge, so the click zone always travels with the visible dot.
    col = _ACCENT if on else _ACCENT_DIM
    if not has_skin:
        bg = (0.04, 0.20, 0.10, 1.0) if on else (0.01, 0.04, 0.02, 1.0)
        _r(px, py, pw, ph, bg)
        _box(px, py, pw, ph, col)
    dr = ph * 0.36
    dx = (px + pw - dr * 2 - ph * 0.1) if on else (px + ph * 0.1)
    _draw_circle(dx + dr, py + ph * 0.5, dr, col)


def _sel_row(lx, sy, col_w, row_h, arr_w, val_str, scale, has_skin=False):
    # Arrow buttons + value box chrome suppressed once skinned (baked into
    # the art either side of the value display, same treatment as the OUT CH
    # box in rack_voicefixer.py); the current value text always draws.
    fs_v = max(1, int(9 * scale))
    nbw  = col_w - arr_w * 2 - 4 * scale
    nbx  = lx + arr_w + 2 * scale
    rax  = nbx + nbw + 2 * scale
    if not has_skin:
        for bx, lbl in [(lx, "<"), (rax, ">")]:
            _r(bx, sy, arr_w, row_h, (0.03, 0.08, 0.04, 1.0))
            _box(bx, sy, arr_w, row_h, _ACCENT_DIM)
            tw = _text_width(lbl, fs_v)
            _draw_text(lbl, bx + arr_w/2 - tw/2, sy + row_h/2 - fs_v/2, fs_v, _ACCENT)
        _r(nbx, sy, nbw, row_h, _PANEL)
        _box(nbx, sy, nbw, row_h, _ACCENT_DIM)
    tw2 = _text_width(val_str, fs_v)
    _draw_text(val_str, nbx + nbw/2 - tw2/2, sy + row_h/2 - fs_v/2, fs_v, _ACCENT)


def _draw_text_bold(text, x, y, fs, col, scale):
    # No font-weight control in the GPU text path, so fake bold by drawing
    # the same string twice with a hairline horizontal offset — just enough
    # to thicken the strokes without visibly doubling the glyphs.
    _draw_text(text, x, y, fs, col)
    _draw_text(text, x + 0.6 * scale, y, fs, col)


def _hdiv(lx, y, col_w, scale):
    sh = gpu.shader.from_builtin("UNIFORM_COLOR")
    b  = batch_for_shader(sh, "LINES", {"pos": [(lx, y), (lx + col_w, y)]})
    sh.bind(); sh.uniform_float("color", (0.04, 0.14, 0.06, 1.0)); b.draw(sh)


# ── Warning panel ──────────────────────────────────────────────────────────────
def _draw_warning(rx, ry, rw, rh, scale):
    mg   = 8 * scale
    bw   = rw - mg * 4
    bh   = min(rh * 0.80, 175 * scale)
    bx   = rx + (rw - bw) / 2
    by   = ry + (rh - bh) / 2
    _r(bx, by, bw, bh, _WARN_BG)
    _r(bx, by, 4 * scale, bh, _WARN_TEXT)
    _box(bx, by, bw, bh, _WARN_BORDER)

    tx   = bx + 14 * scale
    fs_h = max(1, int(9 * scale))
    fs_b = max(1, int(8 * scale))
    fs_s = max(1, int(7 * scale))
    lh   = fs_b + 5 * scale
    ty   = by + bh - fs_h - 8 * scale

    _draw_text("FASTER-WHISPER NOT INSTALLED", tx, ty, fs_h, _WARN_TEXT)
    ty -= lh * 1.5
    _draw_text("Run in your terminal:", tx, ty, fs_b, _WARN_DIM)
    ty -= lh * 1.2
    cw = bw - 28 * scale; ch = lh * 1.6 + 6 * scale; cx2 = tx; cy2 = ty - ch
    _r(cx2, cy2, cw, ch, (0.02, 0.01, 0.00, 1.0))
    _box(cx2, cy2, cw, ch, (0.44, 0.24, 0.00, 1.0))
    _draw_text("pip install faster-whisper",
               cx2 + 6 * scale, cy2 + ch / 2 - fs_b / 2, fs_b, _WARN_TEXT)
    ty = cy2 - lh * 1.2
    _draw_text("CUDA auto-used if available.  CPU also works.", tx, ty, fs_s, _WARN_DIM)
    ty -= lh
    _draw_text("Models download on first run (~150MB for base).", tx, ty, fs_s, _WARN_DIM)

    try:
        from core import ai_pydeps as _pydeps
        _wsp_lines, _wsp_btn_lbl, _wsp_kind = _pydeps.get_progress_display("whisper")
    except Exception:
        _wsp_lines, _wsp_btn_lbl, _wsp_kind = [], "INSTALL AUTOMATICALLY  >", "idle"

    _wsp_status_y = cy2 + ch + 4 * scale
    if _wsp_kind == "busy":
        for _i, _line in enumerate(_wsp_lines):
            _draw_text(_line, tx, _wsp_status_y + _i * (fs_s + 3*scale), fs_s, _WARN_TEXT)
        # Animated activity bar — pip doesn't reliably report byte-level %
        # progress when it isn't talking to a real terminal, so this shows
        # "still working" rather than a fabricated percentage.
        import time as _wsp_time
        _bar_w = bw - 28 * scale
        _bar_h = max(3 * scale, 3)
        _bar_x = tx
        _bar_y = _wsp_status_y + len(_wsp_lines) * (fs_s + 3*scale) + 3*scale
        _r(_bar_x, _bar_y, _bar_w, _bar_h, (0.10, 0.07, 0.0, 1.0))
        _seg_w = _bar_w * 0.28
        _t     = (_wsp_time.time() * 0.35) % 1.0
        _pos   = _t * (_bar_w + _seg_w) - _seg_w
        _seg_x = max(_bar_x, min(_bar_x + _bar_w - _seg_w, _bar_x + _pos))
        _r(_seg_x, _bar_y, min(_seg_w, _bar_x + _bar_w - _seg_x), _bar_h, _WARN_TEXT)
    elif _wsp_kind == "error" and _wsp_lines:
        _draw_text(_wsp_lines[0], tx, _wsp_status_y, fs_s, _WARN_TEXT)

    dw = min(170 * scale, bw * 0.44); dh = max(16 * scale, fs_s + 8 * scale)
    dx2 = bx + bw - dw - 12 * scale; dy2 = by + 8 * scale
    if _wsp_kind == "error":
        d_bg, d_edge = (0.10, 0.02, 0.02, 1.0), (1.0, 0.35, 0.3, 1.0)
    elif _wsp_kind == "busy":
        d_bg, d_edge = (0.06, 0.04, 0.00, 1.0), _WARN_DIM
    else:
        d_bg, d_edge = (0.10, 0.06, 0.00, 1.0), _WARN_BORDER
    lbl = _wsp_btn_lbl
    _r(dx2, dy2, dw, dh, d_bg)
    _box(dx2, dy2, dw, dh, d_edge)
    tw_d = _text_width(lbl, fs_s)
    _draw_text(lbl, dx2 + dw / 2 - tw_d / 2,
               dy2 + dh / 2 - fs_s / 2, fs_s, _WARN_TEXT)
    if _wsp_kind == "error" and _wsp_lines:
        _draw_text(_wsp_lines[0][:70], tx, dy2 + dh + 4*scale,
                    max(1, int(6*scale)), (1.0, 0.45, 0.4, 1.0))
    return dx2, dy2, dw, dh


def _start_whisper_install():
    from core import ai_pydeps as _pydeps

    # The warning-panel button doubles as CANCEL while an install is
    # already running (see _draw_warning's get_progress_display() call) —
    # same button rect, same click handler, branch on current state here.
    if _pydeps.is_installing("whisper"):
        _pydeps.cancel_install("whisper")
        return

    def _on_done(success):
        _dep_cache["checked"] = False

    _pydeps.start_install(
        "whisper",
        pip_specs=["faster-whisper"],
        check_module="faster_whisper",
        on_done=_on_done,
    )


# ── Main draw ──────────────────────────────────────────────────────────────────
def _draw_whisper_body(rx, ry, rw, rh, rack, ai_idx, scale):
    # Full-rack photoreal skin — when present, Racks.py's _draw_ai_rack_expanded
    # has already blit the whole unit (rail + body) before calling this
    # function, so the flat panel fills/borders below are skipped entirely and
    # only dynamic content (selection highlights, live values, status text)
    # draws on top at the same coordinates. Falls back to the old flat panel
    # look if the PNG isn't found yet — same convention as rack_knnvc.py /
    # rack_voicefixer.py's _has_skin gating.
    # (Replaces the old body-only draw_element() skin attempt, which predates
    # the full-unit blit Racks.py now does before dispatching here.)
    try:
        from ui.mixer.texture_cache import get_texture as _gtc_wsp
        _has_skin = _gtc_wsp("rack_whisper_bg") is not None
    except Exception:
        _has_skin = False

    rail_h   = RACK_RAIL_H * scale
    body_bot = ry
    body_top = ry + rh - rail_h
    body_h   = body_top - body_bot

    # Deps not ready yet — Racks.py's _draw_ai_rack_expanded withholds the
    # full-unit skin blit in this state (mirrors the RVC/VoiceFixer dep-check
    # gating), so even though the skin texture may already be loaded in
    # cache, nothing has actually been drawn behind us. These branches always
    # draw their own flat black body chrome regardless of _has_skin.
    if not _check_dep():
        if _dep_cache["checking"]:
            # Still checking — show brief status instead of full warning
            _r(rx, body_bot, rw, body_h, _BG)
            fs = max(1, int(8 * scale))
            msg = "Checking Whisper installation..."
            from ui.mixer.draw_utils import text_width as _tw
            tw = _tw(msg, fs)
            _draw_text(msg, rx + rw/2 - tw/2, body_bot + body_h/2 - fs/2,
                       fs, (0.4, 0.5, 0.6, 1.0))
            return
        _r(rx, body_bot, rw, body_h, _BG)
        _box(rx, body_bot, rw, body_h, _BORDER)
        try:
            rack['wsp_setup_btn'] = _draw_warning(rx, body_bot, rw, body_h, scale)
        except Exception:
            _draw_warning(rx, body_bot, rw, body_h, scale)
        return

    get_system_fonts()   # kick off scan if not done

    if not _has_skin:
        _r(rx, body_bot, rw, body_h, _BG)
        _box(rx, body_bot, rw, body_h, _BORDER)

    mg       = 8 * scale
    fs_lbl   = max(1, int(8 * scale))
    fs_val   = max(1, int(9 * scale))
    fs_sm    = max(1, int(7 * scale))
    row_h    = max(18 * scale, body_h * 0.105)
    arr_w    = max(14 * scale, row_h * 0.9)
    btn_gap  = 3 * scale
    fitem_h  = max(12 * scale, fs_sm + 4 * scale)

    # ── Read all params ────────────────────────────────────────────────────────
    status     = getattr(rack, "ai_status", _READY)
    model_idx  = max(0, min(int(getattr(rack, "p0", 1.0)), len(_MODEL_LABELS) - 1))
    lang_idx   = max(0, min(int(getattr(rack, "p1", 0.0)), len(_LANG_LABELS) - 1))
    mode_idx   = int(getattr(rack, "p2", 0.0))
    out_ch     = int(getattr(rack, "p3", 0.0))   # 1-based; 0 = not set
    font_size  = int(getattr(rack, "p4", 48.0))
    style_flags= int(getattr(rack, "p5", 0.0))
    pos_idx    = int(getattr(rack, "p6", 2.0))   # 0=top 1=mid 2=bot; default bot
    vad_on     = float(getattr(rack, "p7", 1.0)) > 0.5
    srt_on     = rack.get("wsp_srt_enabled", True)
    srt_path   = getattr(rack, "wsp_srt_path", "")
    sel_font_path = getattr(rack, "wsp_font_path", "")
    chunk_len  = getattr(rack, "wsp_chunk_length", 10)
    active_chs = [ci for ci in range(9) if getattr(rack, f"ch{ci}", False)]
    in_ch      = active_chs[0] if active_chs else -1

    status_col = {_READY: _TEXT_DIM, _PROCESSING: _AMBER,
                  _DONE: _GREEN, _ERROR: _RED}.get(status, _TEXT_DIM)

    # ── Status / footer bar ────────────────────────────────────────────────────
    sbar_h   = max(22 * scale, body_h * 0.07)
    sbar_y   = body_bot
    work_bot = sbar_y + sbar_h + 2 * scale
    work_top = body_top - 2 * scale
    work_h   = work_top - work_bot

    if not _has_skin:
        _r(rx, sbar_y, rw, sbar_h, (0.01, 0.03, 0.02, 1.0))
        _box(rx, sbar_y, rw, sbar_h, _BORDER)

    sbar_txt = (f"STATUS: {status}  |  MODEL: {_MODEL_LABELS[model_idx]}  |  "
                f"LANG: {_LANG_LABELS[lang_idx]}  |  OUT CH: {out_ch or '?'}  |  "
                f"SEG: {chunk_len}s  |  VAD: {'ON' if vad_on else 'OFF'}  |  "
                f"MODE: {'TRANSLATE->EN' if mode_idx else 'TRANSCRIBE'}")
    _draw_text(sbar_txt, rx + mg,
               sbar_y + sbar_h / 2 - fs_sm / 2, fs_sm, status_col)

    # TRANSCRIBE button (right end of status bar)
    tbtn_w = min(rw * 0.22, 150 * scale)
    tbtn_x = rx + rw - tbtn_w - mg
    tbtn_y = sbar_y + 2 * scale
    tbtn_h = sbar_h - 4 * scale
    if status == _PROCESSING:
        tb_bg = (0.08, 0.18, 0.04, 1.0); tb_col = _AMBER; tb_lbl = "PROCESSING..."
    else:
        tb_bg = (0.02, 0.18, 0.06, 1.0); tb_col = _ACCENT; tb_lbl = "TRANSCRIBE"
    if not _has_skin:
        _r(tbtn_x, tbtn_y, tbtn_w, tbtn_h, tb_bg)
        _box(tbtn_x, tbtn_y, tbtn_w, tbtn_h, tb_col)
        tw_tb = _text_width(tb_lbl, fs_sm)
        _draw_text(tb_lbl, tbtn_x + tbtn_w / 2 - tw_tb / 2,
                   tbtn_y + tbtn_h / 2 - fs_sm / 2, fs_sm, tb_col)

    # ── 3-column grid ─────────────────────────────────────────────────────────
    col_w  = (rw - mg * 4) / 3
    col1_x = rx + mg
    col2_x = col1_x + col_w + mg
    col3_x = col2_x + col_w + mg

    # Vertical dividers — suppressed once skinned, baked into the art.
    if not _has_skin:
        sh_dv = gpu.shader.from_builtin("UNIFORM_COLOR")
        for dvx in [col1_x + col_w + mg * 0.5, col2_x + col_w + mg * 0.5]:
            dv = [(dvx, work_bot + 4 * scale), (dvx, work_top - 4 * scale)]
            db = batch_for_shader(sh_dv, "LINES", {"pos": dv})
            sh_dv.bind(); sh_dv.uniform_float("color", (0.04, 0.14, 0.06, 1.0)); db.draw(sh_dv)

    # ─ COL 1 ──────────────────────────────────────────────────────────────────
    ch_s  = min(20 * scale, (col_w - btn_gap * 8) / 9)
    y1    = work_top

    # Input channel
    y1 -= fs_lbl + 4 * scale
    if not _has_skin:
        _draw_text("INPUT CHANNEL", col1_x, y1, fs_lbl, _TEXT_LABEL)
    y1 -= ch_s + 4 * scale
    for ci in range(9):
        bx = col1_x + ci * (ch_s + btn_gap)
        sel = (ci == in_ch)
        # Chrome fully suppressed once skinned — the button art (including
        # the selected/lit-up look) is baked into the background; only the
        # channel number's colour still shifts live to reflect selection.
        if not _has_skin:
            _r(bx, y1, ch_s, ch_s, _PANEL_SEL if sel else _PANEL)
            _box(bx, y1, ch_s, ch_s, _ACCENT if sel else _ACCENT_DIM)
        lc = str(ci + 1); tw_c = _text_width(lc, fs_sm)
        _draw_text(lc, bx + ch_s/2 - tw_c/2, y1 + ch_s/2 - fs_sm/2, fs_sm,
                   _ACCENT if sel else _TEXT_LABEL)

    # Output channel
    y1 -= fs_lbl + 10 * scale
    if not _has_skin:
        _draw_text("OUTPUT CHANNEL (TEXT STRIPS)", col1_x, y1, fs_lbl, _TEXT_LABEL)
    y1 -= ch_s + 4 * scale
    for ci in range(9):
        bx = col1_x + ci * (ch_s + btn_gap)
        sel = (ci + 1 == out_ch)
        if not _has_skin:
            _r(bx, y1, ch_s, ch_s, _PANEL_SEL if sel else _PANEL)
            _box(bx, y1, ch_s, ch_s, _ACCENT if sel else _ACCENT_DIM)
        lc = str(ci + 1); tw_c = _text_width(lc, fs_sm)
        _draw_text(lc, bx + ch_s/2 - tw_c/2, y1 + ch_s/2 - fs_sm/2, fs_sm,
                   _ACCENT if sel else _TEXT_LABEL)

    # Divider + model
    y1 -= 8 * scale
    if not _has_skin:
        _hdiv(col1_x, y1, col_w, scale)
    y1 -= 6 * scale

    y1 -= fs_lbl
    if not _has_skin:
        _draw_text("MODEL SIZE", col1_x, y1, fs_lbl, _TEXT_LABEL)
    y1 -= row_h + 2 * scale
    _sel_row(col1_x, y1, col_w, row_h, arr_w, _MODEL_LABELS[model_idx], scale, has_skin=_has_skin)
    y1 -= fs_sm + 4 * scale
    _draw_text_bold(_MODEL_DESCS[model_idx], col1_x, y1, fs_sm, _TEXT_DIM, scale)

    # Max segment length (chunk_length) — controls subtitle density
    y1 -= fs_lbl + 8 * scale
    if not _has_skin:
        _draw_text("MAX SEGMENT LENGTH", col1_x, y1, fs_lbl, _TEXT_LABEL)
    y1 -= row_h + 2 * scale
    _sel_row(col1_x, y1, col_w, row_h, arr_w, f"{chunk_len}s", scale, has_skin=_has_skin)
    y1 -= fs_sm + 4 * scale
    _draw_text_bold("5s = more strips   30s = fewer strips", col1_x, y1, fs_sm, _TEXT_DIM, scale)

    # ─ COL 2 ──────────────────────────────────────────────────────────────────
    fonts = get_system_fonts()
    y2    = work_top

    y2 -= fs_lbl + 4 * scale
    if not _has_skin:
        _draw_text("FONT", col2_x, y2, fs_lbl, _TEXT_LABEL)

    flist_h  = min(work_h * 0.30, fitem_h * 7)
    scroll_btn_w = max(14 * scale, fitem_h * 0.9)
    # flist_w_auto is the un-narrowed width — the scroll buttons anchor off
    # this so their (baked-in-art) position never moves when the box is
    # narrowed below. flist_w is what WSP_FONT_LIST_W_SCALE actually shrinks:
    # the visible box + its selection highlight, from the right edge inward.
    flist_w_auto = col_w - scroll_btn_w - 2 * scale
    flist_w  = flist_w_auto * WSP_FONT_LIST_W_SCALE
    y2      -= flist_h + 2 * scale
    flist_y  = y2
    # List container + scroll button chrome suppressed once skinned (baked
    # into the art as the "screen" cutout); per-row selection highlight,
    # font names, and the scroll position indicator still draw live below.
    if not _has_skin:
        _r(col2_x, flist_y, flist_w, flist_h, (0.01, 0.03, 0.01, 1.0))
        _box(col2_x, flist_y, flist_w, flist_h, _BORDER)

    # Scroll buttons — ▲ top, ▼ bottom, to the right of the list. Anchored
    # off flist_w_auto (not flist_w) so WSP_FONT_LIST_W_SCALE never moves
    # them — their baked-in art position is independent of the list box width.
    sbtn_x = col2_x + flist_w_auto + 2 * scale
    sbtn_h = flist_h / 2 - 1 * scale
    if not _has_skin:
        _r(sbtn_x, flist_y + sbtn_h + 2 * scale, scroll_btn_w, sbtn_h, (0.02, 0.06, 0.03, 1.0))
        _box(sbtn_x, flist_y + sbtn_h + 2 * scale, scroll_btn_w, sbtn_h, _ACCENT_DIM)
        tw_up = _text_width("▲", fs_sm)
        _draw_text("▲", sbtn_x + scroll_btn_w/2 - tw_up/2,
                   flist_y + flist_h*0.75 - fs_sm/2, fs_sm, _ACCENT)

        _r(sbtn_x, flist_y, scroll_btn_w, sbtn_h, (0.02, 0.06, 0.03, 1.0))
        _box(sbtn_x, flist_y, scroll_btn_w, sbtn_h, _ACCENT_DIM)
        tw_dn = _text_width("▼", fs_sm)
        _draw_text("▼", sbtn_x + scroll_btn_w/2 - tw_dn/2,
                   flist_y + flist_h*0.25 - fs_sm/2, fs_sm, _ACCENT)

    if fonts:
        vis = max(1, int(flist_h / fitem_h))
        # Use explicit scroll offset if set, otherwise centre on selection
        if ai_idx in _font_scroll:
            scroll = max(0, min(_font_scroll[ai_idx], len(fonts) - vis))
        else:
            sel_fi = 0
            for fi, (fn, fp) in enumerate(fonts):
                if fp == sel_font_path:
                    sel_fi = fi; break
            scroll = max(0, min(sel_fi - vis // 2, len(fonts) - vis))
            _font_scroll[ai_idx] = scroll

        for slot in range(vis):
            fi = scroll + slot
            if fi >= len(fonts): break
            fn, fp = fonts[fi]
            iy  = flist_y + flist_h - (slot + 1) * fitem_h
            sel = (fp == sel_font_path)
            if sel:
                _r(col2_x + 1 * scale, iy, flist_w - 2 * scale, fitem_h, _PANEL_SEL)
            disp = fn[:22] if len(fn) <= 22 else fn[:21] + "\u2026"
            _draw_text(disp, col2_x + 5 * scale + WSP_FONT_LIST_X_OFFSET * scale,
                       iy + fitem_h / 2 - fs_sm / 2 + WSP_FONT_LIST_Y_OFFSET * scale, fs_sm,
                       _ACCENT if sel else _TEXT_DIM)

        # Scroll position indicator \u2014 suppressed once skinned; the scroll
        # arrows/box it sits next to are baked into the art and no longer
        # drawn, so leaving this on would show a stray square next to them.
        if not _has_skin and len(fonts) > vis:
            pct = scroll / max(1, len(fonts) - vis)
            ind_h = max(4 * scale, sbtn_h * 0.3)
            ind_y = flist_y + sbtn_h * 0.1 + pct * (sbtn_h * 0.8 - ind_h)
            _r(sbtn_x + 2 * scale, ind_y, scroll_btn_w - 4 * scale, ind_h, _ACCENT_DIM)
    else:
        _draw_text("Scanning fonts...", col2_x + 5 * scale + WSP_FONT_LIST_X_OFFSET * scale,
                   flist_y + flist_h / 2 - fs_sm / 2 + WSP_FONT_LIST_Y_OFFSET * scale, fs_sm, _TEXT_LABEL)

    # Size
    y2 -= fs_lbl + 8 * scale
    if not _has_skin:
        _draw_text("SIZE", col2_x, y2, fs_lbl, _TEXT_LABEL)
    y2 -= row_h + 2 * scale
    sz_aw = max(14 * scale, row_h * 0.9)
    sz_vw = col_w - sz_aw * 2 - 4 * scale
    if not _has_skin:
        for bx2, lbl2 in [(col2_x, "-"), (col2_x + sz_aw + 2 * scale + sz_vw + 2 * scale, "+")]:
            _r(bx2, y2, sz_aw, row_h, (0.03, 0.08, 0.04, 1.0))
            _box(bx2, y2, sz_aw, row_h, _ACCENT_DIM)
            tw2 = _text_width(lbl2, fs_val)
            _draw_text(lbl2, bx2 + sz_aw/2 - tw2/2, y2 + row_h/2 - fs_val/2, fs_val, _ACCENT)
        sv_x = col2_x + sz_aw + 2 * scale
        _r(sv_x, y2, sz_vw, row_h, _PANEL)
        _box(sv_x, y2, sz_vw, row_h, _ACCENT_DIM)
    else:
        sv_x = col2_x + sz_aw + 2 * scale
    fs_s = str(font_size); tw_s = _text_width(fs_s, fs_val)
    _draw_text(fs_s, sv_x + sz_vw/2 - tw_s/2, y2 + row_h/2 - fs_val/2, fs_val, _ACCENT)

    # Style — position/size exposed via WSP_STYLE_* tuning constants so the
    # buttons can be lined up with the baked-in button art.
    y2 -= fs_lbl + 8 * scale
    if not _has_skin:
        _draw_text("STYLE", col2_x, y2, fs_lbl, _TEXT_LABEL)
    y2 -= row_h + 2 * scale
    _sty_gap     = WSP_STYLE_GAP * scale
    _sty_base_bw = (col_w - 4 * _sty_gap) / 5
    sty_bh       = row_h * WSP_STYLE_H_SCALE
    sty_y_off    = WSP_STYLE_Y_OFFSET * scale
    for si, (sname, sflag) in enumerate(zip(_STYLE_NAMES, _STYLE_FLAGS)):
        sty_bw = _sty_base_bw * WSP_STYLE_W_SCALES[si]
        sbx = col2_x + si * (_sty_base_bw + _sty_gap) + WSP_STYLE_X_OFFSETS[si] * scale
        sby = y2 + sty_y_off
        on  = bool(style_flags & sflag)
        if not _has_skin:
            _r(sbx, sby, sty_bw, sty_bh, _PANEL_SEL if on else _PANEL)
        if not _has_skin or on:
            _box(sbx, sby, sty_bw, sty_bh, _ACCENT if on else _ACCENT_DIM)
        # Label suppressed once skinned — baked into the button art now
        # that the boxes are lined up with it.
        if not _has_skin:
            tw_s2 = _text_width(sname, fs_sm)
            _draw_text(sname, sbx + sty_bw/2 - tw_s2/2, sby + sty_bh/2 - fs_sm/2,
                       fs_sm, _ACCENT if on else _TEXT_LABEL)

    # Position — position/size exposed via WSP_POSITION_* tuning constants.
    y2 -= fs_lbl + 8 * scale
    if not _has_skin:
        _draw_text("POSITION", col2_x, y2, fs_lbl, _TEXT_LABEL)
    y2 -= row_h + 2 * scale
    _pos_gap     = WSP_POSITION_GAP * scale
    _pos_base_bw = (col_w - 2 * _pos_gap) / 3
    pos_bh       = row_h * WSP_POSITION_H_SCALE
    pos_y_off    = WSP_POSITION_Y_OFFSET * scale
    for pi, plbl in enumerate(["TOP", "MID", "BOT"]):
        pos_bw = _pos_base_bw * WSP_POSITION_W_SCALES[pi]
        pbx = col2_x + pi * (_pos_base_bw + _pos_gap) + WSP_POSITION_X_OFFSETS[pi] * scale
        pby = y2 + pos_y_off
        on  = (pi == pos_idx)
        if not _has_skin:
            _r(pbx, pby, pos_bw, pos_bh, _PANEL_SEL if on else _PANEL)
        if not _has_skin or on:
            _box(pbx, pby, pos_bw, pos_bh, _ACCENT if on else _ACCENT_DIM)
        # Label suppressed once skinned — baked into the button art now
        # that the boxes are lined up with it.
        if not _has_skin:
            tw_p = _text_width(plbl, fs_sm)
            _draw_text(plbl, pbx + pos_bw/2 - tw_p/2, pby + pos_bh/2 - fs_sm/2,
                       fs_sm, _ACCENT if on else _TEXT_LABEL)

    # ─ COL 3 ──────────────────────────────────────────────────────────────────
    pill_w = 30 * scale
    pill_h = 14 * scale
    pill_x = col3_x + col_w - pill_w
    y3     = work_top

    # Language
    y3 -= fs_lbl + 4 * scale
    if not _has_skin:
        _draw_text("AUDIO LANGUAGE", col3_x, y3, fs_lbl, _TEXT_LABEL)
    y3 -= row_h + 2 * scale
    _sel_row(col3_x, y3, col_w, row_h, arr_w, _LANG_LABELS[lang_idx], scale, has_skin=_has_skin)
    y3 -= fs_sm + 3 * scale
    # Suppressed once skinned — this hint is baked into the background art
    # next to the language selector now.
    if not _has_skin:
        _draw_text("Set manually if auto-detect is wrong", col3_x, y3, fs_sm, _TEXT_LABEL)

    # Mode \u2014 position/size exposed via WSP_MODE_* tuning constants.
    y3 -= fs_lbl + 8 * scale
    if not _has_skin:
        _draw_text("MODE", col3_x, y3, fs_lbl, _TEXT_LABEL)
    y3 -= row_h + 2 * scale
    _wsp_mode_gap = WSP_MODE_GAP * scale
    _mode_base_bw = (col_w - _wsp_mode_gap) / 2
    mbh = row_h * WSP_MODE_H_SCALE
    wsp_mode_y_off = WSP_MODE_Y_OFFSET * scale
    for mi, mlbl in enumerate(["TRANSCRIBE", "TRANSLATE\u2192EN"]):
        mbw = _mode_base_bw * WSP_MODE_W_SCALES[mi]
        mbx = col3_x + mi * (_mode_base_bw + _wsp_mode_gap) + WSP_MODE_X_OFFSETS[mi] * scale
        mby = y3 + wsp_mode_y_off
        on  = (mi == mode_idx)
        if not _has_skin:
            _r(mbx, mby, mbw, mbh, _PANEL_SEL if on else _PANEL)
        if not _has_skin or on:
            _box(mbx, mby, mbw, mbh, _ACCENT if on else _ACCENT_DIM)
        # Label suppressed once skinned — baked into the button art now
        # that the boxes are lined up with it.
        if not _has_skin:
            tw_m = _text_width(mlbl, fs_sm)
            _draw_text(mlbl, mbx + mbw/2 - tw_m/2, mby + mbh/2 - fs_sm/2,
                       fs_sm, _ACCENT if on else _TEXT_LABEL)
    y3 -= fs_sm + 3 * scale
    if mode_idx == 1:
        _draw_text("Any language \u2192 English only (Whisper limit)",
                   col3_x, y3, fs_sm, _AMBER)
    else:
        _draw_text("Outputs text in source language", col3_x, y3, fs_sm, _TEXT_DIM)

    # Divider
    y3 -= 8 * scale
    if not _has_skin:
        _hdiv(col3_x, y3, col_w, scale)
    y3 -= 6 * scale

    # VAD filter
    y3 -= fs_lbl + 2 * scale
    if not _has_skin:
        _draw_text("VAD FILTER", col3_x, y3, fs_lbl, _TEXT_LABEL)
    _pill(pill_x, y3 - 1 * scale + WSP_VAD_Y_OFFSET * scale, pill_w, pill_h,
          vad_on, scale, has_skin=_has_skin)
    y3 -= pill_h + 2 * scale
    _draw_text("Strips silence before transcribing", col3_x, y3, fs_sm, _TEXT_DIM)

    # SRT export
    y3 -= fs_lbl + 10 * scale
    if not _has_skin:
        _draw_text("SRT EXPORT", col3_x, y3, fs_lbl, _TEXT_LABEL)
    _pill(pill_x, y3 - 1 * scale + WSP_SRT_Y_OFFSET * scale, pill_w, pill_h,
          srt_on, scale, has_skin=_has_skin)
    y3 -= pill_h + 4 * scale
    if srt_on:
        # Browse button carved from the path box's right edge \u2014 this is new
        # interactive functionality with no baked-in art counterpart yet, so
        # it always draws (not gated by _has_skin) until the art is updated
        # to include a matching button graphic.
        bbw = WSP_SRT_BROWSE_BTN_W * scale
        pbox_w = col_w - bbw - 2 * scale
        path_disp = srt_path if srt_path else "(no path set)"
        max_c = int(pbox_w / max(1, fs_sm * 0.55))
        if len(path_disp) > max_c:
            path_disp = "\u2026" + path_disp[-(max_c - 1):]
        if not _has_skin:
            _r(col3_x, y3, pbox_w, fitem_h, (0.01, 0.04, 0.02, 1.0))
            _box(col3_x, y3, pbox_w, fitem_h, _ACCENT_DIM)
        _draw_text(path_disp, col3_x + 4 * scale,
                   y3 + fitem_h / 2 - fs_sm / 2, fs_sm, _TEXT_DIM)

        bbx = col3_x + pbox_w + 2 * scale + WSP_SRT_BROWSE_X_OFFSET * scale
        bby = y3 + WSP_SRT_BROWSE_Y_OFFSET * scale
        _r(bbx, bby, bbw, fitem_h, (0.03, 0.10, 0.05, 1.0))
        _box(bbx, bby, bbw, fitem_h, _ACCENT)
        _bl = "\u2026"
        tw_b = _text_width(_bl, fs_sm)
        _draw_text(_bl, bbx + bbw / 2 - tw_b / 2,
                   bby + fitem_h / 2 - fs_sm / 2, fs_sm, _ACCENT)
        y3 -= fitem_h

    # Last run
    y3 -= fs_sm + 8 * scale
    if not _has_skin:
        _draw_text("LAST RUN", col3_x, y3, fs_sm, _TEXT_LABEL)
    y3 -= fs_sm + 2 * scale
    if status == _DONE:
        _draw_text("\u2713 Strips placed on timeline", col3_x, y3, fs_sm, _GREEN)
    elif status == _ERROR:
        _draw_text("\u2717 Failed \u2014 check console", col3_x, y3, fs_sm, _RED)
    else:
        _draw_text("No output yet", col3_x, y3, fs_sm, _TEXT_LABEL)