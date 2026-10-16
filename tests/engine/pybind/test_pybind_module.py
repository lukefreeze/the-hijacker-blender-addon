import sys, os, glob, ctypes, numpy as np, tempfile, math
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..'))
from hj import write_wav
modpath=glob.glob(os.path.join(sys.argv[1],'hijacker_engine*'))[0]
sys.path.insert(0,os.path.dirname(modpath))
import hijacker_engine as engine
lib=ctypes.CDLL(modpath); lib.fake_run.argtypes=[ctypes.POINTER(ctypes.c_float),ctypes.c_ulong]
def run(frames,n=1):
    out=np.zeros(frames*2*n,dtype=np.float32)
    for i in range(n): lib.fake_run(out[i*frames*2:].ctypes.data_as(ctypes.POINTER(ctypes.c_float)),frames)
    return out.reshape(-1,2)
FAILS=[]
def check(name, cond):
    print(("ok   " if cond else "FAIL ")+name)
    if not cond: FAILS.append(name)
check("module attributes", all(hasattr(engine,a) for a in ("engine_init","get_engine","Segment","process_buffer","compute_spec_bins_all","FX_EQ_PARAM","FX_DELAY_PARAM","MB_BANDS")))
assert engine.engine_init(48000)
hj=engine.get_engine()
check("is_running", hj.is_running())
check("diagnostic getters", hj.get_sample_rate()==48000 and hj.get_output_latency_s()>=0 and hj.get_xrun_count()>=0)
check("latency compensation API", hasattr(hj,"set_latency_compensation") and hasattr(hj,"get_latency_compensation_s"))
hj.set_latency_compensation(0.0); check("compensation off reads 0", hj.get_latency_compensation_s()==0.0)
tmp=tempfile.mkdtemp(); SR=48000; t=np.arange(SR*3)/SR
A=np.stack([0.3*np.sin(2*np.pi*400*t),0.3*np.sin(2*np.pi*500*t)],1); p=os.path.join(tmp,'a.wav'); write_wav(p,A,SR,'pcm16')
segs=[]
for (off,dur,pos) in ((0.0,0.5,0.0),(0.5,0.5,0.5),(1.0,0.5,1.5)):
    s=engine.Segment(); s.filepath=p; s.file_offset_s=off; s.duration_s=dur; s.timeline_pos_s=pos; s.volume=1.0; segs.append(s)
hj.clear_all_channels(); hj.set_channel_playlist(0,segs)
hj.set_volume(0,0.8); hj.set_mute(0,False); hj.set_solo(0,False); hj.set_pan(0,0.5)
hj.set_effect_slot(0,0,engine.FX_EQ_PARAM,[0.5]*7+[0.0]*14+[0.0]*3)
hj.play(0.0)
import time
o=[]
for i in range(200): o.append(run(480)); time.sleep(0.0007)
o=np.concatenate(o)
check("plays audio (peak %.3f)"%np.abs(o).max(), np.abs(o).max()>0.1)
check("playhead advances (%.3f s)"%hj.get_playhead_s(), hj.get_playhead_s()>0.05)
check("peak meter (%.3f)"%hj.get_meter_peak(0), hj.get_meter_peak(0)>0.0 or hj.get_meter_rms(0)>0.0)
st=hj.get_state(); print("state gr_levels:",st.get_gr_levels(0)[:2],"spec bins len",len(st.get_spec_bins(0)))
engine.compute_spec_bins_all(48000.0); print("spec max bin %.3f"%max(st.get_spec_bins(0)))
fx=st.get_effect_slot(0,0); check("get_effect_slot", fx.type==engine.FX_EQ_PARAM and fx.enabled and abs(fx.params[0]-0.5)<1e-6)
# offline process_buffer (AI rack path)
x=(0.3*np.sin(2*np.pi*300*np.arange(48000)/48000)).astype(np.float32); buf=np.stack([x,x],1).copy()
fx.enabled=False
y=engine.process_buffer(3,buf,48000); check("process_buffer passthrough", np.allclose(y,buf))
hj.set_effect_slot(3,0,engine.FX_DELAY_PARAM,[0.1,0.5,0.5,0.0,0.9]+[0.0]*19)
y=engine.process_buffer(3,buf,48000); check("process_buffer delay changes signal and is finite", (not np.allclose(y,buf)) and np.isfinite(y).all())
y2=engine.process_buffer(3,buf,48000); check("offline processing is deterministic (state reset)", np.allclose(y,y2))
hj.stop(); run(480,12)
engine.engine_shutdown()
print("RESULT","PASS" if not FAILS else "FAIL "+str(FAILS)); sys.exit(0 if not FAILS else 1)
