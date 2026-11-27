# The Hijacker 1.0.0-rc43 (pre-release)

A full mixing desk and offline AI audio suite for Blender's Video Sequence Editor. Free and open source (GPL-3.0-or-later), with a custom C++ audio engine, effect racks, and AI racks that run locally with no accounts, credits or uploads.

> **This is a pre-release.** Every feature is in and working, and it has been tested by hand on Windows and macOS. It is not "1.0 final" yet because Linux is only partly tested and I want real-world feedback first. Bug reports are very welcome.

## Download and install

1. Download `TheHijacker-1.0.0-rc43.zip` below (don't unzip it).
2. In Blender: `Edit → Preferences → Add-ons`, open the **▼** menu at the top right, choose **Install from Disk...** and pick the zip. (You can also drag the zip into the Add-ons panel.)
3. Tick **The Hijacker**, then open a Node Editor and click **The Hijacker** in the header.

Needs **Blender 4.5 or newer**. Blender 4.3 and older are not supported.

## What's in it

- **Mixing desk** — 9 channels with faders, gain, pan, mute, solo and live VU meters, drawn right inside Blender and fully skinnable.
- **Effect racks** — single and multiband compressor, 7-band EQ, reverb, noise gate and stereo delay, with presets.
- **Booster** — raises very quiet clips far beyond Blender's strip volume limit (up to +40 dB). A look-ahead limiter keeps it clean; switch **LIMIT: OFF** to push it into distortion on purpose.
- **Mixdown** — render the whole mix, or every channel with its effects baked in, to WAV or FLAC without leaving Blender.
- **AI racks** (each downloads its own engine and models the first time you use it): **Demucs** stem separation, **Whisper** speech-to-text subtitles, **Piper** text-to-speech, **kNN-VC** voice conversion, **VoiceFixer** restoration.
- **PACK-A-PUNCH** skin mode, with its own rack artwork and intro transition.

## Tested

| Platform | Blender | Status |
|----------|---------|--------|
| Windows x64 | 4.5, 5.2, 5.3 | Everything tested, including all AI racks |
| macOS (Apple Silicon) | 4.5, 5.1 | Everything tested, including all AI racks |
| Linux x64 | 4.5 | Engine and mixing desk load and play. **AI racks not tested yet** |

## Known limits

- Piper on Apple Silicon runs under Rosetta (Piper only ships an Intel Mac build).
- Whisper is built for speech, not singing or dense music.
- The first AI model download can sit near 0% for a few minutes before it speeds up. It is resumable.
- With the Booster's limiter off, the boosted file is saved at full scale and the extra gain sits on the strip's volume, so Blender's waveform can display it. It sounds identical.
- The AI racks need an internet connection the first time you install them, and work offline afterwards.

## Licences and AI

The add-on contains no AI engines, models or voices; they are downloaded from their official sources and stay under their own licences (see `THIRD_PARTY_NOTICES.md`, included in the zip). The starter Piper voice is public domain; other voices and the VoiceFixer models have their own terms, so check them before publishing results.

The Hijacker was created by Luke Bridger with AI assistance (Claude by Anthropic): the design, direction and hand-testing are mine, and much of the code was written by AI under that direction.

## Feedback

Found a bug? [Open an issue](https://github.com/lukefreeze/the-hijacker-blender-addon/issues) and paste the Blender console log (`Window → Toggle System Console` on Windows). Mention your OS and Blender version.
