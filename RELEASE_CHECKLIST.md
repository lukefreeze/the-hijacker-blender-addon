# The Hijacker: pre-release checklist

Items we agreed to come back to when release is close.

## 1. AI model and voice licensing (double-check everything)
For each engine, confirm the licence of the code AND of the model weights/voices, and whether we may
bundle them, auto-download them, or only link to them. Check commercial-use terms.

- [x] Piper TTS: the engine, and each voice in the voice catalogue (voices have different licences; some restrict commercial use). Engine MIT (archived; successor is GPL); voices are per-voice. Starter voice switched to en_US-ljspeech-medium (public domain); catalogue voices carry their own terms, documented in the notices
- [x] kNN-VC voice conversion: code and the WavLM / vocoder weights it pulls in (MIT; LibriSpeech CC BY 4.0 credited)
- [x] Whisper: code and model weights (MIT)
- [x] Demucs: code and model weights (MIT; no separate weights licence)
- [x] Voice Fixer: code and weights (code MIT; weights CC BY 4.0, credited)
- [x] RVC engine: not part of this release
- [x] Any other bundled DLLs / wheels / libraries (check their licences and attribution requirements): PortAudio, pybind11, Dear ImGui + fonts; static-ffmpeg / PyAV FFmpeg builds do not state LGPL vs GPL (run as separate programs)
- [x] Decide for each: bundled with the addon, downloaded on first use, or user-supplied (all AI engines and models are downloaded on first use; nothing is bundled)
- [x] Write the credits / third-party notices file from the results (THIRD_PARTY_NOTICES.md, now shipped inside the zip)

## 2. IP clean-up on the packApunch skins
- [x] Deadpool rack: redo so it is clearly its own design
- [x] Remove the rebel-fighter symbol from its rack
- [x] Review the PIPER logo (HAL 9000 logo layout with the name swapped)
- [x] Single-band compressor skin: the OFF version (rack_comp_single_bg_off.png, and the collapsed OFF version) still has the old emblem; update it to match the ON version
- [x] Final pass over every skin for logos, symbols, names or other recognisable elements

## 3. Documentation and positioning
- [ ] README: pitch around "the audio suite Blender never had", free, local AI, no credits or uploads
- [ ] AI-transparency note (parts of the tool and its art were built with AI assistance)
- [ ] Credits page

## 4. Other open items
- [x] Booster rack: circular knob sprite, art fixes, full wiring
- [x] Voice Fixer restyle (new packApunch layout: rack_voicefixer_pp.py), optional lit art rack_voicefixer_bg_lit.png still to add
- [x] Demucs (Matrix) restyle
- [x] Rename the Voice Fixer rack (drop the "Mother" working name): done, the skin carries no title and none is needed
- [x] Noise-gate power-off, EQ power-off, delay audio (done and checked)
- [x] Mixdown rack skin: button "on" overlays (mode, format, sample rate, bit depth, range) and collapsed background
