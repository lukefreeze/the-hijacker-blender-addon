import sys, os, numpy as np, math, tempfile, threading, time
sys.path.insert(0,os.path.dirname(__file__)); from hj import *
lib=sys.argv[1]; SR=48000; FR=480; tmp=tempfile.mkdtemp()
N=SR*20; t=np.arange(N)/SR
A=np.stack([0.4*np.sin(2*np.pi*300*t)]*2,1); B=np.stack([0.4*np.sin(2*np.pi*1000*t)]*2,1)
pa=os.path.join(tmp,'a.wav'); pb=os.path.join(tmp,'b.wav'); write_wav(pa,A,SR,'pcm16'); write_wav(pb,B,SR,'pcm16')
hj=HJ(lib); assert hj.init(SR)==1; L=hj.l
hj.playlist(0,[dict(path=pa,off=0,dur=20.0,pos=0.0)]); L.t_vol(0,1.0)
hj.playlist(1,[dict(path=pb,off=0,dur=20.0,pos=0.0)]); L.t_vol(1,0.0)   # silent channel to be hammered; ch0 must stay perfect
L.t_play(0.0)
stop=False
def hammer():
    i=0
    while not stop:
        hj.playlist(1,[dict(path=pb,off=0.1*(i%7),dur=1.0+0.1*(i%5),pos=0.01*(i%9))]); i+=1; time.sleep(0.004)
th=threading.Thread(target=hammer); th.start()
real=FR/SR                           # REAL-TIME pacing
t0=time.time(); out,ph=hj.run(300,FR,sleep_us=int(real*1e6*0.9)); stop=True; th.join()
x=out[:,0]; k0=[i for i in range(len(ph)) if ph[i]>0][0]
x=x[k0*FR:]; tt=np.arange(len(x))/SR+0  # expected: 0.4*sin(2*pi*300*t)*declick
exp=0.4*np.sin(2*np.pi*300*tt); exp[:96]*=(np.arange(96)+1)/97; exp[:48]*=(np.arange(48)+1)/49
err=np.abs(x-exp[:len(x)]); print(f"ch0 while ch1's playlist is swapped ~{int(300*real/0.004)} times (real-time pacing): max deviation from pure tone {err.max():.2e}")
print("  k0",k0,"ph[k0]",ph[k0],"worst idx",err.argmax(),"time",err.argmax()/SR,"count>1e-4:",(err>1e-4).sum(),"first bad idx",np.where(err>1e-4)[0][:5])
ok1=err.max()<1e-4
# now swap ch0's OWN playlist (identical content) mid-play and measure the audible gap
L.t_stop(); hj.run(10,FR,sleep_us=2000)
hj.playlist(0,[dict(path=pa,off=0,dur=20.0,pos=0.0)]); L.t_vol(1,0.0); hj.l.t_clear(1)
L.t_play(1.0); out0,ph0=hj.run(60,FR,sleep_us=int(real*1e6*0.9))
hj.playlist(0,[dict(path=pa,off=0,dur=20.0,pos=0.0)])            # same playlist re-published during playback
out1,ph1=hj.run(60,FR,sleep_us=int(real*1e6*0.9))
o=np.concatenate([out0,out1])[:,0]; 
sil=0; maxrun=0
for i in range(60,len(o)//FR):
    seg=o[i*FR:(i+1)*FR]
    if np.abs(seg).max()<1e-6: sil+=1
print(f"re-publishing the SAME channel's playlist during playback: {sil} silent callbacks (each {FR/SR*1000:.0f} ms)")
ok2=sil<=2
L.t_shutdown(); print("RESULT","PASS" if (ok1 and ok2) else "FAIL"); sys.exit(0 if (ok1 and ok2) else 1)
