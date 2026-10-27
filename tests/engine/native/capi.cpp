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
void t_playlist_ramp(int ch,int n,const char** paths,const double* off,const double* dur,const double* pos,const float* vol,const float* vol_end){
    std::vector<HijackerSegment> v(n);
    for(int i=0;i<n;i++){ strncpy(v[i].filepath,paths[i],511); v[i].file_offset_s=off[i]; v[i].duration_s=dur[i]; v[i].timeline_pos_s=pos[i]; v[i].volume=vol[i]; v[i].volume_end=vol_end[i]; }
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
void  t_master(float g){ E()->set_master_gain(g);} float t_master_get(){ return E()->get_master_gain();}
float t_master_peak(){ return E()->get_master_peak();} int t_master_clip(){ return E()->get_master_clip()?1:0;}
void  t_master_clear(){ E()->clear_master_clip();}
int   t_off_begin(double start_s,int sr,unsigned mask,int respect,int master){ return E()->offline_begin(start_s,sr,mask,respect!=0,master!=0)?1:0;}
int   t_off_render(float* out,int n){ return E()->offline_render(out,n);}
void  t_loud_reset(){ E()->reset_loudness();}
// out[9]: momentary, short_term, integrated, lra, momentary_max, short_term_max, true_peak_db, sample_peak_db, duration_s
static void loud_out(const HijackerLoudness& l, double* o){ o[0]=l.momentary;o[1]=l.short_term;o[2]=l.integrated;o[3]=l.lra;o[4]=l.momentary_max;o[5]=l.short_term_max;o[6]=l.true_peak_db;o[7]=l.sample_peak_db;o[8]=l.duration_s;}
void  t_loud_get(double* o){ loud_out(E()->get_loudness(), o);}
void  t_off_loud(double* o){ loud_out(E()->offline_loudness(), o);}
void  t_off_end(){ E()->offline_end();} int t_off_active(){ return E()->offline_active()?1:0;} 
void t_comp(double s){ E()->set_latency_compensation(s);} double t_comp_get(){ return E()->get_latency_compensation_s(); }
double t_latency(){ return E()->get_output_latency_s(); }
int t_sr(){ return E()->get_sample_rate(); }

void t_set_device(const char* name){ E()->set_output_device(name?name:""); }
int  t_init2(int sr,int frames){ return E()->init(sr,frames)?1:0; }
int  t_list_devices(char* buf,int cap){ auto v=hj_list_output_devices(); std::string s; for(auto&d:v){ s+=d.name; s+=d.is_default?"|1\n":"|0\n"; } strncpy(buf,s.c_str(),cap-1); buf[cap-1]=0; return (int)v.size(); }
int  t_device_name(char* buf,int cap){ std::string n=E()->get_output_device_name(); strncpy(buf,n.c_str(),cap-1); buf[cap-1]=0; return (int)n.size(); }
int  t_buffer_frames(){ return E()->get_buffer_frames(); }
int  t_is_running(){ return E()->is_running()?1:0; }
void t_kill_stream(){ fake_pa_kill_stream(); }
int  t_open_dev(){ return fake_pa_open_device(); }
int  t_open_fpb(){ return (int)fake_pa_open_frames_per_buffer(); }
double t_open_latency(){ return fake_pa_open_suggested_latency(); }
void t_reset_dsp(int ch){ g_state.eq_state[ch]=EqChannelState{}; }
}
