import sys, os, numpy as np, tempfile, ctypes
sys.path.insert(0,os.path.dirname(__file__)); from hj import *
lib=sys.argv[1]; SR=48000; FR=480
tmp=tempfile.mkdtemp(); hj=HJ(lib); L=hj.l
L.t_master.argtypes=[ctypes.c_float]; L.t_master_get.restype=ctypes.c_float
L.t_master_peak.restype=ctypes.c_float; L.t_master_clip.restype=ctypes.c_int
ok=True
def check(name, cond, detail=""):
    global ok
    print(("  ok   " if cond else "  FAIL ")+name+(("  "+detail) if detail else "")); ok &= bool(cond)

t=np.arange(SR*6)/SR; A=0.5
tone=A*np.sin(2*np.pi*300*t)
p=os.path.join(tmp,'tone.wav'); write_wav(p,np.stack([tone,tone],1),SR,'f32')
assert hj.init(SR)==1
hj.playlist(0,[dict(path=p,off=0,dur=5.5,pos=0.0)])
L.t_play(0.0); hj.run(30,FR,sleep_us=300); L.t_master_peak(); L.t_master_clear()

# unity: master at 1.0 changes nothing, peak reports the signal peak
out,_=hj.run(40,FR,sleep_us=300); pk=L.t_master_peak()
check("unity gain: peak equals signal peak", abs(pk-np.abs(out).max())<1e-4, f"reported {pk:.4f}, actual {np.abs(out).max():.4f}")
check("no clip at 0.5 amplitude", L.t_master_clip()==0)

# half gain (-6.02 dB): settles to exactly half
L.t_master(0.5); hj.run(60,FR,sleep_us=300); L.t_master_peak()
out,_=hj.run(40,FR,sleep_us=300); r=np.abs(out).max()/A
check("gain 0.5 settles to half amplitude", abs(r-0.5)<0.005, f"ratio {r:.4f}")
check("meter follows the fader (post-fader)", abs(L.t_master_peak()-np.abs(out).max())<0.02)

# smoothing: a full-scale gain step must not click
L.t_master(1.0); hj.run(60,FR,sleep_us=300)
L.t_master(0.0); out,_=hj.run(40,FR,sleep_us=300)
step=np.abs(np.diff(out[:,0])).max(); tone_step=2*np.pi*300/SR*A
check("fader step is smoothed (no click)", step<tone_step*1.5, f"largest sample step {step:.4f}, plain tone {tone_step:.4f}")
check("gain 0 is silent once settled", np.abs(out[-480:]).max()<1e-4)

# above 0 dBFS: clip latch set, output limited
L.t_master(2.0); hj.run(60,FR,sleep_us=300); L.t_master_peak(); L.t_master_clear()
L.t_master(1.0); hj.run(60,FR,sleep_us=300)
check("clip latch stays clear below 0 dBFS", L.t_master_clip()==0)
hj.playlist(0,[dict(path=p,off=0,dur=5.5,pos=0.0,vol=1.9)])   # 0.95 -> after master 2.0 = 1.9 pre-limiter
L.t_master(2.0); L.t_play(0.0); hj.run(60,FR,sleep_us=300); L.t_master_peak()
out,_=hj.run(40,FR,sleep_us=300); pk=L.t_master_peak()
check("clip latch set above 0 dBFS", L.t_master_clip()==1)
check("meter reads above 1.0 so the clip is visible", pk>1.0, f"peak {pk:.3f}")
check("output stays below full scale (limiter)", np.abs(out).max()<1.0, f"max {np.abs(out).max():.4f}")
L.t_master_clear(); check("clip latch clears", L.t_master_clip()==0)

# range / bad values
L.t_master(10.0); check("gain capped at 2.0", abs(L.t_master_get()-2.0)<1e-6)
L.t_master(-1.0); check("negative gain clamped to 0", L.t_master_get()==0.0)
L.t_master(0.7); L.t_master(float('nan')); check("NaN ignored", abs(L.t_master_get()-0.7)<1e-6)
L.t_shutdown(); print("PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
