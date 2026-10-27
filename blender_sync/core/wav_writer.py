# =============================================================================
# core/wav_writer.py
# The Hijacker — WAV file writer: 16-bit and 24-bit PCM, and 32-bit float.
#
# Python's built-in `wave` module can only write integer PCM, so a "32-bit" file
# written with it holds float samples labelled as integers and plays back as
# loud noise. This writes the headers itself (WAVE_FORMAT_PCM for 16/24-bit,
# WAVE_FORMAT_IEEE_FLOAT plus a `fact` chunk for 32-bit float) and handles the
# two things a careful bounce needs:
#   * 16-bit output is rounded (not truncated) and dithered with TPDF dither
#     (+-1 LSB triangular noise), so quiet fades don't turn into grainy
#     quantisation distortion. 24-bit output is rounded; at that depth the
#     quantisation noise is far below anything audible, so it is not dithered.
#   * 32-bit float is written exactly as rendered, with no clipping, so peaks
#     over 0 dBFS survive for later mastering.
# No bpy import: this module is unit-tested outside Blender.
# =============================================================================

import struct

import numpy as np

_MAX_DATA_BYTES = 0xFFFFFFFF - 1024      # classic RIFF limit; a bigger file needs RF64


def encode(audio, bit_depth, dither=True, rng=None):
    """Float array (frames, channels) -> little-endian sample bytes for the bit depth."""
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim == 1:
        a = a[:, None]
    if bit_depth == 32:
        return np.ascontiguousarray(a, dtype='<f4').tobytes(), 3, 32
    x = a.astype(np.float64)
    if bit_depth == 24:
        q = np.clip(np.rint(x * 8388607.0), -8388608, 8388607).astype('<i4')
        return q.view(np.uint8).reshape(-1, 4)[:, :3].tobytes(), 1, 24
    # 16-bit (also the fallback for anything else)
    x = x * 32767.0
    if dither:
        rng = rng or np.random.default_rng()
        x = x + (rng.random(x.shape) - rng.random(x.shape))      # TPDF, +-1 LSB
    q = np.clip(np.rint(x), -32768, 32767).astype('<i2')
    return q.tobytes(), 1, 16


def write_wav(filepath, audio, sample_rate, bit_depth=24, dither=True):
    """Write `audio` (frames x channels, float, nominally -1..1) to `filepath`."""
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim == 1:
        a = a[:, None]
    frames, nch = a.shape
    data, tag, bps = encode(a, bit_depth, dither)
    if len(data) > _MAX_DATA_BYTES:
        raise ValueError("audio is too long for a WAV file (4 GB limit): choose a lower "
                         "sample rate or bit depth, or a shorter range")
    block = nch * bps // 8
    if tag == 3:
        fmt = struct.pack('<HHIIHHH', 3, nch, sample_rate, sample_rate * block, block, bps, 0)
        extra = b'fact' + struct.pack('<II', 4, frames)
    else:
        fmt = struct.pack('<HHIIHH', 1, nch, sample_rate, sample_rate * block, block, bps)
        extra = b''
    body = (b'WAVE' + b'fmt ' + struct.pack('<I', len(fmt)) + fmt + extra +
            b'data' + struct.pack('<I', len(data)))
    pad = b'\x00' if len(data) & 1 else b''
    with open(filepath, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', len(body) + len(data) + len(pad)) + body)
        f.write(data)
        f.write(pad)
    return filepath
