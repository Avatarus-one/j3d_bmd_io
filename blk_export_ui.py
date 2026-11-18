"""Settings dialog for exporting J3D shape-key animations (``.blk``)."""

from __future__ import annotations

import math
import os

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup, UIList

from . import blender_common as bc
from . import bls_export_ui
from .bck_common import LOOP_ITEMS
from .blk_export import (BlkExportError, _action_frame_range,
                         _action_targets_keys, _has_shape_key_tracks,
                         _shape_key_actions, export_blk)
from .j3d import anim
from .j3d.common import J3DError


BLK_LOOP_ITEMS = [item for item in LOOP_ITEMS if item[0] != "AUTO"]
MAX_TREE_ROWS = 10
_SYNCING = False
_TARGET_MISSING = object()


def _get_blk_settings(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    return getattr(scene, "bmd_blk_export_settings", None)


def _rebuild_tree(settings):
    settings.tree_rows.clear()

    for mesh_index, mesh in enumerate(settings.meshes):
        row = settings.tree_rows.add()
        row.kind = "mesh"
        row.mesh_index = mesh_index

        for action_index, action in enumerate(settings.animations):
            if action.mesh_name != mesh.name:
                continue

            row = settings.tree_rows.add()
            row.kind = "animation"
            row.mesh_index = mesh_index
            row.action_index = action_index


def _populate_animations(settings, context, force=False):
    """Show the target mesh and the one animation that will be exported."""
    global _SYNCING

    if settings is None:
        return

    export_settings = bls_export_ui._get_export_settings(context)
    target = export_settings.target if export_settings is not None else None
    target_name = target.name if target is not None else ""

    if not force and settings.populated_target == target_name:
        return

    previous_mesh = settings.meshes[0] if settings.meshes else None
    same_mesh = previous_mesh is not None and previous_mesh.name == target_name
    previous_include = previous_mesh.include if same_mesh else True
    previous_expanded = previous_mesh.expanded if same_mesh else True
    previous_actions = (
        {item.name: item.include for item in settings.animations}
        if same_mesh else {}
    )

    _SYNCING = True

    try:
        settings.meshes.clear()
        settings.animations.clear()
        settings.tree_rows.clear()
        settings.populated_target = target_name
        settings.selected_mesh = target_name
        settings.selected_action = ""

        if target is None or target.type != "MESH" or target.data is None:
            return

        keys = target.data.shape_keys

        if keys is None or len(keys.key_blocks) < 2:
            return

        actions = _shape_key_actions(keys)

        if not actions:
            return

        action = None
        animation_data = keys.animation_data

        if animation_data is not None and animation_data.action is not None:
            active_name = animation_data.action.name
            action = next(
                (item for item in actions
                 if item.name == active_name), None)

        if action is None:
            action = actions[0]

        start, end = _action_frame_range(action)
        mesh = settings.meshes.add()
        mesh.name = target.name
        mesh.include = previous_include
        mesh.expanded = previous_expanded
        item = settings.animations.add()
        item.mesh_name = target.name
        item.name = action.name
        item.frame_start = start
        item.frame_end = end
        item.include = previous_actions.get(action.name, True)

        _rebuild_tree(settings)
        settings.selected_action = action.name
        settings.active_tree_row = 1 if len(settings.tree_rows) > 1 else 0
    finally:
        _SYNCING = False


def _blk_target_changed(context, target):
    settings = _get_blk_settings(context)

    if settings is not None:
        _populate_animations(settings, context, force=True)


def register_hooks():
    bls_export_ui.add_target_changed_hook(_blk_target_changed)


def unregister_hooks():
    bls_export_ui.remove_target_changed_hook(_blk_target_changed)


def _visible_tree_rows(settings):
    count = 0

    for mesh in settings.meshes:
        count += 1

        if mesh.expanded:
            count += sum(
                1 for action in settings.animations
                if action.mesh_name == mesh.name)

    return count


def _mesh_include_changed(self, context):
    global _SYNCING

    if _SYNCING:
        return

    settings = _get_blk_settings(context)

    if settings is None:
        return

    _SYNCING = True

    try:
        for action in settings.animations:
            if action.mesh_name == self.name:
                action.include = self.include
    finally:
        _SYNCING = False


def _action_include_changed(self, context):
    global _SYNCING

    if _SYNCING:
        return

    settings = _get_blk_settings(context)

    if settings is None:
        return

    _SYNCING = True

    try:
        for mesh in settings.meshes:
            if mesh.name == self.mesh_name:
                mesh.include = any(
                    action.include for action in settings.animations
                    if action.mesh_name == mesh.name)
                break
    finally:
        _SYNCING = False


def _active_row_changed(self, context):
    """Select the mesh / animation under the cursor for the key list."""
    global _SYNCING

    if _SYNCING:
        return

    export_settings = bls_export_ui._get_export_settings(context)

    if export_settings is None:
        return

    index = self.active_tree_row

    if not (0 <= index < len(self.tree_rows)):
        return

    row = self.tree_rows[index]

    if not (0 <= row.mesh_index < len(self.meshes)):
        return

    mesh = self.meshes[row.mesh_index]
    obj = bpy.data.objects.get(mesh.name)

    if obj is None or obj.type != "MESH":
        return

    action_name = ""

    if row.kind == "animation":
        if not (0 <= row.action_index < len(self.animations)):
            return

        action_name = self.animations[row.action_index].name

    target_changed = export_settings.target is not obj

    _SYNCING = True

    try:
        self.selected_mesh = mesh.name
        self.selected_action = action_name

        if target_changed:
            export_settings.target = obj
    finally:
        _SYNCING = False

    if not target_changed:
        bls_export_ui._populate_export_keys(export_settings, obj)


class BMD_SK_BlkMeshItem(PropertyGroup):
    name: StringProperty()
    include: BoolProperty(
        name="Export mesh", default=True,
        update=_mesh_include_changed)
    expanded: BoolProperty(name="Expanded", default=True)


class BMD_SK_BlkActionItem(PropertyGroup):
    mesh_name: StringProperty()
    name: StringProperty()
    frame_start: IntProperty()
    frame_end: IntProperty()
    include: BoolProperty(
        name="Export animation", default=True,
        update=_action_include_changed)


class BMD_SK_BlkTreeRow(PropertyGroup):
    kind: StringProperty()
    mesh_index: IntProperty(default=0)
    action_index: IntProperty(default=-1)


class BMD_SK_BlkExportSettings(PropertyGroup):
    meshes: CollectionProperty(type=BMD_SK_BlkMeshItem)
    animations: CollectionProperty(type=BMD_SK_BlkActionItem)
    tree_rows: CollectionProperty(type=BMD_SK_BlkTreeRow)
    active_tree_row: IntProperty(
        default=0, min=0, update=_active_row_changed)
    selected_mesh: StringProperty()
    selected_action: StringProperty()
    populated_target: StringProperty()


class BMD_SK_UL_blk_export_tree(UIList):
    """Mesh / shape-key animation tree."""

    bl_idname = "BMD_SK_UL_blk_export_tree"

    def filter_items(self, context, data, propname):
        rows = getattr(data, propname)
        flags = []

        for row in rows:
            if row.kind == "mesh":
                flags.append(self.bitflag_filter_item)
                continue

            expanded = True

            if 0 <= row.mesh_index < len(data.meshes):
                expanded = data.meshes[row.mesh_index].expanded

            flags.append(self.bitflag_filter_item if expanded else 0)

        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag=0):
        settings = data

        if item.kind == "mesh":
            mesh = (settings.meshes[item.mesh_index]
                    if 0 <= item.mesh_index < len(settings.meshes) else None)

            if mesh is None:
                return

            count = sum(
                1 for action in settings.animations
                if action.mesh_name == mesh.name)
            row = layout.row(align=True)
            row.alignment = "LEFT"

            check = row.row(align=True)
            check.enabled = count > 0
            check.prop(mesh, "include", text="")

            if count:
                row.prop(
                    mesh, "expanded", text="",
                    icon="TRIA_DOWN" if mesh.expanded else "TRIA_RIGHT",
                    emboss=False)
                row.label(text=mesh.name)
            else:
                row.label(text=mesh.name, icon="MESH_DATA")

            return

        if not (0 <= item.action_index < len(settings.animations)):
            return

        action = settings.animations[item.action_index]
        row = layout.row(align=True)
        row.label(text="", icon="BLANK1")

        check = row.row(align=True)
        check.prop(action, "include", text="")

        split = row.split(factor=0.68, align=True)

        name_col = split.column(align=True)
        name_col.alignment = "LEFT"
        name_col.label(text=action.name, icon="ACTION")

        frames_col = split.column(align=True)
        frames_col.alignment = "RIGHT"
        frames_col.label(text=f"{action.frame_start}..{action.frame_end}")


def open_export_dialog():
    """Timer callback: open the settings dialog after the file browser."""
    try:
        bpy.ops.bmd_shape_keys.blk_export_settings("INVOKE_DEFAULT")
    except Exception as exc:  # pragma: no cover - only in unusual UI states
        print(f"BLK exporter: could not open settings dialog: {exc}")

    return None


def _target_action(context):
    """Return the shape-key action selected for the dialog's target mesh."""
    settings = bls_export_ui._get_export_settings(context)
    target = settings.target if settings is not None else None

    if target is None or target.type != "MESH" or target.data is None:
        return None

    keys = target.data.shape_keys

    if keys is None:
        return None

    blk_settings = _get_blk_settings(context)

    if (blk_settings is not None
            and blk_settings.selected_mesh == target.name
            and blk_settings.selected_action):
        action = bpy.data.actions.get(blk_settings.selected_action)

        if action is not None and _action_targets_keys(action, keys):
            return action

    if keys.animation_data is not None:
        return keys.animation_data.action

    return None


def _target_actions(context):
    """Return the shape-key actions that will be exported.

    Batch export is not implemented yet, so this is the selected action only.
    The tree default below is written against this list so it keeps working
    when several animations can be exported at once.
    """
    action = _target_action(context)
    return [action] if action is not None else []


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


def _sync_action_settings(operator, context):
    """Refresh loop mode and range when the selected animation changes."""
    action = _target_action(context)
    key = action.name if action is not None else None

    if getattr(operator, "_action_cache", _TARGET_MISSING) == key:
        return

    operator._action_cache = key
    operator.loop_mode = _loop_mode_name(action)

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
        _populate_animations(_get_blk_settings(context), context, force=True)

        # One animation is expanded for review; a batch export with several
        # animations starts collapsed so the dialog stays compact.
        settings.expanded = len(_target_actions(context)) <= 1

        self.frame_source = "ACTION"
        self._target_cache = _TARGET_MISSING
        self._action_cache = _TARGET_MISSING
        _sync_target_settings(self, context)
        _sync_action_settings(self, context)

        return context.window_manager.invoke_props_dialog(
            self, width=620, confirm_text="Export")

    def draw(self, context):
        layout = self.layout
        settings = bls_export_ui._get_export_settings(context)

        if settings is None:
            layout.label(text="BLK export settings are unavailable", icon="ERROR")
            return

        _sync_target_settings(self, context)

        blk_settings = _get_blk_settings(context)
        _populate_animations(blk_settings, context)
        _sync_action_settings(self, context)

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

        if blk_settings is not None and blk_settings.tree_rows:
            layout.label(text="Animations", icon="ACTION")
            visible = _visible_tree_rows(blk_settings)
            rows = min(max(1, visible), MAX_TREE_ROWS)
            layout.template_list(
                "BMD_SK_UL_blk_export_tree", "",
                blk_settings, "tree_rows",
                blk_settings, "active_tree_row",
                rows=rows,
                maxrows=MAX_TREE_ROWS,
            )

        if settings.target is None:
            layout.label(text="Select a target mesh", icon="INFO")
            return

        if not settings.keys:
            layout.label(text="No shape keys found", icon="ERROR")
            return

        if not _has_shape_key_tracks(_target_action(context)):
            layout.label(text="No shape-key animation tracks found", icon="ERROR")
            return

        layout.label(text="Shape Keys", icon="SHAPEKEY_DATA")
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

        if not _has_shape_key_tracks(_target_action(context)):
            self.report({"WARNING"}, "No shape-key animation tracks found")
            return {"CANCELLED"}

        custom_range = ((self.custom_start, self.custom_end)
                        if self.frame_source == "CUSTOM" else None)

        try:
            export_blk(context, settings.blk_filepath, names,
                       self.loop_mode, self.frame_source, self.report,
                       target=settings.target, custom_range=custom_range,
                       action=_target_action(context))
        except (BlkExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # e.g. file-system errors
            self.report({"ERROR"}, f"Export failed: {exc}")
            return {"CANCELLED"}

        return {"FINISHED"}


CLASSES = (
    BMD_SK_BlkMeshItem,
    BMD_SK_BlkActionItem,
    BMD_SK_BlkTreeRow,
    BMD_SK_BlkExportSettings,
    BMD_SK_UL_blk_export_tree,
    BMD_SK_OT_blk_export_settings,
)
