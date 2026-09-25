r"""
core/ai_python_finder.py
========================
Shared utility for finding system Python with a required package.
Used by all AI rack backends to locate the correct Python executable
regardless of OS or whether Blender inherits the user's PATH.

Usage:
    from core.ai_python_finder import find_python_with
    cmd = find_python_with("voicefixer")   # returns e.g. [r"C:\...\python.exe"]
    cmd = find_python_with("faster_whisper")
"""

import os
import subprocess
import platform


def _win_python_paths():
    """Return candidate Python executable paths on Windows."""
    paths = []

    # USERPROFILE is always set on Windows and works inside subprocesses
    # even when ~ expansion fails. More reliable than os.path.expanduser.
    user_home = os.environ.get("USERPROFILE") or os.path.expanduser("~")

    for ver in ["312", "311", "310", "39", "38"]:
        ver_dot = f"{ver[0]}.{ver[1:]}"   # "312" -> "3.12"
        candidates = [
            # Standard user install (most common)
            os.path.join(user_home, "AppData", "Local", "Programs",
                         "Python", f"Python{ver}", "python.exe"),
            # System-wide install
            f"C:\\Python{ver}\\python.exe",
            f"C:\\Program Files\\Python{ver}\\python.exe",
            f"C:\\Program Files (x86)\\Python{ver}\\python.exe",
            # Windows Store Python (in WindowsApps — usually not useful but try)
            os.path.join(user_home, "AppData", "Local", "Microsoft",
                         "WindowsApps", f"python{ver_dot}.exe"),
            # Conda/Miniconda common locations
            os.path.join(user_home, "miniconda3", "python.exe"),
            os.path.join(user_home, "anaconda3", "python.exe"),
            os.path.join(user_home, "miniconda3", "envs", "audio", "python.exe"),
        ]
        for p in candidates:
            if os.path.isfile(p):
                paths.append([p])

    # py launcher — works if Python Launcher for Windows is installed
    for ver in ["3.12", "3.11", "3.10", "3.9"]:
        paths.append(["py", f"-{ver}"])

    # Plain names as last resort
    paths += [["python"], ["python3"]]
    return paths


def _mac_python_paths():
    """Return candidate Python executable paths on macOS."""
    paths = []
    for p in [
        # Homebrew (Apple Silicon and Intel)
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        # pyenv
        os.path.expanduser("~/.pyenv/shims/python3"),
        # Conda
        os.path.expanduser("~/miniconda3/bin/python3"),
        os.path.expanduser("~/anaconda3/bin/python3"),
        os.path.expanduser("~/miniconda3/envs/audio/bin/python3"),
        # System
        "/usr/bin/python3",
    ]:
        if os.path.isfile(p):
            paths.append([p])
    paths += [["python3"], ["python"]]
    return paths


def _linux_python_paths():
    """Return candidate Python executable paths on Linux."""
    paths = []
    for p in [
        "/usr/local/bin/python3",
        "/usr/bin/python3",
        os.path.expanduser("~/.local/bin/python3"),
        os.path.expanduser("~/miniconda3/bin/python3"),
        os.path.expanduser("~/anaconda3/bin/python3"),
        os.path.expanduser("~/miniconda3/envs/audio/bin/python3"),
    ]:
        if os.path.isfile(p):
            paths.append([p])
    paths += [["python3"], ["python"]]
    return paths


def find_python_with(package, timeout=4):
    """Find a Python executable that has `package` importable.

    `package` is normally one module name, but can also be a list/tuple of
    module names that must ALL be importable in the same interpreter — for
    a rack like kNN-VC that needs more than one top-level package (torch
    AND torchaudio) with no single wrapper module that would transitively
    prove both are present, checking just one of them is a false-positive
    trap: a Python that happens to have torch but not torchaudio would
    incorrectly be reported as "ready".

    Returns the command list (e.g. [r'C:\\...\\python.exe']) or None.
    Checks the addon's own managed venv first (see core/ai_pydeps.py) —
    populated by each rack's "INSTALL" button, so it needs nothing from the
    user's own system Python setup. Falls back to scanning OS-specific
    common install locations for anyone who already has a working system
    Python with the package installed the old way.
    """
    packages = [package] if isinstance(package, str) else list(package)

    try:
        from core.ai_pydeps import get_venv_python, venv_has_module
        vp = get_venv_python()
        if vp and all(venv_has_module(p, timeout=timeout) for p in packages):
            return [vp]
    except Exception:
        pass

    sys = platform.system()
    if sys == "Windows":
        candidates = _win_python_paths()
    elif sys == "Darwin":
        candidates = _mac_python_paths()
    else:
        candidates = _linux_python_paths()

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for cmd in candidates:
        key = " ".join(cmd)
        if key not in seen:
            seen.add(key)
            deduped.append(cmd)

    import_stmt = "; ".join(f"import {p}" for p in packages)
    for cmd in deduped:
        try:
            r = subprocess.run(
                cmd + ["-c", f"{import_stmt}; print('ok')"],
                capture_output=True, timeout=timeout,
                text=True, encoding="utf-8",
            )
            if r.returncode == 0 and "ok" in r.stdout:
                return cmd
        except Exception:
            continue

    return None
