"""Unified import UI for J3D animations (``.bck`` and ``.blk``)."""

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
from bpy_extras.io_utils import ImportHelper

from . import animation_registry
from . import bck_import
from . import blender_common as bc
from . import blk_import
from . import bls_import
from . import bls_session
from .j3d import anim, blk


MAX_ANIMATION_ROWS = 12

# Parsed files kept while the settings dialog is open:
# {normalized filepath: (kind, parsed object)}.
_ANIM_SESSION = {}
_SYNCING = False


def _get_settings(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    return getattr(scene, "bmd_anim_import_settings", None)


def _target_poll(_self, obj):
    if obj is None or obj.type not in {"MESH", "ARMATURE"}:
        return False

    if bc.is_bmd_object(obj):
        return True

    arm = bc.target_armature(obj) if obj.type == "MESH" else None
    return arm is not None and bc.is_bmd_object(arm)


def _has_kind(settings, kind):
    return any(item.kind == kind for item in settings.animations)


def _mesh_for_target(context, target):
    if target is None:
        return None

    if target.type == "MESH":
        return target

    if target.type == "ARMATURE":
        return blk_import._mesh_for_armature(context, target)

    return None


def _session_key(filepath):
    return os.path.normcase(os.path.abspath(filepath))


def _session_for(filepath):
    return _ANIM_SESSION.get(_session_key(filepath))


def _target_changed(self, context):
    settings = _get_settings(context)

    if settings is None:
        return

    settings.convert_units = bc.saved_convert_units(context, settings.target)
    mesh = _mesh_for_target(context, settings.target)
    source = mesh.get("bmd_bls_source", "") if mesh is not None else ""
    settings.bls_filepath = str(source) if source else ""
    _refresh(settings)


def _allow_fewer_changed(self, context):
    settings = _get_settings(context)

    if settings is not None:
        _refresh(settings)


def _bls_path_changed(self, context):
    settings = _get_settings(context)

    if settings is not None:
        _refresh(settings)


def _include_changed(self, context):
    """Refresh the warning counts when an animation checkbox changes."""
    if _SYNCING:
        return

    settings = _get_settings(context)

    if settings is not None:
        _refresh(settings)


def _convert_units_changed(self, context):
    """Remember the user's Convert Units choice across dialogs."""
    if _SYNCING:
        return

    bc.remember_convert_units(context, self.convert_units, self.target)


class BMD_ANIM_ImportItem(PropertyGroup):
    name: StringProperty()
    filepath: StringProperty(subtype="FILE_PATH")
    kind: StringProperty()  # "BCK" or "BLK"
    include: BoolProperty(
        name="Import", default=True, update=_include_changed,
        options=set())
    compatible: BoolProperty(default=True)
    already_imported: BoolProperty(default=False)
    status: StringProperty()
    status_icon: StringProperty(default="INFO")
    track_count: IntProperty()
    key_count: IntProperty()
    duration: IntProperty()


class BMD_ANIM_ImportSettings(PropertyGroup):
    target: PointerProperty(
        name="Target",
        description="Mesh or armature that will receive the animations",
        type=bpy.types.Object,
        poll=_target_poll,
        update=_target_changed,
    )
    animations: CollectionProperty(type=BMD_ANIM_ImportItem)
    active_animation: IntProperty(default=0, min=0, options=set())
    convert_units: BoolProperty(
        name="Convert Units",
        description="Convert J3D units to Blender units (scale 0.01)",
        default=True,
        update=_convert_units_changed,
        options=set(),
    )
    allow_fewer_bck: BoolProperty(
        name="BCK: Allow fewer joints",
        description="Import BCK animations that animate only the first joints "
                    "of the armature",
        default=False,
        update=_allow_fewer_changed,
        options=set(),
    )
    bls_filepath: StringProperty(
        name="BLS File",
        description="BLS file used to name and fill the BLK tracks",
        subtype="NONE",
        update=_bls_path_changed,
        options=set(),
    )
    warning: StringProperty()
    mismatch: StringProperty()


class BMD_ANIM_UL_import_animations(UIList):
    bl_idname = "BMD_ANIM_UL_import_animations"

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag):
        row = layout.row(align=True)
        check = row.row()
        check.enabled = item.compatible
        check.prop(item, "include", text="")
        row.label(text=item.name)

        type_col = row.column()
        type_col.ui_units_x = 2.2
        type_col.alignment = "CENTER"
        type_col.label(text=item.kind)

        row.label(text=item.status, icon=item.status_icon)


def _action_source(action):
    """Normalized ``bmd_source`` of *action*, or an empty string."""
    if action is None:
        return ""

    source = action.get("bmd_source")

    if not source:
        return ""

    return os.path.normcase(bpy.path.abspath(str(source)))


def _file_source(filepath):
    return os.path.normcase(os.path.abspath(filepath)) if filepath else ""


def _target_track_actions(owner, fmt, target_name):
    """Actions registered in *owner*'s registry for this format and target."""
    registry = animation_registry.registry_for(owner)

    if registry is None:
        return []

    actions = []

    for entry in registry.animations:
        for track in entry.tracks:
            if track.format != fmt or track.target != target_name:
                continue

            action = bpy.data.actions.get(track.action_name)

            if action is not None:
                actions.append(action)

    return actions


def _already_imported_for_target(item, arm, mesh):
    """True when this file's action already exists on *this* target."""
    source = _file_source(item.filepath)

    if not source:
        return False

    if item.kind == "BCK":
        if arm is None:
            return False

        actions = _target_track_actions(arm, "BCK", arm.name)
        animation_data = arm.animation_data

        if animation_data is not None and animation_data.action is not None:
            actions.append(animation_data.action)
    else:
        if mesh is None:
            return False

        actions = _target_track_actions(mesh, "BLK", mesh.name)
        shape_keys = mesh.data.shape_keys if mesh.data is not None else None
        animation_data = (shape_keys.animation_data
                          if shape_keys is not None else None)

        if animation_data is not None and animation_data.action is not None:
            actions.append(animation_data.action)

    stem = os.path.splitext(os.path.basename(item.filepath))[0].casefold()

    for action in actions:
        if _action_source(action) == source:
            return True

        if (not action.get("bmd_source")
                and action.name.casefold() == stem):
            return True

    return False


def _refresh(settings):
    global _SYNCING

    if settings is None:
        return

    previous = _SYNCING
    _SYNCING = True

    try:
        _refresh_impl(settings)
    finally:
        _SYNCING = previous


def _refresh_impl(settings):
    """Update every animation status for the current target and BLS."""
    context = bpy.context
    target = settings.target
    arm = bck_import._armature_for(target)
    mesh = _mesh_for_target(context, target)
    has_bck = _has_kind(settings, "BCK")
    has_blk = _has_kind(settings, "BLK")
    settings.warning = ""
    settings.mismatch = ""

    joints = bck_import._armature_joint_count(arm) if arm is not None else 0
    bones_ok = arm is not None and bool(arm.data.bones) and joints > 0

    bls_path = settings.bls_filepath.strip()
    bls_session_obj = None
    bls_error = ""
    bls_keys = []
    bls_incompatible = []
    session_parts = []

    if has_blk and mesh is not None and bls_path:
        bls_session_obj, bls_error = blk_import._bls_session_for(mesh, bls_path)

    if has_blk and bls_session_obj is not None:
        file_entry = bls_session_obj.files[0]
        bls_keys = blk_import._animated_keys(file_entry)
        bls_incompatible = [key for key in bls_keys
                            if key.status == bls_session.INCOMPATIBLE]
        importable = [key for key in bls_keys
                      if key.status == bls_session.UNIQUE]
        duplicates = [key for key in bls_keys
                      if key.status == bls_session.DUPLICATE]
        conflicts = [key for key in bls_keys
                     if key.status == bls_session.CONFLICT]

        if importable:
            session_parts.append(f"{len(importable)} missing key(s) will be "
                                 "imported from BLS")

            if duplicates:
                session_parts.append(f"{len(duplicates)} duplicate(s) skipped")

            if conflicts:
                session_parts.append(f"{len(conflicts)} conflict(s) skipped")

    shape_keys = (mesh.data.shape_keys
                  if mesh is not None and mesh.data is not None else None)
    order_key_count = len(list(shape_keys.key_blocks)[1:]) if shape_keys else 0
    key_count = len(bls_keys) if bls_session_obj is not None else order_key_count
    incompatible = 0
    # Keep manual checkbox choices across refreshes, but check animations that
    # just became compatible (for example after enabling "BCK: Allow fewer
    # tracks") and leave incompatible entries unchecked.
    previous = {
        item.filepath: (item.compatible, item.include, item.already_imported)
        for item in settings.animations
    }

    def restore_include(entry):
        was_compatible, was_include, _was_imported = previous.get(
            entry.filepath, (False, True, False))

        if not was_compatible:
            entry.include = True
        else:
            entry.include = was_include

    for item in settings.animations:
        item.compatible = False
        item.include = False
        item.already_imported = False
        item.status = ""
        item.status_icon = "ERROR"

        if item.kind == "BCK":
            if target is None:
                item.status = "No target"
                incompatible += 1
                continue

            if arm is None:
                item.status = "Target has no armature"
                incompatible += 1
                continue

            if not bones_ok:
                item.status = "No J3D joint data"
                incompatible += 1
                continue

            if item.track_count <= 0:
                item.status = "No joints"
                incompatible += 1
                continue

            if item.track_count > joints:
                item.status = (f"Incompatible: {item.track_count} joints vs "
                               f"{joints} bone(s)")
                incompatible += 1
                continue

            if item.track_count < joints and not settings.allow_fewer_bck:
                item.status = (f"Incompatible: {item.track_count} joints vs "
                               f"{joints} bone(s)")
                incompatible += 1
                continue

            item.compatible = True
            restore_include(item)

            if item.track_count < joints:
                item.status = f"OK: {item.track_count} of {joints} joints"
            else:
                item.status = f"OK: {joints} joints"

            item.status_icon = "CHECKMARK"

            if _already_imported_for_target(item, arm, mesh):
                _was_imported = previous.get(
                    item.filepath, (False, True, False))[2]
                item.status = "Already imported"
                item.status_icon = "INFO"
                item.already_imported = True

                if not _was_imported:
                    item.include = False
        else:
            if target is None:
                item.status = "No target"
                incompatible += 1
                continue

            if mesh is None:
                item.status = "No target mesh"
                incompatible += 1
                continue

            item.key_count = key_count

            if item.track_count == 0:
                item.status = "No tracks"
                incompatible += 1
                continue

            if bls_session_obj is not None:
                if item.track_count != key_count:
                    item.status = (f"Incompatible: {item.track_count} tracks "
                                   f"vs {key_count} BLS keys")
                    incompatible += 1
                    continue

                if bls_incompatible:
                    item.status = (f"Incompatible: {len(bls_incompatible)} BLS "
                                   "key(s) do not fit this mesh")
                    incompatible += 1
                    continue

                item.compatible = True
                restore_include(item)
                item.status = f"OK: {item.track_count} tracks"
            else:
                if item.track_count > key_count:
                    item.status = (f"Incompatible: {item.track_count} tracks "
                                   f"vs {key_count} shape keys")
                    incompatible += 1
                    continue

                item.compatible = True
                restore_include(item)

                if item.track_count < key_count:
                    item.status = (f"OK: {item.track_count} of {key_count} "
                                   "tracks, by order")
                else:
                    item.status = f"OK: {item.track_count} tracks, by order"

            item.status_icon = "CHECKMARK"

            if _already_imported_for_target(item, arm, mesh):
                _was_imported = previous.get(
                    item.filepath, (False, True, False))[2]
                item.status = "Already imported"
                item.status_icon = "INFO"
                item.already_imported = True

                if not _was_imported:
                    item.include = False

    included_blk = sum(
        1 for item in settings.animations
        if item.kind == "BLK" and item.compatible and item.include)

    if bls_session_obj is not None:
        settings.warning = "; ".join(session_parts)
    elif not bls_path:
        if included_blk:
            settings.warning = ("No BLS file selected; using shape key order "
                                f"for {included_blk} animation(s)")
    elif bls_error:
        if included_blk:
            settings.warning = (f"{bls_error}; using shape key order for "
                                f"{included_blk} animation(s)")
        else:
            settings.warning = bls_error

    if incompatible:
        settings.mismatch = (f"{incompatible} animation(s) do not match the "
                             "target")


def _populate(settings, parsed):
    global _SYNCING

    previous = _SYNCING
    _SYNCING = True

    try:
        settings.animations.clear()

        for filepath, kind, animation in parsed:
            item = settings.animations.add()
            item.name = os.path.basename(filepath)
            item.filepath = filepath
            item.kind = kind
            item.include = True

            if kind == "BCK":
                item.track_count = len(animation.tracks)
            else:
                item.track_count = max(0, len(animation.tracks) - 1)

            item.duration = int(animation.duration)

        settings.active_animation = 0
    finally:
        _SYNCING = previous


def _is_relevant_target(context, obj, has_bck, has_blk):
    if obj is None or obj.type not in {"MESH", "ARMATURE"}:
        return False

    if has_bck:
        arm = bck_import._armature_for(obj)

        if arm is not None and bck_import._armature_joint_count(arm) > 0:
            return True

    if has_blk:
        mesh = _mesh_for_target(context, obj)

        if (mesh is not None and mesh.data is not None
                and mesh.data.shape_keys is not None):
            return True

    return False


def _choose_target(context, parsed, selected):
    has_bck = any(item[1] == "BCK" for item in parsed)
    has_blk = any(item[1] == "BLK" for item in parsed)

    if _is_relevant_target(context, selected, has_bck, has_blk):
        return selected

    bck_items = [item for item in parsed if item[1] == "BCK"]

    if bck_items:
        joint_count = max(len(item[2].tracks) for item in bck_items)
        target = bck_import.find_compatible_target(context, joint_count)

        if target is not None:
            return target

    blk_items = [item for item in parsed if item[1] == "BLK"]

    if blk_items:
        counts = [max(0, len(item[2].tracks) - 1) for item in blk_items]
        track_count = max(set(counts), key=counts.count)
        return blk_import.find_compatible_mesh(
            context, track_count, blk_import._selected_mesh(context))

    return None


def begin_import(context, paths, operator):
    """Parse BCK/BLK files and open the unified import dialog."""
    global _ANIM_SESSION

    parsed = []

    for filepath in paths:
        extension = os.path.splitext(filepath)[1].lower()

        try:
            if extension == ".bck":
                with open(filepath, "rb") as file:
                    animation = anim.parse_bck(file.read())

                parsed.append((filepath, "BCK", animation))
            elif extension == ".blk":
                with open(filepath, "rb") as file:
                    animation = blk.parse_blk(file.read())

                parsed.append((filepath, "BLK", animation))
            else:
                operator.report(
                    {"WARNING"},
                    f"Unsupported animation file: {os.path.basename(filepath)}")
        except Exception as exc:
            operator.report(
                {"ERROR"},
                f"Could not read {os.path.basename(filepath)}: {exc}")

    if not parsed:
        operator.report({"ERROR"}, "No BCK or BLK animations could be read")
        return {"CANCELLED"}

    parsed.sort(key=lambda item: os.path.basename(item[0]).casefold())

    settings = _get_settings(context)

    if settings is None:
        operator.report({"ERROR"}, "Animation import settings are unavailable")
        return {"CANCELLED"}

    _ANIM_SESSION = {_session_key(filepath): (kind, animation)
                     for filepath, kind, animation in parsed}

    selected = context.active_object
    target = _choose_target(context, parsed, selected)
    settings.target = target
    settings.convert_units = bc.saved_convert_units(context, target)
    mesh = _mesh_for_target(context, target)
    source = mesh.get("bmd_bls_source", "") if mesh is not None else ""
    settings.bls_filepath = str(source) if source else ""

    _populate(settings, parsed)
    _refresh(settings)

    bpy.app.timers.register(_open_settings_dialog, first_interval=0.05)
    return {"FINISHED"}


def _open_settings_dialog():
    try:
        bpy.ops.import_anim.j3d_settings("INVOKE_DEFAULT")
    except Exception as exc:  # pragma: no cover - only in unusual UI states
        print(f"J3D animation importer: could not open dialog: {exc}")

    return None


class BMD_ANIM_OT_import(Operator, ImportHelper):
    """Import J3D joint and/or shape-key animations (.bck / .blk)"""
    bl_idname = "import_anim.j3d"
    bl_label = "Import J3D Animations"
    bl_options = {"UNDO"}

    filename_ext = ".bck"
    filter_glob: StringProperty(default="*.bck;*.blk", options={"HIDDEN"})
    files: CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})
    filepath: StringProperty(subtype="FILE_PATH",
                             options={"SKIP_SAVE", "HIDDEN"})

    def invoke(self, context, event):
        if self.filepath and not self.files:
            return self.execute(context)

        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        paths = []

        if self.files:
            directory = self.directory or os.path.dirname(self.filepath)

            for element in self.files:
                if element.name:
                    paths.append(os.path.join(directory, element.name))
        elif self.filepath:
            paths.append(self.filepath)

        if not paths:
            self.report({"ERROR"}, "No animation files selected")
            return {"CANCELLED"}

        return begin_import(context, paths, self)


class BMD_ANIM_OT_select_bls(Operator):
    bl_idname = "import_anim.j3d_select_bls"
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


class BMD_ANIM_OT_settings(Operator):
    bl_idname = "import_anim.j3d_settings"
    bl_label = "Import J3D Animations"
    bl_description = "Import the selected BCK and BLK animations"
    bl_options = {"UNDO"}

    def invoke(self, context, _event):
        if not _ANIM_SESSION:
            self.report({"ERROR"}, "No animation files are loaded")
            return {"CANCELLED"}

        settings = _get_settings(context)

        if settings is None:
            self.report({"ERROR"}, "Animation import settings are unavailable")
            return {"CANCELLED"}

        _refresh(settings)

        return context.window_manager.invoke_props_dialog(
            self, width=720, confirm_text="Import")

    def draw(self, context):
        layout = self.layout
        settings = _get_settings(context)

        if settings is None:
            layout.label(text="Animation import settings are unavailable",
                         icon="ERROR")
            return

        row = layout.row()
        row.alert = settings.target is None
        row.prop(settings, "target")

        if settings.target is None:
            layout.label(
                text="Select the mesh or armature that should receive the "
                     "animations",
                icon="INFO")

        if _has_kind(settings, "BCK"):
            layout.prop(settings, "allow_fewer_bck")
            layout.prop(settings, "convert_units")

        if not settings.animations:
            layout.label(text="No animations to import", icon="ERROR")
            return

        rows = min(max(1, len(settings.animations)), MAX_ANIMATION_ROWS)
        layout.template_list(
            "BMD_ANIM_UL_import_animations", "",
            settings, "animations",
            settings, "active_animation",
            rows=rows,
            maxrows=MAX_ANIMATION_ROWS,
        )

        if _has_kind(settings, "BLK"):
            row = layout.row(align=True)
            row.prop(settings, "bls_filepath", text="BLS File")
            row.operator("import_anim.j3d_select_bls", text="",
                         icon="FILEBROWSER")

        if settings.warning:
            layout.label(text=settings.warning, icon="INFO")

        if settings.mismatch:
            layout.label(text=settings.mismatch, icon="INFO")

    def execute(self, context):
        settings = _get_settings(context)

        if not _ANIM_SESSION or settings is None or settings.target is None:
            self.report({"ERROR"}, "Animation import data is unavailable")
            return {"CANCELLED"}

        _refresh(settings)

        selected = [item for item in settings.animations
                    if item.include and item.compatible]

        if not selected:
            self.report({"WARNING"}, "No animations selected for import")
            return {"CANCELLED"}

        arm = bck_import._armature_for(settings.target)
        mesh = _mesh_for_target(context, settings.target)
        scale = 0.01 if settings.convert_units else 1.0
        imported = 0
        last_entry = None
        last_registry = None
        last_action = None
        registries = {}

        blk_selected = [item for item in selected if item.kind == "BLK"]
        key_names = []

        if blk_selected:
            if mesh is None:
                self.report({"ERROR"}, "The target has no mesh")
                return {"CANCELLED"}

            bls_path = settings.bls_filepath.strip()
            session = None

            if bls_path:
                session, _bls_error = blk_import._bls_session_for(mesh,
                                                                  bls_path)

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
                            context, mesh, jobs,
                            delta_scale=blk_import._delta_scale(mesh),
                            base_key_name=base_name,
                        )
                    except Exception as exc:
                        self.report({"ERROR"}, f"BLS import failed: {exc}")
                        return {"CANCELLED"}

                key_names = [blk_import._key_target_name(key)
                             for key in keys]
            else:
                shape_keys = (mesh.data.shape_keys
                              if mesh.data is not None else None)
                key_names = ([block.name
                              for block in shape_keys.key_blocks[1:]]
                             if shape_keys else [])

        for item in selected:
            if item.kind != "BCK":
                continue

            if arm is None:
                self.report({"ERROR"},
                            f"{item.name}: the target has no armature")
                continue

            session_item = _session_for(item.filepath)
            parsed = (session_item[1]
                      if session_item is not None and session_item[0] == "BCK"
                      else None)

            try:
                action = bck_import.import_bck(
                    context, item.filepath, scale=scale,
                    set_scene_range=False, report=self.report,
                    target=arm, bck=parsed)
            except Exception as exc:
                self.report({"ERROR"}, f"{item.name}: {exc}")
                continue

            if action is not None:
                last_action = action
                base = os.path.splitext(os.path.basename(item.filepath))[0]
                frame_end = int(action.get("bmd_duration", 0))
                entry = animation_registry.register_armature_action(
                    arm, action, base, 0, frame_end,
                    animation_registry.loop_mode_name(action),
                    select=False, sort=False)

                if entry is not None:
                    registry = animation_registry.registry_for(arm)
                    last_entry = entry
                    last_registry = registry

                    if registry is not None:
                        registries[registry.as_pointer()] = registry

            imported += 1

        for item in selected:
            if item.kind != "BLK":
                continue

            if mesh is None:
                self.report({"ERROR"},
                            f"{item.name}: the target has no mesh")
                continue

            session_item = _session_for(item.filepath)

            if session_item is None:
                continue

            _kind, animation = session_item
            mapping = []
            used = set()

            for index in range(1, len(animation.tracks)):
                key_index = index - 1

                if key_index >= len(key_names):
                    break

                name = key_names[key_index]

                if not name or name in used:
                    continue

                used.add(name)
                mapping.append((index, name))

            if not mapping:
                self.report(
                    {"WARNING"},
                    f"{item.name}: no tracks could be assigned to shape keys")
                continue

            try:
                action = blk_import.apply_blk_animation(
                    context, mesh, animation, mapping,
                    filepath=item.filepath,
                    set_scene_range=False,
                    report=self.report,
                )
            except Exception as exc:
                self.report({"ERROR"}, f"{item.name}: {exc}")
                continue

            if action is not None:
                last_action = action
                base = os.path.splitext(os.path.basename(item.filepath))[0]
                frame_end = int(animation.duration)
                entry = animation_registry.register_shape_action(
                    mesh, action, base, 0, frame_end,
                    animation_registry.loop_mode_name(action),
                    select=False, sort=False)

                if entry is not None:
                    registry = animation_registry.registry_for(mesh)
                    last_entry = entry
                    last_registry = registry

                    if registry is not None:
                        registries[registry.as_pointer()] = registry

            imported += 1

        for registry in registries.values():
            animation_registry.sort_animations(registry)

        if last_entry is not None and last_registry is not None:
            animation_registry._select_entry(last_registry, last_entry)
        elif last_action is not None:
            context.scene.frame_start = 0
            context.scene.frame_end = int(last_action.get("bmd_duration", 0))

            try:
                context.scene.frame_set(0)
            except Exception:
                pass

        if (blk_selected and settings.bls_filepath.strip()
                and os.path.isfile(bpy.path.abspath(
                    settings.bls_filepath.strip()))):
            mesh["bmd_bls_source"] = bpy.path.abspath(
                settings.bls_filepath.strip())

        _ANIM_SESSION.clear()

        if not imported:
            self.report({"ERROR"}, "No animations were imported")
            return {"CANCELLED"}

        bc.remember_convert_units(
            context, settings.convert_units, settings.target)
        return {"FINISHED"}


def register_properties():
    bpy.types.Scene.bmd_anim_import_settings = PointerProperty(
        type=BMD_ANIM_ImportSettings)


def unregister_properties():
    if hasattr(bpy.types.Scene, "bmd_anim_import_settings"):
        del bpy.types.Scene.bmd_anim_import_settings


CLASSES = (
    BMD_ANIM_ImportItem,
    BMD_ANIM_ImportSettings,
    BMD_ANIM_UL_import_animations,
    BMD_ANIM_OT_import,
    BMD_ANIM_OT_select_bls,
    BMD_ANIM_OT_settings,
)
