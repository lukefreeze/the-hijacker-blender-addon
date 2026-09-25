# =============================================================================
# core/perf_monitor.py
# Tracks how expensive Hijacker's own HUD redraw is, and nudges the user to
# collapse racks when it's consistently slow enough to hurt UI responsiveness
# and audio sync.
#
# What's measured: the wall-clock time of ui.mixer.mixer_hud.draw_callback_px()
# itself — not a generic "Blender FPS" (Blender has no such API for a custom
# GPU overlay), and not scene.render.fps (that's the timeline/video frame
# rate, unrelated). Timing the draw call directly is a faithful, unambiguous
# signal: it's exactly the cost of what Hijacker is drawing on any given
# redraw, so it correlates directly with "how many racks are expanded right
# now" — which is the thing the popup below is actually asking the user to
# reduce.
# =============================================================================

import bpy

# Rolling window of the last N draw_callback_px() durations, in seconds.
_DRAW_HISTORY_LEN = 40
_draw_times = []

# A single draw taking longer than this is "slow" on its own (33ms ~= the
# cost budget of a 30fps frame, spent just on Hijacker's own HUD — on top of
# whatever else Blender is drawing that redraw).
_SLOW_DRAW_THRESHOLD = 0.033

# The rolling average must stay above the threshold for this many consecutive
# checks (checked once per meter-timer tick, see core/meters.py) before we
# say anything — a couple of one-off slow frames (e.g. right after expanding
# a rack) shouldn't trigger a nag.
_SUSTAINED_CHECKS_REQUIRED = 40   # ~2s at METER_POLL_INTERVAL (0.05s) spacing
_slow_streak = 0

# Once shown, don't show it again for the rest of the Blender session —
# one nudge is a tip, a dozen is nagware.
_warned_this_session = False


def record_draw_time(dt):
    """Called from mixer_hud.draw_callback_px() with how long that single
    draw took, in seconds."""
    global _draw_times
    _draw_times.append(dt)
    if len(_draw_times) > _DRAW_HISTORY_LEN:
        _draw_times.pop(0)


def _rolling_average():
    if not _draw_times:
        return 0.0
    return sum(_draw_times) / len(_draw_times)


def check_and_maybe_warn():
    """Call periodically (from the meter timer) — evaluates the rolling
    average draw cost and, if it's been consistently slow, invokes the
    collapse-racks popup once per session."""
    global _slow_streak, _warned_this_session

    if _warned_this_session:
        return
    if len(_draw_times) < _DRAW_HISTORY_LEN:
        return   # not enough samples yet to judge "sustained"

    if _rolling_average() > _SLOW_DRAW_THRESHOLD:
        _slow_streak += 1
    else:
        _slow_streak = 0

    if _slow_streak >= _SUSTAINED_CHECKS_REQUIRED:
        _warned_this_session = True
        try:
            bpy.ops.hijacker.perf_warning('INVOKE_DEFAULT')
        except Exception as e:
            print(f"[HIJACKER] perf warning popup failed to invoke: {e}")


def reset():
    """Reset all tracking — e.g. when the HUD is toggled off then back on,
    so a slow patch from before a toggle doesn't immediately fire the
    moment it's re-enabled."""
    global _draw_times, _slow_streak, _warned_this_session
    _draw_times           = []
    _slow_streak          = 0
    _warned_this_session  = False


class HIJACKER_OT_perf_warning(bpy.types.Operator):
    bl_idname  = "hijacker.perf_warning"
    bl_label   = "The Hijacker — Performance"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=380)

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.label(text="The Hijacker's mixer is running slow.", icon='ERROR')
        col.separator()
        col.label(text="Try collapsing racks you're not actively using —")
        col.label(text="fewer expanded racks means faster redraws and")
        col.label(text="tighter audio sync.")


_classes = (HIJACKER_OT_perf_warning,)


def register():
    for cls in _classes:
        # Safely unregister first — mirrors Loader.py's own register(),
        # which re-runs this on every addon (re)registration to support
        # hot-reloading .py changes without restarting Blender.
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
