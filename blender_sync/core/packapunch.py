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
#   VSE mix. This is a short one-off transition sting, not part of the
#   timeline mix. The tradeoff: subprocess launch has some tens-of-ms of
#   startup latency before audio is actually audible, so the audio/video
#   sync here is "good enough for a fun transition", not sample-accurate.
#   Sample-accurate sync would mean routing this through the C++ PortAudio
#   engine instead — deliberately out of scope for this first pass.
#
# Frames are optional.
#   Luke's real transition art (a PNG sequence exported from his video
#   edit) isn't in the repo yet. If ui/assets/packapunch/frames/ is empty
#   or missing, the transition still runs in full — skin swap + audio cue
#   — just without an image overlay on top. Drop numbered PNGs in that
#   folder later and they're picked up automatically on the next trigger,
#   no code changes needed.
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
FRAMES_DIR   = os.path.join(ADDON_DIR, "ui", "assets", "packapunch", "frames")
AUDIO_SRC    = os.path.join(ADDON_DIR, "ui", "PackAPunch.mp3")
_WAV_CACHE   = os.path.join(tempfile.gettempdir(), "hijacker_packapunch_cache.wav")

TARGET_SKIN  = "packapunch"
DEFAULT_SKIN = "default"

DEFAULT_FPS          = 24.0
# Used only when no frame sequence exists yet, so an audio-only transition
# still has a sensible on-screen duration before the button re-arms.
FALLBACK_DURATION_S  = 3.0

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_active          = False
_start_wall       = 0.0
_frame_paths      = []      # sorted absolute PNG paths for this run
_frame_tex_cache  = {}       # frame index -> gpu.types.GPUTexture, rebuilt per run
_fps              = DEFAULT_FPS
_duration_s       = FALLBACK_DURATION_S
_target_skin      = TARGET_SKIN
_redraw_timer_registered = False


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
    if not os.path.isdir(FRAMES_DIR):
        return []
    return sorted(
        glob.glob(os.path.join(FRAMES_DIR, "*.png")) +
        glob.glob(os.path.join(FRAMES_DIR, "*.PNG"))
    )


def _get_frame_texture(idx):
    if idx in _frame_tex_cache:
        return _frame_tex_cache[idx]
    if idx < 0 or idx >= len(_frame_paths):
        return None
    try:
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
        return tex
    except Exception as e:
        print(f"[PACKAPUNCH] frame {idx} load failed: {e}")
        _frame_tex_cache[idx] = None
        return None


# ---------------------------------------------------------------------------
# Audio — decode once to a cached WAV so play_oneshot() (WAV-only on
# Windows, via PowerShell's Media.SoundPlayer) can play it regardless of
# what format the source cue ships as.
# ---------------------------------------------------------------------------
def _ensure_wav_audio():
    if not os.path.exists(AUDIO_SRC):
        print(f"[PACKAPUNCH] audio cue not found: {AUDIO_SRC}")
        return None
    if os.path.exists(_WAV_CACHE):
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


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------
def toggle():
    """Trigger the transition, alternating direction each press: swaps INTO
    the packapunch skin if currently on default (or anything else), or back
    OUT to default if already on packapunch. Same transition animation +
    audio cue either way — see module docstring for why."""
    target = DEFAULT_SKIN if get_active_skin() == TARGET_SKIN else TARGET_SKIN
    start(target)


def start(target_skin: str = TARGET_SKIN, fps: float = DEFAULT_FPS):
    """Begin the PackAPunch transition: swap the skin immediately, start the
    audio cue, and arm the wall-clock reference the draw callback reads via
    get_overlay_frame(). Re-entrant-safe — ignored if already running."""
    global _active, _start_wall, _frame_paths, _frame_tex_cache, _fps, \
           _duration_s, _target_skin, _redraw_timer_registered

    if _active:
        return

    from ui.mixer.texture_cache import set_skin_dir, clear_cache

    _frame_paths     = _scan_frames()
    _frame_tex_cache = {}
    _fps             = max(1.0, fps)
    _target_skin     = target_skin
    _duration_s      = (len(_frame_paths) / _fps) if _frame_paths else FALLBACK_DURATION_S

    # Skin swap FIRST — see module docstring. Everything underneath the
    # overlay is already the destination skin for the whole transition.
    set_skin_dir(ASSETS_DIR, _target_skin)
    clear_cache()

    wav = _ensure_wav_audio()
    if wav:
        play_oneshot(wav)

    _start_wall = time.perf_counter()
    _active     = True

    if not _redraw_timer_registered:
        bpy.app.timers.register(_redraw_timer, first_interval=0.0)
        _redraw_timer_registered = True

    print(f"[PACKAPUNCH] started -> skin '{_target_skin}' | "
          f"{len(_frame_paths)} frame(s) @ {_fps:.0f}fps ({_duration_s:.2f}s)")


def get_overlay_frame():
    """Called once per HUD redraw. Returns (texture_or_None, progress 0..1)
    while the transition is playing, or None once it has finished (or was
    never active). texture_or_None is None whenever no frame art exists yet
    for this index — the caller should just skip drawing the overlay that
    tick, the skin swap underneath already happened at start()."""
    global _active
    if not _active:
        return None

    elapsed = time.perf_counter() - _start_wall
    if elapsed >= _duration_s:
        _active = False
        print(f"[PACKAPUNCH] transition finished ({_duration_s:.2f}s)")
        return None

    progress  = elapsed / _duration_s if _duration_s > 0 else 1.0
    frame_idx = int(elapsed * _fps)
    tex       = _get_frame_texture(frame_idx)
    return (tex, progress)


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
    global _active
    _active = False


def register():
    pass  # nothing to register up front — the redraw timer self-registers
          # in start() and self-unregisters in _redraw_timer()


def unregister():
    global _active
    _active = False
