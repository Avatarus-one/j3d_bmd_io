"""Shared Blender-side helpers for J3D joint animations (``.bck``).

The BCK importer and exporter share the armature lookup, loop-mode list and
rest-pose conversion.  Keeping them here avoids importing the UI modules from
the format logic.
"""

from __future__ import annotations

import numpy as np

from . import blender_common as bc


class AnimError(Exception):
    pass


LOOP_ITEMS = [
    ("AUTO", "Auto",
     "Use the loop mode stored on the action (imported files), otherwise Loop"),
    ("ONCE", "Once", "Play once and hold the last frame"),
    ("ONCE_RESET", "Once and Reset", "Play once and return to the first frame"),
    ("LOOP", "Loop", "Repeat forever"),
    ("MIRROR_ONCE", "Mirror Once", "Play forwards and backwards once"),
    ("MIRROR_LOOP", "Mirror Loop", "Play forwards and backwards forever"),
]


def find_armature(context):
    """Return the armature selected directly or through a mesh."""
    obj = context.active_object

    if obj is not None:
        if obj.type == "ARMATURE":
            return obj

        if obj.type == "MESH":
            arm = bc.target_armature(obj)
            if arm is not None:
                return arm

    for obj in context.selected_objects:
        if obj.type == "ARMATURE":
            return obj

    for obj in context.selected_objects:
        if obj.type == "MESH":
            arm = bc.target_armature(obj)
            if arm is not None:
                return arm

    raise AnimError("Select the imported armature or its mesh first")


def rest_chain_unchanged(bone):
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

    return bone.parent is None or rest_chain_unchanged(bone.parent)


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

    if local is not None and len(local) >= 9 and rest_chain_unchanged(bone):
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
