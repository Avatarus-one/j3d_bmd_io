"""Shared helpers for the Blender import / export operators."""

import json
import math
import os

import numpy as np
import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty,
                       PointerProperty, StringProperty)
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

WRAP_EXT = {0: "EXTEND", 1: "REPEAT", 2: "MIRROR"}
WRAP_ITEMS = [
    ("0", "Clamp", "Clamp to edge"),
    ("1", "Repeat", "Repeat the texture"),
    ("2", "Mirror", "Mirror the texture"),
]
MIN_FILTER_ITEMS = [
    ("0", "Nearest", "Nearest neighbour"),
    ("1", "Linear", "Linear"),
    ("2", "Nearest Mipmap Nearest", "Nearest mipmap, nearest texel"),
    ("3", "Linear Mipmap Nearest", "Nearest mipmap, linear texel"),
    ("4", "Nearest Mipmap Linear", "Linear mipmap, nearest texel"),
    ("5", "Linear Mipmap Linear", "Trilinear"),
]
MAG_FILTER_ITEMS = [
    ("0", "Nearest", "Nearest neighbour"),
    ("1", "Linear", "Linear"),
]

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


def _set_wrap_math(node, mode):
    if mode == 1:
        node.operation = "FRACT"
        node.use_clamp = False
    elif mode == 2:
        node.operation = "PINGPONG"
        node.inputs[1].default_value = 1.0
        node.use_clamp = False
    else:
        node.operation = "ADD"
        node.inputs[1].default_value = 0.0
        node.use_clamp = True


def _layout_wrap_chain(node, sep, comb, s, t, uv=None):
    """Lay the per-axis wrap chain out as a compact tree left of *node*."""
    x, y = node.location
    if uv is not None:
        uv.location = (x - 800, y)
    sep.location = (x - 600, y)
    s.location = (x - 400, y)
    t.location = (x - 400, y - 160)
    comb.location = (x - 200, y)


def _apply_wrap_to_node(node, wrap_s, wrap_t):
    tree = node.id_data
    chain = {n.get("bmd_wrap_axis"): n for n in tree.nodes
             if n.get("bmd_wrap_for") == node.name}
    if not chain and wrap_s == wrap_t:
        node.extension = WRAP_EXT.get(wrap_s, "REPEAT")
        return
    if not chain:
        uv = None
        if node.inputs["Vector"].is_linked:
            source = node.inputs["Vector"].links[0].from_socket
        else:
            uv = tree.nodes.new("ShaderNodeUVMap")
            uv.uv_map = "UV0"
            source = uv.outputs["UV"]
        sep = tree.nodes.new("ShaderNodeSeparateXYZ")
        comb = tree.nodes.new("ShaderNodeCombineXYZ")
        s = tree.nodes.new("ShaderNodeMath")
        s["bmd_wrap_for"] = node.name
        s["bmd_wrap_axis"] = "S"
        t = tree.nodes.new("ShaderNodeMath")
        t["bmd_wrap_for"] = node.name
        t["bmd_wrap_axis"] = "T"
        _layout_wrap_chain(node, sep, comb, s, t, uv)
        tree.links.new(source, sep.inputs["Vector"])
        tree.links.new(sep.outputs["X"], s.inputs[0])
        tree.links.new(sep.outputs["Y"], t.inputs[0])
        tree.links.new(s.outputs[0], comb.inputs["X"])
        tree.links.new(t.outputs[0], comb.inputs["Y"])
        tree.links.new(comb.outputs["Vector"], node.inputs["Vector"])
        chain = {"S": s, "T": t}
    _set_wrap_math(chain["S"], wrap_s)
    _set_wrap_math(chain["T"], wrap_t)
    node.extension = "EXTEND"


def _image_wrap_changed(self, context):
    image = self.id_data
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree is None:
            continue
        for node in tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is image:
                try:
                    _apply_wrap_to_node(node, int(self.wrap_s), int(self.wrap_t))
                except Exception:
                    pass


def _image_max_lod_changed(self, context):
    self.max_lod_custom = True


def _image_mip_count_changed(self, context):
    if self.max_lod_custom:
        return
    mips = max(1, int(self.mip_count))
    self.max_lod = (mips - 1) * 8
    self.max_lod_custom = False


def image_interpolation(mag_filter):
    """Blender has one interpolation mode; the GX magnification filter selects it."""
    return "Closest" if int(mag_filter) == 0 else "Linear"


def _image_filter_changed(self, context):
    image = self.id_data
    interpolation = image_interpolation(self.mag_filter)
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree is None:
            continue
        for node in tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is image:
                try:
                    node.interpolation = interpolation
                except Exception:
                    pass


def texture_slot_key(slot):
    """Stable key of a texture slot (the image key is authoritative)."""
    if slot.image is not None:
        return texture_key(slot.image)
    if slot.key:
        return str(slot.key)
    return ""


def texture_duplicates(arm_obj):
    """{key: [slot indices]} for textures used by more than one slot."""
    by_key = {}
    for i, slot in enumerate(arm_obj.bmd_texture_list):
        key = texture_slot_key(slot)
        if key:
            by_key.setdefault(key, []).append(i)
    return {key: indices for key, indices in by_key.items() if len(indices) > 1}


def texture_is_duplicate(arm_obj, key):
    if not key:
        return False
    count = 0
    for slot in arm_obj.bmd_texture_list:
        if texture_slot_key(slot) == key:
            count += 1
            if count > 1:
                return True
    return False


def image_by_key(key):
    if not key:
        return None
    for image in bpy.data.images:
        if texture_key(image) == key:
            return image
    return None


def sync_texture_legacy(arm_obj):
    """Keep the old bmd_textures / bmd_texture_keys arrays in sync."""
    names, keys = [], []
    for slot in arm_obj.bmd_texture_list:
        key = texture_slot_key(slot)
        name = slot.name
        if slot.image is not None:
            # Legacy arrays stored image datablock names, not BMD names.
            name = slot.image.name
        names.append(name)
        keys.append(key)
    arm_obj["bmd_textures"] = names
    arm_obj["bmd_texture_keys"] = keys


def ensure_texture_list(arm_obj):
    """Return the texture list, rebuilding it from the legacy arrays if needed."""
    coll = arm_obj.bmd_texture_list
    if len(coll):
        return coll
    names = list(arm_obj.get("bmd_textures", []))
    keys = list(arm_obj.get("bmd_texture_keys", []))
    if not keys and names:
        keys = [""] * len(names)
    if not keys:
        return coll
    was_loading = bool(arm_obj.get("bmd_texture_loading", False))
    arm_obj["bmd_texture_loading"] = True
    try:
        for i, key in enumerate(keys):
            slot = coll.add()
            slot.key = str(key)
            image = image_by_key(key)
            slot.image = image
            if image is not None:
                slot.name = str(image.get("bmd_name") or
                                 (names[i] if i < len(names) else clean_texture_name(image)))
            else:
                slot.name = str(names[i]) if i < len(names) else ""
    finally:
        if not was_loading and "bmd_texture_loading" in arm_obj:
            del arm_obj["bmd_texture_loading"]
    sync_texture_legacy(arm_obj)
    return coll


def texture_slot(arm_obj, image):
    """(index, slot) of *image* in the BMD texture list, or (-1, None)."""
    if arm_obj is None or image is None:
        return -1, None
    ensure_texture_list(arm_obj)
    key = texture_key(image)
    for i, slot in enumerate(arm_obj.bmd_texture_list):
        if slot.image == image or texture_slot_key(slot) == key:
            return i, slot
    return -1, None


def texture_label(arm_obj, image):
    """Human readable texture reference for the material panel."""
    index, slot = texture_slot(arm_obj, image)
    if slot is not None:
        return f"{index}: {slot.name or image.name}"
    return f"{image.name} (not in BMD list)"


def material_texture_slots(mat, owner=None, store=True):
    """BMD texture index per material texture slot (8 entries)."""
    raw = mat.get("bmd_texture_slots")
    slots = None
    if raw:
        try:
            slots = json.loads(raw)
        except Exception:
            slots = None
    if slots is None:
        if owner is None:
            return [None] * 8
        slots = [None] * 8
        key_to_index = {texture_slot_key(s): i
                        for i, s in enumerate(owner.bmd_texture_list)}
        stored = mat.get("bmd_material")
        names = []
        if stored:
            try:
                names = json.loads(stored).get("texNames") or []
            except Exception:
                names = []
        for i, key in enumerate(names):
            if key and key in key_to_index:
                slots[i] = key_to_index[key]
        tree = mat.node_tree
        if tree is not None:
            for node in tree.nodes:
                if (node.type == "TEX_IMAGE" and node.name.startswith("bmd_tex")
                        and node.name[7:].isdigit() and node.image is not None):
                    key = texture_key(node.image)
                    if key in key_to_index:
                        slots[int(node.name[7:])] = key_to_index[key]
        if store:
            set_material_texture_slots(mat, slots)
    return list(slots)[:8]


def set_material_texture_slots(mat, slots):
    values = list(slots)[:8]
    while len(values) < 8:
        values.append(None)
    mat["bmd_texture_slots"] = json.dumps(values, separators=(",", ":"))


def remap_material_texture_slots(owner, mapping):
    for material in bpy.data.materials:
        slots = material_texture_slots(material, owner)
        new = [mapping.get(s, s) if s is not None else None for s in slots]
        if new != slots:
            set_material_texture_slots(material, new)


def remove_material_texture_slot(owner, index):
    for material in bpy.data.materials:
        slots = material_texture_slots(material, owner)
        changed = False
        for n, s in enumerate(slots):
            if s is None:
                continue
            if s == index:
                slots[n] = None
                changed = True
            elif s > index:
                slots[n] = s - 1
                changed = True
        if changed:
            set_material_texture_slots(material, slots)


def material_slot_users(owner, index):
    return sum(1 for material in bpy.data.materials
               if index in material_texture_slots(material, owner, store=False))


def count_texture_users(key):
    if not key:
        return 0
    count = 0
    for material in bpy.data.materials:
        tree = material.node_tree
        stored = material.get("bmd_material")
        names = None
        if stored:
            try:
                names = json.loads(stored).get("texNames")
            except Exception:
                names = None
        if names:
            count += sum(1 for name in names if name == key)
        if tree is None:
            continue
        for node in tree.nodes:
            if node.type != "TEX_IMAGE":
                continue
            if names and key in names:
                continue
            if node.get("bmd_cleared_texture") == key:
                count += 1
            elif node.image is not None and texture_key(node.image) == key:
                count += 1
    return count


def clear_material_textures(old_key, slot_index=None, owner=None):
    """Clear the material image but keep the texture link for a later assignment."""
    if not old_key:
        return
    for material in bpy.data.materials:
        tree = material.node_tree
        slots = (material_texture_slots(material, owner)
                 if slot_index is not None and owner is not None else None)
        if slots is not None and any(s is not None for s in slots):
            if tree is None:
                continue
            for n, s in enumerate(slots):
                if s != slot_index:
                    continue
                node = tree.nodes.get(f"bmd_tex{n}")
                if node is not None and node.type == "TEX_IMAGE":
                    node.image = None
                    node["bmd_cleared_texture"] = old_key
            continue
        if tree is None:
            continue
        stored = material.get("bmd_material")
        if stored:
            try:
                data = json.loads(stored)
            except Exception:
                data = None
            if data is not None:
                for slot, name in enumerate(data.get("texNames") or []):
                    if name != old_key:
                        continue
                    node = tree.nodes.get(f"bmd_tex{slot}")
                    if node is not None and node.type == "TEX_IMAGE":
                        node.image = None
                        node["bmd_cleared_texture"] = old_key
        for node in tree.nodes:
            if (node.type == "TEX_IMAGE" and node.image is not None
                    and texture_key(node.image) == old_key):
                node["bmd_cleared_texture"] = old_key
                node.image = None


def retarget_material_textures(old_key, new_key, new_image, slot_index=None,
                               owner=None, drop_slot=False):
    """Point the materials linked to the changed BMD texture at its replacement."""
    if not old_key:
        return
    for material in bpy.data.materials:
        tree = material.node_tree
        slots = (material_texture_slots(material, owner)
                 if slot_index is not None and owner is not None else None)
        if slots is not None and any(s is not None for s in slots):
            stored = material.get("bmd_material")
            data = None
            if stored:
                try:
                    data = json.loads(stored)
                except Exception:
                    data = None
            names_changed = False
            slots_changed = False
            for n, s in enumerate(slots):
                if s != slot_index:
                    continue
                if data is not None:
                    names = list(data.get("texNames") or [])
                    while len(names) < 8:
                        names.append(None)
                    names[n] = new_key or None
                    data["texNames"] = names
                    names_changed = True
                if tree is not None:
                    node = tree.nodes.get(f"bmd_tex{n}")
                    if node is not None and node.type == "TEX_IMAGE":
                        node.image = new_image
                        if "bmd_cleared_texture" in node:
                            del node["bmd_cleared_texture"]
                if drop_slot and not new_key:
                    slots[n] = None
                    slots_changed = True
            if names_changed:
                material["bmd_material"] = json.dumps(data, separators=(",", ":"))
            if slots_changed:
                set_material_texture_slots(material, slots)
            continue
        stored = material.get("bmd_material")
        data = None
        if stored:
            try:
                data = json.loads(stored)
            except Exception:
                data = None
        if data is not None:
            names = data.get("texNames")
            if names:
                changed = False
                for slot, name in enumerate(names):
                    if name != old_key:
                        continue
                    names[slot] = new_key or None
                    changed = True
                    if tree is not None:
                        node = tree.nodes.get(f"bmd_tex{slot}")
                        if node is not None and node.type == "TEX_IMAGE":
                            node.image = new_image
                            if "bmd_cleared_texture" in node:
                                del node["bmd_cleared_texture"]
                if changed:
                    material["bmd_material"] = json.dumps(data, separators=(",", ":"))
        if tree is None:
            continue
        for node in tree.nodes:
            if node.type != "TEX_IMAGE":
                continue
            if node.get("bmd_cleared_texture") == old_key:
                node.image = new_image
                del node["bmd_cleared_texture"]
            elif (node.image is not None
                  and texture_key(node.image) == old_key):
                node.image = new_image


def _texture_slot_image_changed(self, context):
    arm_obj = self.id_data
    if arm_obj is None or arm_obj.get("bmd_texture_loading"):
        return
    old_key = str(self.key or "")
    image = self.image
    new_key = texture_key(image) if image is not None else ""
    slot_index = None
    for i, slot in enumerate(arm_obj.bmd_texture_list):
        if slot == self:
            slot_index = i
            break
    if image is None:
        if old_key:
            clear_material_textures(old_key, slot_index, arm_obj)
        # Keep self.key so assigning a new image can restore the material link.
        sync_texture_legacy(arm_obj)
        return
    retarget_material_textures(old_key, new_key, image, slot_index, arm_obj)
    self.key = new_key
    stored = image.get("bmd_name")
    self.name = str(stored) if stored else clean_texture_name(image)
    sync_texture_legacy(arm_obj)


def _texture_slot_name_changed(self, context):
    arm_obj = self.id_data
    if arm_obj is None or arm_obj.get("bmd_texture_loading"):
        return
    if self.image is not None:
        if self.name:
            self.image["bmd_name"] = self.name
        elif "bmd_name" in self.image:
            del self.image["bmd_name"]
    sync_texture_legacy(arm_obj)


class BMDTextureSlot(PropertyGroup):
    image: PointerProperty(type=bpy.types.Image, name="Image",
                           update=_texture_slot_image_changed)
    name: StringProperty(name="BMD Name", update=_texture_slot_name_changed)
    key: StringProperty(options={"HIDDEN"})


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
    wrap_s: EnumProperty(name="Wrap S", items=WRAP_ITEMS, default="1",
                         update=_image_wrap_changed)
    wrap_t: EnumProperty(name="Wrap T", items=WRAP_ITEMS, default="1",
                         update=_image_wrap_changed)
    min_filter: EnumProperty(name="Min Filter", items=MIN_FILTER_ITEMS, default="1")
    mag_filter: EnumProperty(name="Mag Filter", items=MAG_FILTER_ITEMS, default="1",
                             update=_image_filter_changed)
    min_lod: IntProperty(name="Min LOD", default=0, min=-128, max=127,
                         description="Raw GX LOD value (1/8 units)")
    max_lod: IntProperty(name="Max LOD", default=0, min=-128, max=127,
                         description="Raw GX LOD value (1/8 units)",
                         update=_image_max_lod_changed)
    max_lod_custom: BoolProperty(default=False, options={"HIDDEN"})
    lod_bias: IntProperty(name="LOD Bias", default=0, min=-32768, max=32767,
                          description="Raw GX LOD bias (1/100 units)")
    mip_count: IntProperty(name="Mip Levels", default=0, min=0, max=12,
                           description="Number of mip levels (0 = automatic / keep imported)",
                           update=_image_mip_count_changed)
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
    bpy.utils.register_class(BMDTextureSlot)
    bpy.types.Material.bmd = PointerProperty(type=BMDMaterialProps)
    bpy.types.Image.bmd = PointerProperty(type=BMDImageProps)
    bpy.types.Object.bmd_texture_list = CollectionProperty(type=BMDTextureSlot)
    bpy.types.Object.bmd_texture_index = IntProperty(default=0, min=0)


def unregister_classes():
    del bpy.types.Object.bmd_texture_index
    del bpy.types.Object.bmd_texture_list
    del bpy.types.Image.bmd
    del bpy.types.Material.bmd
    bpy.utils.unregister_class(BMDTextureSlot)
    bpy.utils.unregister_class(BMDImageProps)
    bpy.utils.unregister_class(BMDMaterialProps)
