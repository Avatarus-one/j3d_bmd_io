"""Settings dialog for exporting J3D joint animations (``.bck``)."""

from __future__ import annotations

import json
import math
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
from . import bck_common
from . import bck_export
from .j3d import anim
from .j3d.common import J3DError


_TARGET_MISSING = object()
MAX_TREE_ROWS = 10
_SYNCING = False
_ACTION_SELECTION_PROP = "bmd_bck_export_actions"


def _get_settings(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    return getattr(scene, "bmd_bck_export_settings", None)


def _armature(obj):
    if obj is None:
        return None
    if obj.type == "ARMATURE":
        return obj
    if obj.type == "MESH":
        return bc.target_armature(obj)
    return None


def _target_poll(_self, obj):
    return obj is not None and obj.type == "ARMATURE"


def _action(settings):
    arm = _armature(settings.target) if settings is not None else None
    if arm is None or arm.animation_data is None:
        return None
    return arm.animation_data.action


def _loop_mode(action):
    if action is None:
        return "ONCE"
    try:
        mode = int(action.get("bmd_loop_mode", anim.LOOP_ONCE))
    except (TypeError, ValueError):
        return "ONCE"
    if 0 <= mode < len(anim.LOOP_NAMES):
        return anim.LOOP_NAMES[mode]
    return "ONCE"


def _sync_range(operator, context, settings):
    if operator.frame_source == "SCENE":
        operator.custom_start = context.scene.frame_start
        operator.custom_end = context.scene.frame_end
    elif operator.frame_source == "ACTION":
        action = _action(settings)
        if action is None:
            operator.custom_start = context.scene.frame_start
            operator.custom_end = context.scene.frame_end
        else:
            start, end = action.frame_range
            operator.custom_start = int(math.floor(start))
            operator.custom_end = int(math.ceil(end))


def _sync_target_settings(operator, context, settings):
    target = settings.target if settings is not None else None

    if getattr(operator, "_target_cache", _TARGET_MISSING) is target:
        return

    operator._target_cache = target
    operator.convert_units = bc.saved_convert_units(context, target)
    operator.loop_mode = _loop_mode(_action(settings))

    if operator.frame_source != "CUSTOM":
        _sync_range(operator, context, settings)


def _frame_source_changed(self, context):
    settings = _get_settings(context)

    if settings is not None:
        _sync_range(self, context, settings)


def open_export_dialog():
    """Timer callback: open the settings dialog after the file browser."""
    try:
        bpy.ops.export_anim.bck_settings("INVOKE_DEFAULT")
    except Exception as exc:  # pragma: no cover - only in unusual UI states
        print(f"BCK exporter: could not open settings dialog: {exc}")

    return None


def _stored_action_selection(arm):
    """Return the per-armature action selection stored on *arm*."""
    if arm is None:
        return {}

    raw = arm.get(_ACTION_SELECTION_PROP)

    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}

    if not isinstance(data, dict):
        return {}

    return {str(name): bool(value) for name, value in data.items()}


def _store_action_selection(settings, arm=None):
    """Store the current action selection on *arm* (per armature)."""
    arm = arm if arm is not None else _armature(settings.target)

    if arm is None:
        return

    data = {item.name: bool(item.include) for item in settings.actions}

    try:
        arm[_ACTION_SELECTION_PROP] = json.dumps(data, separators=(",", ":"))
    except Exception:
        pass


def _armature_include_changed(self, context):
    global _SYNCING

    if _SYNCING:
        return

    settings = _get_settings(context)

    if settings is None:
        return

    _SYNCING = True

    try:
        for action_item in settings.actions:
            action_item.include = self.armature_include
    finally:
        _SYNCING = False

    _store_action_selection(settings)


def _action_include_changed(self, context):
    global _SYNCING

    if _SYNCING:
        return

    settings = _get_settings(context)

    if settings is None:
        return

    _SYNCING = True

    try:
        settings.armature_include = any(
            action_item.include for action_item in settings.actions)
    finally:
        _SYNCING = False

    _store_action_selection(settings)


def _action_frame_range(action):
    if action is None:
        return 0, 0

    start, end = action.frame_range
    start, end = int(math.floor(start)), int(math.ceil(end))

    if start == end and "bmd_duration" in action:
        start = min(start, 0)
        end = max(end, int(action["bmd_duration"]))

    return start, end


def _rebuild_tree(settings):
    settings.tree_rows.clear()

    root = settings.tree_rows.add()
    root.kind = "armature"

    for index, _item in enumerate(settings.actions):
        row = settings.tree_rows.add()
        row.kind = "action"
        row.action_index = index

    settings.active_tree_row = 0


def _populate_actions(settings, arm):
    global _SYNCING

    old_arm = None
    old_name = settings.populated_armature

    if old_name:
        old_arm = bpy.data.objects.get(old_name)

        if old_arm is not None and old_arm is not arm:
            _store_action_selection(settings, old_arm)

    if arm is None:
        _SYNCING = True

        try:
            settings.actions.clear()
            settings.tree_rows.clear()
            settings.armature_include = True
            settings.populated_armature = ""
        finally:
            _SYNCING = False

        return

    action = arm.animation_data.action if arm.animation_data else None
    items = []

    if action is not None:
        start, end = _action_frame_range(action)
        items.append((action.name, start, end))

    current = [
        (item.name, item.frame_start, item.frame_end)
        for item in settings.actions
    ]

    if settings.populated_armature != arm.name or current != items:
        stored = _stored_action_selection(arm)
        _SYNCING = True

        try:
            settings.actions.clear()

            for name, start, end in items:
                item = settings.actions.add()
                item.name = name
                item.frame_start = start
                item.frame_end = end
                item.include = stored.get(name, True)

            settings.armature_include = any(
                item.include for item in settings.actions)
            settings.populated_armature = arm.name
        finally:
            _SYNCING = False

        _store_action_selection(settings, arm)

    _rebuild_tree(settings)


def _visible_tree_rows(settings):
    return 1 + (len(settings.actions) if settings.expanded else 0)


def _target_changed(self, context):
    settings = _get_settings(context)

    if settings is not None:
        _populate_actions(settings, _armature(settings.target))


class BMD_ANIM_BCKActionItem(PropertyGroup):
    name: StringProperty()
    frame_start: IntProperty()
    frame_end: IntProperty()
    include: BoolProperty(
        name="Export animation", default=True,
        update=_action_include_changed)


class BMD_ANIM_BCKTreeRow(PropertyGroup):
    kind: StringProperty()
    action_index: IntProperty()


class BMD_ANIM_UL_bck_export_tree(UIList):
    bl_idname = "BMD_ANIM_UL_bck_export_tree"

    def filter_items(self, context, data, propname):
        rows = getattr(data, propname)
        flags = []

        for row in rows:
            if row.kind == "armature":
                flags.append(self.bitflag_filter_item)
            else:
                flags.append(
                    self.bitflag_filter_item if data.expanded else 0)

        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag=0):
        settings = data

        if item.kind == "armature":
            arm = _armature(settings.target)
            row = layout.row(align=True)
            row.alignment = "LEFT"

            check = row.row(align=True)
            check.enabled = arm is not None and bool(settings.actions)
            check.prop(settings, "armature_include", text="")

            row.prop(
                settings, "expanded", text="",
                icon="TRIA_DOWN" if settings.expanded else "TRIA_RIGHT",
                emboss=False)
            row.label(
                text=arm.name if arm is not None else "No armature")
            return

        if 0 <= item.action_index < len(settings.actions):
            action_item = settings.actions[item.action_index]
            row = layout.row(align=True)
            row.label(text="", icon="BLANK1")

            check = row.row(align=True)
            check.prop(action_item, "include", text="")

            split = row.split(factor=0.68, align=True)

            name_col = split.column(align=True)
            name_col.alignment = "LEFT"
            name_col.label(text=action_item.name, icon="ACTION")

            frames_col = split.column(align=True)
            frames_col.alignment = "RIGHT"
            frames_col.label(
                text=f"{action_item.frame_start}..{action_item.frame_end}")


class BMD_ANIM_BCKExportSettings(PropertyGroup):
    filepath: StringProperty(subtype="FILE_PATH")
    target: PointerProperty(
        name="Target Armature",
        description="Armature whose active action will be exported",
        type=bpy.types.Object,
        poll=_target_poll,
        update=_target_changed,
    )
    actions: CollectionProperty(type=BMD_ANIM_BCKActionItem)
    tree_rows: CollectionProperty(type=BMD_ANIM_BCKTreeRow)
    active_tree_row: IntProperty(default=0, min=0)
    expanded: BoolProperty(name="Expanded", default=True)
    armature_include: BoolProperty(
        name="Export armature", default=True,
        update=_armature_include_changed)
    populated_armature: StringProperty()


class BMD_ANIM_OT_bck_export_settings(Operator):
    bl_idname = "export_anim.bck_settings"
    bl_label = "Export BCK"
    bl_description = "Choose the BCK export options"
    bl_options = {"REGISTER", "UNDO"}

    convert_units: BoolProperty(
        name="Convert Units", default=True,
        description="Convert Blender units to J3D units (scale 100)")
    loop_mode: EnumProperty(
        name="Loop Mode",
        items=[item for item in bck_common.LOOP_ITEMS if item[0] != "AUTO"],
        default="ONCE")
    frame_source: EnumProperty(
        name="Frame Range",
        items=[("ACTION", "Action Range",
                "Frame range of the keyframes in the action"),
               ("SCENE", "Scene Range", "Scene start / end frame"),
               ("CUSTOM", "Custom Range",
                "Use the Start Frame and End Frame fields")],
        default="ACTION",
        update=_frame_source_changed)
    custom_start: IntProperty(name="Start Frame", default=0)
    custom_end: IntProperty(name="End Frame", default=0)

    def invoke(self, context, _event):
        settings = _get_settings(context)

        if settings is None or not settings.filepath:
            self.report({"ERROR"}, "Select an export file first")
            return {"CANCELLED"}

        if settings.target is None:
            try:
                settings.target = bck_common.find_armature(context)
            except bck_common.AnimError:
                pass

        self.frame_source = "ACTION"
        self._target_cache = _TARGET_MISSING
        _populate_actions(settings, _armature(settings.target))
        _sync_target_settings(self, context, settings)

        return context.window_manager.invoke_props_dialog(
            self, width=620, confirm_text="Export")

    def draw(self, context):
        layout = self.layout
        settings = _get_settings(context)

        if settings is None:
            layout.label(text="BCK export settings are unavailable", icon="ERROR")
            return

        _sync_target_settings(self, context, settings)

        box = layout.box()
        box.label(text=f"File: {os.path.basename(settings.filepath)}", icon="FILE")
        row = box.row()
        row.alert = settings.target is None
        row.prop(settings, "target")
        box.prop(self, "convert_units")
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

        arm = _armature(settings.target)
        action = _action(settings)

        if arm is None:
            layout.label(text="Select the armature with an action", icon="INFO")
            return

        if action is None:
            layout.label(text="The armature has no active action to export",
                         icon="ERROR")
            return

        if not settings.actions:
            layout.label(text="No animations to export", icon="ERROR")
            return

        rows = min(max(1, _visible_tree_rows(settings)), MAX_TREE_ROWS)
        layout.template_list(
            "BMD_ANIM_UL_bck_export_tree", "",
            settings, "tree_rows",
            settings, "active_tree_row",
            rows=rows,
            maxrows=MAX_TREE_ROWS,
        )

        if not any(item.include for item in settings.actions):
            layout.label(text="Select at least one animation", icon="ERROR")

    def execute(self, context):
        settings = _get_settings(context)

        if settings is None or not settings.filepath:
            self.report({"ERROR"}, "BCK export settings are incomplete")
            return {"CANCELLED"}

        arm = _armature(settings.target)
        action = _action(settings)

        if arm is None:
            self.report({"ERROR"}, "Select the armature with an action")
            return {"CANCELLED"}

        if action is None:
            self.report({"ERROR"}, "The armature has no active action to export")
            return {"CANCELLED"}

        selected = [item for item in settings.actions if item.include]

        if not selected:
            self.report({"ERROR"}, "Select at least one animation")
            return {"CANCELLED"}

        scale = 100.0 if self.convert_units else 1.0
        custom_range = ((self.custom_start, self.custom_end)
                        if self.frame_source == "CUSTOM" else None)

        try:
            bck_export.export_bck(
                context, settings.filepath, scale, self.loop_mode,
                self.frame_source, self.report,
                custom_range=custom_range, target=arm)
        except (bck_common.AnimError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except Exception as exc:  # file-system errors, etc.
            self.report({"ERROR"}, f"Export failed: {exc}")
            return {"CANCELLED"}

        return {"FINISHED"}


CLASSES = (
    BMD_ANIM_BCKActionItem,
    BMD_ANIM_BCKTreeRow,
    BMD_ANIM_BCKExportSettings,
    BMD_ANIM_UL_bck_export_tree,
    BMD_ANIM_OT_bck_export_settings,
)
