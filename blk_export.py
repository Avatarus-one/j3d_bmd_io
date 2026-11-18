"""Blender-side export of J3D shape-key animations (``.blk``)."""

from __future__ import annotations

import math

import bpy

from . import blender_common as bc
from .j3d import anim, blk


class BlkExportError(Exception):
    pass


def _shape_key_name(path):
    prefix = 'key_blocks["'
    suffix = '"].value'

    if path.startswith(prefix) and path.endswith(suffix):
        return path[len(prefix):-len(suffix)]

    return None


def _action_key_slot(action, keys):
    """Return the action slot that drives *keys*, or the first key slot."""
    if action is None or keys is None:
        return None

    slots = list(getattr(action, "slots", ()))
    identifier = f"KE{keys.name}"

    for slot in slots:
        if slot.target_id_type == "KEY" and slot.identifier == identifier:
            return slot

    for slot in slots:
        if slot.target_id_type == "KEY":
            return slot

    return None


def _action_targets_keys(action, keys):
    """True when *action* animates the shape keys of *keys*."""
    if action is None or keys is None:
        return False

    slot = _action_key_slot(action, keys)

    if slot is not None and slot.identifier == f"KE{keys.name}":
        return True

    names = {block.name for block in keys.key_blocks[1:]}

    try:
        curves = action.fcurves
    except Exception:  # actions without a legacy single-slot view
        return False

    for curve in curves:
        if _shape_key_name(curve.data_path) in names:
            return True

    return False


def _shape_key_actions(keys):
    """Return the shape-key actions of *keys*, active action first."""
    if keys is None:
        return []

    found = []
    seen = set()

    def add(action):
        if action is None or action.name in seen:
            return

        if not _has_shape_key_tracks(action):
            return

        if not _action_targets_keys(action, keys):
            return

        seen.add(action.name)
        found.append(action)

    animation_data = keys.animation_data

    if animation_data is not None:
        add(animation_data.action)

        for track in animation_data.nla_tracks:
            for strip in track.strips:
                add(strip.action)

    for action in sorted(bpy.data.actions, key=lambda item: item.name.lower()):
        add(action)

    return found


def _action_frame_range(action):
    """Return the inclusive frame range shown in the animation list."""
    if action is None:
        return 0, 0

    start, end = action.frame_range
    start, end = int(math.floor(start)), int(math.ceil(end))

    if start == end and "bmd_duration" in action:
        start = min(start, 0)
        end = max(end, int(action["bmd_duration"]))

    return start, end


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
               custom_range=None, action=None):
    """Export the shape-key animation of *target* to *filepath*.

    *key_names* selects which shape keys are animated; other keys are skipped.
    *custom_range* is ``(start, end)`` for ``frame_source="CUSTOM"``.
    *action* selects the animation to sample; the active action is used when
    it is omitted.
    """
    obj = target if target is not None else find_mesh(context)

    if obj is None or obj.type != "MESH":
        raise BlkExportError("Select the mesh with shape keys first")

    keys = obj.data.shape_keys if obj.data else None

    if keys is None or len(keys.key_blocks) < 2:
        raise BlkExportError("The mesh has no shape keys")

    animation_data = keys.animation_data

    if action is None:
        action = animation_data.action if animation_data is not None else None

    if not _has_shape_key_tracks(action):
        raise BlkExportError("The shape keys have no animation tracks")

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
    previous_action = None
    previous_slot = None
    switched = False

    if action is not None:
        if animation_data is None:
            animation_data = keys.animation_data_create()

        if animation_data.action is not action:
            previous_action = animation_data.action
            previous_slot = getattr(animation_data, "action_slot", None)
            animation_data.action = action
            slot = _action_key_slot(action, keys)

            if slot is not None and hasattr(animation_data, "action_slot"):
                animation_data.action_slot = slot

            switched = True

    try:
        for name, block in selected_targets:
            values = []

            for frame in frames:
                scene.frame_set(frame)
                values.append(float(block.value))

            sampled.append((name, block, values))
    finally:
        scene.frame_set(current)

        if switched:
            animation_data.action = previous_action

            if (previous_slot is not None
                    and hasattr(animation_data, "action_slot")):
                animation_data.action_slot = previous_slot

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
    bc.remember_export(action or obj, "blk", filepath)
    if obj is not None and obj is not action:
        bc.remember_export(obj, "blk", filepath)

    if report:
        parts = [
            f"Exported BLK: {len(tracks)} tracks",
            f"{len(frames)} frames",
            f"{len(data)} bytes",
        ]

        report({"INFO"}, ", ".join(parts))

    return animation


def _has_shape_key_tracks(action):
    """True when *action* animates at least one shape-key value."""
    if action is None:
        return False

    try:
        curves = action.fcurves
    except Exception:  # actions without a legacy single-slot view
        return False

    for fcurve in curves:
        path = fcurve.data_path
        if "key_blocks[" in path and path.endswith(".value"):
            return True

    return False
