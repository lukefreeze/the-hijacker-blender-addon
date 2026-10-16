# =============================================================================
# core/prefs.py
# The Hijacker — audio settings (output device, buffer size, latency compensation)
#
# Where they appear:
#   * Node Editor > Sidebar (N) > Tool tab > "The Hijacker: Audio"  (always; the
#     speaker button in the Node Editor header opens the sidebar)
#   * Edit > Preferences > Add-ons > The Hijacker                   (when the add-on
#                                                                   is installed as one)
#
# Both show the same settings. They are stored in a small JSON file in Blender's
# config folder (not in the .blend), so they follow the user from project to
# project and work the same whether the add-on is installed or run from the
# dev Loader.py script (which Blender doesn't treat as an add-on, so it can't
# have a page in the Add-ons list).
#
# The Hijacker plays through PortAudio itself and switches Blender's own audio
# device off while it is open, so Blender's Preferences > System > Audio has no
# effect on it. Device and buffer changes take effect when the engine starts,
# or immediately with "Apply Now". Latency compensation applies live.
#
# The device is stored by NAME, not index: PortAudio device indices change
# whenever a USB interface is plugged in or a driver reorders itself.
# =============================================================================

import json
import os

import bpy
from bpy.props import EnumProperty, IntProperty, PointerProperty

from core.engine import get_engine

ADDON_ID = ""            # set by register() when running as an installed add-on
SYSTEM_DEFAULT_ID = "SYSTEM_DEFAULT"
_FILE_NAME = "the_hijacker_audio_settings.json"

_DEFAULTS = {"output_device": "", "buffer_frames": 0,
             "latency_mode": "AUTO", "latency_ms": 0}
_settings = dict(_DEFAULTS)
_loaded = False

_devices = None          # cached list of dicts from engine.list_output_devices()
_enum_items = []         # kept alive: Blender holds raw pointers into these strings

_BUFFER_VALUES = [0, 64, 128, 256, 512, 1024, 2048]
_BUFFER_ITEMS = [
    ("0",    "Automatic",    "Let the audio driver choose. Best for most setups", "", 0),
    ("64",   "64 samples",   "Snappiest response, but crackles easily. Needs a fast machine and a good interface", "", 1),
    ("128",  "128 samples",  "Very snappy; may crackle on busy projects", "", 2),
    ("256",  "256 samples",  "Snappy; fine on most modern machines", "", 3),
    ("512",  "512 samples",  "Balanced", "", 4),
    ("1024", "1024 samples", "Safer against crackles, slightly slower response", "", 5),
    ("2048", "2048 samples", "Safest against crackles; use if you hear dropouts", "", 6),
]
_LAT_MODES = ["AUTO", "OFF", "MANUAL"]
_LAT_ITEMS = [
    ("AUTO",   "Automatic", "Starts the sound early by the output device's measured delay so it lines up with the picture. Best for most setups", "", 0),
    ("OFF",    "Off",       "No shift: the sound arrives late by the device's delay (about 10-40 ms on wired outputs, 100-250 ms on Bluetooth)", "", 1),
    ("MANUAL", "Manual",    "Use your own fixed offset instead of the measured one (for Bluetooth, TVs and receivers whose delay the computer can't know)", "", 2),
]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _path():
    try:
        return os.path.join(bpy.utils.user_resource('CONFIG', create=True), _FILE_NAME)
    except Exception:
        return ""


def load():
    global _loaded
    _settings.clear()
    _settings.update(_DEFAULTS)
    p = _path()
    if p and os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("output_device"), str):
                _settings["output_device"] = data["output_device"]
            if data.get("buffer_frames") in _BUFFER_VALUES:
                _settings["buffer_frames"] = data["buffer_frames"]
            if data.get("latency_mode") in _LAT_MODES:
                _settings["latency_mode"] = data["latency_mode"]
            if isinstance(data.get("latency_ms"), int):
                _settings["latency_ms"] = max(0, min(250, data["latency_ms"]))
        except Exception as e:
            print(f"[HIJACKER] could not read audio settings ({e}); using defaults")
    _loaded = True


def _save():
    p = _path()
    if not p:
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(_settings, f, indent=2)
    except Exception as e:
        print(f"[HIJACKER] could not save audio settings: {e}")


def _ensure_loaded():
    if not _loaded:
        load()


# ---------------------------------------------------------------------------
# Public helpers (used by core/audio.py)
# ---------------------------------------------------------------------------
def audio_settings():
    """(device_name, buffer_frames) to hand to engine_init().
    device_name '' means the system default; buffer_frames 0 means automatic."""
    _ensure_loaded()
    return _settings["output_device"], int(_settings["buffer_frames"])


def apply_latency(hj):
    """Push the latency-compensation setting into a running engine."""
    _ensure_loaded()
    if hj is None or not hasattr(hj, "set_latency_compensation"):
        return
    try:
        mode = _settings["latency_mode"]
        if mode == 'AUTO':
            hj.set_latency_compensation(-1.0)
        elif mode == 'OFF':
            hj.set_latency_compensation(0.0)
        else:
            hj.set_latency_compensation(_settings["latency_ms"] / 1000.0)
    except Exception as e:
        print(f"[HIJACKER] could not set latency compensation: {e}")


def refresh_devices():
    """Re-read the output device list from the engine. PortAudio only really
    rescans hardware while the audio engine isn't running."""
    global _devices
    _devices = []
    engine = get_engine()
    if engine and hasattr(engine, "list_output_devices"):
        try:
            _devices = list(engine.list_output_devices())
        except Exception as e:
            print(f"[HIJACKER] could not list audio devices: {e}")
    return _devices


def get_devices():
    if _devices is None:
        refresh_devices()
    return _devices or []


# ---------------------------------------------------------------------------
# Property plumbing (all properties read/write the module-level _settings, so
# nothing is lost if Blender replaces the window manager when a file loads)
# ---------------------------------------------------------------------------
def _device_items(self, context):
    global _enum_items
    _ensure_loaded()
    items = [(SYSTEM_DEFAULT_ID, "System Default",
              "Follow whatever Windows / macOS / Linux has set as the default output", 'NONE', 0)]
    saved = _settings["output_device"]
    found = False
    for i, d in enumerate(get_devices()):
        label = d.get("name", "?")
        desc = (f"{d.get('host_api', '')}  |  {d.get('default_sample_rate', 0):.0f} Hz  |  "
                f"{d.get('low_latency_s', 0) * 1000:.1f} ms low-latency estimate")
        items.append((f"DEV{i}", label, desc, 'NONE', i + 1))
        if saved and label == saved:
            found = True
    if saved and not found:
        items.append(("MISSING", f"{saved} (not connected)",
                      "This device isn't available right now. The Hijacker will use "
                      "the system default until it comes back.", 'ERROR', len(items)))
    _enum_items = items
    return _enum_items


def _device_get(self):
    saved = _settings["output_device"]
    if not saved:
        return 0
    for it in (_enum_items or _device_items(self, None)):
        if it[0] == "MISSING" or (it[0] != SYSTEM_DEFAULT_ID and it[1] == saved):
            return it[4]
    return 0


def _device_set(self, value):
    for it in (_enum_items or _device_items(self, None)):
        if it[4] == value:
            if it[0] == SYSTEM_DEFAULT_ID:
                _settings["output_device"] = ""
            elif it[0] != "MISSING":
                _settings["output_device"] = it[1]
            _save()
            return


def _buffer_get(self):
    v = _settings["buffer_frames"]
    return _BUFFER_VALUES.index(v) if v in _BUFFER_VALUES else 0


def _buffer_set(self, i):
    if 0 <= i < len(_BUFFER_VALUES):
        _settings["buffer_frames"] = _BUFFER_VALUES[i]
        _save()


def _live_latency():
    engine = get_engine()
    if engine:
        try:
            apply_latency(engine.get_engine())
        except Exception:
            pass


def _latmode_get(self):
    m = _settings["latency_mode"]
    return _LAT_MODES.index(m) if m in _LAT_MODES else 0


def _latmode_set(self, i):
    if 0 <= i < len(_LAT_MODES):
        _settings["latency_mode"] = _LAT_MODES[i]
        _save()
        _live_latency()


def _latms_get(self):
    return int(_settings["latency_ms"])


def _latms_set(self, v):
    _settings["latency_ms"] = max(0, min(250, int(v)))
    _save()
    _live_latency()


class HIJACKER_PG_AudioSettings(bpy.types.PropertyGroup):
    output_device: EnumProperty(
        name="Output Device", description="Where the Hijacker plays its audio",
        items=_device_items, get=_device_get, set=_device_set)
    buffer_mode: EnumProperty(
        name="Buffer Size",
        description="Audio buffer size. Smaller = less delay but more risk of crackles",
        items=_BUFFER_ITEMS, get=_buffer_get, set=_buffer_set)
    latency_mode: EnumProperty(
        name="Latency Compensation",
        description="Keeps the sound lined up with the picture and the playhead",
        items=_LAT_ITEMS, get=_latmode_get, set=_latmode_set)
    latency_ms: IntProperty(
        name="Offset (ms)", description="Fixed audio delay compensation",
        min=0, max=250, soft_max=250, get=_latms_get, set=_latms_set)


def _group(context):
    try:
        return context.window_manager.hijacker_audio
    except Exception:
        return None


# ---------------------------------------------------------------------------
# UI (shared by the sidebar panel and the Add-ons page)
# ---------------------------------------------------------------------------
def draw_settings(layout, st, compact=False):
    if st is None:
        layout.label(text="Audio settings unavailable", icon='ERROR')
        return
    _ensure_loaded()

    box = layout.box()
    box.label(text="Audio Output", icon='SPEAKER')
    row = box.row(align=True)
    row.prop(st, "output_device", text="" if compact else "Device")
    row.operator("hijacker.refresh_audio_devices", text="", icon='FILE_REFRESH')
    box.prop(st, "buffer_mode", text="" if compact else "Buffer Size")
    frames = int(_settings["buffer_frames"])
    box.label(text=(f"~{frames / 48000.0 * 1000.0:.1f} ms per buffer at 48 kHz" if frames
                    else "Driver picks the buffer size"), icon='TIME')
    if not get_devices():
        box.label(text="No output devices listed (engine not installed?)", icon='ERROR')
    if compact:
        box.label(text="Applies next time you open the Hijacker")
    else:
        col = box.column(align=True)
        col.label(text="Changes apply the next time the Hijacker is opened,")
        col.label(text="or right now with the button below.")
    box.operator("hijacker.apply_audio_settings", icon='FILE_REFRESH')

    box = layout.box()
    box.label(text="Sync", icon='TIME')
    box.prop(st, "latency_mode", text="" if compact else "Latency Compensation")
    if _settings["latency_mode"] == 'MANUAL':
        box.prop(st, "latency_ms")

    box = layout.box()
    box.label(text="Status", icon='INFO')
    hj = None
    try:
        import core.audio as _audio
        if _audio._pb_engine_active:
            eng = get_engine()
            hj = eng.get_engine() if eng else None
    except Exception:
        hj = None
    if hj is None:
        box.label(text="Hijacker is closed." if compact else
                  "Hijacker is closed. Open it from the Node Editor header to see live status.")
        return
    try:
        col = box.column(align=True)
        name = hj.get_output_device_name() if hasattr(hj, "get_output_device_name") else ""
        col.label(text=f"Device: {name or 'system default'}")
        col.label(text=f"Sample rate: {hj.get_sample_rate()} Hz")
        col.label(text=f"Output latency: {hj.get_output_latency_s() * 1000.0:.1f} ms")
        col.label(text=f"Dropouts since start: {hj.get_xrun_count()}")
        col.label(text="Master bus: " + ("ready" if hasattr(hj, "set_master_gain")
                                         else "engine needs a rebuild"),
                  icon='CHECKMARK' if hasattr(hj, "set_master_gain") else 'ERROR')
        if not hj.is_running():
            col.label(text="Audio device stopped. Reopen the Hijacker.", icon='ERROR')
    except Exception as e:
        box.label(text=f"Status unavailable: {e}")


# ---------------------------------------------------------------------------
# Operators, panel, add-on page
# ---------------------------------------------------------------------------
def _redraw_all(context):
    try:
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                area.tag_redraw()
    except Exception:
        pass


class HIJACKER_OT_RefreshAudioDevices(bpy.types.Operator):
    bl_idname = "hijacker.refresh_audio_devices"
    bl_label = "Refresh Devices"
    bl_description = ("Re-scan for audio output devices. (If the Hijacker is open "
                      "the list can only update once it is closed and reopened.)")

    def execute(self, context):
        refresh_devices()
        _redraw_all(context)
        self.report({'INFO'}, f"{len(get_devices())} output device(s) found")
        return {'FINISHED'}


class HIJACKER_OT_ApplyAudioSettings(bpy.types.Operator):
    bl_idname = "hijacker.apply_audio_settings"
    bl_label = "Apply Now"
    bl_description = "Restart the Hijacker audio engine so the new device / buffer size is used"

    def execute(self, context):
        import core.audio as _audio
        if not _audio._pb_engine_active:
            self.report({'INFO'}, "Saved. It will be used the next time the Hijacker is opened.")
            return {'FINISHED'}
        try:
            if context.screen.is_animation_playing:
                bpy.ops.screen.animation_cancel(restore_frame=False)
        except Exception:
            pass
        try:
            _audio._pb_engine_disable()
            _audio._pb_engine_enable()
        except Exception as e:
            self.report({'ERROR'}, f"Could not restart audio engine: {e}")
            return {'CANCELLED'}
        if _audio._pb_engine_active:
            self.report({'INFO'}, "Audio engine restarted")
        else:
            self.report({'WARNING'}, "Audio engine did not start. See the system console.")
        _redraw_all(context)
        return {'FINISHED'}


AUDIO_PANEL_CATEGORY = "Tool"     # which tab of the Node Editor sidebar holds the panel


class HIJACKER_PT_AudioSettings(bpy.types.Panel):
    bl_label = "The Hijacker: Audio"
    bl_idname = "HIJACKER_PT_AudioSettings"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = AUDIO_PANEL_CATEGORY

    def draw(self, context):
        draw_settings(self.layout, _group(context), compact=True)


class HIJACKER_OT_OpenAudioSettings(bpy.types.Operator):
    bl_idname = "hijacker.open_audio_settings"
    bl_label = "Audio Settings"
    bl_description = ("Open the sidebar with the Hijacker's audio settings "
                      "(output device, buffer size, sync). Look in the Tool tab")

    @classmethod
    def poll(cls, context):
        return context.space_data is not None and context.space_data.type == 'NODE_EDITOR'

    def execute(self, context):
        context.space_data.show_region_ui = True
        _redraw_all(context)
        return {'FINISHED'}


class HijackerPreferences(bpy.types.AddonPreferences):
    bl_idname = "blender_sync"      # replaced by register() with the real package name

    def draw(self, context):
        draw_settings(self.layout, _group(context), compact=False)


_classes = [
    HIJACKER_PG_AudioSettings,
    HIJACKER_OT_RefreshAudioDevices,
    HIJACKER_OT_ApplyAudioSettings,
    HIJACKER_OT_OpenAudioSettings,
    HIJACKER_PT_AudioSettings,
]


def _register_class(cls):
    try:
        bpy.utils.register_class(cls)
    except Exception:
        # A previous copy of this class (before a dev-script reload) is still registered
        old = getattr(bpy.types, cls.__name__, None)
        if old is not None:
            try:
                bpy.utils.unregister_class(old)
            except Exception:
                pass
        bpy.utils.register_class(cls)


def register(addon_id=""):
    """Always registers the sidebar settings; also the Add-ons page when the
    add-on is installed (addon_id = the real package name)."""
    global ADDON_ID
    ADDON_ID = addon_id or ""
    load()
    # The panel used to live in the Sequence Editor sidebar; remove a leftover copy
    # if a dev-script reload left one registered.
    _old = getattr(bpy.types, "VSE_PT_Hijacker_Audio", None)
    if _old is not None:
        try:
            bpy.utils.unregister_class(_old)
        except Exception:
            pass
    for cls in _classes:
        _register_class(cls)
    bpy.types.WindowManager.hijacker_audio = PointerProperty(type=HIJACKER_PG_AudioSettings)
    if ADDON_ID:
        HijackerPreferences.bl_idname = ADDON_ID
        _register_class(HijackerPreferences)
    else:
        print("[HIJACKER] running as a script (not an installed add-on): audio settings are in "
              "the Node Editor sidebar (N) > Tool tab")


def unregister():
    for cls in [HijackerPreferences] + list(reversed(_classes)):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
    try:
        del bpy.types.WindowManager.hijacker_audio
    except Exception:
        pass
