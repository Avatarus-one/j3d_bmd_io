"""Blender-side import of J3D joint animations (``.bck``)."""

from __future__ import annotations

import os

import bpy
import numpy as np

from . import blender_common as bc
from .bck_common import AnimError, find_armature, rest_local
from .blender_export import joint_index_owners, ordered_bones
from .j3d import anim


def _armature_for(obj):
    """Return the armature that receives a BCK, or None."""
    if obj is None:
        return None
    if obj.type == "ARMATURE":
        return obj
    if obj.type == "MESH":
        return bc.target_armature(obj)
    return None


def _armature_joint_count(arm):
    """Joint count used for BCK compatibility.

    Imported BMD armatures keep the original joint list in ``bmd_bone_names``;
    bones added or removed later must not change compatibility.  A from-scratch
    armature has no such list, so its current bone count is used.
    """
    if arm is None:
        return 0

    names = arm.get("bmd_bone_names", [])
    if names:
        return len(names)

    return len(arm.data.bones)


def _selected_object(context):
    obj = context.active_object

    if obj is not None:
        return obj

    for candidate in context.selected_objects:
        return candidate

    return None


def collect_target_candidates(context):
    """Return one candidate per armature in the scene.

    The selected object is first, so it wins ties against other meshes that
    share its armature.  Meshes are preferred over bare armatures, keeping the
    Target Mesh field meaningful.
    """
    selected = _selected_object(context)
    selected_arm = _armature_for(selected)
    seen = set()
    candidates = []

    def add(obj, arm):
        if arm is None or arm in seen:
            return
        seen.add(arm)
        candidates.append(
            bc.TargetCandidate(
                obj, arm, _armature_joint_count(arm)))

    if selected is not None and selected_arm is not None:
        add(selected, selected_arm)

    for obj in context.scene.objects:
        if obj.type == "MESH":
            add(obj, bc.target_armature(obj))

    for obj in context.scene.objects:
        if obj.type == "ARMATURE":
            add(obj, obj)

    return candidates


def find_compatible_target(context, joint_count):
    """Convenience wrapper: best target for one animation in this scene."""
    candidate = bc.find_best_target(
        joint_count, collect_target_candidates(context))
    return candidate.object if candidate is not None else None


def assign_targets(animations, candidates):
    """Batch helper used by the future multi-animation importer."""
    return bc.assign_targets(animations, candidates)


def _bone_joint_map(arm_obj, njoints, report=None):
    """Map BCK joint indices to bones, tolerating duplicates and stale indices.

    Duplicated bones copy ``bmd_index`` from the original.  The bone whose name
    is still listed in ``bmd_bone_names`` keeps the track; the duplicate is left
    at rest and reported.  This keeps a BCK import working after any skeleton
    edit instead of silently animating the wrong bone.
    """
    names = [str(n) for n in arm_obj.get("bmd_bone_names", [])]
    bones, duplicates = joint_index_owners(arm_obj.data, names)

    if not bones and not names:
        # A from-scratch armature has no J3D indices: map the tracks to the
        # bones in hierarchy order (parents first), like a fresh export would.
        ordered = ordered_bones(arm_obj.data, [])
        bones = {index: bone for index, bone in enumerate(ordered)}
        duplicates = []

    stale = sorted(idx for idx in bones if idx >= njoints)
    missing = [j for j in range(njoints) if j not in bones]

    if report:
        if duplicates:
            label = ", ".join(b.name for b in duplicates[:8])

            if len(duplicates) > 8:
                label += ", ..."

            report({"WARNING"},
                   "Bones with a duplicate bmd_index are not animated; the "
                   "original bone keeps the track: " + label)

        if stale:
            label = ", ".join(
                bones[index].name if index in bones else str(index)
                for index in stale[:8]
            )

            if len(stale) > 8:
                label += ", ..."

            report({"WARNING"},
                   f"Bones outside the animation's joint range "
                   f"(0..{njoints - 1}) stay at rest: {label}")

        if missing:
            report({"WARNING"},
                   f"Animation has {len(missing)} joints without a matching "
                   "bone; their tracks are ignored")

    return bones


def _write_track_keys(action, data_path, axis, group, keys, frames):
    """Insert sparse keys and set Bezier handles so they match the BCK tangents."""
    fcurve = action.fcurves.find(data_path, index=axis)

    if fcurve is None:
        fcurve = action.fcurves.new(data_path, index=axis, action_group=group)

    frame_at = [float(frames[int(round(t))]) for t, _v, _tin, _tout in keys]

    for (t, v, _tin, _tout), frame in zip(keys, frame_at):
        kp = fcurve.keyframe_points.insert(frame, float(v), options={"FAST"})
        kp.interpolation = "BEZIER"
        kp.handle_left_type = "FREE"
        kp.handle_right_type = "FREE"

    fcurve.update()
    points = fcurve.keyframe_points

    if len(points) != len(keys):
        raise AnimError("Could not write BCK keys: duplicate frame times")

    for i, kp in enumerate(points):
        frame = frame_at[i]
        value = float(keys[i][1])
        tin = float(keys[i][2])
        tout = float(keys[i][3])

        if len(keys) == 1:
            kp.handle_left = (frame, value)
            kp.handle_right = (frame, value)
            continue

        if i > 0:
            dt_prev = frame - frame_at[i - 1]
        else:
            dt_prev = frame_at[1] - frame

        if i < len(keys) - 1:
            dt_next = frame_at[i + 1] - frame
        else:
            dt_next = frame - frame_at[i - 1]

        kp.handle_left = (frame - dt_prev / 3.0,
                          value - tin * dt_prev / 3.0)
        kp.handle_right = (frame + dt_next / 3.0,
                           value + tout * dt_next / 3.0)

    fcurve.update()


def _key_joint(arm_obj, bone, smp, frames, s, fix, cyclic=False):
    pb = arm_obj.pose.bones[bone.name]
    rest_inv = np.linalg.inv(rest_local(bone, fix, s))
    n = len(frames)
    loc, quat, scl = np.zeros((n, 3)), np.zeros((n, 4)), np.zeros((n, 3))
    shear = 0.0

    for i in range(n):
        L = bc.trs_matrix(smp[[0, 3, 6], i], smp[[1, 4, 7], i],
                          smp[[2, 5, 8], i])
        basis = rest_inv @ bc.j3d_local_to_blender(
            L, s, fix, bone.parent is None)
        R, sc, sh = bc.split_trs(basis[:3, :3])
        loc[i] = basis[:3, 3]
        quat[i] = bc.matrix_to_quat(R)
        scl[i] = sc
        shear = max(shear, sh)

        if i and np.dot(quat[i], quat[i - 1]) < 0:
            # Keep the quaternion on the same hemisphere.
            quat[i] = -quat[i]

    pb.rotation_mode = "QUATERNION"
    action = arm_obj.animation_data.action
    channels = (
        ("location", loc, 0.001 * s),
        ("rotation_quaternion", quat, 5e-5),
        ("scale", scl, 5e-5),
    )

    for prop, data, tolerance in channels:
        data_path = pb.path_from_id(prop)
        varying = np.abs(data - data[0]).max() > tolerance * 0.5

        for axis in range(data.shape[1]):
            if varying:
                track = anim.fit_track(data[:, axis], tolerance, cyclic=cyclic)
                keys = track.keys
            else:
                keys = [(0.0, float(data[0, axis]), 0.0, 0.0)]

            _write_track_keys(action, data_path, axis, bone.name, keys, frames)

    return shear


def import_bck(context, filepath, scale=0.01, set_scene_range=True, report=None,
               set_fps=True, target=None):
    with open(filepath, "rb") as f:
        bck = anim.parse_bck(f.read())

    if target is not None:
        arm_obj = (target if target.type == "ARMATURE"
                   else bc.target_armature(target))
        if arm_obj is None:
            raise AnimError("The selected target has no armature")
    else:
        arm_obj = find_armature(context)

    s = float(scale)
    fix = bc.axis_fix(arm_obj)

    if set_fps:
        context.scene.render.fps = 30
        context.scene.render.fps_base = 1.0

    njoints = len(bck.tracks)
    bones = _bone_joint_map(arm_obj, njoints, report)

    if not bones:
        raise AnimError(
            "The armature has no imported J3D joint data (bmd_index)")

    if not any(j < njoints for j in bones):
        raise AnimError(
            "No bone matches the animation's joint indices; import the model "
            "first")

    frames = np.arange(0, bck.duration + 1)
    samples = anim.sample(bck, frames)  # (joints, 9, frames)

    name = os.path.splitext(os.path.basename(filepath))[0]
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    action["bmd_loop_mode"] = int(bck.loop_mode)
    action["bmd_duration"] = int(bck.duration)
    action["bmd_source"] = os.path.abspath(filepath)
    ad = arm_obj.animation_data or arm_obj.animation_data_create()
    ad.action = action

    cyclic = bck.loop_mode == anim.LOOP_LOOP
    sheared = []

    for j in range(njoints):
        bone = bones.get(j)

        if bone is None:
            continue

        shear = _key_joint(arm_obj, bone, samples[j], frames, s, fix,
                           cyclic=cyclic)

        if shear > 1e-4:
            sheared.append(bone.name)

    # Blender 4.4+ actions use slots.  Creating F-curves directly does not bind
    # the new slot to the armature, so the action would not be evaluated.
    if hasattr(action, "slots") and len(action.slots):
        try:
            ad.action_slot = action.slots[0]
        except Exception:
            pass

    if sheared and report:
        names = ", ".join(sheared[:8])

        if len(sheared) > 8:
            names += ", ..."

        report({"WARNING"},
               "Animation is approximate for joints with a non-uniform rest "
               "scale (Blender pose bones cannot store shear): " + names)

    if set_scene_range:
        context.scene.frame_start = 0
        context.scene.frame_end = int(bck.duration)

        try:
            context.scene.frame_set(0)
        except Exception:
            pass

    if report:
        loop = (anim.LOOP_NAMES[bck.loop_mode]
                if bck.loop_mode < 5 else bck.loop_mode)
        report({"INFO"},
               f"Imported animation '{name}': {bck.duration + 1} frames, "
               f"loop mode {loop}")

    return action
