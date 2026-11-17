"""Import / export of J3D joint animations (.bck) as Blender actions.

Mapping
-------
A J3D animation replaces the local transform ``L(t) = T * R * S`` of every joint.
Blender stores the *difference* to the rest pose in ``PoseBone.matrix_basis``, with

    pose_i = pose_parent @ (rest_parent^-1 @ rest_i) @ basis_i

so, with ``Lb(t)`` the joint transform converted to Blender's axes and bone frame,

    basis_i(t) = (rest_parent^-1 @ rest_i)^-1 @ Lb(t)

Export inverts this by sampling ``matrix_basis`` for every frame of the action.
"""

import math
import os

import bpy
import numpy as np

from . import blender_common as bc
from .blender_export import (joint_index_owners, ordered_bones,
                             remap_joint_references)
from .j3d import anim
from .j3d.model import matrix_to_euler


class AnimError(Exception):
    pass


LOOP_ITEMS = [
    ("AUTO", "Auto", "Use the loop mode stored on the action (imported files), otherwise Loop"),
    ("ONCE", "Once", "Play once and hold the last frame"),
    ("ONCE_RESET", "Once and Reset", "Play once and return to the first frame"),
    ("LOOP", "Loop", "Repeat forever"),
    ("MIRROR_ONCE", "Mirror Once", "Play forwards and backwards once"),
    ("MIRROR_LOOP", "Mirror Loop", "Play forwards and backwards forever"),
]


def find_armature(context):
    obj = context.active_object
    if obj is not None and obj.type == "ARMATURE":
        return obj
    for o in context.selected_objects:
        if o.type == "ARMATURE":
            return o
    raise AnimError("Select the imported armature first")


def _rest_chain_unchanged(bone):
    """True when the bone and all of its ancestors still match the import."""
    rest = bone.get("bmd_rest")
    if rest is None or len(rest) != 16:
        return False
    parent_index = bone.get("bmd_parent")
    if parent_index is not None:
        if bone.parent is None:
            if int(parent_index) >= 0:
                return False
        else:
            parent_bmd = bone.parent.get("bmd_index")
            if parent_bmd is None or int(parent_bmd) != int(parent_index):
                return False
    stored = np.array([float(x) for x in rest], dtype=np.float64)
    current = bc.to_np(bone.matrix_local).reshape(-1)
    if not np.allclose(current, stored, atol=1e-5, rtol=0.0):
        return False
    return bone.parent is None or _rest_chain_unchanged(bone.parent)


def rest_local(bone, fix, scale=1.0):
    """Rest transform of a bone relative to its parent (armature space for roots).

    Blender bones carry no scale, but J3D joints can (e.g. 1.6 on eyebrows).
    For an unchanged imported bone the stored ``bmd_local`` is used directly:
    it contains the exact joint scale, including negative (mirrored) scales
    that Blender's edit-bone matrix cannot represent.  For an edited bone the
    current Blender rest matrix is used and the magnitudes of the stored scale
    are folded back in.
    """
    local = bone.get("bmd_local")
    if local is not None and len(local) >= 9 and _rest_chain_unchanged(bone):
        L = bc.trs_matrix(local[6:9], local[3:6], local[0:3])
        L[:3, 3] *= scale
        if bone.parent is None:
            return bc.C @ L @ bc.C_INV @ fix
        return fix.T @ bc.C @ L @ bc.C_INV @ fix

    M = bc.to_np(bone.matrix_local)
    if bone.parent is not None:
        M = np.linalg.inv(bc.to_np(bone.parent.matrix_local)) @ M
    if local is not None and len(local) >= 9:
        S = np.eye(4)
        # The bone rest matrix keeps the sign of a negative scale but not its
        # magnitude.  Fold in the magnitudes only, otherwise the sign would be
        # applied twice.
        S[:3, :3] = np.diag([abs(float(x)) for x in local[6:9]])
        M = M @ (fix.T @ bc.C @ S @ bc.C_INV @ fix)
    return M


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

def _bone_joint_map(arm_obj, njoints, report=None):
    """Map BCK joint indices to bones, tolerating duplicates and stale indices.

    Duplicated bones copy ``bmd_index`` from the original.  The bone whose name
    is still listed in ``bmd_bone_names`` keeps the track; the duplicate is left
    at rest and reported.  This keeps a BCK import working after any skeleton
    edit instead of silently animating the wrong bone.
    """
    names = [str(n) for n in arm_obj.get("bmd_bone_names", [])]
    bones, duplicates = joint_index_owners(arm_obj.data, names)

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
            label = ", ".join(str(i) for i in stale[:8])
            if len(stale) > 8:
                label += ", ..."
            report({"WARNING"},
                   f"Bones with joint indices outside the animation "
                   f"(0..{njoints - 1}) are ignored: {label}")
        if missing:
            report({"WARNING"},
                   f"Animation has {len(missing)} joints without a matching bone; "
                   "their tracks are ignored")
    return bones


def import_bck(context, filepath, scale=0.01, set_scene_range=True, report=None,
               set_fps=True):
    with open(filepath, "rb") as f:
        bck = anim.parse_bck(f.read())
    arm_obj = find_armature(context)
    s = float(scale)
    fix = bc.axis_fix(arm_obj)

    if set_fps:
        context.scene.render.fps = 30
        context.scene.render.fps_base = 1.0

    njoints = len(bck.tracks)
    bones = _bone_joint_map(arm_obj, njoints, report)
    if not bones:
        raise AnimError("The armature has no imported J3D joint data (bmd_index)")
    if not any(j < njoints for j in bones):
        raise AnimError(
            "No bone matches the animation's joint indices; import the model first")

    frames = np.arange(0, bck.duration + 1)
    samples = anim.sample(bck, frames)  # (joints, 9, frames)

    name = os.path.splitext(os.path.basename(filepath))[0]
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    action["bmd_loop_mode"] = int(bck.loop_mode)
    action["bmd_duration"] = int(bck.duration)
    ad = arm_obj.animation_data or arm_obj.animation_data_create()
    ad.action = action

    cyclic = bck.loop_mode == anim.LOOP_LOOP
    sheared = []
    for j in range(njoints):
        bone = bones.get(j)
        if bone is None:
            continue
        shear = _key_joint(arm_obj, bone, samples[j], frames, s, fix, cyclic=cyclic)
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
        report({"INFO"}, f"Imported animation '{name}': {bck.duration + 1} frames, "
                         f"loop mode {anim.LOOP_NAMES[bck.loop_mode] if bck.loop_mode < 5 else bck.loop_mode}")
    return action


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
        kp.handle_left = (frame - dt_prev / 3.0, value - tin * dt_prev / 3.0)
        kp.handle_right = (frame + dt_next / 3.0, value + tout * dt_next / 3.0)
    fcurve.update()


def _key_joint(arm_obj, bone, smp, frames, s, fix, cyclic=False):
    pb = arm_obj.pose.bones[bone.name]
    rest_inv = np.linalg.inv(rest_local(bone, fix, s))
    n = len(frames)
    loc, quat, scl = np.zeros((n, 3)), np.zeros((n, 4)), np.zeros((n, 3))
    shear = 0.0
    for i in range(n):
        L = bc.trs_matrix(smp[[0, 3, 6], i], smp[[1, 4, 7], i], smp[[2, 5, 8], i])
        basis = rest_inv @ bc.j3d_local_to_blender(L, s, fix, bone.parent is None)
        R, sc, sh = bc.split_trs(basis[:3, :3])
        loc[i] = basis[:3, 3]
        quat[i] = bc.matrix_to_quat(R)
        scl[i] = sc
        shear = max(shear, sh)
        if i and np.dot(quat[i], quat[i - 1]) < 0:  # keep the quaternion on the same hemisphere
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


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def _continuous_euler(eul):
    """Make per-frame Euler angles (joints, 3, frames) continuous over time.

    Every rotation has two Euler representations, (x, y, z) and (x+pi, pi-y, z+pi); pick the one
    closest to the previous frame, then unwrap the 2*pi jumps.
    """
    out = eul.copy()
    for j in range(out.shape[0]):
        for f in range(1, out.shape[2]):
            prev = out[j, :, f - 1]
            a = out[j, :, f]
            alt = np.array([a[0] + math.pi, math.pi - a[1], a[2] + math.pi])

            def dist(v):
                d = (v - prev + math.pi) % (2 * math.pi) - math.pi
                return float(np.abs(d).sum())

            out[j, :, f] = alt if dist(alt) < dist(a) else a
        out[j] = np.unwrap(out[j], axis=1)
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
    unchanged = {b.name: _rest_chain_unchanged(b) for b in ordered}
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
               frame_source="ACTION", report=None, custom_range=None):
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
    extra_root = sum(1 for b in ordered if b.parent is None) > 1  # model export adds a root joint
    rests = [rest_local(b, fix, s) for b in ordered]
    corrections = []
    for b, exact in zip(ordered, rests):
        blender_rest = bc.to_np(b.matrix_local)
        if b.parent is not None:
            blender_rest = np.linalg.inv(bc.to_np(b.parent.matrix_local)) @ blender_rest
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

    scene = context.scene
    current = scene.frame_current
    current_subframe = scene.frame_subframe
    sheared = set()
    try:
        for fi, f in enumerate(frames):
            scene.frame_set(f)
            for j, bone in enumerate(ordered):
                pb = arm_obj.pose.bones[bone.name]
                pose = bc.to_np(pb.matrix)
                if pb.parent is not None:
                    parent_pose = bc.to_np(pb.parent.matrix)
                    local_pose = np.linalg.inv(parent_pose) @ pose
                else:
                    local_pose = pose
                Lb = corrections[j] @ local_pose
                L = bc.blender_local_to_j3d(Lb, s, fix, bone.parent is None)
                R, sc, shear_l = bc.split_trs(L[:3, :3])
                if shear_l > 1e-4:
                    sheared.add(bone.name)
                eul = matrix_to_euler(R)
                smp[j, [0, 3, 6], fi] = sc
                smp[j, [1, 4, 7], fi] = eul
                smp[j, [2, 5, 8], fi] = L[:3, 3]
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

    tolerance = {anim.KIND_SCALE: 1e-4, anim.KIND_ROT: 2e-4, anim.KIND_TRANS: 0.01}
    bck = anim.Bck()
    bck.loop_mode = mode
    bck.duration = len(frames) - 1
    cyclic = mode == anim.LOOP_LOOP
    for j in range(nj):
        bck.tracks.append([anim.fit_track(smp[j, k], tolerance[k % 3],
                                          cyclic=cyclic) for k in range(9)])
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
        report({"INFO"}, f"Exported {len(frames)} frames for {nj} joints ({keys} keys, "
                         f"{len(data) // 1024 + 1} KiB)")
    return bck
