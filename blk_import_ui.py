"""Blender-side UI for importing J3D shape-key animations (``.blk``)."""

from __future__ import annotations

import os

import bpy
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
from . import blk_import
from . import bls_import
from . import bls_session
from .j3d import blk


MAX_TRACK_ROWS = 10
_BLK_SESSION = None


def _get_settings(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    return getattr(scene, "bmd_blk_import_settings", None)


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
        else:
            item.shape_key = "__none__"


def _is_shape_key_action(action):
    return any("key_blocks[" in curve.data_path
               for curve in action.fcurves)


def _action_lookup():
    """Return ``({source: action}, {name: action})`` for existing BLK actions.

    Built once per refresh; a future multi-file import can reuse it for every
    file instead of scanning ``bpy.data.actions`` each time.
    """
    by_source = {}
    by_name = {}

    for action in bpy.data.actions:
        source = action.get("bmd_source")
        if source:
            by_source.setdefault(source, action)
        elif _is_shape_key_action(action):
            by_name.setdefault(action.name, action)

    return by_source, by_name


def _existing_action(filepath, lookup=None):
    if not filepath:
        return None

    name = os.path.splitext(os.path.basename(filepath))[0]
    source = os.path.abspath(filepath)
    by_source, by_name = lookup if lookup is not None else _action_lookup()

    return by_source.get(source) or by_name.get(name)


def _refresh_animation(settings):
    target = settings.target
    if not settings.animations:
        return
    animation = settings.animations[0]
    track_count = max(0, len(_BLK_SESSION.tracks) - 1) if _BLK_SESSION else 0
    animation.track_count = track_count
    animation.compatible = False
    animation.include = False
    animation.already_imported = False
    animation.status_icon = "ERROR"
    animation.status = ""
    settings.warning = ""

    if target is None:
        animation.status = "No target mesh"
        return
    if track_count == 0:
        animation.status = "No tracks"
        return

    path = settings.bls_filepath.strip()
    session = None
    bls_error = ""
    if path:
        session, bls_error = blk_import._bls_session_for(target, path)

    lookup = _action_lookup()

    if session is not None:
        file_entry = session.files[0]
        keys = blk_import._animated_keys(file_entry)
        key_count = len(keys)
        animation.key_count = key_count
        if key_count != track_count:
            animation.status = (
                f"Incompatible: {track_count} tracks vs {key_count} BLS keys")
            return

        incompatible = [key for key in keys
                        if key.status == bls_session.INCOMPATIBLE]
        if incompatible:
            animation.status = (
                f"Incompatible: {len(incompatible)} BLS key(s) do not fit this mesh")
            return

        animation.compatible = True
        animation.include = True
        animation.status = f"OK ({track_count} tracks)"
        animation.status_icon = "CHECKMARK"

        importable = [key for key in keys if key.status == bls_session.UNIQUE]
        duplicates = [key for key in keys if key.status == bls_session.DUPLICATE]
        conflicts = [key for key in keys if key.status == bls_session.CONFLICT]
        parts = []
        # Only mention the BLS import when something is actually imported;
        # a file with nothing but duplicates/conflicts needs no message.
        if importable:
            parts.append(f"{len(importable)} missing key(s) will be imported from BLS")
            if duplicates:
                parts.append(f"{len(duplicates)} duplicate(s) skipped")
            if conflicts:
                parts.append(f"{len(conflicts)} conflict(s) skipped")
        if parts:
            settings.warning = "; ".join(parts)

        existing = _existing_action(settings.filepath, lookup)
        if existing is not None:
            animation.status = f"Already imported: {existing.name}"
            animation.status_icon = "INFO"
            animation.already_imported = True
            animation.include = False
        return

    # No usable BLS: map by the mesh's existing shape-key order.
    if bls_error:
        settings.warning = f"{bls_error}; using shape-key order"
    elif not path:
        settings.warning = "No BLS file selected; using shape-key order"

    shape_keys = target.data.shape_keys if target.data else None
    blocks = list(shape_keys.key_blocks)[1:] if shape_keys else []
    key_count = len(blocks)
    animation.key_count = key_count
    if key_count != track_count:
        animation.status = (
            f"Incompatible: {track_count} tracks vs {key_count} shape keys")
        return

    animation.compatible = True
    animation.include = True
    animation.status = f"OK ({track_count} tracks, by shape-key order)"
    animation.status_icon = "CHECKMARK"

    existing = _existing_action(settings.filepath, lookup)
    if existing is not None:
        animation.status = f"Already imported: {existing.name}"
        animation.status_icon = "INFO"
        animation.already_imported = True
        animation.include = False


def _target_changed(self, context):
    settings = _get_settings(context)

    if settings is not None:
        # The BLS path belongs to the mesh, not to the scene: switching the
        # target loads that mesh's own remembered BLS file.
        target = settings.target
        source = target.get("bmd_bls_source", "") if target is not None else ""
        settings.bls_filepath = str(source) if source else ""
        _auto_assign(settings)
        _refresh_animation(settings)


def _bls_path_changed(self, context):
    settings = _get_settings(context)

    if settings is not None:
        _refresh_animation(settings)


def _shape_key_items(_self, context):
    """Shape keys selectable for a BLK track; Basis is not offered."""
    settings = _get_settings(context)
    target = settings.target if settings is not None else None
    items = [("__none__", "None", "Do not import this track")]

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


class BMD_SK_BlkAnimationItem(PropertyGroup):
    name: StringProperty()
    filepath: StringProperty(subtype="FILE_PATH")
    include: BoolProperty(name="Import", default=True)
    compatible: BoolProperty(default=True)
    already_imported: BoolProperty(default=False)
    status: StringProperty()
    status_icon: StringProperty(default="INFO")
    track_count: IntProperty()
    key_count: IntProperty()


class BMD_SK_UL_blk_animations(UIList):
    bl_idname = "BMD_SK_UL_blk_animations"

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        row = layout.row(align=True)
        check = row.row()
        check.enabled = item.compatible
        check.prop(item, "include", text="")
        row.label(text=item.name)
        row.label(text=item.status, icon=item.status_icon)


class BMD_SK_BlkImportSettings(PropertyGroup):
    target: PointerProperty(
        name="Target Mesh",
        description="Mesh that will receive the animation and shape keys",
        type=bpy.types.Object,
        poll=bc.mesh_object_poll,
        update=_target_changed,
    )
    filepath: StringProperty(subtype="FILE_PATH")
    bls_filepath: StringProperty(
        name="BLS File",
        description="BLS file used to name and fill the BLK tracks",
        subtype="FILE_PATH",
        update=_bls_path_changed,
    )
    tracks: CollectionProperty(type=BMD_SK_BlkTrackItem)
    active_track: IntProperty(default=0, min=0)
    animations: CollectionProperty(type=BMD_SK_BlkAnimationItem)
    active_animation: IntProperty(default=0, min=0)
    warning: StringProperty()


def _populate_tracks(settings, animation, filepath):
    settings.filepath = filepath
    settings.tracks.clear()
    settings.animations.clear()
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

    item = settings.animations.add()
    item.name = os.path.basename(filepath)
    item.filepath = filepath
    item.include = True

    settings.active_track = 0
    settings.active_animation = 0
    _auto_assign(settings)
    _refresh_animation(settings)


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

    selected = blk_import._selected_mesh(context)
    track_count = max(0, len(animation.tracks) - 1)
    target = blk_import.find_compatible_mesh(context, track_count, selected)

    # Clear the previous session first so the path update below does not
    # re-parse a BLS file for an animation that is about to be replaced.
    settings.tracks.clear()
    settings.animations.clear()

    source = target.get("bmd_bls_source", "") if target is not None else ""
    settings.bls_filepath = str(source) if source else ""
    _BLK_SESSION = animation
    settings.target = target
    _populate_tracks(settings, animation, filepath)

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


class BMD_SK_OT_blk_select_bls(Operator):
    bl_idname = "bmd_shape_keys.blk_select_bls"
    bl_label = "Select BLS File"
    bl_description = "Select the BLS file used to name and fill the BLK tracks"
    bl_options = {"INTERNAL"}

    filter_glob: StringProperty(default="*.bls", options={"HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH")

    def invoke(self, context, _event):
        settings = _get_settings(context)

        if settings is not None and settings.bls_filepath:
            self.filepath = settings.bls_filepath

        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        settings = _get_settings(context)

        if settings is not None:
            settings.bls_filepath = self.filepath

        return {"FINISHED"}


class BMD_SK_OT_blk_settings(Operator):
    bl_idname = "bmd_shape_keys.blk_settings"
    bl_label = "Import BLK"
    bl_description = "Import the BLK animation and fill missing shape keys from BLS"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, _event):
        if _BLK_SESSION is None:
            self.report({"ERROR"}, "No BLK file is loaded")
            return {"CANCELLED"}

        settings = _get_settings(context)

        if settings is None:
            self.report({"ERROR"}, "BLK import settings are unavailable")
            return {"CANCELLED"}

        if not settings.animations:
            _populate_tracks(settings, _BLK_SESSION, settings.filepath)

        if not settings.tracks:
            self.report({"ERROR"}, "The BLK file has no tracks")
            return {"CANCELLED"}

        _refresh_animation(settings)
        return context.window_manager.invoke_props_dialog(
            self, width=620, confirm_text="Import")

    def draw(self, context):
        layout = self.layout
        settings = _get_settings(context)

        if settings is None:
            layout.label(text="BLK import settings are unavailable", icon="ERROR")
            return

        row = layout.row()
        row.alert = settings.target is None
        row.prop(settings, "target")

        if settings.target is None:
            layout.label(text="Select the mesh with shape keys", icon="INFO")

        if not settings.animations:
            layout.label(text="The BLK file has no tracks", icon="ERROR")
            return

        rows = min(max(1, len(settings.animations)), MAX_TRACK_ROWS)
        layout.template_list(
            "BMD_SK_UL_blk_animations", "",
            settings, "animations",
            settings, "active_animation",
            rows=rows,
            maxrows=MAX_TRACK_ROWS,
        )
        row = layout.row(align=True)
        row.prop(settings, "bls_filepath", text="BLS File")
        row.operator("bmd_shape_keys.blk_select_bls", text="", icon="FILEBROWSER")

        if settings.warning:
            layout.label(text=settings.warning, icon="INFO")

        index = min(max(int(settings.active_animation), 0),
                    len(settings.animations) - 1)
        animation = settings.animations[index]
        if not animation.compatible:
            layout.label(text=animation.status, icon="ERROR")

    def execute(self, context):
        global _BLK_SESSION

        settings = _get_settings(context)

        if _BLK_SESSION is None or settings is None or settings.target is None:
            self.report({"ERROR"}, "BLK import data is unavailable")
            return {"CANCELLED"}

        if not settings.animations:
            self.report({"WARNING"}, "No animation to import")
            return {"CANCELLED"}

        animation = settings.animations[0]
        # Keep the user's checkbox choice; _refresh_animation recomputes the
        # status and would otherwise re-enable a row the user turned off.
        include = animation.include
        _refresh_animation(settings)
        animation.include = include and animation.compatible

        if not animation.include:
            if not animation.compatible:
                self.report({"WARNING"},
                            animation.status or "Animation is incompatible")
            elif animation.already_imported:
                self.report({"WARNING"}, animation.status)
            else:
                self.report({"WARNING"},
                            "The animation is not selected for import")
            return {"CANCELLED"}

        target = settings.target
        bls_path = settings.bls_filepath.strip()
        session = None

        if bls_path:
            session, _bls_error = blk_import._bls_session_for(target, bls_path)

        if session is not None:
            file_entry = session.files[0]
            keys = blk_import._animated_keys(file_entry)
            jobs = [
                (key.new_name or key.name, key.record.slots, key.name)
                for key in keys
                if key.status == bls_session.UNIQUE
            ]
            base_keys = [key for key in file_entry.keys if key.is_base]
            base_name = base_keys[0].name if base_keys else None

            if jobs:
                try:
                    bls_import.apply_shape_keys(
                        context, target, jobs,
                        delta_scale=blk_import._delta_scale(target),
                        base_key_name=base_name,
                    )
                except Exception as exc:
                    self.report({"ERROR"}, f"BLS import failed: {exc}")
                    return {"CANCELLED"}

            key_names = [blk_import._key_target_name(key) for key in keys]
        else:
            shape_keys = target.data.shape_keys if target.data else None
            key_names = ([block.name for block in shape_keys.key_blocks[1:]]
                         if shape_keys else [])

        mapping = []
        used = set()

        for index, item in enumerate(settings.tracks):
            if index >= len(key_names):
                break
            name = key_names[index]
            if not name or name in used:
                continue
            used.add(name)
            mapping.append((item.track_index, name))

        if not mapping:
            self.report({"WARNING"}, "No tracks could be assigned to shape keys")
            return {"CANCELLED"}

        try:
            blk_import.apply_blk_animation(
                context, target, _BLK_SESSION, mapping,
                filepath=settings.filepath,
                set_scene_range=True,
                report=self.report,
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Import failed: {exc}")
            return {"CANCELLED"}

        if bls_path and os.path.isfile(bpy.path.abspath(bls_path)):
            target["bmd_bls_source"] = bpy.path.abspath(bls_path)

        _BLK_SESSION = None
        return {"FINISHED"}


CLASSES = (
    BMD_SK_BlkTrackItem,
    BMD_SK_BlkAnimationItem,
    BMD_SK_BlkImportSettings,
    BMD_SK_UL_blk_animations,
    BMD_SK_OT_blk_auto_assign,
    BMD_SK_OT_blk_select_bls,
    BMD_SK_OT_blk_settings,
)
