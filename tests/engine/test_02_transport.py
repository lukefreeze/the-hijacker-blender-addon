import sys, os, numpy as np, math, tempfile
sys.path.insert(0,os.path.dirname(__file__)); from hj import *
lib=sys.argv[1]; SR=48000; FR=480
tmp=tempfile.mkdtemp()
N=SR*8; t=np.arange(N)/SR
A=np.stack([0.5*np.sin(2*np.pi*440*t),0.5*np.sin(2*np.pi*660*t+0.5)],1)
p=os.path.join(tmp,'A.wav'); write_wav(p,A,SR,'f32'); Ad=quant(A,'f32')
hj=HJ(lib); assert hj.init(SR)==1
hj.playlist(0,[dict(path=p,off=0.0,dur=8.0,pos=0.0,vol=1.0)]); hj.l.t_vol(0,1.0); hj.l.t_pan(0,0.5)
def cb_start(ph,i,prev): return int(round(ph[i]*SR))-FR
def check(out,ph,i0,i1,tag,start_frame_of=None):
    """compare callbacks i0..i1 against reference using playhead; returns (n_cb_silent_first, max_err_after_fade)"""
    errs=[]; silent=0; started=False
    for i in range(i0,i1):
        if ph[i]<=ph[i-1]+1e-12: continue
        sf=int(round(ph[i]*SR))-FR
        seg=out[i*FR:(i+1)*FR]
        exp=Ad[sf:sf+FR]
        if not started:
            if np.abs(seg).max()<1e-9: silent+=1; continue
            started=True; first_nz=i
        errs.append((i,np.abs(seg-exp).max()))
    return silent,errs
# ---- 1. play, then seek during playback
hj.l.t_play(0.0)
out1,ph1=hj.run(60,FR,sleep_us=500)        # ~0.6 s of audio at ~5x realtime
hj.l.t_seek(2.0)
out2,ph2=hj.run(120,FR,sleep_us=500)
out=np.concatenate([out1,out2]); ph=np.concatenate([ph1,ph2])
# seek time: first callback whose playhead jumps backward/forward
ji=[i for i in range(1,len(ph)) if abs(ph[i]-ph[i-1])>0.5][0]
print("seek applied at callback",ji,"playhead",ph[ji-1],"->",ph[ji])
# gap = callbacks after the jump with silent output
gap=0; i=ji
while i<len(ph) and np.abs(out[i*FR:(i+1)*FR]).max()<1e-9: gap+=1; i+=1
print(f"silence after seek: {gap} callbacks = {gap*FR/SR*1000:.1f} ms of audio time (worker latency incl.)")
# after declick, audio must equal reference
worst=0; cnt=0
for j in range(i+1,len(ph)):
    sf=int(round(ph[j]*SR))-FR; seg=out[j*FR:(j+1)*FR]; exp=Ad[sf:sf+FR]; worst=max(worst,np.abs(seg-exp).max()); cnt+=1
print(f"post-seek audio vs reference: {cnt} callbacks, max err {worst:.2e}")
# click check: biggest sample-to-sample step in the whole post-seek region vs a pure 440/660Hz tone's natural max step
steps=np.abs(np.diff(out[ji*FR:],axis=0)).max()
print(f"largest sample step after seek: {steps:.4f} (natural max for these tones ~ {2*math.pi*660/SR*0.5:.4f})")
ok1 = worst<2e-5 and steps<0.2
# ---- 2. stop -> fade out, then resume exact
hj.l.t_stop()
out3,ph3=hj.run(10,FR,sleep_us=500)
stopped_at=ph3[-1]
tail=np.abs(out3[-FR:]).max(); 
print(f"after stop: last buffer peak {tail:.2e}, playhead frozen at {ph3[-1]:.4f} (prev {ph3[-2]:.4f})")
# fade must be monotone smooth: max step across the stop fade
st=np.abs(np.diff(out3[:2*FR],axis=0)).max(); print(f"max step during stop fade: {st:.4f}")
ok2 = tail<1e-9 and ph3[-1]==ph3[-2] and st<0.2
hj.l.t_play(stopped_at)
out4,ph4=hj.run(30,FR,sleep_us=500)
first_adv=[i for i in range(1,len(ph4)) if ph4[i]>ph4[i-1]+1e-12][0]
print(f"resume-in-place: first advancing callback index {first_adv} (0 = instant, no pre-roll hold)")
worst=0
for j in range(first_adv+1,len(ph4)):
    sf=int(round(ph4[j]*SR))-FR; seg=out4[j*FR:(j+1)*FR]; worst=max(worst,np.abs(seg-Ad[sf:sf+FR]).max())
print(f"resume audio vs reference max err {worst:.2e}")
ok3 = worst<2e-5 and first_adv<=1
# ---- 3. play from a new position after being stopped
hj.l.t_stop(); hj.run(10,FR,sleep_us=500)
hj.l.t_play(5.0)
out5,ph5=hj.run(40,FR,sleep_us=500)
first_adv=[i for i in range(1,len(ph5)) if ph5[i]>ph5[i-1]+1e-12][0]
print(f"play from 5.0s: pre-roll hold {first_adv} callbacks = {first_adv*FR/SR*1000:.1f} ms; first playhead {ph5[first_adv]:.4f}")
worst=0
for j in range(first_adv+3,len(ph5)):
    sf=int(round(ph5[j]*SR))-FR; seg=out5[j*FR:(j+1)*FR]; worst=max(worst,np.abs(seg-Ad[sf:sf+FR]).max())
print(f"max err {worst:.2e}")
ok4 = worst<2e-5 and abs(ph5[first_adv]-(5.0+FR/SR))<1e-6
_ok=(ok1 and ok2 and ok3 and ok4)
print("RESULT","PASS" if _ok else f"FAIL {ok1,ok2,ok3,ok4}"); sys.exit(0 if _ok else 1)
hj.l.t_shutdown()
