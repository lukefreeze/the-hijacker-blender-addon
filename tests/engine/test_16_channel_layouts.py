"""Source channel layouts: a mono file plays centred on both speakers (and pans like any other
source), a stereo file is untouched, and a file with more than two channels (5.1 etc.) plays
its front left/right pair. Checked offline (Mixdown) and live."""
import sys, os, numpy as np, tempfile, ctypes
sys.path.insert(0, os.path.dirname(__file__)); from hj import *
lib = sys.argv[1]; SR = 48000; FR = 480
tmp = tempfile.mkdtemp(); ok = True
def check(name, cond, detail=""):
    global ok
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail else "")); ok &= bool(cond)
t = np.arange(SR * 2) / SR
A = 0.5 * np.sin(2 * np.pi * 440 * t); B = 0.4 * np.sin(2 * np.pi * 660 * t + 0.3); C = 0.9 * np.sin(2 * np.pi * 1000 * t)
def wav(name, cols, fmt='f32', ext=False):
    p = os.path.join(tmp, name + '.wav'); write_wav(p, np.stack(cols, 1), SR, fmt, extensible=ext); return p
def render(path, pan=0.5, live=False):
    hj = HJ(lib); assert hj.init(SR) == 1; L = hj.l
    hj.playlist(0, [dict(path=path, off=0, dur=2.0, pos=0.0)]); L.t_vol(0, 1.0); L.t_pan(0, pan)
    if live:
        L.t_play(0.0); out, _ = hj.run(int(2.2 * SR / FR), FR, sleep_us=300); L.t_stop(); hj.run(10, FR); L.t_shutdown()
        nz = np.nonzero(np.abs(out).max(1) > 1e-7)[0][0]; return out[nz:nz + 90000]
    L.t_off_begin.argtypes = [ctypes.c_double, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_int]; L.t_off_render.argtypes = [ctypes.c_void_p, ctypes.c_int]
    assert L.t_off_begin(0.0, SR, 1, 1, 1) == 1
    out = np.zeros((2 * SR, 2), dtype=np.float32); L.t_off_render(out.ctypes.data, 2 * SR); L.t_off_end(); L.t_shutdown(); return out
m = slice(500, 90000)
for fmt in ('f32', 'pcm16', 'pcm24'):
    mono = wav('mono_' + fmt, [A], fmt); out = render(mono)
    q = quant(A, fmt)
    check(f"mono {fmt}: same signal on both sides at centre", np.abs(out[m, 0] - out[m, 1]).max() < 1e-7 and np.abs(out[m, 0] - q[m]).max() < 1e-6, f"{np.abs(out[m,0]-q[m]).max():.1e}")
mono = wav('mono', [A])
stereo_same = wav('stereo_same', [A, A])
check("mono sounds exactly like the same signal in a stereo file", np.array_equal(render(mono), render(stereo_same)))
o = render(mono, pan=0.0); check("mono panned hard left: left only, full level", np.abs(o[m, 1]).max() == 0 and np.abs(o[m, 0] - A[m]).max() < 1e-6)
o = render(mono, pan=1.0); check("mono panned hard right: right only, full level", np.abs(o[m, 0]).max() == 0 and np.abs(o[m, 1] - A[m]).max() < 1e-6)
st = wav('stereo', [A, B]); o = render(st)
check("stereo file untouched at centre (L=A, R=B)", np.abs(o[m, 0] - A[m]).max() < 1e-6 and np.abs(o[m, 1] - B[m]).max() < 1e-6)
six = wav('six', [A, B, C, C, C, C], 'pcm24', ext=True); o = render(six)
check("6-channel (5.1) file: plays front left/right (centre, LFE, surrounds are not mixed in)", np.abs(o[m, 0] - quant(A, 'pcm24')[m]).max() < 1e-6 and np.abs(o[m, 1] - quant(B, 'pcm24')[m]).max() < 1e-6)
off = render(mono); live = render(mono, live=True)
nz = np.nonzero(np.abs(off).max(1) > 1e-7)[0][0]
check("mono: live playback equals the offline render", np.abs(live[2000:80000] - off[nz + 2000:nz + 80000]).max() < 1e-6)
print("RESULT", "PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
