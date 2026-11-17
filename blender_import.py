"""Create Blender data from a parsed J3D model."""

import base64
import hashlib
import json
import os
import uuid
import zlib

import bpy
import numpy as np
from mathutils import Matrix

from . import blender_common as bc
from . import blender_bti
from . import bmd_metadata
from .j3d import gx_texture as gx
from .j3d import material as mat3
from .j3d import model as j3d


def _warn(warn, message):
    if warn is not None:
        warn(message)
    else:
        print(f"[BMD] {message}")


def import_bmd(context, filepath, scale=0.01, bone_axis="X", import_textures=True,
               report=None, texc_dir=None):
    with open(filepath, "rb") as f:
        raw = f.read()
    bmd = j3d.parse(raw)
    base = os.path.splitext(os.path.basename(filepath))[0]

    def warn(message):
        if report:
            report({"WARNING"}, message)
        else:
            print(f"[BMD] {message}")

    if import_textures and any(t.external for t in bmd.textures):
        missing_external = blender_bti.load_for_bmd(bmd, filepath, texc_dir)
    else:
        missing_external = []

    coll = bpy.data.collections.new(base)
    context.scene.collection.children.link(coll)

    if import_textures:
        images = _create_images(bmd, base, uuid.uuid4().hex)
    else:
        images = {}
    materials = [_create_material(bmd, i, m, images, warn)
                 for i, m in enumerate(bmd.materials)]
    arm_obj = _create_armature(context, coll, bmd, base, scale, bone_axis)
    bone_names = [b for b in arm_obj["bmd_bone_names"]]

    meshes = j3d.extract_meshes(bmd)
    merged = j3d.merge_meshes(bmd, meshes)

    if merged is not None:
        _create_mesh_object(coll, arm_obj, bone_names, bmd, merged, materials,
                            base, scale, warn)
        mesh_count = 1
    else:
        mesh_count = 0

    # keep the raw model data that is not editable in Blender
    # texture order matters in-game (texture animations switch textures by index)
    # The texture list is the exported TEX1 order and the UI for replacing /
    # re-ordering textures; materials reference textures by key, not by index.
    arm_obj["bmd_texture_loading"] = True
    try:
        slots = arm_obj.bmd_texture_list
        for texture in bmd.textures:
            slot = slots.add()
            slot.key = texture.key
            slot.name = texture.name
            slot.image = images.get(texture.key)
    finally:
        if "bmd_texture_loading" in arm_obj:
            del arm_obj["bmd_texture_loading"]
    bc.sync_texture_legacy(arm_obj)
    arm_obj["bmd_source"] = os.path.abspath(filepath)
    arm_obj["bmd_vtx_formats"] = json.dumps({str(a): [c, t, f] for a, c, t, f in bmd.vtx_formats})
    arm_obj["bmd_inf_flags"] = int(bmd.inf_flags)
    arm_obj["bmd_sub_version"] = bmd.sub_version.hex()
    arm_obj["bmd_scale"] = float(scale)
    arm_obj["bmd_bone_axis"] = bone_axis
    if report:
        report({"INFO"}, f"Imported {base}: {len(bmd.joints)} joints, {mesh_count} mesh"
                         f"{'' if mesh_count == 1 else 'es'}, {len(bmd.materials)} materials, "
                         f"{len(bmd.textures)} textures")
        if missing_external:
            report({"WARNING"},
                   f"{len(missing_external)} external texture(s) not found, using "
                   f"placeholders: {', '.join(missing_external)}")
    return arm_obj


# --------------------------------------------------------------------------
# textures & materials
# --------------------------------------------------------------------------

def _texture_signature(t):
    header = (
        bool(t.external),
        int(t.format), int(t.mip_count), int(t.wrap_s), int(t.wrap_t),
        int(t.min_filter), int(t.mag_filter), int(t.min_lod), int(t.max_lod),
        int(t.lod_bias), int(t.transparency), bytes(t.flags10),
        int(t.pal_enabled), int(t.pal_format), int(t.pal_count),
        tuple(int(v) for v in (t.external_size or ())),
    )
    digest = hashlib.sha1()
    digest.update(repr(header).encode("utf-8", "surrogatepass"))
    if t.raw_data is not None:
        # Hash the original encoded payload instead of the decoded RGBA.  This
        # is exact and much faster for large (e.g. 4K CMPR) textures.
        digest.update(t.raw_data)
    else:
        digest.update(b"placeholder")
        if t.rgba is not None:
            digest.update(f"{t.rgba.shape[1]}x{t.rgba.shape[0]}".encode("ascii"))
    return digest.hexdigest()


def _image_texture_signature(image):
    height, width = image.size[1], image.size[0]
    if width == 0 or height == 0:
        return None
    fmt = gx.NAME_TO_FORMAT.get(str(image.get("bmd_orig_format", "")), None)
    if fmt is None:
        fmt = gx.NAME_TO_FORMAT.get(str(image.bmd.format), None)
    if fmt is None:
        fmt = int(image.get("bmd_raw_fmt", 0))
    flags = str(image.get("bmd_flags10", "00000000"))
    try:
        flags_bytes = bytes.fromhex(flags)
    except ValueError:
        flags_bytes = b"\0\0\0\0"
    header = (
        bool(image.bmd.external),
        int(fmt), int(image.bmd.mip_count),
        int(image.get("bmd_wrap_s", 1)), int(image.get("bmd_wrap_t", 1)),
        int(image.get("bmd_min_filter", 1)), int(image.get("bmd_mag_filter", 1)),
        int(image.get("bmd_min_lod", 0)), int(image.get("bmd_max_lod", 0)),
        int(image.get("bmd_lod_bias", 0)), int(image.get("bmd_transparency", 0)),
        flags_bytes,
        int(image.get("bmd_pal_en", 0)), int(image.get("bmd_pal_fmt", 0)),
        int(image.get("bmd_pal_count", 0)),
        tuple(int(v) for v in (image.get("bmd_external_size") or ())),
    )
    digest = hashlib.sha1()
    digest.update(repr(header).encode("utf-8", "surrogatepass"))
    if bool(image.bmd.external):
        digest.update(b"placeholder")
        digest.update(f"{width}x{height}".encode("ascii"))
        return digest.hexdigest()
    raw = image.get("bmd_raw")
    if raw and not image.is_dirty:
        try:
            # Same basis as _texture_signature: the original encoded payload.
            digest.update(zlib.decompress(base64.b64decode(raw)))
            return digest.hexdigest()
        except Exception:
            pass
    buf = np.empty(width * height * 4, np.float32)
    image.pixels.foreach_get(buf)
    pixels = buf.reshape(height, width, 4)[::-1]
    pixels *= 255.0
    np.rint(pixels, out=pixels)
    np.clip(pixels, 0, 255, out=pixels)
    digest.update(pixels.astype(np.uint8))
    return digest.hexdigest()


def apply_texture_metadata(image, texture, path=None):
    """Copy parsed J3D texture metadata to a Blender image."""
    h, w = texture.rgba.shape[:2]
    image["bmd_name"] = texture.name
    image["bmd_key"] = texture.key
    if path is not None:
        image["bmd_source_path"] = os.path.abspath(path)
    image["bmd_transparency"] = int(texture.transparency)
    image["bmd_wrap_s"] = int(texture.wrap_s)
    image["bmd_wrap_t"] = int(texture.wrap_t)
    image["bmd_min_filter"] = int(texture.min_filter)
    image["bmd_mag_filter"] = int(texture.mag_filter)
    image["bmd_min_lod"] = int(texture.min_lod)
    image["bmd_max_lod"] = int(texture.max_lod)
    image["bmd_lod_bias"] = int(texture.lod_bias)
    image["bmd_flags10"] = texture.flags10.hex()
    image["bmd_orig_format"] = gx.FORMAT_NAMES.get(texture.format, str(texture.format))
    if texture.rgba is not None and texture.raw_data is not None:
        # Fast "unchanged since import" check on export (avoids decoding 4K
        # textures just to compare every pixel).
        image["bmd_pixel_hash"] = hashlib.sha1(texture.rgba).hexdigest()
    if texture.external:
        external_size = texture.external_size or (w, h)
        image["bmd_external_size"] = [int(external_size[0]), int(external_size[1])]
    if texture.external_path:
        image["bmd_external_path"] = texture.external_path
    if texture.raw_data is not None:
        image["bmd_raw"] = base64.b64encode(
            zlib.compress(texture.raw_data, 6)).decode("ascii")
        image["bmd_raw_fmt"] = int(texture.format)
        image["bmd_raw_size"] = [int(w), int(h)]
        image["bmd_raw_mips"] = int(texture.mip_count)
        if texture.raw_palette:
            image["bmd_raw_pal"] = base64.b64encode(
                zlib.compress(texture.raw_palette, 6)).decode("ascii")
            image["bmd_pal_fmt"] = int(texture.pal_format)
            image["bmd_pal_count"] = int(texture.pal_count)
        image["bmd_pal_en"] = int(texture.pal_enabled)
    if texture.raw_bti is not None:
        image["bmd_raw_bti"] = base64.b64encode(
            zlib.compress(texture.raw_bti, 6)).decode("ascii")
    fmt_name = gx.FORMAT_NAMES.get(texture.format, "CMPR")
    try:
        image.bmd.format = (
            fmt_name if fmt_name in {i[0] for i in bc.IMAGE_FORMAT_ITEMS} else "CMPR")
        image.bmd.key = texture.key
        image.bmd.wrap_s = str(int(texture.wrap_s))
        image.bmd.wrap_t = str(int(texture.wrap_t))
        image.bmd.min_filter = str(int(texture.min_filter))
        image.bmd.mag_filter = str(int(texture.mag_filter))
        image.bmd.min_lod = int(texture.min_lod)
        image.bmd.lod_bias = int(texture.lod_bias)
        image.bmd.mip_count = int(texture.mip_count)
        image.bmd.max_lod = int(texture.max_lod)
        image.bmd.max_lod_custom = (
            int(texture.max_lod) != (max(1, int(texture.mip_count)) - 1) * 8)
        image.bmd.external = bool(texture.external)
    except Exception:
        pass


def create_texture_image(texture, name=None, path=None):
    """Create a packed Blender image from a parsed J3D texture."""
    height, width = texture.rgba.shape[:2]
    image = bpy.data.images.new(name or texture.key, width, height, alpha=True)
    pixels = (texture.rgba[::-1].astype(np.float32) / 255.0).reshape(-1)
    image.pixels.foreach_set(pixels)

    try:
        image.alpha_mode = "STRAIGHT"
    except Exception:
        pass

    apply_texture_metadata(image, texture, path=path)

    try:
        image.pack()
    except Exception:
        pass

    image.use_fake_user = True
    return image


def _create_images(bmd, base, import_id):
    images = {}
    candidates = {}
    for image in bpy.data.images:
        name = image.get("bmd_name")
        if (not name or not image.get("bmd_key")
                or image.get("bmd_import_id") == import_id):
            continue
        candidates.setdefault(str(name), []).append(image)
    candidate_signatures = {}
    current_keys = {t.key for t in bmd.textures}

    def reusable(t):
        same_name = candidates.get(t.name)
        if not same_name:
            return None
        signature = _texture_signature(t)
        for image in same_name:
            image_key = image.get("bmd_key")
            if image_key != t.key and image_key in current_keys:
                continue
            key = image.name
            if key not in candidate_signatures:
                candidate_signatures[key] = _image_texture_signature(image)
            if candidate_signatures[key] == signature:
                return image
        return None

    for t in bmd.textures:
        existing = reusable(t)
        if existing is not None:
            apply_texture_metadata(existing, t)
            images[t.key] = existing
            continue
        img = create_texture_image(t, name=t.key)
        img["bmd_import_id"] = import_id
        images[t.key] = img
    return images


def _get_json_material(bmd_mat):
    return json.dumps(bmd_mat, separators=(",", ":"))


def _create_material(bmd, index, m, images, warn=None):
    name = bmd.material_names[index] if index < len(bmd.material_names) else f"material{index}"
    mat = bpy.data.materials.new(name)
    mat["bmd_name"] = name
    mat["bmd_index"] = index
    mat["bmd_material"] = _get_json_material(m)
    key_to_index = {t.key: i for i, t in enumerate(bmd.textures)}
    mat["bmd_texture_slots"] = json.dumps(
        [key_to_index.get(key) if key else None
         for key in m.get("texNames", [])], separators=(",", ":"))
    mat.use_backface_culling = (m.get("cull") == 2)
    mat.bmd.render_mode = "AUTO"
    alpha_ref = m["alphaComp"][1] if m.get("alphaComp") else 128
    mat.bmd.alpha_ref = int(alpha_ref)
    try:
        _build_nodes(mat, m, images)
    except Exception as exc:  # node API differences must never break the import
        _warn(warn, f"Material '{name}': preview nodes could not be built: {exc}")
    return mat


_WRAP_EXT = {0: "EXTEND", 1: "REPEAT", 2: "MIRROR"}


def _wrap_axis(nodes, links, src, mode, x, y, owner, axis):
    """Per-axis UV wrapping: 0 clamp, 1 repeat, 2 mirror. Returns the output socket."""
    n = nodes.new("ShaderNodeMath")
    n.location = (x, y)
    n["bmd_wrap_for"] = owner
    n["bmd_wrap_axis"] = axis
    if mode == 1:
        n.operation = "FRACT"
    elif mode == 2:
        n.operation = "PINGPONG"
        n.inputs[1].default_value = 1.0
    else:
        n.operation = "ADD"
        n.inputs[1].default_value = 0.0
        n.use_clamp = True
    links.new(src, n.inputs[0])
    return n.outputs[0]


def _build_nodes(mat, m, images):
    try:
        mat.use_nodes = True
    except Exception:
        pass
    nt = mat.node_tree
    nt.nodes.clear()
    nodes, links = nt.nodes, nt.links
    out = nodes.new("ShaderNodeOutputMaterial")
    out.location = (1100, 0)
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (800, 0)
    links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    for sock, val in (("Roughness", 1.0), ("Specular IOR Level", 0.0), ("Specular", 0.0)):
        if sock in bsdf.inputs:
            bsdf.inputs[sock].default_value = val

    # texture slots -> image nodes
    tex_names = m.get("texNames", [None] * 8)
    slot_nodes = {}
    for slot, tname in enumerate(tex_names):
        if tname is None or tname not in images:
            continue
        node = nodes.new("ShaderNodeTexImage")
        node.name = f"bmd_tex{slot}"
        node.label = f"BMD tex{slot}: {tname}"
        node.image = images[tname]
        node.location = (-100, 300 - 280 * slot)
        img = node.image
        node.interpolation = bc.image_interpolation(int(img.bmd.mag_filter))
        slot_nodes[slot] = node

    # which slot does TEV stage 0 sample?
    main_slot, main_coord = None, 0
    order = m.get("tevOrder", [None])[0]
    if order and order[1] < 8 and order[1] in slot_nodes:
        main_slot, main_coord = order[1], order[0]
    elif slot_nodes:
        main_slot = min(slot_nodes)

    if main_slot is not None:
        node = slot_nodes[main_slot]
        img = node.image
        ws, wt = int(img.bmd.wrap_s), int(img.bmd.wrap_t)
        tc = m.get("texCoord", [None] * 8)
        uv_name = "UV0"
        if main_coord < 8 and tc[main_coord] and 4 <= tc[main_coord][1] <= 11:
            uv_name = f"UV{tc[main_coord][1] - 4}"
        uvn = nodes.new("ShaderNodeUVMap")
        uvn.uv_map = uv_name
        use_chain = ws != wt
        if not use_chain:
            try:
                node.extension = _WRAP_EXT.get(ws, "REPEAT")
            except Exception:
                use_chain = True
        if not use_chain:
            links.new(uvn.outputs["UV"], node.inputs["Vector"])
        else:
            # the image node has a single extension mode, GX has one per axis
            sep = nodes.new("ShaderNodeSeparateXYZ")
            comb = nodes.new("ShaderNodeCombineXYZ")
            links.new(uvn.outputs["UV"], sep.inputs["Vector"])
            xs = _wrap_axis(nodes, links, sep.outputs["X"], ws, 0, 0,
                            node.name, "S")
            ys = _wrap_axis(nodes, links, sep.outputs["Y"], wt, 0, 0,
                            node.name, "T")
            s_node = next(n for n in nodes
                          if n.get("bmd_wrap_for") == node.name
                          and n.get("bmd_wrap_axis") == "S")
            t_node = next(n for n in nodes
                          if n.get("bmd_wrap_for") == node.name
                          and n.get("bmd_wrap_axis") == "T")
            bc._layout_wrap_chain(node, sep, comb, s_node, t_node, uvn)
            links.new(xs, comb.inputs["X"])
            links.new(ys, comb.inputs["Y"])
            links.new(comb.outputs["Vector"], node.inputs["Vector"])
            node.extension = "EXTEND"
    # remaining slots: keep their own wrap mode (only the first axis is representable)
    for slot, node in slot_nodes.items():
        if slot != main_slot:
            try:
                node.extension = _WRAP_EXT.get(int(node.image.bmd.wrap_s), "REPEAT")
            except Exception:
                pass

    color_out = alpha_out = None
    if main_slot is not None:
        color_out = slot_nodes[main_slot].outputs["Color"]
        alpha_out = slot_nodes[main_slot].outputs["Alpha"]

    vc = None
    if mat3.uses_vertex_colors(m):
        vc = nodes.new("ShaderNodeVertexColor")
        vc.layer_name = "Col0"
        vc.location = (-100, -450)
        if color_out is None:
            color_out = vc.outputs["Color"]
        else:
            try:
                mix = nodes.new("ShaderNodeMix")
                mix.data_type = "RGBA"
                mix.blend_type = "MULTIPLY"
                mix.inputs[0].default_value = 1.0
                mix.location = (300, 100)
                links.new(color_out, mix.inputs[6])
                links.new(vc.outputs["Color"], mix.inputs[7])
                color_out = mix.outputs[2]
            except Exception:
                pass
    if color_out is not None:
        links.new(color_out, bsdf.inputs["Base Color"])

    # alpha: tex alpha (TEXA) and/or vertex alpha (RASA) of TEV stage 0, as the game draws it
    kind, ref = mat3.classify_alpha(m)
    if kind != mat3.RENDER_OPAQUE:
        stage = (m.get("tevStage") or [None])[0]
        a_in = stage["alphaIn"] if stage else []
        sources = []
        if alpha_out is not None and (4 in a_in or not a_in):
            sources.append(alpha_out)
        if vc is not None and 5 in a_in:
            sources.append(vc.outputs["Alpha"])
        if not sources and alpha_out is not None:
            sources.append(alpha_out)
        if sources:
            a = sources[0]
            for extra in sources[1:]:
                mul = nodes.new("ShaderNodeMath")
                mul.operation = "MULTIPLY"
                mul.location = (450, -300)
                links.new(a, mul.inputs[0])
                links.new(extra, mul.inputs[1])
                a = mul.outputs[0]
            if kind == mat3.RENDER_CUTOUT:
                gt = nodes.new("ShaderNodeMath")
                gt.operation = "GREATER_THAN"
                gt.inputs[1].default_value = max(0.0, (float(ref) - 0.5) / 255.0)
                gt.location = (600, -300)
                links.new(a, gt.inputs[0])
                a = gt.outputs[0]
            links.new(a, bsdf.inputs["Alpha"])
            try:
                mat.surface_render_method = "BLENDED" if kind == mat3.RENDER_TRANSLUCENT else "DITHERED"
            except Exception:
                pass


# --------------------------------------------------------------------------
# armature
# --------------------------------------------------------------------------

def _unique_names(joints):
    seen, out = set(), []
    for i, j in enumerate(joints):
        n = j.name or f"joint{i}"
        if n in seen:
            n = f"{n}.{i}"
        seen.add(n)
        out.append(n)
    return out


def _create_armature(context, coll, bmd, base, scale, bone_axis):
    joints = bmd.joints
    names = _unique_names(joints)
    arm = bpy.data.armatures.new(base)
    obj = bpy.data.objects.new(f"{base}_armature", arm)
    coll.objects.link(obj)
    obj.show_in_front = True

    fix = bc.R_FIX if bone_axis == "X" else np.eye(4)
    rest = []  # bone matrices in Blender armature space
    for i, j in enumerate(joints):
        W = bc.C @ bmd.world[i] @ bc.C_INV
        W[:3, 3] *= scale
        rest.append(bc.orthonormalize(W) @ fix)

    heads = [r[:3, 3] for r in rest]
    children = {i: [] for i in range(len(joints))}
    for i, j in enumerate(joints):
        if j.parent >= 0:
            children[j.parent].append(i)
    default_len = 5.0 * scale
    lengths = [0.0] * len(joints)
    for i in range(len(joints)):
        d = [np.linalg.norm(heads[c] - heads[i]) for c in children[i]]
        d = [x for x in d if x > 1e-6]
        lengths[i] = min(d) if d else 0.0
    for i in range(len(joints)):
        if lengths[i] <= 0:
            p = joints[i].parent
            lengths[i] = max(lengths[p] * 0.5, default_len * 0.2) if p >= 0 and lengths[p] > 0 else default_len
        lengths[i] = max(lengths[i], default_len * 0.2)

    view_layer = context.view_layer
    prev_active = view_layer.objects.active
    for o in view_layer.objects:
        o.select_set(False)
    view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    ebones = []
    for i, j in enumerate(joints):
        eb = arm.edit_bones.new(names[i])
        eb.head = (0.0, 0.0, 0.0)
        eb.tail = (0.0, float(lengths[i]), 0.0)
        eb.matrix = Matrix(rest[i].tolist())
        ebones.append(eb)
    for i, j in enumerate(joints):
        if j.parent >= 0:
            ebones[i].parent = ebones[j.parent]
            ebones[i].use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    view_layer.objects.active = prev_active

    for i, j in enumerate(joints):
        b = arm.bones[names[i]]
        b["bmd_index"] = i
        b["bmd_parent"] = int(j.parent)
        b["bmd_joint_name"] = j.name
        b["bmd_bone_name"] = names[i]
        b["bmd_kind"] = int(j.kind)
        b["bmd_scale_comp"] = int(j.scale_compensate)
        if bmd.maya and j.scale_compensate and j.parent >= 0:
            # Maya "segment scale compensate": the parent's scale is not inherited
            try:
                b.inherit_scale = "NONE"
            except Exception:
                pass
        b["bmd_local"] = [float(x) for x in (*j.trans, *j.rot, *j.scale)]
        b["bmd_rest"] = [float(x) for x in bc.to_np(b.matrix_local).reshape(-1)]
    obj["bmd_bone_names"] = names
    return obj


# --------------------------------------------------------------------------
# meshes
# --------------------------------------------------------------------------

def _create_mesh_object(coll, arm_obj, bone_names, bmd, md, materials, base, scale,
                        warn=None):
    shape_name = base
    verts = bc.point_to_blender(md.verts, scale)
    # GX front faces are wound the opposite way to Blender's: flip every triangle and
    # permute the per-corner attributes to match.
    faces = md.faces[:, [0, 2, 1]]
    perm = (np.arange(len(faces))[:, None] * 3 + np.array([0, 2, 1])).reshape(-1)
    md.loop_normals = None if md.loop_normals is None else md.loop_normals[perm]
    md.loop_normal_indices = (
        None if md.loop_normal_indices is None
        else md.loop_normal_indices[perm])
    md.loop_colors = [None if c is None else c[perm] for c in md.loop_colors]
    md.loop_uvs = [None if u is None else u[perm] for u in md.loop_uvs]
    me = bpy.data.meshes.new(shape_name)
    me.from_pydata(verts.tolist(), [], faces.tolist())
    me.update()
    n_loops = len(faces) * 3

    # smooth shading so custom normals take effect
    try:
        me.polygons.foreach_set("use_smooth", np.ones(len(faces), dtype=bool))
    except Exception:
        pass

    for t in range(8):
        uv = md.loop_uvs[t]
        if uv is None:
            continue
        layer = me.uv_layers.new(name=f"UV{t}")
        a = uv.astype(np.float32).copy()
        a[:, 1] = 1.0 - a[:, 1]
        layer.data.foreach_set("uv", a.reshape(-1))
    for c in range(2):
        col = md.loop_colors[c]
        if col is None:
            continue
        attr = me.color_attributes.new(name=f"Col{c}", type="FLOAT_COLOR", domain="CORNER")
        attr.data.foreach_set("color", col.astype(np.float32).reshape(-1))

    if md.loop_normals is not None:
        n = bc.point_to_blender(md.loop_normals, 1.0)
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        ln[ln == 0] = 1
        try:
            me.normals_split_custom_set((n / ln).tolist())
        except Exception as exc:
            _warn(warn, f"Mesh '{shape_name}': custom normals failed: {exc}")

    if md.loop_normal_indices is not None and len(md.loop_normal_indices) == n_loops:
        normal_count = len(bmd.arrays.get(j3d.VA_NRM, ()))
        try:
            bmd_metadata.write_metadata(
                me, md.loop_normal_indices, normal_count, source="import",
                position_offset=0, vertex_order=True)
        except ValueError as exc:
            _warn(warn, f"Mesh '{shape_name}': could not store normal metadata: {exc}")

    # Keep the original material indices.  The exporter groups faces by the
    # source shape node, so one material slot per source material is enough.
    for mat in materials:
        me.materials.append(mat)
    if md.face_material is not None and len(md.face_material):
        me.polygons.foreach_set("material_index", md.face_material.astype(np.int32))

    obj = bpy.data.objects.new(shape_name, me)
    coll.objects.link(obj)
    # The single mesh keeps the source node number on each face.  The small table
    # below is only used to restore the shape/joint/material grouping on export.
    attr = me.attributes.new("bmd_node", "INT", "FACE")
    attr.data.foreach_set("value", md.face_node.astype(np.int32))
    obj["bmd_shape_nodes"] = json.dumps(
        [[int(j), int(m), int(s), i,
          int(bmd.shapes[s].matrix_type) if 0 <= s < len(bmd.shapes) else 0]
         for i, (j, m, s) in enumerate(bmd.shape_nodes)])
    obj.parent = arm_obj
    mod = obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj

    groups = {}
    for set_idx, infl in enumerate(md.influence_sets):
        verts_idx = np.nonzero(md.vert_influence == set_idx)[0].tolist()
        if not verts_idx:
            continue
        for joint, weight in infl:
            if joint >= len(bone_names):
                continue
            vg = groups.get(joint)
            if vg is None:
                vg = obj.vertex_groups.new(name=bone_names[joint])
                groups[joint] = vg
            vg.add(verts_idx, float(weight), "REPLACE")
    return obj
