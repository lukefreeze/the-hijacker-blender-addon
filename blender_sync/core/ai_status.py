"""
core/ai_status.py
=================
Live progress text for the AI racks (Demucs, kNN-VC, VoiceFixer, Whisper).

Each rack's runner prints   PROGRESS:<0-100>   and   STATUS:<text>   lines.
The rack's worker thread hands every stdout line to feed(); the rack
banner (see Racks.py) asks get_text() / get_fraction() each redraw.

Plain dict state: written by worker threads, read by the draw callback.
"""

import time

_state = {}   # (group, idx) -> dict


def reset(group, idx):
    _state[(group, idx)] = {"pct": 0.0, "msg": "", "t_start": time.time(),
                            "base_t": None, "base_pct": 0.0, "dl": False}


def clear(group, idx):
    _state.pop((group, idx), None)


def feed(group, idx, line):
    """Handle one runner stdout line. Returns True if it was PROGRESS/STATUS."""
    if not (line.startswith("PROGRESS:") or line.startswith("STATUS:")):
        return False
    st = _state.get((group, idx))
    if st is None:
        reset(group, idx)
        st = _state[(group, idx)]
    try:
        if line.startswith("PROGRESS:"):
            pct = float(line.split(":", 1)[1].strip())
            st["pct"] = max(st["pct"], min(100.0, pct)) if not st["dl"] else min(100.0, pct)
            # the speed estimate starts once downloading is over
            if st["base_t"] is None and not st["dl"]:
                st["base_t"], st["base_pct"] = time.time(), st["pct"]
        else:
            msg = line.split(":", 1)[1].strip()
            was_dl = st["dl"]
            st["msg"] = msg
            st["dl"] = msg.startswith("Downloading")
            if was_dl and not st["dl"]:
                st["base_t"], st["base_pct"] = time.time(), st["pct"]
            elif st["dl"]:
                st["base_t"] = None
    except Exception:
        return True
    return True


def is_downloading(group, idx):
    st = _state.get((group, idx))
    return bool(st and st["dl"])


def get_fraction(group, idx):
    """0..1 for a progress bar (download fraction while downloading)."""
    st = _state.get((group, idx))
    if not st:
        return 0.0
    if st["dl"]:
        # "Downloading X: 23% (...)" -> 0.23
        try:
            seg = st["msg"].split(":", 1)[1]
            return max(0.0, min(1.0, float(seg.split("%", 1)[0].strip()) / 100.0))
        except Exception:
            return 0.0
    return max(0.0, min(1.0, st["pct"] / 100.0))


def _eta(st):
    bt = st.get("base_t")
    if bt is None:
        return ""
    done = st["pct"] - st["base_pct"]
    el = time.time() - bt
    if done < 4.0 or el < 4.0 or st["pct"] >= 99.0:
        return ""
    left = el * (100.0 - st["pct"]) / done
    if left < 5:
        return "almost done"
    if left < 90:
        return f"about {int(round(left / 5.0) * 5)}s left"
    m, s = divmod(int(left), 60)
    return f"about {m}m {int(round(s / 10.0) * 10) % 60:02d}s left" if m < 60 else "over an hour left"


def get_text(group, idx):
    """e.g. 'Processing 45% - about 1m 20s left'  or  'Downloading ...'."""
    st = _state.get((group, idx))
    if not st:
        return "Starting..."
    if st["dl"]:
        return st["msg"]
    parts = [f"Processing {int(st['pct'])}%"]
    if st["msg"]:
        parts = [f"{int(st['pct'])}% - {st['msg']}"]
    eta = _eta(st)
    if eta:
        parts.append(eta)
    return " - ".join(parts)


# ---------------------------------------------------------------------------
# Text for on-screen display. The racks print this INSIDE their own screens
# (status bar / the "ANALYZING" screen) in place of the usual text while a
# job runs, so it must fit: pick() returns the most informative variant that
# fits in max_chars characters.
# ---------------------------------------------------------------------------
import re as _re

_DL_RE = _re.compile(r"^Downloading (.+?): (\d+)% \((\d+) of (\d+) MB\)")


def get_variants(group, idx):
    """Candidate texts, most informative first."""
    st = _state.get((group, idx))
    if not st:
        return ["Starting..."]
    if st["dl"]:
        m = _DL_RE.match(st["msg"])
        if not m:
            return [st["msg"], "Downloading model - one-time download", "Downloading model"]
        label, pct, d, t = m.group(1), m.group(2), m.group(3), m.group(4)
        return [
            st["msg"],
            f"Downloading {label}: {pct}% ({d}/{t} MB) - one-time download",
            f"Downloading {pct}% ({d}/{t} MB) - one-time download",
            f"Downloading model {pct}% - one-time download",
            f"Downloading {pct}%",
            f"DL {pct}%",
        ]
    pct = int(st["pct"])
    eta = _eta(st)
    out = []
    if st["msg"]:
        out.append(f"{pct}% - {st['msg']}" + (f" - {eta}" if eta else ""))
        if eta:
            out.append(f"{pct}% - {st['msg']}")
    if eta:
        out.append(f"{pct}% - {eta}")
    out.append(f"Processing {pct}%")
    out.append(f"{pct}%")
    return out


def pick(group, idx, max_chars):
    """Most informative variant that fits max_chars (falls back to the shortest)."""
    vs = get_variants(group, idx)
    for v in vs:
        if len(v) <= max_chars:
            return v
    return vs[-1]


def headline(group, idx, default="ANALYZING..."):
    """Big-screen title: says DOWNLOADING while a model downloads."""
    return "DOWNLOADING MODEL..." if is_downloading(group, idx) else default
