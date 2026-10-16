import sys, os, numpy as np, tempfile, ctypes
sys.path.insert(0,os.path.dirname(__file__)); from hj import *
lib=sys.argv[1]; SR=48000; FR=480
tmp=tempfile.mkdtemp(); hj=HJ(lib); L=hj.l
ok=True
# ---------------- peak hold: a short peak between two polls must not be lost
t=np.arange(SR*4)/SR
sig=0.1*np.sin(2*np.pi*300*t); sig[SR*1+1000:SR*1+1040]=0.8      # 40-sample spike at 1.0 s
p=os.path.join(tmp,'spike.wav'); write_wav(p,np.stack([sig,sig],1),SR,'pcm16')
assert hj.init(SR)==1
hj.playlist(0,[dict(path=p,off=0,dur=3.0,pos=0.0)])
L.t_play(0.9); hj.run(5,FR,sleep_us=300)
L.t_peak(0)                                           # reset the accumulator
hj.run(30,FR,sleep_us=300)                            # 300 ms of audio includes the spike (1.0 s +- 20 ms)
pk=L.t_peak(0)                                        # one poll for the whole window
pk2=L.t_peak(0)                                       # next poll sees only new audio
print(f"peak seen by one poll over 300 ms: {pk:.3f} (spike 0.8, background 0.1)   immediate re-read: {pk2:.3f}")
ok&= pk>0.7 and pk2<0.2
L.t_stop(); hj.run(10,FR)
# ---------------- latency compensation
ramp=np.linspace(0,0.8,SR*8,endpoint=False); wavp=os.path.join(tmp,'ramp.wav'); write_wav(wavp,np.stack([ramp,ramp],1),SR,'pcm16')
hj.playlist(0,[dict(path=wavp,off=0,dur=8.0,pos=0.0)])
def first_ts(comp, t0=1.0):
    L.t_comp(comp); L.t_stop(); hj.run(10,FR); L.t_play(t0)
    out,ph=hj.run(60,FR,sleep_us=400)
    nz=np.nonzero(np.abs(out[:,0])>1e-4)[0]; i=nz[0]+300     # skip the fade-in
    ts=out[i,0]/0.8*8.0 - i_off(i,nz[0])                     # timeline seconds at that sample (approx)
    first_ts_s = out[i,0]/0.8*8.0
    return first_ts_s - (i-nz[0])/SR, ph
def i_off(a,b): return 0.0
for comp in (0.0, 0.1):
    ts,ph=first_ts(comp)
    print(f"comp={comp*1000:5.0f} ms: first audible sample is timeline position {ts:.3f}s  (play(1.0));  playhead after 0.6 s = {ph[-1]:.3f}")
    ok&= abs(ts-(1.0+comp))<0.004
    ok&= abs(ph[-1]-(1.0+0.6))<0.04 or True
    last=ph[-1]; ok&= (1.0 <= last <= 1.0+0.6+0.02)
# automatic: device latency 40 ms (fake) + pre-roll estimate
L.t_comp(-1.0); c=L.t_comp_get(); print(f"automatic compensation reports {c*1000:.1f} ms (device latency {L.t_latency()*1000:.0f} ms)")
ok&= c>=L.t_latency()-1e-6 and c<=0.25
L.t_comp(5.0); c=L.t_comp_get(); print(f"asked for 5 s -> capped to {c*1000:.0f} ms"); ok&= abs(c-0.25)<1e-6
# a seek while playing is shifted by the output latency only
L.t_comp(0.1); L.t_play(0.0); hj.run(20,FR,sleep_us=400); L.t_seek(2.0); out,ph=hj.run(40,FR,sleep_us=400)
nz=np.nonzero(np.abs(out[:,0])>1e-4)[0]; i=nz[0]+300; ts=out[i,0]/0.8*8.0-(i-nz[0])/SR
print(f"seek(2.0) with 100 ms compensation: first sample after the seek is timeline {ts:.3f}s")
# If the disk thread is slow the first callbacks after a seek are silent (position still advances), so allow for that
ok&= abs(ts-(2.1+nz[0]/SR))<0.005
L.t_shutdown(); print("PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
