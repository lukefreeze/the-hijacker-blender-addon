#include <portaudio.h>
#include <cstring>
#include "pa_fake.h"
// A fake PortAudio with three output devices (plus one input-only device that must never be listed).
static PaStreamCallback* g_cb=nullptr; static void* g_ud=nullptr; static bool g_active=false;
static PaHostApiInfo g_api={paWASAPI,"FakeAPI",0};
static PaDeviceInfo g_devs[4];
static PaStreamInfo g_si={1,0,0.04,48000.0}; static double g_rate=48000;
static int g_open_dev=-1; static unsigned long g_open_fpb=0; static double g_open_latency=0;
bool g_fake_rate_supported=true;
static bool g_inited=false;
static void init_devs(){
    if(g_inited) return;
    g_inited=true;
    const char* names[4]={"FakeDevice","Fake Headphones","Fake Speakers","Fake Microphone"};
    for(int i=0;i<4;i++){ PaDeviceInfo d; memset(&d,0,sizeof d); d.structVersion=2; d.name=names[i]; d.hostApi=0;
        d.maxInputChannels=(i==3)?2:0; d.maxOutputChannels=(i==3)?0:2; d.defaultLowOutputLatency=0.01; d.defaultHighOutputLatency=0.04; d.defaultSampleRate=48000.0; g_devs[i]=d; }
}
PaError Pa_Initialize(){init_devs();return 0;} PaError Pa_Terminate(){return 0;} const char* Pa_GetErrorText(PaError){return "err";}
int Pa_GetHostApiCount(){return 1;} const PaHostApiInfo* Pa_GetHostApiInfo(int){return &g_api;}
PaDeviceIndex Pa_GetDefaultOutputDevice(){return 0;} int Pa_GetDeviceCount(){return 4;}
const PaDeviceInfo* Pa_GetDeviceInfo(PaDeviceIndex i){init_devs(); return (i>=0&&i<4)?&g_devs[i]:nullptr;}
PaError Pa_IsFormatSupported(const PaStreamParameters*,const PaStreamParameters*,double){return g_fake_rate_supported?0:-1;}
PaError Pa_OpenStream(PaStream** s,const PaStreamParameters*,const PaStreamParameters* out,double sr,unsigned long fpb,unsigned long,PaStreamCallback* cb,void* ud){
    g_cb=cb;g_ud=ud;g_rate=sr;*s=(PaStream*)&g_cb; g_open_dev=out?out->device:-1; g_open_fpb=fpb; g_open_latency=out?out->suggestedLatency:0; return 0;}
PaError Pa_StartStream(PaStream*){g_active=true;return 0;} PaError Pa_StopStream(PaStream*){g_active=false;return 0;} PaError Pa_CloseStream(PaStream*){return 0;}
int Pa_IsStreamActive(PaStream*){return g_active?1:0;} const PaStreamInfo* Pa_GetStreamInfo(PaStream*){return &g_si;}
void fake_pa_run(float* out,unsigned long frames,unsigned long flags){ PaStreamCallbackTimeInfo ti={0,0,0}; g_cb(nullptr,out,frames,&ti,flags,g_ud);} 
double fake_pa_rate(){return g_rate;}
void fake_pa_kill_stream(){ g_active=false; }          // simulates the device being unplugged
int fake_pa_open_device(){return g_open_dev;} unsigned long fake_pa_open_frames_per_buffer(){return g_open_fpb;} double fake_pa_open_suggested_latency(){return g_open_latency;}
