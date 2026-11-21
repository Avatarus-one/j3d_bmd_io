"""Unified export dialog for J3D animations (``.bck`` and ``.blk``).

The dialog lists every mesh of the scene with the animations stored in its
:mod:`animation_registry` registry.  BCK and BLK can be exported in one batch,
optionally into ``bck`` / ``blk`` sub-folders of an ``.arc`` hierarchy.
"""

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

from . import animation_registry as registry_data
from . import bck_export
from . import blk_export
from . import blender_common as bc
from . import bls_export_ui


FORMAT_BCK = registry_data.FORMAT_BCK
FORMAT_BLK = registry_data.FORMAT_BLK
MAX_TREE_ROWS = 12
MAX_KEY_ROWS = 8
_SYNCING = False


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def _get_settings(context=None):
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    return getattr(scene, "bmd_anim_export_settings", None)


def _mesh(name):
    obj = bpy.data.objects.get(name) if name else None

    if obj is None or obj.type != "MESH":
        return None

    return obj


def _entry(mesh_obj, name):
    return registry_data.find_animation(
        registry_data.registry_for(mesh_obj), name)


def _mesh_has_format(mesh_obj, entry, fmt):
    """True when *entry* stores *fmt* data for *mesh_obj*."""
    if mesh_obj is None or entry is None:
        return False

    if fmt == FORMAT_BCK:
        owner = registry_data.owner_for(mesh_obj)

        if owner is None or owner.type != "ARMATURE":
            return False

        return any(track.format == FORMAT_BCK and track.target == owner.name
                   and registry_data._track_has_keys(track)
                   for track in entry.tracks)

    return any(track.format == FORMAT_BLK and track.target == mesh_obj.name
               and registry_data._track_has_keys(track)
               for track in entry.tracks)


def _visible_formats(settings, mesh_obj, entry):
    formats = []

    if settings.export_bck and _mesh_has_format(mesh_obj, entry, FORMAT_BCK):
        formats.append(FORMAT_BCK)

    if settings.export_blk and _mesh_has_format(mesh_obj, entry, FORMAT_BLK):
        formats.append(FORMAT_BLK)

    return formats


def _selected_mesh(settings):
    return _mesh(settings.selected_mesh)


def _mesh_animations(settings, mesh_name):
    return [item for item in settings.animations
            if item.mesh_name == mesh_name]


def _mesh_can_export(settings, mesh_name):
    return any(item.enabled for item in _mesh_animations(settings, mesh_name))


def _mesh_selected(settings, mesh_name):
    return any(item.include for item in _mesh_animations(settings, mesh_name))


# ---------------------------------------------------------------------------
# Property updates
# ---------------------------------------------------------------------------

def _sync_mesh_checks(settings):
    for mesh_item in settings.meshes:
        mesh_item.include = _mesh_selected(settings, mesh_item.name)


def _refresh_formats(settings):
    """Recompute the type column and the checkbox state of every animation."""
    global _SYNCING

    if settings is None:
        return

    previous = _SYNCING
    _SYNCING = True

    try:
        for item in settings.animations:
            mesh_obj = _mesh(item.mesh_name)
            formats = _visible_formats(
                settings, mesh_obj, _entry(mesh_obj, item.name))
            item.types = ", ".join(formats)
            was_enabled = item.enabled
            item.enabled = bool(formats)

            if not item.enabled:
                item.include = False
            elif not was_enabled:
                item.include = True

        _sync_mesh_checks(settings)
    finally:
        _SYNCING = previous


def _formats_changed(self, context):
    if _SYNCING:
        return

    _refresh_formats(_get_settings(context))


def _mesh_include_changed(self, context):
    global _SYNCING

    if _SYNCING:
        return

    settings = _get_settings(context)

    if settings is None:
        return

    previous = _SYNCING
    _SYNCING = True

    try:
        for item in settings.animations:
            if item.mesh_name == self.name and item.enabled:
                item.include = self.include
    finally:
        _SYNCING = previous


def _animation_include_changed(self, context):
    global _SYNCING

    if _SYNCING:
        return

    settings = _get_settings(context)

    if settings is None:
        return

    previous = _SYNCING
    _SYNCING = True

    try:
        _sync_mesh_checks(settings)
    finally:
        _SYNCING = previous


def _store_keys(settings):
    mesh_obj = _mesh(settings.keys_mesh)

    if mesh_obj is not None:
        bls_export_ui._store_key_selection(settings, mesh_obj)


def _key_include_changed(self, context):
    if _SYNCING:
        return

    settings = _get_settings(context)

    if settings is not None:
        _store_keys(settings)


def _populate_keys(settings, mesh_obj):
    """Fill the shared shape-key list from *mesh_obj*."""
    global _SYNCING

    if settings is None:
        return

    previous = _SYNCING
    _SYNCING = True

    try:
        _store_keys(settings)
        settings.keys.clear()
        settings.active_key = 0
        settings.keys_mesh = mesh_obj.name if mesh_obj is not None else ""
        stored = (bls_export_ui._stored_key_selection(mesh_obj)
                  if mesh_obj is not None else {})

        if mesh_obj is not None and mesh_obj.data is not None:
            shape_keys = mesh_obj.data.shape_keys

            if shape_keys is not None:
                for block in shape_keys.key_blocks[1:]:
                    item = settings.keys.add()
                    item.name = block.name
                    item.include = stored.get(block.name, True)
    finally:
        _SYNCING = previous


def _active_row_changed(self, context):
    global _SYNCING

    if _SYNCING:
        return

    index = self.active_tree_row

    if not (0 <= index < len(self.tree_rows)):
        return

    row = self.tree_rows[index]

    if not (0 <= row.mesh_index < len(self.meshes)):
        return

    previous = _SYNCING
    _SYNCING = True

    try:
        self.selected_mesh = self.meshes[row.mesh_index].name
        _populate_keys(self, _mesh(self.selected_mesh))
    finally:
        _SYNCING = previous


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class BMD_ANIM_ExportMeshItem(PropertyGroup):
    name: StringProperty()
    include: BoolProperty(
        name="Export mesh", default=True,
        update=_mesh_include_changed, options=set())
    expanded: BoolProperty(name="Expanded", default=True, options=set())


class BMD_ANIM_ExportAnimItem(PropertyGroup):
    mesh_name: StringProperty()
    name: StringProperty()
    types: StringProperty()
    enabled: BoolProperty(default=True, options=set())
    include: BoolProperty(
        name="Export animation", default=True,
        update=_animation_include_changed, options=set())


class BMD_ANIM_ExportTreeRow(PropertyGroup):
    kind: StringProperty()
    mesh_index: IntProperty(default=0)
    anim_index: IntProperty(default=-1)


class BMD_ANIM_ExportKeyItem(PropertyGroup):
    name: StringProperty()
    include: BoolProperty(
        name="Export key", default=True,
        update=_key_include_changed, options=set())


def _bck_target(settings):
    """Mesh that owns the first BCK track in the export list."""
    if settings is None:
        return None

    for item in settings.animations:
        if FORMAT_BCK in item.types:
            return _mesh(item.mesh_name)

    return None


def _convert_units_changed(self, context):
    """Remember the user's Convert Units choice across dialogs."""
    if _SYNCING:
        return

    settings = _get_settings(context)
    bc.remember_convert_units(
        context, self.convert_units, _bck_target(settings))


class BMD_ANIM_ExportSettings(PropertyGroup):
    folder: StringProperty(
        name="Export Folder",
        description="Folder that receives the exported .bck and .blk files",
        subtype="DIR_PATH",
        default="",
        options=set(),
    )
    export_bck: BoolProperty(
        name="BCK",
        description="Export joint animations",
        default=True,
        update=_formats_changed,
        options=set(),
    )
    export_blk: BoolProperty(
        name="BLK",
        description="Export shape-key animations",
        default=True,
        update=_formats_changed,
        options=set(),
    )
    arc_hierarchy: BoolProperty(
        name="Create .arc hierarchy",
        description="Write animations into bck/ and blk/ sub-folders",
        default=False,
        options=set(),
    )
    convert_units: BoolProperty(
        name="Convert Units",
        description="Convert Blender units to J3D units (scale 100)",
        default=True,
        update=_convert_units_changed,
        options=set(),
    )
    bck_available: BoolProperty(default=True, options=set())
    blk_available: BoolProperty(default=True, options=set())
    meshes: CollectionProperty(type=BMD_ANIM_ExportMeshItem)
    animations: CollectionProperty(type=BMD_ANIM_ExportAnimItem)
    tree_rows: CollectionProperty(type=BMD_ANIM_ExportTreeRow)
    active_tree_row: IntProperty(
        default=0, min=0, update=_active_row_changed, options=set())
    keys: CollectionProperty(type=BMD_ANIM_ExportKeyItem)
    active_key: IntProperty(default=0, min=0, options=set())
    selected_mesh: StringProperty()
    keys_mesh: StringProperty()


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------

def _rebuild_tree(settings):
    settings.tree_rows.clear()

    for mesh_index, mesh_item in enumerate(settings.meshes):
        row = settings.tree_rows.add()
        row.kind = "mesh"
        row.mesh_index = mesh_index

        for anim_index, item in enumerate(settings.animations):
            if item.mesh_name != mesh_item.name:
                continue

            row = settings.tree_rows.add()
            row.kind = "animation"
            row.mesh_index = mesh_index
            row.anim_index = anim_index


def _scan_scene(context):
    """Register actions that were never added to a registry."""
    owners = set()

    for obj in context.scene.objects:
        if obj.type != "MESH":
            continue

        owner = registry_data.owner_for(obj)

        if owner is not None:
            owners.add(owner.as_pointer())

    for pointer in owners:
        owner = next((obj for obj in context.scene.objects
                      if obj.as_pointer() == pointer), None)

        if owner is None:
            continue

        try:
            registry_data.scan_actions(context, owner, select=False)
        except Exception:
            pass


def scene_has_animations(context):
    """True when any BMD mesh in the scene has registered animations."""
    scene = getattr(context, "scene", None)

    if scene is None:
        return False

    for obj in scene.objects:
        if obj.type != "MESH" or not bc.is_bmd_object(obj):
            continue

        owner = registry_data.owner_for(obj)

        if owner is None or "j3d_animations" not in owner:
            continue

        for entry in owner.j3d_animations.animations:
            if any(registry_data._track_has_keys(track)
                   for track in entry.tracks):
                return True

    return False


def _populate(settings, context):
    """Collect every mesh of the scene and its BCK/BLK animations."""
    global _SYNCING

    if settings is None:
        return

    previous = _SYNCING
    _SYNCING = True

    try:
        settings.meshes.clear()
        settings.animations.clear()
        settings.tree_rows.clear()
        settings.selected_mesh = ""
        settings.keys_mesh = ""
        settings.keys.clear()
        settings.active_key = 0
        has_bck = False
        has_blk = False
        meshes = sorted(
            (obj for obj in context.scene.objects if obj.type == "MESH"),
            key=lambda obj: obj.name.casefold())

        for obj in meshes:
            if not bc.is_bmd_object(obj):
                continue

            reg = registry_data.registry_for(obj)

            if reg is None or not reg.animations:
                continue

            rows = []

            for entry in reg.animations:
                bck = _mesh_has_format(obj, entry, FORMAT_BCK)
                blk = _mesh_has_format(obj, entry, FORMAT_BLK)

                if not (bck or blk):
                    continue

                rows.append((entry, bck, blk))

            if not rows:
                continue

            mesh_item = settings.meshes.add()
            mesh_item.name = obj.name
            mesh_item.include = True
            mesh_item.expanded = True

            for entry, bck, blk in rows:
                has_bck = has_bck or bck
                has_blk = has_blk or blk
                item = settings.animations.add()
                item.mesh_name = obj.name
                item.name = entry.name
                item.include = True
                item.enabled = True

        settings.bck_available = has_bck
        settings.blk_available = has_blk
        settings.export_bck = has_bck
        settings.export_blk = has_blk
        _refresh_formats(settings)
        _rebuild_tree(settings)

        if settings.tree_rows:
            settings.active_tree_row = 0
            settings.selected_mesh = settings.meshes[0].name
            _populate_keys(settings, _mesh(settings.selected_mesh))

        first_bck_mesh = next(
            (_mesh(item.mesh_name) for item in settings.animations
             if FORMAT_BCK in item.types), None)
        settings.convert_units = bc.saved_convert_units(
            context, first_bck_mesh)
    finally:
        _SYNCING = previous


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def _visible_tree_rows(settings):
    count = 0

    for mesh_item in settings.meshes:
        count += 1

        if mesh_item.expanded:
            count += len(_mesh_animations(settings, mesh_item.name))

    return count


def _is_expanded(settings, mesh_index):
    if 0 <= mesh_index < len(settings.meshes):
        return settings.meshes[mesh_index].expanded

    return True


class BMD_ANIM_UL_export_tree(UIList):
    """Mesh / animation tree with the exported format column."""

    bl_idname = "BMD_ANIM_UL_export_tree"

    def filter_items(self, context, data, propname):
        flags = []

        for row in getattr(data, propname):
            if row.kind == "mesh" or _is_expanded(data, row.mesh_index):
                flags.append(self.bitflag_filter_item)
            else:
                flags.append(0)

        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag=0):
        settings = data

        if item.kind == "mesh":
            mesh_item = (settings.meshes[item.mesh_index]
                         if 0 <= item.mesh_index < len(settings.meshes)
                         else None)

            if mesh_item is None:
                return

            row = layout.row(align=True)
            row.alignment = "LEFT"
            check = row.row(align=True)
            check.enabled = _mesh_can_export(settings, mesh_item.name)
            check.prop(mesh_item, "include", text="")

            if _mesh_animations(settings, mesh_item.name):
                row.prop(
                    mesh_item, "expanded", text="",
                    icon=("TRIA_DOWN" if mesh_item.expanded else "TRIA_RIGHT"),
                    emboss=False)

            row.label(text=mesh_item.name, icon="MESH_DATA")
            return

        if not (0 <= item.anim_index < len(settings.animations)):
            return

        animation = settings.animations[item.anim_index]
        row = layout.row(align=True)
        row.label(text="", icon="BLANK1")
        check = row.row(align=True)
        check.enabled = animation.enabled
        check.prop(animation, "include", text="")
        name_col = row.row(align=True)
        name_col.enabled = animation.enabled
        name_col.label(text=animation.name, icon="ACTION")
        type_col = row.column(align=True)
        type_col.ui_units_x = 3.0
        type_col.alignment = "CENTER"
        type_col.label(text=animation.types)


class BMD_ANIM_UL_export_keys(UIList):
    bl_idname = "BMD_ANIM_UL_export_keys"

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag=0):
        row = layout.row(align=True)
        row.prop(item, "include", text="")
        row.label(text=item.name, icon="SHAPEKEY_DATA")


def _selected_mesh_has_blk(settings):
    if not settings.export_blk:
        return False

    mesh_obj = _selected_mesh(settings)

    if (mesh_obj is None or mesh_obj.data is None
            or mesh_obj.data.shape_keys is None):
        return False

    reg = registry_data.registry_for(mesh_obj)

    if reg is None:
        return False

    return any(_mesh_has_format(mesh_obj, entry, FORMAT_BLK)
               for entry in reg.animations)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _safe_stem(name):
    stem = str(name or "").strip() or "animation"

    for char in '<>:"/\\|?*':
        stem = stem.replace(char, "_")

    return stem


def _output_path(settings, name, extension):
    folder = bpy.path.abspath(settings.folder).strip()

    if settings.arc_hierarchy:
        folder = os.path.join(folder, extension)

    return os.path.join(folder, f"{_safe_stem(name)}.{extension}")


def _unique_path(path, used):
    candidate = path
    stem, extension = os.path.splitext(path)
    index = 1

    while candidate.casefold() in used:
        candidate = f"{stem}.{index:03d}{extension}"
        index += 1

    used.add(candidate.casefold())
    return candidate


def _export_bck_action(context, arm_obj, action, entry, path, scale,
                       operator):
    animation_data = arm_obj.animation_data
    previous_action = (animation_data.action
                       if animation_data is not None else None)
    previous_slot = (getattr(animation_data, "action_slot", None)
                     if animation_data is not None else None)

    try:
        registry_data._assign_action(arm_obj, action.name)
        bck_export.export_bck(
            context, path, scale, entry.loop_mode, "ACTION",
            operator.report, target=arm_obj)
    finally:
        animation_data = arm_obj.animation_data

        if animation_data is not None:
            animation_data.action = previous_action

            if (previous_slot is not None
                    and hasattr(animation_data, "action_slot")):
                try:
                    animation_data.action_slot = previous_slot
                except Exception:
                    pass


def _mesh_key_names(mesh_obj):
    """Shape keys selected for *mesh_obj* in its shared key list."""
    keys = mesh_obj.data.shape_keys if mesh_obj.data is not None else None

    if keys is None:
        return []

    names = [block.name for block in keys.key_blocks[1:]]

    if not names:
        return []

    stored = bls_export_ui._stored_key_selection(mesh_obj)

    if not stored:
        return names

    return [name for name in names if stored.get(name, True)]


def _export_all(context, settings, operator):
    folder = bpy.path.abspath(settings.folder).strip()

    if not folder:
        operator.report({"ERROR"}, "Select an export folder first")
        return {"CANCELLED"}

    if not (settings.export_bck or settings.export_blk):
        operator.report({"ERROR"}, "Select BCK, BLK or both")
        return {"CANCELLED"}

    selected_meshes = {mesh_item.name for mesh_item in settings.meshes
                       if mesh_item.include}

    for mesh_name in selected_meshes:
        mesh_obj = _mesh(mesh_name)

        if mesh_obj is None or not bc.is_bmd_object(mesh_obj):
            operator.report({"ERROR"}, "Incorrect target")
            return {"CANCELLED"}

    jobs = [
        (item.mesh_name, item.name)
        for item in settings.animations
        if item.include and item.enabled and item.mesh_name in selected_meshes
    ]

    if not jobs:
        operator.report({"ERROR"}, "Select at least one animation to export")
        return {"CANCELLED"}

    try:
        os.makedirs(folder, exist_ok=True)

        if settings.arc_hierarchy:
            for fmt, enabled in ((FORMAT_BCK, settings.export_bck),
                                 (FORMAT_BLK, settings.export_blk)):
                if enabled:
                    os.makedirs(
                        os.path.join(folder, fmt.lower()), exist_ok=True)
    except OSError as exc:
        operator.report({"ERROR"}, f"Could not create the export folder: {exc}")
        return {"CANCELLED"}

    scale = 100.0 if settings.convert_units else 1.0
    used_paths = set()
    done_bck = set()
    written = 0
    errors = []

    for mesh_name, entry_name in jobs:
        mesh_obj = _mesh(mesh_name)
        entry = _entry(mesh_obj, entry_name)

        if mesh_obj is None or entry is None:
            continue

        if settings.export_bck:
            owner = registry_data.owner_for(mesh_obj)

            if owner is not None and owner.type == "ARMATURE":
                for track in entry.tracks:
                    if (track.format != FORMAT_BCK
                            or track.target != owner.name
                            or not registry_data._track_has_keys(track)):
                        continue

                    action = registry_data._track_action(track)

                    if action is None:
                        continue

                    dedupe = (FORMAT_BCK, owner.name, action.name)

                    if dedupe in done_bck:
                        break

                    done_bck.add(dedupe)
                    path = _unique_path(
                        _output_path(settings, entry.name, "bck"), used_paths)

                    try:
                        _export_bck_action(
                            context, owner, action, entry, path, scale,
                            operator)
                        written += 1
                    except Exception as exc:
                        errors.append(f"{os.path.basename(path)}: {exc}")

                    break

        if settings.export_blk:
            for track in entry.tracks:
                if (track.format != FORMAT_BLK
                        or track.target != mesh_obj.name
                        or not registry_data._track_has_keys(track)):
                    continue

                action = registry_data._track_action(track)

                if action is None:
                    continue

                key_names = _mesh_key_names(mesh_obj)

                if not key_names:
                    errors.append(
                        f"{mesh_obj.name}: select at least one shape key")
                    break

                path = _unique_path(
                    _output_path(settings, entry.name, "blk"), used_paths)

                try:
                    blk_export.export_blk(
                        context, path, key_names, entry.loop_mode,
                        "ACTION", operator.report, target=mesh_obj,
                        action=action)
                    written += 1
                except Exception as exc:
                    errors.append(f"{os.path.basename(path)}: {exc}")

                break

    if errors:
        operator.report({"ERROR"},
                        "Export failed: " + "; ".join(errors[:4]))
        return {"CANCELLED"}

    if not written:
        operator.report({"ERROR"}, "Nothing was exported")
        return {"CANCELLED"}

    settings.folder = folder
    bc.remember_convert_units(
        context, settings.convert_units, _bck_target(settings))
    operator.report(
        {"INFO"},
        f"Exported {written} animation file(s) to {folder}")
    return {"FINISHED"}


class BMD_ANIM_OT_export(Operator):
    bl_idname = "export_anim.j3d"
    bl_label = "Export J3D Animations"
    bl_description = "Export the scene's BCK and BLK animations in one batch"
    # REGISTER would leave an "Adjust Last Operation" popup with this dialog's
    # layout in the bottom-left corner after a successful export.
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return scene_has_animations(context)

    def invoke(self, context, _event):
        settings = _get_settings(context)

        if settings is None:
            self.report({"ERROR"}, "Animation export settings are unavailable")
            return {"CANCELLED"}

        _scan_scene(context)

        if not settings.folder:
            blend_path = bpy.data.filepath

            if blend_path:
                settings.folder = os.path.dirname(blend_path)

        _populate(settings, context)
        return context.window_manager.invoke_props_dialog(
            self, width=760, confirm_text="Export")

    def draw(self, context):
        layout = self.layout
        layout.use_property_decorate = False
        settings = _get_settings(context)

        if settings is None:
            layout.label(text="Animation export settings are unavailable",
                         icon="ERROR")
            return

        invalid_target = False

        for mesh_item in settings.meshes:
            if not mesh_item.include:
                continue

            mesh_obj = _mesh(mesh_item.name)

            if mesh_obj is None or not bc.is_bmd_object(mesh_obj):
                invalid_target = True
                break

        if invalid_target:
            layout.label(text="Incorrect target", icon="ERROR")

        layout.prop(settings, "folder", text="Export Folder")

        row = layout.row(align=True)
        row.alignment = "LEFT"
        bck_box = row.row(align=True)
        bck_box.enabled = settings.bck_available
        bck_box.prop(settings, "export_bck", text="BCK")
        blk_box = row.row(align=True)
        blk_box.enabled = settings.blk_available
        blk_box.prop(settings, "export_blk", text="BLK")

        layout.prop(settings, "arc_hierarchy")

        convert = layout.row()
        convert.enabled = settings.export_bck
        convert.prop(settings, "convert_units")

        layout.separator()
        layout.label(text="Animations", icon="ACTION")

        if not settings.tree_rows:
            return

        rows = min(max(1, _visible_tree_rows(settings)), MAX_TREE_ROWS)
        layout.template_list(
            "BMD_ANIM_UL_export_tree", "",
            settings, "tree_rows",
            settings, "active_tree_row",
            rows=rows,
            maxrows=MAX_TREE_ROWS,
        )

        if not any(item.include and item.enabled
                   for item in settings.animations):
            layout.label(text="Select at least one animation", icon="ERROR")

        if _selected_mesh_has_blk(settings):
            mesh_obj = _selected_mesh(settings)
            layout.separator()
            layout.label(text=f"Shape Keys ({mesh_obj.name})",
                         icon="SHAPEKEY_DATA")
            key_rows = min(max(1, len(settings.keys)), MAX_KEY_ROWS)
            layout.template_list(
                "BMD_ANIM_UL_export_keys", "",
                settings, "keys",
                settings, "active_key",
                rows=key_rows,
                maxrows=MAX_KEY_ROWS,
            )

            if not any(item.include for item in settings.keys):
                layout.label(text="Select at least one shape key",
                             icon="ERROR")

    def execute(self, context):
        settings = _get_settings(context)

        if settings is None:
            self.report({"ERROR"}, "Animation export settings are unavailable")
            return {"CANCELLED"}

        return _export_all(context, settings, self)


CLASSES = (
    BMD_ANIM_ExportMeshItem,
    BMD_ANIM_ExportAnimItem,
    BMD_ANIM_ExportTreeRow,
    BMD_ANIM_ExportKeyItem,
    BMD_ANIM_ExportSettings,
    BMD_ANIM_UL_export_tree,
    BMD_ANIM_UL_export_keys,
    BMD_ANIM_OT_export,
)


def register_properties():
    bpy.types.Scene.bmd_anim_export_settings = PointerProperty(
        type=BMD_ANIM_ExportSettings)


def unregister_properties():
    if hasattr(bpy.types.Scene, "bmd_anim_export_settings"):
        del bpy.types.Scene.bmd_anim_export_settings
