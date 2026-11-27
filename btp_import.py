"""Import BTP texture-pattern animations into the shared animation registry."""

from __future__ import annotations

import os

import bpy

from . import animation_registry as registry_data
from . import blender_common as bc
from . import btp_preview
from .j3d import anim
from .j3d import btp as btp_data


def owner_meshes(owner):
    if owner is None:
        return []
    if owner.type == "MESH":
        return [owner]
    if owner.type == "ARMATURE":
        return [obj for obj in bpy.data.objects
                if obj.type == "MESH" and bc.target_armature(obj) is owner]
    return []


def material_map(owner):
    """Material name -> material for all meshes of *owner*.

    Both the Blender datablock name and the name written to the BMD are keys, so
    a BTP that names the material the way the file does (`bmd_name`, or without
    Blender's ``.001`` suffix) still finds the right slot.
    """
    result = {}
    for mesh in owner_meshes(owner):
        for slot in mesh.material_slots:
            material = slot.material
            if material is None:
                continue
            result.setdefault(material.name, material)
            result.setdefault(bc.material_export_name(material), material)
    return result


def missing_materials(owner, btp):
    available = material_map(owner)
    return [entry.name for entry in btp.entries
            if entry.name not in available]


def find_compatible_owner(context, animations):
    """Return a BMD owner whose materials cover every BTP entry."""
    active = getattr(context, "active_object", None)
    selected = list(getattr(context, "selected_objects", ()) or ())
    candidates = []
    seen = set()

    for obj in [active] + selected + list(getattr(context.scene, "objects", ())):
        if obj is None or obj.as_pointer() in seen:
            continue
        seen.add(obj.as_pointer())
        owner = registry_data.owner_for(obj)
        if owner is None or not bc.is_bmd_object(owner):
            continue
        if all(not missing_materials(owner, animation)
               for animation in animations):
            return owner
    return None


def _assign_frames(material, frames, action):
    """Animate the material's BTP texture index."""
    btp_preview.set_field(material, frames[0])
    material.keyframe_insert(data_path="bmd.btp_texture_index", frame=0)
    previous = frames[0]
    for frame, index in enumerate(frames[1:], 1):
        if index != previous:
            btp_preview.set_field(material, index)
            material.keyframe_insert(data_path="bmd.btp_texture_index",
                                     frame=frame)
            previous = index
    for fcurve in action.fcurves:
        for point in fcurve.keyframe_points:
            point.interpolation = "CONSTANT"


def import_btp(context, owner, animation, filepath, report=None, name=None):
    """Import one parsed BTP file; return the registered animation entry."""
    if owner is None or animation is None:
        return None

    stem = name or os.path.splitext(os.path.basename(filepath))[0]
    available = material_map(owner)
    # Range and loop mode come from the file's own TPT1 header.
    frame_end = max(0, btp_data.frame_end(animation))
    try:
        loop_name = anim.LOOP_NAMES[int(animation.loop_mode)]
    except (IndexError, ValueError):
        loop_name = "ONCE"

    last_entry = None
    last_entry_name = None
    imported = 0

    for item in animation.entries:
        material = available.get(item.name)
        if material is None:
            if report:
                report({"WARNING"},
                       f"{stem}: material '{item.name}' is not in the target")
            continue
        frames = list(item.frames)
        if not frames:
            continue

        action = bpy.data.actions.new(f"{material.name}.{stem}.btp")
        animation_data = (material.animation_data
                          or material.animation_data_create())
        animation_data.action = action
        _assign_frames(material, frames, action)
        action.use_fake_user = True
        action["bmd_duration"] = frame_end
        action["bmd_loop_mode"] = int(animation.loop_mode)
        action["bmd_source"] = os.path.abspath(filepath)
        # Only materials animated by this BTP get the Texture Index field.
        bc.ensure_material_track(material, "BTP")
        btp_preview.prepare_preview(material, owner, frames)
        btp_preview.set_field(material, frames[0])

        entry = registry_data.register_btp_action(
            owner, material, action, stem, 0, frame_end, loop_name,
            select=False, sort=False)
        if entry is not None:
            last_entry = entry
            last_entry_name = entry.name
            imported += 1

    if imported:
        registry = registry_data.registry_for(owner)
        if registry is not None:
            registry_data.sort_animations(registry)
        if last_entry_name:
            # Sorting may move collection items; resolve the entry again.
            return registry_data.find_animation(registry, last_entry_name)
    return None


def _action_source(action):
    if action is None:
        return ""
    source = action.get("bmd_source")
    return os.path.normcase(os.path.abspath(str(source))) if source else ""


def already_imported(owner, filepath):
    """True when an action from this file is already present on the model."""
    source = os.path.normcase(os.path.abspath(filepath))
    stem = os.path.splitext(os.path.basename(filepath))[0].casefold()
    for material in material_map(owner).values():
        animation_data = material.animation_data
        action = animation_data.action if animation_data is not None else None
        if action is None:
            continue
        if _action_source(action) == source:
            return True
        if (not action.get("bmd_source")
                and action.name.casefold().startswith(stem)):
            return True
    registry = registry_data.registry_for(owner)
    if registry is not None:
        for entry in registry.animations:
            for track in entry.tracks:
                if track.format != registry_data.FORMAT_BTP:
                    continue
                action = registry_data._track_action(track)
                if _action_source(action) == source:
                    return True
    return False
