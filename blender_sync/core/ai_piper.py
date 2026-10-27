# =============================================================================
# core/ai_piper.py
# Addon-side caller for the Piper TTS subprocess.
#
# Piper is a pre-built binary that reads text from stdin and writes a WAV.
# No Python packages needed — just subprocess.
#
# Piper CLI:
#   piper.exe --model <voice.onnx>
#             --output_file <out.wav>
#             --length_scale <float>   1.0=normal, 0.5=faster, 2.0=slower
#             --noise_scale  <float>   expressiveness 0.0-1.0
#             --noise_w      <float>   phoneme duration variation 0.0-1.0
#   (text read from stdin)
# =============================================================================

import os
import platform
import shutil
import subprocess
import threading
import tempfile
import json

import bpy

from core import vse_compat as _vse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ADDON_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PIPER_DIR    = os.path.join(_ADDON_DIR, "ai_engines", "piper")
_VOICES_DIR   = os.path.join(_PIPER_DIR, "voices")   # legacy/dev location inside the add-on folder


def _user_piper_dir():
    """Per-user folder for the downloaded Piper engine + voices. Lives OUTSIDE
    the add-on folder (next to the AI virtual environment) so removing,
    updating or reinstalling the add-on doesn't delete several hundred MB
    of downloads. Created on demand."""
    try:
        return bpy.utils.user_resource('SCRIPTS', path="hijacker_piper", create=True)
    except Exception:
        return _PIPER_DIR


def _piper_roots():
    """Folders searched for the engine and voices: the per-user download
    folder first, then the add-on's own folder (dev checkouts / manual installs)."""
    roots = [_user_piper_dir()]
    if _PIPER_DIR not in roots:
        roots.append(_PIPER_DIR)
    return roots


def _user_voices_dir():
    return os.path.join(_user_piper_dir(), "voices")


def _ensure_executable(path):
    """Make sure the piper binary actually has its +x bit set, and return it.

    macOS/Linux binaries lose their Unix executable permission whenever
    they pass through a non-Unix-aware step — most relevantly, a beta zip
    built/packaged on Windows never preserves it at all, so a binary that
    was perfectly executable when we downloaded it can still land on a
    tester's Mac/Linux machine as a plain non-executable file, which fails
    with a permission error that looks a lot like "piper isn't installed"
    even though the file is right there. Self-healing this here means one
    less manual `chmod +x` step for every tester on every platform.
    Windows ignores the Unix mode bits entirely, so this is a harmless
    no-op there.
    """
    if platform.system().lower() != "windows":
        try:
            import stat as _stat
            mode = os.stat(path).st_mode
            want = mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH
            if mode != want:
                os.chmod(path, want)
                print(f"[PIPER] restored +x permission on {path}")
        except Exception as e:
            print(f"[PIPER] could not verify/set +x on {path}: {e}")

        # piper shells out to its own bundled helper binaries at runtime
        # (piper_phonemize, espeak-ng) — they need +x too, or piper itself
        # fails even though the main "piper" binary launched fine.
        exe_dir = os.path.dirname(path)
        for helper in ("piper_phonemize", "espeak-ng"):
            helper_path = os.path.join(exe_dir, helper)
            if os.path.exists(helper_path):
                try:
                    import stat as _stat
                    hmode = os.stat(helper_path).st_mode
                    hwant = hmode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH
                    if hmode != hwant:
                        os.chmod(helper_path, hwant)
                        print(f"[PIPER] restored +x permission on {helper_path}")
                except Exception as e:
                    print(f"[PIPER] could not verify/set +x on {helper_path}: {e}")
    return path


def _get_piper_exe():
    """Return path to piper executable for this platform, or None.

    Searches in order:
    1. Known platform subfolder (win_x64, macos_arm, etc.)
    2. Any subfolder of ai_engines/piper/ that contains piper.exe / piper
    3. ai_engines/piper/ directly (if exe is placed there flat)
    """
    system  = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        preferred_sub = "win_x64"
        exe_name      = "piper.exe"
    elif system == "darwin":
        preferred_sub = "macos_arm" if "arm" in machine else "macos_x64"
        exe_name      = "piper"
    else:
        preferred_sub = "linux_x64"
        exe_name      = "piper"

    for root in _piper_roots():
        # 1. Preferred platform subfolder
        path = os.path.join(root, preferred_sub, exe_name)
        if os.path.exists(path):
            return _ensure_executable(path)

        # 2. Scan all subfolders for the exe (handles any naming convention)
        if os.path.isdir(root):
            for entry in os.listdir(root):
                sub_path = os.path.join(root, entry)
                if os.path.isdir(sub_path):
                    candidate = os.path.join(sub_path, exe_name)
                    if os.path.exists(candidate):
                        print(f"[PIPER] found exe in folder: {entry}/")
                        return _ensure_executable(candidate)

        # 3. Exe placed directly in the root folder
        flat = os.path.join(root, exe_name)
        if os.path.exists(flat):
            print(f"[PIPER] found exe in {root}")
            return _ensure_executable(flat)

    print(f"[PIPER] searched: {_piper_roots()}")
    return None


def _discover_voices():
    """Return list of (display_name, onnx_path, json_path) for all voices.
    Searches voices/ subfolder first, then piper/ root as fallback.
    """
    voices  = []
    found   = set()
    search_dirs = []
    for _r in _piper_roots():
        search_dirs += [os.path.join(_r, "voices"), _r]
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for fname in sorted(os.listdir(search_dir)):
            if not fname.endswith(".onnx") or fname in found:
                continue
            found.add(fname)
            onnx_path = os.path.join(search_dir, fname)
            json_path = onnx_path + ".json"
            display   = fname.replace(".onnx", "")
            if os.path.exists(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    key   = meta.get("key", display)
                    parts = key.split("-")
                    if len(parts) >= 3:
                        lang_tag   = parts[0].replace("_", "-").upper()
                        voice_name = parts[1].capitalize()
                        qual_tag   = parts[2].capitalize() if len(parts) > 2 else ""
                        display    = f"{voice_name} ({lang_tag}"
                        if qual_tag:
                            display += f", {qual_tag}"
                        display += ")"
                    else:
                        display = key
                except Exception:
                    pass
            voices.append((display, onnx_path, json_path))
    return voices
def get_voices():
    """Public accessor — returns list of (name, onnx_path, json_path)."""
    return _discover_voices()


def is_voice_installed(voice_key):
    """Cheap already-downloaded check for the voice browser — matches by
    the .onnx filename Piper's own catalog uses for that key, against
    whatever _discover_voices() finds on disk right now."""
    target = f"{voice_key}.onnx"
    for _, onnx_path, _j in _discover_voices():
        if os.path.basename(onnx_path) == target:
            return True
    return False


# ---------------------------------------------------------------------------
# Voice catalog — browse & download more voices from Piper's public catalog
# (huggingface.co/rhasspy/piper-voices) instead of shipping every voice in
# the beta zip. See rack_piper.py's "+ ADD VOICE" button and browse mode.
# ---------------------------------------------------------------------------
_VOICES_JSON_URL  = "https://huggingface.co/rhasspy/piper-voices/raw/main/voices.json"
_VOICE_FILES_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
_HTTP_USER_AGENT  = "TheHijacker-Blender-Addon"

_voice_catalog  = []     # list of dicts, see fetch_voice_catalog()
_catalog_status = "IDLE" # IDLE, LOADING, LOADED, ERROR, BLOCKED
_catalog_error  = ""
_download_state = {}     # voice_key -> {'status': DOWNLOADING/DONE/ERROR, 'pct': 0-100, 'error': str}


def is_online_access_allowed():
    """Blender 4.2+ gates ALL addon network access behind a user-controlled
    preference (in 4.5: Preferences > Get Extensions > Allow Online Access)
    and expects addons to check it before making any request. Older Blender
    (pre-4.2) has no such property at all — treat that as always-allowed
    rather than erroring, since there's nothing to respect there."""
    try:
        return bool(bpy.app.online_access)
    except Exception:
        return True


def get_catalog_status():
    """Returns (status, error_message) for the UI to draw."""
    return _catalog_status, _catalog_error


def get_voice_catalog():
    """Returns the currently-loaded catalog list (empty until LOADED)."""
    return _voice_catalog


def fetch_voice_catalog(force=False):
    """Kick off a background fetch of Piper's public voice catalog.
    Safe to call repeatedly (e.g. once per button click) — no-ops once a
    fetch is already in flight or already succeeded, unless force=True."""
    global _catalog_status, _catalog_error
    if not force and _catalog_status in ("LOADING", "LOADED"):
        return
    if not is_online_access_allowed():
        _catalog_status = "BLOCKED"
        return

    _catalog_status = "LOADING"
    _catalog_error  = ""

    def _worker():
        global _voice_catalog, _catalog_status, _catalog_error
        try:
            import urllib.request
            req = urllib.request.Request(
                _VOICES_JSON_URL, headers={"User-Agent": _HTTP_USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))

            catalog = []
            for key, entry in raw.items():
                files = entry.get("files", {}) or {}
                onnx_path = onnx_size = json_path = None
                for path, meta in files.items():
                    if path.endswith(".onnx.json"):
                        json_path = path
                    elif path.endswith(".onnx"):
                        onnx_path = path
                        onnx_size = (meta or {}).get("size_bytes", 0)
                if not onnx_path or not json_path:
                    continue  # incomplete catalog entry — skip rather than crash
                lang = entry.get("language", {}) or {}
                catalog.append({
                    "key":       key,
                    "name":      entry.get("name", key),
                    "lang_code": lang.get("code", ""),
                    "lang_name": lang.get("name_english") or lang.get("code", "?"),
                    "quality":   entry.get("quality", ""),
                    "onnx_path": onnx_path,
                    "onnx_size": onnx_size or 0,
                    "json_path": json_path,
                })
            catalog.sort(key=lambda v: (v["lang_name"], v["name"], v["quality"]))
            _voice_catalog  = catalog
            _catalog_status = "LOADED"
            print(f"[PIPER] voice catalog loaded — {len(catalog)} voices")
        except Exception as e:
            _catalog_error  = str(e)
            _catalog_status = "ERROR"
            print(f"[PIPER] voice catalog fetch failed: {e}")

    threading.Thread(target=_worker, name="PiperCatalogFetch", daemon=True).start()
    _ensure_redraw_timer()


def get_download_state(voice_key):
    return _download_state.get(voice_key)


def download_voice(voice_key):
    """Download one catalog voice's .onnx + .onnx.json into voices/, in a
    background thread, reporting progress via _download_state so the rack
    can draw a live percentage without blocking Blender's UI thread."""
    if not is_online_access_allowed():
        _download_state[voice_key] = {"status": "ERROR", "pct": 0,
                                       "error": "Online access is disabled"}
        return

    existing = _download_state.get(voice_key)
    if existing and existing.get("status") == "DOWNLOADING":
        return  # already in progress — don't start a second thread for it

    entry = next((v for v in _voice_catalog if v["key"] == voice_key), None)
    if not entry:
        _download_state[voice_key] = {"status": "ERROR", "pct": 0,
                                       "error": "voice not found in catalog"}
        return

    _download_state[voice_key] = {"status": "DOWNLOADING", "pct": 0.0, "error": ""}

    def _dl_one(url, dest, pct_lo, pct_hi):
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": _HTTP_USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0)) or 1
            done  = 0
            tmp   = dest + ".part"
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    frac = min(1.0, done / total)
                    pct  = pct_lo + frac * (pct_hi - pct_lo)
                    _download_state[voice_key] = {"status": "DOWNLOADING",
                                                   "pct": pct, "error": ""}
            os.replace(tmp, dest)

    def _worker():
        try:
            os.makedirs(_user_voices_dir(), exist_ok=True)
            onnx_dest = os.path.join(_user_voices_dir(), os.path.basename(entry["onnx_path"]))
            json_dest = onnx_dest + ".json"
            _dl_one(_VOICE_FILES_BASE + entry["onnx_path"], onnx_dest, 0, 90)
            _dl_one(_VOICE_FILES_BASE + entry["json_path"], json_dest, 90, 100)
            _download_state[voice_key] = {"status": "DONE", "pct": 100.0, "error": ""}
            print(f"[PIPER] downloaded voice: {voice_key}")
        except Exception as e:
            _download_state[voice_key] = {"status": "ERROR", "pct": 0, "error": str(e)}
            print(f"[PIPER] voice download failed ({voice_key}): {e}")

    threading.Thread(target=_worker, name=f"PiperVoiceDL_{voice_key}", daemon=True).start()
    _ensure_redraw_timer()


# ---------------------------------------------------------------------------
# Piper ENGINE download — the repo and add-on zip do not ship Piper itself.
# One click on the rack's setup screen downloads the official release for this
# OS from github.com/rhasspy/piper, unpacks it into ai_engines/piper/<platform>/
# and then fetches a starter voice so the rack works straight away.
# ---------------------------------------------------------------------------
_ENGINE_VERSION    = "2023.11.14-2"
_ENGINE_RELEASE    = "https://github.com/rhasspy/piper/releases/tag/" + _ENGINE_VERSION
_ENGINE_BASE_URL   = "https://github.com/rhasspy/piper/releases/download/" + _ENGINE_VERSION + "/"
# Path of the starter voice inside the rhasspy/piper-voices repo (no extension).
STARTER_VOICE_PATH = "en/en_US/lessac/medium/en_US-lessac-medium"

_engine_state  = {"status": "IDLE", "pct": 0.0, "msg": "", "error": ""}
_engine_cancel = False
_present_cache = {"t": 0.0, "v": False}


def engine_target():
    """(archive_name, platform_subfolder, exe_name) for this machine."""
    system  = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "piper_windows_amd64.zip", "win_64", "piper.exe"
    if system == "darwin":
        if "arm" in machine:
            return "piper_macos_aarch64.tar.gz", "macos_arm", "piper"
        return "piper_macos_x64.tar.gz", "macos_x64", "piper"
    if "aarch64" in machine or "arm64" in machine:
        return "piper_linux_aarch64.tar.gz", "linux_arm64", "piper"
    return "piper_linux_x86_64.tar.gz", "linux_x64", "piper"


def engine_install_dir():
    return os.path.join(_user_piper_dir(), engine_target()[1])


def piper_engine_present(max_age=1.0):
    """Quiet, cached 'is there a piper executable?' check — safe to call
    every frame from draw code (the noisy _get_piper_exe() is not)."""
    import time as _t
    now = _t.time()
    if now - _present_cache["t"] < max_age:
        return _present_cache["v"]
    exe_name = engine_target()[2]
    found = False
    try:
        cands = []
        for root in _piper_roots():
            cands.append(os.path.join(root, exe_name))
            if os.path.isdir(root):
                for entry in os.listdir(root):
                    cands.append(os.path.join(root, entry, exe_name))
        found = any(os.path.isfile(c) for c in cands)
    except Exception:
        found = False
    _present_cache["t"] = now
    _present_cache["v"] = found
    return found


def get_engine_state():
    return dict(_engine_state)


def is_engine_installing():
    return _engine_state["status"] in ("DOWNLOADING", "EXTRACTING", "VOICE")


def get_engine_progress_display():
    """(lines, button_label, kind) — same shape as ai_pydeps.get_progress_display."""
    st = _engine_state["status"]
    if st == "ERROR":
        return ([_engine_state["error"][:70]], "RETRY  >", "error")
    if st in ("DOWNLOADING", "EXTRACTING", "VOICE"):
        pct = int(_engine_state["pct"])
        return ([f'{_engine_state["msg"]}  {pct}%'], "CANCEL", "busy")
    return ([], "DOWNLOAD & INSTALL  >", "idle")


def cancel_engine_install():
    global _engine_cancel
    if is_engine_installing():
        _engine_cancel = True


class _EngineCancelled(Exception):
    pass


def _engine_download(url, dest, pct_lo, pct_hi, msg, magic=None):
    """Download url -> dest with progress, RESUMING after dropped connections.

    Some servers/networks close the connection part-way through a large file
    (seen in the wild: GitHub's CDN cutting a 22 MB zip at 17 MB). When the
    stream ends early or errors, this re-requests only the missing bytes with
    an HTTP Range header and carries on, giving up only after repeated
    attempts that make no progress at all. The finished file's size is checked
    against the server's Content-Length and, when `magic` (tuple of allowed
    leading bytes) is given, its first bytes are checked too - so an error
    page saved under a .zip name fails with a clear message."""
    import urllib.request, time as _t
    tmp = dest + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    done = 0
    total = 0
    head = b""
    ctype = ""
    last_err = None
    stalls = 0
    attempts = 0
    while True:
        if _engine_cancel:
            raise _EngineCancelled()
        if total and done >= total:
            break
        if stalls >= 6 or attempts >= 80:
            raise RuntimeError(
                f"download kept stopping at {done} of {total or '?'} bytes "
                f"({last_err or 'connection closed'}) - check your internet/firewall and try again")
        attempts += 1
        headers = {"User-Agent": _HTTP_USER_AGENT}
        if done:
            headers["Range"] = f"bytes={done}-"
        progressed = False
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = getattr(resp, "status", 200)
                ctype = resp.headers.get("Content-Type", ctype)
                if done and status != 206:
                    done = 0                      # server ignored Range: start over
                    head = b""
                cr = resp.headers.get("Content-Range", "")
                if cr and "/" in cr and cr.rsplit("/", 1)[1].strip().isdigit():
                    total = int(cr.rsplit("/", 1)[1])
                elif not done:
                    total = int(resp.headers.get("Content-Length", 0) or 0)
                with open(tmp, "ab" if done else "wb") as f:
                    while True:
                        if _engine_cancel:
                            raise _EngineCancelled()
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        if len(head) < 16:
                            head += chunk[:16 - len(head)]
                        f.write(chunk)
                        done += len(chunk)
                        progressed = True
                        frac = min(1.0, done / total) if total else 0.0
                        _engine_state.update(pct=pct_lo + frac * (pct_hi - pct_lo), msg=msg)
        except _EngineCancelled:
            raise
        except Exception as e:
            last_err = e
            print(f"[PIPER] download interrupted at {done} of {total or '?'} bytes: {e}")
        if progressed:
            stalls = 0
        else:
            stalls += 1
            _t.sleep(min(2.0 * stalls, 8.0))
        if not total:
            break          # server didn't say how big it is: accept what we got
        if done < total:
            print(f"[PIPER] resuming download from byte {done} of {total}")
    print(f"[PIPER] downloaded {url} -> {done} bytes, type '{ctype}', starts {head[:8]!r}")
    if total and done != total:
        raise RuntimeError(f"download incomplete ({done} of {total} bytes) - try again")
    if magic and not head.startswith(tuple(magic)):
        raise RuntimeError(
            f"server did not send the expected file (type '{ctype or 'unknown'}', "
            f"{done} bytes) - check your internet/firewall")
    os.replace(tmp, dest)


def _safe_join(base, *parts):
    dest = os.path.normpath(os.path.join(base, *parts))
    if os.path.commonpath([os.path.abspath(base), os.path.abspath(dest)]) != os.path.abspath(base):
        raise ValueError("unsafe path in archive: " + "/".join(parts))
    return dest


def _extract_engine(archive, dest_dir):
    """Unpack a Piper release into dest_dir, dropping the archive's leading
    'piper/' folder. Rejects any entry that would land outside dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)

    def _parts(name):
        p = [x for x in name.replace("\\", "/").split("/") if x not in ("", ".")]
        if p and p[0] == "piper":
            p = p[1:]
        return p

    if archive.endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(archive) as z:
            members = z.infolist()
            for i, m in enumerate(members):
                if _engine_cancel:
                    raise _EngineCancelled()
                parts = _parts(m.filename)
                if not parts:
                    continue
                target = _safe_join(dest_dir, *parts)
                if m.is_dir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(m) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                _engine_state.update(pct=60 + 10.0 * (i + 1) / max(1, len(members)))
    else:
        import tarfile
        with tarfile.open(archive, "r:*") as t:
            members = t.getmembers()
            for i, m in enumerate(members):
                if _engine_cancel:
                    raise _EngineCancelled()
                parts = _parts(m.name)
                if not parts:
                    continue
                target = _safe_join(dest_dir, *parts)
                if m.isdir():
                    os.makedirs(target, exist_ok=True)
                elif m.issym():
                    link = m.linkname
                    # only plain sibling links (libfoo.dylib -> libfoo.1.dylib)
                    if os.path.isabs(link) or ".." in link.replace("\\", "/").split("/"):
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    if os.path.lexists(target):
                        os.remove(target)
                    os.symlink(link, target)
                elif m.isfile():
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with t.extractfile(m) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                _engine_state.update(pct=60 + 10.0 * (i + 1) / max(1, len(members)))


def start_engine_install():
    """Download + install the Piper engine and a starter voice, in the
    background. Poll get_engine_progress_display() for UI state."""
    global _engine_cancel
    if is_engine_installing():
        return
    if not is_online_access_allowed():
        _engine_state.update(status="ERROR", pct=0.0, msg="",
                             error="Online access is disabled in Blender preferences")
        return
    _engine_cancel = False
    _engine_state.update(status="DOWNLOADING", pct=0.0, msg="Downloading Piper", error="")

    archive_name, sub, exe_name = engine_target()
    dest_dir = os.path.join(_user_piper_dir(), sub)

    def _worker():
        tmp_dir = tempfile.mkdtemp(prefix="hijacker_piper_")
        try:
            archive = os.path.join(tmp_dir, archive_name)
            _engine_download(_ENGINE_BASE_URL + archive_name, archive, 0, 60, "Downloading Piper",
                             magic=((b"PK\x03\x04",) if archive_name.endswith(".zip") else (b"\x1f\x8b",)))
            _engine_state.update(status="EXTRACTING", pct=60.0, msg="Unpacking")
            _extract_engine(archive, dest_dir)

            if platform.system().lower() != "windows":
                import stat as _stat
                for n in (exe_name, "espeak-ng", "piper_phonemize"):
                    p = os.path.join(dest_dir, n)
                    if os.path.isfile(p):
                        os.chmod(p, os.stat(p).st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
            if not os.path.isfile(os.path.join(dest_dir, exe_name)):
                raise RuntimeError("download unpacked but " + exe_name + " was not found")
            _present_cache["t"] = 0.0

            # Starter voice so the rack can speak immediately (skipped if the
            # user already has any voice installed).
            if not _discover_voices():
                _engine_state.update(status="VOICE", pct=70.0, msg="Downloading starter voice")
                try:
                    os.makedirs(_user_voices_dir(), exist_ok=True)
                    base = os.path.basename(STARTER_VOICE_PATH)
                    onnx = os.path.join(_user_voices_dir(), base + ".onnx")
                    _engine_download(_VOICE_FILES_BASE + STARTER_VOICE_PATH + ".onnx", onnx, 70, 98,
                                     "Downloading starter voice")
                    _engine_download(_VOICE_FILES_BASE + STARTER_VOICE_PATH + ".onnx.json",
                                     onnx + ".json", 98, 100, "Downloading starter voice")
                except _EngineCancelled:
                    raise
                except Exception as ve:
                    print(f"[PIPER] starter voice download failed (engine is installed): {ve}")
            _engine_state.update(status="DONE", pct=100.0, msg="Installed", error="")
            print("[PIPER] engine installed")
        except _EngineCancelled:
            _engine_state.update(status="IDLE", pct=0.0, msg="", error="")
            print("[PIPER] engine install cancelled")
        except Exception as e:
            _engine_state.update(status="ERROR", pct=0.0, msg="", error=str(e) or type(e).__name__)
            print(f"[PIPER] engine install failed: {e}")
        finally:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
            _present_cache["t"] = 0.0

    threading.Thread(target=_worker, name="PiperEngineInstall", daemon=True).start()
    _ensure_redraw_timer()


# ---------------------------------------------------------------------------
# Active jobs
# ---------------------------------------------------------------------------
_active_jobs  = {}   # ai_idx -> Thread
_cancel_flags = {}   # ai_idx -> bool

_pending_finish = {}   # ai_idx -> info dict
_piper_output_wave    = {}  # ai_idx -> list[float] waveform for display
_piper_output_path    = {}  # ai_idx -> str wav path (module-level fallback)
_piper_preview_handle = {}  # ai_idx -> aud.Handle
_piper_preview_settings = {}  # ai_idx -> dict of settings at last preview generate


def is_processing(ai_idx):
    t = _active_jobs.get(ai_idx)
    return t is not None and t.is_alive()


def get_output_wave(ai_idx):
    return _piper_output_wave.get(ai_idx, [])


def _get_preview_settings(rack):
    """Return a dict of the settings that affect preview output."""
    return {
        'text': (getattr(rack, 'ai_text', '') or '').strip(),
        'p0':   round(float(getattr(rack, 'p0', 0.5)), 4),
        'p1':   round(float(getattr(rack, 'p1', 0.667)), 4),
        'p2':   round(float(getattr(rack, 'p2', 0.8)), 4),
        'p3':   round(float(getattr(rack, 'p3', 0.5)), 4),
        'p4':   int(getattr(rack, 'p4', 0)),
    }


def preview_piper(ai_idx, context):
    """Preview TTS without adding to VSE timeline.

    - If currently previewing: stop playback.
    - If settings unchanged and temp file exists: replay from beginning.
    - If settings changed or no temp file: regenerate then auto-play.
    """
    import bpy as _bpy, tempfile as _tf
    scene    = context.scene if context else _bpy.context.scene
    ai_racks = getattr(scene, "pb_ai_racks", []) if scene else []
    if ai_idx >= len(ai_racks):
        return

    rack = ai_racks[ai_idx]

    # Stop any in-progress generation
    if is_processing(ai_idx):
        print(f"[PIPER] rack {ai_idx} still generating, please wait")
        return

    # Stop current playback if previewing
    handle = _piper_preview_handle.get(ai_idx)
    if handle is not None:
        try:
            handle.stop()
        except Exception:
            pass
        _piper_preview_handle.pop(ai_idx, None)
        rack.ai_status = "READY"
        print(f"[PIPER] rack {ai_idx} preview stopped")
        return

    # Check if settings changed since last preview
    current_settings = _get_preview_settings(rack)
    last_settings    = _piper_preview_settings.get(ai_idx)
    preview_wav      = os.path.join(_tf.gettempdir(),
                                    f"pb_piper_preview_{ai_idx}.wav")
    settings_changed = (last_settings != current_settings)
    file_exists      = os.path.exists(preview_wav)

    if not settings_changed and file_exists:
        # Replay existing temp file — no regeneration needed
        try:
            from core.audio import play_oneshot
            new_handle = play_oneshot(preview_wav)
            _piper_preview_handle[ai_idx] = new_handle
            rack.ai_status = "PREVIEWING"
            print(f"[PIPER] rack {ai_idx} replaying preview (no changes)")
        except Exception as e:
            rack.ai_status    = "ERROR"
            rack.ai_error_msg = f"replay failed: {e}"
            print(f"[PIPER] replay error: {e}")
    else:
        # Settings changed or no cached file — regenerate
        print(f"[PIPER] rack {ai_idx} generating preview"
              f"{'  (settings changed)' if settings_changed else ''}...")
        _piper_preview_settings[ai_idx] = current_settings
        generate_piper(ai_idx, context, preview_only=True)


# ---------------------------------------------------------------------------
# Main generate function
# ---------------------------------------------------------------------------
def generate_piper(ai_idx, context, preview_only=False):
    """
    Generate speech for AI rack ai_idx in a background thread.
    preview_only=True: write to separate temp file, auto-play, skip VSE placement.
    """
    if is_processing(ai_idx):
        print(f"[PIPER] rack {ai_idx} already processing")
        return

    scene = context.scene
    if not scene:
        return

    ai_racks = getattr(scene, "pb_ai_racks", [])
    if ai_idx >= len(ai_racks):
        return

    rack = ai_racks[ai_idx]

    # ── Pre-flight ────────────────────────────────────────────────────────
    piper_exe = _get_piper_exe()
    if piper_exe is None:
        rack.ai_status    = "ERROR"
        rack.ai_error_msg = "piper executable not found for this platform"
        print(f"[PIPER] ERROR: piper executable not found")
        print(f"[PIPER] Expected at: {os.path.join(_user_piper_dir(), '<platform>', 'piper[.exe]')}")
        return

    text = (getattr(rack, 'ai_text', '') or '').strip()
    if not text:
        rack.ai_status    = "ERROR"
        rack.ai_error_msg = "no script text — click the script field and type something"
        print("[PIPER] ERROR: no text to synthesise — type something in the script field")
        return

    # ── Voice selection ───────────────────────────────────────────────────
    voices = _discover_voices()
    if not voices:
        rack.ai_status    = "ERROR"
        rack.ai_error_msg = "no voice models found — click + ADD VOICE in the rack"
        print(f"[PIPER] ERROR: no voice models found in {_piper_roots()}")
        return

    # p4 stores the voice index (float), separate from preset_idx (knob preset)
    voice_idx  = int(getattr(rack, 'p4', 0.0)) % max(1, len(voices))
    _, onnx_path, _ = voices[voice_idx]

    # ── Knob values ───────────────────────────────────────────────────────
    # p0 speed norm: 0=slow(2.0×), 0.5=normal(1.0×), 1=fast(0.5×)
    speed_norm   = float(getattr(rack, 'p0', 0.5))
    length_scale = 2.0 - speed_norm * 1.5     # maps 0→2.0, 0.5→1.25, 1→0.5
    noise_scale  = float(getattr(rack, 'p1', 0.667))
    noise_w      = float(getattr(rack, 'p2', 0.8))
    vol_norm     = float(getattr(rack, 'p3', 0.5))
    vol_db       = (vol_norm - 0.5) * 24.0    # maps 0→-12dB, 0.5→0dB, 1→+12dB

    # ── Target channel ────────────────────────────────────────────────────
    # ch buttons on Piper select where to PLACE the output, not where input comes from
    from Racks import get_ai_rack_channels
    assigned = list(get_ai_rack_channels(rack))
    target_ch_idx = assigned[0] if assigned else None

    # ── Temp output path ──────────────────────────────────────────────────
    tmp_dir    = tempfile.gettempdir()
    if preview_only:
        output_wav = os.path.join(tmp_dir, f"pb_piper_preview_{ai_idx}.wav")
    else:
        output_wav = os.path.join(tmp_dir, f"pb_piper_out_{ai_idx}.wav")

    scene_name = scene.name

    rack.ai_status    = "PROCESSING"
    rack.ai_error_msg = ""
    _cancel_flags[ai_idx] = False

    def _worker():
        try:
            # Piper needs to find espeak-ng-data relative to its exe
            exe_dir = os.path.dirname(piper_exe)

            cmd = [
                piper_exe,
                "--model",        onnx_path,
                "--output_file",  output_wav,
                "--length_scale", f"{length_scale:.3f}",
                "--noise_scale",  f"{noise_scale:.3f}",
                "--noise_w",      f"{noise_w:.3f}",
            ]

            print(f"[PIPER] rack {ai_idx}: generating {len(text)} chars")
            print(f"[PIPER] voice: {os.path.basename(onnx_path)}")
            print(f"[PIPER] speed={length_scale:.2f} noise={noise_scale:.2f} "
                  f"noise_w={noise_w:.2f}")

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=exe_dir,        # so piper finds espeak-ng-data/
                text=True,
                encoding="utf-8",
            )

            stdout, stderr = proc.communicate(input=text, timeout=120)

            if _cancel_flags.get(ai_idx):
                _finish(ai_idx, scene_name, "READY")
                return

            if proc.returncode != 0:
                print(f"[PIPER] stderr: {stderr[:500]}")
                _finish(ai_idx, scene_name, "ERROR",
                        error_msg=f"piper exited {proc.returncode}: {stderr[:200]}")
                return

            if not os.path.exists(output_wav):
                _finish(ai_idx, scene_name, "ERROR",
                        error_msg="piper ran but no output WAV was written")
                return

            # Apply volume gain if needed
            if abs(vol_db) > 0.1:
                _apply_gain(output_wav, vol_db)

            # Build waveform for display
            wave_data = _wav_to_waveform(output_wav)
            _piper_output_wave[ai_idx] = wave_data
            _piper_output_path[ai_idx] = output_wav

            print(f"[PIPER] rack {ai_idx} {'PREVIEW' if preview_only else 'DONE'} — {output_wav}")
            _finish(ai_idx, scene_name, "DONE",
                    output_wav=output_wav,
                    target_ch_idx=target_ch_idx,
                    preview_only=preview_only)

        except subprocess.TimeoutExpired:
            proc.kill()
            _finish(ai_idx, scene_name, "ERROR", error_msg="Timed out after 120s")
        except Exception as e:
            import traceback
            traceback.print_exc()
            _finish(ai_idx, scene_name, "ERROR", error_msg=str(e))

    t = threading.Thread(target=_worker, name=f"Piper_rack{ai_idx}", daemon=True)
    _active_jobs[ai_idx] = t
    t.start()

    if not bpy.app.timers.is_registered(_redraw_timer):
        bpy.app.timers.register(_redraw_timer, first_interval=0.25)

    print(f"[PIPER] rack {ai_idx} generation started")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _apply_gain(wav_path, gain_db):
    """Apply gain in-place to a WAV file using numpy."""
    import wave, struct, math
    try:
        import numpy as np
        gain = 10.0 ** (gain_db / 20.0)
        with wave.open(wav_path, 'rb') as wf:
            sr  = wf.getframerate()
            nch = wf.getnchannels()
            sw  = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if sw == 2:
            s = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
            s = np.clip(s * gain, -1.0, 1.0)
            s16 = (s * 32767).astype(np.int16)
            with wave.open(wav_path, 'wb') as wf:
                wf.setnchannels(nch); wf.setsampwidth(2)
                wf.setframerate(sr); wf.writeframes(s16.tobytes())
    except Exception as e:
        print(f"[PIPER] gain apply failed: {e}")


def _wav_to_waveform(wav_path, n_slots=80):
    """Return list of n_slots RMS amplitude values (0-1) for waveform display."""
    import wave
    try:
        import numpy as np
        with wave.open(wav_path, 'rb') as wf:
            sr  = wf.getframerate()
            nch = wf.getnchannels()
            sw  = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if sw == 2:
            s = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
        else:
            s = np.frombuffer(raw, np.uint8).astype(np.float32) / 128.0 - 1.0
        if nch > 1:
            s = s.reshape(-1, nch).mean(axis=1)
        slot = max(1, len(s) // n_slots)
        result = []
        for i in range(n_slots):
            chunk = s[i*slot:(i+1)*slot]
            rms   = float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0.0
            result.append(min(1.0, rms * 4.0))
        return result
    except Exception:
        return []


def _register_preview_watchdog(ai_idx, scene_name):
    """Register a timer that polls until the preview handle goes inactive,
    then resets ai_status back to READY so the PREVIEW button is ready again."""
    import bpy as _bpy

    def _watchdog():
        handle = _piper_preview_handle.get(ai_idx)
        if handle is None:
            return None  # already stopped manually
        still_playing = False
        try:
            still_playing = bool(handle.status)
        except Exception:
            pass
        if not still_playing:
            # Playback finished naturally — clean up and reset status
            _piper_preview_handle.pop(ai_idx, None)
            try:
                scene = _bpy.data.scenes.get(scene_name)
                if scene:
                    ai_racks = getattr(scene, "pb_ai_racks", [])
                    if ai_idx < len(ai_racks):
                        ai_racks[ai_idx].ai_status = "READY"
                        print(f"[PIPER] rack {ai_idx} preview finished — ready")
                for window in _bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == 'NODE_EDITOR':
                            area.tag_redraw()
            except Exception:
                pass
            return None  # unregister timer
        return 0.1  # check again in 100ms

    _bpy.app.timers.register(_watchdog, first_interval=0.1)


# ---------------------------------------------------------------------------
# Finish / timer
# ---------------------------------------------------------------------------
def _finish(ai_idx, scene_name, status, output_wav=None,
            target_ch_idx=None, error_msg=None, preview_only=False):
    _pending_finish[ai_idx] = {
        'status':        status,
        'output_wav':    output_wav,
        'target_ch_idx': target_ch_idx,
        'error_msg':     error_msg,
        'scene_name':    scene_name,
        'preview_only':  preview_only,
    }


def _redraw_timer():
    for ai_idx, info in list(_pending_finish.items()):
        del _pending_finish[ai_idx]
        _apply_finish(ai_idx, info)
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'NODE_EDITOR':
                    area.tag_redraw()
    except Exception:
        pass
    any_active = (any(t.is_alive() for t in _active_jobs.values()) or
                  bool(_pending_finish) or
                  _catalog_status == "LOADING" or
                  is_engine_installing() or
                  any(s.get("status") == "DOWNLOADING" for s in _download_state.values()))
    return 0.25 if any_active else None


def _ensure_redraw_timer():
    """Shared by generate_piper(), fetch_voice_catalog() and download_voice()
    — anything that changes state in a background thread needs the HUD to
    keep redrawing (region draw callbacks don't re-run on their own just
    because a dict value changed on another thread) until it settles."""
    if not bpy.app.timers.is_registered(_redraw_timer):
        bpy.app.timers.register(_redraw_timer, first_interval=0.25)


def _apply_finish(ai_idx, info):
    try:
        scene = bpy.data.scenes.get(info['scene_name'])
        if not scene:
            return
        ai_racks = getattr(scene, "pb_ai_racks", [])
        if ai_idx >= len(ai_racks):
            return
        rack           = ai_racks[ai_idx]
        rack.ai_status = info['status']

        if info['status'] == "ERROR":
            rack.ai_error_msg = info.get('error_msg', 'unknown error') or 'unknown error'
            print(f"[PIPER] rack {ai_idx} ERROR: {info.get('error_msg', 'unknown')}")

        elif info['status'] == "DONE":
            output_wav    = info.get('output_wav')
            target_ch_idx = info.get('target_ch_idx')
            preview_only  = info.get('preview_only', False)
            if output_wav:
                if preview_only:
                    # Play immediately, don't add to VSE
                    # When playback ends the handle becomes inactive;
                    # a watchdog timer resets status back to READY.
                    rack.ai_status = "PREVIEWING"
                    try:
                        from core.audio import play_oneshot
                        handle = play_oneshot(output_wav)
                        _piper_preview_handle[ai_idx] = handle
                        print(f"[PIPER] rack {ai_idx} auto-playing preview")
                        # Register a watchdog to reset status when playback ends
                        _register_preview_watchdog(ai_idx, scene.name)
                    except Exception as e:
                        print(f"[PIPER] auto-play failed: {e}")
                        rack.ai_status = "READY"
                else:
                    persistent = _place_output_in_vse(scene, ai_idx, output_wav,
                                                       target_ch_idx, rack)
                    rack.ai_output_path        = persistent or output_wav
                    _piper_output_path[ai_idx] = persistent or output_wav

    except Exception as e:
        print(f"[PIPER] _apply_finish error: {e}")


def _get_persistent_output_dir():
    """Return a persistent output folder next to the saved .blend file.

    Falls back to a 'piper_output' subfolder inside the addon directory if
    the project has not been saved yet, so the file is never in the OS temp dir.
    """
    blend_path = bpy.data.filepath
    if blend_path:
        project_dir = os.path.dirname(os.path.abspath(blend_path))
        out_dir = os.path.join(project_dir, "piper_output")
    else:
        out_dir = os.path.join(_ADDON_DIR, "piper_output")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _persist_output(tmp_wav, ai_idx):
    """Copy *tmp_wav* from the temp dir into the project's piper_output folder.

    Returns the new persistent path, or *tmp_wav* unchanged if the copy fails.
    Uses a timestamped filename so repeated generates never overwrite each other.
    """
    import shutil, time as _t
    try:
        out_dir   = _get_persistent_output_dir()
        timestamp = _t.strftime("%Y%m%d_%H%M%S")
        dest_name = f"piper_rack{ai_idx}_{timestamp}.wav"
        dest_path = os.path.join(out_dir, dest_name)
        shutil.copy2(tmp_wav, dest_path)
        print(f"[PIPER] output saved to project folder: {dest_path}")
        return dest_path
    except Exception as e:
        print(f"[PIPER] WARNING: could not copy to project folder ({e}), "
              f"using temp path — file may be lost on reboot")
        return tmp_wav


def _place_output_in_vse(scene, ai_idx, output_wav, target_ch_idx, rack):
    """Place the generated WAV as a new strip on the target VSE channel."""
    try:
        # Copy to project folder so the file is not lost when the OS clears temp.
        # _persist_output returns the new path; falls back to output_wav on error.
        persistent_wav = _persist_output(output_wav, ai_idx)
        # Update the module-level path so has_output / status display stays accurate
        _piper_output_path[ai_idx] = persistent_wav

        if not scene.sequence_editor:
            scene.sequence_editor_create()
        seq = scene.sequence_editor

        # Target channel: user-selected via CH buttons, else next free above existing
        if target_ch_idx is not None:
            target_ch = target_ch_idx + 1   # 1-based
        else:
            used = {s.channel for s in _vse.get_all_strips(seq)}
            target_ch = 1
            while target_ch in used:
                target_ch += 1

        # Place at playhead frame
        place_frame = scene.frame_current

        # Give the strip a unique name with timestamp so multiple generates
        # don't overwrite each other — user keeps all versions
        import time as _t
        strip_name = f"Piper_{ai_idx}_{int(_t.time()) % 100000}"

        new_strip = _vse.get_strips_collection(seq).new_sound(
            name        = strip_name,
            filepath    = persistent_wav,
            channel     = target_ch,
            frame_start = place_frame,
        )

        # Store for ON/OFF toggling
        rack['piper_output_channel'] = target_ch
        rack['piper_place_frame']    = place_frame

        print(f"[PIPER] placed on VSE ch{target_ch} at frame {place_frame}")

        # Redraw VSE
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in ('SEQUENCE_EDITOR', 'NODE_EDITOR'):
                    area.tag_redraw()

        return persistent_wav

    except Exception as e:
        import traceback
        print(f"[PIPER] VSE placement failed: {e}")
        traceback.print_exc()
        return None
