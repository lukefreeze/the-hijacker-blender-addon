"""
packApunch lava for the mixer strips (foreground strip art).

 * strip_bottom_bg.png and mixer_desk_bg.png ship with the lava cracks DIMMED (dead embers) and with the old static sparks painted out.
 * strip_lava_atlas.png is a pre-rendered LOOP (FRAMES frames) of the hot lava: the yellow / orange / red colours
   flow and swirl along the cracks.  It is cut into small square tiles (only where there is lava), one column of
   the atlas per frame, and strip_lava_cells.json says where every tile sits in the art.  Each draw, the current
   frame of every tile (two layers: 'strip' = EQ/pan/fader art, 'desk' = sends/upper art) goes over the dim art in ONE batched cross-fade (ALPHA_PREMULT, colour-space safe).
 * Sparks are simulated live (stateless, a pure function of time): they leave the cracks, are blown up and to the
   left by a gusting wind with a bit of turbulence, tumble as hard-edged glowing ash chips, cool from burning orange to dull red and die.

Tuning (module constants):
  LOOP_S        seconds for the lava loop to repeat (smaller = faster flow)
  BREATH        how much the whole lava swells/dims on top of the flow (0 = none)
  SPARKS        number of sparks alive per strip block
  WIND_X/WIND_Y spark drift in art px / second (y = upwards)
  FADE_PX       how far from the seam (where the strip art meets the desk art) the lava fades to dead cracks
  CHIP_SIZE     size of the ember chips; HALO = faint haze strength behind each one
"""
import os, json, math, time

LOOP_S = 4.0
BREATH = 0.10
SPARKS = 52
WIND_X = -44.0     # negative = embers drift to the LEFT (positive = right)
WIND_Y = 34.0
SPARK_SIZE = 1.9
FADE_PX = 38.0      # the lava fades out over this many px (scale 1) toward the seam between the desk art and the strip art
FADE_STEPS = 14
CHIP_SIZE = 1.8     # ember length relative to spark size
HALO = 0.10         # faint tight haze around each ember (0 = none)

_shader = None


def _hash(i, salt=0):
    x = math.sin(i * 127.1 + salt * 311.7 + 17.3) * 43758.5453
    return x - math.floor(x)


_LAYERS = {
    "strip": ("strip_lava_cells.json", "strip_lava_atlas"),   # foreground strip art (EQ / pan / fader)
    "desk":  ("desk_lava_cells.json",  "desk_lava_atlas"),    # desk art behind the sends section (upper part)
}
_LOADED = {}


def _load(name="strip"):
    """Load the cell data of one lava layer (cached).  Returns the dict or False."""
    L = _LOADED.get(name)
    if L is not None:
        return L["d"] if L else False
    _LOADED[name] = False
    try:
        from ui.mixer import texture_cache as tc
        with open(os.path.join(tc._skin_dir, _LAYERS[name][0]), "r", encoding="utf-8") as f:
            d = json.load(f)
        AW, AH = d["atlas"]; TS = d["tile"]; PD = d["pad"]; C = TS + 2 * PD
        uv = []   # uv rect of tile t at frame f (interior only), bottom-up v
        for fr in range(d["frames"]):
            uv.append([((fr * C + PD) / AW, 1.0 - (t * C + PD + TS) / AH,
                        (fr * C + PD + TS) / AW, 1.0 - (t * C + PD) / AH) for t in range(len(d["tiles"]))])
        _LOADED[name] = {"d": d, "uv": uv, "uvc": {}, "pos": {}}
        return d
    except Exception as e:
        print(f"[LAVA] {name} data not available: {e}")
    return False


# ── sparks ───────────────────────────────────────────────────────────────────────────────────────────────
def sparks(t, group, seeds, W, H):
    """[(x_art, y_art(top-down), size_art_px, (r,g,b), alpha, vx, vy, angle, aspect)] for time t — stateless."""
    out = []
    if not seeds:
        return out
    gust = 1.0 + 0.55 * math.sin(t * 0.45 + group * 1.7) + 0.25 * math.sin(t * 1.3 + 2.0)
    for s in range(SPARKS):
        life = 2.4 + 4.0 * _hash(s, 1)
        ph = _hash(s, 2) * life
        cyc = math.floor((t + ph) / life)
        age = (t + ph) - cyc * life
        u = age / life
        sd = s * 7 + cyc * 131 + group * 977
        sx0, sy0 = seeds[int(_hash(sd, 3) * len(seeds)) % len(seeds)]
        vx = WIND_X * (0.55 + 0.9 * _hash(sd, 4))
        vy = WIND_Y * (0.40 + 1.2 * _hash(sd, 5))
        g = 1.0 + (gust - 1.0) * 0.8
        tw = 6.0 + 10.0 * _hash(sd, 6); w1 = 1.5 + 3.0 * _hash(sd, 7); w2 = 2.0 + 3.0 * _hash(sd, 8)
        x = sx0 + vx * g * age + tw * math.sin(age * w1 + 6.28 * _hash(sd, 9))
        y = sy0 - vy * age - 0.5 * 6.0 * age * age * 0 + 0.6 * tw * math.sin(age * w2 + 6.28 * _hash(sd, 10))
        if x < -4 or x > W + 4 or y < -4 or y > H + 4:
            continue
        a = min(1.0, age / 0.10) * (1.0 - u ** 5.0) * (0.85 + 0.15 * math.sin(age * (12.0 + 10.0 * _hash(sd, 11)) + 6.28 * _hash(sd, 12)))
        if a <= 0.02:
            continue
        # burning orange -> deep red-orange as it cools (never white-yellow: these are glowing ash, not light)
        if u < 0.35:
            k = u / 0.35; col = (1.0 - 0.08 * k, 0.52 - 0.18 * k, 0.10 - 0.05 * k)
        else:
            k = (u - 0.35) / 0.65; col = (0.92 - 0.38 * k, 0.34 - 0.22 * k, 0.05 - 0.03 * k)
        # still burning: each ember pulses between dull and bright
        col = tuple(c * (0.72 + 0.28 * math.sin(age * (7.0 + 9.0 * _hash(sd, 14)) + 6.28 * _hash(sd, 15))) for c in col)
        size = (1.9 + 2.6 * _hash(sd, 13)) * SPARK_SIZE * (1.0 - 0.35 * u)
        # direction of travel (art px / s) for the streak behind the head
        dvx = vx * g + tw * w1 * math.cos(age * w1 + 6.28 * _hash(sd, 9))
        dvy = -vy + 0.6 * tw * w2 * math.cos(age * w2 + 6.28 * _hash(sd, 10))
        ang = 6.28 * _hash(sd, 16) + age * (2.0 + 5.0 * _hash(sd, 17)) * (1.0 if _hash(sd, 18) > 0.5 else -1.0)
        aspect = 0.55 + 0.35 * _hash(sd, 19)
        flip = 0.55 + 0.45 * abs(math.cos(age * (1.5 + 3.0 * _hash(sd, 20)) + 6.28 * _hash(sd, 21)))   # tumbling
        out.append((x, y, size, col, min(1.0, a), dvx, dvy, ang, aspect * flip))
    return out


def lava_level(t):
    """Gentle global breathing 1-BREATH..1."""
    return 1.0 - BREATH * (0.5 + 0.5 * math.sin(t * 1.15)) * (0.6 + 0.4 * math.sin(t * 0.37 + 1.0))


def _batch_uv(L, f):
    v = L["uvc"].get(f)
    if v is None:
        v = []
        for (u0, v0, u1, v1) in L["uv"][f]:
            v += [(u0, v0), (u1, v0), (u1, v1), (u0, v0), (u1, v1), (u0, v1)]
        L["uvc"][f] = v
    return v


def _active():
    from ui.mixer.texture_cache import get_active_skin
    return get_active_skin() == "packapunch"


def _get_shader():
    global _shader
    if _shader is None:
        import gpu
        _shader = gpu.shader.from_builtin("IMAGE_COLOR")
    return _shader


def _draw_layer(name, x, y, w, h, t):
    """Cross-fade the current lava frame of one layer over its (dim) art drawn at (x, y, w, h) (y = bottom edge)."""
    import gpu
    from gpu_extras.batch import batch_for_shader
    from ui.mixer.texture_cache import get_texture
    d = _load(name)
    if not d:
        return
    L = _LOADED[name]
    tex = get_texture(_LAYERS[name][1])
    if tex is None:
        return
    W, H = d["art"]
    sx, sy = w / W, h / H
    shader = _get_shader()
    f = int(t / LOOP_S * d["frames"]) % d["frames"]
    key = (round(x, 1), round(y, 1), round(w, 1), round(h, 1))
    pos = L["pos"].get(key)
    if pos is None:
        if len(L["pos"]) > 16:
            L["pos"].clear()
        TS = d["tile"]; pos = []
        for (tx, ty) in d["tiles"]:
            x0 = x + tx * sx; x1 = x + (tx + TS) * sx
            y1 = y + (H - ty) * sy; y0 = y + (H - ty - TS) * sy
            pos += [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]
        L["pos"][key] = pos
    batch = batch_for_shader(shader, "TRIS", {"pos": pos, "texCoord": _batch_uv(L, f)})
    k = lava_level(t)
    gpu.state.blend_set("ALPHA_PREMULT")
    try:
        shader.bind()
        shader.uniform_sampler("image", tex)
        shader.uniform_float("color", (k, k, k, k))
        batch.draw(shader)
    finally:
        gpu.state.blend_set("ALPHA")


def _fade_out(tex_name, rect, seam_y, up, scale):
    """Fade the lava out toward a seam: re-draw the dim art over the lava in thin bands whose opacity ramps from
    1 (at the seam) to 0 (FADE_PX away).  rect = (x, y_bottom, w, h) of that art on screen; up=True fades the band
    above the seam, False the band below it."""
    from ui.mixer.texture_cache import get_texture, blit_texture_tinted
    tex = get_texture(tex_name)
    if tex is None or FADE_PX <= 0:
        return
    x, y, w, h = rect
    bh = FADE_PX * scale / FADE_STEPS
    for i in range(FADE_STEPS):
        a = 1.0 - i / FADE_STEPS
        if up:
            y0 = seam_y + i * bh; y1 = y0 + bh
        else:
            y1 = seam_y - i * bh; y0 = y1 - bh
        y0c = max(y0, y); y1c = min(y1, y + h)
        if y1c <= y0c:
            continue
        blit_texture_tinted(tex, x, y0c, w, y1c - y0c, color=(a, a, a, a), blend="ALPHA_PREMULT",
                            uv=(0.0, (y0c - y) / h, 1.0, (y1c - y) / h), key=tex_name)


def draw_strip_lava(x, y, w, h, group=0, scale=None, now=None):
    """Lava over the strip-bottom art (EQ / pan / fader) drawn at (x, y, w, h); y = bottom edge, screen px.
    The lava fades out toward the top edge of this art (the seam with the desk art above it)."""
    try:
        if _active():
            _draw_layer("strip", x, y, w, h, time.monotonic() if now is None else now)
            d = _load("strip")
            sc = scale if scale else (w / d["art"][0] if d else 1.0)
            _fade_out("strip_bottom_bg", (x, y, w, h), y + h, False, sc)
    except Exception as e:
        print(f"[LAVA] draw error: {e}")


def draw_desk_lava(x, y, w, h, seam_y=None, scale=1.0, now=None):
    """Lava over the desk art (sends section / upper part) drawn at (x, y, w, h); y = bottom edge, screen px.
    Call right after the desk art is drawn and BEFORE the strip skin slices that cover parts of it.
    seam_y = screen y of the top edge of the strip-bottom art (where it starts covering the desk art): the lava
    fades out toward it so no cut-off glowing crack is left against the seam."""
    try:
        if _active():
            _draw_layer("desk", x, y, w, h, time.monotonic() if now is None else now)
            if seam_y is not None:
                _fade_out("mixer_desk_bg", (x, y, w, h), seam_y, True, scale)
    except Exception as e:
        print(f"[LAVA] draw error: {e}")


_SEED_CACHE = {}


def _screen_seeds(name, rect, gx, gy_top, s_, y_above=None, y_below=None):
    """Spawn points of one layer in spark space (units of s_ screen px, origin = group's top-left, v downwards)."""
    d = _load(name)
    if not d:
        return []
    x, y, w, h = rect
    W, H = d["art"]
    sx, sy = w / W, h / H
    out = []
    for (xa, ya) in d["seeds"]:
        xs = x + xa * sx; ys = y + (H - ya) * sy
        if y_above is not None and ys < y_above:      # hidden behind other art / faded out: no glowing crack to spawn from
            continue
        if y_below is not None and ys > y_below:
            continue
        out.append(((xs - gx) / s_, (gy_top - ys) / s_))
    return out


def draw_embers(gx, gy_top, gw, bot_rect, desk_rect, group=0, now=None):
    """Drifting ash embers for one group of strips, over the WHOLE desk height.
    gx, gy_top, gw : left edge, top edge (screen y, up) and width of the group's desk
    bot_rect       : (x, y_bottom, w, h) the strip-bottom art
    desk_rect      : (x, y_bottom, w, h) the desk art (sends / upper part)"""
    try:
        if not _active():
            return
        import gpu
        from gpu_extras.batch import batch_for_shader
        from ui.mixer.texture_cache import get_texture, blit_texture_tinted
        chip = get_texture("strip_ember_chip")
        if chip is None:
            return
        t = time.monotonic() if now is None else now
        d = _load("strip")
        if not d:
            return
        s_ = bot_rect[2] / d["art"][0]
        if s_ <= 0:
            return
        key = tuple(round(v, 1) for v in (gx, gy_top, gw) + tuple(bot_rect) + tuple(desk_rect))
        seeds = _SEED_CACHE.get(key)
        if seeds is None:
            if len(_SEED_CACHE) > 16:
                _SEED_CACHE.clear()
            bot_top = bot_rect[1] + bot_rect[3]          # screen y of the top edge of the strip-bottom art
            fz = FADE_PX * s_
            seeds = _screen_seeds("strip", bot_rect, gx, gy_top, s_, y_below=bot_top - fz) + \
                    _screen_seeds("desk", desk_rect, gx, gy_top, s_, y_above=bot_top + fz)
            _SEED_CACHE[key] = seeds
        Wd = gw / s_; Hd = (gy_top - bot_rect[1]) / s_
        shader = _get_shader()
        dot = get_texture("strip_ember_dot")
        for (px, py, sz, col, a, dvx, dvy, ang, asp) in sparks(t, group, seeds, Wd, Hd):
            cxs = gx + px * s_; cys = gy_top - py * s_
            if dot is not None and HALO > 0.0:      # very tight, faint heat haze behind the chip (not a glow ball)
                h_ = sz * s_ * 2.2; aa = a * HALO
                blit_texture_tinted(dot, cxs - h_ / 2, cys - h_ / 2, h_, h_,
                                    color=(col[0] * aa, col[1] * aa, col[2] * aa, aa),
                                    blend="ALPHA_PREMULT", key="strip_ember_dot")
            hw = sz * s_ * CHIP_SIZE * 0.5; hh = hw * asp / 0.72
            ca, sa = math.cos(ang), math.sin(ang)
            q = [(cxs + u_ * ca - v_ * sa, cys + u_ * sa + v_ * ca) for (u_, v_) in
                 ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
            b_ = batch_for_shader(shader, "TRIS", {"pos": [q[0], q[1], q[2], q[0], q[2], q[3]],
                                                    "texCoord": [(0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1)]})
            gpu.state.blend_set("ALPHA_PREMULT")
            try:
                shader.bind()
                shader.uniform_sampler("image", chip)
                shader.uniform_float("color", (col[0] * a, col[1] * a, col[2] * a, a))
                b_.draw(shader)
            finally:
                gpu.state.blend_set("ALPHA")
    except Exception as e:
        print(f"[LAVA] ember error: {e}")
