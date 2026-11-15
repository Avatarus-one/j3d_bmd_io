"""Blender-side export of J3D shape-key animations (``.blk``)."""

from __future__ import annotations

import math
import os

import bpy
from bpy.props import EnumProperty, IntProperty
from bpy.types import Operator

from . import bls_export_ui
from .blender_anim import LOOP_ITEMS
from .j3d import anim, blk
from .j3d.common import J3DError


BLK_LOOP_ITEMS = [item for item in LOOP_ITEMS if item[0] != "AUTO"]


class BlkExportError(Exception):
    pass


def find_mesh(context):
    """Return the active mesh, or the first selected mesh."""
    obj = context.active_object

    if obj is not None and obj.type == "MESH":
        return obj

    for candidate in context.selected_objects:
        if candidate.type == "MESH":
            return candidate

    raise BlkExportError("Select the mesh with shape keys first")


def _targets(keys):
    """Return ``[(name, block), ...]`` in the current shape-key order.

    BLK tracks are positional, so the track list follows the shape keys that
    currently exist on the mesh.  The BLS a key came from is deliberately not
    consulted: keys may be imported from several files, deleted, renamed or
    reordered at any time.
    """
    return [(block.name, block) for block in list(keys.key_blocks)[1:]]


def export_blk(context, filepath, key_names=None, loop_mode="ONCE",
               frame_source="ACTION", report=None, target=None,
               custom_range=None):
    """Export the shape-key animation of *target* to *filepath*.

    *key_names* selects which shape keys are animated; other keys are skipped.
    *custom_range* is ``(start, end)`` for ``frame_source="CUSTOM"``.
    """
    obj = target if target is not None else find_mesh(context)

    if obj is None or obj.type != "MESH":
        raise BlkExportError("Select the mesh with shape keys first")

    keys = obj.data.shape_keys if obj.data else None

    if keys is None or len(keys.key_blocks) < 2:
        raise BlkExportError("The mesh has no shape keys")

    animation_data = keys.animation_data
    action = animation_data.action if animation_data else None

    try:
        mode = anim.LOOP_NAMES.index(loop_mode)
    except ValueError:
        raise BlkExportError(f"Unknown loop mode: {loop_mode}")

    if frame_source == "CUSTOM":
        if custom_range is None:
            raise BlkExportError("Custom frame range is not set")

        start, end = int(custom_range[0]), int(custom_range[1])
    elif frame_source == "SCENE":
        start, end = context.scene.frame_start, context.scene.frame_end
    elif frame_source == "ACTION":
        if action is None:
            raise BlkExportError("The shape keys have no active action")

        f0, f1 = action.frame_range
        start, end = int(math.floor(f0)), int(math.ceil(f1))
    else:
        raise BlkExportError(f"Unknown frame range: {frame_source}")

    if end < start:
        raise BlkExportError("End frame must not be before start frame")

    frames = list(range(start, end + 1))
    selected = set(key_names) if key_names is not None else None
    targets = _targets(keys)
    selected_targets = [
        (name, block) for name, block in targets
        if selected is None or name in selected
    ]
    if not selected_targets:
        raise BlkExportError("No shape keys selected for export")

    scene = context.scene
    current = scene.frame_current
    sampled = []

    try:
        for name, block in selected_targets:
            values = []

            for frame in frames:
                scene.frame_set(frame)
                values.append(float(block.value))

            sampled.append((name, block, values))
    finally:
        scene.frame_set(current)

    orig_values = []

    for index in range(len(frames)):
        total = sum(values[index] for _name, _block, values in sampled)
        orig_values.append(1.0 - total)

    tracks = [anim.fit_track(orig_values, 1e-4)]
    tracks.extend(anim.fit_track(values, 1e-4)
                  for _name, _block, values in sampled)

    animation = blk.Blk()
    animation.loop_mode = mode
    animation.duration = len(frames) - 1
    animation.tracks = tracks
    data = blk.write_blk(animation)

    with open(filepath, "wb") as file:
        file.write(data)

    if action is not None:
        try:
            action["bmd_loop_mode"] = mode
        except Exception:
            pass

    if report:
        parts = [
            f"Exported BLK: {len(tracks)} tracks",
            f"{len(frames)} frames",
            f"{len(data)} bytes",
        ]

        report({"INFO"}, ", ".join(parts))

    return animation


def open_export_dialog():
    """Timer callback: open the BLK export dialog after the file browser."""
    try:
        bpy.ops.bmd_shape_keys.blk_export_settings("INVOKE_DEFAULT")
    except Exception as exc:  # pragma: no cover - only in unusual UI states
        print(f"BLK exporter: could not open settings dialog: {exc}")

    return None


def _target_action(context):
    """Return the active shape-key action of the dialog's target mesh."""
    settings = bls_export_ui._get_export_settings(context)
    target = settings.target if settings is not None else None

    if target is not None and target.type == "MESH" and target.data is not None:
        keys = target.data.shape_keys

        if keys is not None and keys.animation_data is not None:
            return keys.animation_data.action

    return None


def _loop_mode_name(action):
    """Loop mode stored on *action*, or Once for a fresh animation."""
    if action is None:
        return "ONCE"

    try:
        mode = int(action.get("bmd_loop_mode", anim.LOOP_ONCE))
    except (TypeError, ValueError):
        return "ONCE"

    if 0 <= mode < len(anim.LOOP_NAMES):
        return anim.LOOP_NAMES[mode]

    return "ONCE"


def _sync_frame_range(operator, context):
    """Fill Start/End Frame from the selected frame-range variant."""
    if operator.frame_source == "SCENE":
        operator.custom_start = context.scene.frame_start
        operator.custom_end = context.scene.frame_end
    elif operator.frame_source == "ACTION":
        action = _target_action(context)

        if action is None:
            operator.custom_start = context.scene.frame_start
            operator.custom_end = context.scene.frame_end
        else:
            start, end = action.frame_range
            operator.custom_start = int(math.floor(start))
            operator.custom_end = int(math.ceil(end))


def _frame_source_changed(operator, context):
    _sync_frame_range(operator, context)


_TARGET_MISSING = object()


def _sync_target_settings(operator, context):
    """Refresh loop mode and range fields when the dialog target changes."""
    settings = bls_export_ui._get_export_settings(context)
    target = settings.target if settings is not None else None

    if getattr(operator, "_target_cache", _TARGET_MISSING) is target:
        return

    operator._target_cache = target
    operator.loop_mode = _loop_mode_name(_target_action(context))

    if operator.frame_source == "ACTION":
        _sync_frame_range(operator, context)


class BMD_SK_OT_blk_export_settings(Operator):
    bl_idname = "bmd_shape_keys.blk_export_settings"
    bl_label = "Export BLK"
    bl_description = "Choose which shape-key animations to write"
    bl_options = {"REGISTER", "UNDO"}

    loop_mode: EnumProperty(name="Loop Mode", items=BLK_LOOP_ITEMS,
                            default="ONCE")
    frame_source: EnumProperty(
        name="Frame Range",
        items=[("ACTION", "Action Range",
                "Frame range of the shape-key action"),
               ("SCENE", "Scene Range", "Scene start / end frame"),
               ("CUSTOM", "Custom Range",
                "Use the Start Frame and End Frame fields")],
        default="ACTION",
        update=_frame_source_changed)
    custom_start: IntProperty(name="Start Frame", default=0)
    custom_end: IntProperty(name="End Frame", default=0)

    def invoke(self, context, _event):
        settings = bls_export_ui._get_export_settings(context)

        if settings is None or not settings.blk_filepath:
            self.report({"ERROR"}, "Select an export file first")
            return {"CANCELLED"}

        settings.show_base = False
        bls_export_ui._populate_export_keys(settings, settings.target)

        self.frame_source = "ACTION"
        _sync_target_settings(self, context)

        return context.window_manager.invoke_props_dialog(
            self, width=620, confirm_text="Export")

    def draw(self, context):
        layout = self.layout
        settings = bls_export_ui._get_export_settings(context)

        if settings is None:
            layout.label(text="BLK export settings are unavailable", icon="ERROR")
            return

        _sync_target_settings(self, context)

        box = layout.box()
        box.label(text=f"File: {os.path.basename(settings.blk_filepath)}", icon="FILE")
        row = box.row()
        row.alert = settings.target is None
        row.prop(settings, "target")
        box.prop(self, "loop_mode")
        box.prop(self, "frame_source")
        range_row = box.row(align=True)
        range_row.enabled = self.frame_source == "CUSTOM"
        range_row.prop(self, "custom_start")
        range_row.prop(self, "custom_end")

        if (self.frame_source == "CUSTOM"
                and self.custom_end < self.custom_start):
            layout.label(text="End Frame must not be before Start Frame",
                         icon="ERROR")

        if settings.target is None:
            layout.label(text="Select a target mesh", icon="INFO")
            return

        if not settings.keys:
            layout.label(text="No shape keys found", icon="ERROR")
            return

        visible = len(settings.tree_rows) if settings.expanded else 1
        rows = min(max(1, visible), bls_export_ui.MAX_EXPORT_ROWS)
        layout.template_list(
            "BMD_SK_UL_export_tree", "",
            settings, "tree_rows",
            settings, "active_tree_row",
            rows=rows,
            maxrows=bls_export_ui.MAX_EXPORT_ROWS,
        )
        layout.label(
            text="Selected shape keys must match the BLS keys",
            icon="INFO",
        )

        if not any(item.include for item in settings.keys):
            layout.label(text="Select at least one shape key to export",
                         icon="ERROR")

    def execute(self, context):
        settings = bls_export_ui._get_export_settings(context)

        if settings is None or settings.target is None or not settings.blk_filepath:
            self.report({"ERROR"}, "BLK export settings are incomplete")
            return {"CANCELLED"}

        names = [item.name for item in settings.keys if item.include]

        if not names:
            self.report({"WARNING"}, "No shape keys selected for export")
            return {"CANCELLED"}

        custom_range = ((self.custom_start, self.custom_end)
                        if self.frame_source == "CUSTOM" else None)

        try:
            export_blk(context, settings.blk_filepath, names,
                       self.loop_mode, self.frame_source, self.report,
                       target=settings.target, custom_range=custom_range)
        except (BlkExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # e.g. file-system errors
            self.report({"ERROR"}, f"Export failed: {exc}")
            return {"CANCELLED"}

        return {"FINISHED"}


CLASSES = (BMD_SK_OT_blk_export_settings,)
