"""
core/audio_export.py
====================
Render one VSE channel to a WAV file for the AI racks (Demucs, VoiceFixer,
Whisper) using Blender's own Render Audio (sound.mixdown).

Hardened against the ways that call can come back with no file:
  * the file may appear a moment after the operator returns, so wait for it;
  * a source strip that starts before the scene's start frame (negative
    frames) or outside the scene range can leave nothing to render, so retry
    with the scene range temporarily widened to the strip's own span;
  * strip mutes and the scene range are always put back, even on failure;
  * every failed attempt prints what Blender returned and the ranges involved,
    so a failure can be diagnosed from the console.
"""

import os
import time


def _wait_for_file(path, timeout=10.0):
    """True once path exists with a size that has stopped growing."""
    t_end = time.time() + timeout
    last = -1
    while time.time() < t_end:
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > 44 and size == last:
                return True
            last = size
        time.sleep(0.25)
    return os.path.exists(path) and os.path.getsize(path) > 44


def _set_range(scene, start, end):
    # keep start <= end at every step so Blender does not shove the other end
    if start > scene.frame_end:
        scene.frame_end = end
        scene.frame_start = start
    else:
        scene.frame_start = start
        scene.frame_end = end


def mixdown_channel(scene, strips, src_ch, out_path, strip=None, tag="AI", limit_to_strip=False):
    """Render channel `src_ch` (1-based) to out_path as PCM WAV.

    limit_to_strip: render only the span of `strip` on the timeline (its trimmed
    in/out points), not the whole scene range.

    Returns the timeline frame the file's first sample corresponds to.
    Raises RuntimeError (with details printed) if no audio file results.
    """
    import bpy

    original_mutes = {s.name: s.mute for s in strips if hasattr(s, "mute")}
    saved_range = (scene.frame_start, scene.frame_end)

    def _attempt(label, start=None, end=None):
        used_start = scene.frame_start
        try:
            if start is not None:
                _set_range(scene, start, end)
                used_start = start
            for s in strips:
                if hasattr(s, "mute"):
                    s.mute = (s.channel != src_ch)
            ret = bpy.ops.sound.mixdown(
                filepath=out_path, check_existing=False,
                relative_path=False, codec="PCM", container="WAV")
            ok = _wait_for_file(out_path)
            print(f"[{tag}] mixdown ({label}): returned {set(ret)}, "
                  f"range {scene.frame_start}-{scene.frame_end}, "
                  f"file {'ok' if ok else 'MISSING'}")
            return ok, used_start
        finally:
            for s in strips:
                if hasattr(s, "mute") and s.name in original_mutes:
                    s.mute = original_mutes[s.name]
            if start is not None:
                _set_range(scene, *saved_range)

    if limit_to_strip and strip is not None:
        s0 = int(strip.frame_final_start)
        e0 = int(strip.frame_final_start + strip.frame_final_duration) - 1
        if e0 > s0:
            ok, used = _attempt(f"strip span {s0}-{e0}", s0, e0)
            if ok:
                return used
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass

    ok, used = _attempt("scene range")
    if ok:
        return used

    if strip is not None:
        s0 = int(strip.frame_final_start)
        e0 = int(strip.frame_final_start + strip.frame_final_duration) - 1
        if e0 > s0:
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            ok, used = _attempt(f"strip range {s0}-{e0}", s0, e0)
            if ok:
                return used

    raise RuntimeError("mixdown produced no file "
                       f"(scene range {saved_range[0]}-{saved_range[1]}; see the lines above)")
