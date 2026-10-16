#include "hijacker_audio_engine.h"
#include "pa_fake.h"
#include <thread>
#include <chrono>
#include <cstring>
extern "C" { EngineState g_state; }
static HijackerEngine* E() { if(!g_hijacker_engine) g_hijacker_engine=new HijackerEngine(); return g_hijacker_engine; }
extern "C" {
int  t_init(int sr){ return E()->init(sr)?1:0; }
void t_shutdown(){ if(g_hijacker_engine){ g_hijacker_engine->shutdown(); delete g_hijacker_engine; g_hijacker_engine=nullptr; } }
void t_set_rate_supported(int v){ g_fake_rate_supported = v!=0; }
void t_playlist(int ch,int n,const char** paths,const double* off,const double* dur,const double* pos,const float* vol){
    std::vector<HijackerSegment> v(n);
    for(int i=0;i<n;i++){ strncpy(v[i].filepath,paths[i],511); v[i].file_offset_s=off[i]; v[i].duration_s=dur[i]; v[i].timeline_pos_s=pos[i]; v[i].volume=vol[i]; }
    E()->set_channel_playlist(ch,v);
}
void t_clear(int ch){ E()->clear_channel(ch);} void t_clear_all(){ E()->clear_all_channels(); }
void t_play(double t){ E()->play(t);} void t_stop(){ E()->stop();} void t_seek(double t){ E()->seek(t);} 
void t_vol(int c,float v){E()->set_volume(c,v);} void t_mute(int c,int m){E()->set_mute(c,m!=0);} void t_solo(int c,int m){E()->set_solo(c,m!=0);} void t_pan(int c,float p){E()->set_pan(c,p);} 
void t_fx(int ch,int slot,int type,const float* p,int n){ E()->set_effect_slot(ch,slot,type,std::vector<float>(p,p+n)); }
void t_fx_clear(int ch,int slot){ E()->clear_effect_slot(ch,slot);} 
void t_run(int ncb,int frames,float* out,double* ph,int sleep_us){
    for(int i=0;i<ncb;i++){ fake_pa_run(out+(size_t)i*frames*2,frames,0); ph[i]=E()->get_playhead_s(); if(sleep_us>0) std::this_thread::sleep_for(std::chrono::microseconds(sleep_us)); }
}
void t_run_timed(int ncb,int frames,float* out,double* ph,double* ns,int sleep_us){
    for(int i=0;i<ncb;i++){ auto a=std::chrono::steady_clock::now(); fake_pa_run(out+(size_t)i*frames*2,frames,0); auto b=std::chrono::steady_clock::now(); ns[i]=std::chrono::duration<double,std::nano>(b-a).count(); ph[i]=E()->get_playhead_s(); if(sleep_us>0) std::this_thread::sleep_for(std::chrono::microseconds(sleep_us)); }
}
double t_playhead(){ return E()->get_playhead_s(); }
int t_xruns(){ return E()->get_xrun_count(); }
float t_rms(int ch){ return E()->get_meter_rms(ch);} float t_peak(int ch){ return E()->get_meter_peak(ch);} 
void t_comp(double s){ E()->set_latency_compensation(s);} double t_comp_get(){ return E()->get_latency_compensation_s(); }
double t_latency(){ return E()->get_output_latency_s(); }
int t_sr(){ return E()->get_sample_rate(); }
void t_reset_dsp(int ch){ g_state.eq_state[ch]=EqChannelState{}; }
}
