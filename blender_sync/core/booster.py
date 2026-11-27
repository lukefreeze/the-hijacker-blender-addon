# =============================================================================
# core/booster.py
# THE BOOSTER!! backend — export/boost/reimport.
#
# Called from Racks.py handle_click when the APPLY BOOST button is clicked.
# Runs in a background thread, same pattern as the other AI/processing racks.
#
# Steps:
#   1. Extract channel audio (32-bit float, nothing is clipped or rounded)
#   2. Apply gain + look-ahead limiter in-process (core/booster_dsp.py, NumPy)
#   3. Save boosted 32-bit float WAV as <original>_boosted_+NdB.wav next to the original
#   4. Place new strip on next free VSE channel, strip.volume = 1.0
#   5. Mute original strip so new louder one plays immediately
# =============================================================================

import os
import threading
import tempfile

import bpy

from core import vse_compat as _vse

# ---------------------------------------------------------------------------
# Active jobs
# ---------------------------------------------------------------------------
_active_jobs  = {}   # rack_idx -> Thread
_cancel_flags = {}   # rack_idx -> bool
_pending_finish = {} # rack_idx -> dict


def is_processing(rack_idx):
    t = _active_jobs.get(rack_idx)
    return t is not None and t.is_alive()


def _dsp_db(lin):
    import math
    return 20.0 * math.log10(lin) if lin > 1e-9 else -180.0


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------
def _strip_gain_envelope(strip, vis_start, vis_end, fps, n):
    """Per-sample gain [n, 1] for a strip's volume (static or keyframed)."""
    import numpy as np
    try:
        vol = float(strip.volume)
    except Exception:
        return np.float32(1.0)
    try:
        from core import strip_volume as _sv
        if _sv.is_animated(strip) and n > 1:
            f0, f1 = vis_start * fps, vis_end * fps
            pts = _sv.volume_polyline(strip, f0, f1)
            if pts and len(pts) >= 2:
                xs = np.array([p[0] for p in pts], dtype=np.float64)
                ys = np.array([p[1] for p in pts], dtype=np.float64)
                fr = f0 + (np.arange(n, dtype=np.float64) / n) * (f1 - f0)
                return np.interp(fr, xs, ys).astype(np.float32)[:, None]
    except Exception as e:
        print(f"[BOOSTER] volume envelope failed for '{strip.name}': {e}")
    return np.float32(vol)


def _extract_channel_audio(channel_idx):
    """Mix all audio on a VSE channel into one float32 array using aud.
    Returns (data[n, channels], sample_rate). Nothing is clipped or rounded to
    16-bit here - the booster must see the exact signal."""
    import aud
    import numpy as np

    scene = bpy.context.scene
    if not scene or not scene.sequence_editor:
        raise RuntimeError("No scene or sequence editor")

    fps       = scene.render.fps / scene.render.fps_base
    seq_start = scene.frame_start / fps
    seq_end   = scene.frame_end   / fps

    strips = sorted(
        [s for s in _vse.get_all_strips(scene.sequence_editor)
         if s.type == "SOUND" and s.sound
         and (s.channel - 1) == channel_idx],
        key=lambda s: (s.frame_final_start
                       if hasattr(s, 'frame_final_start') else s.frame_start)
    )
    if not strips:
        raise RuntimeError(f"No audio strips on channel {channel_idx + 1}")

    first_path = bpy.path.abspath(strips[0].sound.filepath)
    spec       = aud.Sound.file(first_path).specs
    TARGET_SR  = int(spec[0])
    TARGET_NCH = min(int(spec[1]), 2)

    total_dur_s   = seq_end - seq_start
    total_samples = max(1, int(total_dur_s * TARGET_SR))
    audio_out     = np.zeros((total_samples, TARGET_NCH), dtype=np.float32)

    for strip in strips:
        try:
            filepath       = bpy.path.abspath(strip.sound.filepath)
            strip_tl_start = (strip.frame_final_start / fps
                              if hasattr(strip, 'frame_final_start')
                              else strip.frame_start / fps)
            strip_tl_end   = strip.frame_final_end / fps
            vis_start      = max(strip_tl_start, seq_start)
            vis_end        = min(strip_tl_end,   seq_end)
            if vis_end <= vis_start:
                continue

            file_offset = getattr(strip, 'frame_offset_start', 0) / fps
            src_start   = file_offset + (vis_start - strip_tl_start)
            src_end     = src_start + (vis_end - vis_start)

            snd  = aud.Sound.file(filepath).limit(src_start, src_end)
            snd  = snd.resample(TARGET_SR, False).rechannel(TARGET_NCH)
            data = np.array(snd.data(), dtype=np.float32)

            if data.ndim == 1:
                data = data.reshape(-1, 1)
                if TARGET_NCH == 2:
                    data = np.repeat(data, 2, axis=1)

            # Bake in the clip's own volume (including the desk fader/gain, which
            # live in strip.volume, and any volume keyframes) so the boost is
            # applied to what you actually hear. Not clipped: a clip pushed past
            # 1.0 can be driven into the limiter - or past it with LIMIT: OFF.
            data = data * _strip_gain_envelope(strip, vis_start, vis_end, fps, len(data))

            buf_start = int((vis_start - seq_start) * TARGET_SR)
            buf_end   = min(buf_start + len(data), total_samples)
            data      = data[:buf_end - buf_start]
            audio_out[buf_start:buf_end] += data

        except Exception as e:
            print(f"[BOOSTER] strip '{strip.name}' extract failed: {e}")

    print(f"[BOOSTER] extracted ch{channel_idx+1}: "
          f"{len(audio_out)} frames @ {TARGET_SR}Hz {TARGET_NCH}ch")
    return audio_out, TARGET_SR


# ---------------------------------------------------------------------------
# VSE output placement
# ---------------------------------------------------------------------------
def _place_output_in_vse(scene, ch_idx, rack_idx, output_wav, rack, strip_volume=1.0):
    """Place boosted WAV on next free VSE channel, mute original."""
    try:
        if not scene.sequence_editor:
            scene.sequence_editor_create()
        seq = scene.sequence_editor

        fps = scene.render.fps / scene.render.fps_base

        orig_strips = sorted(
            [s for s in _vse.get_all_strips(seq)
             if s.type == "SOUND" and s.sound
             and (s.channel - 1) == ch_idx],
            key=lambda s: (s.frame_final_start
                           if hasattr(s, 'frame_final_start') else s.frame_start)
        )
        if not orig_strips:
            print(f"[BOOSTER] no original strips on ch{ch_idx+1}")
            return

        first_frame = min(
            s.frame_final_start if hasattr(s, 'frame_final_start') else s.frame_start
            for s in orig_strips)
        last_frame  = max(s.frame_final_end for s in orig_strips)

        # Find target channel — use rack's p2 setting (0 = auto-next-free)
        used = {s.channel for s in _vse.get_all_strips(seq)}
        requested = int(getattr(rack, 'p2', 0))
        if requested > 0 and requested not in used:
            target_ch = requested
        else:
            # Auto: find next free channel above the source
            target_ch = ch_idx + 2
            while target_ch in used:
                target_ch += 1

        new_strip = _vse.get_strips_collection(seq).new_sound(
            name        = f"BOOSTED_ch{ch_idx+1}_r{rack_idx}",
            filepath    = output_wav,
            channel     = target_ch,
            frame_start = int(first_frame),
        )
        new_strip.frame_final_end = int(last_frame)
        new_strip.volume          = float(strip_volume)

        # Mute originals
        for s in orig_strips:
            s.mute = True

        # Update engine handles
        src_idx = ch_idx
        out_idx = target_ch - 1
        tracks  = getattr(scene, "pb_sync_tracks", [])
        if src_idx < len(tracks):
            tracks[src_idx].mute = True
        if out_idx < len(tracks):
            tracks[out_idx].mute = False
        try:
            from core.audio import _pb_engine_update_volume
            _pb_engine_update_volume(src_idx)
            _pb_engine_update_volume(out_idx)
        except Exception as e:
            print(f"[BOOSTER] volume update error: {e}")

        print(f"[BOOSTER] placed boosted strip on VSE ch{target_ch}, vol={float(strip_volume):.2f}")
        print(f"[BOOSTER] original ch{ch_idx+1} muted — mute/unmute to A/B compare")

        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ('SEQUENCE_EDITOR', 'NODE_EDITOR'):
                    area.tag_redraw()

    except Exception as e:
        print(f"[BOOSTER] _place_output_in_vse error: {e}")
        import traceback; traceback.print_exc()


# ---------------------------------------------------------------------------
# Finish / timer
# ---------------------------------------------------------------------------
def _finish(rack_idx, scene_name, status, output_wav=None,
            ch_idx=None, error_msg=None, strip_volume=1.0):
    _pending_finish[rack_idx] = {
        'status':     status,
        'output_wav': output_wav,
        'ch_idx':     ch_idx,
        'error_msg':  error_msg,
        'scene_name': scene_name,
        'strip_volume': strip_volume,
    }


def _redraw_timer():
    for rack_idx, info in list(_pending_finish.items()):
        del _pending_finish[rack_idx]
        _apply_finish(rack_idx, info)

    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'NODE_EDITOR':
                    area.tag_redraw()
    except Exception:
        pass

    any_active = (any(t.is_alive() for t in _active_jobs.values()) or
                  bool(_pending_finish))
    return 0.25 if any_active else None


def _apply_finish(rack_idx, info):
    try:
        scene = bpy.data.scenes.get(info['scene_name'])
        if not scene:
            return

        racks = getattr(scene, "pb_racks", [])
        if rack_idx >= len(racks):
            return

        rack           = racks[rack_idx]
        rack.ai_status = info['status']

        if info['status'] == 'ERROR':
            print(f"[BOOSTER] rack {rack_idx} ERROR: {info.get('error_msg')}")
        elif info['status'] == 'DONE':
            output_wav = info.get('output_wav')
            ch_idx     = info.get('ch_idx')
            if output_wav and ch_idx is not None:
                _place_output_in_vse(scene, ch_idx, rack_idx, output_wav, rack,
                                     info.get('strip_volume', 1.0))
    except Exception as e:
        print(f"[BOOSTER] _apply_finish error: {e}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def process_booster(rack_idx, context):
    """Launch BOOSTER processing in a background thread."""
    if is_processing(rack_idx):
        print(f"[BOOSTER] rack {rack_idx} already processing")
        return

    scene = context.scene
    if not scene:
        return

    racks = getattr(scene, "pb_racks", [])
    if rack_idx >= len(racks):
        return

    rack = racks[rack_idx]

    try:
        import Racks as _rk
        assigned = list(_rk.get_rack_channels(rack))
    except Exception:
        assigned = []

    if not assigned:
        rack.ai_status = "NO_CHANNEL"
        print("[BOOSTER] ERROR: no channel assigned")
        return

    ch_idx     = assigned[0]
    boost_norm = float(getattr(rack, 'p0', 0.30))
    limiter_on = float(getattr(rack, 'p1', 1.0)) > 0.5
    boost_db   = boost_norm * 40.0

    rack.ai_status        = "PROCESSING"
    _cancel_flags[rack_idx] = False

    tmp_dir   = tempfile.gettempdir()

    # Build output path alongside original file
    try:
        seq = scene.sequence_editor
        orig_strips = [s for s in _vse.get_all_strips(seq)
                       if s.type == "SOUND" and s.sound
                       and (s.channel - 1) == ch_idx]
        if orig_strips:
            orig_path = bpy.path.abspath(orig_strips[0].sound.filepath)
            base, ext = os.path.splitext(orig_path)
            db_tag    = f"+{boost_db:.0f}dB" + ("" if limiter_on else "_raw")
            out_ext   = ext if ext.lower() == '.wav' else '.wav'
            output_wav = f"{base}_boosted_{db_tag}{out_ext}"
            # Never reuse a name: Blender keeps one cached sound (and timeline
            # waveform) per file path, so overwriting an earlier boost leaves the
            # old waveform on screen even though the audio has changed.
            n = 2
            while os.path.exists(output_wav):
                output_wav = f"{base}_boosted_{db_tag}_{n}{out_ext}"
                n += 1
        else:
            output_wav = os.path.join(tmp_dir, f"pb_boost_out_{rack_idx}.wav")
    except Exception:
        output_wav = os.path.join(tmp_dir, f"pb_boost_out_{rack_idx}.wav")

    scene_name = scene.name

    def _worker():
        try:
            from core import booster_dsp as _dsp
            import numpy as np

            # Step 1: extract (float32, unclipped)
            print(f"[BOOSTER] rack {rack_idx}: extracting ch{ch_idx+1}...")
            try:
                audio, sr = _extract_channel_audio(ch_idx)
            except Exception as e:
                _finish(rack_idx, scene_name, "ERROR",
                        error_msg=f"Audio extraction failed: {e}")
                return

            if _cancel_flags.get(rack_idx):
                _finish(rack_idx, scene_name, "READY"); return

            # Step 2: gain + limiter
            in_peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            print(f"[BOOSTER] rack {rack_idx}: boost={boost_db:.1f}dB "
                  f"limiter={'on' if limiter_on else 'off'} "
                  f"input peak={_dsp_db(in_peak):.1f}dBFS")
            try:
                out = _dsp.boost_and_limit(audio, sr, boost_db, limiter=limiter_on)
            except Exception as e:
                _finish(rack_idx, scene_name, "ERROR",
                        error_msg=f"Boost failed: {e}")
                return
            out_peak = float(np.max(np.abs(out))) if out.size else 0.0
            print(f"[BOOSTER] rack {rack_idx}: output peak={_dsp_db(out_peak):.1f}dBFS")

            if _cancel_flags.get(rack_idx):
                _finish(rack_idx, scene_name, "READY"); return

            # Blender draws a strip's waveform from the file, but re-normalises any
            # file whose peak is above full scale, so a file holding +40 dB looks
            # exactly like the quiet original. Keep the FILE at or below full scale
            # and carry the overshoot on the strip's own volume instead: the sound
            # is identical (file x strip volume), and the timeline waveform now
            # shows the boost, with Blender's red overshoot marking, just like
            # raising the volume by hand.
            strip_vol = 1.0
            if out_peak > 1.0:
                strip_vol = min(out_peak, 100.0)       # Blender's strip volume limit
                out = (out / np.float32(strip_vol)).astype(np.float32)
                print(f"[BOOSTER] rack {rack_idx}: file saved at full scale, "
                      f"strip volume set to {strip_vol:.2f} (= +{_dsp_db(strip_vol):.1f} dB)")

            # Step 3: write a real 32-bit float WAV
            try:
                _dsp.write_wav_float32(output_wav, out, sr)
            except Exception as e:
                _finish(rack_idx, scene_name, "ERROR",
                        error_msg=f"Could not write {output_wav}: {e}")
                return

            if not os.path.exists(output_wav) or os.path.getsize(output_wav) < 44:
                _finish(rack_idx, scene_name, "ERROR",
                        error_msg="Output file missing or empty")
                return

            _finish(rack_idx, scene_name, "DONE",
                    output_wav=output_wav, ch_idx=ch_idx, strip_volume=strip_vol)

        except Exception as e:
            import traceback; traceback.print_exc()
            _finish(rack_idx, scene_name, "ERROR", error_msg=str(e))

    t = threading.Thread(target=_worker,
                         name=f"BOOSTER_rack{rack_idx}", daemon=True)
    _active_jobs[rack_idx] = t
    t.start()

    if not bpy.app.timers.is_registered(_redraw_timer):
        bpy.app.timers.register(_redraw_timer, first_interval=0.25)

    print(f"[BOOSTER] rack {rack_idx} started in background thread")
