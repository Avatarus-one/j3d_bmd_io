"""Per-object registry of J3D animations.

An animation is a named container for tracks.  A track is one action in a
specific format (BCK joint animation, BLK shape-key animation, ...) and for a
specific target (armature or mesh).  Actions are created lazily when keys are
created or imported, and empty tracks are removed automatically.
"""

from __future__ import annotations

import math
import os
import re

import bpy
from bpy.props import (
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup, UIList

from . import blender_common as bc
from . import blk_export
from .bck_common import LOOP_ITEMS
from .j3d import anim


LOOP_ENUM_ITEMS = [item for item in LOOP_ITEMS if item[0] != "AUTO"]

FORMAT_BCK = "BCK"
FORMAT_BLK = "BLK"
FORMAT_ICONS = {
    FORMAT_BCK: "ARMATURE_DATA",
    FORMAT_BLK: "SHAPEKEY_DATA",
}
FORMAT_META_KEYS = {
    FORMAT_BCK: "j3d_owner",
    FORMAT_BLK: "j3d_mesh",
}

# Blender ID names are limited to 63 bytes. Uniqueness is inserted before the
# type suffix so it always stays last.
ACTION_NAME_MAX = 63

_SYNCING = False
_SYNC_SCHEDULED = False
_LAST_SYNC = None
_LAST_TARGET = None


# ---------------------------------------------------------------------------
# Object / registry lookup
# ---------------------------------------------------------------------------

def owner_for(obj):
    """Return the object that owns the animation list for *obj*."""
    if obj is None:
        return None

    if obj.type == "ARMATURE":
        return obj

    if obj.type == "MESH":
        arm = bc.target_armature(obj)

        if arm is not None and bc.is_bmd_object(arm):
            return arm

        return obj

    return None


def context_owner(context):
    """Animation owner targeted by the panels.

    Only BMD objects are considered.  Selecting an unrelated mesh or armature
    must not create animation data on it.
    """
    active = getattr(context, "active_object", None)
    owner = owner_for(active)

    if owner is not None and bc.is_bmd_object(owner):
        return owner

    # A non-BMD model is an explicit miss; do not fall back to another object.
    if active is not None and active.type in {"MESH", "ARMATURE"}:
        return None

    for obj in getattr(context, "selected_objects", ()) or ():
        owner = owner_for(obj)

        if owner is not None and bc.is_bmd_object(owner):
            return owner

    return None


def registry_for(obj):
    owner = owner_for(obj)

    if owner is None or not bc.is_bmd_object(owner):
        return None

    return owner.j3d_animations


def loop_mode_name(action):
    if action is None:
        return "ONCE"

    try:
        mode = int(action.get("bmd_loop_mode", anim.LOOP_ONCE))
    except (TypeError, ValueError):
        return "ONCE"

    if 0 <= mode < len(anim.LOOP_NAMES):
        return anim.LOOP_NAMES[mode]

    return "ONCE"


def find_animation(registry, name):
    if registry is None:
        return None

    key = str(name or "").strip().casefold()

    if not key:
        return None

    for entry in registry.animations:
        if entry.name.strip().casefold() == key:
            return entry

    return None


def unique_name(registry, base, exclude=None):
    base = str(base or "").strip() or "animation"
    exclude_ptr = exclude.as_pointer() if exclude is not None else None
    names = set()

    for entry in registry.animations:
        if exclude_ptr is not None and entry.as_pointer() == exclude_ptr:
            continue

        names.add(entry.name.strip().casefold())

    if base.casefold() not in names:
        return base

    index = 1

    while True:
        candidate = f"{base}.{index:03d}"

        if candidate.casefold() not in names:
            return candidate

        index += 1


def copy_name(registry, base):
    """Return the next Blender-style name for a copied animation."""
    base = str(base or "").strip() or "animation"
    match = re.match(r"^(.*)\.(\d{3})$", base)

    if match:
        stem = match.group(1)
        index = int(match.group(2)) + 1
    else:
        stem = base
        index = 1

    names = {entry.name.strip().casefold() for entry in registry.animations}

    while True:
        candidate = f"{stem}.{index:03d}"

        if candidate.casefold() not in names:
            return candidate

        index += 1


def _sort_key(entry):
    return (entry.name.casefold(), entry.name)


def sort_animations(registry):
    """Sort the animation list alphabetically, keeping the active entry."""
    global _SYNCING

    if registry is None or len(registry.animations) < 2:
        return

    active_name = None
    active_index = int(registry.active_index)
    names = [entry.name for entry in registry.animations]

    if 0 <= active_index < len(names):
        active_name = names[active_index]

    # Compute the final order once in Python, then reorder the collection with
    # as few moves as possible.  The old selection sort re-read every
    # CollectionProperty item for each comparison, which becomes very slow
    # while importing hundreds of animations.
    order = sorted(range(len(names)),
                   key=lambda i: (names[i].casefold(), names[i]))
    desired = [names[i] for i in order]
    current = names[:]
    previous = _SYNCING
    _SYNCING = True

    try:
        for i, name in enumerate(desired):
            if current[i] == name:
                continue

            j = current.index(name, i)
            registry.animations.move(j, i)
            current.insert(i, current.pop(j))

        if active_name is not None:
            for i, name in enumerate(current):
                if name == active_name:
                    registry.active_index = i
                    break
    finally:
        _SYNCING = previous


_SORT_PENDING = set()


def schedule_sort(registry):
    """Debounced sort after an inline rename edit."""
    if registry is None:
        return

    try:
        ptr = registry.as_pointer()
    except Exception:
        return

    if ptr in _SORT_PENDING:
        return

    _SORT_PENDING.add(ptr)

    def run():
        _SORT_PENDING.discard(ptr)

        try:
            sort_animations(registry)
            _tag_redraw()
        except Exception:
            pass

        return None

    bpy.app.timers.register(run, first_interval=0.15)


# ---------------------------------------------------------------------------
# Action helpers
# ---------------------------------------------------------------------------

def _action_slot(action):
    if action is not None and hasattr(action, "slots") and len(action.slots):
        return action.slots[0]

    return None


def _assign_action(owner, action_name):
    action = bpy.data.actions.get(action_name) if action_name else None

    if owner is None or action is None:
        return False

    animation_data = owner.animation_data or owner.animation_data_create()
    animation_data.action = action
    slot = _action_slot(action)

    if slot is not None and hasattr(animation_data, "action_slot"):
        try:
            animation_data.action_slot = slot
        except Exception:
            pass

    return True


def _clear_action(owner, action_name):
    action = bpy.data.actions.get(action_name) if action_name else None

    if owner is None or action is None:
        return

    animation_data = owner.animation_data

    if animation_data is not None and animation_data.action is action:
        animation_data.action = None


def _shape_keys(mesh):
    if mesh is None or mesh.type != "MESH" or mesh.data is None:
        return None

    return mesh.data.shape_keys


def _has_keys(action):
    if action is None:
        return False

    try:
        return any(len(fcurve.keyframe_points) > 0
                   for fcurve in action.fcurves)
    except Exception:
        return False


def _owner_meshes(owner):
    if owner is None:
        return []

    if owner.type == "MESH":
        return [owner]

    if owner.type == "ARMATURE":
        return [obj for obj in bpy.data.objects
                if obj.type == "MESH" and bc.target_armature(obj) is owner]

    return []


# ---------------------------------------------------------------------------
# Track helpers
# ---------------------------------------------------------------------------

def _track_for(entry, fmt, target):
    for track in entry.tracks:
        if track.format == fmt and track.target == target:
            return track

    return None


def _set_track(entry, fmt, target, action_name):
    track = _track_for(entry, fmt, target)

    if track is None:
        track = entry.tracks.add()
        track.format = fmt
        track.target = target

    track.action_name = action_name
    return track


def _tag_action(action, fmt, target, entry_name):
    """Store the owning animation and target on an action."""
    if action is None:
        return

    try:
        if action.get("j3d_animation_name") != entry_name:
            action["j3d_animation_name"] = entry_name

        key = FORMAT_META_KEYS.get(fmt)

        if key and not action.get(key):
            action[key] = target
    except Exception:
        pass


def _new_entry(registry, name, frame_start=0, frame_end=0,
               loop_mode="ONCE"):
    """Add an animation entry without triggering UI callbacks."""
    global _SYNCING

    # Resolve the name before adding: a new item gets the default name
    # "Animation", which would otherwise make unique_name() append ".001".
    entry_name = unique_name(registry, name)
    _SYNCING = True

    try:
        entry = registry.animations.add()
        entry.name = entry_name
        entry.frame_start = int(frame_start)
        entry.frame_end = int(frame_end)
        entry.loop_mode = loop_mode
    finally:
        _SYNCING = False

    return entry


def _track_action(track):
    return bpy.data.actions.get(track.action_name)


def _track_has_keys(track):
    return _has_keys(_track_action(track))


def entry_has_format(entry, fmt):
    if entry is None:
        return False

    return any(track.format == fmt and _track_has_keys(track)
               for track in entry.tracks)


def entry_has_bck(entry):
    return entry_has_format(entry, FORMAT_BCK)


def entry_has_blk(entry):
    return entry_has_format(entry, FORMAT_BLK)


def _entry_action_names(entry):
    return [track.action_name for track in entry.tracks
            if track.action_name]


def _format_owner(fmt, target):
    """Return the datablock whose ``animation_data`` holds *fmt* actions."""
    if fmt == FORMAT_BCK:
        return bpy.data.objects.get(target)

    if fmt == FORMAT_BLK:
        return _shape_keys(bpy.data.objects.get(target))

    return None


def _format_owners(owner):
    """Yield ``(format, target, action_owner)`` for *owner*.

    New formats are added here; the rest of the registry is format-agnostic.
    """
    if owner is None:
        return

    if owner.type == "ARMATURE":
        yield FORMAT_BCK, owner.name, owner

    for mesh in _owner_meshes(owner):
        keys = _shape_keys(mesh)

        if keys is not None:
            yield FORMAT_BLK, mesh.name, keys


def _format_targets(owner):
    """Yield ``(format, target, action_owner, active_action)`` for *owner*."""
    for fmt, target, action_owner in _format_owners(owner):
        animation_data = action_owner.animation_data
        action = animation_data.action if animation_data else None
        yield fmt, target, action_owner, action


def _assign_track(owner, track):
    action = _track_action(track)

    if action is None:
        return

    action_owner = _format_owner(track.format, track.target)

    if action_owner is not None:
        _assign_action(action_owner, action.name)


def _clear_track(owner, track):
    action = _track_action(track)

    if action is None:
        return

    action_owner = _format_owner(track.format, track.target)

    if action_owner is not None:
        _clear_action(action_owner, action.name)


def _action_used_elsewhere(registry, action_name, exclude=None):
    if registry is None:
        return False

    exclude_name = exclude.name if exclude is not None else None

    for entry in registry.animations:
        if exclude_name is not None and entry.name == exclude_name:
            continue

        if any(track.action_name == action_name for track in entry.tracks):
            return True

    return False


def _unique_action_name(base, fmt, action):
    """Return ``<animation>.<type>`` (or ``<animation>.001.<type>``)."""
    suffix = f".{fmt.lower()}"
    index = 0

    while True:
        index_part = "" if index == 0 else f".{index:03d}"
        limit = ACTION_NAME_MAX - len(suffix) - len(index_part)

        if limit < 1:
            limit = 1

        candidate = base[:limit] + index_part + suffix
        existing = bpy.data.actions.get(candidate)

        if existing is None or existing.as_pointer() == action.as_pointer():
            return candidate

        index += 1


def _set_action_name(action, entry, registry, fmt, target):
    """Name a track action ``<target>.<animation>.<type>`` unless shared."""
    if action is None:
        return None

    if registry is not None and _action_used_elsewhere(registry, action.name,
                                                       exclude=entry):
        return action.name

    desired = _unique_action_name(entry.name, fmt, action)

    if action.name != desired:
        action.name = desired

    return action.name


def _prune_tracks(entry, registry, owner=None):
    """Remove tracks whose actions are missing or have no keys."""
    changed = False

    for index in reversed(range(len(entry.tracks))):
        track = entry.tracks[index]
        action = _track_action(track)

        if action is not None and _has_keys(action):
            continue

        if owner is not None:
            _clear_track(owner, track)

        entry.tracks.remove(index)
        changed = True

        if action is not None and not _action_used_elsewhere(registry,
                                                             action.name):
            if action.users <= 1:
                try:
                    bpy.data.actions.remove(action)
                except Exception:
                    pass

    return changed


# ---------------------------------------------------------------------------
# Migration from the old fixed armature_action / shape_actions schema
# ---------------------------------------------------------------------------

def _migrate_entry(entry, owner):
    if entry.armature_action:
        _set_track(entry, FORMAT_BCK, owner.name, entry.armature_action)
        entry.armature_action = ""

    for shape in list(entry.shape_actions):
        _set_track(entry, FORMAT_BLK, shape.mesh_name, shape.action_name)

    if len(entry.shape_actions):
        entry.shape_actions.clear()


def migrate_registry(owner):
    registry = registry_for(owner)

    if registry is None:
        return

    for entry in registry.animations:
        _migrate_entry(entry, owner)


# ---------------------------------------------------------------------------
# Activation / range / loop mode
# ---------------------------------------------------------------------------

def _apply_loop_mode(entry):
    try:
        mode = anim.LOOP_NAMES.index(entry.loop_mode)
    except ValueError:
        mode = anim.LOOP_ONCE

    for action_name in _entry_action_names(entry):
        action = bpy.data.actions.get(action_name)

        if action is not None:
            try:
                action["bmd_loop_mode"] = int(mode)
            except Exception:
                pass


def _apply_range(context, entry):
    scene = getattr(context, "scene", None)

    if scene is None:
        return

    start = int(entry.frame_start)
    end = max(start, int(entry.frame_end))
    scene.frame_start = start
    scene.frame_end = end

    try:
        scene.frame_set(start)
    except Exception:
        pass


def _active_entry(registry):
    if registry is None or not registry.animations:
        return None

    index = min(max(int(registry.active_index), 0),
                len(registry.animations) - 1)
    return registry.animations[index]


def _select_entry(registry, entry):
    name = entry.name

    for index, item in enumerate(registry.animations):
        if item.name == name:
            registry.active_index = index
            return


def reset_to_rest(context, owner):
    """Reset the owner's pose bones and shape keys to their rest values."""
    if owner is None:
        return

    if owner.type == "ARMATURE":
        animation_data = owner.animation_data

        if animation_data is not None:
            animation_data.action = None

        for pose_bone in owner.pose.bones:
            pose_bone.matrix_basis.identity()

    for mesh in _owner_meshes(owner):
        keys = _shape_keys(mesh)

        if keys is None:
            continue

        animation_data = keys.animation_data

        if animation_data is not None:
            animation_data.action = None

        for block in keys.key_blocks[1:]:
            block.value = 0.0


def activate_animation(context, owner, entry):
    global _LAST_SYNC

    if owner is None or entry is None:
        return

    _LAST_SYNC = None
    migrate_registry(owner)
    registry = registry_for(owner)

    reset_to_rest(context, owner)

    if registry is not None:
        _prune_tracks(entry, registry, owner)

    for track in entry.tracks:
        _assign_track(owner, track)

    _apply_range(context, entry)
    _apply_loop_mode(entry)
    view_layer = getattr(context, "view_layer", None)

    if view_layer is not None:
        try:
            view_layer.update()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Register imported actions
# ---------------------------------------------------------------------------

def register_armature_action(owner, action, name, frame_start=0,
                             frame_end=0, loop_mode="ONCE", select=True,
                             sort=True):
    """Add or update a BCK track in the owner's animation list."""
    migrate_registry(owner)
    registry = registry_for(owner)

    if registry is None or action is None:
        return None

    entry = find_animation(registry, name)
    created = entry is None

    if created:
        entry = _new_entry(registry, name, frame_start, frame_end, loop_mode)

    action_name = _set_action_name(action, entry, registry, FORMAT_BCK,
                                   owner.name)
    _set_track(entry, FORMAT_BCK, owner.name, action_name)
    action.use_fake_user = True
    _tag_action(action, FORMAT_BCK, owner.name, entry.name)

    if created:
        _apply_loop_mode(entry)

    entry_name = entry.name

    if select:
        _select_entry(registry, entry)

    if sort:
        sort_animations(registry)
        return find_animation(registry, entry_name) or entry

    return entry


def register_shape_action(mesh, action, name, frame_start=0,
                          frame_end=0, loop_mode="ONCE", select=True,
                          sort=True):
    """Add or update a BLK track for *mesh* in the owner's animation list."""
    owner = owner_for(mesh)
    migrate_registry(owner)
    registry = registry_for(owner)

    if registry is None or action is None or mesh is None:
        return None

    entry = find_animation(registry, name)
    created = entry is None

    if created:
        entry = _new_entry(registry, name, frame_start, frame_end, loop_mode)

    action_name = _set_action_name(action, entry, registry, FORMAT_BLK,
                                   mesh.name)
    _set_track(entry, FORMAT_BLK, mesh.name, action_name)
    action.use_fake_user = True
    _tag_action(action, FORMAT_BLK, mesh.name, entry.name)

    if created:
        _apply_loop_mode(entry)

    entry_name = entry.name

    if select:
        _select_entry(registry, entry)

    if sort:
        sort_animations(registry)
        return find_animation(registry, entry_name) or entry

    return entry


# ---------------------------------------------------------------------------
# Create / copy / delete
# ---------------------------------------------------------------------------

def create_animation(context, owner, base_name="animation"):
    """Create an empty animation container (no actions yet)."""
    migrate_registry(owner)
    registry = registry_for(owner)

    if registry is None:
        return None

    scene = context.scene
    entry = _new_entry(registry, base_name, scene.frame_start,
                       scene.frame_end, "ONCE")
    entry_name = entry.name
    _select_entry(registry, entry)
    sort_animations(registry)
    entry = find_animation(registry, entry_name) or entry
    activate_animation(context, owner, entry)
    return entry


def copy_animation(context, owner, entry):
    migrate_registry(owner)
    registry = registry_for(owner)

    if registry is None or entry is None:
        return None

    new_name = copy_name(registry, entry.name)
    new_entry = _new_entry(registry, new_name, entry.frame_start,
                           entry.frame_end, entry.loop_mode)
    new_entry_name = new_entry.name

    for track in entry.tracks:
        old = _track_action(track)

        if old is None:
            continue

        new = old.copy()
        new.use_fake_user = True
        new.name = _unique_action_name(new_entry.name, track.format, new)
        _tag_action(new, track.format, track.target, new_entry.name)
        _set_track(new_entry, track.format, track.target, new.name)

    _apply_loop_mode(new_entry)
    _select_entry(registry, new_entry)
    sort_animations(registry)
    return find_animation(registry, new_entry_name) or new_entry


def delete_animation(context, owner, entry):
    migrate_registry(owner)
    registry = registry_for(owner)

    if registry is None or entry is None:
        return False

    action_names = _entry_action_names(entry)

    for track in entry.tracks:
        _clear_track(owner, track)

    index = next(
        (i for i, item in enumerate(registry.animations)
         if item.name == entry.name), -1)

    if index < 0:
        return False

    registry.animations.remove(index)

    for action_name in action_names:
        if _action_used_elsewhere(registry, action_name):
            continue

        action = bpy.data.actions.get(action_name)

        if action is None or action.users > 1:
            continue

        try:
            bpy.data.actions.remove(action)
        except Exception:
            pass

    if registry.animations:
        registry.active_index = min(index, len(registry.animations) - 1)
        activate_animation(context, owner, _active_entry(registry))
    else:
        registry.active_index = 0
        reset_to_rest(context, owner)

    return True


# ---------------------------------------------------------------------------
# Property callbacks
# ---------------------------------------------------------------------------

def _active_changed(self, context):
    if _SYNCING:
        return

    # ``active_index`` belongs to this registry, so apply the entry to the
    # registry's own owner.  Using ``context.active_object`` here activated the
    # new clip on an unrelated object and reset that object's animation.
    owner = owner_for(self.id_data)

    if owner is None:
        owner = owner_for(context.active_object)

    activate_animation(context, owner, _active_entry(self))


def _range_changed(self, context):
    if _SYNCING:
        return

    owner = owner_for(self.id_data)

    if owner is None:
        return

    registry = registry_for(owner)
    entry = _active_entry(registry)

    if entry is not None and entry.as_pointer() == self.as_pointer():
        _apply_range(context, self)


def _loop_changed(self, context):
    if _SYNCING:
        return

    _apply_loop_mode(self)


def _name_changed(self, context):
    global _SYNCING

    if _SYNCING:
        return

    name = self.name.strip()
    owner = self.id_data
    registry = registry_for(owner) if owner is not None else None
    _SYNCING = True

    try:
        if not name:
            name = "animation"

        if registry is not None:
            unique = unique_name(registry, name, exclude=self)

            if self.name != unique:
                self.name = unique

            name = unique

        for track in self.tracks:
            action = _track_action(track)

            if action is None:
                continue

            action_name = _set_action_name(action, self, registry,
                                           track.format, track.target)
            track.action_name = action_name
            _tag_action(action, track.format, track.target, name)
    finally:
        _SYNCING = False

    schedule_sort(registry)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class J3DShapeActionItem(PropertyGroup):
    """Legacy BLK target list, migrated into ``tracks`` on load."""

    mesh_name: StringProperty()
    action_name: StringProperty()


class J3DAnimationTrack(PropertyGroup):
    format: StringProperty()
    target: StringProperty()
    action_name: StringProperty()


class J3DAnimationItem(PropertyGroup):
    # options=set() keeps the panel fields out of the keyframe context menu.
    name: StringProperty(name="Name", default="Animation",
                         update=_name_changed, options=set())
    frame_start: IntProperty(name="Start", default=0, update=_range_changed,
                             options=set())
    frame_end: IntProperty(name="End", default=0, update=_range_changed,
                           options=set())
    loop_mode: EnumProperty(name="Loop Mode", items=LOOP_ENUM_ITEMS,
                            default="ONCE", update=_loop_changed, options=set())
    tracks: CollectionProperty(type=J3DAnimationTrack)

    # Legacy fields, migrated by ``migrate_registry``.
    armature_action: StringProperty()
    shape_actions: CollectionProperty(type=J3DShapeActionItem)


class J3DAnimationRegistry(PropertyGroup):
    animations: CollectionProperty(type=J3DAnimationItem)
    active_index: IntProperty(default=0, min=0, update=_active_changed,
                              options=set())


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def _draw_prop_row(layout, data, prop, label, factor=0.3):
    """Label + property field."""
    row = layout.split(factor=factor)
    row.alignment = "RIGHT"
    row.label(text=label)
    row.prop(data, prop, text="")


class BMD_UL_animations(UIList):
    bl_idname = "BMD_UL_animations"

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag=0):
        entry = item
        row = layout.row(align=True)
        row.prop(entry, "name", text="", emboss=False)

        for fmt, icon_name in FORMAT_ICONS.items():
            if entry_has_format(entry, fmt):
                row.label(text="", icon=icon_name)

        frames = row.row(align=True)
        frames.alignment = "RIGHT"
        frames.label(text=f"{entry.frame_start}..{entry.frame_end}")


class BMD_OT_animation_new(Operator):
    bl_idname = "bmd_shape_keys.animation_new"
    bl_label = "New Animation"
    bl_description = "Create an empty animation for the selected mesh or armature"
    bl_options = {"UNDO"}

    def execute(self, context):
        owner = context_owner(context)

        if owner is None:
            self.report({"ERROR"}, "Select a mesh or armature")
            return {"CANCELLED"}

        if owner.type == "MESH":
            keys = owner.data.shape_keys if owner.data is not None else None

            if keys is None or len(keys.key_blocks) < 2:
                self.report({"ERROR"}, "The mesh has no shape keys")
                return {"CANCELLED"}

        if create_animation(context, owner) is None:
            self.report({"ERROR"}, "Could not create an animation")
            return {"CANCELLED"}

        return {"FINISHED"}


class BMD_OT_animation_copy(Operator):
    bl_idname = "bmd_shape_keys.animation_copy"
    bl_label = "Copy Animation"
    bl_description = "Duplicate the active animation"
    bl_options = {"UNDO"}

    def execute(self, context):
        owner = context_owner(context)
        registry = registry_for(owner)

        if registry is None:
            self.report({"ERROR"}, "Select a mesh or armature")
            return {"CANCELLED"}

        entry = _active_entry(registry)

        if entry is None or copy_animation(context, owner, entry) is None:
            self.report({"ERROR"}, "No animation to copy")
            return {"CANCELLED"}

        return {"FINISHED"}


class BMD_OT_animation_delete(Operator):
    bl_idname = "bmd_shape_keys.animation_delete"
    bl_label = "Delete Animation"
    bl_description = "Delete the active animation and its actions"
    bl_options = {"UNDO"}

    def execute(self, context):
        owner = context_owner(context)
        registry = registry_for(owner)

        if registry is None:
            self.report({"ERROR"}, "Select a mesh or armature")
            return {"CANCELLED"}

        entry = _active_entry(registry)

        if entry is None or not delete_animation(context, owner, entry):
            self.report({"ERROR"}, "No animation to delete")
            return {"CANCELLED"}

        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Scan / default animation
# ---------------------------------------------------------------------------

def _is_bone_action(action):
    try:
        return any("pose.bones[" in curve.data_path
                   for curve in action.fcurves)
    except Exception:
        return False


def _action_belongs_to(action, owner):
    if action is None or owner is None:
        return False

    animation_data = owner.animation_data

    if animation_data is not None and animation_data.action is action:
        return True

    stored = action.get("j3d_owner")

    if stored:
        return str(stored) == owner.name

    slots = list(getattr(action, "slots", ()))

    if not slots:
        return True

    identifier = f"OB{owner.name}"
    legacy = False

    for slot in slots:
        if slot.target_id_type == "OBJECT" and slot.identifier == identifier:
            return True

        if (slot.identifier in {"OBLegacy Slot", "XXLegacy Slot"}
                or slot.target_id_type == "UNSPECIFIED"):
            legacy = True

    if legacy:
        armatures = [obj for obj in bpy.data.objects
                     if obj.type == "ARMATURE"]
        return len(armatures) == 1 and armatures[0] is owner

    return False


def _action_range(action):
    start, end = action.frame_range
    return int(math.floor(start)), int(math.ceil(end))


def _scan_entry_name(registry, action):
    """Return the clip name for an action found while scanning."""
    name = action.get("j3d_animation_name")

    if name:
        return str(name)

    source = action.get("bmd_source")

    if source:
        return os.path.splitext(os.path.basename(str(source)))[0]

    # Unregistered actions: the first one forms the default "animation" clip.
    if not registry.animations:
        return "animation"

    return action.name


def scan_actions(context, owner, select=True):
    """Add existing armature and shape-key actions to the registry."""
    global _SYNCING, _LAST_SYNC

    if owner is None or not bc.is_bmd_object(owner):
        return 0

    registry = registry_for(owner)

    if registry is None:
        return 0

    _LAST_SYNC = None
    migrate_registry(owner)
    ensure_default_animation(context, owner, select=False)
    count = 0
    had_entries = bool(registry.animations)

    if owner.type == "ARMATURE":
        for action in bpy.data.actions:
            if not _has_keys(action):
                continue

            if not _is_bone_action(action):
                continue

            if not _action_belongs_to(action, owner):
                continue

            name = _scan_entry_name(registry, action)
            entry = find_animation(registry, name)

            if entry is None:
                start, end = _action_range(action)
                entry = _new_entry(registry, name, start, end,
                                   loop_mode_name(action))

            _set_track(entry, FORMAT_BCK, owner.name, action.name)
            action.use_fake_user = True
            _tag_action(action, FORMAT_BCK, owner.name, entry.name)

            count += 1

        meshes = [obj for obj in bpy.data.objects
                  if obj.type == "MESH" and bc.target_armature(obj) is owner]
    elif owner.type == "MESH":
        meshes = [owner]
    else:
        meshes = []

    for mesh in meshes:
        keys = _shape_keys(mesh)

        if keys is None:
            continue

        for action in blk_export._shape_key_actions(keys):
            if not _has_keys(action):
                continue

            name = _scan_entry_name(registry, action)
            entry = find_animation(registry, name)

            if entry is None:
                start, end = _action_range(action)
                entry = _new_entry(registry, name, start, end,
                                   loop_mode_name(action))

            _set_track(entry, FORMAT_BLK, mesh.name, action.name)
            action.use_fake_user = True
            _tag_action(action, FORMAT_BLK, mesh.name, entry.name)

            count += 1

    sort_animations(registry)

    if count and not had_entries and registry.animations:
        _SYNCING = True

        try:
            registry.active_index = 0
        finally:
            _SYNCING = False

    return count


def ensure_default_animation(context, owner, select=True):
    """Create an ``animation`` entry for the active, unregistered actions."""
    migrate_registry(owner)
    registry = registry_for(owner)

    if registry is None or registry.animations:
        return None

    targets = [
        (fmt, target, action)
        for fmt, target, _action_owner, action in _format_targets(owner)
        if _has_keys(action)
    ]

    if not targets:
        return None

    # Old imported actions keep their file-derived names for scan_actions().
    if any(action.get("bmd_source") for _fmt, _target, action in targets):
        return None

    entry = _new_entry(registry, "animation", _action_range(targets[0][2])[0],
                       _action_range(targets[0][2])[1],
                       loop_mode_name(targets[0][2]))

    for fmt, target, action in targets:
        action_name = _set_action_name(action, entry, registry, fmt, target)
        _set_track(entry, fmt, target, action_name)
        action.use_fake_user = True
        _tag_action(action, fmt, target, entry.name)

    if select:
        _select_entry(registry, entry)

    return entry


def _tag_redraw():
    screen = getattr(bpy.context, "screen", None)

    if screen is None:
        return

    for area in screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


# ---------------------------------------------------------------------------
# Lazy track attachment
# ---------------------------------------------------------------------------

def _attach_active_actions(entry, owner, registry):
    changed = False

    for fmt, target, _action_owner, action in _format_targets(owner):
        if not _has_keys(action):
            continue

        if _action_used_elsewhere(registry, action.name, exclude=entry):
            continue

        if not action.use_fake_user:
            action.use_fake_user = True

        _tag_action(action, fmt, target, entry.name)

        track = _track_for(entry, fmt, target)

        if track is None or track.action_name != action.name:
            action_name = _set_action_name(action, entry, registry, fmt,
                                           target)

            if track is None:
                _set_track(entry, fmt, target, action_name)
                changed = True
            elif track.action_name != action_name:
                track.action_name = action_name
                changed = True

    return changed


def _sync_active_entry():
    global _SYNC_SCHEDULED, _LAST_SYNC, _LAST_TARGET

    _SYNC_SCHEDULED = False
    context = bpy.context
    owner = context_owner(context)
    target = owner.as_pointer() if owner is not None else 0
    target_changed = target != _LAST_TARGET
    _LAST_TARGET = target
    registry = registry_for(owner)

    if registry is None:
        _LAST_SYNC = None
        return None

    if not registry.animations:
        if ensure_default_animation(context, owner, select=False) is None:
            _LAST_SYNC = None
            return None

        _LAST_SYNC = None
        _tag_redraw()

    entry = _active_entry(registry)

    if entry is None:
        _LAST_SYNC = None
        return None

    signature = (
        owner.name,
        tuple((fmt, target, action.name if action else None,
               _has_keys(action))
              for fmt, target, _action_owner, action
              in _format_targets(owner)),
    )

    if signature == _LAST_SYNC:
        if target_changed:
            _apply_range(context, entry)
        return None

    _LAST_SYNC = signature
    _attach_active_actions(entry, owner, registry)

    if _prune_tracks(entry, registry, owner):
        _tag_redraw()

    # Switching to another mesh/armature applies that target's active clip
    # range, just like choosing the clip from the Animations list.
    if target_changed:
        _apply_range(context, entry)

    return None


@bpy.app.handlers.persistent
def _depsgraph_update_post(_scene, _depsgraph):
    global _SYNC_SCHEDULED

    if _SYNCING:
        return

    if _SYNC_SCHEDULED and not bpy.app.timers.is_registered(_sync_active_entry):
        _SYNC_SCHEDULED = False

    if _SYNC_SCHEDULED:
        return

    _SYNC_SCHEDULED = True
    bpy.app.timers.register(_sync_active_entry, first_interval=0.05)


@bpy.app.handlers.persistent
def _load_post_ensure_animations(*_args):
    global _SYNC_SCHEDULED

    _SYNC_SCHEDULED = False

    # Python handlers are dropped on file load unless persistent; re-attach
    # in case this module was registered by an older build or a reload.
    if _depsgraph_update_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_update_post)

    context = bpy.context

    for obj in bpy.data.objects:
        if not bc.is_bmd_object(obj):
            continue

        if obj.type == "ARMATURE":
            scan_actions(context, obj, select=False)
        elif obj.type == "MESH" and bc.target_armature(obj) is None:
            scan_actions(context, obj, select=False)


def _scan_all_registries():
    """Scan and migrate after enabling the addon on an already open scene."""
    try:
        _load_post_ensure_animations()
        _tag_redraw()
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_properties():
    global _LAST_SYNC, _LAST_TARGET, _SYNC_SCHEDULED

    _LAST_SYNC = None
    _LAST_TARGET = None
    _SYNC_SCHEDULED = False
    bpy.types.Object.j3d_animations = PointerProperty(
        type=J3DAnimationRegistry)

    if _load_post_ensure_animations not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post_ensure_animations)

    if _depsgraph_update_post not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_update_post)

    if not bpy.app.timers.is_registered(_scan_all_registries):
        bpy.app.timers.register(_scan_all_registries, first_interval=0.5)


def unregister_properties():
    global _LAST_SYNC, _LAST_TARGET, _SYNC_SCHEDULED

    _LAST_SYNC = None
    _LAST_TARGET = None
    _SYNC_SCHEDULED = False
    _SORT_PENDING.clear()

    try:
        if bpy.app.timers.is_registered(_sync_active_entry):
            bpy.app.timers.unregister(_sync_active_entry)
    except Exception:
        pass

    if hasattr(bpy.types.Object, "j3d_animations"):
        del bpy.types.Object.j3d_animations

    try:
        if bpy.app.timers.is_registered(_scan_all_registries):
            bpy.app.timers.unregister(_scan_all_registries)
    except Exception:
        pass

    try:
        bpy.app.handlers.load_post.remove(_load_post_ensure_animations)
    except ValueError:
        pass

    try:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_update_post)
    except ValueError:
        pass


CLASSES = (
    J3DShapeActionItem,
    J3DAnimationTrack,
    J3DAnimationItem,
    J3DAnimationRegistry,
    BMD_UL_animations,
    BMD_OT_animation_new,
    BMD_OT_animation_copy,
    BMD_OT_animation_delete,
)
