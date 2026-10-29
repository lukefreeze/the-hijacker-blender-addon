# =============================================================================
# ui/mixer/mixer_hud.py
# The GPU draw callback that paints the entire HUD onto the Node Editor area.
# Also owns the UI state globals (UI_SCALE, SCROLL_X/Y) so other modules
# can import them without circular dependencies.
# =============================================================================

import bpy
import gpu
import blf
import time

from ui.mixer.draw_utils import draw_rect, draw_rounded_rect
from ui.mixer.channel_strip import (
    draw_channel_strip, send_section_height,
    STRIP_LEFT_MARGIN, STRIP_W, STRIP_STRIDE,
)
from core.constants import (
    DEFAULT_CHANNELS, MAX_CHANNELS,
    AUTOFIT_CONTENT_W, AUTOFIT_CONTENT_H, AUTOFIT_PADDING,
    SB_TRACK_PX, SB_THUMB_PX, SB_INSET, SB_MARGIN, SB_RADIUS,
    SB_H_RANGE, SB_V_RANGE,
)
import core.meters as _meters_mod  # import module not values — avoids stale refs after reload
import core.perf_monitor as _perf_mod

# ---------------------------------------------------------------------------
# UI state — imported by interaction.py and Loader.py
# ---------------------------------------------------------------------------
UI_SCALE  = 1.0
SCROLL_X  = 0.0
SCROLL_Y  = 0.0

pb_ui_enabled = False
HUD_AREA_PTR  = None
_draw_diag_done = False  # print layer order once on first draw


# ---------------------------------------------------------------------------
# Lazy cached wrapper for draw_racks
# A plain module-level import silently binds to a stub because Racks.py is
# registered after mixer_hud imports. Defer lookup to first call and cache.
# ---------------------------------------------------------------------------
_draw_racks_fn = None

def _draw_racks(width, height, scroll_x, scroll_y, ui_scale):
    global _draw_racks_fn
    if _draw_racks_fn is None:
        from Racks import draw_racks as _fn
        _draw_racks_fn = _fn
    _draw_racks_fn(width, height, scroll_x, scroll_y, ui_scale)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def save_ui_state() -> None:
    scene = bpy.context.scene
    if not scene:
        return
    scene.pb_ui_scale    = UI_SCALE
    scene.pb_ui_scroll_x = SCROLL_X
    scene.pb_ui_scroll_y = SCROLL_Y
    scene.pb_ui_enabled  = pb_ui_enabled


def load_ui_state() -> None:
    global UI_SCALE, SCROLL_X, SCROLL_Y, pb_ui_enabled
    scene = bpy.context.scene
    if not scene:
        return
    UI_SCALE      = getattr(scene, "pb_ui_scale",    1.0)
    SCROLL_X      = getattr(scene, "pb_ui_scroll_x", 0.0)
    SCROLL_Y      = getattr(scene, "pb_ui_scroll_y", 0.0)
    pb_ui_enabled = getattr(scene, "pb_ui_enabled",  False)
    global _draw_diag_done; _draw_diag_done = False  # re-print layers on next draw
    print(f"[STATE] scale={round(UI_SCALE,2)} "
          f"scroll=({round(SCROLL_X)},{round(SCROLL_Y)}) "
          f"enabled={pb_ui_enabled}")


# The PACK-A-PUNCH trigger button sits to the left of the mixing desk, at the
# desk's mid-height, clear of the desk/board artwork, and moves with the desk
# when you pan/zoom so it always stays beside it. Art is
# ui/assets/packapunch/packapunch_btn(.png|_pressed.png).
#   PAP_BTN_W       unscaled width of the whole picture (the plate itself is
#                   only about a quarter of it — the rest is sticky notes)
#   PAP_BTN_RIGHT_X where the picture's RIGHT edge sits, in content units
#                   (desk starts at STRIP_LEFT_MARGIN=30). The Background.png
#                   table art spans x=-300 .. 1590, so -330 puts the picture
#                   just past its left edge, on its own. Lower = further left.
PAP_BTN_W         = 900.0
PAP_BTN_ASPECT    = 676.0 / 936.0      # fallback only: the real proportions are
                                       # read from the art (get_button_aspect())
PAP_BTN_RIGHT_X   = -330.0
# Extra width compute_autofit() keeps free on the left so the whole picture
# fits on screen (plus a little padding).
PAP_BTN_LEFT_RESERVE = max(0.0, STRIP_LEFT_MARGIN - (PAP_BTN_RIGHT_X - PAP_BTN_W)) + 6.0
# Only the red plate is clickable, not the sticky notes around it — as
# fractions of the picture (x from left, y from TOP).
PAP_BTN_HIT = (0.39, 0.655, 0.30, 0.69)    # x0, x1, y0, y1


def packapunch_button_rect(width: float, height: float, ui_scale: float,
                           scroll_x: float = None, scroll_y: float = None):
    """Screen rect (x, y bottom-left, w, h) of the PACK-A-PUNCH button
    picture. Shared by draw_callback_px() and interaction.py's click test
    so they can never drift apart. Pass the current scroll values; they
    default to this module's own."""
    sx = SCROLL_X if scroll_x is None else scroll_x
    sy = SCROLL_Y if scroll_y is None else scroll_y
    bw = PAP_BTN_W * ui_scale
    try:
        from core import packapunch as _pap_a
        bh = bw * _pap_a.get_button_aspect()      # proportions of the actual art
    except Exception:
        bh = bw * PAP_BTN_ASPECT
    # Same desk height the desk background uses in draw_callback_px().
    try:
        from ui.mixer.channel_strip import (
            send_section_height as _sh, SEND_MIN_SLOTS as _SMS,
            STRIP_TOTAL_H as _STH)
        from Racks import RACK_MARGIN_TOP as _RMT
        desk_h = _STH * ui_scale + _sh(_SMS, ui_scale) + _RMT * ui_scale
    except Exception:
        desk_h = 400.0 * ui_scale
    base_y    = height - 150 * ui_scale - sy          # top of the desk
    bx = sx + PAP_BTN_RIGHT_X * ui_scale - bw
    by = base_y - desk_h / 2.0 - bh / 2.0
    return bx, by, bw, bh


def packapunch_button_hit(rx, ry, width, height, ui_scale,
                          scroll_x=None, scroll_y=None) -> bool:
    bx, by, bw, bh = packapunch_button_rect(width, height, ui_scale,
                                            scroll_x, scroll_y)
    x0, x1, y0, y1 = PAP_BTN_HIT
    return (bx + x0 * bw <= rx <= bx + x1 * bw and
            by + (1.0 - y1) * bh <= ry <= by + (1.0 - y0) * bh)


def compute_autofit(draw_w: float, draw_h: float, with_button: bool = False):
    """Return (UI_SCALE, SCROLL_X, SCROLL_Y) to centre the mixer in the canvas.

    By default (with_button=False) it frames the desk and racks alone, so
    the PACK-A-PUNCH button sits just off-screen to the left for people to
    find. with_button=True would make room for it in the picture."""
    # The master widget is a FIXED-size panel (it ignores the zoom), so its
    # width is taken off the canvas in real screen pixels before fitting.
    # PAP_BTN_LEFT_RESERVE: room (in zoomed units) on the left for the
    # PACK-A-PUNCH button that sits beside the desk.
    try:
        import ui.mixer.master_widget as _mw_fit
        master_px = float(_mw_fit.reserve_w())
    except Exception:
        master_px = 100.0
    usable_w = max(200.0, draw_w - master_px)
    btn_reserve = PAP_BTN_LEFT_RESERVE if with_button else 0.0
    content_w = AUTOFIT_CONTENT_W + btn_reserve
    fit_x   = usable_w / content_w
    fit_y   = draw_h / AUTOFIT_CONTENT_H
    scale   = max(0.35, min(2.0, min(fit_x, fit_y) * AUTOFIT_PADDING))
    scr_x   = ((usable_w - content_w * scale) / 2.0 - 30.0 * scale
               + btn_reserve * scale)
    scr_y   = draw_h / 2.0 - 550.0 * scale
    return scale, scr_x, scr_y


def frame_on_desk() -> bool:
    """Re-frame the view on the mixing desk and racks, as the first-open
    auto-fit does but WITHOUT leaving room for the PACK-A-PUNCH button — so
    the button sits just off-screen to the left and the desk fills the view.
    Used by the PACK-A-PUNCH transition: it runs while the overlay is opaque,
    so when the overlay fades out the reveal is already framed on the desk.
    Returns False if no Node Editor to size it from was found."""
    global UI_SCALE, SCROLL_X, SCROLL_Y
    best, best_px = None, 0
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != 'NODE_EDITOR':
                    continue
                for rgn in area.regions:
                    if rgn.type != 'WINDOW':
                        continue
                    px = rgn.width * rgn.height
                    if HUD_AREA_PTR is not None and area.as_pointer() == HUD_AREA_PTR:
                        px += 10 ** 12          # the HUD's own area always wins
                    if px > best_px:
                        best_px, best = px, (rgn.width, rgn.height)
    except Exception:
        return False
    if best is None or best[0] < 100 or best[1] < 100:
        return False
    UI_SCALE, SCROLL_X, SCROLL_Y = compute_autofit(best[0], best[1], with_button=False)
    save_ui_state()
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:
        pass
    print(f"[HUD] framed on desk: scale={UI_SCALE:.3f} canvas={best[0]}x{best[1]}")
    return True


# ---------------------------------------------------------------------------
# Background image tuning
# BG_IMG_W / BG_IMG_H — match these to your PNG pixel dimensions.
# BG_X_OFFSET / BG_Y_OFFSET — unscaled px, multiplied by UI_SCALE at draw time.
#   Positive X moves right, negative moves left.
#   Positive Y moves up, negative moves down.
# ---------------------------------------------------------------------------
BG_IMG_W    = 1891.0   # Background.png width  in px  ← update when image changes
BG_IMG_H    = 5210.0   # Background.png height in px  ← update when image changes
BG_X_OFFSET = -300.0   # nudge background left/right
BG_Y_OFFSET = 0.0      # nudge background up/down

# ---------------------------------------------------------------------------
# Main draw callback — registered on SpaceNodeEditor in Loader.py
# ---------------------------------------------------------------------------
def draw_callback_px(self, context) -> None:
    global pb_ui_enabled, UI_SCALE, SCROLL_X, SCROLL_Y

    if not pb_ui_enabled:
        return
    if HUD_AREA_PTR is not None and bpy.context.area is not None:
        if bpy.context.area.as_pointer() != HUD_AREA_PTR:
            return

    region = bpy.context.region
    if not region:
        return
    width, height = region.width, region.height

    _draw_start = time.perf_counter()
    try:
        gpu.state.blend_set("ALPHA")

        # Background — table PNG scrolls with the UI.
        # Falls back to nothing (transparent canvas) if PNG not loaded.
        # Tune BG_X_OFFSET / BG_Y_OFFSET below to reposition the image.
        try:
            from ui.mixer.texture_cache import get_texture as _gt_bg
            from ui.mixer.draw_utils import blit_texture as _bt_bg
            _bg_tex = _gt_bg("background")
            if _bg_tex:
                _bg_w = BG_IMG_W * UI_SCALE
                _bg_h = BG_IMG_H * UI_SCALE
                _bg_x = SCROLL_X + BG_X_OFFSET * UI_SCALE
                _bg_y = height - _bg_h - SCROLL_Y + BG_Y_OFFSET * UI_SCALE
                _bt_bg(_bg_tex, _bg_x, _bg_y, _bg_w, _bg_h, key="background")
            else:
                draw_rect(0, 0, width, height, (0.01, 0.01, 0.01, 0.95))
        except Exception:
            draw_rect(0, 0, width, height, (0.01, 0.01, 0.01, 0.95))

        # Header label
        blf.color(0, 1, 1, 1, 1)
        blf.size(0, int(20 * UI_SCALE))
        blf.position(0, STRIP_LEFT_MARGIN*UI_SCALE + SCROLL_X,
                     height - 40*UI_SCALE - SCROLL_Y, 0)
        blf.draw(0, f"THE HIJACKER | Scale: {round(UI_SCALE, 2)}")

        # PACK-A-PUNCH trigger button — a picture just left of the desk; the
        # pressed picture shows while it's held / the transition runs.
        try:
            from core import packapunch as _pap
            _pbx, _pby, _pbw, _pbh = packapunch_button_rect(
                width, height, UI_SCALE, SCROLL_X, SCROLL_Y)
            _pap_dn      = _pap.button_is_down()
            _pap_tex_btn = _pap.get_button_texture(_pap_dn)
            if _pap_tex_btn is not None:
                from ui.mixer.texture_cache import blit_texture as _bt_btn
                _bt_btn(_pap_tex_btn, _pbx, _pby, _pbw, _pbh,
                        key="packapunch_button", blend="ALPHA",
                        uv=_pap.get_button_uv(_pap_dn))
        except Exception as _pape:
            print(f"[PACKAPUNCH] button draw error: {_pape}")

        tracks = getattr(bpy.context.scene, "pb_sync_tracks", [])
        base_y = height - 150*UI_SCALE - SCROLL_Y

        # Groups of up to 9 channels — mirrors the strip loop below and
        # draw_racks() in Racks.py, which both continue seamlessly rightward
        # with no gap between groups. Previously the desk background blocks
        # below drew exactly once, sized only for the first 9 channels, so
        # channel 10+ strips and racks rendered fine but sat on bare canvas —
        # no second desk background was ever drawn for them. Now each group
        # gets its own copy of both background blocks, tiled to match.
        _num_groups = max(1, (len(tracks) + 8) // 9)

        # ── Mixer desk background ─────────────────────────────────────────
        # One PNG blit per group of up to 9 strips, tiled left to right.
        try:
            from ui.mixer.draw_utils import draw_element as _de
            from ui.mixer.channel_strip import send_section_height as _sh, SEND_MIN_SLOTS as _SMS
            from ui.mixer.channel_strip import STRIP_TOTAL_H as _STH
            from Racks import RACK_MARGIN_TOP as _RMT
            # Use fixed base height (SEND_MIN_SLOTS only) — PNG is never stretched.
            # Extra sends slots open downward and the PNG is tall enough to cover them.
            _base_send_h = _sh(_SMS, UI_SCALE)
            _strip_h_fixed = _STH * UI_SCALE + _base_send_h + _RMT * UI_SCALE
            _desk_y     = base_y - _strip_h_fixed   # anchored at top (base_y) — same for every group
            for _g in range(_num_groups):
                _n_strips = min(len(tracks) - _g*9, 9)
                if _n_strips <= 0:
                    break
                _desk_x = STRIP_LEFT_MARGIN * UI_SCALE + _g*9*STRIP_STRIDE*UI_SCALE + SCROLL_X
                _desk_w = _n_strips * STRIP_STRIDE * UI_SCALE + (STRIP_W - STRIP_STRIDE) * UI_SCALE
                _de("mixer_desk_bg", _desk_x, _desk_y, _desk_w, _strip_h_fixed,
                    draw_rect, (0.07, 0.07, 0.07, 1.0))
        except Exception:
            pass

        _ember_jobs = []   # packApunch embers: queued here, drawn AFTER the strip controls so they fly on top
        # ── 3-part strip skin backgrounds (drawn per group, once per group's strips) ──
        # Drawn BEFORE strip elements so knobs/faders draw on top of the background.
        try:
            from ui.mixer.texture_cache import get_texture as _gt, get_active_skin as _gas
            from ui.mixer.draw_utils import blit_texture as _bt
            from ui.mixer.channel_strip import (
                SEC_HEADER_H as _SHH, SEC_GAIN_H as _SGH,
                SEC_EQ_H as _SEQ, SEC_PAN_H as _SPAN,
                FADER_TOP_PAD as _FTP, FADER_HEIGHT as _FH,
                NUMBOX_H as _NBH, FADER_BOTTOM_PAD as _FBP,
                SENDS_LABEL_H as _SLH, SLOT_H as _SLOTH,
                SEND_MIN_SLOTS as _SMS,
                STRIP_LEFT_MARGIN as _SLM, STRIP_STRIDE as _SSTRIDE, STRIP_W as _SW,
            )
            scene2     = bpy.context.scene
            all_racks2 = getattr(scene2, "pb_racks", []) if scene2 else []

            for _g in range(_num_groups):
                _n2 = min(len(tracks) - _g*9, 9)
                if _n2 <= 0:
                    break
                n_racks2  = sum(1 for r in all_racks2 if getattr(r, "group_idx", 0) == _g)
                _slots2   = max(_SMS, n_racks2)
                _send_h2  = (_SLH + _slots2 * _SLOTH) * UI_SCALE
                _dx       = _SLM * UI_SCALE + _g*9*_SSTRIDE*UI_SCALE + SCROLL_X
                _dw       = _n2 * _SSTRIDE * UI_SCALE + (_SW - _SSTRIDE) * UI_SCALE

                # packApunch: animated lava cracks on the desk art (sends section / upper part), drawn over the desk
                # art but under the strip slices; it fades out toward the seam where the strip-bottom art starts
                try:
                    from ui.mixer.lava_fx import draw_desk_lava as _desk_lava
                    _seam_y = base_y - (_SHH + _SGH + 12 + 12) * UI_SCALE - _send_h2 + 7 * UI_SCALE   # top edge of the strip-bottom art
                    _desk_lava(_dx, base_y - _strip_h_fixed, _dw, _strip_h_fixed, seam_y=_seam_y, scale=UI_SCALE)
                except Exception:
                    pass

                # Top slice: HEADER + GAIN  (135px at scale 1)
                _top_h2 = (_SHH + _SGH) * UI_SCALE
                _tex = _gt("strip_top_bg")
                if _tex: _bt(_tex, _dx, base_y - _top_h2, _dw, _top_h2, key="strip_top_bg")

                # Send slot tile: tiled per slot row  (27px each at scale 1)
                _slot_h2   = _SLOTH * UI_SCALE
                _slot_top2 = base_y - (_SHH + _SGH + 12) * UI_SCALE - _SLH * UI_SCALE
                _stex = _gt("strip_send_slot_bg")
                if _stex:
                    for _si in range(_slots2):
                        _sy = _slot_top2 - _si * _slot_h2 - _slot_h2
                        _bt(_stex, _dx, _sy, _dw, _slot_h2, key="strip_send_slot_bg")

                # Bottom slice: EQ + PAN + FADER  (523px at scale 1)
                _bot_h2  = (_SEQ + _SPAN + _FTP + _FH + _NBH + _FBP) * UI_SCALE
                _bot_top = base_y - (_SHH + _SGH + 12 + 12) * UI_SCALE - _send_h2
                _btex = _gt("strip_bottom_bg")
                # the strip-bottom art is 572px tall: the original 565 plus 7px extending UP over the desk art for a
                # cleaner blend at the seam (bottom edge unchanged)
                # ONLY when the art really is taller than the 565px layout (packApunch ships 572): every other skin's
                # 565px art must be drawn 1:1, otherwise it is stretched and its knob holes drift off the live knobs.
                _bot_extra = 0.0
                if _btex is not None:
                    try:
                        _bot_extra = max(0.0, min(float(_btex.height) - 565.0, 16.0))
                    except Exception:
                        _bot_extra = 7.0 if _gas() == "packapunch" else 0.0
                _bot_ht = _bot_h2 + _bot_extra * UI_SCALE
                if _btex: _bt(_btex, _dx, _bot_top - _bot_h2, _dw, _bot_ht, key="strip_bottom_bg")
                if _btex:
                    try:   # packApunch: animated lava cracks on top of the (dimmed) strip art
                        from ui.mixer.lava_fx import draw_strip_lava as _lava, draw_embers as _embers
                        _lava(_dx, _bot_top - _bot_h2, _dw, _bot_ht, group=_g, scale=UI_SCALE)
                        # embers fly over the whole desk height (sends section + strips); drawn later, on top of everything
                        _ember_jobs.append((_dx, base_y, _dw, (_dx, _bot_top - _bot_h2, _dw, _bot_ht),
                                            (_dx, base_y - _strip_h_fixed, _dw, _strip_h_fixed), _g))
                    except Exception:
                        pass
                try:   # packApunch: the knob caps are in the art; draw only the live pointers on them
                    from ui.mixer import knob_fx as _kfx
                    if _kfx.art_knobs_active():
                        _kfx.draw_group_knobs((_dx, base_y - _top_h2, _dw, _top_h2),
                                              (_dx, _bot_top - _bot_h2, _dw, _bot_ht), tracks, _g * 9, _n2)
                except Exception as _ek:
                    print(f"[KNOBS] {_ek}")
        except Exception as _e3:
            pass


        draw_col = 0
        for i, track in enumerate(tracks):
            group_idx = i // 9
            sx = STRIP_LEFT_MARGIN*UI_SCALE + draw_col*STRIP_STRIDE*UI_SCALE + SCROLL_X
            draw_col += 1
            if sx + STRIP_W*UI_SCALE < 0 or sx > width:
                continue

            eng   = _meters_mod._engine_levels[i] if i < MAX_CHANNELS else 0.0
            peak  = _meters_mod._peak_hold[i]     if i < MAX_CHANNELS else 0.0
            draw_channel_strip(i, track, sx, base_y, UI_SCALE, tracks,
                               eng, peak, group_idx)

        # packApunch embers: the top layer of the desk, over the knobs, buttons and faders
        if _ember_jobs:
            try:
                from ui.mixer.lava_fx import draw_embers as _embers_top
                for _ej in _ember_jobs:
                    _embers_top(_ej[0], _ej[1], _ej[2], _ej[3], _ej[4], group=_ej[5])
            except Exception:
                pass

        # Racks
        try:
            _draw_racks(width, height, SCROLL_X, SCROLL_Y, UI_SCALE)
        except Exception as e:
            print(f"[RACKS] draw error: {e}")

        # Master bus fader + meter — fixed to the right edge, drawn over the desks
        try:
            import ui.mixer.master_widget as _mw_draw
            _mw_draw.draw(width, height, UI_SCALE)
        except Exception as _mwe:
            print(f"[MASTER] draw error: {_mwe}")

        # Scrollbars
        _draw_scrollbars(width, height)

        # PACK-A-PUNCH transition overlay — drawn last, on top of everything,
        # covering the full canvas. The skin swap already happened the
        # instant the transition started (see core/packapunch.py's module
        # docstring) — this overlay is purely a visual mask that fades away
        # via its OWN alpha channel to reveal the new skin underneath.
        try:
            from core import packapunch as _pap2
            _pap_frame = _pap2.get_overlay_frame()
            if _pap_frame is not None:
                _pap_tex, _pap_progress, _pap_alpha = _pap_frame
                if _pap_tex is not None:
                    # blend="ALPHA" (not "ALPHA_PREMULT") — a PNG-sequence
                    # transition exported from video/compositing software
                    # is typically straight (non-premultiplied) alpha,
                    # unlike the pre-baked premultiplied skin PNGs
                    # elsewhere in this file. Switch this if Luke's export
                    # pipeline produces premultiplied alpha instead.
                    #
                    # _pap_alpha < 1 during the closing fade: the tinted
                    # blit multiplies the frame (and so its alpha) by it,
                    # dissolving the overlay to reveal the skin beneath.
                    if _pap_alpha >= 0.999:
                        from ui.mixer.draw_utils import blit_texture as _bt_pap
                        _bt_pap(_pap_tex, 0, 0, width, height,
                                key="packapunch_frame", blend="ALPHA")
                    elif _pap_alpha > 0.0:
                        from ui.mixer.texture_cache import blit_texture_tinted as _btt_pap
                        _btt_pap(_pap_tex, 0, 0, width, height,
                                 color=(1.0, 1.0, 1.0, _pap_alpha),
                                 blend="ALPHA", key="packapunch_frame_fade")
        except Exception as _pape2:
            print(f"[PACKAPUNCH] overlay draw error: {_pape2}")

        # "Already welcomed" toast (PACK-A-PUNCH button after the first use).
        try:
            from core import packapunch as _pap3
            _toast = _pap3.get_toast()
            if _toast is not None:
                _t_text, _t_a = _toast
                _t_scale = max(0.75, min(1.5, height / 900.0))
                _t_size = int(16 * _t_scale)
                blf.size(0, _t_size)
                _t_w, _t_h = blf.dimensions(0, _t_text)
                _pad_x, _pad_y = 22 * _t_scale, 12 * _t_scale
                _bw, _bh = _t_w + 2 * _pad_x, _t_h + 2 * _pad_y
                _bx = (width - _bw) / 2.0
                _by = height * 0.12
                gpu.state.blend_set("ALPHA")
                draw_rounded_rect(_bx, _by, _bw, _bh, 8 * _t_scale,
                                  (0.05, 0.05, 0.06, 0.88 * _t_a))
                blf.color(0, 1.0, 1.0, 1.0, _t_a)
                blf.position(0, _bx + _pad_x, _by + _pad_y + 2 * _t_scale, 0)
                blf.draw(0, _t_text)
        except Exception as _pape3:
            print(f"[PACKAPUNCH] toast draw error: {_pape3}")

    except Exception as e:
        print(f"DRAW ERROR: {e}")
        import traceback; traceback.print_exc()
    finally:
        try:
            gpu.state.blend_set("NONE")
        except Exception:
            pass
        _perf_mod.record_draw_time(time.perf_counter() - _draw_start)


# ---------------------------------------------------------------------------
# Scrollbars
# ---------------------------------------------------------------------------
def _draw_scrollbars(width: float, height: float) -> None:
    t   = SB_TRACK_PX
    th  = SB_THUMB_PX
    ins = SB_INSET
    mar = SB_MARGIN
    r   = SB_RADIUS

    # Horizontal
    h_track_y = mar
    h_track_w = width - t - mar * 2
    draw_rect(mar, h_track_y, h_track_w, t, (0.10, 0.10, 0.10, 0.55))
    h_thumb_w = max(30, int(h_track_w * 0.12))
    h_travel  = h_track_w - h_thumb_w
    h_frac    = min(1.0, abs(SCROLL_X) / max(1.0, SB_H_RANGE))
    h_thumb_x = mar + int(h_frac * h_travel)
    draw_rounded_rect(h_thumb_x, h_track_y + ins, h_thumb_w, th, r,
                      (0.50, 0.50, 0.50, 0.80))

    # Vertical
    v_track_x = width - t - mar
    v_track_h = height - t - mar * 2
    draw_rect(v_track_x, mar + t, t, v_track_h, (0.10, 0.10, 0.10, 0.55))
    v_thumb_h = max(20, int(v_track_h * 0.18))
    v_travel  = v_track_h - v_thumb_h
    v_frac    = min(1.0, abs(SCROLL_Y) / max(1.0, SB_V_RANGE))
    v_thumb_y = mar + t + v_travel - int(v_frac * v_travel)
    draw_rounded_rect(v_track_x + ins, v_thumb_y, th, v_thumb_h, r,
                      (0.50, 0.50, 0.50, 0.80))