"""
core/ai_pydeps.py
==================
Self-contained Python dependency manager for the AI racks (Demucs, kNN-VC,
Whisper, VoiceFixer).

Why this exists: those four racks used to require the *user* to have their
own system Python with the right packages pip-installed, with the rack UI
just printing terminal commands for them to run by hand. That's a real
source of friction — everyone's system Python setup is different (versions,
PATH, venvs, conda, Windows Store Python, missing entirely on macOS by
default, etc.) — and it's the same class of problem the old Homebrew-linked
hijacker_engine.so had: works on the dev machine, breaks for anyone else.

Instead, this module manages ONE private virtual environment, built with
Blender's own bundled Python (which is guaranteed to exist and be a known,
consistent version — no dependency on what the user happens to have
installed), stored under Blender's per-user scripts folder. Each rack gets
an "INSTALL" button that pip-installs its packages into that shared venv
in a background thread, with live progress shown in the rack's warning
panel. Once installed, `ai_python_finder.find_python_with()` picks the venv
up automatically, so both the dependency check AND the actual processing
subprocess calls use it — no separate wiring needed per rack.

Packages are shared across racks in one venv (torch, numpy etc. overlap
between Demucs/Whisper/VoiceFixer/kNN-VC), so installing more than one
rack's deps doesn't redundantly re-download shared dependencies.
"""

import os
import sys
import platform
import subprocess
import threading
import time


# ---------------------------------------------------------------------------
# Venv location
# ---------------------------------------------------------------------------

def _venv_root():
    """Per-user, per-Blender-version folder to hold the managed venv.

    Lives under Blender's own user scripts resource dir (always writable,
    never requires admin/root — unlike Blender's install folder itself,
    which on Windows/macOS often isn't). Scoped per Blender version because
    the venv is bootstrapped from that version's bundled Python; switching
    Blender versions means rebuilding it, which is just clicking INSTALL
    again.
    """
    import bpy
    base = bpy.utils.user_resource('SCRIPTS', path="hijacker_pydeps", create=True)
    return base


def get_venv_python():
    """Path to the managed venv's Python executable, or None if it doesn't
    exist yet."""
    root = _venv_root()
    if platform.system() == "Windows":
        p = os.path.join(root, "Scripts", "python.exe")
    else:
        p = os.path.join(root, "bin", "python3")
    return p if os.path.isfile(p) else None


def has_venv():
    return get_venv_python() is not None


# ---------------------------------------------------------------------------
# Fast checks against the venv (used by rack dep-checks and the real
# processing python-finder alike)
# ---------------------------------------------------------------------------

def venv_has_pip_package(pip_name, timeout=5):
    """`pip show <pip_name>` against the managed venv. Mirrors the check
    style each rack's existing system-Python scan already uses, so behavior
    is consistent whether the package came from the venv or system Python."""
    vp = get_venv_python()
    if not vp:
        return False
    try:
        r = subprocess.run(
            [vp, "-m", "pip", "show", pip_name],
            capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def venv_has_module(module_name, timeout=5):
    """`import <module_name>` against the managed venv. Mirrors the check
    style find_python_with() uses for system Python candidates."""
    vp = get_venv_python()
    if not vp:
        return False
    try:
        r = subprocess.run(
            [vp, "-c", f"import {module_name}; print('ok')"],
            capture_output=True, timeout=timeout, text=True, encoding="utf-8")
        return r.returncode == 0 and "ok" in r.stdout
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Install state — one entry per rack group ("demucs", "knnvc", "whisper",
# "voicefixer"), polled by each rack's draw code.
# ---------------------------------------------------------------------------

_state = {}   # group -> {"status": IDLE/CREATING_VENV/INSTALLING/DONE/ERROR/CANCELLED,
              #           "message": str, "started": float,
              #           "package": str, "package_size": str, "package_num": int}
_lock  = threading.Lock()

# Live Popen handles for a running install, keyed by group — lets
# cancel_install() actually stop the subprocess rather than just changing
# a status label. Only holds an entry while that group's pip install is
# actually running.
_proc_handles = {}

_STATUS_IDLE          = "IDLE"
_STATUS_CREATING_VENV = "CREATING_VENV"
_STATUS_INSTALLING    = "INSTALLING"
_STATUS_DONE          = "DONE"
_STATUS_ERROR         = "ERROR"
_STATUS_CANCELLED     = "CANCELLED"


def get_state(group):
    return _state.get(group, {"status": _STATUS_IDLE, "message": "", "started": 0.0})


def is_installing(group):
    return get_state(group)["status"] in (_STATUS_CREATING_VENV, _STATUS_INSTALLING)


def get_progress_display(group):
    """Returns (lines, button_label, button_kind) for a rack's warning
    panel to render — button_kind is 'idle' | 'busy' | 'error', which also
    tells the rack how a click on that button should be handled (idle/error
    → start a fresh install, busy → cancel the running one). Centralised
    here so all four racks (Whisper/VoiceFixer/kNN-VC/Demucs) show the same
    thing instead of four copies of this formatting logic drifting apart.

    pip doesn't reliably report byte-level download progress when it isn't
    talking to a real terminal (which a background-thread subprocess never
    is) — so rather than fabricate a percentage, this shows what IS
    reliably available: which package is being fetched, its size (parsed
    from pip's own "Downloading X (Y MB)" line), and how long the install
    has been running.
    """
    st     = get_state(group)
    status = st["status"]

    if status == _STATUS_ERROR:
        return ([st.get("message", "")[:70]], "RETRY INSTALL  >", "error")

    if status in (_STATUS_CREATING_VENV, _STATUS_INSTALLING):
        lines = []
        if status == _STATUS_CREATING_VENV:
            lines.append("Setting up Python environment…")
        else:
            pkg  = st.get("package", "")
            size = st.get("package_size", "")
            num  = st.get("package_num", 0)
            if pkg:
                sz_txt = f" ({size})" if size else ""
                lines.append(f"Installing package {num}: {pkg}{sz_txt}")
            else:
                lines.append((st.get("message") or "Installing…")[:64])
        started = st.get("started", 0.0) or time.time()
        elapsed = max(0.0, time.time() - started)
        mins, secs = divmod(int(elapsed), 60)
        lines.append(f"Elapsed {mins}m {secs:02d}s" if mins else f"Elapsed {secs}s")
        return (lines, "CANCEL", "busy")

    if status == _STATUS_CANCELLED:
        return (["Cancelled."], "INSTALL AUTOMATICALLY  >", "idle")

    return ([], "INSTALL AUTOMATICALLY  >", "idle")


# ---------------------------------------------------------------------------
# Redraw timer — keeps rack panels updating live while a background install
# is running. Small standalone copy of the same pattern core/ai_piper.py
# uses for its download progress, rather than sharing code across modules.
# ---------------------------------------------------------------------------

_redraw_timer_registered = False


def _tag_redraw_all():
    try:
        import bpy
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ('NODE_EDITOR', 'SEQUENCE_EDITOR'):
                    area.tag_redraw()
    except Exception:
        pass


def _redraw_timer():
    _tag_redraw_all()
    any_active = any(is_installing(g) for g in list(_state.keys()))
    if any_active:
        return 0.25
    global _redraw_timer_registered
    _redraw_timer_registered = False
    return None  # unregister


def _ensure_redraw_timer():
    global _redraw_timer_registered
    if _redraw_timer_registered:
        return
    try:
        import bpy
        bpy.app.timers.register(_redraw_timer, first_interval=0.25)
        _redraw_timer_registered = True
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def start_install(group, pip_specs, check_module=None, on_done=None, extra_pip_specs=None):
    """Kick off a background install of `pip_specs` into the managed venv
    for `group` (e.g. "demucs"). No-op if already installing. `on_done` is
    called (on the main-thread-safe timer tick, not from the worker thread
    itself) once the install finishes, whether success or failure — racks
    use it to reset their own dep-check cache so the UI re-checks and flips
    out of the warning panel automatically.

    `extra_pip_specs` installs silently after the main packages (used for
    Demucs's bundled-ffmpeg package) without being called out in progress
    text as a separate step.
    """
    with _lock:
        if is_installing(group):
            return
        _state[group] = {"status": _STATUS_CREATING_VENV,
                          "message": "Setting up…", "started": time.time(),
                          "package": "", "package_size": "", "package_num": 0}
    _ensure_redraw_timer()
    t = threading.Thread(target=_install_worker,
                          args=(group, pip_specs, check_module, on_done, extra_pip_specs),
                          daemon=True)
    t.start()


def cancel_install(group):
    """Stop a running install for `group`. Terminates the pip subprocess if
    one is currently running; _install_worker notices the CANCELLED status
    once pip exits and skips turning that into an ERROR. The venv/whatever
    packages already finished installing are left as-is — clicking INSTALL
    again just resumes (pip skips anything already satisfied)."""
    if not is_installing(group):
        return
    _state[group] = {"status": _STATUS_CANCELLED, "message": "Cancelling…",
                      "started": time.time(), "package": "", "package_size": "",
                      "package_num": 0}
    _tag_redraw_all()
    proc = _proc_handles.get(group)
    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass


def _install_worker(group, pip_specs, check_module, on_done, extra_pip_specs):
    try:
        root = _venv_root()
        vp = get_venv_python()

        if not vp:
            _state[group]["message"] = "Creating Python environment…"
            _tag_redraw_all()
            blender_python = sys.executable
            r = subprocess.run(
                [blender_python, "-m", "venv", root],
                capture_output=True, timeout=120, text=True, encoding="utf-8")
            vp = get_venv_python()
            if not vp:
                err = (r.stderr or r.stdout or "unknown error").strip()[-400:]
                _state[group] = {
                    "status": _STATUS_ERROR,
                    "message": f"Could not create Python environment: {err}",
                    "started": time.time()}
                if on_done:
                    on_done(False)
                return

        if get_state(group)["status"] == _STATUS_CANCELLED:
            if on_done:
                on_done(False)
            return

        _state[group]["status"]  = _STATUS_INSTALLING
        _state[group]["message"] = "Installing…"
        _tag_redraw_all()

        # Best-effort pip upgrade — failure here isn't fatal. Also brings
        # pip itself up to a version that reliably prints the "Collecting
        # X" / "Downloading X (SIZE)" lines the progress parsing below
        # depends on, on whatever pip Blender's bundled Python shipped with.
        try:
            subprocess.run([vp, "-m", "pip", "install", "--upgrade", "pip"],
                            capture_output=True, timeout=120)
        except Exception:
            pass

        all_specs = list(pip_specs) + list(extra_pip_specs or [])
        proc = subprocess.Popen(
            [vp, "-m", "pip", "install", "--disable-pip-version-check"] + all_specs,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", bufsize=1)
        _proc_handles[group] = proc

        # pip doesn't give a live byte-level percentage when it isn't
        # talking to a real terminal (which it never is here — stdout is a
        # pipe to this thread), only these two lines once per package:
        #   "Collecting torch"
        #   "Downloading torch-2.1.0-...whl (192.3 MB)"
        # Parsed out so the rack panel can show which package is being
        # fetched and its size instead of a raw, truncated pip log line.
        import re
        _collect_re = re.compile(r'^Collecting\s+(\S+)')
        _dl_re      = re.compile(r'^Downloading\s+\S+\s+\(([\d.]+\s*[KMGT]?B)\)')

        last_line = ""
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            last_line = line[:70]
            st = _state.get(group)
            if st is None or st.get("status") != _STATUS_INSTALLING:
                continue  # cancelled mid-stream — stop updating progress fields
            st["message"] = last_line
            m = _collect_re.match(line)
            if m:
                st["package"]      = m.group(1)
                st["package_size"] = ""
                st["package_num"]  = st.get("package_num", 0) + 1
            m2 = _dl_re.match(line)
            if m2:
                st["package_size"] = m2.group(1)
        proc.wait()
        _proc_handles.pop(group, None)

        if get_state(group)["status"] == _STATUS_CANCELLED:
            if on_done:
                on_done(False)
            return

        if proc.returncode != 0:
            _state[group] = {
                "status": _STATUS_ERROR,
                "message": f"pip install failed: {last_line}",
                "started": time.time()}
            if on_done:
                on_done(False)
            return

        # check_module may be one module name or a list — a rack like
        # kNN-VC installs more than one top-level package (torch AND
        # torchaudio) with no single wrapper module that would prove both
        # imported successfully, so it needs to verify each one.
        check_modules = ([check_module] if isinstance(check_module, str)
                          else list(check_module) if check_module else [])
        missing = [m for m in check_modules if not venv_has_module(m, timeout=15)]
        if missing:
            _state[group] = {
                "status": _STATUS_ERROR,
                "message": f"Installed, but {', '.join(missing)!r} still won't import.",
                "started": time.time()}
            if on_done:
                on_done(False)
            return

        _state[group] = {"status": _STATUS_DONE, "message": "Installed.",
                          "started": time.time()}
        if on_done:
            on_done(True)

    except Exception as e:
        _state[group] = {"status": _STATUS_ERROR, "message": f"Install error: {e}",
                          "started": time.time()}
        if on_done:
            on_done(False)
    finally:
        _proc_handles.pop(group, None)
        _tag_redraw_all()


# ---------------------------------------------------------------------------
# ffmpeg helper (Demucs only) — `static-ffmpeg` ships self-contained
# ffmpeg AND ffprobe binaries as a pip package (downloading them on first
# use if needed), so it can be installed into the same venv instead of
# asking users to `winget install`/`brew install` ffmpeg separately, or to
# manually copy ffprobe.exe into their Python folder (the old workaround).
# ---------------------------------------------------------------------------

def get_bundled_ffmpeg_dir():
    """Directory containing the static-ffmpeg-bundled ffmpeg/ffprobe
    binaries inside the managed venv, or None if not available. Calls
    static_ffmpeg.add_paths(), which downloads the binaries on first call
    if they aren't already cached, then reads back where it put them."""
    vp = get_venv_python()
    if not vp:
        return None
    try:
        r = subprocess.run(
            [vp, "-c",
             "import static_ffmpeg, os; before=set(os.environ['PATH'].split(os.pathsep)); "
             "static_ffmpeg.add_paths(); after=os.environ['PATH'].split(os.pathsep); "
             "added=[p for p in after if p not in before]; "
             "print(added[0] if added else after[0])"],
            capture_output=True, timeout=60, text=True, encoding="utf-8")
        if r.returncode == 0:
            d = r.stdout.strip()
            return d if d and os.path.isdir(d) else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# AI models directory — one folder holding both the managed venv (packages)
# AND every model checkpoint Whisper/VoiceFixer/kNN-VC/Demucs download on
# first use.
#
# Those four libraries would otherwise each cache their model weights to
# their own default OS location (faster-whisper/huggingface_hub under
# ~/.cache/huggingface, VoiceFixer under ~/.cache/voicefixer, Demucs/kNN-VC
# under ~/.cache/torch via torch.hub) — scattered, undocumented to the user,
# and impossible to answer "where are my files" or "how much space is this
# using" about in one place. get_model_env() below redirects all of that
# into subfolders here instead, via the env vars each library actually
# respects (HF_HOME for huggingface_hub, TORCH_HOME for torch.hub) or, for
# VoiceFixer specifically (which hardcodes "~/.cache/voicefixer" with no
# documented override), by pointing HOME/USERPROFILE at a folder of our own
# for just that one subprocess — VoiceFixer then resolves "~" to OUR folder
# instead of the real user profile, consistently, every run.
#
# Each rack backend (core/ai_whisper.py etc.) merges get_model_env() into
# the env= it passes when launching its processing subprocess — NOT into
# the pip-install subprocess above, since installing the packages never
# downloads model weights; those only download the first time a rack
# actually runs.
# ---------------------------------------------------------------------------

def _models_root():
    root = os.path.join(_venv_root(), "hijacker_models")
    os.makedirs(root, exist_ok=True)
    return root


def get_model_env(base_env=None):
    """Returns an environment dict (based on `base_env`, or the current
    process environment if omitted) with model-cache locations redirected
    into our own managed folder. Pass the result as subprocess.Popen's
    env= when launching a rack's processing subprocess."""
    env = dict(base_env) if base_env is not None else dict(os.environ)

    hf_dir    = os.path.join(_models_root(), "huggingface")
    torch_dir = os.path.join(_models_root(), "torch")
    home_dir  = os.path.join(_models_root(), "home")
    for d in (hf_dir, torch_dir, home_dir):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass

    env["HF_HOME"]               = hf_dir
    env["HUGGINGFACE_HUB_CACHE"] = os.path.join(hf_dir, "hub")
    env["TORCH_HOME"]            = torch_dir
    if platform.system() == "Windows":
        env["USERPROFILE"] = home_dir
    else:
        env["HOME"] = home_dir
    return env


def get_storage_usage():
    """Total bytes used by the managed venv + all downloaded AI models.
    Walks the whole folder tree — can take a moment once site-packages has
    torch etc. in it, so callers should run this off the main thread (see
    refresh_storage_usage() below) rather than calling it from draw code."""
    root  = _venv_root()
    total = 0
    if os.path.isdir(root):
        for dirpath, _dirs, filenames in os.walk(root):
            for fname in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fname))
                except Exception:
                    pass
    return total


def format_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


# Cached async storage total — get_storage_usage() walks the whole folder
# tree, which can be slow once torch etc. are installed, so the settings
# popup triggers this in a background thread rather than blocking a draw.
_storage_cache = {"status": "IDLE", "bytes": 0}   # status: IDLE/CALCULATING/DONE


def get_storage_cache():
    return _storage_cache


def refresh_storage_usage():
    if _storage_cache["status"] == "CALCULATING":
        return
    _storage_cache["status"] = "CALCULATING"

    def _worker():
        total = get_storage_usage()
        _storage_cache["bytes"]  = total
        _storage_cache["status"] = "DONE"
        _tag_redraw_all()

    threading.Thread(target=_worker, name="AIStorageUsage", daemon=True).start()


def open_models_folder():
    """Open the managed venv + models folder in Explorer/Finder/file
    manager. Returns True on success."""
    root = _venv_root()
    try:
        os.makedirs(root, exist_ok=True)
        system = platform.system()
        if system == "Windows":
            os.startfile(root)
        elif system == "Darwin":
            subprocess.Popen(["open", root])
        else:
            subprocess.Popen(["xdg-open", root])
        return True
    except Exception as e:
        print(f"[AI PYDEPS] could not open models folder: {e}")
        return False


def any_group_installing():
    """True if ANY rack group currently has an install running — used to
    block deleting the shared folder out from under a live pip subprocess."""
    return any(is_installing(g) for g in ("whisper", "voicefixer", "knnvc", "demucs"))


# ---------------------------------------------------------------------------
# Move-to-trash helpers
# ---------------------------------------------------------------------------
# delete_everything() below can remove several GB that took real time to
# download, so we make it recoverable the normal way a user expects — via
# the OS Recycle Bin (Windows) or Trash (macOS/Linux) — instead of a raw
# shutil.rmtree, which deletes at the filesystem level with no OS undo.
# Each of these raises on failure; the caller decides whether to fall back
# to a permanent delete.

def _trash_windows(path):
    """Move `path` to the Windows Recycle Bin via the Shell API — the exact
    same mechanism as dragging a folder there in Explorer, so Explorer's
    own Restore works on it afterward."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd",                  wintypes.HWND),
            ("wFunc",                 wintypes.UINT),
            ("pFrom",                 wintypes.LPCWSTR),
            ("pTo",                   wintypes.LPCWSTR),
            ("fFlags",                ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings",         ctypes.c_void_p),
            ("lpszProgressTitle",     wintypes.LPCWSTR),
        ]

    FO_DELETE          = 0x0003
    FOF_ALLOWUNDO      = 0x0040   # send to Recycle Bin instead of a hard delete
    FOF_NOCONFIRMATION = 0x0010   # we already confirmed in our own UI
    FOF_SILENT         = 0x0004   # no Windows progress dialog
    FOF_NOERRORUI      = 0x0400

    # pFrom must be double-null-terminated. create_unicode_buffer() always
    # appends its own terminator, so one explicit "\0" here gives us two.
    from_buf = ctypes.create_unicode_buffer(path + "\0")

    op = SHFILEOPSTRUCTW()
    op.hwnd   = None
    op.wFunc  = FO_DELETE
    op.pFrom  = ctypes.cast(from_buf, wintypes.LPCWSTR)
    op.pTo    = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted:
        raise OSError(f"SHFileOperationW returned {result} (aborted={op.fAnyOperationsAborted})")


def _trash_macos(path):
    """Move `path` to the macOS Trash via Finder — recoverable from the
    Trash exactly like a normal Finder delete."""
    script = f'tell application "Finder" to delete POSIX file "{path}"'
    r = subprocess.run(["osascript", "-e", script],
                        capture_output=True, timeout=60, text=True)
    if r.returncode != 0:
        raise OSError(f"osascript trash failed: {(r.stderr or r.stdout).strip()}")


def _trash_linux(path):
    """Move `path` to the freedesktop trash via `gio trash` (ships with
    GNOME / most modern desktop distros)."""
    r = subprocess.run(["gio", "trash", path],
                        capture_output=True, timeout=60, text=True)
    if r.returncode != 0:
        raise OSError(f"gio trash failed: {(r.stderr or r.stdout).strip()}")


def _move_to_trash(path):
    """Best-effort move of `path` to the OS Recycle Bin/Trash.

    Returns True if it landed in the trash (normal OS undo now works on
    it), or False if the trash mechanism wasn't available on this machine
    (e.g. no `gio` on a minimal Linux install) and the caller should fall
    back to a permanent delete."""
    system = platform.system()
    try:
        if system == "Windows":
            _trash_windows(path)
        elif system == "Darwin":
            _trash_macos(path)
        else:
            _trash_linux(path)
        return True
    except Exception as e:
        print(f"[AI PYDEPS] move-to-trash unavailable on {system}, "
              f"falling back to a permanent delete: {e}")
        return False


def delete_everything():
    """Deletes the ENTIRE managed folder — the shared venv (so all four
    racks go back to needing INSTALL again) and every downloaded model.
    Refuses while an install is running.

    Tries to move the folder to the OS Recycle Bin/Trash first, so an
    accidental confirm can still be undone the normal way. Only falls back
    to a permanent shutil.rmtree delete if the OS trash mechanism isn't
    available on this machine.

    Returns a dict: {"ok": bool, "trashed": bool, "reason": str|None}.
    "trashed" tells the caller whether this can be undone via Recycle
    Bin/Trash (True) or was a permanent delete (False).
    """
    import shutil
    if any_group_installing():
        print("[AI PYDEPS] refusing to delete — an install is running")
        return {"ok": False, "trashed": False, "reason": "installing"}

    root = _venv_root()
    trashed = False
    try:
        if os.path.isdir(root):
            trashed = _move_to_trash(root)
            if not trashed:
                shutil.rmtree(root, ignore_errors=True)
        _state.clear()
        _storage_cache["status"] = "IDLE"
        _storage_cache["bytes"]  = 0
        if trashed:
            print("[AI PYDEPS] moved managed Python environment + all "
                  "downloaded AI models to the Recycle Bin/Trash")
        else:
            print("[AI PYDEPS] permanently deleted managed Python "
                  "environment + all downloaded AI models "
                  "(Recycle Bin/Trash unavailable)")
        return {"ok": True, "trashed": trashed, "reason": None}
    except Exception as e:
        print(f"[AI PYDEPS] delete failed: {e}")
        return {"ok": False, "trashed": False, "reason": str(e)}
