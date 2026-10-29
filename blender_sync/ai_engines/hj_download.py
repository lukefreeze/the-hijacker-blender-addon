"""
ai_engines/hj_download.py
=========================
Small shared helper for the AI runner scripts (standalone subprocesses):
a RESUMABLE model downloader plus the status wording the rack banner shows.

Why: the libraries' own downloaders (voicefixer's urlretrieve, torch.hub)
cannot resume. When the server closes the connection early they either
raise (ContentTooShortError) or - worse - leave a truncated file at the
final path that later runs mistake for a finished download.

Runners talk to the add-on over stdout lines:
    PROGRESS:<0-100>      overall progress of the job
    STATUS:<text>         what is happening right now ("" clears it)
"""

import os
import sys
import time
import hashlib
import urllib.request
import urllib.error

_UA = "TheHijacker-Blender-Addon"
_CHUNK = 1024 * 256


def status_text(label, done, total):
    """The one wording every rack uses while a model downloads."""
    if total and total > 0:
        pct = int(min(100.0, 100.0 * done / total))
        return (f"Downloading {label}: {pct}% ({done / 1e6:.0f} of {total / 1e6:.0f} MB)"
                f" - one-time download, not needed again")
    return f"Downloading {label}: {done / 1e6:.0f} MB - one-time download, not needed again"


def stdout_emitter(label, pct_lo=5, pct_hi=15):
    """emit(done, total) -> prints PROGRESS/STATUS lines (throttled by caller)."""
    def emit(done, total):
        if total and total > 0:
            pct = pct_lo + int((pct_hi - pct_lo) * min(1.0, done / total))
            print(f"PROGRESS:{pct}")
        print(f"STATUS:{status_text(label, done, total)}")
        sys.stdout.flush()
    return emit


def _open(url, start):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    if start > 0:
        req.add_header("Range", f"bytes={start}-")
    return urllib.request.urlopen(req, timeout=30)


def _total_from(resp, start):
    """Total file size from a response, or 0 when unknown."""
    try:
        cr = resp.headers.get("Content-Range")
        if cr and "/" in cr:
            t = cr.rsplit("/", 1)[1].strip()
            if t.isdigit():
                return int(t)
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit():
            return start + int(cl) if getattr(resp, "status", 200) == 206 else int(cl)
    except Exception:
        pass
    return 0


def remote_size(url):
    """Size in bytes from the server, 0 if it will not say."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            cl = r.headers.get("Content-Length")
            return int(cl) if cl and cl.isdigit() else 0
    except Exception:
        return 0


def _sha256_prefix_ok(path, prefix):
    if not prefix:
        return True
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest().startswith(prefix)


def download_file(url, dest, emit=None, hash_prefix=None, max_stalls=6, adopt_existing=False):
    """Download url -> dest with resume + retries. Returns dest.

    * Data goes to dest + '.part' and is renamed only when complete.
    * adopt_existing: a file already sitting at dest is checked against the
      server's size; if it is shorter (a truncated earlier download) it is
      resumed instead of trusted, if equal it is kept.
    * Raises RuntimeError with a plain message if it cannot finish.
    """
    part = dest + ".part"
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)

    if adopt_existing and os.path.isfile(dest):
        want = remote_size(url)
        have = os.path.getsize(dest)
        if want and have == want:
            return dest
        if not want:
            return dest            # server will not say; assume the file is fine
        if have < want and not os.path.exists(part):
            os.replace(dest, part)  # resume the truncated copy
        else:
            os.remove(dest)

    total = 0
    stalls = 0
    last_emit = 0.0
    last_err = ""
    while True:
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if total and have == total:
            break
        try:
            resp = _open(url, have)
        except urllib.error.HTTPError as e:
            if e.code == 416 and have > 0:       # asked past the end: already complete
                break
            last_err = f"HTTP {e.code}"
            stalls += 1
            if stalls > max_stalls:
                raise RuntimeError(f"download failed ({last_err})")
            time.sleep(min(2 * stalls, 8))
            continue
        except Exception as e:
            last_err = str(e)
            stalls += 1
            if stalls > max_stalls:
                raise RuntimeError(f"download failed ({last_err})")
            time.sleep(min(2 * stalls, 8))
            continue

        with resp:
            status = getattr(resp, "status", 200)
            if have > 0 and status != 206:
                have = 0                        # server ignored Range: start over
            total = _total_from(resp, have) or total
            progressed = False
            try:
                with open(part, "ab" if have > 0 else "wb") as f:
                    while True:
                        blk = resp.read(_CHUNK)
                        if not blk:
                            break
                        f.write(blk)
                        have += len(blk)
                        progressed = True
                        now = time.time()
                        if emit and now - last_emit >= 0.3:
                            last_emit = now
                            emit(have, total)
            except Exception as e:
                last_err = str(e)
        if emit:
            emit(have, total)
        if total and have >= total:
            break
        if not total:
            break                                # unknown size: trust a clean end of stream
        stalls = 0 if progressed else stalls + 1
        if stalls > max_stalls:
            raise RuntimeError(
                f"download kept stopping at {have / 1e6:.0f} of {total / 1e6:.0f} MB"
                f" ({last_err or 'connection closed'}) - check your connection and try again")
        time.sleep(1)

    if not _sha256_prefix_ok(part, hash_prefix):
        try:
            os.remove(part)
        except OSError:
            pass
        raise RuntimeError("downloaded file failed its checksum - try again")
    os.replace(part, dest)
    return dest


def install_torch_hub_patch(label="AI model", min_bytes=3 * 1024 * 1024,
                            emit_factory=None, print_done=True):
    """Make torch.hub use the resumable downloader above, with status output.

    torch.hub's own download cannot resume and only draws a console bar.
    Files smaller than min_bytes (repo zips, tiny configs) download silently.
    emit_factory(label) -> emit(done, total); default prints PROGRESS/STATUS.
    """
    import torch.hub as _hub

    def _patched(url, dst, hash_prefix=None, progress=True):
        size = remote_size(url)
        loud = size == 0 or size >= min_bytes
        if emit_factory:
            emit = emit_factory(label) if loud else None
        else:
            emit = stdout_emitter(label, 5, 15) if loud else None
        download_file(url, dst, emit=emit, hash_prefix=hash_prefix)
        if loud and print_done:
            print("STATUS:")
            sys.stdout.flush()

    _hub.download_url_to_file = _patched
