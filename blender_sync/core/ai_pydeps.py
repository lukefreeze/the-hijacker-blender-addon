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
        if r.returncode == 0 and "ok" in r.stdout:
            return True
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no output"]
        # "No module named X" just means it is not installed yet, which is the
        # normal state before an install: only report genuine load failures.
        if "ModuleNotFoundError" not in tail[0] and "No module named" not in tail[0]:
            print(f"[AI PYDEPS] 'import {module_name}' failed in the add-on environment "
                  f"(exit {r.returncode}): {tail[0][:300]}")
        return False
    except subprocess.TimeoutExpired:
        print(f"[AI PYDEPS] 'import {module_name}' did not finish within {timeout}s "
              "(first start after an install can be slow)")
        return False
    except Exception as e:
        print(f"[AI PYDEPS] could not test 'import {module_name}': {e}")
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


def get_install_percent(group):
    """0-100 progress of a running install (an estimate: pip only reveals the
    total download size as it goes), or 100 once finished."""
    st = get_state(group)
    if st["status"] == _STATUS_DONE:
        return 100
    return int(st.get("pct", 0.0))


def get_error_text(group):
    """The full text of the last install error (what a user would paste into
    a bug report), or "" when there is none."""
    st = get_state(group)
    if st.get("status") != _STATUS_ERROR:
        return ""
    return st.get("detail") or st.get("message", "")


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
        return ([st.get("message", "")], "RETRY INSTALL  >", "error")

    if status in (_STATUS_CREATING_VENV, _STATUS_INSTALLING):
        lines = []
        if st.get("waiting"):
            lines.append("Waiting for another install to finish…")
            return (lines, "CANCEL", "busy")
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

def _pick_error_line(tail, fallback):
    """pip's last line is often boilerplate ("note: This error originates from a
    subprocess..."). Prefer the most recent real "ERROR:" line, or a known
    helpful hint, so the rack shows something a person can act on."""
    import re as _re
    hint = None
    for ln in reversed(tail):
        t = ln.strip()
        if not t:
            continue
        if hint is None and ("pkg-config is required" in t or "No matching distribution" in t
                             or "Could not find a version" in t):
            hint = t
        m = _re.match(r"^ERROR:\s*(.+)$", t)
        if m and "subprocess-exited-with-error" not in m.group(1):
            return (m.group(1) + ("  (" + hint + ")" if hint and hint not in m.group(1) else ""))[:400]
    return (hint or fallback)[:400]


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


_venv_lock = threading.Lock()   # one rack at a time creates the venv / upgrades pip
_pip_ready = set()              # venv pythons whose pip has been verified this session


def _pip_works(vp):
    try:
        r = subprocess.run([vp, "-m", "pip", "--version"], capture_output=True,
                           timeout=60, text=True, encoding="utf-8", errors="replace")
        return r.returncode == 0
    except Exception:
        return False


def _pip_version_major(vp):
    """Major version of the venv's pip, or None if pip does not run."""
    try:
        r = subprocess.run([vp, "-m", "pip", "--version"], capture_output=True,
                           timeout=60, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            return None
        import re as _re
        m = _re.match(r"pip\s+(\d+)", r.stdout.strip())
        return int(m.group(1)) if m else 0
    except Exception:
        return None


def _wipe_pip_files(vp):
    """Delete pip's own folders from the venv so ensurepip can lay down a clean copy.
    (ensurepip alone keeps a damaged pip: it sees the right version and does nothing.)"""
    import glob
    import shutil
    root = os.path.dirname(os.path.dirname(vp))
    sites = [os.path.join(root, "Lib", "site-packages")]
    sites += glob.glob(os.path.join(root, "lib", "python*", "site-packages"))
    for sp in sites:
        for pat in ("pip", "pip-*.dist-info", "pip-*.egg-info"):
            for path in glob.glob(os.path.join(sp, pat)):
                shutil.rmtree(path, ignore_errors=True)


def _repair_pip(vp):
    """Make the venv's pip work again. Returns True when `pip --version` runs."""
    def _ensurepip():
        try:
            subprocess.run([vp, "-m", "ensurepip", "--upgrade"], capture_output=True,
                           timeout=180, text=True, encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"[AI PYDEPS] ensurepip failed: {e}")
    _ensurepip()
    if _pip_works(vp):
        return True
    print("[AI PYDEPS] pip still damaged after ensurepip; reinstalling pip's files")
    try:
        _wipe_pip_files(vp)
    except Exception as e:
        print(f"[AI PYDEPS] could not clear pip files: {e}")
    _ensurepip()
    return _pip_works(vp)


def _looks_like_broken_pip(text):
    t = (text or "").lower()
    return ("no module named 'pip" in t or "no module named pip" in t
            or "pip._internal" in t and "modulenotfounderror" in t)


# Only one pip install may run at a time. All racks share one virtual
# environment, and two pips changing it together can collide (for example
# "[WinError 5] Access is denied" on torch's _C.pyd while another rack's pip
# or a freshly started Python has that file open, or a half-removed package).
# Installs started while another is running wait their turn. An RLock so the
# retry path below (same thread calling _install_worker again) can re-enter.
_install_run_lock = threading.RLock()


def _import_error_text(vp, module_name, timeout=120):
    """Run `import <module>` in the managed venv; return None if it works,
    otherwise the error text (or a note that it timed out)."""
    try:
        r = subprocess.run([vp, "-c", f"import {module_name}"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        if r.returncode == 0:
            return None
        return (r.stderr or r.stdout or "import failed").strip()
    except subprocess.TimeoutExpired:
        return f"import timed out after {timeout}s"
    except Exception as e:
        return str(e)


def _heal_broken_import(vp, module_name, group):
    """pip said the install worked but `import <module>` fails (typically a
    package left half-written by an interrupted or overlapping install, e.g.
    PyAV's 'DLL load failed while importing _core'). Find the package named
    in the traceback and force-reinstall just that one. Returns True if the
    module imports afterwards."""
    import re
    err = _import_error_text(vp, module_name)
    if err is None:
        return True
    names = re.findall(r"site-packages[\\/]+([A-Za-z0-9_]+)", err)
    names = [n for n in names if not n.startswith("_")]
    if not names:
        print(f"[AI PYDEPS] {group}: import {module_name} fails and no package could be "
              f"identified: {err.splitlines()[-1][:300]}")
        return False
    pkg = names[-1]
    print(f"[AI PYDEPS] {group}: import {module_name} failed ({err.splitlines()[-1][:200]}); "
          f"reinstalling '{pkg}'")
    try:
        _state[group]["message"] = f"Repairing {pkg}…"
        _tag_redraw_all()
    except Exception:
        pass
    try:
        subprocess.run([vp, "-m", "pip", "install", "--disable-pip-version-check",
                        "--force-reinstall", "--no-deps", pkg],
                       capture_output=True, text=True, timeout=900)
    except Exception as e:
        print(f"[AI PYDEPS] {group}: reinstall of '{pkg}' failed: {e}")
        return False
    err = _import_error_text(vp, module_name)
    if err is not None:
        print(f"[AI PYDEPS] {group}: still failing after reinstalling '{pkg}': "
              f"{err.splitlines()[-1][:300]}")
        return False
    return True


def _install_worker(group, pip_specs, check_module, on_done, extra_pip_specs, _retry=False):
    if not _install_run_lock.acquire(blocking=False):
        try:
            _state[group]["waiting"] = True
        except Exception:
            pass
        _tag_redraw_all()
        _install_run_lock.acquire()
    try:
        try:
            _state[group]["waiting"] = False
        except Exception:
            pass
        if get_state(group)["status"] == _STATUS_CANCELLED:
            if on_done:
                on_done(False)
            return
        _install_worker_locked(group, pip_specs, check_module, on_done,
                               extra_pip_specs, _retry)
    finally:
        _install_run_lock.release()


def _install_worker_locked(group, pip_specs, check_module, on_done, extra_pip_specs, _retry=False):
    try:
        root = _venv_root()

        # Creating the venv and upgrading pip must never overlap between
        # racks: one rack replacing pip's files while another rack's pip is
        # running makes that pip die with "No module named 'pip._internal'".
        with _venv_lock:
            cleanup_pending_deletes()
            vp = get_venv_python()
            if not vp:
                _state[group]["message"] = "Creating Python environment…"
                _tag_redraw_all()
                blender_python = sys.executable
                r = subprocess.run(
                    [blender_python, "-m", "venv", root],
                    capture_output=True, timeout=120, text=True, encoding="utf-8",
                    errors="replace")
                vp = get_venv_python()
                if not vp:
                    err = (r.stderr or r.stdout or "unknown error").strip()[-400:]
                    _state[group] = {
                        "status": _STATUS_ERROR,
                        "message": f"Could not create Python environment: {err}",
                        "detail": (r.stderr or r.stdout or "").strip()[-3000:],
                        "started": time.time()}
                    if on_done:
                        on_done(False)
                    return

            if vp not in _pip_ready:
                if not _pip_works(vp):
                    _state[group]["message"] = "Repairing pip…"
                    _tag_redraw_all()
                    _repair_pip(vp)
                # Upgrade pip only when it is old (the progress parsing below
                # needs a reasonably recent one). A modern pip is left alone:
                # an upgrade that gets interrupted leaves pip half-removed.
                _major = _pip_version_major(vp)
                if _major is not None and _major < 23:
                    try:
                        subprocess.run([vp, "-m", "pip", "install", "--upgrade", "pip"],
                                       capture_output=True, timeout=600)
                    except Exception:
                        pass
                if not _pip_works(vp):
                    _repair_pip(vp)
                if _pip_works(vp):
                    _pip_ready.add(vp)
                else:
                    _state[group] = {
                        "status": _STATUS_ERROR,
                        "message": "pip inside the add-on's Python environment is damaged and "
                                   "could not be repaired. Delete the AI models from the AI "
                                   "settings menu and install again.",
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

        all_specs = list(pip_specs) + list(extra_pip_specs or [])
        # `--progress-bar raw` makes pip print plain "Progress <done> of
        # <total>" byte counts per download even though it is not talking to
        # a terminal, which is what the percentage below is built from.
        # Only used when this pip lists the option (older pips reject it).
        _raw_args = []
        try:
            _help = subprocess.run([vp, "-m", "pip", "install", "--help"], capture_output=True,
                                   text=True, timeout=30).stdout
            if "--progress-bar" in _help and "raw" in _help.split("--progress-bar", 1)[1][:300]:
                _raw_args = ["--progress-bar", "raw"]
        except Exception:
            pass
        # --prefer-binary: take a ready-made wheel for this Python even if a newer
        # version exists only as a source package. Some package mirrors drop the
        # "requires Python 3.12+" metadata, so pip picks a brand-new release that
        # has no wheel for Blender 4.5's Python 3.11 and then fails trying to
        # build it (NumPy, PyAV...). Packages that only ship source (demucs)
        # still install from source as before.
        proc = subprocess.Popen(
            [vp, "-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary"]
            + _raw_args + all_specs,
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
        _dl_re      = re.compile(r'^Downloading\s+(\S+)\s+\(([\d.]+)\s*([kKMGT]?B)\)')
        _prog_re    = re.compile(r'^Progress\s+(\d+)\s+of\s+(\d+)')
        _have_re    = re.compile(r'^Requirement already satisfied:\s+([A-Za-z0-9_.\-]+)')
        _units      = {"B": 1.0, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}

        # Rough size (MB) of what each rack normally has to download on a
        # fresh machine. Only a floor for the percentage's denominator
        # (the real total is not known until pip has seen every package).
        _floor_mb = {"demucs": 300.0, "knnvc": 300.0, "voicefixer": 450.0, "whisper": 120.0}.get(group, 200.0)
        _shared_mb = {"torch": 120.0, "torchaudio": 10.0, "numpy": 15.0}
        pr = {"completed": 0.0, "cur_total": 0.0, "cur_done": 0.0,
              "floor": _floor_mb * 1e6, "phase": "resolve", "t_inst": 0.0}

        def _update_pct(st):
            if pr["phase"] == "install":
                # no byte counts while pip unpacks: creep from 90 toward 98
                new = min(98.0, 90.0 + (time.time() - pr["t_inst"]) * 0.25)
            elif pr["completed"] + pr["cur_total"] > 0:
                denom = max(pr["completed"] + pr["cur_total"], pr["floor"])
                frac = (pr["completed"] + pr["cur_done"]) / denom
                new = 3.0 + 85.0 * min(1.0, frac)
            else:
                new = 2.0
            st["pct"] = max(st.get("pct", 0.0), new)     # never goes backwards

        last_line = ""
        _tail = []
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            st = _state.get(group)
            if st is None or st.get("status") != _STATUS_INSTALLING:
                continue  # cancelled mid-stream — stop updating progress fields
            mp = _prog_re.match(line)
            if mp:                                   # byte progress of the current file
                pr["cur_done"] = float(mp.group(1))
                if int(mp.group(2)) > 0:
                    pr["cur_total"] = float(mp.group(2))
                _update_pct(st)
                continue
            last_line = line[:400]
            _tail.append(line[:400])
            if len(_tail) > 25:
                del _tail[0]
            st["message"] = last_line
            m = _collect_re.match(line)
            if m:
                st["package"]      = m.group(1)
                st["package_size"] = ""
                st["package_num"]  = st.get("package_num", 0) + 1
            m2 = _dl_re.match(line)
            if m2 and not m2.group(1).endswith(".metadata"):   # (metadata stubs are tiny: ignore)
                st["package"]      = m2.group(1).split("-")[0]
                st["package_size"] = f"{m2.group(2)} {m2.group(3)}"
                pr["completed"] += pr["cur_total"]   # previous file finished
                pr["cur_done"] = 0.0
                try:
                    pr["cur_total"] = float(m2.group(2)) * _units.get(m2.group(3).upper(), 1.0)
                except Exception:
                    pr["cur_total"] = 0.0
                pr["phase"] = "download"
            m3 = _have_re.match(line)
            if m3 and m3.group(1).lower() in _shared_mb:   # already on disk: less to fetch
                pr["floor"] = max(30e6, pr["floor"] - _shared_mb[m3.group(1).lower()] * 1e6)
            if line.startswith("Installing collected packages"):
                pr["completed"] += pr["cur_total"]
                pr["cur_total"] = pr["cur_done"] = 0.0
                pr["phase"] = "install"
                pr["t_inst"] = time.time()
                st["package"] = ""
                st["message"] = "Installing packages…"
            _update_pct(st)
        proc.wait()
        _proc_handles.pop(group, None)

        if get_state(group)["status"] == _STATUS_CANCELLED:
            if on_done:
                on_done(False)
            return

        if proc.returncode != 0:
            _detail = "\n".join(_tail)
            if not _retry and _looks_like_broken_pip(_detail):
                # pip itself was damaged (e.g. replaced mid-run): repair it and go again once
                print("[AI PYDEPS] pip looks broken; repairing and retrying once")
                with _venv_lock:
                    _pip_ready.discard(vp)
                    _fixed = _repair_pip(vp)
                    if _fixed:
                        _pip_ready.add(vp)
                if _fixed:
                    _state[group]["status"]  = _STATUS_INSTALLING
                    _state[group]["message"] = "Installing…"
                    _install_worker(group, pip_specs, check_module, on_done,
                                    extra_pip_specs, _retry=True)
                    return
            print(f"[AI PYDEPS] {group}: pip install failed:\n{_detail}")
            _state[group] = {
                "status": _STATUS_ERROR,
                "message": f"pip install failed: {_pick_error_line(_tail, last_line)}",
                "detail": f"pip install failed ({group}):\n{_detail}",
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
        missing = [m for m in check_modules if not venv_has_module(m, timeout=60)]
        # Packages that installed but won't load: repair them once.
        missing = [m for m in missing if not _heal_broken_import(vp, m, group)]
        if missing:
            _err = (_import_error_text(vp, missing[0], timeout=120) or "").strip()
            _last = _err.splitlines()[-1][:160] if _err else ""
            # Some Blender builds ship a Python without python3.dll, which
            # packages built against Python's stable ABI (e.g. PyAV 18) need.
            try:
                if (sys.platform == "win32" and "DLL load failed" in _err and not
                        os.path.exists(os.path.join(sys.base_prefix, "bin", "python3.dll"))):
                    _last = ("This Blender's built-in Python has no python3.dll, which this "
                             "package needs. Please use Blender 4.5 or newer.")
            except Exception:
                pass
            _state[group] = {
                "status": _STATUS_ERROR,
                "message": f"Installed, but {', '.join(missing)!r} still won't import. {_last}".strip(),
                "detail": _err[-3000:],
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
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"   # runners' stderr is piped; bars are just noise there
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


def invalidate_dep_caches():
    """Forget every rack's cached "dependencies found" answer, so each AI rack
    re-checks and goes back to its INSTALL screen when its packages are gone
    (used after deleting the managed environment). Safe to call any time."""
    targets = (
        ("ui.racks.rack_demucs",     "_dm_dep_cache"),
        ("ui.racks.rack_knnvc",      "_dep_cache"),
        ("ui.racks.rack_voicefixer", "_vf_dep_cache"),
        ("ui.racks.rack_whisper",    "_dep_cache"),
    )
    import importlib
    for mod_name, attr in targets:
        try:
            mod = importlib.import_module(mod_name)
            cache = getattr(mod, attr)
            cache["ok"] = False
            cache["checked"] = False
            if "when" in cache:
                cache["when"] = 0.0
        except Exception as e:
            print(f"[AI PYDEPS] could not reset {mod_name} dependency check: {e}")
    try:
        import bpy
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ('NODE_EDITOR', 'SEQUENCE_EDITOR'):
                    area.tag_redraw()
    except Exception:
        pass


_PENDING_TAG = ".deleting-"


def _force_rmtree(path):
    """Delete a folder tree, clearing read-only flags, using extended-length
    paths on Windows (model caches nest deeper than the old 260-character
    limit). Returns True if the folder is gone afterwards."""
    import shutil, stat
    target = path
    if platform.system() == "Windows":
        ap = os.path.abspath(path)
        if not ap.startswith("\\\\?\\"):
            target = "\\\\?\\" + ap

    def _on_err(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    for _ in range(3):
        try:
            shutil.rmtree(target, onerror=_on_err)
        except Exception:
            pass
        if not os.path.exists(path):
            return True
        time.sleep(0.5)
    return not os.path.exists(path)


def cleanup_pending_deletes():
    """Remove folders left behind by an earlier delete that could not finish
    (files still locked then). They are renamed out of the way first, so the
    live environment is never half-deleted; this just finishes the job."""
    try:
        root = _venv_root()
        parent = os.path.dirname(root.rstrip("\\/"))
        base = os.path.basename(root.rstrip("\\/")) + _PENDING_TAG
        for name in os.listdir(parent):
            if name.startswith(base):
                _force_rmtree(os.path.join(parent, name))
    except Exception as e:
        print(f"[AI PYDEPS] could not clean up an old deleted folder: {e}")


def delete_everything():
    """Deletes the ENTIRE managed folder: the shared venv (so all four racks
    go back to needing INSTALL again) and every downloaded model.
    Refuses while an install is running, or while anything is still using
    the folder (a download or AI job in progress).

    The folder is first RENAMED out of the way. That is an all-or-nothing
    check: Windows refuses to rename a folder with a running program or open
    file inside it, so a half-deleted environment (the cause of the damaged
    pip we kept seeing) can no longer happen. The renamed folder is then
    moved to the Recycle Bin/Trash, or deleted if that is not possible.

    Returns a dict: {"ok": bool, "trashed": bool, "reason": str|None}.
    reason == "installing" / "in_use" when it refused.
    """
    if any_group_installing():
        print("[AI PYDEPS] refusing to delete: an install is running")
        return {"ok": False, "trashed": False, "reason": "installing"}

    root = _venv_root()
    trashed = False
    try:
        cleanup_pending_deletes()
        if os.path.isdir(root):
            pending = root.rstrip("\\/") + _PENDING_TAG + str(int(time.time()))
            renamed = False
            last_err = None
            for _ in range(6):
                try:
                    os.rename(root, pending)
                    renamed = True
                    break
                except OSError as e:
                    last_err = e
                    time.sleep(0.4)
            if not renamed:
                print(f"[AI PYDEPS] cannot delete: the folder is in use ({last_err})")
                return {"ok": False, "trashed": False, "reason": "in_use"}
            trashed = _move_to_trash(pending)
            if not trashed:
                if not _force_rmtree(pending):
                    print("[AI PYDEPS] some files are still locked; they will be removed next time")
        _state.clear()
        _pip_ready.clear()
        _storage_cache["status"] = "IDLE"
        _storage_cache["bytes"]  = 0
        invalidate_dep_caches()     # racks must notice the packages are gone
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
