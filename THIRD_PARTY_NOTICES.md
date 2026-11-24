# Third-Party Notices

The Hijacker is licensed under the GNU GPL v3 or later (see [LICENSE](LICENSE)
and [NOTICE.md](NOTICE.md)).

**The repository and the add-on zip do not contain any AI engine, AI model or
voice.** The AI racks download them, with the user's click, from their
official sources onto the user's own machine. Each is the property of its own
authors and is used under its own licence. Nothing here changes those licences.

Licences below were checked against each project's own repository, package
page or download page. Versions change, so please open an issue if anything
here is wrong or out of date.

> **Using Piper voices commercially?** The starter voice is public domain, but
> other voices in the catalogue vary and some are non-commercial only. See
> [Piper TTS and its voices](#piper-tts-and-its-voices).

## 1. Compiled into the engine (shipped inside the add-on zip)

The native audio engine (`hijacker_engine`) is built from this repository's C++
and links or compiles in the libraries below. Their licence texts are
reproduced in full in section 5, as their licences require.

| Component | Use | Licence | Copyright |
|---|---|---|---|
| [PortAudio](http://www.portaudio.com/) | Real-time audio I/O, statically linked | MIT | (c) 1999-2011 Ross Bencina and Phil Burk |
| [pybind11](https://github.com/pybind/pybind11) | C++/Python bindings, compiled in | BSD-3-Clause | (c) 2016 Wenzel Jakob |
| [Dear ImGui](https://github.com/ocornut/imgui) (`src/imgui/`) | Compiled into the engine build | MIT | (c) 2014-2026 Omar Cornut |
| ProggyClean font (embedded in Dear ImGui) | Built-in font data | MIT | (c) 2004, 2005 Tristan Grimmer |
| ProggyForever font (embedded in Dear ImGui) | Built-in font data | MIT | (c) 2026 Disco Hello; (c) 2019, 2023 Tristan Grimmer |
| Japanese kanji glyph ranges (in Dear ImGui) | Glyph-range tables | CC BY 4.0 | Agency for Cultural Affairs (Joyo kanji) and Ministry of Justice (Jinmeiyo kanji), Japan |

The engine is also compiled against Blender's header files (read-only, for the
sound API); no Blender source is copied into this project.

## 2. Downloaded on demand by the AI racks

None of this is in the zip. Each rack asks first, then fetches it from the
official source. Engines run as **separate programs / a separate Python
environment** (`hijacker_pydeps`, one per Blender version); they are not linked
into the add-on.

### Piper TTS and its voices

| Item | Source | Licence |
|---|---|---|
| Piper engine, release 2023.11.14-2 (`rhasspy/piper`) | GitHub release, downloaded by the rack's DOWNLOAD & INSTALL button into the add-on folder | MIT. The repository was archived on 6 Oct 2025; development moved to [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl) (GPL-3.0). The Hijacker uses the old, MIT release |
| espeak-ng (phonemiser, part of the Piper release) | Same release archive | **GPL-3.0-or-later**, run as a separate subprocess. Source: https://github.com/espeak-ng/espeak-ng |
| piper-phonemize, onnxruntime (part of the Piper release) | Same release archive | MIT |
| Voice models (`rhasspy/piper-voices` on Hugging Face) | Downloaded per voice | **Per voice.** The Hugging Face repository is tagged MIT, but that does not cover each voice's training data. Each voice has its own model card and dataset licence |

**Starter voice.** On first install the rack downloads one starter voice,
`en_US-ljspeech-medium`. It is trained from scratch on the LJ Speech dataset,
which is in the public domain, so it is free to use commercially with no
attribution or royalties. (This is not legal advice; Piper itself takes no
position on whether a trained model is a derivative of its recordings.)

**Other voices - read the model card.** The rack can also browse Piper's
public catalogue and download further voices. Those carry their own terms:
- Voices built on **CC0 / public-domain** data (for example `en_US-kristin-medium`,
  `en_US-norman-medium`, `en_US-joe-medium`) need no credit.
- Voices built on **CC BY** data (for example `en_US-libritts-high`,
  `en_US-hifi_tts_92-medium`) are fine commercially but need credit to the dataset.
- Voices derived from the Blizzard "Lessac" dataset (`en_US-lessac-*`, and anything
  fine-tuned from it) and from RyanSpeech (`en_US-ryan-*`, CC BY-NC-SA) are
  **research / non-commercial only**. Do not use their output in commercial work.

The Piper project keeps a list of voices whose whole training chain is
commercially usable (`docs/COMMERCIAL_USE.md` in OHF-Voice/piper1-gpl). You are
responsible for the voice you choose.

### Demucs (stem separation)

| Item | Source | Licence |
|---|---|---|
| [Demucs](https://github.com/adefossez/demucs) (`pip install demucs`) | PyPI. The original `facebookresearch/demucs` repository is archived; the maintained fork is by its author Alexandre Defossez | MIT, (c) Meta Platforms / Alexandre Defossez |
| Pretrained Demucs weights | Downloaded by Demucs on first run | No separate weights licence is published; they ship with the MIT-licensed project. Some models (e.g. `mdx_extra`) were trained with extra data, including the MUSDB test set |
| [static-ffmpeg](https://pypi.org/project/static-ffmpeg/) (`pip`) | PyPI | MIT. On first use it downloads FFmpeg binaries from `github.com/zackees/ffmpeg_bins` |
| FFmpeg binaries (fetched by static-ffmpeg) | GitHub | FFmpeg is LGPL-2.1+, and GPL-2.0+ when built with optional components. static-ffmpeg does not state which configuration its builds use, so treat them as possibly GPL. FFmpeg is only ever run as a separate program |

### kNN-VC (voice conversion)

| Item | Source | Licence |
|---|---|---|
| [kNN-VC](https://github.com/bshall/knn-vc) | `torch.hub` / GitHub | MIT, (c) 2023 MediaLab, Stellenbosch University |
| WavLM-Large encoder (checkpoint used by kNN-VC) | Original release by Microsoft ([microsoft/unilm](https://github.com/microsoft/unilm)) | MIT, (c) Microsoft Corporation |
| HiFi-GAN vocoder checkpoints | GitHub releases of kNN-VC (trained by its authors; architecture adapted from [jik876/hifi-gan](https://github.com/jik876/hifi-gan), MIT) | Distributed with the MIT-licensed kNN-VC project; no separate weights licence is published |
| Training / evaluation data | LibriSpeech | CC BY 4.0 (credit: V. Panayotov, G. Chen, D. Povey, S. Khudanpur) |

### VoiceFixer (speech restoration)

| Item | Source | Licence |
|---|---|---|
| [VoiceFixer](https://github.com/haoheliu/voicefixer) (`pip install voicefixer`) | PyPI | MIT |
| Model files `vf.ckpt` and the vocoder checkpoint | Zenodo records [5600188](https://zenodo.org/records/5600188) and [5469951](https://zenodo.org/records/5469951) | **CC BY 4.0**, author Haohe Liu (ByteDance). **Credit:** "VoiceFixer pretrained models by Haohe Liu, licensed CC BY 4.0" |

### Whisper (transcription)

| Item | Source | Licence |
|---|---|---|
| [OpenAI Whisper](https://github.com/openai/whisper) code and model weights | Weights are fetched through faster-whisper | MIT (code and weights) |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper), converted models `Systran/faster-whisper-*` | PyPI / Hugging Face | MIT |
| CTranslate2, onnxruntime | PyPI | MIT |
| PyAV (`av`) | PyPI | BSD-3-Clause; its wheels bundle FFmpeg libraries (LGPL/GPL configuration not stated by the project) |
| huggingface-hub | PyPI | Apache-2.0 |

### Shared Python packages

PyTorch (BSD-style), torchaudio (BSD; its CC-BY-NC SQUIM models are not used),
NumPy (BSD-3-Clause and others), SciPy (BSD-3-Clause), python-soundfile
(BSD-3-Clause; its wheels bundle libsndfile, LGPL-2.1+). All are installed with
`pip` into the private `hijacker_pydeps` environment, never into Blender's own
Python. See each package's own licence.

## 3. Voice cloning and synthetic speech

kNN-VC and Piper can imitate or synthesise human voices. You are responsible
for having the right to use any voice recording you convert and for complying
with the licence of any voice model you download. Do not use these tools to
impersonate a person without their consent.

## 4. Acknowledgement

The PACK-A-PUNCH skin mode is a tip of the hat to the *Call of Duty* series
for the idea. The Hijacker is not affiliated with or endorsed by Activision.

## 5. Licence texts for components shipped in the zip

### PortAudio (MIT)

```
PortAudio Portable Real-Time Audio Library
Copyright (c) 1999-2011 Ross Bencina and Phil Burk

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

PortAudio's maintainers also ask (non-binding) that anyone distributing
modifications to PortAudio send them to the original developers, and that this
request be included along with the licence above.

### pybind11 (BSD-3-Clause)

```
Copyright (c) 2016 Wenzel Jakob <wenzel.jakob@epfl.ch>, All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### Dear ImGui (MIT)

```
The MIT License (MIT)

Copyright (c) 2014-2026 Omar Cornut

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Fonts embedded in Dear ImGui (MIT)

ProggyClean - Copyright (c) 2004, 2005 Tristan Grimmer
(https://github.com/bluescan/proggyfonts).
ProggyForever - Copyright (c) 2026 Disco Hello; Copyright (c) 2019, 2023
Tristan Grimmer.
Both are released under the MIT licence, with the same permission and
disclaimer text as the Dear ImGui licence above.

### Japanese kanji glyph ranges (CC BY 4.0)

The compact kanji glyph-range tables in Dear ImGui are derived from the Joyo
kanji list (Agency for Cultural Affairs, Japan) and the Jinmeiyo kanji list
(Ministry of Justice, Japan), available under the Creative Commons Attribution
4.0 International licence (https://creativecommons.org/licenses/by/4.0/).
