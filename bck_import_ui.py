"""Blender-side UI for importing J3D joint animations (``.bck``)."""

from __future__ import annotations

import os

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup, UIList

from . import blender_common as bc
from . import bck_import
from .j3d import anim


MAX_ANIMATION_ROWS = 10

# Parsed animations kept while the settings dialog is open.
_BCK_SESSION = []


def _get_settings(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    return getattr(scene, "bmd_bck_import_settings", None)


def _target_poll(_self, obj):
    return bck_import._armature_for(obj) is not None


def _is_bone_action(action):
    return any("pose.bones[" in curve.data_path
               for curve in action.fcurves)


def _action_lookup():
    """Return ``({source: action}, {name: action})`` for existing BCK actions.

    Built once per status refresh; a batch import can reuse it for every file.
    Actions with a ``bmd_source`` are matched by path, older/source-less actions
    by name (only bone actions, so a shape-key action cannot match).
    """
    by_source = {}
    by_name = {}

    for action in bpy.data.actions:
        source = action.get("bmd_source")
        if source:
            by_source.setdefault(source, action)
        elif _is_bone_action(action):
            by_name.setdefault(action.name, action)

    return by_source, by_name


def _existing_action(filepath, lookup=None):
    if not filepath:
        return None

    name = os.path.splitext(os.path.basename(filepath))[0]
    source = os.path.abspath(filepath)
    by_source, by_name = lookup if lookup is not None else _action_lookup()

    return by_source.get(source) or by_name.get(name)


class BMD_ANIM_BckAnimationItem(PropertyGroup):
    name: StringProperty()
    filepath: StringProperty(subtype="FILE_PATH")
    include: BoolProperty(name="Import", default=True)
    compatible: BoolProperty(default=True)
    already_imported: BoolProperty(default=False)
    status: StringProperty()
    status_icon: StringProperty(default="INFO")
    duration: IntProperty()
    joint_count: IntProperty()


class BMD_ANIM_UL_bck_animations(UIList):
    bl_idname = "BMD_ANIM_UL_bck_animations"

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        row = layout.row(align=True)
        check = row.row()
        check.enabled = item.compatible
        check.prop(item, "include", text="")
        row.label(text=item.name)
        row.label(text=item.status, icon=item.status_icon)


def _target_changed(self, context):
    settings = _get_settings(context)

    if settings is None:
        return

    settings.convert_units = bc.saved_convert_units(context, settings.target)
    _refresh_animations(settings)


class BMD_ANIM_BckImportSettings(PropertyGroup):
    target: PointerProperty(
        name="Target Mesh",
        description="Mesh or armature that will receive the animation",
        type=bpy.types.Object,
        poll=_target_poll,
        update=_target_changed,
    )
    animations: CollectionProperty(type=BMD_ANIM_BckAnimationItem)
    active_animation: IntProperty(default=0, min=0)
    convert_units: BoolProperty(
        name="Convert Units",
        description="Convert J3D units to Blender units (scale 0.01)",
        default=True,
    )
    warning: StringProperty()


def _refresh_animations(settings):
    """Update every animation status for the current target."""
    arm = bck_import._armature_for(settings.target)
    settings.warning = ""

    if settings.target is None:
        for item in settings.animations:
            item.compatible = False
            item.include = False
            item.already_imported = False
            item.status = "No target"
            item.status_icon = "ERROR"
        return

    if arm is None:
        for item in settings.animations:
            item.compatible = False
            item.include = False
            item.already_imported = False
            item.status = "Target has no armature"
            item.status_icon = "ERROR"
        return

    if not arm.data.bones:
        for item in settings.animations:
            item.compatible = False
            item.include = False
            item.already_imported = False
            item.status = "No bones"
            item.status_icon = "ERROR"
        return

    joints = bck_import._armature_joint_count(arm)

    if joints == 0:
        for item in settings.animations:
            item.compatible = False
            item.include = False
            item.already_imported = False
            item.status = "No J3D joint data"
            item.status_icon = "ERROR"
        return

    incompatible = 0
    lookup = _action_lookup()

    for item in settings.animations:
        item.already_imported = False

        if item.joint_count <= 0:
            item.compatible = False
            item.include = False
            item.status = "No joints"
            item.status_icon = "ERROR"
            incompatible += 1
            continue

        if item.joint_count > joints:
            item.compatible = False
            item.include = False
            item.status = (
                f"Incompatible: {item.joint_count} joints vs {joints} bone(s)")
            item.status_icon = "ERROR"
            incompatible += 1
            continue

        item.compatible = True
        item.include = True
        if item.joint_count < joints:
            item.status = f"OK ({item.joint_count} of {joints} joints)"
        else:
            item.status = f"OK ({joints} joints)"
        item.status_icon = "CHECKMARK"

        existing = _existing_action(item.filepath, lookup)
        if existing is not None:
            item.status = f"Already imported: {existing.name}"
            item.status_icon = "INFO"
            item.already_imported = True
            item.include = False

    if incompatible:
        settings.warning = (
            f"{incompatible} animation(s) do not match the target armature")


def _populate_settings(settings, parsed):
    settings.animations.clear()

    for filepath, bck in parsed:
        item = settings.animations.add()
        item.name = os.path.basename(filepath)
        item.filepath = filepath
        item.include = True
        item.duration = int(bck.duration)
        item.joint_count = len(bck.tracks)

    settings.active_animation = 0


def begin_import(context, paths, operator):
    """Parse BCK files and open the import dialog."""
    global _BCK_SESSION

    parsed = []

    for filepath in paths:
        try:
            with open(filepath, "rb") as file:
                bck = anim.parse_bck(file.read())
        except Exception as exc:
            operator.report(
                {"ERROR"}, f"Could not read {os.path.basename(filepath)}: {exc}")
            continue
        parsed.append((filepath, bck))

    if not parsed:
        operator.report({"ERROR"}, "No BCK animations could be read")
        return {"CANCELLED"}

    settings = _get_settings(context)

    if settings is None:
        operator.report({"ERROR"}, "BCK import settings are unavailable")
        return {"CANCELLED"}

    _BCK_SESSION = parsed
    _populate_settings(settings, parsed)

    joint_count = len(parsed[0][1].tracks)
    settings.target = bck_import.find_compatible_target(context, joint_count)
    settings.convert_units = bc.saved_convert_units(context, settings.target)
    _refresh_animations(settings)

    bpy.app.timers.register(_open_settings_dialog, first_interval=0.05)
    return {"FINISHED"}


def _open_settings_dialog():
    try:
        bpy.ops.import_anim.bck_settings("INVOKE_DEFAULT")
    except Exception as exc:  # pragma: no cover - only in unusual UI states
        print(f"BCK importer: could not open settings dialog: {exc}")

    return None


class BMD_ANIM_OT_bck_settings(Operator):
    bl_idname = "import_anim.bck_settings"
    bl_label = "Import BCK"
    bl_description = "Import the BCK animations onto the selected armature"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, _event):
        if not _BCK_SESSION:
            self.report({"ERROR"}, "No BCK files are loaded")
            return {"CANCELLED"}

        settings = _get_settings(context)

        if settings is None:
            self.report({"ERROR"}, "BCK import settings are unavailable")
            return {"CANCELLED"}

        _refresh_animations(settings)
        return context.window_manager.invoke_props_dialog(
            self, width=620, confirm_text="Import")

    def draw(self, context):
        layout = self.layout
        settings = _get_settings(context)

        if settings is None:
            layout.label(text="BCK import settings are unavailable", icon="ERROR")
            return

        row = layout.row()
        row.alert = settings.target is None
        row.prop(settings, "target")

        if settings.target is None:
            layout.label(
                text="Select a mesh or armature with J3D joint data", icon="INFO")

        layout.prop(settings, "convert_units")

        if not settings.animations:
            layout.label(text="No BCK animations to import", icon="ERROR")
            return

        rows = min(max(1, len(settings.animations)), MAX_ANIMATION_ROWS)
        layout.template_list(
            "BMD_ANIM_UL_bck_animations", "",
            settings, "animations",
            settings, "active_animation",
            rows=rows,
            maxrows=MAX_ANIMATION_ROWS,
        )

        if settings.warning:
            layout.label(text=settings.warning, icon="INFO")

        index = min(max(int(settings.active_animation), 0),
                    len(settings.animations) - 1)
        animation = settings.animations[index]
        if not animation.compatible:
            layout.label(text=animation.status, icon="ERROR")

    def execute(self, context):
        global _BCK_SESSION

        settings = _get_settings(context)

        if not _BCK_SESSION or settings is None or settings.target is None:
            self.report({"ERROR"}, "BCK import data is unavailable")
            return {"CANCELLED"}

        arm = bck_import._armature_for(settings.target)

        if arm is None:
            self.report({"ERROR"}, "The target has no armature")
            return {"CANCELLED"}

        previous = {item.filepath: item.include for item in settings.animations}
        _refresh_animations(settings)

        # Keep the user's checkbox choices; refresh only clears incompatible
        # rows and re-checks rows that were never touched.
        for item in settings.animations:
            if item.compatible:
                item.include = previous.get(item.filepath, item.include)

        selected = [item for item in settings.animations
                    if item.include and item.compatible]

        if not selected:
            self.report({"WARNING"}, "No animations selected for import")
            return {"CANCELLED"}

        scale = 0.01 if settings.convert_units else 1.0
        imported = 0

        for item in selected:
            try:
                bck_import.import_bck(
                    context, item.filepath, scale=scale,
                    set_scene_range=True, report=self.report,
                    set_fps=True, target=arm)
            except Exception as exc:
                self.report(
                    {"ERROR"}, f"{item.name}: {exc}")
                continue
            imported += 1

        _BCK_SESSION = []

        if not imported:
            self.report({"ERROR"}, "No animations were imported")
            return {"CANCELLED"}

        return {"FINISHED"}


CLASSES = (
    BMD_ANIM_BckAnimationItem,
    BMD_ANIM_BckImportSettings,
    BMD_ANIM_UL_bck_animations,
    BMD_ANIM_OT_bck_settings,
)
