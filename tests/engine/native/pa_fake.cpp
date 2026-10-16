#include <portaudio.h>
#include <cstring>
#include "pa_fake.h"
static PaStreamCallback* g_cb=nullptr; static void* g_ud=nullptr; static bool g_active=false;
static PaHostApiInfo g_api={paWASAPI,"FakeAPI",0}; static PaDeviceInfo g_dev={"FakeDevice",48000.0,0.01,0.04};
static PaStreamInfo g_si={1,0,0.04,48000.0}; static double g_rate=48000;
bool g_fake_rate_supported=true;
PaError Pa_Initialize(){return 0;} PaError Pa_Terminate(){return 0;} const char* Pa_GetErrorText(PaError){return "err";}
int Pa_GetHostApiCount(){return 1;} const PaHostApiInfo* Pa_GetHostApiInfo(int){return &g_api;}
PaDeviceIndex Pa_GetDefaultOutputDevice(){return 0;} const PaDeviceInfo* Pa_GetDeviceInfo(PaDeviceIndex){return &g_dev;}
PaError Pa_IsFormatSupported(const PaStreamParameters*,const PaStreamParameters*,double){return g_fake_rate_supported?0:-1;}
PaError Pa_OpenStream(PaStream** s,const PaStreamParameters*,const PaStreamParameters*,double sr,unsigned long,unsigned long,PaStreamCallback* cb,void* ud){g_cb=cb;g_ud=ud;g_rate=sr;*s=(PaStream*)&g_cb;return 0;}
PaError Pa_StartStream(PaStream*){g_active=true;return 0;} PaError Pa_StopStream(PaStream*){g_active=false;return 0;} PaError Pa_CloseStream(PaStream*){return 0;}
int Pa_IsStreamActive(PaStream*){return g_active?1:0;} const PaStreamInfo* Pa_GetStreamInfo(PaStream*){return &g_si;}
void fake_pa_run(float* out,unsigned long frames,unsigned long flags){ PaStreamCallbackTimeInfo ti={0,0,0}; g_cb(nullptr,out,frames,&ti,flags,g_ud);} 
double fake_pa_rate(){return g_rate;}
