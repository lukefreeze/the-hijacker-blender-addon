import sys, os, numpy as np, math, tempfile, struct
sys.path.insert(0,os.path.dirname(__file__)); from hj import *
lib=sys.argv[1]; SR=48000; FR=480
tmp=tempfile.mkdtemp(); rng=np.random.default_rng(1)
hj=HJ(lib)
# playlist before init (should be accepted, engine not running)
t=np.arange(SR*3)/SR; tone=np.stack([0.3*np.sin(2*np.pi*500*t),0.3*np.sin(2*np.pi*600*t)],1)
good=os.path.join(tmp,'good.wav'); write_wav(good,tone,SR,'pcm16')
hj.playlist(0,[dict(path=good,off=0,dur=2.0,pos=0.0)])
assert hj.init(SR)==1
def start_and_run(secs=0.5):
    hj.l.t_stop(); hj.run(10,FR)
    hj.l.t_play(0.0)
    out,ph=hj.run(int(secs*SR/FR)+10,FR,sleep_us=400); return out,ph
out,ph=start_and_run(); print("playlist set before init plays:", np.abs(out).max()>0.1)
ok=np.abs(out).max()>0.1
# --- bad inputs -> must not crash; other channels keep playing
cases={}
cases['missing']=os.path.join(tmp,'nope.wav')
open(os.path.join(tmp,'garbage.wav'),'wb').write(rng.bytes(5000)); cases['garbage']=os.path.join(tmp,'garbage.wav')
open(os.path.join(tmp,'empty.wav'),'wb').write(b''); cases['empty']=os.path.join(tmp,'empty.wav')
raw=open(good,'rb').read(); open(os.path.join(tmp,'trunc.wav'),'wb').write(raw[:3000]); cases['truncated']=os.path.join(tmp,'trunc.wav')
# data size field says huge
b=bytearray(raw); i=b.index(b'data'); b[i+4:i+8]=struct.pack('<I',0xFFFFFFFF); open(os.path.join(tmp,'stream.wav'),'wb').write(bytes(b)); cases['streamed(size=-1)']=os.path.join(tmp,'stream.wav')
# 6 channel float
d6=np.stack([0.1*np.sin(2*np.pi*(200+100*c)*t) for c in range(6)],1); write_wav(os.path.join(tmp,'six.wav'),d6,SR,'f32'); cases['6ch']=os.path.join(tmp,'six.wav')
write_wav(os.path.join(tmp,'u8.wav'),tone,SR,'pcm8'); cases['8bit']=os.path.join(tmp,'u8.wav')
write_wav(os.path.join(tmp,'f64.wav'),tone,SR,'f64'); cases['f64']=os.path.join(tmp,'f64.wav')
short=tone[:SR//10]; write_wav(os.path.join(tmp,'short.wav'),short,SR,'pcm16'); cases['short(0.1s, asked 2s)']=os.path.join(tmp,'short.wav')
nanf=tone.copy().astype(np.float32); nanf[1000:1100,0]=np.nan; nanf[2000:2010,1]=np.inf; write_wav(os.path.join(tmp,'nan.wav'),nanf,SR,'f32'); cases['NaN/Inf samples']=os.path.join(tmp,'nan.wav')
for name,path in cases.items():
    hj.playlist(1,[dict(path=path,off=0.0,dur=2.0,pos=0.0)])
    out,ph=start_and_run(0.4)
    fin=np.isfinite(out).all(); mx=np.abs(out).max()
    print(f"  {name:24s} finite={fin}  peak={mx:.3f}  (ch0 good file keeps playing: {mx>0.1})")
    ok&=fin and mx>0.1 and mx<=1.0
# weird playlists
hj.playlist(1,[dict(path=good,off=-0.5,dur=1.0,pos=0.2),dict(path=good,off=1e9,dur=1.0,pos=0.5),dict(path=good,off=0,dur=0.0,pos=0.1),dict(path=good,off=0,dur=-1,pos=0.1),dict(path=good,off=0,dur=1.0,pos=-0.3)])
out,ph=start_and_run(0.4); ok&=np.isfinite(out).all(); print("  odd offsets / zero / negative durations / negative pos: finite", np.isfinite(out).all())
segs=[dict(path=good,off=0.0,dur=0.02,pos=0.02*i,vol=1.0) for i in range(100)]   # 100 segs > HJ_MAX_SEGMENTS(64)
hj.playlist(1,segs); out,ph=start_and_run(0.4); ok&=np.isfinite(out).all(); print("  100 segments (cap 64): finite", np.isfinite(out).all())
overl=[dict(path=good,off=0.0,dur=1.0,pos=0.0,vol=0.5),dict(path=good,off=0.0,dur=1.0,pos=0.0,vol=0.5)]
hj.playlist(1,overl); out,ph=start_and_run(0.3); print("  overlapping segments sum, peak %.3f"%np.abs(out).max()); ok&=np.isfinite(out).all()
hj.l.t_clear(5); hj.l.t_vol(99,1.0); hj.l.t_pan(-1,0.5); hj.l.t_mute(40,1)   # out of range indices
hj.l.t_vol(0,float('nan')); hj.l.t_pan(0,float('nan'))
out,ph=start_and_run(0.2); ok&=np.isfinite(out).all(); print("  out-of-range channel indices / NaN params ignored: finite", np.isfinite(out).all())
# clear everything -> silence
hj.l.t_clear_all(); out,ph=start_and_run(0.2); print("  cleared -> silent:", np.abs(out[FR*3:]).max()==0.0); ok&=np.abs(out[FR*3:]).max()==0.0
# re-init cycle
hj.l.t_shutdown(); hj2=HJ(lib)  # same lib, state reset
print("  shutdown ok")
print("RESULT","PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
