import sys, os, numpy as np, math, tempfile
sys.path.insert(0,os.path.dirname(__file__)); from hj import *
lib=sys.argv[1]; SR=48000
tmp=tempfile.mkdtemp(); rng=np.random.default_rng(5)
def tone(n,f,ph=0,a=0.4): t=np.arange(n)/SR; return a*np.sin(2*np.pi*f*t+ph)
# files (all 48k), various formats
N=SR*4
A=np.stack([tone(N,220)+0.05*rng.standard_normal(N),tone(N,330,0.3)],1); fa='pcm16'
B=np.stack([tone(N,440,0,0.5)],1); fb='pcm24'          # mono
C=np.stack([tone(N,700),tone(N,900)],1); fc='f32'
D=np.stack([tone(N,150,0,0.3),tone(N,151,0,0.3)],1); fd='pcm32'
E=np.stack([tone(N,1234,0,0.3),tone(N,555,0,0.3)],1); fe='pcm24'
files={}
for name,(d,f,kw) in {'A':(A,fa,{}),'B':(B,fb,{}),'C':(C,fc,{}),'D':(D,fd,{}),'E':(E,fe,{'extensible':True,'extra_chunk':True})}.items():
    p=os.path.join(tmp,name+'.wav'); write_wav(p,d,SR,f,**kw); dec=quant(d,f); 
    if dec.shape[1]==1: dec=np.repeat(dec,2,1)
    files[name]=(p,dec)
hj=HJ(lib); assert hj.init(SR)==1
chans={
 0:dict(segs=[dict(path='A',off=0.0,dur=0.5,pos=0.0,vol=1.0), dict(path='A',off=0.5,dur=0.5,pos=0.5,vol=1.0),   # contiguous same file => no fade at join
              dict(path='D',off=1.0,dur=0.4,pos=1.0,vol=0.8), dict(path='D',off=2.0,dur=0.3,pos=1.4,vol=0.8)],  # different offsets => faded join
        vol=0.9,pan=0.5,mute=False),
 1:dict(segs=[dict(path='B',off=0.25,dur=1.5,pos=0.3,vol=0.7)],vol=0.6,pan=0.25,mute=False),
 2:dict(segs=[dict(path='C',off=0.0,dur=2.0,pos=0.0,vol=1.0)],vol=1.0,pan=0.5,mute=True),
 3:dict(segs=[dict(path='E',off=0.1,dur=1.0,pos=0.9,vol=0.5)],vol=1.0,pan=0.9,mute=False),
}
for ch,c in chans.items():
    segs=[dict(s,path=files[s['path']][0]) for s in c['segs']]
    hj.playlist(ch,segs); hj.l.t_vol(ch,c['vol']); hj.l.t_pan(ch,c['pan']); hj.l.t_mute(ch,1 if c['mute'] else 0)
total=int(2.2*SR)
def reference():
    mix=np.zeros((total,2))
    for ch,c in chans.items():
        if c['mute']: continue
        # file lookup by original name
        tl=np.zeros((total,2))
        for name in sorted(set(s['path'] for s in c['segs'])):
            ss=[dict(s,path=name) for s in c['segs'] if s['path']==name]
            tl+=ref_channel(files[name][1],ss,SR,total)
        gl,gr=pan_gains(c['pan']); tl=tl*c['vol']; tl[:,0]*=gl; tl[:,1]*=gr
        mix+=tl
    return limiter(mix)
ref=reference()
ok_all=True
for frames in (480,1023,2048,64):
    hj.l.t_stop(); hj.run(40,frames)   # let any previous play fade out
    hj.l.t_play(0.0)
    ncb=int(math.ceil(total/frames))+400
    out,ph=hj.run(ncb,frames,sleep_us=int(frames/48000*1e6/8))
    prev=0.0; k0=None
    for i,p in enumerate(ph):
        if p>prev+1e-9: k0=i; break
    assert k0 is not None, "never started"
    # callback k0 is the first advancing one; timeline frame = (i-k0)*frames
    got=out[k0*frames:k0*frames+total]
    exp=ref.copy()
    # declick fade-in on first 96 frames of the mix
    f=(np.arange(DECL)+1)/(DECL+1); exp[:DECL]*=f[:,None]
    n=min(len(got),len(exp)); d=np.abs(got[:n]-exp[:n])
    print(f"frames/cb={frames:5d} hold_callbacks={k0:3d}  max|err|={d.max():.3e}  rms|err|={math.sqrt((d**2).mean()):.3e}  worst@{d.max(1).argmax() if d.ndim>1 else d.argmax()} xruns={hj.l.t_xruns()}")
    ok_all &= d.max()<2e-5
    # nothing audible from the muted channel (ch2) -> implicit in match
print("RESULT", "PASS" if ok_all else "FAIL"); sys.exit(0 if ok_all else 1)
hj.l.t_shutdown()
