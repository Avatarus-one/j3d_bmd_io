"""Blender-side export of J3D joint animations (``.bck``)."""

from __future__ import annotations

import math

import bpy
import numpy as np

from . import blender_common as bc
from .bck_common import (AnimError, find_armature, rest_chain_unchanged,
                         rest_local)
from .blender_export import (joint_index_owners, ordered_bones,
                             remap_joint_references)
from .j3d import anim
from .j3d.model import matrix_to_euler


def _continuous_euler(eul):
    """Make per-frame Euler angles (joints, 3, frames) continuous over time.

    Every rotation has two Euler representations, (x, y, z) and
    (x+pi, pi-y, z+pi); pick the one closest to the previous frame, then
    unwrap the 2*pi jumps.
    """
    out = eul.copy()

    if out.shape[2] > 1:
        two_pi = 2.0 * math.pi

        for f in range(1, out.shape[2]):
            prev = out[:, :, f - 1]
            current = out[:, :, f]
            alt = np.stack((current[:, 0] + math.pi,
                            math.pi - current[:, 1],
                            current[:, 2] + math.pi), axis=1)
            d_current = (current - prev + math.pi) % two_pi - math.pi
            d_alt = (alt - prev + math.pi) % two_pi - math.pi
            choose_alt = (np.abs(d_alt).sum(axis=1) <
                          np.abs(d_current).sum(axis=1))
            out[choose_alt, :, f] = alt[choose_alt]

        out = np.unwrap(out, axis=2)

    return out


def _store_bck_layout(arm_obj, ordered, rests, s, fix):
    """Store the joint layout written to the BCK on the bones.

    A later BCK import maps tracks by ``bmd_index``.  After the user adds,
    duplicates or reparents bones, the indices written by this export must be
    the ones used by the next import, so they are refreshed here.  Bones whose
    rest chain is unchanged keep their exact ``bmd_local`` (including negative
    and non-uniform joint scales); edited bones get a fresh local transform.
    Stored shape-node joint indices are remapped to the new layout as well.
    """
    index = {b.name: i for i, b in enumerate(ordered)}
    unchanged = {b.name: rest_chain_unchanged(b) for b in ordered}
    owners, _duplicates = joint_index_owners(
        arm_obj.data, arm_obj.get("bmd_bone_names", []))
    old_to_new = {old: index[bone.name] for old, bone in owners.items()}

    for b in ordered:
        b["bmd_index"] = index[b.name]
        b["bmd_parent"] = index[b.parent.name] if b.parent is not None else -1
        b["bmd_rest"] = [float(x) for x in bc.to_np(b.matrix_local).reshape(-1)]

        if not unchanged[b.name]:
            L = bc.blender_local_to_j3d(rests[index[b.name]], s, fix,
                                        b.parent is None)
            R, sc, _shear = bc.split_trs(L[:3, :3])
            b["bmd_local"] = [
                float(x) for x in (*L[:3, 3], *matrix_to_euler(R), *sc)]

    arm_obj["bmd_bone_names"] = [b.name for b in ordered]
    mesh_objs = [obj for obj in bpy.data.objects
                 if obj.type == "MESH" and bc.target_armature(obj) is arm_obj]
    remap_joint_references(old_to_new, mesh_objs)


def export_bck(context, filepath, scale=100.0, loop_mode="ONCE",
               frame_source="ACTION", report=None, custom_range=None,
               target=None):
    if target is not None:
        arm_obj = (target if target.type == "ARMATURE"
                   else bc.target_armature(target))
        if arm_obj is None:
            raise AnimError("The selected target has no armature")
    else:
        arm_obj = find_armature(context)

    ad = arm_obj.animation_data
    action = ad.action if ad else None

    if action is None:
        raise AnimError("The armature has no active action to export")

    s = 1.0 / float(scale)
    fix = bc.axis_fix(arm_obj)
    ordered = ordered_bones(arm_obj.data, arm_obj.get("bmd_bone_names", []))

    if not ordered:
        raise AnimError("The armature has no bones")

    # Model export adds a root joint when there are several root bones.
    extra_root = sum(1 for b in ordered if b.parent is None) > 1
    rests = [rest_local(b, fix, s) for b in ordered]
    corrections = []

    for b, exact in zip(ordered, rests):
        blender_rest = bc.to_np(b.matrix_local)

        if b.parent is not None:
            blender_rest = (np.linalg.inv(bc.to_np(b.parent.matrix_local))
                            @ blender_rest)

        corrections.append(exact @ np.linalg.inv(blender_rest))

    if frame_source == "CUSTOM":
        if custom_range is None:
            raise AnimError("Custom frame range is not set")

        start, end = int(custom_range[0]), int(custom_range[1])
    elif frame_source == "SCENE":
        start, end = context.scene.frame_start, context.scene.frame_end
    else:
        f0, f1 = action.frame_range
        start, end = int(math.floor(f0)), int(math.ceil(f1))

        if start == end and "bmd_duration" in action:
            # A completely static clip has one key at frame 0; keep the
            # duration that was imported instead of collapsing to one frame.
            start = min(start, 0)
            end = max(end, int(action["bmd_duration"]))

    if end < start:
        raise AnimError("End frame must not be before start frame")

    if end - start > 0xFFFF:
        raise AnimError(
            f"The frame range {start}..{end} is too long for a BCK "
            "(at most 65536 frames)")

    frames = list(range(start, end + 1))
    nj = len(ordered) + (1 if extra_root else 0)
    smp = np.zeros((nj, 9, len(frames)))
    smp[:, [0, 3, 6]] = 1.0  # unit scale for joints without a bone

    count = len(ordered)
    corrections_arr = np.stack(corrections) if corrections else np.empty((0, 4, 4))
    is_root = np.array([b.parent is None for b in ordered], dtype=bool)
    bone_index = {b.name: i for i, b in enumerate(ordered)}

    scene = context.scene
    current = scene.frame_current
    current_subframe = scene.frame_subframe
    sheared = set()

    try:
        for fi, f in enumerate(frames):
            scene.frame_set(f)

            pose_mats = np.empty((count, 4, 4), dtype=np.float64)

            for j, bone in enumerate(ordered):
                pose_mats[j] = bc.to_np(arm_obj.pose.bones[bone.name].matrix)

            local_pose = np.empty_like(pose_mats)
            parent_inv = {}

            for j, bone in enumerate(ordered):
                if bone.parent is None:
                    local_pose[j] = pose_mats[j]
                    continue

                parent_name = bone.parent.name
                inverse = parent_inv.get(parent_name)

                if inverse is None:
                    inverse = np.linalg.inv(pose_mats[bone_index[parent_name]])
                    parent_inv[parent_name] = inverse

                local_pose[j] = inverse @ pose_mats[j]

            Lb = corrections_arr @ local_pose
            L = bc.blender_local_to_j3d_batch(Lb, s, fix, is_root)
            R, sc, shear_values = bc.split_trs_batch(L[:, :3, :3])
            eul = bc.matrix_to_euler_batch(R)

            smp[:count, 0, fi] = sc[:, 0]
            smp[:count, 3, fi] = sc[:, 1]
            smp[:count, 6, fi] = sc[:, 2]
            smp[:count, 1, fi] = eul[:, 0]
            smp[:count, 4, fi] = eul[:, 1]
            smp[:count, 7, fi] = eul[:, 2]
            smp[:count, 2, fi] = L[:, 0, 3]
            smp[:count, 5, fi] = L[:, 1, 3]
            smp[:count, 8, fi] = L[:, 2, 3]

            for j in np.nonzero(shear_values > 1e-4)[0]:
                sheared.add(ordered[int(j)].name)
    finally:
        scene.frame_set(current, subframe=current_subframe)

    if sheared and report:
        names = ", ".join(sorted(sheared)[:8])

        if len(sheared) > 8:
            names += ", ..."

        report({"WARNING"},
               "Animation is approximate for joints with a non-uniform rest "
               "scale (Blender pose bones cannot store shear): " + names)

    smp[:, [1, 4, 7], :] = _continuous_euler(smp[:, [1, 4, 7], :])

    if loop_mode == "AUTO":
        mode = int(action.get("bmd_loop_mode", anim.LOOP_LOOP))
    else:
        try:
            mode = anim.LOOP_NAMES.index(loop_mode)
        except ValueError:
            raise AnimError(f"Unknown loop mode: {loop_mode}")

    tolerance = {
        anim.KIND_SCALE: 1e-4,
        anim.KIND_ROT: 2e-4,
        anim.KIND_TRANS: 0.01,
    }
    bck = anim.Bck()
    bck.loop_mode = mode
    bck.duration = len(frames) - 1
    cyclic = mode == anim.LOOP_LOOP

    for j in range(nj):
        bck.tracks.append([
            anim.fit_track(smp[j, k], tolerance[k % 3], cyclic=cyclic)
            for k in range(9)
        ])

    data = anim.write_bck(bck)

    with open(filepath, "wb") as f:
        f.write(data)

    _store_bck_layout(arm_obj, ordered, rests, s, fix)

    try:
        action["bmd_loop_mode"] = mode
    except Exception:
        pass

    if report:
        keys = sum(len(t.keys) for jt in bck.tracks for t in jt)
        report({"INFO"},
               f"Exported {len(frames)} frames for {nj} joints "
               f"({keys} keys, {len(data) // 1024 + 1} KiB)")

    return bck
