"""Collect Blender data into an :class:`ExportModel` and write it as BMD3."""

import base64
import copy
import hashlib
import heapq
import json
import math
import os
import zlib

import bpy
import numpy as np

from . import blender_common as bc
from . import bmd_metadata
from .j3d import gx_texture as gx
from .j3d import material as mat3
from .j3d import model as j3d
from .j3d import writer


class ExportError(Exception):
    pass


def _warn(warn, message):
    if warn is not None:
        warn(message)
    else:
        print(f"[BMD] {message}")


def _same_geometry(original, evaluated):
    """True when the modifier result has identical topology and positions."""
    if original is evaluated:
        return True
    if (len(original.vertices) != len(evaluated.vertices) or
            len(original.loops) != len(evaluated.loops) or
            len(original.polygons) != len(evaluated.polygons)):
        return False
    if bmd_metadata.topology_hash(original) != bmd_metadata.topology_hash(evaluated):
        return False

    count = len(original.vertices) * 3
    original_co = np.empty(count, np.float64)
    evaluated_co = np.empty(count, np.float64)
    original.vertices.foreach_get("co", original_co)
    evaluated.vertices.foreach_get("co", evaluated_co)
    return bool(np.allclose(original_co, evaluated_co, rtol=0.0, atol=1e-6))


def _format_override(choice, components, fraction):
    """Translate a UI format choice into a writer override."""
    if choice == "AUTO":
        return None
    if choice == "F32":
        return (components, 4, 0)
    if choice == "S16":
        return (components, 3, int(fraction))
    if choice == "S16_OPT":
        return "OPTIMIZE"
    if choice.startswith("S16_"):
        return (components, 3, int(choice[4:]))
    return None


def export_bmd(context, filepath, scale=100.0, use_selection=True, apply_modifiers=False,
               source_file="", allow_missing_textures=False,
               keep_vertex_formats=True, report=None,
               optimize_bmd=False, bls_compatible=False,
               position_format="AUTO", normal_format="AUTO", uv_format="AUTO",
               position_frac=6, normal_frac=14, uv_frac=14):
    armatures, mesh_objs, valid = bc.bmd_export_objects(
        context, use_selection)

    if not valid or not mesh_objs:
        raise ExportError(bc.BMD_NO_MESH_ERROR)

    if len(armatures) > 1:
        raise ExportError(
            "The export contains meshes from multiple armatures. "
            "Select one armature and its meshes, or hide the other models.")

    arm_obj = armatures[0] if armatures else None
    mesh_objs.sort(key=lambda o: (o.get("bmd_shape_index", 1e9), o.get("bmd_order", 1e9), o.name))
    model = writer.ExportModel()

    # Backwards-compatible shortcuts for callers of the old API.
    if optimize_bmd and position_format == "AUTO":
        position_format = "S16_OPT"
    if bls_compatible:
        position_format = "F32"
        normal_format = "F32"

    model.format_overrides = {
        j3d.VA_POS: _format_override(position_format, 1, position_frac),
        j3d.VA_NRM: _format_override(normal_format, 0, normal_frac),
    }
    uv_override = _format_override(uv_format, 1, uv_frac)
    for t in range(8):
        model.format_overrides[j3d.VA_TEX0 + t] = uv_override

    if optimize_bmd:
        model.format_overrides[j3d.VA_POS] = "OPTIMIZE"
    if bls_compatible:
        model.format_overrides[j3d.VA_POS] = (1, 4, 0)
        model.format_overrides[j3d.VA_NRM] = (0, 4, 0)

    model.preserve_vertex_order = (
        len(mesh_objs) == 1 or
        any(o.data is not None and o.data.attributes.get("bmd_node") is not None
            for o in mesh_objs))
    bone_index = _build_joints(model, arm_obj, scale)
    joint_map = {}
    if arm_obj is not None:
        owners, _duplicates = joint_index_owners(
            arm_obj.data, arm_obj.get("bmd_bone_names", []))
        for old, bone in owners.items():
            new = bone_index.get(bone.name)
            if new is not None:
                joint_map[int(old)] = int(new)

    def warn(message):
        if report:
            report({"WARNING"}, message)
        else:
            print(f"[BMD] {message}")

    mats = _MaterialCollector(model, warn)
    texture_owner = None
    if arm_obj is not None and ("bmd_textures" in arm_obj or len(arm_obj.bmd_texture_list)):
        texture_owner = arm_obj
    if texture_owner is None:
        for obj in mesh_objs:
            if "bmd_textures" in obj or len(obj.bmd_texture_list):
                texture_owner = obj
                break
    if texture_owner is not None:
        mats.preload_original_textures(texture_owner, source_file, allow_missing_textures, report)
    # Keep every imported material slot, including slots that currently have
    # no faces.  The game addresses materials by index, so dropping an unused
    # original material would shift every later material index.  New materials
    # are built lazily with the mesh that uses them (they have no original
    # index to preserve).
    for obj in mesh_objs:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is not None and mat.get("bmd_material"):
                mats.register_material(mat)
    normal_updates = []
    for obj in mesh_objs:
        _export_mesh_object(model, obj, arm_obj, bone_index, mats, scale, apply_modifiers,
                            context, normal_updates, joint_map, warn)
    if not model.meshes:
        raise ExportError("The selected meshes have no faces to export")
    _add_empty_shapes(model, mesh_objs, mats, joint_map)
    mats.finish()
    # keep the original shape order: the game addresses shapes and materials by index
    model.meshes.sort(key=lambda m: (m.sort_key, m.order))

    if arm_obj is not None:
        model.inf_flags = int(arm_obj.get("bmd_inf_flags", 0))
        if keep_vertex_formats and arm_obj.get("bmd_vtx_formats"):
            model.vtx_formats = {int(k): tuple(v) for k, v in json.loads(arm_obj["bmd_vtx_formats"]).items()}
        sv = arm_obj.get("bmd_sub_version")
        if sv:
            model.sub_version = bytes.fromhex(sv)
    data = writer.write(model)
    for obj, indices, position_offset, vertex_order in normal_updates:
        if obj.data is None:
            continue
        if indices is None:
            bmd_metadata.clear_metadata(obj.data)
            continue
        try:
            bmd_metadata.write_metadata(
                obj.data, indices, model.normal_count, source="export",
                position_offset=position_offset, vertex_order=vertex_order)
        except ValueError as exc:
            warn(f"Object '{obj.name}': could not update normal metadata: {exc}")

    with open(filepath, "wb") as f:
        f.write(data)
    if arm_obj is not None:
        _update_bone_metadata(arm_obj, model, bone_index, mesh_objs)
    bc.remember_export(mesh_objs[0] if mesh_objs else arm_obj,
                       "bmd", filepath)
    if report:
        report({"INFO"}, f"Exported {len(model.meshes)} meshes, {len(model.joints)} joints, "
                         f"{len(model.materials)} materials, {len(model.textures)} textures "
                         f"({len(data) // 1024} KiB)")
    return model


# --------------------------------------------------------------------------
# skeleton
# --------------------------------------------------------------------------

def is_duplicate_bone_name(name):
    """True for Blender's automatic ``.001`` duplicate suffix."""
    _base, sep, suffix = str(name).rpartition(".")
    return bool(sep) and len(suffix) == 3 and suffix.isdigit()


def strip_blender_suffix(name):
    """Remove Blender's automatic ``.001`` duplicate suffix from an exported name."""
    text = str(name)
    base, sep, suffix = text.rpartition(".")
    if sep and len(suffix) == 3 and suffix.isdigit():
        return base
    return text


def exported_joint_name(bone):
    """Joint name to write: the original name unless the user renamed the bone.

    Blender appends unique suffixes (``.001`` or ``.<joint index>``) when a
    file contains duplicate joint names.  Those are ignored, but a manual
    rename must be exported.
    """
    imported_name = bone.get("bmd_bone_name")
    if imported_name is not None and bone.name == str(imported_name):
        return str(bone.get("bmd_joint_name") or bone.name)
    return strip_blender_suffix(bone.name)


def joint_index_owners(arm, imported_names=None):
    """Return ``({bmd_index: bone}, [duplicate bones])`` for the armature.

    Duplicated bones copy ``bmd_index`` from the original.  The bone named in
    ``imported_names`` (or, failing that, the bone without a ``.001`` suffix)
    keeps the index; the other bones are returned as duplicates.
    """
    names = [str(n) for n in (imported_names or [])]
    groups = {}
    for bone in arm.bones:
        raw = bone.get("bmd_index")
        if raw is None:
            continue
        try:
            idx = int(raw)
        except (TypeError, ValueError):
            continue
        if idx >= 0:
            groups.setdefault(idx, []).append(bone)

    owners = {}
    duplicates = []
    for idx, group in groups.items():
        owner = None
        if idx < len(names):
            owner = next((b for b in group if b.name == names[idx]), None)
        if owner is None:
            owner = next((b for b in group if not is_duplicate_bone_name(b.name)),
                         group[0])
        owners[idx] = owner
        duplicates.extend(b for b in group if b is not owner)
    return owners, duplicates


def ordered_bones(arm, imported_names=None, use_indices=True):
    """Bones in J3D joint order.

    Parents always precede their children, so the result is safe for both BMD
    joint tables and BCK track order.  Imported bones keep their relative
    ``bmd_index`` order whenever the hierarchy still allows it; new bones
    (added or duplicated by the user) follow.  When several bones share one
    ``bmd_index``, the bone named in ``imported_names`` keeps it, and the
    others are treated as new bones.

    ``use_indices=False`` ignores stale ``bmd_index`` values and orders the
    current hierarchy, which is what a custom armature needs.
    """
    bones = list(arm.bones)
    if not bones:
        return []

    if use_indices:
        owners, _duplicates = joint_index_owners(arm, imported_names)
        preferred = {b.name: idx for idx, b in owners.items()}
    else:
        preferred = {}

    # Imported indices first, then new bones in armature order.  The keys only
    # decide between bones that are ready at the same time; the topological
    # sort below still guarantees that parents come first.
    next_key = max(preferred.values(), default=-1) + 1
    keys = {}
    for b in bones:
        if b.name in preferred:
            keys[b.name] = preferred[b.name]
        else:
            keys[b.name] = next_key
            next_key += 1

    children = {b.name: [] for b in bones}
    indegree = {b.name: 0 for b in bones}
    for b in bones:
        if b.parent is not None and b.parent.name in indegree:
            children[b.parent.name].append(b.name)
            indegree[b.name] += 1

    ready = [(keys[name], name) for name, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        _key, name = heapq.heappop(ready)
        ordered.append(name)
        for child in children[name]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, (keys[child], child))
    if len(ordered) != len(bones):
        raise ExportError("Bone hierarchy contains a cycle")
    return [arm.bones[name] for name in ordered]


def _add_empty_shapes(model, mesh_objs, mats, joint_map=None):
    """Append placeholder shapes for original shapes that have no faces.

    Shape indices are part of the game-facing interface (BSK animations and
    game code address shapes by index).  Dropping an empty shape would shift
    every later shape index.
    """
    table = None
    for obj in mesh_objs:
        raw = obj.get("bmd_shape_nodes")
        if not raw:
            continue
        try:
            table = json.loads(raw)
        except (TypeError, ValueError):
            table = None
        if table:
            break
    if not table:
        return

    present = {int(mesh.sort_key) for mesh in model.meshes}
    added = set()
    by_bmd_index = {}
    for material in bpy.data.materials:
        index = material.get("bmd_index")
        if index is not None:
            by_bmd_index.setdefault(int(index), material)

    for info in table:
        if len(info) < 4:
            continue
        shape_index = int(info[2])
        if shape_index in present or shape_index in added:
            continue
        joint = int(info[0])
        if joint_map is not None:
            joint = joint_map.get(joint, joint)
        material_index = int(info[1])
        order = int(info[3])
        matrix_type = int(info[4]) if len(info) > 4 else None
        material = by_bmd_index.get(material_index)
        material_slot = mats.register_material(material) if material is not None else 0

        em = writer.ExportMesh()
        em.name = f"empty_shape_{shape_index}"
        em.verts = np.zeros((0, 3), np.float32)
        em.faces = np.zeros((0, 3), np.int32)
        em.vertex_ids = np.zeros(0, np.int64)
        em.vert_influence = np.zeros(0, np.int32)
        em.influence_sets = []
        em.material = material_slot
        em.sort_key = shape_index
        em.order = order
        em.joint = joint
        em.matrix_type = matrix_type
        model.meshes.append(em)
        added.add(shape_index)


def remap_joint_references(old_to_new, mesh_objs=None):
    """Update stored joint indices after a skeleton renumber.

    ``bmd_shape_nodes`` tables and ``bmd_joint`` on mesh objects address joints
    by index.  A BMD or BCK export can change the bone order, so those values
    must follow the bones.
    """
    objects = bpy.data.objects if mesh_objs is None else mesh_objs
    for obj in objects:
        if obj.type != "MESH":
            continue
        changed = False
        raw = obj.get("bmd_shape_nodes")
        if raw:
            try:
                table = json.loads(raw)
            except (TypeError, ValueError):
                table = None
            if table:
                for info in table:
                    if len(info) < 4:
                        continue
                    old_joint = int(info[0])
                    new_joint = old_to_new.get(old_joint)
                    if new_joint is not None and new_joint != old_joint:
                        info[0] = int(new_joint)
                        changed = True
                if changed:
                    obj["bmd_shape_nodes"] = json.dumps(table)
        if "bmd_joint" in obj:
            try:
                old_joint = int(obj["bmd_joint"])
            except (TypeError, ValueError):
                old_joint = None
            if old_joint is not None:
                new_joint = old_to_new.get(old_joint)
                if new_joint is not None and new_joint != old_joint:
                    obj["bmd_joint"] = int(new_joint)
                    changed = True
        if changed:
            obj.update_tag()


def _update_bone_metadata(arm_obj, model, bone_index, mesh_objs):
    """Store the exported joint layout on the armature and shape-node tables.

    A later BCK import/export maps tracks by ``bmd_index`` and reuses
    ``bmd_local``/``bmd_rest`` for unchanged bones.  After a BMD export those
    values must describe the file that was just written, not the previous one.
    """
    owners, _duplicates = joint_index_owners(
        arm_obj.data, arm_obj.get("bmd_bone_names", []))
    new_by_old = {}
    for old, bone in owners.items():
        new = bone_index.get(bone.name)
        if new is not None:
            new_by_old[old] = new

    for name, joint_index in bone_index.items():
        bone = arm_obj.data.bones.get(name)
        if bone is None or not 0 <= joint_index < len(model.joints):
            continue
        joint = model.joints[joint_index]
        bone["bmd_index"] = int(joint_index)
        parent = bone.parent
        bone["bmd_parent"] = (
            int(bone_index[parent.name])
            if parent is not None and parent.name in bone_index else -1)
        bone["bmd_kind"] = int(joint.kind)
        bone["bmd_scale_comp"] = int(joint.scale_compensate)
        bone["bmd_local"] = [
            float(x) for x in (*joint.trans, *joint.rot, *joint.scale)]
        bone["bmd_rest"] = [
            float(x) for x in bc.to_np(bone.matrix_local).reshape(-1)]

    arm_obj["bmd_bone_names"] = sorted(bone_index, key=bone_index.get)
    remap_joint_references(new_by_old, mesh_objs)


def _build_joints(model, arm_obj, scale):
    """Fill model.joints; returns {bone name: joint index}."""
    if arm_obj is None:
        j = j3d.Joint("root")
        model.joints.append(j)
        return {}
    arm = arm_obj.data
    bones = list(arm.bones)
    if not bones:
        model.joints.append(j3d.Joint("root"))
        return {}

    ordered = ordered_bones(arm, arm_obj.get("bmd_bone_names", []))
    index = {b.name: i for i, b in enumerate(ordered)}

    axis_fix = bc.R_FIX if arm_obj.get("bmd_bone_axis", "X") == "X" else np.eye(4)
    fix_inv = axis_fix.T
    world = {}
    for b in ordered:
        Mb = bc.to_np(b.matrix_local)
        Wb = Mb @ fix_inv
        W = bc.C_INV @ Wb @ bc.C
        W[:3, 3] *= scale
        world[b.name] = bc.orthonormalize(W)

    own_same = {}
    for b in ordered:
        stored, rest = b.get("bmd_local"), b.get("bmd_rest")
        cur = bc.to_np(b.matrix_local).reshape(-1)
        own_same[b.name] = bool(stored is not None and rest is not None and len(rest) == 16 and
                                np.allclose(cur, np.array(rest), atol=1e-5))
    reuse_memo = {}

    def can_reuse(b):
        if b.name not in reuse_memo:
            reuse_memo[b.name] = own_same[b.name] and (b.parent is None or can_reuse(b.parent))
        return reuse_memo[b.name]

    for b in ordered:
        j = j3d.Joint(exported_joint_name(b))
        j.parent = index[b.parent.name] if b.parent else -1
        j.kind = int(b.get("bmd_kind", 1))
        j.scale_compensate = int(b.get("bmd_scale_comp", 0))
        if can_reuse(b):
            s = [float(x) for x in b["bmd_local"]]
            j.trans, j.rot, j.scale = s[0:3], s[3:6], s[6:9]
        else:
            P = world[b.parent.name] if b.parent else np.eye(4)
            L = np.linalg.inv(P) @ world[b.name]
            j.trans = [float(x) for x in L[:3, 3]]
            j.rot = list(j3d.matrix_to_euler(L[:3, :3]))
            j.scale = [1.0, 1.0, 1.0]
        model.joints.append(j)

    roots = [i for i, j in enumerate(model.joints) if j.parent < 0]
    if len(roots) > 1:
        root = j3d.Joint("root")
        model.joints.append(root)
        for r in roots:
            model.joints[r].parent = len(model.joints) - 1
    return index


# --------------------------------------------------------------------------
# meshes
# --------------------------------------------------------------------------

def _srgb_from_linear(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def _read_color(me, name_hint):
    attrs = me.color_attributes
    if len(attrs) == 0:
        return None
    attr = attrs.get(name_hint)
    if attr is None and name_hint == "Col0":
        attr = attrs.active_color or attrs[0]
    if attr is None:
        return None
    n = len(attr.data)
    arr = np.empty(n * 4, np.float32)
    attr.data.foreach_get("color", arr)
    arr = arr.reshape(n, 4)
    if attr.data_type in ("BYTE_COLOR",):
        arr = arr.copy()
        arr[:, :3] = _srgb_from_linear(arr[:, :3])
    return arr, attr.domain


def _eval_mesh(obj, apply_modifiers, context):
    if not apply_modifiers:
        return obj.data, None
    disabled = []
    for m in obj.modifiers:
        if m.type == "ARMATURE" and m.show_viewport:
            m.show_viewport = False
            disabled.append(m)
    dg = context.evaluated_depsgraph_get()
    dg.update()
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    return me, (ev, disabled)


def _export_mesh_object(model, obj, arm_obj, bone_index, mats, scale, apply_modifiers, context,
                        normal_updates=None, joint_map=None, warn=None):
    me, cleanup = _eval_mesh(obj, apply_modifiers, context)
    try:
        _export_mesh_data(model, obj, me, arm_obj, bone_index, mats, scale,
                          normal_updates, joint_map, warn)
    finally:
        if cleanup:
            ev, disabled = cleanup
            ev.to_mesh_clear()
            for m in disabled:
                m.show_viewport = True


def _export_mesh_data(model, obj, me, arm_obj, bone_index, mats, scale,
                      normal_updates=None, joint_map=None, warn=None):
    me.calc_loop_triangles()
    nt = len(me.loop_triangles)
    if nt == 0:
        return
    nv = len(me.vertices)
    co = np.empty(nv * 3, np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(nv, 3)
    tri_verts = np.empty(nt * 3, np.int32)
    tri_loops = np.empty(nt * 3, np.int32)
    tri_mat = np.empty(nt, np.int32)
    me.loop_triangles.foreach_get("vertices", tri_verts)
    me.loop_triangles.foreach_get("loops", tri_loops)
    me.loop_triangles.foreach_get("material_index", tri_mat)
    tri_verts = tri_verts.reshape(nt, 3)
    tri_loops = tri_loops.reshape(nt, 3)

    # object -> armature space -> J3D space
    M = bc.to_np(obj.matrix_world)
    if arm_obj is not None:
        M = np.linalg.inv(bc.to_np(arm_obj.matrix_world)) @ M
    # Blender and GX use opposite front-face winding; a mirrored object flips it once more
    if np.linalg.det(M[:3, :3]) >= 0:
        tri_verts = tri_verts[:, [0, 2, 1]]
        tri_loops = tri_loops[:, [0, 2, 1]]
    pts = co @ M[:3, :3].T + M[:3, 3]
    verts = bc.point_from_blender(pts, scale)
    base_vertex = len(model.positions) if model.preserve_vertex_order else 0
    if model.preserve_vertex_order:
        # All vertices of this object, in their current order.  Groups below reference
        # them by their global id; the writer must not sort this table.
        model.positions.extend(verts.tolist())

    normals = None
    try:
        n = np.empty(len(me.loops) * 3, np.float32)
        me.corner_normals.foreach_get("vector", n)
        n = n.reshape(-1, 3).astype(np.float64)
        N = np.linalg.inv(M[:3, :3]).T
        n = bc.vec_from_blender(n @ N.T)
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        ln[ln == 0] = 1
        normals = (n / ln).astype(np.float32)
    except Exception as exc:
        _warn(warn, f"Mesh '{obj.name}': could not read normals; "
                    f"exporting without custom normals: {exc}")

    normal_indices = None
    if normals is not None:
        normal_indices, natural_count = bmd_metadata.natural_normal_layout(me)
        # Each object owns a separate slice of the global normal array; objects
        # are exported one after another, so their local indices must not collide.
        normal_indices = normal_indices + model.normal_count
        model.normal_count += natural_count

    # UV layers -> GX texcoord sets
    uv_layers = {}
    for i, layer in enumerate(me.uv_layers):
        t = None
        if layer.name.startswith("UV") and layer.name[2:].isdigit():
            t = int(layer.name[2:])
        if t is None or t > 7 or t in uv_layers:
            t = next((k for k in range(8) if k not in uv_layers), None)
        if t is None:
            break
        a = np.empty(len(layer.data) * 2, np.float32)
        layer.data.foreach_get("uv", a)
        a = a.reshape(-1, 2).copy()
        a[:, 1] = 1.0 - a[:, 1]
        uv_layers[t] = a

    colors = [None, None]
    loop_vert = None
    for c in range(2):
        res = _read_color(me, f"Col{c}")
        if res is None:
            continue
        arr, domain = res
        if domain == "POINT":
            if loop_vert is None:
                loop_vert = np.empty(len(me.loops), np.int32)
                me.loops.foreach_get("vertex_index", loop_vert)
            arr = arr[loop_vert]
        colors[c] = arr

    # skin weights
    group_joint = {}
    for vg in obj.vertex_groups:
        if vg.name in bone_index:
            group_joint[vg.index] = bone_index[vg.name]
    root = 0
    parent_joint = None
    if obj.parent_type == "BONE" and obj.parent_bone in bone_index:
        parent_joint = bone_index[obj.parent_bone]
    infl_sets, infl_lookup = [], {}
    vert_inf = np.zeros(nv, np.int32)
    for i, v in enumerate(me.vertices):
        ws = {}
        for g in v.groups:
            j = group_joint.get(g.group)
            if j is not None and g.weight > 1e-4:
                ws[j] = ws.get(j, 0.0) + g.weight
        if not ws:
            ws = {parent_joint if parent_joint is not None else root: 1.0}
        max_weights = writer.MAX_ENVELOPE_WEIGHTS
        if len(ws) > max_weights:
            _warn(warn, f"Object '{obj.name}': vertex {i} has more than "
                        f"{writer.MAX_ENVELOPE_WEIGHTS} influences; clamping")
            ws = dict(sorted(ws.items(), key=lambda kv: -kv[1])[:max_weights])
        tot = sum(ws.values())
        key = tuple(sorted((j, round(w / tot, 5)) for j, w in ws.items()))
        k = infl_lookup.get(key)
        if k is None:
            k = len(infl_sets)
            infl_lookup[key] = k
            infl_sets.append(key)
        vert_inf[i] = k

    slots = obj.material_slots
    merged = me.attributes.get("bmd_node") is not None
    node_table = []
    tri_node = None
    if merged:
        node_attr = me.attributes.get("bmd_node")
        try:
            node_table = json.loads(obj.get("bmd_shape_nodes", "[]"))
        except Exception as exc:
            _warn(warn, f"Object '{obj.name}': invalid bmd_shape_nodes; "
                        f"falling back to material groups: {exc}")
            node_table = []
        if node_attr is not None and node_table:
            node_values = np.empty(len(me.polygons), np.int32)
            node_attr.data.foreach_get("value", node_values)
            poly_index = np.empty(nt, np.int32)
            me.loop_triangles.foreach_get("polygon_index", poly_index)
            tri_node = node_values[poly_index]
        else:
            merged = False

    if merged:
        pairs = np.stack([tri_node, tri_mat], 1)
        groups, group_inv = np.unique(pairs, axis=0, return_inverse=True)
        work = [(int(n), int(m), np.nonzero(group_inv == gi)[0])
                for gi, (n, m) in enumerate(groups)]
    else:
        work = [(None, int(mi), np.nonzero(tri_mat == mi)[0])
                for mi in np.unique(tri_mat)]

    for node_id, mi, sel in work:
        mat = slots[mi].material if mi < len(slots) else None
        faces_old = tri_verts[sel]
        used, inv = np.unique(faces_old, return_inverse=True)
        em = writer.ExportMesh()
        em.name = obj.name
        em.verts = verts[used].astype(np.float32)
        em.vert_influence = vert_inf[used]
        em.influence_sets = infl_sets
        em.faces = inv.reshape(-1, 3).astype(np.int32)
        if model.preserve_vertex_order:
            em.vertex_ids = (used + base_vertex).astype(np.int64)
        lp = tri_loops[sel].reshape(-1)
        if normals is not None:
            em.loop_normals = normals[lp]
            if normal_indices is not None:
                em.loop_normal_indices = normal_indices[lp]
        for c in range(2):
            if colors[c] is not None:
                em.loop_colors[c] = colors[c][lp]
        for t, a in uv_layers.items():
            em.loop_uvs[t] = a[lp]
        if merged and 0 <= node_id < len(node_table):
            info = node_table[node_id]
            old_joint = int(info[0])
            em.joint = (joint_map.get(old_joint, old_joint)
                        if joint_map is not None else old_joint)
            em.sort_key = int(info[2])
            em.order = int(info[3])
            if len(info) > 4:
                em.matrix_type = int(info[4])
        else:
            em.order = int(obj.get("bmd_order", 1e6 + len(model.meshes)))
            em.sort_key = int(obj.get("bmd_shape_index", 1e6 + len(model.meshes)))
            if "bmd_joint" in obj:
                em.joint = int(obj["bmd_joint"])
        em.material = mats.index_for(mat, em)
        model.meshes.append(em)

    if normal_updates is not None:
        # Modifiers that leave topology and positions unchanged still describe
        # the same BLS target.  If they change either, the metadata computed
        # from the original mesh can no longer match the exported BMD.
        same_geometry = _same_geometry(obj.data, me)
        vertex_order = bool(model.preserve_vertex_order and same_geometry)
        normal_updates.append((
            obj,
            normal_indices.copy() if vertex_order and normal_indices is not None else None,
            base_vertex if vertex_order else 0,
            vertex_order,
        ))


# --------------------------------------------------------------------------
# materials & textures
# --------------------------------------------------------------------------

def _reuse_raw(image, t, w, h, warn=None):
    """Use the original encoded bytes when the image still looks like the imported one."""
    raw = image.get("bmd_raw")
    if not raw:
        return False
    props = image.bmd
    orig = image.get("bmd_orig_format")
    mips0 = int(image.get("bmd_raw_mips", 0))
    auto_format = bool(image.get("bmd_format_auto"))
    if ((not auto_format and props.format not in ("AUTO", orig))
            or props.mip_count not in (0, mips0)):
        return False
    try:
        data = zlib.decompress(base64.b64decode(raw))
        rfmt = int(image["bmd_raw_fmt"])
        rw, rh = (int(v) for v in image["bmd_raw_size"])
        if (rw, rh) != (w, h):
            return False
        pal_raw, pal = None, None
        if image.get("bmd_raw_pal"):
            pal_raw = zlib.decompress(base64.b64decode(image["bmd_raw_pal"]))
            pal = gx.decode_palette(int(image["bmd_pal_fmt"]), pal_raw,
                                    int(image["bmd_pal_count"]))
        stored_hash = image.get("bmd_pixel_hash")
        if stored_hash:
            # The hash is of the pixels at import time.  Comparing it to the
            # current pixels avoids decoding a 4K texture and comparing two
            # full RGBA arrays.
            if hashlib.sha1(t.rgba).hexdigest() != str(stored_hash):
                return False
        else:
            ref = gx.decode(rfmt, data, rw, rh, pal).astype(np.int16)
            cur = t.rgba.astype(np.int16)
            diff = np.abs(ref - cur)
            visible = ((ref[..., 3] > 0) | (cur[..., 3] > 0))[..., None]
            worst = max(int(diff[..., 3].max()),
                        int(np.where(visible, diff[..., :3], 0).max()))
            if worst > 1:
                return False
    except Exception as exc:
        _warn(warn, f"Image '{image.name}': could not reuse the original texture data; "
                    f"re-encoding from pixels: {exc}")
        return False
    t.raw_data, t.raw_palette = data, pal_raw
    t.format, t.mip_count = rfmt, mips0
    t.max_lod = int(image.get("bmd_max_lod", (mips0 - 1) * 8))
    t.pal_enabled = int(image.get("bmd_pal_en", 0))
    t.pal_format = int(image.get("bmd_pal_fmt", 0))
    t.pal_count = int(image.get("bmd_pal_count", 0))
    raw_bti = image.get("bmd_raw_bti")
    if raw_bti:
        try:
            t.raw_bti = zlib.decompress(base64.b64decode(raw_bti))
        except Exception as exc:
            _warn(warn, f"Image '{image.name}': could not read the original .bti data; "
                        f"it will be re-encoded: {exc}")
            t.raw_bti = None
    return True


class _DefaultImageSettings:
    """Fallback for images that have no BMD settings yet."""

    format = "AUTO"
    external = False
    mip_count = 1
    wrap_s = "1"
    wrap_t = "1"
    min_filter = "1"
    mag_filter = "1"
    min_lod = 0
    max_lod = 0
    max_lod_custom = False
    lod_bias = 0


def _image_settings(image):
    """Return BMD settings without adding a ``bmd`` group to a plain image."""
    if "bmd" in image or any(str(key).startswith("bmd_")
                             for key in image.keys()):
        return image.bmd

    return _DefaultImageSettings()


def texture_from_image(image, warn=None):
    """Build an export :class:`j3d.Texture` from a Blender image."""
    name = bc.clean_texture_name(image)
    t = j3d.Texture(name)
    t.key = bc.texture_key(image)
    w, h = image.size
    if w == 0 or h == 0:
        raise ExportError(f"Image '{image.name}' has no pixel data")
    buf = np.empty(w * h * 4, np.float32)
    image.pixels.foreach_get(buf)
    pixels = buf.reshape(h, w, 4)[::-1]
    pixels *= 255.0
    np.rint(pixels, out=pixels)
    np.clip(pixels, 0, 255, out=pixels)
    t.rgba = pixels.astype(np.uint8)
    props = _image_settings(image)
    orig = image.get("bmd_orig_format")
    external = bool(props.external)
    reused = _reuse_raw(image, t, w, h, warn)
    has_a = gx.has_alpha(t.rgba)
    partial = gx.has_partial_alpha(t.rgba)

    def choose_format(want, auto):
        fmt = gx.NAME_TO_FORMAT.get(want) if want else None
        if fmt is None:
            return gx.RGB5A3 if partial else gx.CMPR
        if fmt in gx.PALETTE_FORMATS:
            fallback = gx.RGB5A3 if partial else gx.CMPR
            _warn(warn, f"Image '{name}': {gx.FORMAT_NAMES.get(fmt, fmt)} export "
                        "needs a palette quantizer; using "
                        f"{gx.FORMAT_NAMES[fallback]}")
            return fallback
        # An automatically kept format must not silently discard alpha that
        # the edited image now has.  An explicit user choice is respected.
        if auto and has_a and fmt in (gx.I4, gx.I8, gx.RGB565):
            return gx.RGB5A3 if partial else gx.CMPR
        return fmt

    # An imported image keeps its original format in the UI.  Treat that value
    # (and "AUTO") as automatic so an edited image can switch to an
    # alpha-capable format; a different explicit choice is respected.
    auto_format = (props.format == "AUTO"
                   or bool(image.get("bmd_format_auto"))
                   or (bool(orig) and props.format == orig))
    if external:
        # Header-only TEX1 entry; the pixels live in a .bti file.
        t.external = True
        if not reused:
            want = orig if auto_format else props.format
            t.format = choose_format(want, auto_format)
        external_size = image.get("bmd_external_size")
        if external_size and len(external_size) == 2:
            ext_w, ext_h = int(external_size[0]), int(external_size[1])
            if (ext_w, ext_h) != (w, h):
                # The image itself can be a 1x1 placeholder; the header keeps
                # the original texture size because no pixels are written.
                t.rgba = np.full((ext_h, ext_w, 4), 255, np.uint8)
    elif not reused:
        if auto_format:
            want = orig
        else:
            want = props.format
        t.format = choose_format(want, auto_format)

    if reused:
        # Unchanged pixels: keep the original header values verbatim.
        t.transparency = int(image.get("bmd_transparency", 2 if has_a else 0))
        mips = t.mip_count
    else:
        # Edited pixels: derive the alpha flag from the actual image.
        t.transparency = 2 if has_a else 0
        mips = max(1, int(props.mip_count))
        t.mip_count = mips

    t.wrap_s = int(props.wrap_s)
    t.wrap_t = int(props.wrap_t)
    t.min_filter = int(props.min_filter)
    t.mag_filter = int(props.mag_filter)
    t.min_lod = int(props.min_lod)
    t.max_lod = int(props.max_lod)
    t.max_lod_custom = bool(props.max_lod_custom)
    t.lod_bias = int(props.lod_bias)
    fl = image.get("bmd_flags10")
    flags = bytes.fromhex(fl) if fl else b"\0\0\0\0"
    if len(flags) < 4:
        flags = flags + b"\0" * (4 - len(flags))
    # flags10 bit 0 is the mipmap flag; keep it in sync with mip_count.
    t.flags10 = bytes([(flags[0] & 0xFE) | (1 if mips > 1 else 0)]) + flags[1:4]
    return t


class _MaterialCollector:
    def __init__(self, model, warn=None):
        self.model = model
        self.warn = warn
        self.mat_index = {}
        self.images = {}   # tex name -> (Texture)
        self.pending = []  # (ExportMesh)
        self.sort_keys = {}  # material index -> original index
        self._source = None

    def register_material(self, bl_mat):
        """Add a material to the export without attaching it to a mesh."""
        key = bl_mat.name if bl_mat else None
        if key not in self.mat_index:
            self.mat_index[key] = len(self.model.materials)
            d, name = self._build(bl_mat, None)
            self.model.materials.append(d)
            self.model.material_names.append(name)
            n = self.mat_index[key]
            self.sort_keys[n] = int(bl_mat.get("bmd_index", 1e6 + n)) if bl_mat else 1e6 + n
        return self.mat_index[key]

    def index_for(self, bl_mat, mesh):
        key = bl_mat.name if bl_mat else None
        if key not in self.mat_index:
            self.mat_index[key] = len(self.model.materials)
            d, name = self._build(bl_mat, mesh)
            self.model.materials.append(d)
            self.model.material_names.append(name)
            n = self.mat_index[key]
            self.sort_keys[n] = int(bl_mat.get("bmd_index", 1e6 + n)) if bl_mat else 1e6 + n
        idx = self.mat_index[key]
        self.pending.append((mesh, idx))
        return idx

    def _source_textures(self, path):
        if self._source is None:
            self._source = {}
            if path and os.path.isfile(path):
                try:
                    with open(path, "rb") as f:
                        self._source = {t.key: t for t in j3d.parse_textures(f.read())}
                except Exception as exc:
                    _warn(self.warn, f"Could not read the original model '{path}' "
                                     f"to restore textures: {exc}")
        return self._source

    def preload_original_textures(self, arm_obj, source_file, allow_missing, report):
        """Add every texture of the BMD texture list in its current order.

        The list order is the exported TEX1 order.  Materials reference textures
        by key, so re-ordering the list only changes their indices.  Textures
        whose image was lost (Blender purges unused images) are restored from
        the original BMD if available.
        """
        slots = bc.ensure_texture_list(arm_obj)
        source = source_file or arm_obj.get("bmd_source", "")
        missing, restored, empty = [], [], 0
        dup_index = 0
        seen_keys = {}

        def duplicate_key(key):
            nonlocal dup_index
            dup_index += 1
            out = f"__dup{dup_index}__{key}"
            while out in self.images:
                dup_index += 1
                out = f"__dup{dup_index}__{key}"
            return out

        for slot in slots:
            image = slot.image

            if image is not None:
                image_key = bc.texture_key(image)
                slot_key = str(slot.key or "") or image_key
                count = seen_keys.get(slot_key, 0)

                tex = texture_from_image(image, self.warn)
                tex.data_group = str(image.as_pointer())

                if count == 0 and slot_key not in self.images:
                    tex.key = slot_key
                else:
                    tex.key = duplicate_key(slot_key)

                if slot.name:
                    tex.name = slot.name

                self.images[tex.key] = tex
                self.images.setdefault(image_key, tex)
                self.model.textures.append(tex)
                seen_keys[slot_key] = count + 1
                slot.key = tex.key
                continue
            key = str(slot.key or "")
            if not key:
                empty += 1
                continue

            source_tex = self._source_textures(source).get(key)

            if key in self.images:
                tex = copy.deepcopy(source_tex if source_tex is not None
                                     else self.images[key])
                tex.key = duplicate_key(key)

                if slot.name:
                    tex.name = slot.name

                self.images[tex.key] = tex
                self.model.textures.append(tex)
                restored.append(key)
                continue

            if source_tex is not None:
                tex = copy.deepcopy(source_tex)
                tex.key = key

                if slot.name:
                    tex.name = slot.name

                self.images[key] = tex
                self.model.textures.append(tex)
                restored.append(key)
            else:
                missing.append(key)
        if restored:
            _warn(self.warn, f"{len(restored)} textures were missing in Blender and were restored "
                             f"from the original model: {', '.join(restored)}")
        if empty:
            _warn(self.warn, f"Skipped {empty} BMD texture slot(s) without an image")
        if missing and not allow_missing:
            raise ExportError(
                f"{len(missing)} textures of the imported model no longer exist in Blender "
                f"({', '.join(missing[:6])}{' ...' if len(missing) > 6 else ''}). Exporting without them "
                "would shift the texture indices and crash the game. Type the path of the file you "
                "imported into the 'Original BMD' export option so they can be restored (or enable "
                "'Allow Missing Textures' if you removed them on purpose).")

    def _image_for_key(self, key):
        """Find the image that currently owns *key* (the BMD Key is editable)."""
        return bc.image_by_key(key)

    def _texture_for(self, image):
        key = bc.texture_key(image)
        if key in self.images:
            return key
        t = texture_from_image(image, self.warn)
        self.images[key] = t
        self.model.textures.append(t)
        return key

    def _image_nodes(self, bl_mat):
        """{slot: (image, extension)} from nodes named bmd_tex<N>, else first image node."""
        out = {}
        if bl_mat is None or bl_mat.node_tree is None:
            return out
        for node in bl_mat.node_tree.nodes:
            if node.type != "TEX_IMAGE":
                continue
            if node.name.startswith("bmd_tex") and node.name[7:].isdigit():
                out[int(node.name[7:])] = (node.image, getattr(node, "extension", "REPEAT"))
        if not out:
            for node in bl_mat.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    out[0] = (node.image, getattr(node, "extension", "REPEAT"))
                    break
        return out

    def _build(self, bl_mat, mesh):
        nodes = self._image_nodes(bl_mat)
        stored = bl_mat.get("bmd_material") if bl_mat else None
        is_bmd = bool(bl_mat is not None and bc.is_bmd_material(bl_mat))
        mode = bl_mat.bmd.render_mode if is_bmd else "AUTO"
        alpha_ref = bl_mat.bmd.alpha_ref if is_bmd else 128
        cull_back = bool(bl_mat.use_backface_culling) if bl_mat else True
        name = ((bl_mat.get("bmd_name")
                 or strip_blender_suffix(bl_mat.name)) if bl_mat else "default")

        d = None
        if stored:
            try:
                d = json.loads(stored)
            except Exception as exc:
                _warn(self.warn, f"Material '{name}': stored material data is invalid; "
                                 f"rebuilding it from its nodes: {exc}")
        if d is not None:
            tex_names = list(d.get("texNames", [None] * 8))
            for slot, (img, _ext) in nodes.items():
                if img is None:
                    tex_names[slot] = None
                    continue

                base = (tex_names[slot]
                        if 0 <= slot < len(tex_names) else None)

                if (base and base in self.images
                        and bc.image_by_key(base) is img):
                    # Keep the slot-specific key.  Several TEX1 indices may
                    # share one image; collapsing them would change material
                    # texture indices (and texture animations).
                    continue

                tex_names[slot] = self._texture_for(img)
            # slots that previously had a texture but now have no node keep their name only
            # if the texture still exists in the scene
            for slot in range(8):
                if tex_names[slot] and tex_names[slot] not in self.images:
                    base = tex_names[slot]
                    img = self._image_for_key(base)
                    if img is not None:
                        tex_names[slot] = self._texture_for(img)
                    else:
                        tex_names[slot] = None
            d["texNames"] = tex_names
            if (d.get("cull") == 2) != cull_back:
                d["cull"] = 2 if cull_back else 0
            if mode != "AUTO":
                mat3.apply_render_mode(d, mode, alpha_ref)
            else:
                kind, ref = mat3.classify_alpha(d)
                if kind == mat3.RENDER_CUTOUT and ref is not None and int(ref) != int(alpha_ref):
                    ac = d["alphaComp"]
                    if ac[0] in (4, 6):
                        ac[1] = int(alpha_ref)
                    else:
                        ac[4] = int(alpha_ref)
            return d, name

        # brand-new material: build a simple unlit texture * vertex colour material
        tex_name = None
        has_alpha = False
        if 0 in nodes and nodes[0][0] is not None:
            tex_name = self._texture_for(nodes[0][0])
            has_alpha = gx.has_alpha(self.images[tex_name].rgba)
        elif nodes:
            slot = min(nodes)
            if nodes[slot][0] is not None:
                tex_name = self._texture_for(nodes[slot][0])
                has_alpha = gx.has_alpha(self.images[tex_name].rgba)
        if mode == "AUTO":
            blended = False
            try:
                blended = bl_mat is not None and bl_mat.surface_render_method == "BLENDED"
            except Exception:
                pass
            if blended:
                mode = mat3.RENDER_TRANSLUCENT
            elif has_alpha:
                mode = mat3.RENDER_CUTOUT
            else:
                mode = mat3.RENDER_OPAQUE
        d = mat3.default_material(
            tex_name,
            use_vertex_colors=bool(mesh is not None and mesh.loop_colors[0] is not None),
            mode=mode, cull_back=cull_back, alpha_ref=alpha_ref)
        return d, name

    def finish(self):
        """Make meshes consistent with what their materials sample."""
        for mesh, idx in self.pending:
            d = self.model.materials[idx]
            if mat3.uses_vertex_colors(d) and mesh.loop_colors[0] is None:
                mesh.loop_colors[0] = np.ones((len(mesh.faces) * 3, 4), np.float32)
            for t in mat3.required_uv_sets(d):
                if mesh.loop_uvs[t] is None:
                    mesh.loop_uvs[t] = np.zeros((len(mesh.faces) * 3, 2), np.float32)
        # keep the original material order (animations and game code address materials by index)
        order = sorted(range(len(self.model.materials)), key=lambda i: (self.sort_keys.get(i, 1e9), i))
        remap = {old: new for new, old in enumerate(order)}
        self.model.materials[:] = [self.model.materials[i] for i in order]
        self.model.material_names[:] = [self.model.material_names[i] for i in order]
        # Placeholder/empty shapes are not in ``pending`` but their material
        # indices also have to follow the reordered material table.
        for mesh in self.model.meshes:
            if 0 <= mesh.material < len(remap):
                mesh.material = remap[mesh.material]
        self.pending = [(m, remap[i]) for m, i in self.pending]
