"""Shared helpers for the Blender import / export operators."""

import math
import os

import numpy as np
import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy.types import PropertyGroup

from .j3d import gx_texture as gx
from .j3d import material as mat3


def saved_convert_units(context):
    """Last Convert Units value stored on the scene (True when unset)."""
    scene = getattr(context, "scene", None)
    return bool(getattr(scene, "bmd_convert_units", True))


def store_convert_units(context, value):
    """Remember *value* as the shared Convert Units setting for this scene."""
    scene = getattr(context, "scene", None)
    if scene is not None and hasattr(scene, "bmd_convert_units"):
        scene.bmd_convert_units = bool(value)

# --- coordinate systems -------------------------------------------------------
# J3D: +Y up, +Z forward.  Blender: +Z up, -Y forward.   p_blender = C @ p_j3d
C = np.array([[1, 0, 0, 0],
              [0, 0, -1, 0],
              [0, 1, 0, 0],
              [0, 0, 0, 1]], dtype=np.float64)
C_INV = C.T.copy()

# Optional bone-frame correction: Blender bones point along their local +Y while
# Maya/J3D skeletons usually run along the joint's +X axis.  v_joint = R_FIX @ v_bone
R_FIX = np.array([[0, 1, 0, 0],
                  [-1, 0, 0, 0],
                  [0, 0, 1, 0],
                  [0, 0, 0, 1]], dtype=np.float64)
R_FIX_INV = R_FIX.T.copy()

BONE_AXIS_ITEMS = [
    ("X", "X-forward (nicer bones)", "Bone Y axis follows the joint X axis, as in Maya skeletons"),
    ("NONE", "Joint axes (exact)", "Bone axes are identical to the J3D joint axes"),
]

IMAGE_FORMAT_ITEMS = [
    (n, n, "") for n in ("CMPR", "RGB5A3", "RGBA32", "RGB565", "IA8", "IA4", "I8", "I4")
]

FORMAT_ITEMS = list(IMAGE_FORMAT_ITEMS)

RENDER_ITEMS = [
    ("AUTO", "Auto (keep imported)", "Keep imported material settings; derive them for new materials"),
    (mat3.RENDER_OPAQUE, "Opaque", "No transparency"),
    (mat3.RENDER_CUTOUT, "Cutout (alpha test)", "Pixels below the alpha reference are discarded"),
    (mat3.RENDER_TRANSLUCENT, "Translucent (alpha blend)", "Alpha blended, no depth write"),
]


class BMDMaterialProps(PropertyGroup):
    render_mode: EnumProperty(name="Render Mode", items=RENDER_ITEMS, default="AUTO",
                              description="How the game renders this material")
    alpha_ref: IntProperty(name="Alpha Reference", default=128, min=0, max=255,
                           description="Alpha test threshold for the Cutout mode")


def _image_key_changed(self, context):
    self.key_custom = True


class BMDImageProps(PropertyGroup):
    format: EnumProperty(name="GX Format", items=IMAGE_FORMAT_ITEMS, default="CMPR",
                         description="Texture format used when exporting this image")
    key: StringProperty(
        name="BMD Key",
        description="Unique key used by materials to reference this texture. "
                    "Empty = use the image datablock name",
        update=_image_key_changed,
    )
    key_custom: BoolProperty(default=False, options={"HIDDEN"})
    mip_count: IntProperty(name="Mip Levels", default=0, min=0, max=12,
                           description="Number of mip levels (0 = automatic / keep imported)")
    external: BoolProperty(name="External Texture", default=False,
                           description="Store this texture outside the BMD (header-only TEX1 "
                                       "entry). Use File > Export > J3D Textures (.bti) to write "
                                       "external textures to a folder. Tick this for any BMD, "
                                       "disk or scene texture to make it external")


def axis_fix(arm_obj):
    """Bone frame correction that was used when the armature was imported."""
    return R_FIX if arm_obj.get("bmd_bone_axis", "X") == "X" else np.eye(4)


def mesh_object_poll(_self, obj):
    """PointerProperty poll for mesh objects."""
    return obj is not None and obj.type == "MESH"


def target_armature(obj):
    """Return the armature that deforms *obj*, if any."""
    if obj is None:
        return None

    if obj.parent is not None and obj.parent.type == "ARMATURE":
        return obj.parent

    for modifier in obj.modifiers:
        if modifier.type == "ARMATURE" and modifier.object is not None:
            return modifier.object

    return None


def model_label(arm):
    """Human-readable label for an imported BMD model/armature."""
    source = str(arm.get("bmd_source", ""))

    if source:
        return os.path.basename(source)

    if arm.users_collection:
        return arm.users_collection[0].name

    return arm.name


def j3d_local_to_blender(L, s, fix, is_root=False):
    """J3D joint-local matrix -> Blender bone-local (rest-relative) matrix.

    ``s`` = Blender units per J3D unit, ``fix`` = bone frame correction (R_FIX or identity).
    For child bones the parent's bone frame cancels the left ``fix``; roots are relative to
    the armature, which has no such frame, so they keep it out.
    """
    M = C @ L @ C_INV @ fix
    if not is_root:
        M = fix.T @ M
    M[:3, 3] *= s
    return M


def blender_local_to_j3d(M, s, fix, is_root=False):
    """Inverse of :func:`j3d_local_to_blender`."""
    M = M.copy()
    M[:3, 3] /= s
    if not is_root:
        M = fix @ M
    return C_INV @ M @ fix.T @ C


def trs_matrix(scale, euler, trans):
    """4x4 matrix T * Rz*Ry*Rx * S (J3D joint convention)."""
    from .j3d.model import euler_to_matrix
    M = np.eye(4)
    M[:3, :3] = euler_to_matrix(*euler) * np.asarray(scale, dtype=np.float64)[None, :]
    M[:3, 3] = trans
    return M


def matrix_to_quat(R):
    """Rotation matrix -> quaternion (w, x, y, z)."""
    t = R[0, 0] + R[1, 1] + R[2, 2]
    if t > 0:
        k = math.sqrt(t + 1.0) * 2
        q = (0.25 * k, (R[2, 1] - R[1, 2]) / k, (R[0, 2] - R[2, 0]) / k, (R[1, 0] - R[0, 1]) / k)
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        k = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        q = ((R[2, 1] - R[1, 2]) / k, 0.25 * k, (R[0, 1] + R[1, 0]) / k, (R[0, 2] + R[2, 0]) / k)
    elif R[1, 1] > R[2, 2]:
        k = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        q = ((R[0, 2] - R[2, 0]) / k, (R[0, 1] + R[1, 0]) / k, 0.25 * k, (R[1, 2] + R[2, 1]) / k)
    else:
        k = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        q = ((R[1, 0] - R[0, 1]) / k, (R[0, 2] + R[2, 0]) / k, (R[1, 2] + R[2, 1]) / k, 0.25 * k)
    q = np.array(q)
    return q / np.linalg.norm(q)


def split_trs(A):
    """Split a 3x3 matrix into the closest proper rotation and signed scales.

    ``A = R @ diag(scale)`` for a normal T*R*S matrix.  A non-uniformly
    scaled joint that rotates produces a shear; Blender pose bones cannot
    store shear, so the closest rotation is returned and ``shear`` reports
    the maximum deviation from it (0 for an exact T*R*S matrix).
    """
    A = np.asarray(A, dtype=np.float64)
    scale = np.linalg.norm(A, axis=0)
    scale[scale == 0] = 1.0
    R = A / scale[None, :]

    # Move a reflection onto one scale axis instead of the rotation.  Prefer
    # the smallest axis so the sign flip is as harmless as possible.
    if np.linalg.det(R) < 0.0:
        axis = int(np.argmin(np.abs(scale)))
        scale[axis] = -scale[axis]
        R[:, axis] = -R[:, axis]

    U, _S, Vt = np.linalg.svd(R)
    Rp = U @ Vt
    if np.linalg.det(Rp) < 0.0:
        U[:, -1] *= -1.0
        Rp = U @ Vt

    shear = float(np.abs(R - Rp).max()) if R.size else 0.0
    return Rp, scale, shear


def decompose(M):
    """4x4 matrix -> (location, quaternion wxyz, signed scale)."""
    loc = M[:3, 3].copy()
    R, scale, _shear = split_trs(M[:3, :3])
    return loc, matrix_to_quat(R), scale


def to_np(m):
    return np.array([list(r) for r in m], dtype=np.float64)


def orthonormalize(M):
    """Remove scale from the 3x3 part of a 4x4 matrix (column normalisation)."""
    R = M.copy()
    for i in range(3):
        n = np.linalg.norm(R[:3, i])
        if n > 1e-12:
            R[:3, i] /= n
    return R


def point_to_blender(p, scale):
    p = np.asarray(p, dtype=np.float64)
    return np.stack([p[:, 0], -p[:, 2], p[:, 1]], 1) * scale


def point_from_blender(p, scale):
    p = np.asarray(p, dtype=np.float64) * scale  # export scale = J3D units per Blender unit
    return np.stack([p[:, 0], p[:, 2], -p[:, 1]], 1)


def vec_from_blender(v):
    v = np.asarray(v, dtype=np.float64)
    return np.stack([v[:, 0], v[:, 2], -v[:, 1]], 1)


def texture_key(image):
    """Unique key of an image inside the exported file."""
    bmd = getattr(image, "bmd", None)
    if bmd is not None and bool(getattr(bmd, "key_custom", False)):
        key = str(bmd.key or "")
        return key or f"__image__{image.name}"
    legacy = image.get("bmd_key")
    if legacy:
        return str(legacy)
    if bmd is not None and bmd.key:
        return str(bmd.key)
    # Never fall back to the BMD texture name here: clearing BMD Key must
    # release the old key, and two new images with the same base name must not
    # collapse into one texture.  The datablock name is unique in Blender.
    return f"__image__{image.name}"


def clean_texture_name(image):
    # The name stored in the BMD is authoritative: duplicate internal textures
    # may legitimately share it (indices, not names, identify them).
    stored = image.get("bmd_name")
    if stored:
        return str(stored)
    name = image.name
    # Blender appends .001, .002 ... to duplicate image datablock names; that
    # suffix is not part of the texture name for images without a stored name.
    base, sep, suffix = name.rpartition(".")
    if sep and len(suffix) == 3 and suffix.isdigit():
        name = base
    low = name.lower()
    for ext in (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff", ".dds", ".webp"):
        if low.endswith(ext):
            return name[: -len(ext)]
    return name


def register_classes():
    bpy.utils.register_class(BMDMaterialProps)
    bpy.utils.register_class(BMDImageProps)
    from bpy.props import PointerProperty
    bpy.types.Material.bmd = PointerProperty(type=BMDMaterialProps)
    bpy.types.Image.bmd = PointerProperty(type=BMDImageProps)


def unregister_classes():
    del bpy.types.Image.bmd
    del bpy.types.Material.bmd
    bpy.utils.unregister_class(BMDImageProps)
    bpy.utils.unregister_class(BMDMaterialProps)
