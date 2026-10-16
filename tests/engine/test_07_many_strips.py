import sys, os, numpy as np, tempfile, time
sys.path.insert(0,os.path.dirname(__file__)); from hj import *
lib=sys.argv[1]; SR=48000; FR=480; N=int(sys.argv[2]) if len(sys.argv)>2 else 5000
tmp=tempfile.mkdtemp(); hj=HJ(lib)
t=np.arange(SR*20)/SR; tone=np.stack([0.3*np.sin(2*np.pi*440*t)]*2,1)
p=os.path.join(tmp,'a.wav'); write_wav(p,tone,SR,'pcm16')
assert hj.init(SR)==1
# N short strips (20 ms) with 20 ms gaps, alternating file offsets, across 1 channel
segs=[dict(path=p,off=(i%100)*0.1,dur=0.02,pos=i*0.04,vol=1.0) for i in range(N)]
t0=time.time(); hj.playlist(0,segs); print(f"publish {N} segs: {(time.time()-t0)*1e3:.1f} ms")
hj.l.t_play(0.0); out,ph=hj.run(300,FR,sleep_us=400)
# strips are 20ms on, 20ms off: count energy bursts in first 3 seconds
x=np.abs(out.reshape(-1,2)[:,0]); blk=SR//25   # 40ms blocks
nb=len(x)//blk; on=[(x[i*blk:i*blk+blk//2].max()>0.05) for i in range(nb)]
print("40ms periods with sound in first half:",sum(on),"of",nb)
hj.l.t_seek(  N*0.04-1.0 ); out2,_=hj.run(100,FR,sleep_us=400)
print("seek near the end of the playlist plays:", np.abs(out2).max()>0.05)
xr=hj.l.t_xruns(); print("xruns",xr)
hj.l.t_shutdown()
_ok = sum(on)>=nb-2 and np.abs(out2).max()>0.05
print("RESULT","PASS" if _ok else "FAIL"); sys.exit(0 if _ok else 1)
