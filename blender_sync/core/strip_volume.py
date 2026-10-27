"""
core/strip_volume.py
=====================
Volume automation on VSE sound strips (keyframes on `strip.volume`).

How the desk and the strips fit together
----------------------------------------
The desk fader and gain knob are *multipliers* that are baked into `strip.volume`
(strip.volume = what the user set x fader x gain). Moving a fader multiplies
`strip.volume` by new/old, which is why the user's own value always comes back when the
fader returns: the fader never goes below FADER_MIN (> 0), so the ratio is never 0 and
the multiplication can always be undone.

A keyframed strip works the same way. Blender re-applies the F-curve to `strip.volume`
on every frame change, so changing `strip.volume` alone would be undone straight away;
instead the fader ratio is applied to the keyframes (their values and bezier handles)
as well. The animation then always contains "user's curve x fader x gain", exactly like
a plain strip, and dividing by fader x gain recovers the user's curve.

Playback
--------
`volume_polyline()` samples the F-curve over the part of the strip that is heard and
simplifies it to a short chain of straight-line pieces. The audio engine plays each piece
as a segment whose gain moves linearly from `volume` to `volume_end` (no clicks at the
joins), so keyframed fades, ducks and rides are heard in playback and in mixdowns.

Blender versions: 4.3 (legacy `action.fcurves`) through 5.x (slotted actions,
`action.layers[].strips[].channelbag(slot).fcurves`).
"""
import math


# --------------------------------------------------------------------------- F-curve lookup
def _iter_fcurves(id_data):
    """Every F-curve of the animation data on `id_data` (the scene, for strips)."""
    ad = getattr(id_data, "animation_data", None)
    action = getattr(ad, "action", None) if ad else None
    if action is None:
        return
    layers = getattr(action, "layers", None)
    if layers is not None and len(layers):                      # layered / slotted actions (4.4+)
        slot = getattr(ad, "action_slot", None)
        for layer in layers:
            for st in getattr(layer, "strips", ()):
                if getattr(st, "type", "KEYFRAME") != "KEYFRAME":
                    continue
                cb = None
                if slot is not None:
                    try:
                        cb = st.channelbag(slot)
                    except Exception:
                        cb = None
                if cb is not None:
                    for fc in cb.fcurves:
                        yield fc
        return
    fcs = getattr(action, "fcurves", None)                      # legacy actions (< 4.4, and 4.4 compat)
    if fcs is not None:
        for fc in fcs:
            yield fc


def volume_fcurve(strip):
    """The F-curve animating `strip.volume`, or None if the strip's volume is not animated."""
    try:
        path = strip.path_from_id("volume")
        for fc in _iter_fcurves(strip.id_data):
            if fc.data_path == path and not getattr(fc, "mute", False):
                return fc
    except Exception:
        pass
    return None


def is_animated(strip):
    return volume_fcurve(strip) is not None


# --------------------------------------------------------------------------- fader / gain
def scale_volume(strip, ratio):
    """Multiply the strip's volume by `ratio` - the strip value and, when the volume is
    keyframed, every keyframe (value and bezier handles). Exactly invertible by 1/ratio."""
    strip.volume = strip.volume * ratio
    fc = volume_fcurve(strip)
    if fc is None:
        return False
    for kp in fc.keyframe_points:
        kp.co[1] = kp.co[1] * ratio
        kp.handle_left[1] = kp.handle_left[1] * ratio
        kp.handle_right[1] = kp.handle_right[1] * ratio
    fc.update()
    return True


# --------------------------------------------------------------------------- sampling
def _simplify(pts, tol_abs, tol_rel):
    """Ramer-Douglas-Peucker on (frame, value) points: keeps a point only when dropping it
    would move the curve by more than tol_abs + tol_rel * value."""
    n = len(pts)
    if n <= 2:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        xa, ya = pts[a]
        xb, yb = pts[b]
        span = xb - xa
        worst, wi = 0.0, -1
        for i in range(a + 1, b):
            x, y = pts[i]
            line = ya + (yb - ya) * ((x - xa) / span if span else 0.0)
            err = abs(y - line) - (tol_abs + tol_rel * abs(y))
            if err > worst:
                worst, wi = err, i
        if wi >= 0:
            keep[wi] = True
            stack.append((a, wi))
            stack.append((wi, b))
    return [p for p, k in zip(pts, keep) if k]


def volume_polyline(strip, f0, f1, max_points=200):
    """Sample the strip's volume F-curve between scene frames f0 and f1.

    Returns None if the volume is not animated, otherwise a list of (frame, volume) points
    - starting at f0 and ending at f1 - such that a straight line between neighbours stays
    within about 1 % (or 0.002) of the real curve. Volume is never negative.
    """
    fc = volume_fcurve(strip)
    if fc is None or f1 <= f0:
        return None
    kps = fc.keyframe_points
    if len(kps) == 0 and len(fc.modifiers) == 0:
        return None

    def ev(f):
        v = fc.evaluate(f)
        return v if v > 0.0 else 0.0

    frames = {float(f0), float(f1)}
    if len(fc.modifiers):                                       # noise / cycles etc.: sample it all
        lo, hi = f0, f1
    else:
        xs = [kp.co[0] for kp in kps]
        lo, hi = max(f0, min(xs)), min(f1, max(xs))
        for x in xs:                                            # the keyframes themselves
            if f0 < x < f1:
                frames.add(float(x))
    if hi > lo:
        for f in range(int(math.ceil(lo)), int(math.floor(hi)) + 1):
            if f0 < f < f1:
                frames.add(float(f))
    pts = [(f, ev(f)) for f in sorted(frames)]

    tol_abs, tol_rel = 0.002, 0.01
    out = _simplify(pts, tol_abs, tol_rel)
    while len(out) > max_points:                                # keep the segment count sane
        tol_abs *= 2.0
        tol_rel *= 2.0
        out = _simplify(pts, tol_abs, tol_rel)
    return out
