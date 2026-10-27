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

Needs a C++17 compiler (`g++` or `clang++`; set `CXX` to choose), Python 3,
`numpy` and `scipy` (`pip install numpy scipy pybind11`); `pip install bpy` additionally enables the keyframed-volume test. Linux and macOS. The Windows/MSVC build is
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
| `test_09_reverb_level` | Every reverb preset keeps a steady signal within a few dB of the dry level (the original engine was 12-21 dB louder) |
| `test_10_output_device` | Output devices are listed (stereo-capable only); a chosen device is opened by name, an unknown name falls back to the default; buffer size is honoured; a lost device is reported by `is_running()` |
| `test_11_master_bus` | The master fader scales the mix exactly (unity is bit-exact, 0.5 = -6 dB), moves without clicks, the master meter reads post-fader peaks (above 1.0 too), the clip latch sets above 0 dBFS and clears on request, and the safety limiter still keeps output below full scale |
| `test_12_offline_mix` | The offline renderer (used by Mixdown) produces what live playback produces: per channel and mixed, at other sample rates, with fader/pan/mute/solo/effects and the master fader, in repeatable runs; it refuses to run during playback and vice versa |
| `test_13_wav_writer` | `wav_writer.py` writes valid 16/24-bit PCM and 32-bit float WAV files (correct headers, dither on 16-bit, float written exactly, no clipping) that read back correctly |
| `test_14_loudness` | EBU R128 / BS.1770 loudness: a -23 dBFS 997 Hz sine reads -23.0 LUFS at 44.1/48/96 kHz; programme material matches an independent numpy implementation (integrated with both gates, loudness range, momentary/short-term maxima) to 0.03 LU; true peak finds inter-sample peaks (checked against an 8x oracle); measured after the master limiter; the live meter matches the same reference and resets on request |
| `test_15_volume_envelope` | Segment volume automation (`volume` -> `volume_end`): the gain follows the straight line exactly, chains of ramps join without clicks or dips, a gain step still gets its short fade, it works through the sample-rate converter and live playback matches the offline render |
| `test_16_channel_layouts` | Source channel layouts: a mono file plays identically on both sides at centre (8/16/24-bit/float), is bit-identical to the same signal in a stereo file and pans like any source; stereo is untouched; a 6-channel (5.1) file plays its front left/right pair; live equals offline |
| `stress_*` | A thread hammering the whole Python-facing API while the audio callback runs: no bad output, **no allocation in the audio callback**, and clean under ASan/UBSan/TSan |
| `pybind_module` | The real `hijacker_engine` Python module loads and its API works |
| `pybind/test_offline_vs_realtime` | Rendering a mix offline with `process_buffer` matches what the live audio callback produces (measured: bit-identical, about -212 dB) |
| `pybind/test_mixdown_rack` | The Mixdown rack's real render loop against the real engine module (Blender stubbed): mix and stem renders, sample rate and bit depth, custom frame range, master fader in the export, and the error path while playing |
| `pybind/test_strip_volume_bpy` | Keyframed strip volume, end to end with real Blender (`bpy`) and the real engine: the playlist follows the F-curve, playback = tone x curve, the fader/gain scale the keyframes and give the user's values back exactly (even after a trip to the fader floor), and quiet or zero strip values are never overwritten. Checked on Blender 4.3, 4.5 and 5.0 |

## Notes

* The tests pace the fake audio callback with short sleeps, so on a very busy
  machine a timing-based check can fail spuriously. Re-run before assuming a regression.
* `native/capi.cpp` exposes a small C interface so the Python tests can drive the
  engine with `ctypes`; `native/pa_fake.cpp` is the fake PortAudio.
* `native/tsan.supp` silences ThreadSanitizer for the effect-slot seqlock, which
  deliberately reads fields a writer may be changing and retries.
