// Concurrency stress: audio thread callback loop vs a "python" thread hammering the whole API.
#include "hijacker_audio_engine.h"
#include "pa_fake.h"
#include <thread>
#include <atomic>
#include <random>
#include <cstdio>
#include <cmath>
#include <chrono>
#include <vector>
#include <string>
#include <cstring>
#include <cstdlib>
extern "C" { EngineState g_state; }
static std::atomic<long> g_allocs_in_cb{0};
static thread_local bool t_in_cb=false;
#ifdef COUNT_ALLOCS
void* operator new(size_t n){ if(t_in_cb) g_allocs_in_cb++; void* p=malloc(n?n:1); if(!p) throw std::bad_alloc(); return p; }
void* operator new[](size_t n){ if(t_in_cb) g_allocs_in_cb++; void* p=malloc(n?n:1); if(!p) throw std::bad_alloc(); return p; }
void operator delete(void* p) noexcept { free(p);} void operator delete[](void* p) noexcept { free(p);} 
void operator delete(void* p,size_t) noexcept { free(p);} void operator delete[](void* p,size_t) noexcept { free(p);} 
#endif
static void write_wav16(const std::string& path,int sr,int nch,const std::vector<int16_t>& d){
    FILE* f=fopen(path.c_str(),"wb"); uint32_t dsz=d.size()*2, riff=36+dsz; uint16_t tag=1,ch=nch,ba=nch*2,bps=16; uint32_t br=sr*ba,u16=16,usr=sr;
    fwrite("RIFF",1,4,f);fwrite(&riff,4,1,f);fwrite("WAVEfmt ",1,8,f);fwrite(&u16,4,1,f);fwrite(&tag,2,1,f);fwrite(&ch,2,1,f);fwrite(&usr,4,1,f);fwrite(&br,4,1,f);fwrite(&ba,2,1,f);fwrite(&bps,2,1,f);fwrite("data",1,4,f);fwrite(&dsz,4,1,f);fwrite(d.data(),2,d.size(),f);fclose(f);}
int main(int argc,char**argv){
    double seconds = argc>1? atof(argv[1]):5.0; const int SR=48000;
    std::vector<std::string> files; std::mt19937 rng(42);
    for(int k=0;k<6;k++){ int sr = (k==4)?44100:SR; int nch=(k%2)+1; int n=sr*6; std::vector<int16_t> d(n*nch);
        for(int i=0;i<n;i++) for(int c=0;c<nch;c++) d[i*nch+c]=(int16_t)(9000*std::sin(2*M_PI*(200+90*k+30*c)*i/sr));
        std::string p="/tmp/hj_stress_"+std::to_string(k)+".wav"; write_wav16(p,sr,nch,d); files.push_back(p);} 
    HijackerEngine* E=new HijackerEngine(); g_hijacker_engine=E; if(!E->init(SR)) {printf("init fail\n");return 1;}
    std::atomic<bool> stop{false}; std::atomic<long> ncb{0}, bad{0}; std::atomic<int> frames_cfg{480};
    std::thread audio([&]{
        std::vector<float> out(4096*2);
        while(!stop){ int fr=frames_cfg.load(); t_in_cb=true; fake_pa_run(out.data(),fr,0); t_in_cb=false; ncb++;
            for(int i=0;i<fr*2;i++){ float v=out[i]; if(!(v==v) || v>1.0f || v<-1.0f){ bad++; break; } }
            std::this_thread::sleep_for(std::chrono::microseconds(fr*1000000LL/SR/8)); } }); // 8x realtime
    std::thread meters([&]{ while(!stop){ for(int c=0;c<8;c++){ (void)E->get_meter_rms(c); (void)E->get_meter_peak(c);} (void)E->get_playhead_s(); (void)E->get_xrun_count(); std::this_thread::sleep_for(std::chrono::milliseconds(3)); } });
    std::mt19937 r(7); auto rnd=[&](int n){ return (int)(r()%n); }; auto rf=[&](){ return (r()%1000)/1000.0f; };
    auto t0=std::chrono::steady_clock::now(); long ops=0;
    while(std::chrono::duration<double>(std::chrono::steady_clock::now()-t0).count()<seconds){
        int op=rnd(14); int ch=rnd(8);
        switch(op){
        case 0: case 1: { int n=1+rnd(6); std::vector<HijackerSegment> v(n); double pos=rf()*2; for(int i=0;i<n;i++){ strcpy(v[i].filepath,files[rnd(6)].c_str()); v[i].file_offset_s=rf()*3; v[i].duration_s=0.05+rf()*1.5; v[i].timeline_pos_s=pos; pos+=v[i].duration_s*(rnd(3)?1.0:1.3); v[i].volume=0.2f+rf(); } E->set_channel_playlist(ch,v);} break;
        case 2: E->clear_channel(ch); break;
        case 3: E->play(rf()*4); break;
        case 4: E->seek(rf()*4); break;
        case 5: E->stop(); break;
        case 6: E->set_volume(ch,rf()*1.5f); break;
        case 7: E->set_pan(ch,rf()); break;
        case 8: E->set_mute(ch,rnd(2)); break;
        case 9: E->set_solo(ch,rnd(4)==0); break;
        case 10: { int types[]={1,3,4,6,7,8,9}; int ty=types[rnd(7)]; std::vector<float> p(24); for(auto& x:p) x=rf(); E->set_effect_slot(ch,rnd(8),ty,p);} break;
        case 11: E->clear_effect_slot(ch,rnd(8)); break;
        case 12: frames_cfg=(int[]){64,480,512,1024,1500,2048}[rnd(6)]; break;
        case 13: E->clear_all_channels(); break;
        }
        ops++; std::this_thread::sleep_for(std::chrono::microseconds(200+rnd(1500)));
    }
    stop=true; audio.join(); meters.join();
    E->shutdown(); delete E;
    printf("stress done: %ld api ops, %ld callbacks, %ld bad-output callbacks, allocations in callback: %ld\n",ops,(long)ncb,(long)bad,(long)g_allocs_in_cb);
    return bad? 2:0;
}
