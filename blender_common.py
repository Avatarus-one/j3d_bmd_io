"""Shared helpers for the Blender import / export operators."""

import json
import math
import os
from dataclasses import dataclass

import numpy as np
import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, IntProperty, PointerProperty,
                       StringProperty)
from bpy.types import PropertyGroup

from .j3d import gx_texture as gx
from .j3d import material as mat3


_AUTO_ARM = object()


def context_model_object(context):
    """The active armature/mesh, or the first selected one."""
    obj = getattr(context, "active_object", None)
    if obj is not None and obj.type in {"ARMATURE", "MESH"}:
        return obj
    for obj in getattr(context, "selected_objects", ()) or ():
        if obj.type in {"ARMATURE", "MESH"}:
            return obj
    return None


# Keys written by the BMD importer/exporter.  ``bmd_vtx`` and
# ``j3d_animations`` are intentionally absent: older builds created those
# empty groups on every object, so they are not proof that an object is BMD.
BMD_ARMATURE_KEYS = frozenset({
    "bmd_bone_names", "bmd_source", "bmd_vtx_formats", "bmd_inf_flags",
    "bmd_sub_version", "bmd_scale", "bmd_bone_axis", "bmd_textures",
    "bmd_texture_keys",
})
BMD_MESH_KEYS = frozenset({
    "bmd_shape_nodes", "bmd_joint", "bmd_order", "bmd_shape_index",
    "bmd_bls_source", "bmd_source", "bmd_vtx_formats", "bmd_scale",
    "bmd_textures", "bmd_texture_keys",
})


def bmd_scale_value(obj):
    """Return the stored BMD scale, or None for a custom object."""
    if obj is None:
        return None

    raw = obj.get("bmd_scale")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass

    return None


def bmd_convert_units_value(obj):
    """Return a custom object's remembered Convert Units value, or None."""
    if obj is None:
        return None

    raw = obj.get("bmd_convert_units")
    if raw is not None:
        return bool(raw)

    return None


def _bmd_scale_get(self):
    try:
        return float(self.get("bmd_scale", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _bmd_convert_units_get(self):
    raw = self.get("bmd_convert_units")
    return True if raw is None else bool(raw)


def _bmd_string_get(name):
    def getter(self):
        value = self.get(name)
        return "" if value is None else str(value)

    return getter


def _bmd_int_get(name):
    def getter(self):
        try:
            return int(self.get(name, 0))
        except (TypeError, ValueError):
            return 0

    return getter


def _has_bmd_texture_list(obj):
    if "bmd_texture_list" not in obj:
        return False

    try:
        return len(obj.bmd_texture_list) > 0
    except Exception:
        return False


def _has_loaded_vertex_formats(obj):
    if "bmd_vtx" not in obj:
        return False

    try:
        return bool(obj.bmd_vtx.loaded)
    except Exception:
        return False


def is_bmd_armature(obj):
    """True for armatures that carry BMD data written by this add-on."""
    if obj is None or obj.type != "ARMATURE":
        return False

    if bmd_scale_value(obj) is not None:
        return True

    if any(key in obj for key in BMD_ARMATURE_KEYS):
        return True

    if _has_loaded_vertex_formats(obj):
        return True

    return _has_bmd_texture_list(obj)


def is_bmd_mesh(obj):
    """True for meshes that are part of a BMD model.

    A mesh is BMD when it carries BMD metadata itself or is deformed by a BMD
    armature.  An empty ``bmd_vtx``/``j3d_animations`` group left by an older
    build is not enough; a ``bmd_vtx`` group that was actually initialized is.
    """
    if obj is None or obj.type != "MESH":
        return False

    if bmd_scale_value(obj) is not None:
        return True

    if any(key in obj for key in BMD_MESH_KEYS):
        return True

    if _has_loaded_vertex_formats(obj):
        return True

    mesh = obj.data

    if mesh is not None:
        if "bmd_normal_meta" in mesh:
            return True

        if mesh.attributes.get("bmd_node") is not None:
            return True

    arm = target_armature(obj)
    return arm is not None and is_bmd_armature(arm)


def is_bmd_object(obj):
    """True when *obj* is an imported or explicitly initialized BMD model."""
    if obj is None:
        return False

    if obj.type == "ARMATURE":
        return is_bmd_armature(obj)

    if obj.type == "MESH":
        return is_bmd_mesh(obj)

    return False


def is_kcl_object(obj):
    """True when *obj* is an imported or explicitly initialized KCL mesh."""
    if obj is None or obj.type != "MESH":
        return False

    return ("kcl_source" in obj or "kcl_thickness" in obj
            or "kcl_area_min" in obj)


def is_bmd_material(mat):
    """True when a material belongs to a BMD model (metadata or BMD nodes)."""
    if mat is None:
        return False

    if "bmd_material" in mat or "bmd_name" in mat:
        return True

    tree = mat.node_tree

    if tree is not None:
        for node in tree.nodes:
            if (node.type == "TEX_IMAGE"
                    and node.name.startswith("bmd_tex")):
                return True

    return False


def is_bmd_image(image):
    """True when an image carries BMD metadata or is in a BMD texture list."""
    if image is None:
        return False

    for key in image.keys():
        if str(key).startswith("bmd_"):
            return True

    for obj in bpy.data.objects:
        if obj.type not in {"MESH", "ARMATURE"} or not is_bmd_object(obj):
            continue

        try:
            for slot in obj.bmd_texture_list:
                if slot.image == image:
                    return True
        except Exception:
            pass

    return False


def _store_convert_units(obj, value):
    if obj is None:
        return

    if is_kcl_object(obj) and "kcl_source" in obj:
        return

    if bmd_scale_value(obj) is not None:
        return

    obj["bmd_convert_units"] = bool(value)


def remember_convert_units(context, value, obj=None):
    """Store the last Convert Units choice on a custom mesh/armature.

    Imported models use ``bmd_scale`` and are left untouched.  A BMD model
    keeps one value on its armature; standalone custom meshes keep it on the
    mesh.  ``context`` is kept for call-site symmetry; nothing is stored on the
    scene.
    """
    if obj is None:
        return

    if obj.type == "MESH":
        if is_kcl_object(obj):
            _store_convert_units(obj, value)
            return

        arm = target_armature(obj)

        if arm is not None:
            _store_convert_units(arm, value)
        else:
            _store_convert_units(obj, value)
    elif obj.type == "ARMATURE":
        _store_convert_units(obj, value)


def saved_convert_units(context, obj=_AUTO_ARM):
    """Convert Units default for a model.

    A mesh/armature with ``bmd_scale`` uses that imported value.  For a custom
    model, BLK/BLS read the armature's stored ``bmd_convert_units``; a
    standalone mesh uses its own value.  Default is True.
    """
    if obj is _AUTO_ARM:
        obj = context_model_object(context)
    candidates = []
    if obj is not None:
        candidates.append(obj)
        if getattr(obj, "type", None) == "MESH":
            arm = target_armature(obj)
            if arm is not None and arm is not obj:
                candidates.append(arm)
    stored = None
    for candidate in candidates:
        if is_kcl_object(candidate) and "kcl_source" in candidate:
            kcl_scale = candidate.get("kcl_scale")

            if kcl_scale is not None:
                try:
                    return abs(float(kcl_scale) - 1.0) > 1e-6
                except (TypeError, ValueError):
                    pass

        scale = bmd_scale_value(candidate)
        if scale is not None:
            return abs(float(scale) - 1.0) > 1e-6

        if stored is None:
            value = bmd_convert_units_value(candidate)
            if value is not None:
                stored = value
    return True if stored is None else stored


VERTEX_FORMAT_ITEMS = [
    ("F32", "Float32", "Write as float32"),
    ("S16", "s16", "Write as signed 16-bit"),
]


# These are export settings shown in the J3D sidebar, not animatable scene
# parameters: options=set() removes "Insert Keyframe" from their context menu.
class BMD_VertexFormatSettings(PropertyGroup):
    """Per-model vertex formats shown in the 3D View BMD panel."""

    loaded: BoolProperty(default=False, options=set())
    position_format: EnumProperty(
        name="Positions", items=VERTEX_FORMAT_ITEMS, default="F32",
        options=set())
    position_frac: IntProperty(
        name="Fraction", default=6, min=0, max=15,
        description="Fraction bits for 16-bit positions", options=set())
    normal_format: EnumProperty(
        name="Normals", items=VERTEX_FORMAT_ITEMS, default="F32",
        options=set())
    normal_frac: IntProperty(
        name="Fraction", default=14, min=0, max=15,
        description="Fraction bits for 16-bit normals", options=set())
    uv_format: EnumProperty(
        name="UVs", items=VERTEX_FORMAT_ITEMS, default="F32",
        options=set())
    uv_frac: IntProperty(
        name="Fraction", default=14, min=0, max=15,
        description="Fraction bits for 16-bit UVs", options=set())


def vertex_format_choice(fmt, default_frac):
    if not fmt:
        return "F32", default_frac
    _components, typ, frac = fmt
    if typ == 3:
        return "S16", int(frac)
    return "F32", default_frac


def apply_vertex_formats(obj, formats):
    """Copy imported BMD ``vtx_formats`` into the object's format settings."""
    settings = getattr(obj, "bmd_vtx", None)
    if settings is None:
        return None
    table = {}
    for attr, components, typ, frac in formats:
        table[int(attr)] = (int(components), int(typ), int(frac))
    settings.position_format, settings.position_frac = vertex_format_choice(
        table.get(9), 6)
    settings.normal_format, settings.normal_frac = vertex_format_choice(
        table.get(10), 14)
    uv_attr = next((attr for attr in range(13, 21) if attr in table), 13)
    settings.uv_format, settings.uv_frac = vertex_format_choice(
        table.get(uv_attr), 14)
    settings.loaded = True
    return settings


def ensure_vertex_formats(obj, read_only=False):
    """Return the object's format settings, initializing them from the import.

    During a panel draw Blender forbids writing ID properties, so ``read_only``
    returns the current values (defaults) without initializing ``loaded`` and
    without creating the group on an object that does not have it yet.
    """
    if read_only and "bmd_vtx" not in obj:
        return None

    settings = getattr(obj, "bmd_vtx", None)
    if settings is None:
        return None
    if not settings.loaded:
        if read_only:
            return settings
        raw = obj.get("bmd_vtx_formats")
        if raw:
            try:
                table = json.loads(raw) if isinstance(raw, str) else raw
                formats = [(int(attr), *value) for attr, value in table.items()]
                apply_vertex_formats(obj, formats)
            except (TypeError, ValueError, AttributeError):
                settings.loaded = True
        else:
            settings.loaded = True
    return settings


def initialize_vertex_formats():
    """Initialize ``bmd_vtx`` on BMD objects imported by an older build.

    Objects without BMD metadata are left untouched; this function must not
    turn every mesh/armature in the file into a J3D object.
    """
    for obj in bpy.data.objects:
        if obj.type not in {"MESH", "ARMATURE"}:
            continue
        if obj.library is not None or obj.override_library is not None:
            continue
        if not is_bmd_object(obj):
            continue

        # A skinned mesh uses the armature's vertex-format settings; only a
        # standalone mesh needs its own bmd_vtx group.
        if obj.type == "MESH":
            arm = target_armature(obj)

            if arm is not None and is_bmd_object(arm):
                continue

        settings = getattr(obj, "bmd_vtx", None)
        if settings is None or settings.loaded:
            continue
        raw = obj.get("bmd_vtx_formats")
        if not raw:
            settings.loaded = True
            continue
        try:
            table = json.loads(raw) if isinstance(raw, str) else raw
            formats = [(int(attr), *value) for attr, value in table.items()]
            apply_vertex_formats(obj, formats)
        except Exception:
            settings.loaded = True


def export_stem(path, fallback=""):
    """File stem of *path*, or *fallback* when the path is empty."""
    if path:
        return os.path.splitext(os.path.basename(str(path)))[0]
    return fallback


_SHARED_EXPORT_KINDS = {"bmd", "kcl"}


def _export_prop(kind, prefix="bmd_last_export"):
    """Property name for the last export path of *kind*.

    BMD and KCL share one field (a model and its collision mesh are exported
    to the same place); BLS keeps its own field.  Animation export remembers
    only its Export Folder, not per-file paths.
    """
    if kind in _SHARED_EXPORT_KINDS:
        return prefix
    return f"{prefix}_{kind}"


def last_export_path(obj, kind):
    if obj is None:
        return ""
    return str(obj.get(_export_prop(kind), "") or "")


def last_export_stem(obj, kind):
    return export_stem(last_export_path(obj, kind), "")


def animation_label(action):
    """User-facing animation name for an action."""
    if action is None:
        return ""

    name = action.get("j3d_animation_name")

    if name:
        return str(name)

    source = action.get("bmd_source")

    if source:
        return export_stem(source, action.name)

    return action.name


def animation_label_by_name(action_name):
    action = bpy.data.actions.get(action_name) if action_name else None
    return animation_label(action) if action is not None else str(action_name or "")


def remember_export(obj, kind, filepath):
    if obj is None or not filepath:
        return
    try:
        obj[_export_prop(kind)] = os.path.abspath(filepath)
        if getattr(obj, "type", None) == "MESH":
            scene = getattr(bpy.context, "scene", None)
            if scene is not None:
                scene[_export_prop(kind, "bmd_last_export_mesh")] = obj.name
    except Exception:
        pass


def last_export_mesh(kind):
    """The mesh used by the last export of *kind*, if it still exists."""
    scene = getattr(bpy.context, "scene", None)
    if scene is not None:
        name = scene.get(_export_prop(kind, "bmd_last_export_mesh"))
        if name:
            obj = bpy.data.objects.get(str(name))
            if obj is not None and obj.type == "MESH":
                return obj
    key = _export_prop(kind)
    for obj in bpy.data.objects:
        if obj.type == "MESH" and obj.get(key):
            return obj
    return None


def first_scene_mesh(context):
    scene = getattr(context, "scene", None)
    if scene is None:
        return None
    return next((o for o in scene.objects if o.type == "MESH"), None)


def export_target_mesh(context, kind):
    """Mesh for export defaults.

    Priority: the selected mesh (or the selected armature's mesh), then the
    mesh used by the last export of this format, then the first mesh in the
    scene.
    """
    active = getattr(context, "active_object", None)
    arm = active if active is not None and active.type == "ARMATURE" else None
    if arm is None:
        arm = next((o for o in getattr(context, "selected_objects", ()) or ()
                    if o.type == "ARMATURE"), None)
    mesh = model_mesh(context, arm)
    if mesh is not None:
        return mesh
    mesh = last_export_mesh(kind)
    if mesh is not None:
        return mesh
    return first_scene_mesh(context)


@dataclass(frozen=True)
class TargetCandidate:
    """One object that can receive an imported animation.

    ``object`` is what the dialog shows (and what the user selects).
    ``target`` is what actually receives the data (an armature for BCK, a mesh
    for BLK, ...).  ``capacity`` is the number of joints/keys available.
    ``data`` is an optional format-specific payload (for example the BLS path
    of a mesh) that can be reused without parsing the file again.
    """

    object: object
    target: object
    capacity: int
    data: object = None


def target_priority(required, candidate):
    """Return the candidate's priority for *required* units, or None.

    An exact capacity is the best match.  A larger capacity is allowed, but
    the priority drops with the difference; a smaller capacity cannot hold the
    animation and is skipped.
    """
    if required <= 0 or required > candidate.capacity:
        return None

    difference = candidate.capacity - required
    return 1.0 / (1.0 + difference)


def find_best_target(required, candidates):
    """Return the highest-priority candidate for *required*, or None.

    Ties keep the first candidate, so callers can put the selected object
    first to make it win against equivalent objects.
    """
    best = None
    best_priority = -1.0

    for candidate in candidates:
        value = target_priority(required, candidate)
        if value is not None and value > best_priority:
            best = candidate
            best_priority = value

    return best


def assign_targets(items, candidates):
    """Assign ``(key, required)`` items to their best candidates.

    ``candidates`` can be collected once and reused for a whole batch; this is
    the entry point for a future multi-file import that distributes animations
    across meshes automatically.
    """
    return [
        (key, find_best_target(required, candidates))
        for key, required in items
    ]


def export_model_objects(context, use_selection=True):
    """Return ``(armature, meshes)`` for a BMD export."""
    objs = (context.selected_objects if use_selection
            else [o for o in context.scene.objects if o.visible_get()])
    meshes = [o for o in objs if o.type == "MESH"]
    meshes.sort(key=lambda o: (o.get("bmd_shape_index", 1e9),
                               o.get("bmd_order", 1e9), o.name))
    armatures = [o for o in objs if o.type == "ARMATURE"]
    for mesh in meshes:
        arm = target_armature(mesh)
        if arm is not None and arm not in armatures:
            armatures.append(arm)
    arm = armatures[0] if len(armatures) == 1 else None
    return arm, meshes


BMD_NO_MESH_ERROR = (
    "No mesh objects to export (select the meshes, or disable "
    "'Selected Only'). Initialize custom meshes as BMD first.")


def bmd_export_objects(context, use_selection=True):
    """Return ``(armatures, meshes, valid)`` for a BMD export.

    Only BMD armatures and meshes deformed by them are accepted.  With
    ``use_selection`` every selected object must belong to that model; an
    unrelated selection makes the export invalid.  Without it, all BMD models
    in the scene are collected and unrelated meshes are skipped.
    """
    scene = getattr(context, "scene", None)
    selected = list(getattr(context, "selected_objects", ()) or ())

    if use_selection:
        if not selected:
            return [], [], False

        meshes = []
        selected_meshes = set()
        selected_armatures = []
        valid = True

        for obj in selected:
            if obj.type == "ARMATURE":
                if is_bmd_object(obj):
                    if obj not in selected_armatures:
                        selected_armatures.append(obj)
                else:
                    valid = False
            elif obj.type == "MESH":
                arm = target_armature(obj)

                if (is_bmd_object(obj) and arm is not None
                        and is_bmd_object(arm)):
                    if obj not in selected_meshes:
                        meshes.append(obj)
                        selected_meshes.add(obj)
                else:
                    valid = False
            else:
                valid = False

        if scene is not None:
            for arm in selected_armatures:
                for obj in scene.objects:
                    if (obj.type == "MESH" and obj not in selected_meshes
                            and target_armature(obj) is arm
                            and is_bmd_object(obj)):
                        meshes.append(obj)
                        selected_meshes.add(obj)
    else:
        valid = True
        meshes = []
        selected_meshes = set()

        if scene is not None:
            for obj in scene.objects:
                if (obj.type != "MESH" or not obj.visible_get()
                        or not is_bmd_object(obj)):
                    continue

                arm = target_armature(obj)

                if arm is not None and is_bmd_object(arm):
                    meshes.append(obj)
                    selected_meshes.add(obj)

    armatures = []

    for mesh in meshes:
        arm = target_armature(mesh)

        if arm is not None and arm not in armatures:
            armatures.append(arm)

    if use_selection:
        for arm in selected_armatures:
            if arm not in armatures:
                armatures.append(arm)

    if not valid or not meshes or not armatures:
        return [], [], False

    meshes.sort(key=lambda o: (o.get("bmd_shape_index", 1e9),
                               o.get("bmd_order", 1e9), o.name))
    return armatures, meshes, True


def model_mesh(context, owner=None):
    """Selected mesh, or the first mesh deformed by *owner* (an armature)."""
    obj = getattr(context, "active_object", None)
    if obj is not None and obj.type == "MESH":
        return obj
    for selected in getattr(context, "selected_objects", ()) or ():
        if selected.type == "MESH":
            return selected
    if owner is not None:
        for candidate in context.scene.objects:
            if candidate.type == "MESH" and target_armature(candidate) == owner:
                return candidate
    return None

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

# Formats the exporter can encode.
EXPORT_FORMAT_ITEMS = [
    ("CMPR", "CMPR",
     "4-bit block compression (DXT1-like); 1-bit transparency. Smallest files."),
    ("RGB5A3", "RGB5A3",
     "16-bit: RGB555 opaque, or RGB444 + 3-bit alpha."),
    ("RGBA32", "RGBA32",
     "32-bit RGBA8888; 8-bit alpha. Largest."),
    ("RGB565", "RGB565",
     "16-bit RGB565, no alpha."),
    ("IA8", "IA8",
     "16-bit grayscale + 8-bit alpha."),
    ("IA4", "IA4",
     "8-bit grayscale + 4-bit alpha."),
    ("I8", "I8",
     "8-bit grayscale, no alpha."),
    ("I4", "I4",
     "4-bit grayscale, no alpha."),
]

# Indexed formats: all are re-encoded from any image with the octree quantizer.
PALETTE_FORMAT_ITEMS = [
    ("C4", "C4", "4-bit indexed, 16 colors (octree-quantized RGBA)."),
    ("C8", "C8", "8-bit indexed, 256 colors (octree-quantized RGBA)."),
    ("C14X2", "C14X2", "14-bit indexed, 16,384 colors (octree-quantized RGBA)."),
]

IMAGE_FORMAT_ITEMS = list(EXPORT_FORMAT_ITEMS + PALETTE_FORMAT_ITEMS)
EXPORT_FORMAT_NAMES = {item[0] for item in EXPORT_FORMAT_ITEMS}
PALETTE_FORMAT_NAMES = {item[0] for item in PALETTE_FORMAT_ITEMS}
ENCODABLE_PALETTE_NAMES = {"C4", "C8", "C14X2"}

FORMAT_ITEMS = list(IMAGE_FORMAT_ITEMS)


def image_format_allowed(image, fmt):
    """C4, C8 and C14X2 can be encoded for any image."""
    if fmt not in PALETTE_FORMAT_NAMES:
        return True
    if fmt in ENCODABLE_PALETTE_NAMES:
        return True
    return str(image.get("bmd_orig_format", "")) == fmt

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

BTP_TRACK_DESCRIPTION = "BTP animation: TEX1 texture index"

MATERIAL_TRACK_ITEMS = [
    ("BTP", "Texture Index", BTP_TRACK_DESCRIPTION),
]


def _material_btp_index_changed(self, context):
    material = self.id_data
    if material is None:
        return
    try:
        from . import btp_preview
        value = int(self.btp_texture_index)
        screen = getattr(context, "screen", None) if context is not None else None
        if getattr(screen, "is_animation_playing", False):
            # Playback writes this property from the animation system.  Swapping
            # the node image here would modify data and tag the depsgraph in the
            # middle of the evaluation; the frame handler swaps it once instead.
            return
        if not btp_preview.updates_suppressed():
            btp_preview.auto_key(material, value, context)
        btp_preview.show_index(material, value)
    except Exception:
        pass


def _material_render_mode_changed(self, context):
    try:
        from . import blender_mat
        blender_mat.sync_shading(self.id_data)
    except Exception:
        pass


class BMDMaterialTrack(PropertyGroup):
    """One animatable material field shown in the Material Animation box."""
    type: EnumProperty(name="Track", items=MATERIAL_TRACK_ITEMS, default="BTP",
                       options=set())


class BMDMaterialProps(PropertyGroup):
    render_mode: EnumProperty(name="Render Mode", items=RENDER_ITEMS, default="AUTO",
                              description="How the game renders this material",
                              update=_material_render_mode_changed,
                              options=set())
    alpha_ref: IntProperty(name="Alpha Reference", default=128, min=0, max=255,
                           description="Alpha test threshold for the Cutout mode",
                           update=_material_render_mode_changed,
                           options=set())
    btp_texture_index: IntProperty(
        name="Texture Index", default=0, min=0, max=65535,
        description=BTP_TRACK_DESCRIPTION,
        update=_material_btp_index_changed)
    tracks: CollectionProperty(type=BMDMaterialTrack)


def material_track_types(material):
    """Track types already added to the material's Material Animation box."""
    if material is None:
        return []
    try:
        return [track.type for track in material.bmd.tracks]
    except Exception:
        return []


def ensure_material_track(material, track_type="BTP"):
    """Add the material animation track unless it is already there."""
    if material is None:
        return False
    try:
        tracks = material.bmd.tracks
    except Exception:
        return False
    if any(track.type == track_type for track in tracks):
        return False
    track = tracks.add()
    track.type = track_type
    return True


def _set_image_colorspace(image, name):
    """Change an image's colour space without losing its pixels.

    Blender regenerates a ``GENERATED`` image (which every imported texture is)
    when its colour space changes, which throws the buffer away - so the pixels
    are kept in memory and written back.
    """
    try:
        if image.colorspace_settings.name == name:
            return
    except Exception:
        return
    buffer = None
    try:
        if image.source == "GENERATED" and image.has_data:
            size = int(image.size[0]) * int(image.size[1]) * 4
            if size > 0:
                from array import array
                buffer = array("f", [0.0]) * size
                image.pixels.foreach_get(buffer)
    except Exception:
        buffer = None
    try:
        image.colorspace_settings.name = name
    except Exception:
        return
    if buffer is not None:
        try:
            image.pixels.foreach_set(buffer)
            image.update()
        except Exception:
            pass


def _sync_image_colorspace(image, fmt):
    """Data formats (I4/I8/IA4/IA8) are sampled raw, colour formats as sRGB."""
    from .j3d import gx_texture as gx
    try:
        value = fmt if isinstance(fmt, int) else gx.NAME_TO_FORMAT.get(str(fmt))
        if value is None:
            return
        wanted = ("Non-Color" if int(value) in gx.DATA_FORMATS else "sRGB")
    except (TypeError, ValueError):
        return
    _set_image_colorspace(image, wanted)


def _refresh_recovery_for_image(image):
    """Re-sync "Texture to TEV" in materials whose own slot uses *image*.

    An image that only sits in a ``bmd_tex<N>`` node (a BTP preview frame or a
    manual swap) is not part of the material, so its format must not touch the
    material's conversion nodes.
    """
    try:
        from . import blender_mat
    except Exception:
        return
    keys = {texture_key(image)}
    raw = image.get("bmd_keys")
    if raw:
        try:
            keys.update(str(key) for key in json.loads(raw))
        except Exception:
            pass
    keys.discard("")
    if not keys:
        return
    for material in bpy.data.materials:
        tree = getattr(material, "node_tree", None)
        if tree is None:
            continue
        try:
            names = {name for name in material_texture_keys(material) if name}
        except Exception:
            continue
        if not (names & keys):
            continue
        try:
            blender_mat.refresh_texture_recovery(material)
        except Exception:
            pass


def _image_format_changed(self, context):
    image = self.id_data
    if image is None:
        return

    if not image_format_allowed(image, self.format):
        orig = str(image.get("bmd_orig_format", ""))
        self.format = orig if orig in {i[0] for i in IMAGE_FORMAT_ITEMS} else "CMPR"
        return

    image.pop("bmd_format_auto", None)
    # The format decides how the image must be sampled (raw intensity vs sRGB),
    # and with it whether the material needs its "Texture to TEV" node.
    _sync_image_colorspace(image, self.format)
    _refresh_recovery_for_image(image)


def _image_key_changed(self, context):
    self.key_custom = True


def _image_wrap_changed(self, context):
    image = self.id_data
    for material in bpy.data.materials:
        tree = material.node_tree
        if tree is None:
            continue
        for node in tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is image:
                try:
                    from . import blender_mat
                    blender_mat.apply_wrap_to_node(
                        node, int(self.wrap_s), int(self.wrap_t), material)
                except Exception:
                    pass


def _image_max_lod_changed(self, context):
    self.max_lod_custom = True


def _update_slot_mip_levels(image, mips):
    """Keep the slots that use *image* on the automatic max LOD."""
    if image is None:
        return
    key = texture_key(image)
    for obj in bpy.data.objects:
        try:
            slots = obj.bmd_texture_list
        except Exception:
            continue
        was_loading = bool(obj.get("bmd_texture_loading", False))
        obj["bmd_texture_loading"] = True
        try:
            for slot in slots:
                if slot.image is image or (key and texture_slot_key(slot) == key):
                    if not slot.max_lod_custom:
                        slot.max_lod = (mips - 1) * 8
        finally:
            if not was_loading and "bmd_texture_loading" in obj:
                del obj["bmd_texture_loading"]


def _image_mip_count_changed(self, context):
    if self.max_lod_custom:
        return
    mips = max(1, int(self.mip_count))
    self.max_lod = (mips - 1) * 8
    self.max_lod_custom = False
    _update_slot_mip_levels(self.id_data, mips)


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


def slot_sampler(slot, image=None):
    """Sampler state of one TEX1 entry as a plain dict.

    Prefers the slot; falls back to the legacy image fields so slots saved
    before the sampler moved can still be read before migration runs.
    """
    if slot is not None and bool(getattr(slot, "sampler_ready", False)):
        data = {
            "wrap_s": int(slot.wrap_s), "wrap_t": int(slot.wrap_t),
            "min_filter": int(slot.min_filter),
            "mag_filter": int(slot.mag_filter),
            "min_lod": int(slot.min_lod), "max_lod": int(slot.max_lod),
            "max_lod_custom": bool(slot.max_lod_custom),
            "lod_bias": int(slot.lod_bias),
        }
        if slot.get("bmd_transparency") is not None:
            data["transparency"] = int(slot["bmd_transparency"])
        if slot.get("bmd_flags10"):
            data["flags10"] = str(slot["bmd_flags10"])
        return data
    if image is None and slot is not None:
        image = slot.image
    legacy = getattr(image, "bmd", None) if image is not None else None
    if legacy is None:
        return {}
    data = {
        "wrap_s": int(getattr(legacy, "wrap_s", 1)),
        "wrap_t": int(getattr(legacy, "wrap_t", 1)),
        "min_filter": int(getattr(legacy, "min_filter", 1)),
        "mag_filter": int(getattr(legacy, "mag_filter", 1)),
        "min_lod": int(getattr(legacy, "min_lod", 0)),
        "max_lod": int(getattr(legacy, "max_lod", 0)),
        "max_lod_custom": bool(getattr(legacy, "max_lod_custom", False)),
        "lod_bias": int(getattr(legacy, "lod_bias", 0)),
    }
    if image.get("bmd_transparency") is not None:
        data["transparency"] = int(image["bmd_transparency"])
    if image.get("bmd_flags10"):
        data["flags10"] = str(image["bmd_flags10"])
    return data


def sampler_for_image(image):
    """Sampler of the first texture slot that uses *image* (BTI/defaults)."""
    if image is None:
        return {}
    key = texture_key(image)
    for obj in bpy.data.objects:
        try:
            slots = obj.bmd_texture_list
        except Exception:
            continue
        for slot in slots:
            if slot.image is image or (key and texture_slot_key(slot) == key):
                return slot_sampler(slot, image)
    return slot_sampler(None, image)


def refresh_slot_sampler(owner, index):
    """Push one slot's sampler state into the materials that use it."""
    if owner is None:
        return
    try:
        slots = owner.bmd_texture_list
        if not 0 <= int(index) < len(slots):
            return
        slot = slots[int(index)]
    except Exception:
        return
    sampler = slot_sampler(slot)
    if not sampler:
        return
    interpolation = image_interpolation(sampler.get("mag_filter", 1))
    try:
        from . import blender_mat
    except Exception:
        return
    for material in _sync_materials(owner):
        raw = material.get("bmd_texture_slots")
        if not raw:
            continue
        try:
            mapping = json.loads(raw)
        except Exception:
            continue
        tree = getattr(material, "node_tree", None)
        if tree is None:
            continue
        for material_slot, texture_index in enumerate(mapping):
            if texture_index != int(index):
                continue
            node = tree.nodes.get(f"bmd_tex{material_slot}")
            if node is None or node.type != "TEX_IMAGE":
                continue
            try:
                blender_mat.set_node_sampler(
                    node, sampler.get("wrap_s", 1), sampler.get("wrap_t", 1),
                    interpolation)
                blender_mat.apply_wrap_to_node(
                    node, int(sampler.get("wrap_s", 1)),
                    int(sampler.get("wrap_t", 1)), material)
            except Exception:
                pass


def migrate_texture_samplers(owner):
    """Move merged image-level sampler state onto the texture slots.

    Old scenes stored wrap/filter/LOD/bias on the image, so every slot that
    shared the pixels also shared (and overwrote) those values.  Fill each
    slot once from its image and mark it; ``sampler_ready`` makes this
    idempotent.  Returns True when anything was migrated.
    """
    if owner is None:
        return False
    try:
        slots = owner.bmd_texture_list
    except Exception:
        return False
    if not len(slots):
        return False
    pending = [slot for slot in slots
               if not bool(getattr(slot, "sampler_ready", False))]
    if not pending:
        return False
    was_loading = bool(owner.get("bmd_texture_loading", False))
    owner["bmd_texture_loading"] = True
    try:
        for slot in pending:
            sampler = slot_sampler(None, slot.image or image_by_key(slot.key))
            slot.wrap_s = str(int(sampler.get("wrap_s", 1)))
            slot.wrap_t = str(int(sampler.get("wrap_t", 1)))
            slot.min_filter = str(int(sampler.get("min_filter", 1)))
            slot.mag_filter = str(int(sampler.get("mag_filter", 1)))
            slot.min_lod = int(sampler.get("min_lod", 0))
            slot.max_lod = int(sampler.get("max_lod", 0))
            slot.max_lod_custom = bool(sampler.get("max_lod_custom", False))
            slot.lod_bias = int(sampler.get("lod_bias", 0))
            if "transparency" in sampler:
                slot["bmd_transparency"] = int(sampler["transparency"])
            if sampler.get("flags10"):
                slot["bmd_flags10"] = str(sampler["flags10"])
            slot.sampler_ready = True
    finally:
        if not was_loading and "bmd_texture_loading" in owner:
            del owner["bmd_texture_loading"]
    return True


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
        raw = image.get("bmd_keys")
        if raw:
            try:
                if key in json.loads(raw):
                    return image
            except Exception:
                pass
    return None


def sync_texture_legacy(arm_obj):
    """Keep the old bmd_textures / bmd_texture_keys arrays in sync."""
    names, keys = [], []
    for slot in arm_obj.bmd_texture_list:
        key = str(slot.key or "") or texture_slot_key(slot)
        name = slot.name
        if slot.image is not None:
            # Legacy arrays stored image datablock names, not BMD names.
            name = slot.image.name
        names.append(name)
        keys.append(key)
    arm_obj["bmd_textures"] = names
    arm_obj["bmd_texture_keys"] = keys


def texture_list(arm_obj):
    """Read-only texture list: never migrates legacy data (safe in draw)."""
    if arm_obj is None:
        return ()
    return arm_obj.bmd_texture_list


def ensure_texture_list(arm_obj):
    """Return the texture list, rebuilding it from the legacy arrays if needed."""
    coll = arm_obj.bmd_texture_list
    if len(coll):
        migrate_texture_samplers(arm_obj)
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
    key = texture_key(image)
    for i, slot in enumerate(texture_list(arm_obj)):
        if slot.image == image or texture_slot_key(slot) == key:
            return i, slot
    return -1, None


def texture_label(arm_obj, image):
    """Human readable texture reference for the material panel."""
    index, slot = texture_slot(arm_obj, image)
    if slot is not None:
        return f"{index}: {slot.name or image.name}"
    return f"{image.name} (not in BMD list)"


def active_material_json(material):
    """JSON of the MAT3 data the material currently uses.

    An imported shader preset (``bmd_shader_material``) shadows the original
    ``bmd_material`` until it is reset, so the original stays available for the
    reset without a rebuild of the scene.
    """
    if material is None:
        return None
    if material.get("bmd_shader_active") and material.get("bmd_shader_material"):
        return str(material["bmd_shader_material"])
    return material.get("bmd_material")


def material_texture_slots(mat, owner=None, store=True):
    """BMD texture index per material texture slot (8 entries).

    ``owner``/``store`` are kept for the callers that still pass them; the
    mapping is stored on the material by the importer and by the texture UI.
    """
    raw = mat.get("bmd_texture_slots")
    slots = None
    if raw:
        try:
            slots = json.loads(raw)
        except Exception:
            slots = None
    if slots is None:
        return [None] * 8
    return list(slots)[:8]


def set_material_texture_slots(mat, slots):
    values = list(slots)[:8]
    while len(values) < 8:
        values.append(None)
    mat["bmd_texture_slots"] = json.dumps(values, separators=(",", ":"))


def material_texture_keys(mat):
    """Native texture key per material slot (8 entries), from the MAT3 data.

    The node tree may show a different image (BTP preview or a manual swap), so
    the material's own ``texNames`` is the authority for its texture slots.
    """
    stored = active_material_json(mat)
    if not stored:
        return []
    try:
        data = json.loads(stored)
    except Exception:
        return []
    return [str(name) if name else None
            for name in (data.get("texNames") or [])][:8]


def owner_meshes(owner):
    """Mesh objects whose texture list / materials belong to *owner*."""
    if owner is None:
        return []
    if owner.type == "MESH":
        return [owner]
    if owner.type == "ARMATURE":
        return [obj for obj in bpy.data.objects
                if obj.type == "MESH" and target_armature(obj) is owner]
    return []


def owner_materials(owner):
    """Materials used by *owner* (only these may follow its texture list)."""
    result, seen = [], set()
    for mesh in owner_meshes(owner):
        for slot in mesh.material_slots:
            material = slot.material
            if material is None or material.name in seen:
                continue
            seen.add(material.name)
            result.append(material)
    return result


def _sync_materials(owner):
    """Materials the texture list of *owner* is allowed to update."""
    if owner is None:
        return list(bpy.data.materials)
    return owner_materials(owner)


def set_active_mat3_json(material, text):
    """Store MAT3 JSON where the material currently reads it from."""
    if material.get("bmd_shader_active") and "bmd_shader_material" in material:
        material["bmd_shader_material"] = text
    else:
        material["bmd_material"] = text


def remap_material_texture_slots(owner, mapping):
    for material in _sync_materials(owner):
        if not is_bmd_material(material):
            continue
        slots = material_texture_slots(material, owner)
        new = [mapping.get(s, s) if s is not None else None for s in slots]
        if new != slots:
            set_material_texture_slots(material, new)


def remove_material_texture_slot(owner, index):
    for material in _sync_materials(owner):
        if not is_bmd_material(material):
            continue
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
    return sum(1 for material in _sync_materials(owner)
               if index in material_texture_slots(material, owner, store=False))


def count_texture_users(key):
    """Materials that reference the texture *key* (hidden key, never the name)."""
    if not key:
        return 0
    count = 0
    for material in bpy.data.materials:
        tree = material.node_tree
        stored = active_material_json(material)
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
    for material in _sync_materials(owner):
        if not is_bmd_material(material):
            continue
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
        stored = active_material_json(material)
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


def _refresh_recovery(material):
    """Re-sync the "Texture to TEV" nodes of a material after an image change."""
    try:
        from . import blender_mat
        blender_mat.refresh_texture_recovery(material)
    except Exception:
        pass


def retarget_material_textures(old_key, new_key, new_image, slot_index=None,
                               owner=None, drop_slot=False):
    """Point the materials linked to the changed BMD texture at its replacement."""
    # A slot that had no image yet has no old key; the slot index is enough to
    # find the materials that reference it (shader presets create such slots).
    if not old_key and slot_index is None:
        return
    for material in _sync_materials(owner):
        if not is_bmd_material(material):
            continue
        tree = material.node_tree
        slots = (material_texture_slots(material, owner)
                 if slot_index is not None and owner is not None else None)
        if slots is not None and any(s is not None for s in slots):
            stored = active_material_json(material)
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
                set_active_mat3_json(material,
                                     json.dumps(data, separators=(",", ":")))
            if slots_changed:
                set_material_texture_slots(material, slots)
            _refresh_recovery(material)
            continue
        stored = active_material_json(material)
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
                    set_active_mat3_json(material,
                                         json.dumps(data, separators=(",", ":")))
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
        _refresh_recovery(material)


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
            # The BMD name belonged to the removed texture; a slot that never
            # had an image keeps the name it was created with (shader presets
            # create named, empty slots that the user fills in later).
            self.name = ""
        # Keep self.key so a new image can restore the material link.
        sync_texture_legacy(arm_obj)
        return
    retarget_material_textures(old_key, new_key, image, slot_index, arm_obj)
    self.key = new_key
    # Replacing an assigned texture renames the slot after the new image.  A slot
    # that had no image yet keeps the name it was created with (a shader preset
    # creates named, empty slots that the user fills in later).
    if old_key or not self.name:
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


def _slot_sampler_changed(self, context):
    owner = self.id_data
    if owner is None or owner.get("bmd_texture_loading"):
        return
    try:
        index = next((i for i, slot in enumerate(owner.bmd_texture_list)
                      if slot == self), None)
    except Exception:
        index = None
    if index is not None:
        refresh_slot_sampler(owner, index)


def _slot_max_lod_changed(self, context):
    owner = self.id_data
    if owner is not None and owner.get("bmd_texture_loading"):
        return
    self.max_lod_custom = True
    _slot_sampler_changed(self, context)


class BMDTextureSlot(PropertyGroup):
    image: PointerProperty(type=bpy.types.Image, name="Image",
                           update=_texture_slot_image_changed, options=set())
    name: StringProperty(name="BMD Name", update=_texture_slot_name_changed,
                         options=set())
    key: StringProperty(options={"HIDDEN"})
    # GX sampler state belongs to the TEX1 entry, not to the pixel block: two
    # entries may share one image and still clamp/repeat it differently.
    wrap_s: EnumProperty(name="Wrap S", items=WRAP_ITEMS, default="1",
                         update=_slot_sampler_changed, options=set())
    wrap_t: EnumProperty(name="Wrap T", items=WRAP_ITEMS, default="1",
                         update=_slot_sampler_changed, options=set())
    min_filter: EnumProperty(name="Min Filter", items=MIN_FILTER_ITEMS,
                             default="1", update=_slot_sampler_changed,
                             options=set())
    mag_filter: EnumProperty(name="Mag Filter", items=MAG_FILTER_ITEMS,
                             default="1", update=_slot_sampler_changed,
                             options=set())
    min_lod: IntProperty(name="Min LOD", default=0, min=-128, max=127,
                         description="Raw GX LOD value (1/8 units)",
                         update=_slot_sampler_changed, options=set())
    max_lod: IntProperty(name="Max LOD", default=0, min=-128, max=127,
                         description="Raw GX LOD value (1/8 units)",
                         update=_slot_max_lod_changed, options=set())
    max_lod_custom: BoolProperty(default=False, options={"HIDDEN"})
    lod_bias: IntProperty(name="LOD Bias", default=0, min=-32768, max=32767,
                          description="Raw GX LOD bias (1/100 units)",
                          update=_slot_sampler_changed, options=set())
    # False on slots saved before the sampler state moved here; the image still
    # carries the merged values and ``migrate_texture_samplers`` copies them.
    sampler_ready: BoolProperty(default=False, options={"HIDDEN"})


class BMDImageProps(PropertyGroup):
    format: EnumProperty(
        name="GX Format", items=IMAGE_FORMAT_ITEMS, default="CMPR",
        description="Texture format used when exporting this image",
        update=_image_format_changed, options=set())
    key: StringProperty(
        name="BMD Key",
        description="Unique key used by materials to reference this texture. "
                    "Empty = use the image datablock name",
        update=_image_key_changed,
        options=set(),
    )
    key_custom: BoolProperty(default=False, options={"HIDDEN"})
    # Legacy sampler fields.  The sampler state now lives on BMDTextureSlot;
    # these stay defined so old .blend files load their merged values and
    # ``migrate_texture_samplers`` can copy them onto the slots.
    wrap_s: EnumProperty(name="Wrap S", items=WRAP_ITEMS, default="1",
                         update=_image_wrap_changed, options=set())
    wrap_t: EnumProperty(name="Wrap T", items=WRAP_ITEMS, default="1",
                         update=_image_wrap_changed, options=set())
    min_filter: EnumProperty(name="Min Filter", items=MIN_FILTER_ITEMS,
                             default="1", options=set())
    mag_filter: EnumProperty(name="Mag Filter", items=MAG_FILTER_ITEMS, default="1",
                             update=_image_filter_changed, options=set())
    min_lod: IntProperty(name="Min LOD", default=0, min=-128, max=127,
                         description="Raw GX LOD value (1/8 units)",
                         options=set())
    max_lod: IntProperty(name="Max LOD", default=0, min=-128, max=127,
                         description="Raw GX LOD value (1/8 units)",
                         update=_image_max_lod_changed, options=set())
    max_lod_custom: BoolProperty(default=False, options={"HIDDEN"})
    lod_bias: IntProperty(name="LOD Bias", default=0, min=-32768, max=32767,
                          description="Raw GX LOD bias (1/100 units)",
                          options=set())
    mip_count: IntProperty(name="Mip Levels", default=1, min=1, max=12,
                           description="Number of mip levels",
                           update=_image_mip_count_changed, options=set())
    external: BoolProperty(name="External Texture", default=False,
                           description="Store this texture outside the BMD (header-only TEX1 "
                                       "entry). Use File > Export > J3D Textures (.bti) to write "
                                       "external textures to a folder. Tick this for any BMD, "
                                       "disk or scene texture to make it external",
                           options=set())


def axis_fix(arm_obj):
    """Bone frame correction that was used when the armature was imported."""
    return R_FIX if arm_obj.get("bmd_bone_axis", "X") == "X" else np.eye(4)


def mesh_object_poll(_self, obj):
    """PointerProperty poll for mesh objects."""
    return obj is not None and obj.type == "MESH"


def bmd_mesh_poll(_self, obj):
    """PointerProperty poll for meshes initialized/imported as BMD."""
    return obj is not None and obj.type == "MESH" and is_bmd_object(obj)


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


# ---------------------------------------------------------------------------
# Material node helpers shared by the importer and the material preview
# ---------------------------------------------------------------------------

def material_meshes(material, owner=None):
    """Mesh datablocks that use *material*, optionally limited to *owner*."""
    if material is None:
        return []

    scopes = []
    if owner is not None:
        if owner.type == "MESH":
            scopes.append([owner])
        elif owner.type == "ARMATURE":
            scopes.append([obj for obj in bpy.data.objects
                           if obj.type == "MESH"
                           and target_armature(obj) is owner])
    scopes.append([obj for obj in bpy.data.objects if obj.type == "MESH"])

    for scope in scopes:
        meshes = []
        for obj in scope:
            if obj.data is None:
                continue
            if not any(slot.material is material for slot in obj.material_slots):
                continue
            if obj.data not in meshes:
                meshes.append(obj.data)
        if meshes:
            return meshes

    return []


def uv_layer_names(material, owner=None):
    """UV layer names of the meshes that use *material*, in mesh order."""
    names = []
    for mesh in material_meshes(material, owner):
        for layer in mesh.uv_layers:
            if layer.name not in names:
                names.append(layer.name)

    return names


def uv_layer_name(material, index=0, owner=None):
    """Real UV layer name for UV channel *index* of *material*'s meshes.

    BMD import creates ``UV<n>`` layers after the materials, so until a mesh
    uses the material the importer's naming is returned unchanged.  Custom
    meshes keep their own layer names (``UVMap``, ``UVMap.001``, ...), which
    are used instead of a non-existent ``UV<n>``.
    """
    try:
        index = max(0, int(index))
    except (TypeError, ValueError):
        index = 0

    preferred = f"UV{index}"
    names = uv_layer_names(material, owner)
    if not names:
        # No mesh uses the material yet (BMD import creates the UV<n> layers
        # afterwards) -> keep the importer's naming.  Meshes without any UV
        # layer fall back to the active UV map instead of a missing name.
        return "" if material_meshes(material, owner) else preferred

    if preferred in names:
        return preferred

    for mesh in material_meshes(material, owner):
        if len(mesh.uv_layers) > index:
            return mesh.uv_layers[index].name

    return names[0]


# ---------------------------------------------------------------------------
# Explicitly initialize a custom mesh as a BMD model
# ---------------------------------------------------------------------------

def default_vertex_formats():
    """F32 position/normal/UV formats used for a newly initialized model."""
    return ([(9, 0, 4, 0), (10, 0, 4, 0)]
            + [(13 + index, 1, 4, 0) for index in range(8)])


def _format_table_json(formats):
    return json.dumps({str(int(attr)): [int(components), int(typ), int(frac)]
                       for attr, components, typ, frac in formats},
                      separators=(",", ":"))


def _read_vertex_formats(obj):
    raw = obj.get("bmd_vtx_formats")

    if raw:
        try:
            table = json.loads(raw) if isinstance(raw, str) else raw
            return [(int(attr), *value) for attr, value in table.items()]
        except (TypeError, ValueError, AttributeError):
            pass

    return None


def _store_vertex_formats(obj, formats):
    obj["bmd_vtx_formats"] = _format_table_json(formats)
    apply_vertex_formats(obj, formats)


def _initialize_armature_data(arm):
    """Give an arbitrary armature the BMD fields needed by the panels.

    Deliberately does not write bone indices or ``bmd_bone_names``: a custom
    armature must keep using its *current* bone count and hierarchy for BCK
    compatibility, even after bones are added or removed.
    """
    if arm is None or arm.type != "ARMATURE":
        return

    formats = _read_vertex_formats(arm) or default_vertex_formats()
    _store_vertex_formats(arm, formats)

    if "bmd_bone_axis" not in arm:
        arm["bmd_bone_axis"] = "X"
    if "bmd_inf_flags" not in arm:
        arm["bmd_inf_flags"] = 0
    if "bmd_textures" not in arm:
        arm["bmd_textures"] = []
    if "bmd_texture_keys" not in arm:
        arm["bmd_texture_keys"] = []


def _new_bmd_armature(context, mesh):
    """Create a single-root BMD armature for a mesh without one."""
    base = mesh.name
    data = bpy.data.armatures.new(f"{base}_armature")
    arm = bpy.data.objects.new(f"{base}_armature", data)
    collections = list(getattr(mesh, "users_collection", ()) or ())

    if collections:
        collections[0].objects.link(arm)
    else:
        context.collection.objects.link(arm)

    arm.show_in_front = True
    arm.matrix_world = mesh.matrix_world.copy()

    view_layer = context.view_layer
    previous_active = view_layer.objects.active
    previous_selected = [obj for obj in view_layer.objects
                         if obj.select_get()]

    if previous_active is not None and previous_active.mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass

    for obj in view_layer.objects:
        obj.select_set(False)

    view_layer.objects.active = arm
    arm.select_set(True)

    try:
        bpy.ops.object.mode_set(mode="EDIT")
        bone = data.edit_bones.new("root")
        bone.head = (0.0, 0.0, 0.0)
        size = max(mesh.dimensions) * 0.1
        bone.tail = (0.0, max(0.1, size), 0.0)
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        if arm.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass

    _initialize_armature_data(arm)

    # Keep the mesh world transform and bind it to the new armature.
    world = mesh.matrix_world.copy()

    if mesh.parent is None:
        mesh.parent = arm
        mesh.parent_type = "OBJECT"
        mesh.matrix_parent_inverse = arm.matrix_world.inverted()
        mesh.matrix_world = world

    if not any(mod.type == "ARMATURE" and mod.object == arm
               for mod in mesh.modifiers):
        modifier = mesh.modifiers.new("Armature", "ARMATURE")
        modifier.object = arm

    if not any(group.name == "root" for group in mesh.vertex_groups):
        group = mesh.vertex_groups.new(name="root")
        group.add(list(range(len(mesh.data.vertices))), 1.0, "REPLACE")

    for obj in view_layer.objects:
        obj.select_set(obj is mesh or obj is arm or obj in previous_selected)

    view_layer.objects.active = mesh
    return arm


def _selected_armature(context):
    """The armature selected for binding a custom mesh, if any."""
    active = getattr(context, "active_object", None)

    if active is not None and active.type == "ARMATURE":
        return active

    selected = [obj for obj in (getattr(context, "selected_objects", ()) or ())
                if obj.type == "ARMATURE"]

    if not selected:
        return None

    for arm in selected:
        if is_bmd_object(arm):
            return arm

    return selected[0]


def _bind_mesh_to_armature(mesh, arm):
    """Bind *mesh* to *arm* without changing its world transform."""
    world = mesh.matrix_world.copy()

    if mesh.parent is None:
        mesh.parent = arm
        mesh.parent_type = "OBJECT"
        mesh.matrix_parent_inverse = arm.matrix_world.inverted()
        mesh.matrix_world = world

    if not any(mod.type == "ARMATURE" and mod.object == arm
               for mod in mesh.modifiers):
        modifier = mesh.modifiers.new("Armature", "ARMATURE")
        modifier.object = arm


def initialize_as_bmd(context, mesh):
    """Add the BMD metadata and (if needed) armature to a custom mesh.

    Called only from the explicit Utility > Initialize as BMD operator.
    """
    if mesh is None or mesh.type != "MESH":
        return None

    arm = target_armature(mesh)

    if arm is None:
        arm = _selected_armature(context)

    if arm is None:
        arm = _new_bmd_armature(context, mesh)
    else:
        _initialize_armature_data(arm)
        _bind_mesh_to_armature(mesh, arm)

    try:
        context.view_layer.update()
    except Exception:
        pass

    return arm


def model_label(arm):
    """Human-readable label for an imported BMD model/armature."""
    source = str(arm.get("bmd_source", ""))

    if source:
        return os.path.basename(source)

    if arm.users_collection:
        return arm.users_collection[0].name

    return arm.name


def euler_to_matrices(euler):
    """Vectorised version of :func:`j3d.model.euler_to_matrix`."""
    euler = np.asarray(euler, dtype=np.float64)
    rx, ry, rz = euler[:, 0], euler[:, 1], euler[:, 2]
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    out = np.empty((len(euler), 3, 3), dtype=np.float64)
    out[:, 0, 0] = cz * cy
    out[:, 0, 1] = cz * sy * sx - sz * cx
    out[:, 0, 2] = cz * sy * cx + sz * sx
    out[:, 1, 0] = sz * cy
    out[:, 1, 1] = sz * sy * sx + cz * cx
    out[:, 1, 2] = sz * sy * cx - cz * sx
    out[:, 2, 0] = -sy
    out[:, 2, 1] = cy * sx
    out[:, 2, 2] = cy * cx
    return out


def trs_matrices(scale, euler, trans):
    """Vectorised version of :func:`trs_matrix` for a stack of samples."""
    scale = np.asarray(scale, dtype=np.float64)
    trans = np.asarray(trans, dtype=np.float64)
    n = len(scale)
    out = np.zeros((n, 4, 4), dtype=np.float64)
    out[:, 3, 3] = 1.0
    out[:, :3, :3] = euler_to_matrices(euler) * scale[:, None, :]
    out[:, :3, 3] = trans
    return out


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


def j3d_local_to_blender_batch(L, s, fix, is_root=False):
    """Vectorised :func:`j3d_local_to_blender` for ``(n, 4, 4)`` matrices."""
    M = C @ L @ C_INV @ fix
    if np.ndim(is_root):
        child = ~np.asarray(is_root, dtype=bool)
        if child.any():
            M[child] = fix.T @ M[child]
    elif not is_root:
        M = fix.T @ M
    M[:, :3, 3] *= s
    return M


def blender_local_to_j3d(M, s, fix, is_root=False):
    """Inverse of :func:`j3d_local_to_blender`."""
    M = M.copy()
    M[:3, 3] /= s
    if not is_root:
        M = fix @ M
    return C_INV @ M @ fix.T @ C


def blender_local_to_j3d_batch(M, s, fix, is_root=False):
    """Vectorised :func:`blender_local_to_j3d` for ``(n, 4, 4)`` matrices."""
    M = M.copy()
    M[:, :3, 3] /= s
    if np.ndim(is_root):
        child = ~np.asarray(is_root, dtype=bool)
        if child.any():
            M[child] = fix @ M[child]
    elif not is_root:
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


def split_trs_batch(A):
    """Vectorised :func:`split_trs` for a stack of 3x3 matrices."""
    A = np.asarray(A, dtype=np.float64)
    n = len(A)
    if n == 0:
        return A.copy(), np.empty((0, 3)), np.empty(0)

    scale = np.linalg.norm(A, axis=1)
    scale[scale == 0] = 1.0
    R = A / scale[:, None, :]

    neg = np.linalg.det(R) < 0.0
    if neg.any():
        indices = np.nonzero(neg)[0]
        axes = np.argmin(np.abs(scale[indices]), axis=1)
        for i, axis in zip(indices, axes):
            scale[i, axis] = -scale[i, axis]
            R[i, :, axis] = -R[i, :, axis]

    U, _S, Vt = np.linalg.svd(R)
    Rp = U @ Vt
    neg = np.linalg.det(Rp) < 0.0
    if neg.any():
        U[neg, :, -1] *= -1.0
        Rp = U @ Vt

    shear = np.abs(R - Rp).reshape(n, -1).max(axis=1)
    return Rp, scale, shear


def matrix_to_quat_batch(R):
    """Vectorised :func:`matrix_to_quat` for a stack of 3x3 rotations."""
    R = np.asarray(R, dtype=np.float64)
    n = len(R)
    if n == 0:
        return np.empty((0, 4))

    r00, r01, r02 = R[:, 0, 0], R[:, 0, 1], R[:, 0, 2]
    r10, r11, r12 = R[:, 1, 0], R[:, 1, 1], R[:, 1, 2]
    r20, r21, r22 = R[:, 2, 0], R[:, 2, 1], R[:, 2, 2]
    trace = r00 + r11 + r22

    b0 = trace > 0.0
    b1 = (~b0) & (r00 > r11) & (r00 > r22)
    b2 = (~b0) & (~b1) & (r11 > r22)
    b3 = ~(b0 | b1 | b2)

    k0 = np.maximum(np.sqrt(np.maximum(trace + 1.0, 0.0)) * 2.0, 1e-12)
    k1 = np.maximum(np.sqrt(np.maximum(1.0 + r00 - r11 - r22, 0.0)) * 2.0,
                    1e-12)
    k2 = np.maximum(np.sqrt(np.maximum(1.0 + r11 - r00 - r22, 0.0)) * 2.0,
                    1e-12)
    k3 = np.maximum(np.sqrt(np.maximum(1.0 + r22 - r00 - r11, 0.0)) * 2.0,
                    1e-12)

    q0 = np.stack([0.25 * k0, (r21 - r12) / k0, (r02 - r20) / k0,
                   (r10 - r01) / k0], axis=1)
    q1 = np.stack([(r21 - r12) / k1, 0.25 * k1, (r01 + r10) / k1,
                   (r02 + r20) / k1], axis=1)
    q2 = np.stack([(r02 - r20) / k2, (r01 + r10) / k2, 0.25 * k2,
                   (r12 + r21) / k2], axis=1)
    q3 = np.stack([(r10 - r01) / k3, (r02 + r20) / k3, (r12 + r21) / k3,
                   0.25 * k3], axis=1)

    q = np.where(b0[:, None], q0,
                 np.where(b1[:, None], q1,
                          np.where(b2[:, None], q2, q3)))
    norm = np.linalg.norm(q, axis=1)
    norm[norm == 0.0] = 1.0
    return q / norm[:, None]


def matrix_to_euler_batch(R):
    """Vectorised :func:`j3d.model.matrix_to_euler`."""
    R = np.asarray(R, dtype=np.float64)
    sy = np.clip(-R[:, 2, 0], -1.0, 1.0)
    y = np.arcsin(sy)
    normal = np.abs(sy) < 0.999999
    x = np.where(normal, np.arctan2(R[:, 2, 1], R[:, 2, 2]),
                 np.arctan2(-R[:, 1, 2], R[:, 1, 1]))
    z = np.where(normal, np.arctan2(R[:, 1, 0], R[:, 0, 0]), 0.0)
    return np.stack([x, y, z], axis=1)


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
    bmd = image.bmd if "bmd" in image else None
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


def strip_blender_suffix(name):
    """Remove Blender's automatic ``.001`` duplicate suffix from an exported name."""
    text = str(name)
    base, sep, suffix = text.rpartition(".")
    if sep and len(suffix) == 3 and suffix.isdigit():
        return base
    return text


def material_export_name(material):
    """Material name written to a J3D file (BMD ``MAT3``, BTP entries).

    The name stored on import is authoritative; a material without one (created
    in Blender) exports under its datablock name without Blender's automatic
    ``.001`` suffix.  The animation exporters use the same helper so a BTP entry
    always matches the name inside the BMD.
    """
    if material is None:
        return "default"
    stored = material.get("bmd_name")
    if stored:
        return str(stored)
    return strip_blender_suffix(material.name)


def material_shader_name(material):
    """Current material name used by shader presets (file name and ``Name``).

    Unlike :func:`material_export_name` this follows a rename in Blender and
    never uses the name stored in the BMD.
    """
    return str(material.name) if material is not None else "material"


def material_preset_payload(material, owner=None):
    """Shader preset for *material* (active shader data)."""
    from . import blender_mat
    from .j3d import mat3_json

    data = blender_mat.material_data(material) or {}
    names = [None] * 8
    try:
        slots = material_texture_slots(material, owner) if owner is not None else []
        textures = owner.bmd_texture_list if owner is not None else ()
    except Exception:
        slots, textures = [], ()
    for slot, index in enumerate(list(slots or [])[:8]):
        if index is None:
            continue
        if 0 <= index < len(textures):
            names[slot] = textures[index].name or None
    return mat3_json.to_superbmd(data, material_shader_name(material), names)



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


def _register_type_property(owner, name, prop):
    try:
        setattr(owner, name, prop)
    except Exception:
        import traceback
        traceback.print_exc()


def register_classes():
    for cls in (BMDMaterialTrack, BMDMaterialProps, BMDImageProps,
                BMDTextureSlot, BMD_VertexFormatSettings):
        try:
            bpy.utils.register_class(cls)
        except Exception:
            import traceback
            traceback.print_exc()

    _register_type_property(
        bpy.types.Object, "bmd_scale",
        FloatProperty(name="BMD Scale", get=_bmd_scale_get))
    _register_type_property(
        bpy.types.Object, "bmd_convert_units",
        BoolProperty(name="Convert Units", get=_bmd_convert_units_get))
    for name in ("bmd_bone_axis", "bmd_vtx_formats", "bmd_sub_version",
                 "bmd_shape_nodes"):
        _register_type_property(
            bpy.types.Object, name,
            StringProperty(name=name, get=_bmd_string_get(name)))
    _register_type_property(
        bpy.types.Object, "bmd_inf_flags",
        IntProperty(name="BMD INF Flags", get=_bmd_int_get("bmd_inf_flags")))
    _register_type_property(
        bpy.types.Material, "bmd", PointerProperty(type=BMDMaterialProps))
    _register_type_property(
        bpy.types.Image, "bmd", PointerProperty(type=BMDImageProps))
    _register_type_property(
        bpy.types.Object, "bmd_texture_list",
        CollectionProperty(type=BMDTextureSlot))
    _register_type_property(
        bpy.types.Object, "bmd_texture_index",
        IntProperty(default=0, min=0, options=set()))
    _register_type_property(
        bpy.types.Object, "bmd_vtx",
        PointerProperty(type=BMD_VertexFormatSettings))


def _remove_type_property(owner, name):
    if not hasattr(owner, name):
        return

    try:
        delattr(owner, name)
    except Exception:
        pass


def unregister_classes():
    for name in ("bmd_vtx", "bmd_texture_index", "bmd_texture_list",
                 "bmd_convert_units", "bmd_scale", "bmd_bone_axis",
                 "bmd_vtx_formats", "bmd_inf_flags", "bmd_shape_nodes",
                 "bmd_sub_version"):
        _remove_type_property(bpy.types.Object, name)
    _remove_type_property(bpy.types.Image, "bmd")
    _remove_type_property(bpy.types.Material, "bmd")

    for cls in (BMD_VertexFormatSettings, BMDTextureSlot,
                BMDImageProps, BMDMaterialProps, BMDMaterialTrack):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass
