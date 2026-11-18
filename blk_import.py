"""Blender-side import of J3D shape-key animations (``.blk``)."""

from __future__ import annotations

import math
import os

import bpy
import numpy as np

from . import blender_common as bc
from . import bls_import
from . import bls_session
from .j3d import anim


class BlkImportError(Exception):
    pass


def _selected_mesh(context):
    """Return the active mesh, the first selected mesh, or None."""
    obj = context.active_object

    if obj is not None and obj.type == "MESH":
        return obj

    for candidate in context.selected_objects:
        if candidate.type == "MESH":
            return candidate

    return None


def find_mesh(context):
    """Return the active mesh, or the first selected mesh."""
    obj = _selected_mesh(context)

    if obj is None:
        raise BlkImportError("Select the mesh with shape keys first")

    return obj


def _shape_keys(obj):
    if obj is None or obj.type != "MESH" or obj.data is None:
        raise BlkImportError("No mesh selected")

    keys = obj.data.shape_keys

    if keys is None or len(keys.key_blocks) < 2:
        raise BlkImportError("The mesh has no shape keys")

    return keys


def _delta_scale(target):
    return 0.01 if bc.saved_convert_units(bpy.context, target) else 1.0


def _bls_session_for(target, path):
    if target is None or not path:
        return None, "BLS file not found"
    path = bpy.path.abspath(path)
    if not os.path.isfile(path):
        return None, "BLS file not found"
    try:
        existing = [
            bls_session.ExistingKey(name, displacement)
            for name, displacement in
            bls_import.existing_shape_key_displacements(target)
        ]
        session = bls_session.build_session(
            [path],
            recursive=False,
            existing_keys=existing,
            vertex_count=len(target.data.vertices) if target.data else 0,
            delta_scale=_delta_scale(target),
        )
    except Exception as exc:
        return None, str(exc)
    if not session.files:
        return None, "No BLS data"
    file_entry = session.files[0]
    if file_entry.error:
        return None, file_entry.error
    return session, ""


def _animated_keys(file_entry):
    return [key for key in file_entry.keys
            if not key.is_base and not key.is_original]


def _mesh_bls_key_info(mesh, path, cache=None):
    """Return ``(key_count, keys_fit)`` for *mesh* and its BLS file."""
    if mesh is None or mesh.type != "MESH" or mesh.data is None:
        return None

    path = bpy.path.abspath(str(path)) if path else ""

    if not path or not os.path.isfile(path):
        return None

    cache_key = (path, len(mesh.data.vertices))

    if cache is not None and cache_key in cache:
        return cache[cache_key]

    result = None

    try:
        session = bls_session.build_session(
            [path],
            recursive=False,
            vertex_count=len(mesh.data.vertices),
            delta_scale=_delta_scale(mesh),
        )
    except Exception:
        session = None

    if session is not None and session.files:
        file_entry = session.files[0]

        if not file_entry.error:
            keys = _animated_keys(file_entry)
            fits = not any(
                key.status == bls_session.INCOMPATIBLE for key in keys)
            result = (len(keys), fits)

    if cache is not None:
        cache[cache_key] = result

    return result


def collect_target_candidates(context, selected=None):
    """Return meshes with a usable saved BLS, selected mesh first."""
    cache = {}
    candidates = []
    seen = set()

    def add(mesh):
        if mesh is None or mesh.type != "MESH" or mesh in seen:
            return

        source = mesh.get("bmd_bls_source", "")

        if not source:
            return

        info = _mesh_bls_key_info(mesh, source, cache)

        if info is None or not info[1]:
            return

        seen.add(mesh)
        candidates.append(
            bc.TargetCandidate(
                mesh, mesh, info[0], bpy.path.abspath(str(source))))

    add(selected)

    for obj in context.scene.objects:
        if obj.type == "MESH":
            add(obj)

    return candidates


def find_exact_target(track_count, candidates):
    """Return the first candidate with exactly *track_count* keys, or None.

    The candidate list can be collected once for a batch import; this keeps
    the exact-count rule while avoiding repeated scene/BLS scans.
    """
    for candidate in candidates:
        if candidate.capacity == track_count:
            return candidate

    return None


def find_compatible_mesh(context, track_count, selected=None):
    """Return the mesh whose saved BLS has exactly *track_count* keys.

    The selected mesh wins when it matches.  Otherwise the scene is searched
    for a matching mesh; if none matches, the selected mesh (or None) is
    returned so the dialog can still show its status.
    """
    candidate = find_exact_target(
        track_count, collect_target_candidates(context, selected))

    if candidate is not None:
        return candidate.object

    return selected


def _key_target_name(key):
    if key.status == bls_session.CONFLICT:
        return key.name
    return key.new_name or key.name


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

    # BLK playback is authored at the game's 30 fps.
    context.scene.render.fps = 30
    context.scene.render.fps_base = 1.0

    # Drop slider ranges left over from a previous animation; only the new
    # track values may expand them below.
    for block in keys.key_blocks[1:]:
        block.slider_min = 0.0
        block.slider_max = 1.0

    animation_data = keys.animation_data or keys.animation_data_create()
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    action["bmd_loop_mode"] = int(animation.loop_mode)
    if filepath:
        action["bmd_source"] = os.path.abspath(filepath)
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
