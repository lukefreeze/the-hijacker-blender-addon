# =============================================================================
# core/booster_dsp.py
# Pure-NumPy DSP for THE BOOSTER!! — gain plus a look-ahead peak limiter, and a
# correct 32-bit float WAV writer. No Blender imports, so it can be unit tested
# on its own.
#
# Why this exists
#   * Python's `wave` module cannot write floating-point WAVs: it labels the
#     data as integer PCM, so players read the float bit patterns as huge
#     integers and play loud distorted noise (even at 0 dB of boost). The
#     writer below writes a real IEEE-float WAV (format tag 3).
#   * The old "soft limiter" (tanh applied only above 1.0) jumped from 1.0 to
#     0.76 at the threshold, which is harsh distortion. The limiter here
#     reduces the GAIN smoothly just before a peak (look-ahead), so a loud
#     passage is turned down instead of being clipped.
# =============================================================================

import struct

import numpy as np

CEILING_DB = -0.3          # limiter output ceiling, dBFS
ATTACK_MS  = 8.0           # look-ahead / gain ramp-down time
RELEASE_MS = 200.0         # how slowly the gain recovers after a loud passage


def db_to_lin(db):
    return 10.0 ** (float(db) / 20.0)


def _sliding_min_forward(a, w):
    """out[i] = min(a[i : i + w]) — a window that looks FORWARD in time.
    O(n) (van Herk / Gil-Werman); positions past the end count as 1.0
    (no gain reduction)."""
    n = len(a)
    if w <= 1 or n == 0:
        return a.copy()
    total = n + w - 1
    extra = (-total) % w
    ap = np.ones(total + extra, dtype=a.dtype)
    ap[:n] = a
    blocks = ap.reshape(-1, w)
    g = np.minimum.accumulate(blocks, axis=1).reshape(-1)                      # prefix min per block
    h = np.minimum.accumulate(blocks[:, ::-1], axis=1)[:, ::-1].reshape(-1)    # suffix min per block
    return np.minimum(h[:n], g[w - 1:w - 1 + n])


def _box_mean_causal(a, w):
    """out[i] = mean(a[i-w+1 .. i]); samples before the start count as 1.0."""
    if w <= 1 or len(a) == 0:
        return a.copy()
    padded = np.concatenate([np.ones(w - 1, dtype=np.float64), a.astype(np.float64)])
    c = np.concatenate([[0.0], np.cumsum(padded)])
    return (c[w:] - c[:-w]) / w


def limit(x, sr, ceiling_db=CEILING_DB, attack_ms=ATTACK_MS, release_ms=RELEASE_MS):
    """Look-ahead peak limiter on x (shape [n, channels], float).

    One gain curve is shared by every channel (linked), so the stereo image is
    preserved. The gain never exceeds what is needed to keep the peak at or
    under the ceiling, ramps down smoothly BEFORE a loud peak arrives, and
    recovers slowly afterwards. Audio already under the ceiling is untouched."""
    ceiling = db_to_lin(ceiling_db)
    peak = np.max(np.abs(x), axis=1).astype(np.float64)
    need = np.minimum(1.0, ceiling / np.maximum(peak, 1e-12))
    if need.size == 0 or float(need.min()) >= 1.0:
        return x
    n_att = max(2, int(sr * attack_ms / 1000.0))
    n_rel = max(n_att, int(sr * release_ms / 1000.0))
    g_fast = _box_mean_causal(_sliding_min_forward(need, n_att), n_att)     # never above `need`
    g_slow = _box_mean_causal(g_fast, n_rel)                                  # lingering release
    gain = np.minimum(g_fast, g_slow)
    y = x * gain[:, None].astype(x.dtype)
    # numerical safety net only (the gain curve already guarantees the ceiling)
    np.clip(y, -ceiling, ceiling, out=y)
    return y


def boost_and_limit(x, sr, boost_db, limiter=True, ceiling_db=CEILING_DB):
    """Apply `boost_db` of gain to x ([n, channels] float32). With the limiter
    on, the result never exceeds `ceiling_db`; with it off (RAW) the boosted
    signal is returned as is and may go above 0 dBFS (floating point keeps it)."""
    y = np.ascontiguousarray(x, dtype=np.float32)
    g = db_to_lin(boost_db)
    if abs(g - 1.0) > 1e-9:
        y = y * np.float32(g)
    if limiter:
        y = limit(y, sr, ceiling_db=ceiling_db)
    return np.ascontiguousarray(y, dtype=np.float32)


def write_wav_float32(path, data, sr):
    """Write data ([n, channels] float) as a 32-bit IEEE-float WAV (format tag 3)."""
    data = np.ascontiguousarray(data, dtype="<f4")
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    n, ch = data.shape
    nbytes = n * ch * 4
    if nbytes >= 0xFFFFFFF0 - 64:
        raise ValueError("audio is too long for a WAV file (over 4 GB)")
    fmt  = struct.pack("<4sIHHIIHHH", b"fmt ", 18, 3, ch, int(sr), int(sr) * ch * 4, ch * 4, 32, 0)
    fact = struct.pack("<4sII", b"fact", 4, n)
    dhdr = struct.pack("<4sI", b"data", nbytes)
    riff = 4 + len(fmt) + len(fact) + len(dhdr) + nbytes
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", riff) + b"WAVE")
        f.write(fmt)
        f.write(fact)
        f.write(dhdr)
        data.tofile(f)
