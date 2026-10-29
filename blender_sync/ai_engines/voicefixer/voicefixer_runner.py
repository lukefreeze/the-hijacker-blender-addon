"""
ai_engines/voicefixer/voicefixer_runner.py
==========================================
Standalone runner for VoiceFixer speech restoration.
Called as a subprocess by core/ai_voicefixer.py.

Usage:
    python voicefixer_runner.py --src INPUT.wav --output OUTPUT.wav --mode 0

Modes:
    0 = Standard restoration (default, recommended)
    1 = Smooth (preprocessing removes high freq first, alters voice slightly)
    2 = Aggressive (best for seriously degraded recordings)

Outputs:
    PROGRESS:N  - progress percentage (0-100)
    STATUS:text - what is happening now ("" clears it)
    DONE:/path  - output path on success
    ERROR:msg   - error message on failure

The two model files (~0.5 GB total) are downloaded here, BEFORE voicefixer
is imported, with a resumable downloader. voicefixer's own import-time
download cannot resume and leaves a truncated file behind when the
connection drops, which then breaks every later run.
"""

import sys
import os
import math
import time
import argparse
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hj_download as hd  # noqa: E402

_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "voicefixer")
_MODELS = [
    ("VoiceFixer model (1 of 2)",
     "https://zenodo.org/record/5600188/files/vf.ckpt?download=1",
     os.path.join(_CACHE, "analysis_module", "checkpoints", "vf.ckpt"), 5, 10),
    ("VoiceFixer voice model (2 of 2)",
     "https://zenodo.org/record/5469951/files/model.ckpt-1490000_trimed.pt?download=1",
     os.path.join(_CACHE, "synthesis_module", "44100", "model.ckpt-1490000_trimed.pt"), 10, 15),
]

SEG_SECONDS = 30      # VoiceFixer's own internal segment length
SR = 44100


def _out(line):
    print(line)
    sys.stdout.flush()


def ensure_models():
    for label, url, path, lo, hi in _MODELS:
        hd.download_file(url, path, emit=hd.stdout_emitter(label, lo, hi),
                         adopt_existing=True)
    _out("STATUS:")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src",    required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode",   type=int, default=0)
    args = parser.parse_args()

    if not os.path.exists(args.src):
        print(f"ERROR: source file not found: {args.src}")
        sys.exit(1)

    os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)

    try:
        _out("PROGRESS:5")
        try:
            ensure_models()
        except Exception as e:
            _out(f"ERROR:could not download the VoiceFixer models: {e}")
            sys.exit(1)

        _out("STATUS:Loading VoiceFixer")
        from voicefixer import VoiceFixer
        import numpy as np
        import torch
        from voicefixer.tools.wav import save_wave
        _out("PROGRESS:20")

        # VoiceFixer's checkpoints are old-style pickles; torch >= 2.6 refuses
        # them by default (weights_only=True). Retry permissively only for
        # that specific failure - these are the project's own model files.
        _orig_load = torch.load

        def _load(*a, **k):
            try:
                return _orig_load(*a, **k)
            except Exception as e_load:
                _m = str(e_load).lower()
                if ("weights_only" in _m or "weights only" in _m or "weightsunpickler" in _m) \
                        and "weights_only" not in k:
                    k["weights_only"] = False
                    return _orig_load(*a, **k)
                raise
        torch.load = _load

        use_cuda = torch.cuda.is_available()
        device_str = "cuda" if use_cuda else "cpu"

        vf = VoiceFixer()
        wav = vf._load_wav(args.src, sample_rate=SR)
        _out("PROGRESS:30")
        _out(f"STATUS:Restoring on {device_str.upper()}")

        # VoiceFixer processes audio in 30 s segments internally; doing the
        # same split here gives identical output and a real progress signal.
        seg_len = SEG_SECONDS * SR
        n_seg = max(1, int(math.ceil(len(wav) / float(seg_len))))
        lo, hi = 30.0, 95.0
        est = [seg_len / float(SR) * (0.12 if use_cuda else 1.0)]   # guess: seconds per segment
        cur = {"k": 0, "t0": time.time(), "run": True, "last": 30}

        def _ticker():
            while cur["run"]:
                span = (hi - lo) / n_seg
                base = lo + span * cur["k"]
                frac = 1.0 - math.exp(-(time.time() - cur["t0"]) / max(0.5, est[0]))
                pct = int(base + span * min(frac, 0.95))
                if pct > cur["last"]:
                    cur["last"] = pct
                    _out(f"PROGRESS:{pct}")
                time.sleep(0.5)

        threading.Thread(target=_ticker, daemon=True).start()

        pieces = []
        for k in range(n_seg):
            cur["k"], cur["t0"] = k, time.time()
            chunk = wav[k * seg_len:(k + 1) * seg_len]
            pieces.append(vf.restore_inmem(chunk, cuda=use_cuda, mode=args.mode))
            est[0] = max(0.5, time.time() - cur["t0"])
            pct = int(lo + (hi - lo) * (k + 1) / n_seg)
            if pct > cur["last"]:
                cur["last"] = pct
                _out(f"PROGRESS:{pct}")
        cur["run"] = False

        _out("STATUS:Saving")
        save_wave(np.concatenate(pieces, axis=-1), fname=args.output, sample_rate=SR)
        _out("PROGRESS:97")

        if os.path.exists(args.output):
            _out(f"DONE:{args.output}")
        else:
            _out(f"ERROR: output file not created at {args.output}")

    except SystemExit:
        raise
    except Exception as e:
        import traceback
        _out(f"ERROR:{e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
