#pragma once
// Minimal fake of the PortAudio API surface used by hijacker_audio_engine.cpp
typedef int PaError; typedef int PaDeviceIndex; typedef int PaHostApiIndex; typedef void PaStream;
typedef unsigned long PaStreamCallbackFlags; typedef double PaTime;
enum { paNoError = 0, paNoDevice = -1, paFormatIsSupported = 0, paContinue = 0 };
enum { paMME = 2, paWASAPI = 13 };
enum { paFloat32 = 1 };
enum { paFramesPerBufferUnspecified = 0 };
enum { paClipOff = 1 };
enum : unsigned long { paOutputUnderflow = 4, paOutputOverflow = 8 };
typedef struct { int type; const char* name; PaDeviceIndex defaultOutputDevice; } PaHostApiInfo;
typedef struct { const char* name; double defaultSampleRate; PaTime defaultLowOutputLatency, defaultHighOutputLatency; } PaDeviceInfo;
typedef struct { PaDeviceIndex device; int channelCount; int sampleFormat; PaTime suggestedLatency; void* hostApiSpecificStreamInfo; } PaStreamParameters;
typedef struct { int structVersion; PaTime inputLatency, outputLatency; double sampleRate; } PaStreamInfo;
typedef struct { PaTime inputBufferAdcTime, currentTime, outputBufferDacTime; } PaStreamCallbackTimeInfo;
typedef int PaStreamCallback(const void*, void*, unsigned long, const PaStreamCallbackTimeInfo*, PaStreamCallbackFlags, void*);
extern "C++" {
PaError Pa_Initialize(); PaError Pa_Terminate(); const char* Pa_GetErrorText(PaError);
int Pa_GetHostApiCount(); const PaHostApiInfo* Pa_GetHostApiInfo(int);
PaDeviceIndex Pa_GetDefaultOutputDevice(); const PaDeviceInfo* Pa_GetDeviceInfo(PaDeviceIndex);
PaError Pa_IsFormatSupported(const PaStreamParameters*, const PaStreamParameters*, double);
PaError Pa_OpenStream(PaStream**, const PaStreamParameters*, const PaStreamParameters*, double, unsigned long, unsigned long, PaStreamCallback*, void*);
PaError Pa_StartStream(PaStream*); PaError Pa_StopStream(PaStream*); PaError Pa_CloseStream(PaStream*);
int Pa_IsStreamActive(PaStream*); const PaStreamInfo* Pa_GetStreamInfo(PaStream*);
}
