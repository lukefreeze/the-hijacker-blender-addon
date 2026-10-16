import ctypes, numpy as np, struct, os, math, time, sys, wave
class HJ:
    def __init__(self, lib):
        self.l = ctypes.CDLL(lib)
        L=self.l
        L.t_init.argtypes=[ctypes.c_int]; L.t_init.restype=ctypes.c_int
        L.t_playlist.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.c_char_p),ctypes.POINTER(ctypes.c_double),ctypes.POINTER(ctypes.c_double),ctypes.POINTER(ctypes.c_double),ctypes.POINTER(ctypes.c_float)]
        for n in ("t_play","t_seek"): getattr(L,n).argtypes=[ctypes.c_double]
        L.t_vol.argtypes=[ctypes.c_int,ctypes.c_float]; L.t_pan.argtypes=[ctypes.c_int,ctypes.c_float]
        L.t_mute.argtypes=[ctypes.c_int,ctypes.c_int]; L.t_solo.argtypes=[ctypes.c_int,ctypes.c_int]
        L.t_fx.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.c_float),ctypes.c_int]
        L.t_run.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.POINTER(ctypes.c_float),ctypes.POINTER(ctypes.c_double),ctypes.c_int]
        L.t_playhead.restype=ctypes.c_double; L.t_latency.restype=ctypes.c_double; L.t_rms.restype=ctypes.c_float; L.t_rms.argtypes=[ctypes.c_int]
        L.t_peak.restype=ctypes.c_float; L.t_peak.argtypes=[ctypes.c_int]
    def init(self, sr, comp=0.0):
        r=self.l.t_init(sr)
        self.l.t_comp.argtypes=[ctypes.c_double]; self.l.t_comp_get.restype=ctypes.c_double
        self.l.t_comp(comp)      # tests assume exact timing unless they ask for compensation
        return r
    def playlist(self, ch, segs):
        n=len(segs)
        paths=(ctypes.c_char_p*n)(*[s['path'].encode() for s in segs])
        off=(ctypes.c_double*n)(*[s['off'] for s in segs]); dur=(ctypes.c_double*n)(*[s['dur'] for s in segs])
        pos=(ctypes.c_double*n)(*[s['pos'] for s in segs]); vol=(ctypes.c_float*n)(*[s.get('vol',1.0) for s in segs])
        self.l.t_playlist(ch,n,paths,off,dur,pos,vol)
    def run(self, ncb, frames, sleep_us=0):
        out=np.zeros(ncb*frames*2,dtype=np.float32); ph=np.zeros(ncb,dtype=np.float64)
        self.l.t_run(ncb,frames,out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),ph.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),sleep_us)
        return out.reshape(-1,2), ph
    def fx(self,ch,slot,typ,params):
        a=(ctypes.c_float*len(params))(*params); self.l.t_fx(ch,slot,typ,a,len(params))

def write_wav(path, data, sr, fmt='pcm16', extensible=False, extra_chunk=False, nch_override=None):
    """data float array [n,ch] in -1..1"""
    data=np.atleast_2d(data.T).T if data.ndim==1 else data
    n,ch=data.shape
    if fmt=='pcm8': raw=(np.clip(np.round(data*127.0),-128,127)+128).astype(np.uint8).tobytes(); bps=8; tag=1
    elif fmt=='pcm16': raw=np.clip(np.round(data*32767.0),-32768,32767).astype('<i2').tobytes(); bps=16; tag=1
    elif fmt=='pcm24':
        v=np.clip(np.round(data*8388607.0),-8388608,8388607).astype('<i4'); b=v.view(np.uint8).reshape(-1,4)[:,:3]; raw=b.tobytes(); bps=24; tag=1
    elif fmt=='pcm32': raw=np.clip(np.round(data.astype(np.float64)*2147483647.0),-2147483648,2147483647).astype('<i4').tobytes(); bps=32; tag=1
    elif fmt=='f32': raw=data.astype('<f4').tobytes(); bps=32; tag=3
    elif fmt=='f64': raw=data.astype('<f8').tobytes(); bps=64; tag=3
    ba=ch*bps//8
    if extensible:
        fmtc=struct.pack('<HHIIHHHHIH14s',0xFFFE,ch,sr,sr*ba,ba,bps,22,bps,3,tag,b'\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71')
    else:
        fmtc=struct.pack('<HHIIHH',tag,ch,sr,sr*ba,ba,bps)
    body=b'WAVE'+b'fmt '+struct.pack('<I',len(fmtc))+fmtc
    if extra_chunk: body+=b'LIST'+struct.pack('<I',5)+b'abcde'+b'\x00'
    body+=b'data'+struct.pack('<I',len(raw))+raw
    if len(raw)&1: body+=b'\x00'
    open(path,'wb').write(b'RIFF'+struct.pack('<I',len(body))+body)

def quant(data, fmt):
    """what the engine should read back for given float data and wav fmt"""
    if fmt=='pcm8': return (np.clip(np.round(data*127.0),-128,127))/128.0
    if fmt=='pcm16': return np.clip(np.round(data*32767.0),-32768,32767)/32768.0
    if fmt=='pcm24': return np.clip(np.round(data*8388607.0),-8388608,8388607)/8388608.0
    if fmt=='pcm32': return np.clip(np.round(data.astype(np.float64)*2147483647.0),-2147483648,2147483647)/2147483648.0
    if fmt=='f32': return data.astype(np.float32).astype(np.float64)
    if fmt=='f64': return data.astype(np.float32).astype(np.float64)
    raise ValueError

EDGE=48; DECL=96
def ref_channel(file, segs, sr, total):
    """reference timeline for one channel: file [n,2] (decoded), segs list of dict(off,dur,pos,vol)"""
    segs=sorted(segs,key=lambda s:s['pos'])
    for s in segs: s['fi']=True; s['fo']=True
    for a,b in zip(segs,segs[1:]):
        if a['path']==b['path'] and abs(a['pos']+a['dur']-b['pos'])<1e-4 and abs(a['off']+a['dur']-b['off'])<1e-4 and abs(a.get('vol',1)-b.get('vol',1))<1e-6:
            a['fo']=False; b['fi']=False
    tl=np.zeros((total,2))
    for s in segs:
        s0=int(round(s['pos']*sr)); s1=int(round((s['pos']+s['dur'])*sr)); L=s1-s0
        if L<=0: continue
        half=min(L//2,EDGE)
        k=np.arange(L); src=int(round(s['off']*sr))+k
        ok=(src>=0)&(src<len(file)); v=np.zeros((L,2)); v[ok]=file[src[ok]]
        ein=np.ones(L); eout=np.ones(L)
        if s['fi']:
            m=k<half; ein[m]=(k[m]+1)/(half+1)
        if s['fo']:
            rem=L-1-k; m=rem<half; eout[m]=(rem[m]+1)/(half+1)
        g=np.minimum(ein,eout)*s.get('vol',1.0)
        lo=max(s0,0); hi=min(s1,total)
        if hi>lo: tl[lo:hi]+= (v*g[:,None])[lo-s0:hi-s0]
    return tl

def pan_gains(p):
    if p>=0.5: return math.cos((p-0.5)*math.pi),1.0
    return 1.0, math.cos((0.5-p)*math.pi)

def limiter(x):
    y=x.copy(); m=y>0.95; y[m]=0.95+(y[m]-0.95)/(1+(y[m]-0.95)*20); m=y<-0.95; y[m]=-(0.95+(-y[m]-0.95)/(1+(-y[m]-0.95)*20)); return y
def limiter_old(x):
    y=x.copy(); m=y>0.95; y[m]=0.95+(y[m]-0.95)/(1+(y[m]-0.95)); m=y<-0.95; y[m]=-(0.95+(-y[m]-0.95)/(1+(-y[m]-0.95))); return y
