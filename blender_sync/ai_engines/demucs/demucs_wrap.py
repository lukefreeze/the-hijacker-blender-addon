"""
ai_engines/demucs/demucs_wrap.py
================================
Launches `python -m demucs ...` with torch.hub's model download swapped for
a resumable one. Download progress goes to stderr as
    HJDL|<label>|<bytes done>|<bytes total>
which demucs_runner.py turns into STATUS/PROGRESS lines for the rack.
"""
import os
import sys
import runpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _factory(label):
    def emit(done, total):
        sys.stderr.write(f"HJDL|{label}|{int(done)}|{int(total)}\n")
        sys.stderr.flush()
    return emit


if __name__ == "__main__":
    try:
        import hj_download as hd
        hd.install_torch_hub_patch("Demucs model", emit_factory=_factory, print_done=False)
    except Exception as e:      # never block separation on the nicety
        sys.stderr.write(f"demucs_wrap: download patch skipped: {e}\n")
    sys.argv = ["demucs"] + sys.argv[1:]
    runpy.run_module("demucs", run_name="__main__", alter_sys=True)
