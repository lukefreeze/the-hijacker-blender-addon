# The Hijacker — Beta Tester Guide

Thanks for helping test **The Hijacker** — a professional audio mixing and AI processing suite for Blender's Video Sequence Editor. This is an early beta: the core mixing desk, all DSP racks, and all five AI racks are complete and working, but you may still hit rough edges. That's exactly what this round is for.

---

## What you're testing

- A 9-channel mixing desk rendered directly in Blender's Node Editor (fader, gain, pan, mute, solo, real-time VU meters)
- 6 real-time DSP racks: Compressor (single + multiband), 7-band EQ, Reverb, Noise Gate, Delay, and a Booster
- A Mixdown rack that renders your mix to a final audio file
- 5 AI racks that run in the background so Blender stays responsive: Demucs (stem separation), Whisper (transcription/subtitles), Voicefixer (voice restoration), KNNVC (voice conversion), and Piper TTS (offline text-to-speech)

## Platform support in this build

| Platform | Status |
|---|---|
| Windows x64 | Primary target — this is what's been tested end-to-end |
| macOS (Apple Silicon) | Included, but **not yet verified running inside Blender** — the engine builds, but real-world testing hasn't happened. If you're on a Mac, you're genuinely helping us find out if it works. |
| Linux | Not included in this build |

---

## Installing

1. Open Blender 4.5.
2. `Edit → Preferences → Add-ons`, then click the **▼ dropdown** in the top-right corner of the Add-ons panel and choose **Install from Disk...** (you can also just drag the zip straight into the Add-ons panel).
3. Select `TheHijacker-beta.zip` (don't unzip it first — Blender does that for you).
4. Enable the checkbox next to **The Hijacker**.
5. Open a **Node Editor** area (or switch an existing area's editor type to Node Editor) and click the **Hijacker** button in the header to open the mixing desk.

If Blender complains that it can't find `hijacker_engine` on your platform, stop there and report it — see **Reporting issues** below.

---

## Using the AI racks

Each AI rack (Demucs, Whisper, Voicefixer, KNNVC, Piper) checks for its own Python dependency the first time you add it. If something's missing, the rack shows a red **"NOT FOUND — REQUIRES SETUP"** card with the exact `pip install` command to run in a terminal, right there in the UI — just follow what it tells you, then restart Blender. You shouldn't need to guess package names or hunt through docs.

Piper TTS is the exception — its voice engine ships bundled inside the addon, so it should work with no extra setup.

---

## What we especially want feedback on

- Does the addon load at all on your platform/Blender version?
- Any crash, freeze, or Blender becoming unresponsive — note what rack or action triggered it
- Anything that looks visually broken or misaligned (skins, buttons, text)
- AI rack processing: did it complete, and did the output land back on the timeline correctly?
- On macOS specifically: does the mixing desk render at all, and does audio actually play?

## Reporting issues

Please email **lukebridgerfreeze@gmail.com**.

Include:
- Your OS and Blender version
- What you were doing when it happened
- Any error text from Blender's System Console (`Window → Toggle System Console` on Windows; run Blender from a terminal on macOS to see console output)

---

Thanks again for testing — every report genuinely shapes what gets fixed before the wider release.
