"""
core/vse_compat.py
===================
Compatibility shim for Blender's VSE "Sequence -> Strip" rename.

bpy.types.Sequence was renamed to bpy.types.Strip starting in Blender 4.4.
The old SequenceEditor.sequences / .sequences_all accessors kept working as
aliases through (at least) 4.5, but by Blender 5.x they're gone entirely —
replaced by SequenceEditor.strips / .strips_all. Confirmed directly against
a running Blender 5.2 (via bpy.context.scene.sequence_editor's dir()):
only 'strips' / 'strips_all' / 'active_strip' exist there, and .strips'
factory methods (new_sound, new_effect, new_meta, etc.) are unchanged.

Since this addon's bl_info declares a minimum of Blender 4.3 and needs to
keep working there too, every call site uses these two helpers instead of
hardcoding either name, so the addon runs unmodified from 4.3 through 5.x
(and future renames only need a fix in this one file).
"""


def get_all_strips(seq_editor):
    """Recursive strip collection — every strip in the sequence editor,
    including ones nested inside meta strips.
    Blender < 5.0: SequenceEditor.sequences_all
    Blender >= 5.0: SequenceEditor.strips_all
    Returns an empty list if seq_editor is None (no VSE data yet)."""
    if seq_editor is None:
        return []
    if hasattr(seq_editor, "strips_all"):
        return seq_editor.strips_all
    return seq_editor.sequences_all


def get_strips_collection(seq_editor):
    """Top-level (non-recursive) strip collection — used to call its
    factory methods (.new_sound(), .new_effect(), .new_meta(), etc.) to
    add strips.
    Blender < 5.0: SequenceEditor.sequences
    Blender >= 5.0: SequenceEditor.strips
    Returns None if seq_editor is None."""
    if seq_editor is None:
        return None
    if hasattr(seq_editor, "strips"):
        return seq_editor.strips
    return seq_editor.sequences
