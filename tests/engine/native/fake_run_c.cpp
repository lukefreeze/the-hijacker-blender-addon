#include "pa_fake.h"
extern "C" void fake_run(float* out, unsigned long frames){ fake_pa_run(out, frames, 0); }
