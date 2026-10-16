#pragma once
void fake_pa_run(float* out, unsigned long frames, unsigned long flags=0);
double fake_pa_rate();
extern bool g_fake_rate_supported;
void fake_pa_kill_stream();
int fake_pa_open_device(); unsigned long fake_pa_open_frames_per_buffer(); double fake_pa_open_suggested_latency();
