"""
ai_engines/knnvc/knnvc_runner.py
=================================
Standalone runner called via system Python subprocess.
Applies the PyTorch 2.6+ compatibility patch to matcher.py before inference.

Usage:
    python knnvc_runner.py --src <wav> --ref <wav> --output <wav>
                           --topk 4 --ref_secs 30 --patch <patch.py>
"""

import sys
import os
import argparse


def apply_patch(patch_path):
    """Apply the kNN-VC matcher.py compatibility patch if needed."""
    if not patch_path or not os.path.exists(patch_path):
        return
    try:
        import torch
        hub_dir = torch.hub.get_dir()
        matcher = os.path.join(hub_dir, "bshall_knn-vc_master", "matcher.py")
        if not os.path.exists(matcher):
            return
        # Check if already patched
        code = open(matcher, encoding="latin-1").read()
        if "normalize+matmul" in code:
            return   # already patched
        # Apply patch
        exec(open(patch_path, encoding="utf-8").read())
        print("[KNNVC_RUNNER] patch applied")
    except Exception as e:
        print(f"[KNNVC_RUNNER] patch warning: {e}")


def _can_torchaudio_load(path):
    """True if torchaudio can decode this file. Newer torchaudio releases (2.9+)
    route load()/save() through the separate 'torchcodec' package, which a
    plain pip install does not bring in, so load() raises ImportError there."""
    try:
        import torchaudio
        torchaudio.load(path, frame_offset=0, num_frames=16)
        return True
    except Exception as e:
        print(f"[KNNVC_RUNNER] torchaudio can't read audio ({type(e).__name__}); using soundfile")
        return False


def _import_soundfile():
    """Import soundfile, pip-installing it first if this environment predates
    it being a kNN-VC dependency."""
    try:
        import soundfile
        return soundfile
    except ImportError:
        import subprocess
        print("[KNNVC_RUNNER] installing soundfile ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--disable-pip-version-check", "soundfile"])
        import soundfile
        return soundfile


def _load_audio(path):
    """-> (float32 tensor [channels, samples], sample_rate), torchaudio first, soundfile second."""
    import torch
    try:
        import torchaudio
        return torchaudio.load(path)
    except Exception as e_ta:
        try:
            sf = _import_soundfile()
            data, sr = sf.read(path, dtype="float32", always_2d=True)
            return torch.from_numpy(data.T.copy()), sr
        except Exception as e_sf:
            raise RuntimeError(f"could not read audio '{path}': torchaudio: {e_ta}; soundfile: {e_sf}")


def _save_audio(path, wav, sr):
    """wav: tensor [channels, samples]."""
    try:
        import torchaudio
        torchaudio.save(path, wav, sr)
    except Exception:
        sf = _import_soundfile()
        sf.write(path, wav.detach().cpu().numpy().T, sr)


def _as_16k_mono(path):
    import torch
    import torchaudio
    wav, sr = _load_audio(path)
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    if sr != 16000:
        wav = torchaudio.functional.resample(wav, sr, 16000)
    return wav[0]


def run(src, ref, output, topk=4, ref_secs=30, patch_path=None):
    import torch
    import torchaudio

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"PROGRESS:5")

    # Apply compatibility patch
    apply_patch(patch_path)
    print(f"PROGRESS:10")

    # Load model
    knn_vc = torch.hub.load(
        "bshall/knn-vc", "knn_vc",
        prematched=True, trust_repo=True, pretrained=True)
    knn_vc = knn_vc.to(device)
    print(f"PROGRESS:30")

    # Trim reference to ref_secs to avoid VRAM issues
    ref_wav, ref_sr = _load_audio(ref)
    max_samples = ref_sr * ref_secs
    if ref_wav.shape[1] > max_samples:
        ref_wav = ref_wav[:, :max_samples]
        tmp_ref = output + "_ref_trimmed.wav"
        _save_audio(tmp_ref, ref_wav, ref_sr)
        ref = tmp_ref
    print(f"PROGRESS:40")

    # kNN-VC's own loader uses torchaudio.load(); when that can't decode (see
    # _can_torchaudio_load) hand it ready-made 16 kHz mono tensors instead.
    if _can_torchaudio_load(src):
        src_arg = src
    else:
        src_arg = _as_16k_mono(src)
    ref_arg = ref if _can_torchaudio_load(ref) else _as_16k_mono(ref)

    # Extract features
    query_seq   = knn_vc.get_features(src_arg)
    print(f"PROGRESS:60")
    matching_set = knn_vc.get_matching_set([ref_arg])
    print(f"PROGRESS:75")

    # Convert
    out_wav = knn_vc.match(query_seq, matching_set, topk=topk)
    print(f"PROGRESS:90")

    # Save
    _save_audio(output, out_wav[None], 16000)
    print(f"PROGRESS:100")
    print(f"DONE:{output}")

    # Clean up temp ref
    try:
        tmp = output + "_ref_trimmed.wav"
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--src",      required=True)
    parser.add_argument("--ref",      required=True)
    parser.add_argument("--output",   required=True)
    parser.add_argument("--topk",     type=int, default=4)
    parser.add_argument("--ref_secs", type=int, default=30)
    parser.add_argument("--patch",    default=None)
    args = parser.parse_args()

    try:
        run(args.src, args.ref, args.output,
            topk=args.topk, ref_secs=args.ref_secs, patch_path=args.patch)
    except Exception as e:
        import traceback
        print(f"ERROR:{e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
