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
