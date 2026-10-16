# Audio engine tests

Automatic checks for the C++ audio engine (`src/hijacker_audio_engine.cpp`,
`src/hijacker_processor.cpp`). They run the **real engine code** against a fake
PortAudio ("a sound card that isn't there"), so they need no audio hardware and
run the same on a developer laptop or a CI server.

```
python tests/engine/run_tests.py               # build + functional tests (about 10 s)
python tests/engine/run_tests.py --stress      # + concurrency stress run
python tests/engine/run_tests.py --sanitize    # + stress under AddressSanitizer/UBSan and ThreadSanitizer
python tests/engine/run_tests.py --pybind      # + build and exercise the real pybind11 module
python tests/engine/run_tests.py --only 02 06  # just some tests
```

Needs a C++17 compiler (`g++` or `clang++`; set `CXX` to choose), Python 3 and
`numpy` (`pip install numpy pybind11`). Linux and macOS. The Windows/MSVC build is
covered by `build.bat` and the CI workflow.

## What is checked

| Test | What it proves |
| --- | --- |
| `test_01_static_mix` | Several channels with fader/pan/volume match a reference mixer to within float rounding, for several device buffer sizes |
| `test_02_transport` | Play, stop, resume and seek: sample-accurate, no clicks, stop fades out, playhead freezes |
| `test_03_resampler` | 22.05 / 32 / 44.1 / 96 kHz files played at 48 kHz stay above 85 dB signal-to-error (measured ~95 dB) |
| `test_04_bad_input` | Missing, garbage, truncated, 8-bit, float64, 6-channel, NaN files and odd playlists never crash or glitch the other channels |
| `test_05_playlist_swap_during_play` | Replacing a channel's playlist hundreds of times during playback leaves other channels untouched |
| `test_06_file_release` | `clear_channel` / replacing a playlist has closed the old WAV by the time it returns (so Windows can delete it) |
| `test_07_many_strips` | 5000 strips on one channel play correctly (per-channel limit is `HJ_MAX_SEGMENTS`, 8192) |
| `test_08_peak_and_latency` | A peak between two meter polls is not lost; latency compensation shifts play/seek ahead by the right amount and the playhead reports the audible position |
| `stress_*` | A thread hammering the whole Python-facing API while the audio callback runs: no bad output, **no allocation in the audio callback**, and clean under ASan/UBSan/TSan |
| `pybind_module` | The real `hijacker_engine` Python module loads and its API works |

## Notes

* The tests pace the fake audio callback with short sleeps, so on a very busy
  machine a timing-based check can fail spuriously. Re-run before assuming a regression.
* `native/capi.cpp` exposes a small C interface so the Python tests can drive the
  engine with `ctypes`; `native/pa_fake.cpp` is the fake PortAudio.
* `native/tsan.supp` silences ThreadSanitizer for the effect-slot seqlock, which
  deliberately reads fields a writer may be changing and retries.
