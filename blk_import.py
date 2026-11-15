"""Blender-side import of J3D shape-key animations (``.blk``)."""

from __future__ import annotations

import math
import os

import bpy
import numpy as np
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup, UIList

from . import blender_common as bc
from .j3d import anim, blk


MAX_TRACK_ROWS = 10
_BLK_SESSION = None


class BlkImportError(Exception):
    pass


def _get_settings(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    return getattr(scene, "bmd_blk_import_settings", None)


def find_mesh(context):
    """Return the active mesh, or the first selected mesh."""
    obj = context.active_object

    if obj is not None and obj.type == "MESH":
        return obj

    for candidate in context.selected_objects:
        if candidate.type == "MESH":
            return candidate

    raise BlkImportError("Select the mesh with shape keys first")


def _shape_keys(obj):
    if obj is None or obj.type != "MESH" or obj.data is None:
        raise BlkImportError("No mesh selected")

    keys = obj.data.shape_keys

    if keys is None or len(keys.key_blocks) < 2:
        raise BlkImportError("The mesh has no shape keys")

    return keys


def _auto_assign(settings):
    """Assign tracks to shape keys in their current order."""
    target = settings.target
    blocks = []

    if target is not None and target.type == "MESH" and target.data is not None:
        keys = target.data.shape_keys

        if keys is not None:
            blocks = list(keys.key_blocks)[1:]

    for index, item in enumerate(settings.tracks):
        if index < len(blocks):
            item.shape_key = blocks[index].name
        elif target is not None:
            item.shape_key = ""


def _target_changed(self, context):
    settings = _get_settings(context)

    if settings is not None:
        _auto_assign(settings)


def _shape_key_items(_self, context):
    """Shape keys selectable for a BLK track; Basis is not offered."""
    settings = _get_settings(context)
    target = settings.target if settings is not None else None
    items = [("", "None", "Do not import this track")]

    if (target is not None and target.type == "MESH"
            and target.data is not None
            and target.data.shape_keys is not None):
        for block in list(target.data.shape_keys.key_blocks)[1:]:
            items.append((block.name, block.name, ""))

    return items


class BMD_SK_BlkTrackItem(PropertyGroup):
    track_index: IntProperty()
    label: StringProperty()
    shape_key: EnumProperty(name="Shape Key", items=_shape_key_items)
    include: BoolProperty(name="Import", default=True)


class BMD_SK_BlkImportSettings(PropertyGroup):
    target: PointerProperty(
        name="Target Mesh",
        description="Mesh that will receive the animation",
        type=bpy.types.Object,
        poll=bc.mesh_object_poll,
        update=_target_changed,
    )
    filepath: StringProperty(subtype="FILE_PATH")
    tracks: CollectionProperty(type=BMD_SK_BlkTrackItem)
    active_track: IntProperty(default=0, min=0)


class BMD_SK_UL_blk_tracks(UIList):
    bl_idname = "BMD_SK_UL_blk_tracks"

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        settings = data
        row = layout.row(align=True)
        check = row.row()
        check.prop(item, "include", text="")
        row.label(text=item.label)
        target = settings.target

        if (target is not None and target.type == "MESH"
                and target.data is not None
                and target.data.shape_keys is not None):
            row.prop(item, "shape_key", text="")
        else:
            row.label(text="No shape keys", icon="ERROR")


def _expand_slider_range(key_block, values):
    """Expand the shape-key slider to the rounded value range (max -10..10)."""
    low = max(math.floor(float(values.min())), -10)
    high = min(math.ceil(float(values.max())), 10)
    key_block.slider_min = min(key_block.slider_min, low)
    key_block.slider_max = max(key_block.slider_max, high)


def _track_key_list(track, duration):
    """Return the track's keys plus hold keys at the animation boundaries.

    BLK keys are sparse; the sampler holds the first/last value outside the
    key range.  Adding one hold key at frame 0 / at the file duration keeps
    that behaviour without keying every frame, and makes the action range
    cover the whole animation.
    """
    keys = [[float(t), float(v), float(tin), float(tout)]
            for t, v, tin, tout in track.keys]

    if not keys:
        return keys

    keys.sort(key=lambda key: key[0])
    first, last = keys[0], keys[-1]

    if first[0] > 0.0:
        keys.insert(0, [0.0, first[1], 0.0, 0.0])
        keys[1][2] = 0.0

    if last[0] < float(duration):
        keys.append([float(duration), last[1], 0.0, 0.0])
        keys[-2][3] = 0.0

    return keys


def _apply_track_keys(block, action, key_list):
    """Insert only the track's keys and reproduce its Hermite tangents."""
    if not key_list:
        return

    for time, value, _tin, _tout in key_list:
        block.value = float(value)
        block.keyframe_insert(data_path="value", frame=float(time))

    path = block.path_from_id("value")
    fcurve = action.fcurves.find(path)

    if fcurve is None:
        fcurve = next((curve for curve in action.fcurves
                       if curve.data_path == path), None)

    if fcurve is None:
        return

    fcurve.extrapolation = "CONSTANT"
    points = {round(float(point.co[0]), 6): point
              for point in fcurve.keyframe_points}
    count = len(key_list)

    for index, (time, value, tin, tout) in enumerate(key_list):
        point = points.get(round(float(time), 6))

        if point is None:
            continue

        point.interpolation = "BEZIER" if count > 1 else "CONSTANT"

        if count <= 1:
            continue

        point.handle_left_type = "FREE"
        point.handle_right_type = "FREE"

        if index > 0:
            dt_prev = float(time) - float(key_list[index - 1][0])
            point.handle_left = (
                float(time) - dt_prev / 3.0,
                float(value) - float(tin) * dt_prev / 3.0,
            )
        else:
            point.handle_left = (float(time), float(value))

        if index + 1 < count:
            dt_next = float(key_list[index + 1][0]) - float(time)
            point.handle_right = (
                float(time) + dt_next / 3.0,
                float(value) + float(tout) * dt_next / 3.0,
            )
        else:
            point.handle_right = (float(time), float(value))

    fcurve.update()


def apply_blk_animation(context, obj, animation, mapping, filepath="",
                        set_scene_range=True, report=None):
    """Apply parsed BLK tracks to shape keys.

    *mapping* is a list of ``(track_index, shape_key_name)`` pairs.
    """
    keys = _shape_keys(obj)
    blocks = {block.name: block for block in keys.key_blocks[1:]}
    frames = np.arange(0, animation.duration + 1)
    name = os.path.splitext(os.path.basename(filepath))[0] or "blk"

    # Drop slider ranges left over from a previous animation; only the new
    # track values may expand them below.
    for block in keys.key_blocks[1:]:
        block.slider_min = 0.0
        block.slider_max = 1.0

    animation_data = keys.animation_data or keys.animation_data_create()
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    action["bmd_loop_mode"] = int(animation.loop_mode)
    animation_data.action = action

    applied = 0
    keyframe_count = 0

    for track_index, shape_key in mapping:
        block = blocks.get(shape_key)

        if block is None or not (0 <= track_index < len(animation.tracks)):
            continue

        track = animation.tracks[track_index]
        values = anim.sample_track(track, frames)

        if report and (values.min() < -10.0 or values.max() > 10.0):
            report({"WARNING"},
                   f"BLK values {values.min():.3f}..{values.max():.3f} "
                   f"exceed Blender's -10..10 limit; they will be clamped")

        _expand_slider_range(block, values)
        key_list = _track_key_list(track, animation.duration)
        _apply_track_keys(block, action, key_list)
        keyframe_count += len(key_list)
        applied += 1

    if set_scene_range:
        context.scene.frame_start = 0
        context.scene.frame_end = int(animation.duration)

    try:
        context.scene.frame_set(0)
    except Exception:
        pass

    if report:
        mode = (anim.LOOP_NAMES[animation.loop_mode]
                if animation.loop_mode < len(anim.LOOP_NAMES)
                else animation.loop_mode)
        report({"INFO"},
               f"Imported BLK '{name}': {applied} tracks, "
               f"{keyframe_count} keyframes, "
               f"{animation.duration + 1} frames, loop mode {mode}")

    return action


def _populate_tracks(settings, animation, filepath):
    settings.filepath = filepath
    settings.tracks.clear()
    display_index = 0

    for index, _track in enumerate(animation.tracks):
        # The first track is the original/zero key (Maya "orig").
        if index == 0:
            continue

        display_index += 1
        item = settings.tracks.add()
        item.track_index = index
        item.label = f"Track {display_index}"
        item.include = True

    settings.active_track = 0
    _auto_assign(settings)


def begin_import(context, filepath, operator):
    """Parse a BLK file and open the track-assignment dialog."""
    global _BLK_SESSION

    try:
        with open(filepath, "rb") as file:
            animation = blk.parse_blk(file.read())
    except Exception as exc:
        operator.report({"ERROR"}, f"Could not read BLK: {exc}")
        return {"CANCELLED"}

    settings = _get_settings(context)

    if settings is None:
        operator.report({"ERROR"}, "BLK import settings are unavailable")
        return {"CANCELLED"}

    try:
        target = find_mesh(context)
    except BlkImportError:
        target = None

    if target is not None:
        settings.target = target

    _populate_tracks(settings, animation, filepath)
    _BLK_SESSION = animation

    bpy.app.timers.register(_open_settings_dialog, first_interval=0.05)
    return {"FINISHED"}


def _open_settings_dialog():
    try:
        bpy.ops.bmd_shape_keys.blk_settings("INVOKE_DEFAULT")
    except Exception as exc:  # pragma: no cover - only in unusual UI states
        print(f"BLK importer: could not open settings dialog: {exc}")

    return None


class BMD_SK_OT_blk_auto_assign(Operator):
    bl_idname = "bmd_shape_keys.blk_auto_assign"
    bl_label = "Auto Assign"
    bl_description = "Assign tracks to shape keys in their current order"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = _get_settings(context)

        if settings is not None:
            _auto_assign(settings)

        return {"FINISHED"}


class BMD_SK_OT_blk_settings(Operator):
    bl_idname = "bmd_shape_keys.blk_settings"
    bl_label = "Import BLK"
    bl_description = "Assign BLK tracks to shape keys and import the animation"
    bl_options = {"REGISTER", "UNDO"}

    set_scene_range: BoolProperty(
        name="Set Scene Range", default=True,
        description="Set the scene frame range to the animation length")

    def invoke(self, context, _event):
        if _BLK_SESSION is None:
            self.report({"ERROR"}, "No BLK file is loaded")
            return {"CANCELLED"}

        settings = _get_settings(context)

        if settings is None:
            self.report({"ERROR"}, "BLK import settings are unavailable")
            return {"CANCELLED"}

        if not settings.tracks:
            self.report({"ERROR"}, "The BLK file has no tracks")
            return {"CANCELLED"}

        _auto_assign(settings)
        return context.window_manager.invoke_props_dialog(
            self, width=620, confirm_text="Import")

    def draw(self, context):
        layout = self.layout
        settings = _get_settings(context)

        if settings is None:
            layout.label(text="BLK import settings are unavailable", icon="ERROR")
            return

        box = layout.box()
        box.label(text=f"File: {os.path.basename(settings.filepath)}", icon="FILE")
        row = box.row()
        row.alert = settings.target is None
        row.prop(settings, "target")
        box.prop(self, "set_scene_range")

        if settings.target is None:
            layout.label(text="Select the mesh with shape keys", icon="INFO")
            return

        if not settings.tracks:
            layout.label(text="The BLK file has no tracks", icon="ERROR")
            return

        rows = min(max(1, len(settings.tracks)), MAX_TRACK_ROWS)
        layout.template_list(
            "BMD_SK_UL_blk_tracks", "",
            settings, "tracks",
            settings, "active_track",
            rows=rows,
            maxrows=MAX_TRACK_ROWS,
        )
        layout.operator("bmd_shape_keys.blk_auto_assign", text="Auto Assign",
                        icon="FILE_REFRESH")
        layout.label(text="Tracks are assigned in shape-key order by default",
                     icon="INFO")

        if not any(item.include and item.shape_key for item in settings.tracks):
            layout.label(text="Assign at least one track to a shape key",
                         icon="ERROR")

    def execute(self, context):
        global _BLK_SESSION

        settings = _get_settings(context)

        if _BLK_SESSION is None or settings is None or settings.target is None:
            self.report({"ERROR"}, "BLK import data is unavailable")
            return {"CANCELLED"}

        mapping = []
        used = set()

        for item in settings.tracks:
            if not item.include or not item.shape_key:
                continue

            if item.shape_key in used:
                self.report({"WARNING"},
                            f"Shape key '{item.shape_key}' is assigned to "
                            f"more than one track")

            used.add(item.shape_key)
            mapping.append((item.track_index, item.shape_key))

        if not mapping:
            self.report({"WARNING"}, "No tracks assigned to shape keys")
            return {"CANCELLED"}

        try:
            apply_blk_animation(
                context, settings.target, _BLK_SESSION, mapping,
                filepath=settings.filepath,
                set_scene_range=self.set_scene_range,
                report=self.report,
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Import failed: {exc}")
            return {"CANCELLED"}

        _BLK_SESSION = None
        return {"FINISHED"}


CLASSES = (
    BMD_SK_BlkTrackItem,
    BMD_SK_BlkImportSettings,
    BMD_SK_UL_blk_tracks,
    BMD_SK_OT_blk_auto_assign,
    BMD_SK_OT_blk_settings,
)
