# The Hijacker: pre-release checklist

Items we agreed to come back to when release is close.

## 1. AI model and voice licensing (double-check everything)
For each engine, confirm the licence of the code AND of the model weights/voices, and whether we may
bundle them, auto-download them, or only link to them. Check commercial-use terms.

- [ ] Piper TTS: the engine, and each voice in the voice catalogue (voices have different licences; some restrict commercial use)
- [ ] kNN-VC voice conversion: code and the WavLM / vocoder weights it pulls in
- [ ] Whisper: code and model weights
- [ ] Demucs: code and model weights
- [ ] Voice Fixer: code and weights
- [ ] RVC engine: code, and any bundled or user-supplied voice models
- [ ] Any other bundled DLLs / wheels / libraries (check their licences and attribution requirements)
- [ ] Decide for each: bundled with the addon, downloaded on first use, or user-supplied
- [ ] Write the credits / third-party notices file from the results

## 2. IP clean-up on the packApunch skins
- [ ] Deadpool rack: redo so it is clearly its own design
- [ ] Remove the rebel-fighter symbol from its rack
- [ ] Review the PIPER logo (HAL 9000 logo layout with the name swapped)
- [ ] Final pass over every skin for logos, symbols, names or other recognisable elements

## 3. Documentation and positioning
- [ ] README: pitch around "the audio suite Blender never had", free, local AI, no credits or uploads
- [ ] Be careful not to promise full Premiere Pro parity
- [ ] AI-transparency note (parts of the tool and its art were built with AI assistance)
- [ ] Credits page

## 4. Other open items
- [ ] Booster rack: circular knob sprite, art fixes, full wiring
- [ ] Voice Fixer (Mother) and Demucs (Matrix) restyles
- [ ] Noise-gate power-off behaviour, EQ live labels, mixdown rack, delay audio
- [ ] Commit the large body of uncommitted work on feature/hijacker-engine
