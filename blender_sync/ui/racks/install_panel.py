"""
ui/racks/install_panel.py
=========================
The "installing..." readout the AI racks show inside their setup panel while
core/ai_pydeps.py installs the Python packages.

It replaces the terminal-command instructions for the duration of the
install (so the two never overlap) with: what is being installed, which
package pip is on, elapsed time, and a progress bar with a percentage.
"""


def draw_install_view(draw_rect, draw_text, text_width, px, py, pw, ph, scale,
                      name, lines, pct, bg, fg, dim, btn_h):
    """Draw inside the setup panel (px, py, pw, ph; y up), above the button.

    draw_rect(x, y, w, h, color) / draw_text(text, x, y, size, color) /
    text_width(text, size) are the calling rack's own primitives.
    """
    cx = px + 6 * scale
    cw = pw - 12 * scale
    cy = py + btn_h + 14 * scale
    ch = ph - (btn_h + 14 * scale) - 4 * scale
    if cw <= 0 or ch <= 0:
        return
    draw_rect(cx, cy, cw, ch, bg)                      # covers the instructions underneath

    fs_h = max(1, int(9 * scale))
    fs_s = max(1, int(7 * scale))
    tx = px + 12 * scale
    ty = cy + ch - fs_h - 6 * scale
    draw_text(f"INSTALLING {name}", tx, ty, fs_h, fg)
    ty -= fs_s + 9 * scale
    for ln in lines:
        draw_text(ln, tx, ty, fs_s, fg)
        ty -= fs_s + 4 * scale
    draw_text("One-time setup. You can keep working while it runs.", tx, ty - 2 * scale, fs_s, dim)

    # progress bar + percentage along the bottom of the readout
    pct = max(0, min(100, int(pct)))
    label = f"{pct}%"
    lab_w = text_width("100%", fs_h) + 10 * scale
    bar_h = max(7 * scale, 7)
    bar_x = tx
    bar_w = (px + pw - 12 * scale) - bar_x - lab_w
    bar_y = cy + 8 * scale
    draw_rect(bar_x - 1, bar_y - 1, bar_w + 2, bar_h + 2, dim)            # thin frame
    draw_rect(bar_x, bar_y, bar_w, bar_h, (0.01, 0.01, 0.01, 1.0))
    draw_rect(bar_x, bar_y, bar_w * pct / 100.0, bar_h, fg)
    draw_text(label, bar_x + bar_w + 8 * scale, bar_y + bar_h / 2 - fs_h / 2, fs_h, fg)


# ---------------------------------------------------------------------------
# Click-to-copy text
#
# The racks are drawn with the GPU, so their text cannot be selected like
# normal UI text. Instead the setup panels register "copy zones": clicking a
# terminal command or an error message copies the full text to the clipboard
# (Racks.py handles the click, see zone 'ai_copy_text').
# ---------------------------------------------------------------------------
import time as _time

_zones = {}          # group -> [(x, y, w, h, text), ...]
_copied_at = {}      # text -> time.time() of the last copy
_COPIED_FLASH_S = 1.6


def reset_copy_zones(group):
    _zones[group] = []


def add_copy_zone(group, x, y, w, h, text):
    _zones.setdefault(group, []).append((x, y, w, h, text))


def hit_copy_zone(group, mx, my):
    """Text to copy if (mx, my) is inside one of the group's zones, else None."""
    for (x, y, w, h, text) in _zones.get(group, ()):
        if x <= mx <= x + w and y <= my <= y + h:
            return text
    return None


def mark_copied(text):
    _copied_at[text] = _time.time()
    try:
        import bpy

        def _later():
            try:
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type in ('NODE_EDITOR', 'SEQUENCE_EDITOR'):
                            area.tag_redraw()
            except Exception:
                pass
            return None
        bpy.app.timers.register(_later, first_interval=_COPIED_FLASH_S + 0.1)
    except Exception:
        pass


def _just_copied(text):
    return _time.time() - _copied_at.get(text, -1e9) < _COPIED_FLASH_S


def draw_copy_hint(draw_text, text_width, group, x, y, w, h, scale, text, dim, fg,
                   avoid_w=0.0):
    """Make the box (x, y, w, h) click-to-copy for `text`, with a small COPY
    label at its right edge (skipped when the command text would run into it)."""
    add_copy_zone(group, x, y, w, h, text)
    fs = max(1, int(6 * scale))
    done = _just_copied(text)
    label = "COPIED" if done else "COPY"
    lw = text_width(label, fs)
    if avoid_w + lw + 14 * scale < w or done:
        draw_text(label, x + w - lw - 6 * scale, y + h / 2 - fs / 2, fs, fg if done else dim)


def _wrap(text_width, text, fs, max_w, max_lines):
    words = text.replace("\n", " ").split()
    lines, cur = [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if text_width(trial, fs) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
        if len(lines) >= max_lines:
            break
    else:
        if cur:
            lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    shown = " ".join(lines)
    clipped = len(shown) < len(" ".join(words))
    if clipped and lines:
        last = lines[-1]
        while last and text_width(last + "...", fs) > max_w:
            last = last[:-1]
        lines[-1] = last + "..."
    return lines


def draw_error_band(draw_rect, draw_text, text_width, group, bg, fg, dim, x, w,
                    y_bottom, y_top, scale, message, full_text=None):
    """Error readout for a setup panel: a wrapped message (as much of it as
    fits) under a "click to copy" heading. Clicking it copies `full_text`
    (the whole pip output when available) to the clipboard."""
    full_text = full_text or message
    if y_top - y_bottom < 8 * scale:
        return
    draw_rect(x - 4 * scale, y_bottom - 2 * scale, w + 8 * scale,
              (y_top - y_bottom) + 4 * scale, bg)
    fs = max(1, int(6 * scale))
    lh = fs + 3 * scale
    n_fit = max(1, int((y_top - y_bottom) / lh))
    red = (1.0, 0.45, 0.4, 1.0)
    done = _just_copied(full_text)
    head = "COPIED TO CLIPBOARD" if done else "ERROR  -  CLICK HERE TO COPY THE FULL TEXT"
    body = _wrap(text_width, message, fs, w - 6 * scale, max(1, min(4, n_fit - 1))) if n_fit > 1 else []
    ty = y_top - fs
    draw_text(head, x, ty, fs, fg if done else dim)
    for ln in body:
        ty -= lh
        draw_text(ln, x, ty, fs, red)
    add_copy_zone(group, x - 4 * scale, y_bottom - 2 * scale, w + 8 * scale,
                  (y_top - y_bottom) + 4 * scale, full_text)
