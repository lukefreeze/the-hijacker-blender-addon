# =============================================================================
# core/rack_screen_sequence.py
# Generic, reusable looping image-sequence player for rack "screens" — small,
# pre-cropped PNG sequences that sit behind a rack's live UI (EQ curve,
# meters, band dots, etc.) and should just keep animating for as long as
# that rack's themed skin is active. First used by rack_eq.py's PackAPunch
# vortex background; built generic from the start since at least two more
# racks will need the same thing — each rack just calls get_sequence_frame()
# with its own seq_id/folder/fps, no copy-pasted player logic per rack.
#
# DESIGN NOTES
# ------------
# Fully preloaded, not a rolling cache like core/packapunch.py.
#   packapunch.py's one-shot transition streams 1920x1080 frames and needs a
#   rolling eviction window so it doesn't accumulate ~2.5GB of VRAM/RAM over
#   a single run (see that module's docstring for the full story). Rack-screen
#   sequences are a different shape entirely: small — already cropped down to
#   just the on-screen content before they ever reach this module, see the
#   scale+matte+crop batch pipeline that produced
#   ui/assets/skins/working/parametricEQ/packApunch/Smaller/ — and looped
#   forever rather than played once. A couple hundred frames at roughly
#   1261x354 RGBA is only a few MB total, so the cheap thing to do is load
#   every frame once on first use and keep all of them resident. No
#   re-decoding cost on any later loop, ever, and no eviction bookkeeping.
#
# No dedicated redraw timer.
#   core/meters.py already runs a 20Hz (METER_POLL_INTERVAL) redraw timer for
#   as long as the mixer HUD is visible — that's what keeps the live meters
#   and this EQ rack's own spectrum analyser animating. Rack screens piggyback
#   on that same redraw cadence instead of registering a second timer:
#   get_sequence_frame() just recomputes "which frame does wall-clock time
#   land on" fresh on every call, the same self-correcting approach
#   core/packapunch.py uses for its own frame-index math.
#
# Caller owns the skin check and the draw call.
#   This module only answers "here is the texture for right now" (or None if
#   the sequence hasn't loaded / isn't found) — it has no opinion on *when* a
#   rack should be showing a sequence instead of its normal background, or
#   where on screen it should land. That stays in the rack file itself, right
#   next to that rack's own position constants (see EQ_SCREEN_SEQ_* in
#   rack_eq.py) so each rack's tuning lives in one obvious place.
# =============================================================================
import os
import glob
import time

import bpy
import gpu

# Set True to print, every 2s per sequence, how often it is being redrawn and how
# many frame changes per second actually reach the screen (target = its fps).
RACKSEQ_DEBUG = False

_sequences = {}
# seq_id -> {
#     "dir":      str                         — frames_dir this entry was loaded from
#     "paths":    [str, ...]                  — sorted frame file paths
#     "images":   [bpy.types.Image|None, ...] — kept alive so we can free them later
#     "textures": [gpu.types.GPUTexture|None, ...]
#     "fps":      float
#     "start":    float                       — time.perf_counter() reference for looping
# }


def _scan(frames_dir):
    """Same case-insensitive-filesystem dedup fix as core/packapunch.py's
    _scan_frames() — on Windows, globbing both *.png and *.PNG matches the
    same files twice (case-insensitive filesystem), silently doubling the
    frame list. Dedupe by the normalized absolute path so a genuinely
    case-sensitive filesystem (Linux) still works correctly too."""
    if not os.path.isdir(frames_dir):
        return []
    seen, paths = set(), []
    for p in (glob.glob(os.path.join(frames_dir, "*.png")) +
              glob.glob(os.path.join(frames_dir, "*.PNG"))):
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            paths.append(p)
    return sorted(paths)


def _load_all(frames_dir):
    """Load + GPU-upload every frame in frames_dir up front. Same per-frame
    recipe as core/packapunch.py's _load_frame() (including the Blender 5.x
    Non-Color colorspace fix, needed for the same reason — see that
    module — so a pre-rendered frame doesn't come out gamma-shifted)."""
    paths = _scan(frames_dir)
    images, textures = [], []
    for p in paths:
        try:
            img = bpy.data.images.load(p, check_existing=True)
            if bpy.app.version >= (5, 0, 0):
                img.colorspace_settings.name = 'Non-Color'
            tex = gpu.texture.from_image(img)
        except Exception as e:
            print(f"[RACKSEQ] failed to load frame {p}: {e}")
            img, tex = None, None
        images.append(img)
        textures.append(tex)
    return paths, images, textures


def get_sequence_frame(seq_id, frames_dir, fps=24.0, speed=None, phase_key=None):
    """Return the GPUTexture for whichever frame of this looping sequence
    wall-clock time currently lands on, loading + caching the whole sequence
    the first time seq_id is requested (or again if frames_dir changes).
    Returns None if the folder has no frames, or every frame in it failed
    to load — callers should fall back to their normal background in that
    case rather than assume a texture is always available.

    speed / phase_key (optional) — variable-speed playback. With speed=None
    (the default) the frame comes straight from wall-clock time, exactly as
    before. With a speed (1.0 = normal, 0.0 = frozen, in between = slow motion)
    the sequence keeps its own running position per phase_key, so the speed can
    change smoothly from call to call without the picture jumping — and several
    racks can share one set of loaded frames while each keeps its own position
    and its own speed (phase_key = e.g. the rack's index)."""
    entry = _sequences.get(seq_id)
    if entry is None or entry["dir"] != frames_dir:
        paths, images, textures = _load_all(frames_dir)
        if not paths:
            print(f"[RACKSEQ] '{seq_id}': no frames found in {frames_dir}")
            return None
        entry = {
            "dir": frames_dir, "paths": paths,
            "images": images, "textures": textures,
            "fps": fps, "start": time.perf_counter(),
            "phases": {},
        }
        _sequences[seq_id] = entry
        print(f"[RACKSEQ] '{seq_id}': loaded {len(paths)} frames from {frames_dir}")

    textures = entry["textures"]
    n = len(textures)
    if n == 0:
        return None
    now = time.perf_counter()
    entry["fps"] = fps          # always honour the fps the caller passes right now
    if speed is None:
        elapsed = now - entry["start"]
        idx = int(elapsed * entry["fps"]) % n
    else:
        ph = entry["phases"].setdefault(phase_key, [0.0, now])
        dt = min(0.25, max(0.0, now - ph[1]))      # clamp: no jump after a long pause
        ph[0] = (ph[0] + dt * entry["fps"] * max(0.0, speed)) % n
        ph[1] = now
        idx = int(ph[0]) % n
    if RACKSEQ_DEBUG:
        st = entry.setdefault("dbg", [now, 0, 0, idx])
        st[1] += 1
        if idx != st[3]:
            st[2] += 1
            st[3] = idx
        if now - st[0] >= 2.0:
            span = now - st[0]
            print(f"[RACKSEQ] '{seq_id}': {st[1] / span:.1f} redraws/s, "
                  f"{st[2] / span:.1f} frame changes/s (target {fps:.1f}fps, {n} frames)")
            st[0], st[1], st[2] = now, 0, 0
    return textures[idx]


def clear_sequence(seq_id):
    """Free every cached frame for one sequence (addon unregister, or to
    force a reload if a rack's frames changed on disk after the first load)."""
    entry = _sequences.pop(seq_id, None)
    if not entry:
        return
    for img in entry["images"]:
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass


def clear_all():
    """Free every cached sequence — call this from the addon's unregister()
    alongside core.packapunch's own cache clear, so nothing lingers in
    bpy.data.images after the addon is disabled/reloaded."""
    for seq_id in list(_sequences.keys()):
        clear_sequence(seq_id)
