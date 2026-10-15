# =============================================================================
# ui/mixer/texture_cache.py
# PNG skin texture loader — the bridge between GPU primitives and future PNG skins.
#
# HOW THE PNG REPLACEMENT WILL WORK
# ----------------------------------
# Every draw call in draw_utils.py routes through draw_element(key, x, y, w, h, ...).
# Right now "key" is ignored and it falls back to GPU primitive drawing.
#
# When you're ready to skin an element:
#   1. Drop a PNG into ui/assets/skins/default/  (e.g. knob.png)
#   2. The key "knob" will match SKIN_MAP below
#   3. load_skin() calls gpu.texture.from_image() once and caches the result
#   4. draw_element() blits the texture instead of drawing primitives
#   5. The call site in channel_strip.py / rack_*.py does NOT change at all
#
# The GPU blit path uses gpu.shader.from_builtin("IMAGE") with a TRI_STRIP
# quad mapped to the draw rect — same coordinate system as everything else.
#
# SKIN_MAP: element key -> filename in the active skin folder
# =============================================================================

import os
import math
import gpu
from gpu_extras.batch import batch_for_shader

# ---------------------------------------------------------------------------
# Skin map — add an entry here when you have a PNG ready
# ---------------------------------------------------------------------------
SKIN_MAP = {
    # key            filename in skins/default/ (or skins/custom/)
    # LED meter tiles
    "meter_led_green":  "GreenLED.png",
    "meter_led_yellow": "YellowLED.png",
    "meter_led_red":    "RedLED.png",
    "meter_led_off":    "BlackLED.png",

    "fader_handle":  "fader_handle.png",  # generic fallback
    # Per-channel fader handles — fader_handle_1.png .. fader_handle_9.png
    # Drop any of these in skins/default/ to override that channel's handle.
    # Falls back to fader_handle.png if the per-channel file isn't present.
    "fader_handle_1":  "fader_handle_1.png",
    "fader_handle_2":  "fader_handle_2.png",
    "fader_handle_3":  "fader_handle_3.png",
    "fader_handle_4":  "fader_handle_4.png",
    "fader_handle_5":  "fader_handle_5.png",
    "fader_handle_6":  "fader_handle_6.png",
    "fader_handle_7":  "fader_handle_7.png",
    "fader_handle_8":  "fader_handle_8.png",
    "fader_handle_9":  "fader_handle_9.png",

    "fader_rail":    "fader_rail.png",
    "knob":          "knob.png",
    "knob_gain":     "knob_gain.png",
    "knob_eq":       "knob_eq.png",
    "knob_pan":      "knob_pan.png",
    # Send slot buttons
    "send_btn_off":  "SendOff.png",
    "send_btn_on":   "SendOn.png",

    "btn_mute_off":  "btn_mute_off.png",
    "btn_mute_on":   "btn_mute_on.png",
    "btn_solo_off":  "btn_solo_off.png",
    "btn_solo_on":   "btn_solo_on.png",
    # Mixer strip — three sections + fader section
    "strip_top_bg":        "strip_top_bg.png",       # label + mute/solo + gain (120x170px)
    "strip_send_slot_bg":  "strip_send_slot_bg.png", # one send row tile (120x23px, tiled)
    "strip_send_section_bg": "strip_send_section_bg.png", # full sends section background
    "strip_bottom_bg":     "strip_bottom_bg.png",    # EQ + pan knob section
    "strip_lava_atlas":    "strip_lava_atlas.png",   # packApunch: animated lava glow cells (see ui/mixer/lava_fx.py)
    "strip_ember_dot":     "strip_ember_dot.png",     # packApunch: soft spark sprite for the drifting lava sparks
    "desk_lava_atlas":     "desk_lava_atlas.png",     # packApunch: animated lava frames for the desk art (sends / upper part)
    "strip_knob_faces":    "strip_knob_faces.png",    # packApunch: the 45 mixer-strip knob tops (gain/high/mid/low/pan x 9), drawn rotated
    "strip_ember_chip":    "strip_ember_chip.png",    # packApunch: hard-edged glowing ash chip for the drifting lava embers
    "strip_fader_bg":      "strip_fader_bg.png",     # fader + numbox + gap section
    "strip_bg":            "strip_bg.png",            # legacy full-strip (unused if above present)
    "mixer_desk_bg":       "mixer_desk_bg.png",
    "background":          "Background.png",        # full table background, scrolls with UI
    # Rack body backgrounds
    "rack_comp_multi_bg":  "rack_comp_multi_bg.png",
    "rack_comp_single_bg": "rack_comp_single_bg.png",
    "rack_comp_single_glass": "rack_comp_single_glass.png",
    "rack_comp_multi_glass":  "rack_comp_multi_glass.png",
    # PackAPunch multiband screen glow: grid lines only (black elsewhere),
    # drawn additively and breathed; rack_pulse_glow is the soft round glow used
    # for the ball of light that runs along the chassis circuit lines.
    "rack_comp_multi_lines":  "rack_comp_multi_lines.png",
    "rack_comp_multi_glow":   "rack_comp_multi_glow.png",
    "rack_comp_multi_off_btn": "rack_comp_multi_off_btn.png",
    "rack_eq_off_btn":         "rack_eq_off_btn.png",
    "rack_reverb_off_btn":     "rack_reverb_off_btn.png",
    "rack_noisegate_off_btn":  "rack_noisegate_off_btn.png",
    # packApunch delay: dimmed ON glass, and the 9 lit channel keys (laid over the all-dim keypad in the art)
    "rack_delay_off_btn":      "rack_delay_off_btn.png",
    # packApunch delay: the five knob faces (Time, Feedback, Mix, Spread, Filter), drawn rotated
    "rack_booster_knob":        "rack_booster_knob.png",
    "rack_delay_knob_1":       "rack_delay_knob_1.png",
    "rack_delay_knob_2":       "rack_delay_knob_2.png",
    "rack_delay_knob_3":       "rack_delay_knob_3.png",
    "rack_delay_knob_4":       "rack_delay_knob_4.png",
    "rack_delay_knob_5":       "rack_delay_knob_5.png",
    "rack_delay_key1_lit":     "rack_delay_key1_lit.png",
    "rack_delay_key2_lit":     "rack_delay_key2_lit.png",
    "rack_delay_key3_lit":     "rack_delay_key3_lit.png",
    "rack_delay_key4_lit":     "rack_delay_key4_lit.png",
    "rack_delay_key5_lit":     "rack_delay_key5_lit.png",
    "rack_delay_key6_lit":     "rack_delay_key6_lit.png",
    "rack_delay_key7_lit":     "rack_delay_key7_lit.png",
    "rack_delay_key8_lit":     "rack_delay_key8_lit.png",
    "rack_delay_key9_lit":     "rack_delay_key9_lit.png",
    # packApunch parametric EQ: the 19 knob faces (gain g0-g6, freq f0-f6, Q q1-q5), drawn rotated
    "rack_eq_knob_g0": "rack_eq_knob_g0.png",
    "rack_eq_knob_g1": "rack_eq_knob_g1.png",
    "rack_eq_knob_g2": "rack_eq_knob_g2.png",
    "rack_eq_knob_g3": "rack_eq_knob_g3.png",
    "rack_eq_knob_g4": "rack_eq_knob_g4.png",
    "rack_eq_knob_g5": "rack_eq_knob_g5.png",
    "rack_eq_knob_g6": "rack_eq_knob_g6.png",
    "rack_eq_knob_f0": "rack_eq_knob_f0.png",
    "rack_eq_knob_f1": "rack_eq_knob_f1.png",
    "rack_eq_knob_f2": "rack_eq_knob_f2.png",
    "rack_eq_knob_f3": "rack_eq_knob_f3.png",
    "rack_eq_knob_f4": "rack_eq_knob_f4.png",
    "rack_eq_knob_f5": "rack_eq_knob_f5.png",
    "rack_eq_knob_f6": "rack_eq_knob_f6.png",
    "rack_eq_knob_q1": "rack_eq_knob_q1.png",
    "rack_eq_knob_q2": "rack_eq_knob_q2.png",
    "rack_eq_knob_q3": "rack_eq_knob_q3.png",
    "rack_eq_knob_q4": "rack_eq_knob_q4.png",
    "rack_eq_knob_q5": "rack_eq_knob_q5.png",
    "rack_noisegate_knob":     "rack_noisegate_knob.png",      # packApunch noise gate knob 1 (skull), drawn rotated
    "rack_noisegate_knob_2":   "rack_noisegate_knob_2.png",
    "rack_noisegate_knob_3":   "rack_noisegate_knob_3.png",
    "rack_noisegate_knob_4":   "rack_noisegate_knob_4.png",
    "rack_noisegate_knob_5":   "rack_noisegate_knob_5.png",
    "rack_reverb_trk_ring":    "rack_reverb_trk_ring.png",     # packApunch reverb motion tracker: rotating ring
    "rack_reverb_trk_mask":    "rack_reverb_trk_mask.png",     # ... matte that hides the part of the ring behind the band
    "rack_reverb_trk_motion":  "rack_reverb_trk_motion.png",   # ... band drawn on top
    "rack_comp_single_bg_off": "rack_comp_single_bg_off.png",
    "rack_pulse_glow":        "rack_pulse_glow.png",
    "rack_eq_bg":          "rack_eq_bg.png",
    "rack_reverb_bg":      "rack_reverb_bg.png",
    "rack_noisegate_bg":   "rack_noisegate_bg.png",
    "rack_delay_bg":       "rack_delay_bg.png",
    "rack_booster_bg":         "rack_booster_bg.png",
    "rack_booster_off_btn":    "rack_booster_off_btn.png",     # packApunch: the ON glass with the light turned down
    "rack_booster_limit_bar":  "rack_booster_limit_bar.png",   # packApunch: lit green bar of the limiter display (limiter on)
    "rack_booster_limit_x":    "rack_booster_limit_x.png",     # packApunch: red cross of the limiter display (limiter off)
    "rack_booster_bullets_lit": "rack_booster_bullets_lit.png", # packApunch: the lit bullet casings round the knob (atlas, premultiplied)
    "rack_booster_btn_glow":   "rack_booster_btn_glow.png",    # packApunch: the six preset buttons' lit faces + lamps (added on top)
    "rack_booster_btn_on":     "booster__button.png",
    "rack_booster_led_green":  "booster_Green__LED.png",   # 17x43px — IN meter
    "rack_booster_led_orange": "booster_Orange__LED.png",  # 17x43px — OUT meter
    # ---------------------------------------------------------------------------
    # Rack on/off + close buttons — universal across all racks.
    # RackOff.png: OFF button + close X side by side (100x38px)
    # RackOn.png:  ON button only, blank space where close X would be (100x38px)
    # Draw order: RackOff always, then RackOn on top if rack.enabled.
    # ---------------------------------------------------------------------------
    "rack_btn_off":          "RackOff.png",
    "rack_btn_on":           "RackOn.png",
    "rack_mixdown_bg":     "rack_mixdown_bg.png",
    # MIXDOWN toggle-button "on" overlays — off-state chrome for every button
    # (mode, format, sample rate, bit depth, range) is baked into
    # rack_mixdown_bg.png above; these blit on top of whichever button in
    # each section is currently active. One PNG per section, reused for
    # every button in that section (same pattern as rack_booster_btn_on).
    "rack_mixdown_mode_btn_on":   "mixdown_mode_btn_on.png",
    "rack_mixdown_format_btn_on": "mixdown_format_btn_on.png",
    "rack_mixdown_sr_btn_on":     "mixdown_sr_btn_on.png",
    "rack_mixdown_bd_btn_on":     "mixdown_bd_btn_on.png",
    "rack_mixdown_range_btn_on":  "mixdown_range_btn_on.png",
    # ---------------------------------------------------------------------------
    # Collapsed rack backgrounds — one PNG per effect type, same convention as
    # the expanded "_bg" skins above (rack_<type>_collapsed_bg.png in
    # skins/default/). Title/logo/expand-arrow are baked into the art; the
    # rest of the row (badge #, preset name, channel badges, ON/OFF+close)
    # draws on top at fixed coordinates — see rack_base._COLLAPSED_BG_KEY.
    # Drop the PNG in with the exact filename below and it just works, no
    # code changes needed. Missing files fall back to a flat rect + border.
    # ---------------------------------------------------------------------------
    "rack_comp_single_collapsed_bg": "rack_comp_single_collapsed_bg.png",
    "rack_comp_multi_collapsed_bg":  "rack_comp_multi_collapsed_bg.png",
    "rack_eq_collapsed_bg":          "rack_eq_collapsed_bg.png",
    "rack_reverb_collapsed_bg":      "rack_reverb_collapsed_bg.png",
    "rack_noisegate_collapsed_bg":   "rack_noisegate_collapsed_bg.png",
    "rack_delay_collapsed_bg":       "rack_delay_collapsed_bg.png",
    "rack_booster_collapsed_bg":     "rack_booster_collapsed_bg.png",
    "rack_mixdown_collapsed_bg":     "rack_mixdown_collapsed_bg.png",
    # ---------------------------------------------------------------------------
    # Collapsed rack per-channel LED strip — universal across all rack types
    # (see rack_base._draw_rack_collapsed). One small LED per channel (1-9),
    # channel number drawn above each. Unused channels show the "off" LED;
    # channels routed to this rack blink between "off" and "on" to show
    # they're live. Same two files cover every rack type — no per-type
    # naming needed here.
    # ---------------------------------------------------------------------------
    "rack_collapsed_ch_led_off": "rack_collapsed_ch_led_off.png",
    "rack_collapsed_ch_led_on":  "rack_collapsed_ch_led_on.png",
    # ---------------------------------------------------------------------------
    # AI rack collapsed backgrounds — same convention as the DSP racks'
    # collapsed "_bg" skins above (_COLLAPSED_BG_KEY in rack_base.py). The AI
    # racks' collapsed row now uses that identical shared layout (badge, the
    # universal channel LED strip above, ON/OFF+close below) instead of its
    # own one-off flat chrome. Title/logo/expand-arrow bake into the art;
    # missing files fall back to the old flat rect + border so nothing
    # disappears before art exists for a given type.
    # ---------------------------------------------------------------------------
    "rack_piper_collapsed_bg":      "rack_piper_collapsed_bg.png",
    "rack_knnvc_collapsed_bg":      "rack_knnvc_collapsed_bg.png",
    "rack_voicefixer_collapsed_bg": "rack_voicefixer_collapsed_bg.png",
    "rack_whisper_collapsed_bg":    "rack_whisper_collapsed_bg.png",
    "rack_demucs_collapsed_bg":     "rack_demucs_collapsed_bg.png",
    "rack_knnvc_bg":       "rack_knnvc_bg.png",
    "rack_knnvc_bg_off":   "rack_knnvc_bg_off.png",
    "rack_knnvc_off_btn":  "rack_knnvc_off_btn.png",
    "rack_knnvc_key_lit":  "rack_knnvc_key_lit.png",
    "rack_demucs_bg":      "rack_demucs_bg.png",
    "rack_demucs_bg_lit":  "rack_demucs_bg_lit.png",   # packApunch: lit stem-pad lens, model-slab glow, ON / SPLIT / channel-key glow (premultiplied)
    "rack_demucs_rain":    "rack_demucs_rain.png",   # packApunch: glyph atlas for the procedural code rain (sharp cells on top, blurred copy below; premultiplied white)
    # Demucs stem-pad ON glow overlay — off-state is baked into rack_demucs_bg.png
    # above; this blits on top of whichever stem pads are currently enabled
    # (same convention as rack_booster_btn_on / mixdown_*_btn_on above).
    "rack_demucs_stem_on": "rack_demucs_stem_on.png",
    # AI rack full-unit skins (rail + body baked into one image, same
    # convention as the DSP rack "_bg" skins above). Drop the PNG in with
    # this exact filename and _draw_ai_rack_expanded picks it up automatically
    # — no code changes needed. Missing files fall back to the flat GPU chassis.
    "rack_piper_bg":       "rack_piper_bg.png",
    "rack_piper_bg_off":   "rack_piper_bg_off.png",        # packApunch: screens dead (eye is separate)
    "rack_piper_eye_off":  "rack_piper_eye_off.png",       # packApunch: dark HAL lens patch
    "rack_piper_screens":  "rack_piper_screens.png",       # packApunch: CRT screen matte (white x alpha) for the phosphor tint
    "rack_piper_eye_glow": "rack_piper_eye_glow.png",      # packApunch: additive eye bloom / pulse
    "rack_voicefixer_bg":  "rack_voicefixer_bg.png",
    "rack_voicefixer_bg_lit": "rack_voicefixer_bg_lit.png",   # packApunch: backlit mode windows + lit keys (optional crossfade art)
    "rack_voicefixer_screen_frame": "rack_voicefixer_screen_frame.png",   # packApunch: bezel over the big screens, cut by the screen matte (premultiplied)
    "rack_whisper_bg":     "rack_whisper_bg.png",
    "rack_whisper_bg_lit": "rack_whisper_bg_lit.png",   # packApunch: lit keys, selected buttons, lit toggle slots, TRANSCRIBE rim pulse + wiped-label patch (premultiplied)
    "rack_whisper_bg_off": "rack_whisper_bg_off.png",   # packApunch: the whole rack with its lights off (blitted over the lit art while powered down)
    "rack_mixdown_bg_lit": "rack_mixdown_bg_lit.png",   # packApunch: Mixdown art with every key lit (patches crossfade over selected keys)
    "rack_mixdown_bg_off": "rack_mixdown_bg_off.png",   # packApunch: Mixdown with its lights off (dark screens + dark ON / arrow / save keys)
    "add_rack_btn":        "add_rack_btn.png",
    "add_ai_rack_btn":     "add_ai_rack_btn.png",
    "rack_chassis":  "rack_chassis.png",
    "rack_rail":     "rack_rail.png",

    # ---------------------------------------------------------------------------
    # Rack channel buttons — universal defaults used across all racks.
    # Per-rack overrides follow the naming convention rack_{type}_ch_btn_*.
    # ---------------------------------------------------------------------------
    "rack_ch_btn_off":       "rack_ch_btn_off.png",       # unassigned state (all racks)
    "rack_ch_btn_on":        "rack_ch_btn_on.png",        # assigned state (all racks)

    # Per-rack channel button overrides — take priority over universal defaults above.
    # Add rack-specific filenames here when needed, e.g:
    # "rack_mb_ch_btn_off":  "rack_mb_ch_btn_off.png",
    # "rack_mb_ch_btn_on":   "rack_mb_ch_btn_on.png",

    # ---------------------------------------------------------------------------
    # Rack fader handles — per-band for multiband compressor.
    # Universal fallback: rack_fader_handle for single-fader racks.
    # ---------------------------------------------------------------------------
    "rack_fader_handle":     "rack_fader_handle.png",     # universal fallback
    "rack_mb_fader_low":     "rack_mb_fader_low.png",     # COMP_MULTI band 0 (blue)
    "rack_mb_fader_lmid":    "rack_mb_fader_lmid.png",    # COMP_MULTI band 1 (green)
    "rack_mb_fader_hmid":    "rack_mb_fader_hmid.png",    # COMP_MULTI band 2 (amber)
    "rack_mb_fader_high":    "rack_mb_fader_high.png",    # COMP_MULTI band 3 (red)
}

# kNN-VC knob LED arc meter segments (packApunch): knnvc_arc_<knob>_<L|R><0-5>
for _ki in (0, 1):
    for _sd in ("L", "R"):
        for _i in range(6):
            SKIN_MAP[f"knnvc_arc_{_ki}_{_sd}{_i}"] = f"KnnvcArcs/knnvc_arc_{_ki}_{_sd}{_i}.png"

# ---------------------------------------------------------------------------
# Runtime cache: key -> gpu.types.GPUTexture
# ---------------------------------------------------------------------------
_texture_cache: dict = {}
_active_skin:   str  = "default"
_skin_dir:      str  = ""
_blit_logged:   set  = set()   # keys already logged to console


def set_skin_dir(assets_dir: str, skin_name: str = "default") -> None:
    """Called once at startup from Loader.py with the assets path."""
    global _skin_dir, _active_skin
    _active_skin = skin_name
    _skin_dir    = os.path.join(assets_dir, skin_name)
    print(f"[SKIN] skin dir: {_skin_dir}")
    print(f"[SKIN] dir exists: {os.path.isdir(_skin_dir)}")
    if os.path.isdir(_skin_dir):
        found = [f for f in os.listdir(_skin_dir) if f.endswith('.png')]
        print(f"[SKIN] PNGs found: {found if found else 'NONE'}")


def get_active_skin() -> str:
    """Return the currently active skin name (e.g. 'default', 'packapunch').
    Used by core/packapunch.py to decide which direction toggle() swaps."""
    return _active_skin


def get_texture(key: str):
    """Return a cached gpu.GPUTexture for key, or None if not available.

    None means the caller should fall back to GPU primitive drawing.
    This is the only function draw_utils.py needs to call.
    """
    if key in _texture_cache:
        return _texture_cache[key]

    filename = SKIN_MAP.get(key)
    if not filename or not _skin_dir:
        return None

    filepath = os.path.join(_skin_dir, filename)
    if not os.path.exists(filepath):
        # Previously silent — every "not found" key looked identical to a key
        # that was simply never requested, which made this exact situation
        # impossible to diagnose from the console alone. Log once and cache
        # None so it doesn't spam every redraw.
        print(f"[SKIN] '{key}' NOT FOUND — expected {filepath}")
        _texture_cache[key] = None
        return None

    try:
        import bpy
        img = bpy.data.images.load(filepath, check_existing=True)
        # Skin PNGs are pre-rendered UI chrome, not scene-referred color
        # photography — they need to land on screen as literally the bytes
        # in the file. Per Blender's own GPU color-management docs, an
        # Image datablock drawn via the UI path is meant to round-trip
        # (sRGB -> linear on upload, linear -> sRGB again on display) and
        # cancel out to a no-op.
        #
        # CONFIRMED (not just theorized) against real Blender builds:
        #   - 4.3 / 4.5: that round-trip completes correctly on its own —
        #     skins render correctly with NO override needed.
        #   - 5.2: only the upload-side half (sRGB -> linear) still runs;
        #     the display-side re-encode back to sRGB is missing, which
        #     showed up as flattened/dark skins losing shading detail.
        # Marking the image 'Non-Color' skips the upload-side conversion
        # entirely, which correctly cancels out 5.2's missing display-side
        # half — but on 4.3/4.5, where BOTH halves already run correctly,
        # doing that removes the first half of an otherwise-complete
        # round-trip and leaves only the display-side re-encode, which
        # blows the image out (confirmed: this was tried unconditionally
        # first, fixed 5.2, blew out 4.5). So it must be gated to the
        # Blender versions that actually need it rather than applied
        # everywhere. Revisit this version cutoff if a future Blender
        # release changes its UI color-management path again.
        if bpy.app.version >= (5, 0, 0):
            img.colorspace_settings.name = 'Non-Color'
        tex = gpu.texture.from_image(img)
        _texture_cache[key] = tex
        print(f"[SKIN] loaded '{key}' from {os.path.basename(filepath)}")
        return tex
    except Exception as e:
        print(f"[SKIN] failed to load '{key}': {e}")
        _texture_cache[key] = None   # don't retry
        return None


def get_texture_with_fallback(*keys):
    # Try each key in order, return (texture, key) for first found, or (None, None)
    for key in keys:
        tex = get_texture(key)
        if tex is not None:
            return tex, key
    return None, None


def blit_texture(tex, x: float, y: float, w: float, h: float,
                 alpha: float = 1.0, key: str = "", blend: str = "ALPHA_PREMULT",
                 uv=None) -> None:
    """Blit a gpu.GPUTexture into a screen-space rect (x,y = bottom-left).

    blend: GPU blend mode passed to gpu.state.blend_set before drawing.
           Default "ALPHA_PREMULT" is correct for pre-multiplied skin PNGs.
           Pass "ALPHA" for overlays whose RGB is NOT pre-multiplied (e.g. glass layers).
    uv:    optional (u0, v0, u1, v1) sub-rect of the source texture to sample,
           normalized 0..1, where v=0 is the BOTTOM of the image and v=1 is
           the TOP (matches the default full-image mapping below). Use this
           to slice one tall combined-art PNG into separate draw calls (e.g.
           a divider bar stacked above a button bar in a single file).
           Defaults to the full image (0, 0, 1, 1) when omitted.
    """
    if tex is None or w <= 0 or h <= 0:
        return
    if key and key not in _blit_logged:
        _blit_logged.add(key)
        print(f"[SKIN] blitting '{key}' ({int(w)}x{int(h)}px)")
    try:
        gpu.state.blend_set(blend)
        shader = gpu.shader.from_builtin("IMAGE")
        verts  = [(x, y), (x+w, y), (x, y+h), (x+w, y+h)]
        u0, v0, u1, v1 = uv if uv is not None else (0, 0, 1, 1)
        uvs    = [(u0, v0), (u1, v0), (u0, v1), (u1, v1)]
        batch  = batch_for_shader(shader, "TRI_STRIP",
                                  {"pos": verts, "texCoord": uvs})
        shader.bind()
        shader.uniform_sampler("image", tex)
        batch.draw(shader)
        gpu.state.blend_set("ALPHA")  # restore standard blend for everything else
    except Exception as e:
        print(f"[SKIN] blit error '{key}': {e}")


_tint_shader = None

def blit_texture_tinted(tex, x: float, y: float, w: float, h: float,
                        color=(1.0, 1.0, 1.0, 1.0), blend: str = "ADDITIVE_PREMULT",
                        uv=None, key: str = "") -> None:
    """Like blit_texture, but multiplies the texture by `color` (r,g,b,a) so a
    layer can be faded / tinted per draw. Built for glow layers: with the
    default "ADDITIVE_PREMULT" blend the texture's rgb x color.rgb is simply
    ADDED to whatever is already on screen, so (k,k,k,k) acts as a brightness
    knob and black pixels in the texture add nothing. Uses Blender's
    IMAGE_COLOR builtin shader. Any GPU error is printed and swallowed, and
    the blend mode is always restored to "ALPHA" afterwards."""
    global _tint_shader
    if tex is None or w <= 0 or h <= 0:
        return
    try:
        if _tint_shader is None:
            _tint_shader = gpu.shader.from_builtin("IMAGE_COLOR")
        gpu.state.blend_set(blend)
        u0, v0, u1, v1 = uv if uv is not None else (0, 0, 1, 1)
        verts = [(x, y), (x+w, y), (x, y+h), (x+w, y+h)]
        uvs   = [(u0, v0), (u1, v0), (u0, v1), (u1, v1)]
        batch = batch_for_shader(_tint_shader, "TRI_STRIP",
                                 {"pos": verts, "texCoord": uvs})
        _tint_shader.bind()
        _tint_shader.uniform_sampler("image", tex)
        _tint_shader.uniform_float("color", color)
        batch.draw(_tint_shader)
    except Exception as e:
        print(f"[SKIN] tinted blit error '{key}': {e}")
    finally:
        try:
            gpu.state.blend_set("ALPHA")
        except Exception:
            pass


def blit_texture_rotated(tex, cx: float, cy: float, w: float, h: float, angle: float,
                         blend: str = "ALPHA_PREMULT", uv=None, key: str = "", color=None, clip_ymin=None, clip_xmax=None) -> None:
    """Blit a texture centred on (cx, cy), w x h in size, rotated by `angle`
    radians (counter-clockwise on screen, y-up). Built for knobs: one still
    image of the knob is turned to show the value, no sprite sheet needed.
    `blend` and `uv` behave as in blit_texture. `color` (r,g,b,a), if given, multiplies the
    texture (for pre-multiplied fades pass (k,k,k,k)). `clip_ymin`, if given, cuts away everything below that
    screen y, and `clip_xmax` everything right of that screen x (so a big rotated image can't spill over the edge of the area it belongs to).
    Errors are printed and swallowed."""
    global _tint_shader
    if tex is None or w <= 0 or h <= 0:
        return
    if key and key not in _blit_logged:
        _blit_logged.add(key)
        print(f"[SKIN] blitting rotated '{key}' ({int(w)}x{int(h)}px)")
    try:
        ca, sa = math.cos(angle), math.sin(angle)
        hw, hh = w * 0.5, h * 0.5
        corners = [(-hw, -hh), (hw, -hh), (-hw, hh), (hw, hh)]
        verts = [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in corners]
        u0, v0, u1, v1 = uv if uv is not None else (0, 0, 1, 1)
        uvs = [(u0, v0), (u1, v0), (u0, v1), (u1, v1)]
        gpu.state.blend_set(blend)
        if color is not None:
            if _tint_shader is None:
                _tint_shader = gpu.shader.from_builtin("IMAGE_COLOR")
            shader = _tint_shader
        else:
            shader = gpu.shader.from_builtin("IMAGE")
        planes = []                                   # half-planes to keep: nx*x + ny*y >= c
        if clip_ymin is not None:
            planes.append((0.0, 1.0, clip_ymin))
        if clip_xmax is not None:
            planes.append((-1.0, 0.0, -clip_xmax))
        if not planes:
            batch = batch_for_shader(shader, "TRI_STRIP", {"pos": verts, "texCoord": uvs})
        else:
            # Sutherland-Hodgman on the quad (ring order 0,1,3,2), uvs follow along
            poly = [(verts[i], uvs[i]) for i in (0, 1, 3, 2)]
            for nx, ny, c in planes:
                out = []
                for i in range(len(poly)):
                    (p0, t0), (p1, t1) = poly[i], poly[(i + 1) % len(poly)]
                    d0 = nx * p0[0] + ny * p0[1] - c
                    d1 = nx * p1[0] + ny * p1[1] - c
                    if d0 >= 0:
                        out.append((p0, t0))
                    if (d0 >= 0) != (d1 >= 0):
                        f = d0 / (d0 - d1)
                        out.append(((p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f),
                                    (t0[0] + (t1[0] - t0[0]) * f, t0[1] + (t1[1] - t0[1]) * f)))
                poly = out
                if len(poly) < 3:
                    return
            batch = batch_for_shader(shader, "TRI_FAN", {"pos": [o[0] for o in poly],
                                                         "texCoord": [o[1] for o in poly]})
        shader.bind()
        shader.uniform_sampler("image", tex)
        if color is not None:
            shader.uniform_float("color", color)
        batch.draw(shader)
    except Exception as e:
        print(f"[SKIN] rotated blit error '{key}': {e}")
    finally:
        try:
            gpu.state.blend_set("ALPHA")
        except Exception:
            pass


def clear_cache() -> None:
    """Flush the texture cache (call on file load or skin change)."""
    global _texture_cache
    _texture_cache = {}