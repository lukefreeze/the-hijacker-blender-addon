#pragma once
void fake_pa_run(float* out, unsigned long frames, unsigned long flags=0);
double fake_pa_rate();
extern bool g_fake_rate_supported;
