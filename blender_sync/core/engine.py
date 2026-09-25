# =============================================================================
# core/engine.py
# The Hijacker — loads hijacker_engine.pyd/.so and exposes get_engine().
#
# The compiled engine is looked up by *platform + architecture + running
# Python's ABI*, not by Blender's version number. Blender's bundled Python
# only changes major.minor occasionally (3.11 for Blender 4.1-5.0, 3.13 from
# Blender 5.1 onward, "matching VFX Platform 2026" per Blender's own 5.1
# Python API release notes) — keying off the Python ABI directly means a
# single build keeps working across every Blender release on that ABI line,
# with no rebuild needed just because Blender shipped a new minor version.
# It also means a macOS build and a Linux build can't collide on the same
# filename, since each platform+arch+ABI combination gets its own subfolder
# under hijacker_native/ (same pattern core/ai_piper.py already uses for its
# per-platform piper binaries) instead of all sharing one flat filename.
# =============================================================================

import os
import sys
import platform
import importlib.util

_ADDON_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NATIVE_DIR = os.path.join(_ADDON_DIR, "hijacker_native")

_engine          = None
_engine_attempted = False   # True once _import_engine() has been tried, win or lose —
                             # distinguishes "never checked yet" from "checked and it
                             # failed", so a failed load doesn't retry (and reprint its
                             # warnings) on every single call. get_engine() used to be
                             # called from the meter timer (~every 10ms), so a failed
                             # load without this flag spammed the console nonstop.


def _platform_tag():
    """Return this machine's hijacker_native/ subfolder name, e.g.
    'win_amd64_py311', 'macos_arm64_py313', 'linux_x86_64_py311'."""
    system  = platform.system().lower()
    machine = platform.machine().lower()
    py_tag  = f"py{sys.version_info[0]}{sys.version_info[1]}"

    if system == "windows":
        plat = "win"
        arch = "amd64" if machine in ("amd64", "x86_64") else machine
    elif system == "darwin":
        plat = "macos"
        arch = "arm64" if "arm" in machine else "x86_64"
    else:
        plat = "linux"
        arch = "x86_64" if machine in ("x86_64", "amd64") else machine

    return f"{plat}_{arch}_{py_tag}"


def _engine_path(tag):
    ext = "pyd" if platform.system().lower() == "windows" else "so"
    return os.path.join(_NATIVE_DIR, tag, f"hijacker_engine.{ext}")


def _import_engine():
    import bpy
    b_major, b_minor, _ = bpy.app.version
    tag  = _platform_tag()
    path = _engine_path(tag)

    if not os.path.isfile(path):
        print(f"[HIJACKER] WARNING: no compiled engine for this platform/Python — "
              f"expected {path}")
        print(f"[HIJACKER]   Blender {b_major}.{b_minor}, Python {platform.python_version()}, "
              f"{platform.system()}/{platform.machine()}")
        print("[HIJACKER]   compile with build.bat / build_py313.bat (Windows) or the "
              "matching .so build (macOS/Linux) — see .github/workflows/build.yml")
        return None

    try:
        # Loaded from an explicit path rather than name-searched on sys.path,
        # so a Windows build, a macOS build and a Linux build can all be
        # named plainly "hijacker_engine.pyd"/".so" without colliding — only
        # one is ever actually present for whichever platform is running.
        spec = importlib.util.spec_from_file_location("hijacker_engine", path)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules["hijacker_engine"] = mod
        spec.loader.exec_module(mod)
        print(f"[HIJACKER] loaded '{tag}/hijacker_engine' for Blender {b_major}.{b_minor}")
        return mod
    except ImportError as e:
        # Keep the real reason — a missing native dependency (e.g. a
        # PortAudio dylib not found via Homebrew on macOS) or an
        # architecture/ABI mismatch both raise ImportError here, and
        # without the original message they're indistinguishable from
        # the file simply not existing.
        sys.modules.pop("hijacker_engine", None)
        print(f"[HIJACKER] WARNING: found {path} but it failed to load: {e}")
        return None


def get_engine():
    global _engine, _engine_attempted
    if not _engine_attempted:
        _engine_attempted = True
        _engine = _import_engine()
    return _engine


def reset_engine():
    """Force the next get_engine() call to retry the import (and, if it
    fails again, reprint the warning once) — e.g. after recompiling the
    engine without restarting Blender."""
    global _engine, _engine_attempted
    _engine            = None
    _engine_attempted  = False


# Kept for import compatibility — no longer installs pedalboard wheel
PEDALBOARD_AVAILABLE = True
HIJACKER_AVAILABLE   = True
