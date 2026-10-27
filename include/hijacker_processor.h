#pragma once
// ---------------------------------------------------------------------------
// hijacker_processor.h
// The Hijacker — DSP effect chain processor
// Place in: <repo>/include/
// ---------------------------------------------------------------------------

#include "mixer_ui.h"
#include <memory>

// Forward declarations
namespace aud { class ISound; class IReader; typedef float sample_t; }

// ---------------------------------------------------------------------------
// Batch DSP — called from wrapper.cpp's process_buffer() and from
// hijacker_audio_engine.cpp's audio callback for real-time processing.
// Applies the full effect chain for channel_idx to an interleaved
// float buffer of (frames * n_channels) samples in-place.
// Also updates fft_bins and gr_levels in g_state.
// ---------------------------------------------------------------------------
void apply_effect_chain_batch(int channel_idx, aud::sample_t* buf,
                               int frames, int n_channels, float sample_rate);

// ---------------------------------------------------------------------------
// State management helpers (called from hijacker_audio_engine.cpp)
// ---------------------------------------------------------------------------
// Make sure the delay line for this channel exists and is large enough for
// the maximum delay time at `sample_rate` (allocates; call from a non-audio
// thread, e.g. when a delay effect slot is assigned).
void hj_ensure_delay_buffers(int channel_idx, float sample_rate);

// Free the delay lines of every channel (engine shutdown).
void hj_release_all_delay_buffers();

// Clear the persistent DSP memory (filters, envelopes, reverb/delay tails)
// of every effect currently enabled on this channel. Audio-thread safe:
// no allocation. Used after seeks so old tails do not leak into new audio.
void hj_reset_dsp_for_active_fx(int channel_idx);

// True if any effect slot of the channel is enabled (seqlock-consistent read).
bool hj_channel_has_fx(int channel_idx);

// Clear all persistent DSP memory of a channel (used if a NaN/Inf appears).
void hj_reset_channel_dsp(int channel_idx);

// Copy effect slot (ch, slot) into `out` using the slot's seqlock so a
// concurrent set_effect_slot() cannot produce a torn read.
void hj_snapshot_slot(int channel_idx, int slot, EffectSlot& out);
