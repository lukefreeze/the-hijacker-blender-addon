"""
Text helpers for the packApunch skins: an optional themed font per rack, plus a soft glow.

Fonts live in  ui/assets/skins/packapunch/fonts/  and each rack picks its own through the
"font" entry of its theme (PACKAPUNCH_RACK_THEME in rack_base.py):

    "font": None / ""        -> normal Blender font
    "font": "auto"           -> the file named after the rack type in the fonts folder,
                                e.g.  noise_gate.ttf  /  reverb.otf  /  comp_multi.ttf
    "font": "SomeName.ttf"   -> that exact file in the fonts folder

A missing file simply falls back to the normal font, so nothing breaks.

PROFILES describes how to lay out a specific font file (keyed by the file name without
extension): glyph spacing, which characters it has, upright text, and so on. Fonts with a profile are
drawn glyph by glyph (tight spacing, glyphs the font lacks fall back to the normal font);
fonts without one are drawn as a normal string.
"""
import os
import blf

try:
    from core.constants import ASSETS_DIR
except Exception:                      # offline tests
    ASSETS_DIR = ""

PP_FONT_DIR = os.path.join(ASSETS_DIR, "packapunch", "fonts")

# noise_gate.ttf (Predator glyph font): every glyph has a 1.0 em advance but only ~0.63 em of ink, the
# ink sits 0.16 em in from the cell edge, digits/capitals are ~0.8 em tall. Only 0-9, A-Z, a-z and space exist.
PROFILES = {
    "noise_gate": {
        "adv":   0.74,            # advance per glyph in em (the font's own 1.0 leaves big gaps)
        "ink":   0.63,            # width of a glyph's ink in em
        "lsb":   0.16,            # ink starts this far into the glyph cell
        "upper": True,            # lowercase uses a different alphabet layout: always draw capitals
        "chars": "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz ",
        "ctr_digit": 0.373,       # height of the ink's centre above the baseline, em
        "ctr_upper": 0.254,
        "value_scale": 2.3,       # knob value text is drawn this much bigger than the normal-font size
    },
}

_ids = {}                              # resolved file path -> blf font id (0 = failed / default)


def font_path(spec, etype=""):
    """Path of the font file a theme asks for, or '' when there is none."""
    try:
        if not spec:
            return ""
        if spec == "auto":
            base = (etype or "").lower()
            for ext in (".ttf", ".otf", ".TTF", ".OTF"):
                p = os.path.join(PP_FONT_DIR, base + ext)
                if base and os.path.isfile(p):
                    return p
            return ""
        p = os.path.join(PP_FONT_DIR, spec)
        return p if os.path.isfile(p) else ""
    except Exception:
        return ""


def font_id(spec, etype=""):
    """blf font id for a theme's font spec (loaded once per file), or 0 (default font)."""
    p = font_path(spec, etype)
    if not p:
        return 0
    fid = _ids.get(p)
    if fid is None:
        fid = 0
        try:
            r = blf.load(p)
            if r is not None and r >= 0:
                fid = r
        except Exception:
            fid = 0
        _ids[p] = fid
    return fid


def font_profile(spec, etype=""):
    """Layout profile for a theme's font file, or None (draw as a normal string)."""
    p = font_path(spec, etype)
    if not p:
        return None
    return PROFILES.get(os.path.splitext(os.path.basename(p))[0].lower())


def reload_fonts():
    """Forget cached fonts (after dropping new files in the folder)."""
    _ids.clear()


def _prepare(text, prof):
    return text.upper() if prof.get("upper") else text


def text_width(text, size, fid=0, prof=None):
    size = max(1, int(size))
    if not prof:
        blf.size(fid, size)
        return blf.dimensions(fid, text)[0]
    text = _prepare(text, prof)
    chars = prof["chars"]
    w = 0.0
    for i, ch in enumerate(text):
        if ch in chars:
            w += prof["adv"] * size if i < len(text) - 1 else prof["ink"] * size
        else:
            blf.size(0, size)
            w += blf.dimensions(0, ch)[0] + 1
    return w


def _ctr(text, prof):
    """Where the ink centre of this text sits above the baseline (em)."""
    vals = []
    for ch in text:
        if ch.isdigit():
            vals.append(prof["ctr_digit"])
        elif ch.isalpha():
            vals.append(prof["ctr_upper"])
    return sum(vals) / len(vals) if vals else prof["ctr_upper"]


# crisp halo: copies of the text one pixel around it
_HALO = ((-1, 0), (1, 0), (0, -1), (0, 1), (-0.7, -0.7), (0.7, -0.7), (-0.7, 0.7), (0.7, 0.7))


def _blit(fid, ch, x, y, color, glow, glow_col, gscale):
    r, g, b, a = color
    if glow > 0.0:
        gr, gg, gb = (glow_col or (r, g, b))[:3]
        blf.color(fid, gr, gg, gb, min(1.0, a * glow * 0.20))
        d = 1.15 * gscale
        for ox, oy in _HALO:
            blf.position(fid, x + ox * d, y + oy * d, 0)
            blf.draw(fid, ch)
    blf.color(fid, r, g, b, a)
    blf.position(fid, x, y, 0)
    blf.draw(fid, ch)


def draw_text(text, x, y, size, color, glow=0.0, glow_col=None, fid=0, prof=None):
    """Draw text with font id fid. glow > 0 adds a tight halo in glow_col (default: the
    text colour), 1.0 is strong. With a layout profile the text is drawn glyph by glyph
    and y is interpreted like the normal font's: the vertical centre of the text is y + size/2 - 1."""
    size = max(1, int(size))
    if not prof:
        blf.size(fid, size)
        _blit(fid, text, x, y, color, glow, glow_col, max(1.0, size / 14.0) ** 0.5)
        return
    text = _prepare(text, prof)
    cy = y + size / 2.0 - 1.0
    base = cy - _ctr(text, prof) * size
    chars = prof["chars"]
    gscale = max(1.0, size / 16.0) ** 0.5
    cx = x
    for ch in text:
        if ch in chars:
            if ch != " ":
                blf.size(fid, size)
                _blit(fid, ch, cx - prof["lsb"] * size, base, color, glow, glow_col, gscale)
                # thin strokes: a second, slightly shifted pass to give them some weight
                blf.color(fid, *color)
                blf.position(fid, cx - prof["lsb"] * size + 0.6, base, 0)
                blf.draw(fid, ch)
            cx += prof["adv"] * size
        else:                                    # glyph the font doesn't have: normal font, same height
            fs0 = max(1, int(size * 0.9))
            blf.size(0, fs0)
            _blit(0, ch, cx, cy - fs0 * 0.36, color, glow, glow_col, gscale)
            cx += blf.dimensions(0, ch)[0] + 1
