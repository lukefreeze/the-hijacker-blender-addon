"""The mixdown WAV writer: valid files at 16/24-bit PCM and 32-bit float, dither only where
it belongs, and files the engine itself can read back."""
import sys, os, importlib.util, wave, struct, tempfile, ctypes
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from hj import *
lib = sys.argv[1]
spec = importlib.util.spec_from_file_location("wav_writer", os.path.join(HERE, "..", "..", "blender_sync", "core", "wav_writer.py"))
ww = importlib.util.module_from_spec(spec); spec.loader.exec_module(ww)
tmp = tempfile.mkdtemp(); ok = True
def check(name, cond, detail=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail else "")); ok &= bool(cond)

SR = 48000; t = np.arange(SR) / SR
sig = np.stack([0.5 * np.sin(2 * np.pi * 440 * t), 0.25 * np.sin(2 * np.pi * 660 * t)], 1).astype(np.float32)
sig[100, 0] = 1.8                     # an over: only float may keep it

def parse(path):
    d = open(path, 'rb').read(); assert d[:4] == b'RIFF' and d[8:12] == b'WAVE'
    assert struct.unpack('<I', d[4:8])[0] == len(d) - 8, "RIFF size field"
    pos = 12; out = {}
    while pos + 8 <= len(d):
        cid = d[pos:pos + 4]; sz = struct.unpack('<I', d[pos + 4:pos + 8])[0]
        out[cid] = d[pos + 8:pos + 8 + sz]; pos += 8 + sz + (sz & 1)
    return out
for bd in (16, 24, 32):
    p = os.path.join(tmp, f"t{bd}.wav"); ww.write_wav(p, sig, SR, bd)
    c = parse(p); tag, nch, sr, br, ba, bps = struct.unpack('<HHIIHH', c[b'fmt '][:16])
    check(f"{bd}-bit header", nch == 2 and sr == SR and bps == bd and ba == 2 * bd // 8 and br == SR * ba and tag == (3 if bd == 32 else 1), f"tag {tag}")
    if bd == 32:
        check("float file has a fact chunk with the frame count", struct.unpack('<I', c[b'fact'])[0] == SR)
        back = np.frombuffer(c[b'data'], '<f4').reshape(-1, 2)
        check("float is bit-exact, including the over 0 dBFS", np.array_equal(back, sig) and back.max() > 1.5)
    elif bd == 24:
        raw = np.frombuffer(c[b'data'], np.uint8).reshape(-1, 3).astype(np.int32)
        v = (raw[:, 0] | raw[:, 1] << 8 | raw[:, 2] << 16); v = np.where(v >= 1 << 23, v - (1 << 24), v).reshape(-1, 2) / 8388607.0
        e = np.abs(v[200:] - np.clip(sig[200:], -1, 1)).max()
        check("24-bit error is under one LSB", e < 1.2e-7, f"{e:.1e}")
    else:
        with wave.open(p) as w:
            check("16-bit opens with Python's wave module", w.getsampwidth() == 2 and w.getnframes() == SR)
        v = np.frombuffer(c[b'data'], '<i2').reshape(-1, 2) / 32767.0
        e = np.abs(v[200:] - sig[200:]).max()
        check("16-bit error is within dither (about 2 LSB)", e < 3.2 / 32767, f"{e * 32767:.2f} LSB")
        check("the over is clipped at full scale in 16-bit", abs(v[100, 0]) > 0.999)
# dither: a very quiet constant must not collapse to digital silence or to a stuck value
quiet = np.full((SR, 2), 0.3 / 32767.0, dtype=np.float32)
p = os.path.join(tmp, "q.wav"); ww.write_wav(p, quiet, SR, 16)
v = np.frombuffer(parse(p)[b'data'], '<i2').astype(float)
check("dithered 16-bit keeps a sub-LSB signal alive on average", abs(v.mean() - 0.3) < 0.05 and v.std() > 0.3, f"mean {v.mean():.2f} LSB (true 0.30)")
p2 = os.path.join(tmp, "q2.wav"); ww.write_wav(p2, quiet, SR, 16, dither=False)
v2 = np.frombuffer(parse(p2)[b'data'], '<i2'); check("without dither it rounds to silence (so dither is doing something)", np.all(v2 == 0))
# odd sample count -> padding byte keeps chunks aligned
p3 = os.path.join(tmp, "odd.wav"); ww.write_wav(p3, np.zeros((7, 1), np.float32), SR, 24); parse(p3)
# the engine reads the 24-bit and float files and plays what was written
for bd in (24, 32):
    p = os.path.join(tmp, f"e{bd}.wav"); body = (0.4 * sig).astype(np.float32); ww.write_wav(p, body, SR, bd)
    hj = HJ(lib); assert hj.init(SR) == 1; hj.playlist(0, [dict(path=p, off=0, dur=0.9, pos=0.0)])
    hj.l.t_play(0.0); out, _ = hj.run(int(0.9 * SR / 480), 480, sleep_us=300); hj.l.t_stop(); hj.run(10, 480); hj.l.t_shutdown()
    nz = np.nonzero(np.abs(out[:, 0]) > 1e-4)[0]
    seg = out[nz[0] + 2000: nz[0] + 8000, 0]; ref = body[nz[0] + 2000 - nz[0]:, 0][:0]
    r = np.sqrt(np.mean(seg ** 2)); expect = 0.4 * 0.5 / np.sqrt(2)
    check(f"engine plays the {bd}-bit file at the right level", abs(r - expect) / expect < 0.05, f"rms {r:.3f} expected ~{expect:.3f}")
print("RESULT", "PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
