# =============================================================================
# core/packapunch.py
# The "PACK-A-PUNCH" transition — a wall-clock-driven PNG-sequence overlay
# that plays once in sync with a one-shot audio cue, while the UI underneath
# is swapped to the PackAPunch skin. Triggered by a button in mixer_hud.py's
# draw_callback_px; click routing lives in ui/mixer/interaction.py.
#
# DESIGN NOTES
# ------------
# Wall-clock sync, not frame counting.
#   Every redraw recomputes  frame_index = int(elapsed_s * fps)  fresh from
#   time.perf_counter(), instead of incrementing a counter once per redraw.
#   This is self-correcting: if Blender skips or delays a redraw (draw load,
#   a slow frame, whatever), the next redraw still lands on the frame that
#   matches real elapsed time rather than drifting behind. The audio cue is
#   fired once, on its own thread, from the same start_wall reference point
#   — neither one drives the other, they just both read the same clock.
#
# Skin swap happens immediately, not at the end.
#   Luke's plan: an overlay (opaque at first) fades out via its OWN alpha
#   channel to reveal the new skin underneath. That only works if the new
#   skin is already active underneath the overlay for the whole transition
#   — so start() swaps the skin immediately, and the overlay is purely a
#   visual mask on top that happens to fade away. There is no separate
#   "finish" step that swaps anything; get_overlay_frame() just stops
#   returning a texture once the sequence's duration has elapsed.
#
# Audio goes through core.audio.play_oneshot(), not the Hijacker engine.
#   play_oneshot() plays a WAV via a short-lived platform-native subprocess
#   (PowerShell/afplay/aplay) and explicitly never touches aud.Device() or
#   the Hijacker C++ engine (see its docstring in core/audio.py) — so
#   triggering PackAPunch mid-playback can never interrupt, desync, or
#   otherwise fight with whatever the Hijacker engine is doing with the
#   VSE mix. The tradeoff: subprocess launch has some tens-of-ms of startup
#   latency before audio is actually audible, so the START of the video and
#   audio is "good enough for a fun transition", not sample-accurate.
#   Sample-accurate sync would mean routing this through the C++ PortAudio
#   engine instead — deliberately out of scope for this first pass.
#
# The video and audio only share a START time, not a duration.
#   Luke's transition audio is a full music cue meant to keep playing as
#   ambient mood music well after the visual reveal finishes — the PNG
#   sequence only covers the video up to its fade-out point (no padding
#   frames wasting memory on a hold that isn't there). So the overlay's
#   own frame count at a FIXED export frame rate (VIDEO_FPS) is what
#   determines how long the visual transition runs; the audio subprocess
#   plays independently to its own full length regardless of how long the
#   overlay took. Once the overlay finishes, is_active() goes False (skin's
#   already swapped, button re-arms) even though the music may still be
#   going in the background — that's intentional, not a bug.
#
# Frames are optional.
#   Luke's real transition art (a PNG sequence exported from his video
#   edit) isn't in the repo yet. If ui/assets/packapunch/frames/ is empty
#   or missing, the transition still runs in full — skin swap + audio cue
#   — just without an image overlay on top. Drop numbered PNGs in that
#   folder later and they're picked up automatically on the next trigger,
#   no code changes needed.
#
# The frame texture cache is a small rolling window, not a growing pile.
#   Luke's real sequence is 305 frames at 1920x1080 RGBA — ~8MB of GPU
#   texture per frame, and Blender keeps the decoded pixel buffer for each
#   loaded Image data-block in system RAM too. The first version of this
#   file cached every frame forever once shown (never evicted, never
#   freed), so a single run accumulated roughly 2.5GB of VRAM and another
#   2.5GB of RAM by the time it reached frame 305 — nothing ever got freed
#   until the whole add-on reloaded. That growing memory pressure is what
#   was actually behind "getting further behind as it plays": it isn't a
#   timing bug (frame_index is still recomputed fresh from wall-clock time
#   every redraw, so it can never architecturally drift), it's that each
#   new frame's load+upload got slower as VRAM/RAM filled up, so real wall
#   time kept advancing past whatever frame was still loading. Fixed by
#   evicting frames well behind the current playhead (playback is strictly
#   forward and one-shot, so nothing behind the playhead is ever needed
#   again) and by fully clearing the cache both at the start of a run (in
#   case a previous run was interrupted mid-playback) and the moment a run
#   finishes.
# =============================================================================

import os
import glob
import time
import tempfile

import bpy
import gpu

from core.constants import ADDON_DIR, ASSETS_DIR
from core.audio import play_oneshot

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
PACKAPUNCH_DIR = os.path.join(ADDON_DIR, "ui", "assets", "packapunch")
FRAMES_DIR   = os.path.join(PACKAPUNCH_DIR, "frames")
# All PackAPunch assets live under one folder — frames/ for the PNG
# sequence, the audio cue right alongside it. Drop the real transition
# audio in here as "PackAPunch.mp3" (any format aud.Sound.file() can
# read works — it gets decoded to WAV once regardless, see
# _ensure_wav_audio() below).
AUDIO_SRC    = os.path.join(PACKAPUNCH_DIR, "PackAPunch.mp3")
if not os.path.isfile(AUDIO_SRC):
    # The file is committed as "packApunch.mp3"; case-sensitive filesystems
    # (Linux, macOS) won't match the name above, so pick it up case-insensitively.
    try:
        for _n in os.listdir(PACKAPUNCH_DIR):
            if _n.lower() == "packapunch.mp3":
                AUDIO_SRC = os.path.join(PACKAPUNCH_DIR, _n)
                break
    except OSError:
        pass
_WAV_CACHE   = os.path.join(tempfile.gettempdir(), "hijacker_packapunch_cache.wav")

TARGET_SKIN  = "packapunch"
DEFAULT_SKIN = "default"

# The frame rate the PNG sequence in frames/ plays at. The original 24fps
# export was thinned to every 2nd frame to halve the download size, so it
# plays at 12fps for the same on-screen speed. Together with HOLD_LAST_S
# below this is the ONLY thing that determines how long the visual overlay
# plays for — deliberately decoupled from the audio cue's length, see the
# module docstring above. Update both if a future re-export changes them.
VIDEO_FPS            = 12.0
# After the last frame (pure black) the overlay simply stays on it for this
# many seconds before the fade starts, instead of shipping a hundred-odd
# identical black frames. Original total on-screen time was 14.75s.
HOLD_LAST_S          = 5.83
# Used only when there's no frame sequence at all yet, so an audio-only
# transition still has a sensible on-screen duration before the button
# re-arms (the audio itself is unaffected by this — it always plays to
# its own full length regardless).
FALLBACK_DURATION_S  = 3.0

# The sequence ends on pure BLACK (held for HOLD_LAST_S). Rather than cutting
# from black straight to the new skin, the overlay's own alpha is faded
# 1 -> 0 over the LAST FADE_S seconds of the transition. The fade is computed
# from wall-clock time like everything else here, so it stays smooth even if
# redraws are skipped. Set to 0 to restore the old hard cut.
FADE_S               = 1.0

# While the transition audio plays, the mixing desk's channel meters follow
# the MUSIC instead of the channels' own signal (see audio_meter_levels()
# and the hook in core/meters.py), then hand back to the channels the
# moment the audio ends.
#   "bands": the 9 desk meters act as a 9-band spectrum analyser — channel
#            1 = bass up to channel 9 = treble — so they jump independently
#            instead of nine bars moving in lockstep. (Channels past 9, if
#            the desk has more, repeat the same bands.)
#   "level": every meter shows the overall loudness of the music.
METER_MODE           = "bands"
METER_BANDS          = 9
METER_BAND_LO_HZ     = 50.0
METER_BAND_HI_HZ     = 14000.0
METER_DB_RANGE       = 30.0    # each band spans this many dB below its own
                                # loud passages, so quiet bands still move
METER_LATENCY_S      = 0.0     # + delays the meters, - makes them lead the
                                # audio; nudge if they feel off the beat
_ENV_HOP_S           = 0.01    # envelope resolution (10ms)
_ENV_WIN             = 2048    # FFT window in samples

# How many already-shown frames to keep cached behind the current
# playhead, plus how far ahead to opportunistically pre-load. Playback is
# strictly forward and one-shot, so a small window is enough to smooth
# over redraw-timing jitter without letting the cache grow unbounded over
# a 300+-frame run — see the module docstring above.
#
# _PRELOAD_AHEAD started at 2, based on real console timing: individual
# frame loads at 1920x1080 RGBA averaged ~22ms but ran up to ~41ms, so
# prefetching 2 per redraw-timer tick could cost up to ~80ms — well over
# the timer's own ~33ms interval. That's backwards: the prefetch meant to
# keep the timer ahead of the draw callback was instead what made the
# timer itself fall behind (observed directly: measured redraw rate
# dropped to 10-24Hz against a 30Hz target for most of a run, recovering
# only once fewer new frames were left to prefetch near the end).
# Prefetching only 1 frame per tick keeps a single tick's worst case
# closer to that ~33ms budget.
_KEEP_BEHIND    = 2
_PRELOAD_AHEAD  = 1

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_active          = False
_start_wall       = 0.0
_frame_paths      = []      # sorted absolute PNG paths for this run
_frame_tex_cache  = {}       # frame index -> gpu.types.GPUTexture, rolling window
_frame_img_cache  = {}       # frame index -> bpy.types.Image, freed alongside the texture
_fps              = VIDEO_FPS
_duration_s       = FALLBACK_DURATION_S
_fade_start_s     = 0.0      # time (s) at which the alpha fade begins
_meter_env        = None     # (wav_path, mtime, ndarray[n, METER_BANDS], hop_s)
_meter_t0         = None     # perf_counter() when the audio cue was started
_cue_handle       = None     # play_oneshot() handle of the transition audio, so going back can stop just it
_target_skin      = TARGET_SKIN
_redraw_timer_registered = False

# ---- Debug/diagnostic counters (reset per run, printed to the console) ----
# Console output, not on-screen — see Window > Toggle System Console on
# Windows. These exist to answer "is it actually running at 24fps" with
# real numbers instead of another guess: DEBUG = False makes this a no-op
# once we've got what we need.
DEBUG                = False
_dbg_redraws_total   = 0
_dbg_last_report_t   = 0.0
_dbg_last_report_n   = 0
_dbg_last_frame_idx  = -1
_dbg_skipped_frames  = 0
_dbg_shown_frames    = set()
_dbg_load_times      = []   # list of (frame_idx, ms, blocking: bool)


# ---------------------------------------------------------------------------
# The on-desk trigger button: an image pair (normal / pressed) drawn by
# ui/mixer/mixer_hud.py just to the left of the mixing desk. It looks pushed
# in while the mouse is held on it, for at least BUTTON_MIN_DOWN_S after any
# click (so even a quick tap visibly travels), and for as long as the
# transition itself is running.
#
# Two art sets, chosen by the active skin:
#   default skin   ui/assets/packapunch/packapunch_btn.png / _pressed.png
#   packApunch     ui/assets/skins/packapunch/packapunch_btn_skin.png / _skin_pressed.png
# (if the packApunch pair is missing, the default pair is used there too).
# ---------------------------------------------------------------------------
BUTTON_IMG        = os.path.join(PACKAPUNCH_DIR, "packapunch_btn.png")
BUTTON_IMG_DOWN   = os.path.join(PACKAPUNCH_DIR, "packapunch_btn_pressed.png")
BUTTON_IMG_SKIN      = os.path.join(ASSETS_DIR, TARGET_SKIN, "packapunch_btn_skin.png")
BUTTON_IMG_SKIN_DOWN = os.path.join(ASSETS_DIR, TARGET_SKIN, "packapunch_btn_skin_pressed.png")
BUTTON_MIN_DOWN_S = 0.22
_btn_tex          = {}       # variant/state key -> GPUTexture (or None)
_btn_held         = False
_btn_down_t       = -1e9


_btn_uv           = {}       # key -> (u0, v0, u1, v1) of the visible art
_btn_px           = {}       # key -> (width, height) of the PNG
_btn_aspect       = {"default": 676.0 / 936.0, "skin": 676.0 / 936.0}   # height / width of the visible art, per art set
_BTN_ALPHA_MIN    = 8.0 / 255.0


def _button_variant() -> str:
    """"skin" while the packApunch skin is active and its own button art
    exists, otherwise "default"."""
    try:
        if get_active_skin() == TARGET_SKIN and os.path.exists(BUTTON_IMG_SKIN):
            return "skin"
    except Exception:
        pass
    return "default"


def _button_paths(variant):
    if variant == "skin":
        return BUTTON_IMG_SKIN, (BUTTON_IMG_SKIN_DOWN if os.path.exists(BUTTON_IMG_SKIN_DOWN) else BUTTON_IMG_SKIN)
    return BUTTON_IMG, BUTTON_IMG_DOWN


def _opaque_uv(img):
    """(u0, v0, u1, v1, w_px, h_px) of the part of `img` that actually has
    pixels (alpha above a tiny threshold), so transparent margins around the
    art — whatever canvas size it was exported on — are never drawn or
    counted in its proportions. Falls back to the whole image."""
    w, h = int(img.size[0]), int(img.size[1])
    try:
        import numpy as np
        buf = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
        alpha = buf.reshape(h, w, 4)[..., 3]          # row 0 = BOTTOM, like uv v=0
        cols = np.where(alpha.max(axis=0) > _BTN_ALPHA_MIN)[0]
        rows = np.where(alpha.max(axis=1) > _BTN_ALPHA_MIN)[0]
        if len(cols) and len(rows):
            x0, x1 = int(cols[0]), int(cols[-1]) + 1
            y0, y1 = int(rows[0]), int(rows[-1]) + 1
            return (x0 / w, y0 / h, x1 / w, y1 / h, x1 - x0, y1 - y0)
    except Exception as e:
        print(f"[PACKAPUNCH] button art trim failed, using whole image: {e}")
    return (0.0, 0.0, 1.0, 1.0, w, h)


def get_button_texture(pressed: bool):
    """GPU texture for the button art (the set that matches the active skin),
    loaded once and cached. None if the PNG is missing (the HUD then simply
    draws no button). The art's transparent margins are trimmed at draw time
    via get_button_uv(), and the on-screen proportions come from the normal
    image's visible area (get_button_aspect()), so the picture can be
    re-exported at any canvas size without coming out squashed."""
    variant = _button_variant()
    state   = "pressed" if pressed else "normal"
    key     = f"{variant}_{state}"
    if key in _btn_tex:
        return _btn_tex[key]
    tex  = None
    path = _button_paths(variant)[1 if pressed else 0]
    nkey = f"{variant}_normal"
    try:
        if os.path.exists(path):
            img = bpy.data.images.load(path, check_existing=True)
            # Straight-alpha art: stop Blender premultiplying it on upload
            # (the HUD draws it with plain "ALPHA" blending), so the soft
            # shadows and anti-aliased note edges composite cleanly.
            try:
                img.alpha_mode = 'CHANNEL_PACKED'
            except Exception:
                pass
            if bpy.app.version >= (5, 0, 0):
                img.colorspace_settings.name = 'Non-Color'   # see _load_frame()
            u0, v0, u1, v1, wpx, hpx = _opaque_uv(img)
            size = (int(img.size[0]), int(img.size[1]))
            _btn_px[key] = size
            if pressed and _btn_px.get(nkey) == size and nkey in _btn_uv:
                # Same canvas as the normal art: use ITS crop so the two stay
                # perfectly registered (the pressed art's own soft shadow edge
                # can end a pixel or two differently, which would shift it).
                u0, v0, u1, v1 = _btn_uv[nkey]
                wpx = max(1, int(round((u1 - u0) * size[0])))
                hpx = max(1, int(round((v1 - v0) * size[1])))
            _btn_uv[key] = (u0, v0, u1, v1)
            if not pressed and wpx > 0:
                _btn_aspect[variant] = hpx / float(wpx)
            elif pressed and wpx > 0 and abs(hpx / float(wpx) - _btn_aspect[variant]) > 0.02:
                print("[PACKAPUNCH] pressed button art has different proportions "
                      "than the normal art — it will be stretched to match")
            tex = gpu.texture.from_image(img)
        else:
            print(f"[PACKAPUNCH] button art not found: {path}")
    except Exception as e:
        print(f"[PACKAPUNCH] button art load failed: {e}")
    _btn_tex[key] = tex
    return tex


def get_button_uv(pressed: bool):
    """Sub-rectangle of the button texture holding the visible art."""
    key = f"{_button_variant()}_{'pressed' if pressed else 'normal'}"
    return _btn_uv.get(key, (0.0, 0.0, 1.0, 1.0))


def get_button_aspect() -> float:
    """Height / width of the button picture as drawn (for the active skin)."""
    get_button_texture(False)          # make sure the normal art has been measured
    return _btn_aspect[_button_variant()]


def _redraw_hud_areas():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "NODE_EDITOR":
                    area.tag_redraw()
    except Exception:
        pass


def _button_release_redraw():
    _redraw_hud_areas()
    return None


def button_press():
    global _btn_held, _btn_down_t
    _btn_held   = True
    _btn_down_t = time.perf_counter()
    # Make sure the button gets repainted both going down and, if the mouse
    # is let go before BUTTON_MIN_DOWN_S, when it comes back up.
    _redraw_hud_areas()
    try:
        bpy.app.timers.register(_button_release_redraw,
                                first_interval=BUTTON_MIN_DOWN_S + 0.02)
    except Exception:
        pass


# How long the pushed-in button is shown before the transition actually
# starts. start() swaps the skin synchronously, which stalls the UI for a
# moment; if that ran inside the click, the pressed picture would never get
# painted before the black overlay (or the skin swap itself) took over the
# screen. Waiting a few frames lets Blender draw the pressed button first.
BUTTON_FIRE_DELAY_S = 0.15
_btn_pending        = False


def button_click(on_fire):
    """The desk button was clicked: show it pressed right away, then call
    on_fire() once after BUTTON_FIRE_DELAY_S. Ignored while a transition is
    running or a click is already waiting to fire."""
    global _btn_pending
    if _active or _btn_pending:
        return
    button_press()
    _btn_pending = True

    def _fire():
        global _btn_pending
        _btn_pending = False
        try:
            if not _active:
                on_fire()
        except Exception as e:
            print(f"[PACKAPUNCH] button action error: {e}")
        _redraw_hud_areas()
        return None

    bpy.app.timers.register(_fire, first_interval=BUTTON_FIRE_DELAY_S)


def button_release():
    """Mouse let go. The button stays visibly down until BUTTON_MIN_DOWN_S
    has passed since the press, so schedule one redraw for the moment it
    pops back up (nothing else is guaranteed to repaint then)."""
    global _btn_held
    _btn_held = False
    remaining = BUTTON_MIN_DOWN_S - (time.perf_counter() - _btn_down_t)
    try:
        bpy.app.timers.register(_button_release_redraw,
                                first_interval=max(0.02, remaining + 0.02))
    except Exception:
        pass


def button_is_down() -> bool:
    return (_btn_held or _active
            or (time.perf_counter() - _btn_down_t) < BUTTON_MIN_DOWN_S)


# ---------------------------------------------------------------------------
# Public state queries — used by mixer_hud.py (draw) and interaction.py (click)
# ---------------------------------------------------------------------------
def is_active() -> bool:
    return _active


def get_active_skin() -> str:
    from ui.mixer.texture_cache import get_active_skin as _gas
    return _gas()


# ---------------------------------------------------------------------------
# Frame sequence discovery + lazy per-frame texture loading
# ---------------------------------------------------------------------------
def _scan_frames():
    """Find the numbered PNG frames for this run.

    Globs both *.png and *.PNG because a case-SENSITIVE filesystem (Linux,
    if this ever runs there) could genuinely have distinct files under
    each. But on Windows — a case-INSENSITIVE filesystem — those two
    glob patterns both match the exact same files, so every real frame
    was showing up twice in the combined list (305 real frames -> 610
    entries). That doubled list, once sorted, put each frame right next
    to a duplicate of itself, so every unique image played for 2/24s
    instead of 1/24s — the whole sequence took twice as long and looked
    like it was running at half speed. This was there well before the
    caching/prefetch work; dedupe by the normalized absolute path so a
    genuinely case-sensitive filesystem still works while Windows no
    longer double-counts."""
    if not os.path.isdir(FRAMES_DIR):
        return []
    seen  = set()
    paths = []
    for p in (glob.glob(os.path.join(FRAMES_DIR, "*.png")) +
              glob.glob(os.path.join(FRAMES_DIR, "*.PNG"))):
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            paths.append(p)
    return sorted(paths)


def _load_frame(idx, blocking):
    """Actually load+upload one frame. Never call directly from the draw
    path without going through _get_frame_texture()/_prefetch_frame() —
    this is the expensive, uncached operation.

    blocking: True when this is happening synchronously inside the draw
    callback because get_overlay_frame() needed this frame *right now*
    (i.e. the redraw timer's look-ahead didn't already warm it). False
    when it's a look-ahead prefetch happening in the redraw timer, which
    has more slack. Only used for the DEBUG timing breakdown below."""
    t0 = time.perf_counter()
    img = bpy.data.images.load(_frame_paths[idx], check_existing=True)
    # Same reasoning as ui/mixer/texture_cache.py's get_texture(): on
    # Blender 5.x only the upload-side sRGB->linear conversion runs for
    # GPU-drawn images, not the matching display-side re-encode, so a
    # pre-rendered transition frame needs Non-Color to land on screen
    # as the literal bytes in the file instead of coming out flattened.
    if bpy.app.version >= (5, 0, 0):
        img.colorspace_settings.name = 'Non-Color'
    tex = gpu.texture.from_image(img)
    _frame_tex_cache[idx] = tex
    _frame_img_cache[idx] = img
    if DEBUG:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        _dbg_load_times.append((idx, dt_ms, blocking))
        if blocking and dt_ms > 15.0:
            print(f"[PACKAPUNCH][DEBUG] frame {idx} loaded ON-DEMAND inside "
                  f"the draw callback, took {dt_ms:.1f}ms "
                  f"(24fps budget is {1000.0/_fps:.1f}ms) — this stalls that redraw")


def _evict_frames_behind(current_idx):
    """Free every cached frame more than _KEEP_BEHIND behind current_idx.
    Playback only ever moves forward within a run, so anything this far
    behind the playhead is never requested again — see the module
    docstring for why this matters (without it, a 305-frame run at
    1920x1080 RGBA accumulates ~2.5GB of VRAM + ~2.5GB of RAM and gets
    progressively slower as it plays)."""
    stale = [i for i in _frame_tex_cache if i < current_idx - _KEEP_BEHIND]
    for i in stale:
        _frame_tex_cache.pop(i, None)
        img = _frame_img_cache.pop(i, None)
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass


def _clear_frame_cache():
    """Free every currently-cached frame outright — called at the start of
    a run (in case a previous run was interrupted mid-playback and left
    frames cached) and the instant a run finishes (so the ~2.5GB this
    sequence can reach doesn't sit around until the next trigger)."""
    for img in _frame_img_cache.values():
        try:
            bpy.data.images.remove(img)
        except Exception:
            pass
    _frame_tex_cache.clear()
    _frame_img_cache.clear()


def _get_frame_texture(idx):
    if idx < 0 or idx >= len(_frame_paths):
        return None
    if idx not in _frame_tex_cache:
        try:
            _load_frame(idx, blocking=True)
        except Exception as e:
            print(f"[PACKAPUNCH] frame {idx} load failed: {e}")
            _frame_tex_cache[idx] = None
    _evict_frames_behind(idx)
    return _frame_tex_cache.get(idx)


def _prefetch_frame(idx):
    """Best-effort look-ahead load, called from the redraw timer (which
    ticks at 30Hz, slightly faster than the 24fps playback rate) so a
    frame's load+upload cost is more often already paid by the time
    get_overlay_frame() actually needs it, instead of always happening
    synchronously inside the draw callback. Silently does nothing if the
    frame's already cached, out of range, or fails to load — this is
    purely an optimization, never load-bearing for correctness."""
    if idx < 0 or idx >= len(_frame_paths) or idx in _frame_tex_cache:
        return
    try:
        _load_frame(idx, blocking=False)
    except Exception as e:
        print(f"[PACKAPUNCH] prefetch frame {idx} failed: {e}")
        _frame_tex_cache[idx] = None


# ---------------------------------------------------------------------------
# Audio — decode once to a cached WAV so play_oneshot() (WAV-only on
# Windows, via PowerShell's Media.SoundPlayer) can play it regardless of
# what format the source cue ships as.
# ---------------------------------------------------------------------------
def _ensure_wav_audio():
    if not os.path.exists(AUDIO_SRC):
        print(f"[PACKAPUNCH] audio cue not found: {AUDIO_SRC}")
        return None
    # Re-decode whenever the source is newer than the cached WAV — this
    # cache lives in the OS temp dir, which persists across Blender
    # restarts, so without this check swapping in new transition audio
    # would silently keep playing the old cached decode until someone
    # thought to clear temp.
    if (os.path.exists(_WAV_CACHE)
            and os.path.getmtime(_WAV_CACHE) >= os.path.getmtime(AUDIO_SRC)):
        return _WAV_CACHE
    try:
        import aud, wave as _wave, numpy as np
        snd  = aud.Sound.file(AUDIO_SRC)
        spec = snd.specs
        sr   = int(spec[0])
        nch  = int(spec[1])
        data = snd.data()
        if data is None or data.size == 0:
            print("[PACKAPUNCH] audio decode produced no samples")
            return None
        i16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
        with _wave.open(_WAV_CACHE, 'wb') as wf:
            wf.setnchannels(nch)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(i16.tobytes())
        print(f"[PACKAPUNCH] decoded {os.path.basename(AUDIO_SRC)} -> cached WAV")
        return _WAV_CACHE
    except Exception as e:
        print(f"[PACKAPUNCH] audio decode failed: {e}")
        return None


def _wav_duration_s(wav_path):
    """Length of a WAV file in seconds, or None if it can't be read.

    Not currently used for anything (start()/get_overlay_frame() no longer
    tie the overlay's duration to the audio's length — see module
    docstring). Kept because it's exactly the building block a future
    "meters react to the transition music" feature would need: decode
    once (already happening via _ensure_wav_audio()), compute a rolling
    amplitude envelope over that same PCM data, then sample the envelope
    by elapsed wall-clock time the same way get_overlay_frame() samples
    frames — same pattern, just feeding meter levels instead of a texture."""
    if not wav_path:
        return None
    try:
        import wave as _wave
        with _wave.open(wav_path, 'rb') as wf:
            frames = wf.getnframes()
            rate   = wf.getframerate()
            return (frames / float(rate)) if rate else None
    except Exception as e:
        print(f"[PACKAPUNCH] could not read audio duration: {e}")
        return None


def _build_meter_envelope(wav_path):
    """Pre-compute the meter envelope for the transition audio: an array of
    shape (n_steps, METER_BANDS) of 0..1 levels, one row per _ENV_HOP_S of
    audio. Done once per audio file (cached on its path + mtime) so the
    per-tick cost while the transition plays is a single array lookup.

    "bands" mode splits the music into METER_BANDS log-spaced frequency
    bands with an FFT; "level" mode uses overall loudness for every column.
    Either way each column is normalised against its OWN loud passages
    (98th percentile) and mapped over METER_DB_RANGE dB, because music's
    spectrum tilts heavily toward the bass — without that, the treble
    meters would barely twitch. Returns None if the file can't be read."""
    global _meter_env
    try:
        import wave as _wave
        import numpy as np
        mtime = os.path.getmtime(wav_path)
        if (_meter_env is not None and _meter_env[0] == wav_path
                and _meter_env[1] == mtime):
            return _meter_env
        with _wave.open(wav_path, 'rb') as wf:
            nch, sw, sr = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        if sw != 2 or sr <= 0:
            print(f"[PACKAPUNCH] meter envelope: unsupported WAV ({sw*8}-bit)")
            return None
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if nch > 1:
            x = x[: (len(x) // nch) * nch].reshape(-1, nch).mean(axis=1)
        hop = max(1, int(round(sr * _ENV_HOP_S)))
        win = _ENV_WIN
        if len(x) < win:
            x = np.pad(x, (0, win - len(x)))
        x = np.pad(x, (win // 2, win))                 # centre each window
        n = (len(x) - win) // hop
        hann = np.hanning(win).astype(np.float32)
        freqs = np.fft.rfftfreq(win, 1.0 / sr)
        if METER_MODE == "level":
            groups = [np.ones_like(freqs, dtype=bool)] * METER_BANDS
        else:
            hi = min(METER_BAND_HI_HZ, sr * 0.5 * 0.95)
            edges = np.geomspace(METER_BAND_LO_HZ, hi, METER_BANDS + 1)
            groups = [(freqs >= edges[b]) & (freqs < edges[b + 1])
                      for b in range(METER_BANDS)]
            for b in range(METER_BANDS):               # never an empty band
                if not groups[b].any():
                    groups[b][np.argmin(np.abs(freqs - edges[b]))] = True
        out = np.zeros((n, METER_BANDS), dtype=np.float32)
        view = np.lib.stride_tricks.sliding_window_view(x, win)[::hop][:n]
        CH = 256
        for a in range(0, n, CH):
            mag2 = np.abs(np.fft.rfft(view[a:a + CH] * hann, axis=1)) ** 2
            for b, m in enumerate(groups):
                out[a:a + CH, b] = np.sqrt(mag2[:, m].mean(axis=1))
        for b in range(METER_BANDS):
            ref = float(np.percentile(out[:, b], 98))
            if ref <= 1e-9:
                out[:, b] = 0.0
                continue
            db = 20.0 * np.log10(np.maximum(out[:, b], 1e-9) / ref)
            out[:, b] = np.clip(1.0 + db / METER_DB_RANGE, 0.0, 1.0)
        _meter_env = (wav_path, mtime, out, hop / float(sr))
        print(f"[PACKAPUNCH] meter envelope: {n} steps x {METER_BANDS} "
              f"({METER_MODE}), {n * hop / float(sr):.1f}s")
        return _meter_env
    except Exception as e:
        print(f"[PACKAPUNCH] meter envelope failed: {e}")
        return None


def audio_meter_levels():
    """Read by core/meters.py's timer on every tick. Returns None when the
    transition audio isn't playing (meters then show their channels as
    normal), or (rms[METER_BANDS], peak[METER_BANDS]) of 0..1 levels for
    the music at this instant. The envelope is sampled by wall-clock time
    since the audio cue was started — the same clock the video overlay
    uses — and goes back to None the moment the audio's length has
    elapsed, which is what hands the meters back to their channels."""
    global _meter_t0
    if _meter_t0 is None or _meter_env is None:
        return None
    env, hop_s = _meter_env[2], _meter_env[3]
    t = time.perf_counter() - _meter_t0 - METER_LATENCY_S
    i = int(t / hop_s) if t >= 0.0 else -1
    if i >= len(env):
        _meter_t0 = None
        return None
    if i < 0:
        z = [0.0] * METER_BANDS
        return z, list(z)
    lo = max(0, i - int(0.05 / hop_s))                 # peak = last ~50ms
    return env[i].tolist(), env[lo:i + 1].max(axis=0).tolist()


def _reset_debug_stats():
    global _dbg_redraws_total, _dbg_last_report_t, _dbg_last_report_n, \
           _dbg_last_frame_idx, _dbg_skipped_frames, _dbg_shown_frames, \
           _dbg_load_times
    _dbg_redraws_total  = 0
    _dbg_last_report_t  = 0.0
    _dbg_last_report_n  = 0
    _dbg_last_frame_idx = -1
    _dbg_skipped_frames = 0
    _dbg_shown_frames   = set()
    _dbg_load_times     = []


def _print_debug_summary(final_elapsed):
    n_shown  = len(_dbg_shown_frames)
    n_total  = len(_frame_paths)
    blocking = [ms for (_, ms, b) in _dbg_load_times if b]
    prefetch = [ms for (_, ms, b) in _dbg_load_times if not b]
    measured_fps = (n_shown / final_elapsed) if final_elapsed > 0 else 0.0
    print("[PACKAPUNCH][DEBUG] ---------------- transition summary ----------------")
    print(f"[PACKAPUNCH][DEBUG]  expected: {n_total} frames @ {_fps:.0f}fps -> {_duration_s:.2f}s")
    print(f"[PACKAPUNCH][DEBUG]  actual wall time elapsed: {final_elapsed:.2f}s")
    print(f"[PACKAPUNCH][DEBUG]  distinct frames actually shown: {n_shown}/{n_total}  "
          f"(effective ~{measured_fps:.1f}fps of unique content)")
    print(f"[PACKAPUNCH][DEBUG]  frames skipped over (never shown): {_dbg_skipped_frames}")
    print(f"[PACKAPUNCH][DEBUG]  total draw-callback redraws while active: {_dbg_redraws_total}")
    if blocking:
        print(f"[PACKAPUNCH][DEBUG]  ON-DEMAND loads (blocked the draw callback): "
              f"{len(blocking)}  avg {sum(blocking)/len(blocking):.1f}ms  "
              f"max {max(blocking):.1f}ms  (24fps budget is {1000.0/_fps:.1f}ms)")
    else:
        print("[PACKAPUNCH][DEBUG]  ON-DEMAND loads (blocked the draw callback): 0 "
              "(the look-ahead prefetch kept up)")
    if prefetch:
        print(f"[PACKAPUNCH][DEBUG]  pre-fetched loads (in the redraw timer): "
              f"{len(prefetch)}  avg {sum(prefetch)/len(prefetch):.1f}ms  "
              f"max {max(prefetch):.1f}ms")
    print("[PACKAPUNCH][DEBUG] -----------------------------------------------------")


# ---------------------------------------------------------------------------
# "Welcome once" - the transition (video + fanfare) plays on the very first
# click only. A small marker file records that it has happened; it lives in
# the user's Blender config area (outside the add-on folder, so reinstalling
# or updating the add-on does not reset it, and outside the OS temp dir, so
# cleanup tools do not wipe it). Delete the file to see the welcome again.
# ---------------------------------------------------------------------------
WELCOME_FILE_NAME = "hijacker_welcome.json"
TOAST_TEXT        = "You've already been welcomed."
TOAST_HOLD_S      = 2.2
TOAST_FADE_S      = 0.4
_toast_t0         = -1e9
_toast_timer_registered = False


def _welcome_file_path() -> str:
    try:
        user = bpy.utils.resource_path('USER')            # .../Blender/<version>
        base = os.path.dirname(os.path.normpath(user))    # .../Blender (all versions)
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".hijacker")
    return os.path.join(base, WELCOME_FILE_NAME)


def _read_welcome() -> dict:
    try:
        import json
        with open(_welcome_file_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {"welcomed": True}
    except FileNotFoundError:
        return {}
    except Exception:
        return {"welcomed": True}      # unreadable marker still counts as welcomed


def _write_welcome(data: dict):
    path = _welcome_file_path()
    try:
        import json
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.write("\n")
    except Exception as e:
        print(f"[PACKAPUNCH] could not save welcome marker: {e}")


def already_welcomed() -> bool:
    return bool(_read_welcome().get("welcomed"))


def _mark_welcomed():
    _write_welcome({"welcomed": True, "toast_shown": False,
                    "when": time.strftime("%Y-%m-%dT%H:%M:%S")})


def _toast_due() -> bool:
    """True exactly once: the second time the user switches INTO the
    packapunch skin. Records that it has been shown."""
    d = _read_welcome()
    if d.get("toast_shown"):
        return False
    d["toast_shown"] = True
    _write_welcome(d)
    return True


def show_toast():
    """Pop the 'already welcomed' message for a couple of seconds."""
    global _toast_t0, _toast_timer_registered
    _toast_t0 = time.perf_counter()
    _redraw_hud_areas()
    if not _toast_timer_registered:
        _toast_timer_registered = True
        bpy.app.timers.register(_toast_timer, first_interval=1.0 / 30.0)


def get_toast():
    """(text, alpha) while the toast is visible, else None."""
    t = time.perf_counter() - _toast_t0
    total = TOAST_FADE_S + TOAST_HOLD_S + TOAST_FADE_S
    if t < 0 or t >= total:
        return None
    if t < TOAST_FADE_S:
        a = t / TOAST_FADE_S
    elif t < TOAST_FADE_S + TOAST_HOLD_S:
        a = 1.0
    else:
        a = 1.0 - (t - TOAST_FADE_S - TOAST_HOLD_S) / TOAST_FADE_S
    return (TOAST_TEXT, max(0.0, min(1.0, a)))


def _toast_timer():
    global _toast_timer_registered
    _redraw_hud_areas()
    if get_toast() is None:
        _toast_timer_registered = False
        return None
    return 1.0 / 30.0


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------
def toggle():
    """Trigger the transition, alternating direction each press: swaps INTO
    the packapunch skin if currently on default (or anything else), or back
    OUT to default if already on packapunch.

    Only the way IN plays the full transition (video overlay + audio cue)
    — see start()'s play_video/play_audio docstrings for why. Reverting
    back to default is an instant skin swap with no fanfare: there's
    nothing to "reveal" on the way out, so replaying the PNG sequence
    (and the "PACK-A-PUNCH!" cue) on revert just looked/sounded like a
    glitch rather than a deliberate second transition.

    The full transition only ever plays ONCE per user (first click ever) -
    see "Welcome once" above; later clicks swap skins silently and show a
    a one-time toast on the second entry, then nothing."""
    entering = get_active_skin() != TARGET_SKIN
    target   = TARGET_SKIN if entering else DEFAULT_SKIN
    if not entering:
        # Going back before the cue has finished: cut the music the moment
        # the normal skin returns, and hand the desk meters back to their
        # channels (they follow the cue while it plays).
        global _meter_t0, _cue_handle
        _meter_t0 = None
        try:
            if _cue_handle is not None:
                _cue_handle.stop()
            _cue_handle = None
        except Exception as e:
            print(f"[PACKAPUNCH] could not stop the transition audio: {e}")
    if entering:
        if already_welcomed():
            # Welcome is a one-time thing: still swap the skin so the
            # button works, but no video / fanfare. The toast shows only on
            # the second entry ever, never again after that.
            start(target, play_audio=False, play_video=False)
            if _toast_due():
                show_toast()
            return
        _mark_welcomed()
    start(target, play_audio=entering, play_video=entering)


def start(target_skin: str = TARGET_SKIN, play_audio: bool = True, play_video: bool = True):
    """Begin the PackAPunch transition: swap the skin immediately, start the
    audio cue, and (if play_video) arm the wall-clock reference the draw
    callback reads via get_overlay_frame(). Re-entrant-safe — ignored if
    already running.

    Video and audio share a start time, not a duration — see the module
    docstring above. The overlay plays for exactly frame_count / VIDEO_FPS
    seconds and then stops (is_active() goes False, button re-arms); the
    audio subprocess plays independently to its own full length and is
    never truncated or waited on.

    play_audio: skips the audio cue for this run. toggle() sets this False
    when reverting back to the default skin — the "PACK-A-PUNCH!" fanfare
    makes sense firing once, going in; playing it again on the way back out
    just sounded like a glitch, not a deliberate second cue.

    play_video: skips the PNG-sequence overlay entirely for this run —
    just the immediate skin swap above, with is_active() never going True.
    toggle() sets this False on the same revert path as play_audio: the
    overlay's whole reason to exist is to fade out and reveal the skin
    already swapped underneath it, which only makes sense going IN. On
    the way back out there's nothing to reveal, so there's no reason to
    play the sequence (or wait through it) at all."""
    global _active, _start_wall, _frame_paths, _fps, \
           _duration_s, _fade_start_s, _target_skin, _redraw_timer_registered

    if _active:
        return

    from ui.mixer.texture_cache import set_skin_dir, clear_cache

    # Free anything left over from a previous run (e.g. Blender closed or
    # the addon reloaded mid-transition) before starting fresh — see
    # _clear_frame_cache()'s docstring.
    _clear_frame_cache()
    _target_skin = target_skin

    # Skin swap FIRST — see module docstring. Everything underneath the
    # overlay is already the destination skin for the whole transition.
    set_skin_dir(ASSETS_DIR, _target_skin)
    clear_cache()

    global _meter_t0, _cue_handle
    _meter_t0 = None
    if play_audio:
        wav = _ensure_wav_audio()
        if wav:
            # Envelope first (cached after the first run) so nothing is
            # computed between the audio starting and the meters reading it.
            have_env = _build_meter_envelope(wav) is not None
            if have_env:
                _meter_t0 = time.perf_counter()
            _cue_handle = play_oneshot(wav)   # plays to its own full length,
                                               # decoupled from the overlay below;
                                               # kept only so going back can stop it

    if not play_video:
        _frame_paths = []
        _active      = False
        print(f"[PACKAPUNCH] instant switch -> skin '{_target_skin}' (no video)")
        return

    # Frame the view on the mixing desk now, while nothing of it can be seen:
    # the overlay's first frame is fully opaque, so by the time it fades out
    # the reveal is already centred on the desk instead of the button.
    try:
        from ui.mixer.mixer_hud import frame_on_desk
        frame_on_desk()
    except Exception as e:
        print(f"[PACKAPUNCH] could not frame the view on the desk: {e}")

    _frame_paths = _scan_frames()
    _fps        = VIDEO_FPS
    _duration_s = ((len(_frame_paths) / _fps) + HOLD_LAST_S) if _frame_paths else FALLBACK_DURATION_S
    _fade_start_s = max(0.0, _duration_s - FADE_S)

    _start_wall = time.perf_counter()
    _active     = True

    if DEBUG:
        _reset_debug_stats()

    if not _redraw_timer_registered:
        bpy.app.timers.register(_redraw_timer, first_interval=0.0)
        _redraw_timer_registered = True

    print(f"[PACKAPUNCH] started -> skin '{_target_skin}' | "
          f"{len(_frame_paths)} frame(s) @ {_fps:.0f}fps ({_duration_s:.2f}s)")


def get_overlay_frame():
    """Called once per HUD redraw. Returns (texture_or_None, progress 0..1,
    alpha 0..1) while the transition is playing, or None once it has
    finished (or was never active). texture_or_None is None whenever no
    frame art exists yet for this index — the caller should just skip
    drawing the overlay that tick, the skin swap underneath already
    happened at start(). alpha is 1.0 until the final FADE_S seconds,
    then eases down to 0 so the overlay dissolves to reveal the skin; the
    caller multiplies it into the frame's own alpha."""
    global _active, _dbg_redraws_total, _dbg_last_report_t, \
           _dbg_last_report_n, _dbg_last_frame_idx, _dbg_skipped_frames
    if not _active:
        return None

    elapsed = time.perf_counter() - _start_wall
    if elapsed >= _duration_s:
        _active = False
        if DEBUG:
            _print_debug_summary(elapsed)
        _clear_frame_cache()  # free the whole run's cached frames now, not
                               # just the rolling window — see docstring
        print(f"[PACKAPUNCH] transition finished ({_duration_s:.2f}s)")
        return None

    progress  = elapsed / _duration_s if _duration_s > 0 else 1.0
    frame_idx = int(elapsed * _fps)
    if _frame_paths:
        frame_idx = min(frame_idx, len(_frame_paths) - 1)

    # Fade-out over the last FADE_S seconds (continuous, not per-frame
    # steps), smoothstepped so it eases in and out instead of starting and
    # ending abruptly.
    alpha = 1.0
    if FADE_S > 0 and _frame_paths:
        f = (elapsed - _fade_start_s) / FADE_S
        if f > 0.0:
            f = min(f, 1.0)
            alpha = 1.0 - f * f * (3.0 - 2.0 * f)

    if DEBUG:
        _dbg_redraws_total += 1
        _dbg_shown_frames.add(frame_idx)
        if _dbg_last_frame_idx >= 0 and frame_idx > _dbg_last_frame_idx + 1:
            _dbg_skipped_frames += frame_idx - _dbg_last_frame_idx - 1
        _dbg_last_frame_idx = frame_idx
        # Once a second: how many redraws actually happened in the last
        # second (target ~30) — if this comes back far below 30, the draw
        # callback itself is what's taking too long per call (almost
        # always the ON-DEMAND load warning above firing repeatedly), not
        # a scheduling issue elsewhere.
        if elapsed - _dbg_last_report_t >= 1.0:
            redraws_this_window = _dbg_redraws_total - _dbg_last_report_n
            print(f"[PACKAPUNCH][DEBUG] t={elapsed:5.2f}s  "
                  f"expected_frame={frame_idx}/{len(_frame_paths)}  "
                  f"redraws_last~1s={redraws_this_window} (target ~30)  "
                  f"skipped_so_far={_dbg_skipped_frames}")
            _dbg_last_report_t = elapsed
            _dbg_last_report_n = _dbg_redraws_total

    tex = _get_frame_texture(frame_idx)
    return (tex, progress, alpha)


def _redraw_timer():
    """Keeps the HUD area redrawing at ~30Hz while a transition is playing,
    so the PNG sequence actually animates instead of only updating on mouse
    move (mirrors the always-on pattern core/meters.py already uses for VU
    meters, just at a higher rate for smoother playback). Self-stops by
    returning None the first tick after the transition ends."""
    global _redraw_timer_registered
    if not _active:
        _redraw_timer_registered = False
        return None

    # Opportunistically warm the cache a couple frames ahead of the
    # current playhead. This timer ticks at 30Hz, a bit faster than the
    # 24fps playback rate, which gives just enough slack for a frame's
    # load+upload to often finish here instead of stalling the draw
    # callback the moment it's actually needed.
    elapsed = time.perf_counter() - _start_wall
    current_idx = int(elapsed * _fps)
    for lookahead in range(1, _PRELOAD_AHEAD + 1):
        _prefetch_frame(current_idx + lookahead)

    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "NODE_EDITOR":
                    area.tag_redraw()
    except Exception:
        pass
    return 1.0 / 30.0


# ---------------------------------------------------------------------------
# Registration — mirrors core/perf_monitor.py's register()/unregister()/reset()
# convention so Loader.py can wire this in identically.
# ---------------------------------------------------------------------------
def reset():
    """Called when the HUD is (re)opened — clears any stuck state left over
    from a previous session (e.g. Blender closed mid-transition)."""
    global _active, _meter_t0, _btn_held, _btn_pending, _redraw_timer_registered, _cue_handle
    _cue_handle = None
    _active = False
    _btn_pending = False
    _redraw_timer_registered = False   # Blender drops timers on file load; don't trust the old flag
    _meter_t0 = None
    _btn_held = False
    _btn_tex.clear()          # re-read the button art each time the HUD opens
    _btn_uv.clear()
    _btn_px.clear()
    _clear_frame_cache()


def register():
    pass  # nothing to register up front — the redraw timer self-registers
          # in start() and self-unregisters in _redraw_timer()


def unregister():
    global _active, _meter_t0
    _active = False
    _meter_t0 = None
    _clear_frame_cache()
