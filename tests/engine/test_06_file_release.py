import sys, os, numpy as np, tempfile, time
sys.path.insert(0,os.path.dirname(__file__)); from hj import *
lib=sys.argv[1]; SR=48000; FR=480
tmp=tempfile.mkdtemp(); hj=HJ(lib)
t=np.arange(SR*3)/SR; tone=np.stack([0.3*np.sin(2*np.pi*500*t)]*2,1)
paths=[]
for i in range(8):
    p=os.path.join(tmp,f'f{i}.wav'); write_wav(p,tone,SR,'pcm16'); paths.append(p)
def open_files():
    r=set()
    for fd in os.listdir('/proc/self/fd'):
        try: l=os.readlink('/proc/self/fd/'+fd)
        except OSError: continue
        if l.startswith(tmp): r.add(l)
    return r
assert hj.init(SR)==1
ok=True
def play_a_bit():
    hj.l.t_play(0.0); hj.run(60,FR,sleep_us=400)
# 1. clear_channel closes the file synchronously
for i in range(4): hj.playlist(i,[dict(path=paths[i],off=0,dur=2.0,pos=0.0)])
play_a_bit(); print("open while playing:",len(open_files()))
hj.l.t_stop(); hj.run(10,FR)
hj.l.t_clear(0); time.sleep(0); o=open_files()
r1 = paths[0] not in o; print("clear_channel releases file immediately:",r1); ok&=r1
# 2. replacing the playlist releases the old file
hj.playlist(1,[dict(path=paths[5],off=0,dur=2.0,pos=0.0)])
o=open_files(); r2 = paths[1] not in o; print("replace playlist releases old file:",r2); ok&=r2
# 3. clear_all releases everything and files can be renamed/deleted
hj.l.t_clear_all(); o=open_files(); r3=len(o)==0; print("clear_all releases everything:",r3, o); ok&=r3
# 4. repeated rapid cycles never leak descriptors
for k in range(50):
    for i in range(8): hj.playlist(i,[dict(path=paths[i],off=0,dur=2.0,pos=0.0)])
    hj.l.t_play(0.0); hj.run(5,FR,sleep_us=300)
    hj.l.t_stop(); hj.run(3,FR); hj.l.t_clear_all()
o=open_files(); r4=len(o)==0; print("50 play/clear cycles leave no files open:",r4); ok&=r4
# 5. stopped engine (no playback running) still releases
hj.playlist(2,[dict(path=paths[2],off=0,dur=2.0,pos=0.0)]); hj.l.t_clear(2)
r5=paths[2] not in open_files(); print("clear before ever playing:",r5); ok&=r5
hj.l.t_shutdown(); print("PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
