// =============================================================================
// wrapper.cpp — The Hijacker pybind11 Python bindings
// =============================================================================
#include <pybind11/pybind11.h>
#include <string>
#include <pybind11/stl.h>
#include <pybind11/numpy.h>
#include "mixer_ui.h"
#include "hijacker_processor.h"
#include "hijacker_audio_engine.h"

namespace py = pybind11;

// g_state — global EngineState shared between wrapper.cpp and
// hijacker_processor.cpp (declared there as: extern "C" { extern EngineState g_state; })
// HijackerEngine::get_state() returns &g_state so DSP state is centralised.
extern "C" { EngineState g_state; }

// Spectrum compute function — defined in hijacker_processor.cpp.
// Declared at file scope (extern "C" must be at global scope, not inside a function).
// Called from Python meter timer to run Goertzel analysis off the audio thread.
extern "C" void compute_spec_bins_all(float sample_rate);

// ---------------------------------------------------------------------------
// Legacy batch DSP entry point — kept for offline processing
// (AI rack outputs, BOOSTER, DeepFilterNet etc. still use this)
// ---------------------------------------------------------------------------
py::array_t<float> process_buffer(int channel_idx,
                                   py::array_t<float, py::array::c_style> samples,
                                   int sample_rate)
{
    if (channel_idx < 0 || channel_idx >= PB_MAX_CHANNELS)
        throw std::invalid_argument("channel_idx out of range");

    py::buffer_info info = samples.request();
    if (info.ndim != 2)
        throw std::runtime_error("samples must be 2D array (n_frames, n_channels)");

    int n_frames   = (int)info.shape[0];
    int n_channels = (int)info.shape[1];
    float sr       = (float)sample_rate;

    py::array_t<float> output({n_frames, n_channels});
    py::buffer_info out_info = output.request();
    float* out_ptr = (float*)out_info.ptr;
    float* in_ptr  = (float*)info.ptr;

    int total = n_frames * n_channels;
    for (int i = 0; i < total; ++i) out_ptr[i] = in_ptr[i];

    // Require engine to be initialised before batch processing
    if (!g_hijacker_engine) {
        printf("[HIJACKER] process_buffer called before engine_init — returning unchanged\n");
        return output;
    }

    {
        EngineState* st = g_hijacker_engine->get_state();
        // Start from clean DSP memory (filters, envelopes, reverb and delay
        // tails). hj_reset_channel_dsp() clears the large reverb state in place
        // rather than building a ~350 KB temporary on the stack.
        hj_reset_channel_dsp(channel_idx);
        st->eq_state[channel_idx]     = EqChannelState{};
        for (int b = 0; b < PB_MB_BANDS; ++b)
            st->fft_state[channel_idx][b] = FFTBandState{};

        float saved_vol = st->volumes[channel_idx];
        st->volumes[channel_idx] = 1.0f;

        const int CHUNK = 1024;
        for (int offset = 0; offset < n_frames; offset += CHUNK) {
            int chunk_frames = std::min(CHUNK, n_frames - offset);
            float* chunk_ptr = out_ptr + offset * n_channels;
            apply_effect_chain_batch(channel_idx, chunk_ptr,
                                      chunk_frames, n_channels, sr);
        }

        st->volumes[channel_idx] = saved_vol;
    }

    printf("[HIJACKER] ch%d batch processed %d frames @ %dHz\n",
           channel_idx, n_frames, sample_rate);
    return output;
}

// ---------------------------------------------------------------------------
// Engine lifecycle helpers
// ---------------------------------------------------------------------------
static bool engine_init(int sample_rate, const std::string& device_name, int buffer_frames) {
    if (!g_hijacker_engine)
        g_hijacker_engine = new HijackerEngine();
    g_hijacker_engine->set_output_device(device_name);
    return g_hijacker_engine->init(sample_rate, buffer_frames);
}

static py::list list_output_devices() {
    py::list out;
    for (const HijackerOutputDevice& d : hj_list_output_devices()) {
        py::dict e;
        e["index"]               = d.index;
        e["name"]                = d.name;
        e["host_api"]            = d.host_api;
        e["max_output_channels"] = d.max_output_channels;
        e["default_sample_rate"] = d.default_sample_rate;
        e["low_latency_s"]       = d.low_latency_s;
        e["high_latency_s"]      = d.high_latency_s;
        e["is_default"]          = d.is_default;
        out.append(e);
    }
    return out;
}

static void engine_shutdown() {
    if (g_hijacker_engine) {
        g_hijacker_engine->shutdown();
        delete g_hijacker_engine;
        g_hijacker_engine = nullptr;
    }
}

static HijackerEngine* get_engine() {
    if (!g_hijacker_engine)
        g_hijacker_engine = new HijackerEngine();
    return g_hijacker_engine;
}

// HIJACKER_MODULE_NAME lets a differently-named build (e.g.
// hijacker_engine_bl5_2.pyd, compiled for a specific Blender version
// per core/engine.py's naming convention) export the matching
// PyInit_<name> symbol Python's import machinery looks for -- it
// must match the .pyd's filename, not just the module's internal
// name. build.bat doesn't define it, so it defaults to the original
// "hijacker_engine" name and behaves exactly as before. A
// version-specific build script defines it via /D on the command
// line (see build_bl5_2.bat).
#ifndef HIJACKER_MODULE_NAME
#define HIJACKER_MODULE_NAME hijacker_engine
#endif

static py::dict loudness_dict(const HijackerLoudness& l) {
    py::dict d;
    d["momentary"] = l.momentary;       d["short_term"] = l.short_term;
    d["integrated"] = l.integrated;     d["lra"] = l.lra;
    d["momentary_max"] = l.momentary_max; d["short_term_max"] = l.short_term_max;
    d["true_peak_db"] = l.true_peak_db; d["sample_peak_db"] = l.sample_peak_db;
    d["duration_s"] = l.duration_s;
    return d;
}

PYBIND11_MODULE(HIJACKER_MODULE_NAME, m)
{
    m.doc() = "The Hijacker — audio engine for Blender";

    // ── Engine lifecycle ──────────────────────────────────────────────────
    m.def("engine_init",     &engine_init,     py::arg("sample_rate") = 44100,
          py::arg("device_name") = std::string(), py::arg("buffer_frames") = 0,
          "Initialise PortAudio and start the audio thread. device_name: output device "
          "(empty = system default); buffer_frames: 0 = driver's choice");
    m.def("list_output_devices", &list_output_devices,
          "Stereo-capable output devices: list of dicts (index, name, host_api, "
          "max_output_channels, default_sample_rate, low_latency_s, high_latency_s, is_default)");
    m.def("engine_shutdown", &engine_shutdown,
          "Stop the audio thread and release PortAudio");
    m.def("get_engine",      &get_engine,
          py::return_value_policy::reference,
          "Return the global HijackerEngine instance");

    // ── Spectrum compute (call from Python timer, NOT audio thread) ──────
    m.def("compute_spec_bins_all",
          [](float sr){ compute_spec_bins_all(sr); },
          py::arg("sample_rate") = 48000.0f,
          "Run Goertzel spectrum analysis for all channels. "
          "Call from Python meter timer, not from the audio callback.");

    // ── Legacy batch DSP (offline processing) ────────────────────────────
    m.def("process_buffer", &process_buffer,
        py::arg("channel_idx"), py::arg("samples"), py::arg("sample_rate"),
        "Offline batch DSP — used by BOOSTER, DeepFilterNet etc.");

    // ── Effect type constants ─────────────────────────────────────────────
    m.attr("FX_NONE")         = (int)EffectType::NONE;
    m.attr("FX_GAIN")         = (int)EffectType::GAIN;
    m.attr("FX_EQ_PARAM")     = (int)EffectType::EQ_PARAM;
    m.attr("FX_COMP_SINGLE")  = (int)EffectType::COMP_SINGLE;
    m.attr("FX_COMP_MULTI")   = (int)EffectType::COMP_MULTI;
    m.attr("FX_REVERB_PARAM") = (int)EffectType::REVERB_PARAM;
    m.attr("FX_GATE_PARAM")   = (int)EffectType::GATE_PARAM;
    m.attr("FX_DELAY_PARAM")  = (int)EffectType::DELAY_PARAM;
    m.attr("MB_BANDS")        = PB_MB_BANDS;

    // ── HijackerSegment ───────────────────────────────────────────────────
    py::class_<HijackerSegment>(m, "Segment")
        .def(py::init<>())
        .def_property("filepath",
            [](const HijackerSegment& s){ return std::string(s.filepath); },
            [](HijackerSegment& s, const std::string& p){
                strncpy(s.filepath, p.c_str(), 511); s.filepath[511] = 0; })
        .def_readwrite("file_offset_s",  &HijackerSegment::file_offset_s)
        .def_readwrite("duration_s",     &HijackerSegment::duration_s)
        .def_readwrite("timeline_pos_s", &HijackerSegment::timeline_pos_s)
        .def_readwrite("volume",         &HijackerSegment::volume)
        .def_readwrite("volume_end",     &HijackerSegment::volume_end);

    // ── HijackerEngine ────────────────────────────────────────────────────
    py::class_<HijackerEngine>(m, "Engine")
        // Transport
        .def("play",  &HijackerEngine::play,  py::arg("timeline_pos_s"))
        .def("stop",  &HijackerEngine::stop)
        .def("seek",  &HijackerEngine::seek,  py::arg("timeline_pos_s"))
        // Channel setup
        .def("set_channel_playlist", &HijackerEngine::set_channel_playlist,
             py::arg("channel"), py::arg("segments"))
        .def("clear_channel",     &HijackerEngine::clear_channel,
             py::arg("channel"))
        .def("clear_all_channels",&HijackerEngine::clear_all_channels)
        // Mixer
        .def("set_volume", &HijackerEngine::set_volume,
             py::arg("channel"), py::arg("volume"))
        .def("set_mute",   &HijackerEngine::set_mute,
             py::arg("channel"), py::arg("muted"))
        .def("set_solo",   &HijackerEngine::set_solo,
             py::arg("channel"), py::arg("soloed"))
        .def("set_pan",    &HijackerEngine::set_pan,
             py::arg("channel"), py::arg("pan"))
        // Effects
        .def("set_effect_slot", &HijackerEngine::set_effect_slot,
             py::arg("channel"), py::arg("slot"),
             py::arg("type"), py::arg("params"))
        .def("clear_effect_slot", &HijackerEngine::clear_effect_slot,
             py::arg("channel"), py::arg("slot"))
        // Metering
        .def("get_meter_rms",  &HijackerEngine::get_meter_rms,
             py::arg("channel"))
        .def("get_meter_peak", &HijackerEngine::get_meter_peak,
             py::arg("channel"))
        .def("get_playhead_s", &HijackerEngine::get_playhead_s)
        .def("offline_begin",
             [](HijackerEngine& e, double start_s, int sample_rate, const std::vector<int>& channels,
                bool respect_mute_solo, bool apply_master) {
                 unsigned mask = 0;
                 for (int c : channels) if (c >= 0 && c < PB_MAX_CHANNELS) mask |= (1u << c);
                 return e.offline_begin(start_s, sample_rate, mask, respect_mute_solo, apply_master);
             },
             py::arg("start_s"), py::arg("sample_rate"), py::arg("channels"),
             py::arg("respect_mute_solo") = true, py::arg("apply_master") = true,
             "Start an offline render of the given channels (list of ints) using the live mix code. "
             "Returns False if playback is running.")
        .def("offline_render",
             [](HijackerEngine& e, int n_frames) {
                 if (n_frames <= 0 || n_frames > (1 << 24))
                     throw std::invalid_argument("n_frames out of range");
                 py::array_t<float> a({n_frames, 2});
                 e.offline_render(a.mutable_data(), n_frames);
                 return a;
             },
             py::arg("n_frames"), "Render the next n_frames; returns a (n_frames, 2) float32 array")
        .def("offline_end",    &HijackerEngine::offline_end)
        .def("offline_active", &HijackerEngine::offline_active)
        .def("set_master_gain",  &HijackerEngine::set_master_gain, py::arg("linear"))
        .def("get_master_gain",  &HijackerEngine::get_master_gain)
        .def("get_master_peak",  &HijackerEngine::get_master_peak)
        .def("get_master_clip",  &HijackerEngine::get_master_clip)
        .def("clear_master_clip",&HijackerEngine::clear_master_clip)
        .def("get_loudness", [](const HijackerEngine& e) { return loudness_dict(e.get_loudness()); },
             "Loudness of the master output since the last reset (LUFS / LU / dBFS / dBTP; -200 = no reading yet)")
        .def("reset_loudness", &HijackerEngine::reset_loudness)
        .def("offline_loudness", [](const HijackerEngine& e) { return loudness_dict(e.offline_loudness()); },
             "Loudness of the offline render (during it, or after offline_end until the next offline_begin)")
        // Diagnostics (new): the rate the engine really opened the device at,
        // the device's reported output latency in seconds, and a running count
        // of audio dropouts (device underflows + disk-thread underruns).
        .def("get_sample_rate",       &HijackerEngine::get_sample_rate)
        .def("get_output_latency_s",  &HijackerEngine::get_output_latency_s)
        .def("get_xrun_count",        &HijackerEngine::get_xrun_count)
        .def("get_output_device_name", &HijackerEngine::get_output_device_name)
        .def("get_buffer_frames",      &HijackerEngine::get_buffer_frames)
        .def("set_latency_compensation",   &HijackerEngine::set_latency_compensation,
             py::arg("seconds"),
             "seconds < 0: automatic (default), 0: off, > 0: fixed offset in seconds")
        .def("get_latency_compensation_s", &HijackerEngine::get_latency_compensation_s)
        .def("get_state",      &HijackerEngine::get_state,
             py::return_value_policy::reference)
        .def("is_running",     &HijackerEngine::is_running);

    // ── EffectSlot (for legacy batch DSP compatibility) ───────────────────
    py::class_<EffectSlot>(m, "EffectSlot")
        .def_readwrite("enabled", &EffectSlot::enabled)
        .def_property("type",
            [](const EffectSlot& s){ return (int)s.type; },
            [](EffectSlot& s, int t){ s.type=(EffectType)t; })
        .def_property("params",
            [](const EffectSlot& s){
                return std::vector<float>(s.params, s.params+24); },
            [](EffectSlot& s, std::vector<float> v){
                for(int i=0;i<24&&i<(int)v.size();i++) s.params[i]=v[i]; });

    // ── EngineState (for GR levels, FFT bins, rack UI display) ───────────
    py::class_<EngineState>(m, "EngineState")
        .def_readwrite("active_track_id", &EngineState::active_track_id)
        .def_readwrite("current_frame",   &EngineState::current_frame)
        .def_readwrite("is_playing",      &EngineState::is_playing)
        .def_property("volumes",
            [](EngineState& s){ return std::vector<float>(s.volumes,s.volumes+PB_MAX_CHANNELS); },
            [](EngineState& s, std::vector<float> v){
                for(int i=0;i<PB_MAX_CHANNELS&&i<(int)v.size();i++) s.volumes[i]=v[i]; })
        .def_property("meter_levels",
            [](EngineState& s){ return std::vector<float>(s.meter_levels,s.meter_levels+PB_MAX_CHANNELS); },
            [](EngineState& s, std::vector<float> v){
                for(int i=0;i<PB_MAX_CHANNELS&&i<(int)v.size();i++) s.meter_levels[i]=v[i]; })
        .def_property("mutes",
            [](EngineState& s){
                std::vector<bool> v;
                for(int i=0;i<PB_MAX_CHANNELS;i++) v.push_back(s.mutes[i]);
                return v; },
            [](EngineState& s, std::vector<bool> v){
                for(int i=0;i<PB_MAX_CHANNELS&&i<(int)v.size();i++) s.mutes[i]=v[i]; })
        .def_property("solos",
            [](EngineState& s){
                std::vector<bool> v;
                for(int i=0;i<PB_MAX_CHANNELS;i++) v.push_back(s.solos[i]);
                return v; },
            [](EngineState& s, std::vector<bool> v){
                for(int i=0;i<PB_MAX_CHANNELS&&i<(int)v.size();i++) s.solos[i]=v[i]; })
        .def("get_band_levels",
            [](EngineState& s, int ch) -> std::vector<float> {
                if(ch<0||ch>=PB_MAX_CHANNELS) throw std::out_of_range("ch");
                return std::vector<float>(s.band_levels[ch],s.band_levels[ch]+PB_MB_BANDS); },
            py::arg("channel"))
        .def("get_gr_levels",
            [](EngineState& s, int ch) -> std::vector<float> {
                if(ch<0||ch>=PB_MAX_CHANNELS) throw std::out_of_range("ch");
                return std::vector<float>(s.gr_levels[ch],s.gr_levels[ch]+PB_MB_BANDS); },
            py::arg("channel"))
        .def("get_spec_bins",
            [](EngineState& s, int ch) -> std::vector<float> {
                if(ch<0||ch>=PB_MAX_CHANNELS)
                    throw std::out_of_range("ch");
                return std::vector<float>(s.spec_bins[ch],
                                          s.spec_bins[ch]+PB_SPEC_BINS); },
            py::arg("channel"))
        .def("get_effect_slot",
            [](EngineState& s, int ch, int slot) -> EffectSlot& {
                if(ch<0||ch>=PB_MAX_CHANNELS||slot<0||slot>=PB_MAX_EFFECTS)
                    throw std::out_of_range("ch/slot");
                return s.effect_chain[ch][slot]; },
            py::arg("channel"), py::arg("slot"),
            py::return_value_policy::reference);
}
