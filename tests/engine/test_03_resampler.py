import sys, os, numpy as np, math, tempfile
sys.path.insert(0,os.path.dirname(__file__)); from hj import *
lib=sys.argv[1]; SR=48000; FR=480
tmp=tempfile.mkdtemp()
hj=HJ(lib); assert hj.init(SR)==1
res=[]
def run_case(fsr, tag, fmt='f32'):
    N=int(fsr*3.0); t=np.arange(N)/fsr
    f1,f2=1000.0,7000.0
    A=np.stack([0.3*np.sin(2*np.pi*f1*t)+0.2*np.sin(2*np.pi*f2*t),0.3*np.sin(2*np.pi*f1*t+1)],1)
    p=os.path.join(tmp,f'r{fsr}.wav'); write_wav(p,A,fsr,fmt)
    hj.l.t_stop(); hj.run(10,FR)
    hj.playlist(0,[dict(path=p,off=0.5,dur=2.0,pos=0.0,vol=1.0)]); hj.l.t_vol(0,1.0); hj.l.t_pan(0,0.5)
    hj.l.t_play(0.0)
    total=int(1.5*SR)
    out,ph=hj.run(int(total/FR)+200,FR,sleep_us=300)
    k0=[i for i in range(len(ph)) if ph[i]>(0.0 if i==0 else ph[i-1])+1e-12][0]
    got=out[k0*FR:k0*FR+total,0].astype(np.float64)
    # ideal: analytic signal evaluated at the output times
    to=np.arange(total)/SR+0.5
    ideal=0.3*np.sin(2*np.pi*f1*to)+0.2*np.sin(2*np.pi*f2*to)
    # compare after the fade-in region and before fade-out
    sl=slice(200,total-200)
    # apply edge fade? only first/last 48 -> excluded
    err=got[sl]-ideal[sl]
    snr=10*np.log10((ideal[sl]**2).mean()/(err**2).mean())
    # continuity: no clicks at 512-frame block joins -> check max |second difference| vs ideal's
    d2=np.abs(np.diff(got[sl],2)).max(); d2i=np.abs(np.diff(ideal[sl],2)).max()
    print(f"file {fsr:6d}Hz -> engine {SR}Hz: SNR vs ideal {snr:6.1f} dB   max 2nd-diff {d2:.4f} (ideal {d2i:.4f})")
    return snr, d2, d2i
for fsr in (44100,32000,96000,22050):
    s,d2,d2i=run_case(fsr,'x')
    res.append((fsr,s,d2<=d2i*1.5))
_ok=all(s>85 and c for _,s,c in res)
print("RESULT", "PASS" if _ok else "FAIL", res); sys.exit(0 if _ok else 1)
hj.l.t_shutdown()
