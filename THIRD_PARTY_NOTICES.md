# Third-Party Notices

The Hijacker is licensed under the GNU GPL v3 or later (see [LICENSE](LICENSE)).

**The repository and the add-on zip do not contain any AI engine, AI model or
voice.** The racks below download them, with the user's click, from their
official sources into the user's own machine. Each is the property of its own
authors and is used under its own licence. Nothing here changes those licences.

> Licence entries marked **(verify)** are from the projects' public pages as
> best known and must be re-checked against the exact version/weights
> downloaded before each release. Please open an issue if anything is wrong.

## What the repository itself contains

| Component | Use | Licence |
|---|---|---|
| Dear ImGui (`src/imgui/`, kept only for build compatibility) | C++ build | MIT, Copyright Omar Cornut |
| PortAudio | Real-time audio I/O, statically linked into the engine | MIT-style (verify) |
| pybind11 | C++/Python bindings, compiled into the engine | BSD-3-Clause (verify) |

## Downloaded on demand by the AI racks

| Rack | Software | How it is obtained | Licence |
|---|---|---|---|
| Piper TTS | [Piper](https://github.com/rhasspy/piper) engine (release binaries) | Downloaded by the rack's DOWNLOAD & INSTALL button from the official GitHub release (2023.11.14-2) into the add-on folder | MIT (verify); the release bundles **espeak-ng (GPL-3.0)**, onnxruntime (MIT) and piper-phonemize (MIT) as separate programs run as a subprocess |
| Piper TTS | Voice models (from `rhasspy/piper-voices`) | One starter voice (`en_US-lessac-medium`) is fetched at install; more are downloaded per voice from Hugging Face | **Per voice.** Each voice has its own model card and training-dataset licence (e.g. the `lessac` voices derive from the Blizzard 2013 Lessac dataset, which has its own terms). Check the card before using output commercially |
| Demucs | [Demucs](https://github.com/facebookresearch/demucs) + pretrained weights | `pip` + first-run weight download | MIT (verify) |
| Demucs | static-ffmpeg (FFmpeg builds) | `pip`, binaries fetched on first run | FFmpeg is LGPL/GPL depending on build (verify) |
| kNN-VC | [kNN-VC](https://github.com/bshall/knn-vc), WavLM encoder, HiFi-GAN vocoder weights | `torch.hub` first-run download | MIT (verify); weights trained on LibriSpeech (CC BY 4.0, verify) |
| VoiceFixer | [VoiceFixer](https://github.com/haoheliu/voicefixer) | `pip` + weight download | Code: MIT. Pretrained model licence is not stated by the project (verify) |
| Whisper | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and OpenAI Whisper weights | `pip` + first-run weight download | MIT (verify) |
| All | PyTorch, NumPy, SciPy, onnxruntime etc. | `pip` into a private virtual environment | BSD-style / MIT / Apache-2.0 (see each package) |

## Voice cloning and synthetic speech

kNN-VC and Piper can imitate or synthesise human voices. You are responsible
for having the right to use any voice recording you convert and for complying
with the licence of any voice model you download. Do not use these tools to
impersonate a person without their consent.

## Acknowledgement

The PACK-A-PUNCH skin mode is a tip of the hat to the *Call of Duty* series
for the idea. The Hijacker is not affiliated with or endorsed by Activision.
