"""Blender material node trees for J3D materials.

Both the BMD importer (TEV stage data) and the interactive material tools
(custom materials, Render Mode / Use Vertex Color) build the same node layout,
so the layout lives here once:

* Material Output at ``(1100, 0)`` and Principled BSDF at ``(800, 0)``;
* ``bmd_tex<N>`` image nodes left of the BSDF (``-320`` per slot);
* a UV Map node below the main texture, with the GX per-axis wrap chain;
* the vertex colour node at ``bsdf - (580, 240)`` with a multiply Mix at
  ``bsdf - (200, 0)``;
* the alpha chain just left of the BSDF (multiply at ``-380``, Greater Than at
  ``-200``).

Nodes created by the interactive tools carry a ``bmd_node_role`` tag so a
Render Mode / Use Vertex Color change can rebuild exactly those nodes without
touching the imported TEV setup.
"""

from __future__ import annotations

import bpy

from . import blender_common as bc
from .j3d import material as mat3

ROLE_PROP = "bmd_node_role"
ROLE_VC = "vc"
ROLE_VC_MIX = "vc_mix"
ROLE_ALPHA_MUL = "alpha_mul"
ROLE_ALPHA_CUTOUT = "alpha_cutout"
ROLES = {ROLE_VC, ROLE_VC_MIX, ROLE_ALPHA_MUL, ROLE_ALPHA_CUTOUT}

READY_MODES = {mat3.RENDER_OPAQUE, mat3.RENDER_CUTOUT,
               mat3.RENDER_TRANSLUCENT}
WRAP_EXT = {0: "EXTEND", 1: "REPEAT", 2: "MIRROR"}

_SUPPRESS = 0


def begin_sync():
    global _SUPPRESS
    _SUPPRESS += 1


def end_sync():
    global _SUPPRESS
    if _SUPPRESS:
        _SUPPRESS -= 1


def sync_suppressed():
    return _SUPPRESS > 0


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def tree_of(material):
    if material is None:
        return None
    if material.node_tree is None:
        try:
            material.use_nodes = True
        except Exception:
            return None
    return material.node_tree


def _node(tree, node_type):
    return next((node for node in tree.nodes if node.type == node_type), None)


def _link(tree, socket, source):
    if socket.is_linked:
        link = socket.links[0]
        if link.from_socket == source:
            return
        tree.links.remove(link)
    tree.links.new(source, socket)


def _unlink(tree, socket):
    for link in list(socket.links):
        tree.links.remove(link)


def _tex_slot(node):
    name = node.name
    if name.startswith("bmd_tex") and name[7:].isdigit():
        return int(name[7:])
    return None


def _wrap_modes(node):
    if node.image is None:
        return 1, 1
    try:
        return int(node.image.bmd.wrap_s), int(node.image.bmd.wrap_t)
    except Exception:
        return 1, 1


def _set_surface_method(material, mode):
    try:
        material.surface_render_method = (
            "BLENDED" if mode == mat3.RENDER_TRANSLUCENT else "DITHERED")
    except Exception:
        pass


def main_texture_node(material):
    """The lowest numbered ``bmd_tex<N>`` node of *material*."""
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return None
    nodes = [node for node in tree.nodes
             if node.type == "TEX_IMAGE" and _tex_slot(node) is not None]
    if not nodes:
        return None
    return min(nodes, key=_tex_slot)


def color_attribute_name(material, owner=None):
    """Colour attribute used by the meshes of *material*.

    Returns an empty name when no mesh has a colour attribute: the Color
    Attribute node then shows an empty field instead of a missing ``Col0``.
    """
    for mesh in bc.material_meshes(material, owner):
        attributes = getattr(mesh, "color_attributes", None)
        if attributes is None:
            continue
        for attribute in attributes:
            if attribute.domain in {"CORNER", "POINT"}:
                return attribute.name
    return ""


def material_for_node(node):
    """Material that owns the shader tree of *node*, if any."""
    tree = getattr(node, "id_data", None)
    if tree is None:
        return None
    owner = getattr(tree, "id_data", None)
    if isinstance(owner, bpy.types.Material):
        return owner
    for material in bpy.data.materials:
        if material.node_tree is tree:
            return material
    return None


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


def apply_wrap_to_node(node, wrap_s, wrap_t, material=None):
    """Apply the GX per-axis wrap (0 clamp, 1 repeat, 2 mirror) to *node*."""
    tree = node.id_data
    chain = {n.get("bmd_wrap_axis"): n for n in tree.nodes
             if n.get("bmd_wrap_for") == node.name}
    if not chain and wrap_s == wrap_t:
        node.extension = WRAP_EXT.get(wrap_s, "REPEAT")
        return
    if not chain:
        uv = None
        if node.inputs["Vector"].is_linked:
            link = node.inputs["Vector"].links[0]
            source = link.from_socket
            if link.from_node.type == "UVMAP":
                uv = link.from_node
        else:
            uv = tree.nodes.new("ShaderNodeUVMap")
            if material is None:
                material = material_for_node(node)
            uv.uv_map = bc.uv_layer_name(material)
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




def texture_node_location(tree, slot):
    """Node position the BMD importer gives ``bmd_tex<N>``."""
    bsdf = next((node for node in tree.nodes
                 if node.type == "BSDF_PRINCIPLED"), None)
    x = bsdf.location.x if bsdf is not None else 800.0
    y = bsdf.location.y if bsdf is not None else 0.0

    return (x - 900.0, y - 320.0 * int(slot))


def uv_node_location(texture_node):
    """Node position the BMD importer gives the UV Map node of a texture."""
    x, y = texture_node.location

    return (x - 200.0, y - 160.0)


# ---------------------------------------------------------------------------
# building blocks shared by the importer and the interactive tools
# ---------------------------------------------------------------------------

def ensure_output_bsdf(tree):
    """Create/position the Material Output and Principled BSDF pair."""
    bsdf = _node(tree, "BSDF_PRINCIPLED")
    if bsdf is None:
        bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (800.0, 0.0)

    output = _node(tree, "OUTPUT_MATERIAL")
    if output is None:
        output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (1100.0, 0.0)

    if not output.inputs["Surface"].is_linked:
        tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    for socket, value in (("Roughness", 1.0), ("Specular IOR Level", 0.0),
                          ("Specular", 0.0)):
        if socket in bsdf.inputs:
            bsdf.inputs[socket].default_value = value

    return output, bsdf


def ensure_texture_node(material, slot, image=None, uv_index=None,
                        label=None, attach_uv=False, link_base=False):
    """Create/update the material's ``bmd_tex<N>`` node.

    *image* may be ``None``: the node is created empty so a texture assigned
    later fills it in.  With *attach_uv* the node gets a UV Map link and the
    GX per-axis wrap chain; otherwise only the single extension mode is set.
    """
    tree = tree_of(material)
    if tree is None:
        return None
    slot = 0 if slot is None else max(0, int(slot))
    name = f"bmd_tex{slot}"

    node = tree.nodes.get(name)
    if node is not None and node.type != "TEX_IMAGE":
        tree.nodes.remove(node)
        node = None

    if node is None:
        node = tree.nodes.new("ShaderNodeTexImage")
        node.name = name
        node.label = label or f"BMD tex{slot}"
        node.location = texture_node_location(tree, slot)

    if image is not None and node.image != image:
        node.image = image
    if node.image is not None:
        try:
            node.interpolation = bc.image_interpolation(
                int(node.image.bmd.mag_filter))
        except Exception:
            pass

    if attach_uv:
        if not node.inputs["Vector"].is_linked:
            uv = next((other for other in tree.nodes
                       if other.type == "UVMAP"), None)
            if uv is None:
                uv = tree.nodes.new("ShaderNodeUVMap")
                uv.location = uv_node_location(node)
            uv.uv_map = bc.uv_layer_name(material, uv_index or 0)
            tree.links.new(uv.outputs["UV"], node.inputs["Vector"])
        wrap_s, wrap_t = _wrap_modes(node)
        apply_wrap_to_node(node, wrap_s, wrap_t, material)
    else:
        wrap_s, _wrap_t = _wrap_modes(node)
        node.extension = WRAP_EXT.get(wrap_s, "REPEAT")

    if link_base:
        bsdf = _node(tree, "BSDF_PRINCIPLED")
        if bsdf is not None and not bsdf.inputs["Base Color"].is_linked:
            tree.links.new(node.outputs["Color"],
                           bsdf.inputs["Base Color"])

    return node


def add_vertex_color(tree, bsdf, layer_name=""):
    """Reuse/create the vertex colour node (tagged for later rebuilds)."""
    vc = next((node for node in tree.nodes
               if node.get(ROLE_PROP) == ROLE_VC), None)
    if vc is None:
        vc = tree.nodes.new("ShaderNodeVertexColor")
        vc[ROLE_PROP] = ROLE_VC
    vc.layer_name = layer_name
    vc.location = (bsdf.location.x - 580.0, bsdf.location.y - 240.0)
    return vc


def apply_color(tree, bsdf, color_source, vc=None):
    """Link Base Color, multiplying by the vertex colour when *vc* is given."""
    if vc is not None and color_source is not None:
        mix = tree.nodes.new("ShaderNodeMix")
        mix[ROLE_PROP] = ROLE_VC_MIX
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.inputs[0].default_value = 1.0
        mix.location = (bsdf.location.x - 200.0, bsdf.location.y)
        tree.links.new(color_source, mix.inputs[6])
        tree.links.new(vc.outputs["Color"], mix.inputs[7])
        color_source = mix.outputs[2]
    elif vc is not None:
        color_source = vc.outputs["Color"]

    if color_source is not None:
        _link(tree, bsdf.inputs["Base Color"], color_source)
    else:
        _unlink(tree, bsdf.inputs["Base Color"])

    return color_source


def alpha_multiply(tree, bsdf, first, second):
    node = tree.nodes.new("ShaderNodeMath")
    node[ROLE_PROP] = ROLE_ALPHA_MUL
    node.operation = "MULTIPLY"
    node.location = (bsdf.location.x - 380.0, bsdf.location.y - 240.0)
    tree.links.new(first, node.inputs[0])
    tree.links.new(second, node.inputs[1])
    return node.outputs[0]


def alpha_cutout(tree, bsdf, source, alpha_ref):
    node = tree.nodes.new("ShaderNodeMath")
    node[ROLE_PROP] = ROLE_ALPHA_CUTOUT
    node.operation = "GREATER_THAN"
    node.inputs[1].default_value = max(0.0,
                                       (float(alpha_ref) - 0.5) / 255.0)
    node.location = (bsdf.location.x - 200.0, bsdf.location.y - 240.0)
    tree.links.new(source, node.inputs[0])
    return node.outputs[0]


def apply_alpha(material, tree, bsdf, sources, mode, alpha_ref=128):
    """Build the alpha chain from *sources* for *mode* and link BSDF Alpha."""
    if mode == mat3.RENDER_OPAQUE or not sources:
        _unlink(tree, bsdf.inputs["Alpha"])
        _set_surface_method(material, mat3.RENDER_OPAQUE)
        return

    alpha = sources[0]
    for extra in sources[1:]:
        alpha = alpha_multiply(tree, bsdf, alpha, extra)
    if mode == mat3.RENDER_CUTOUT:
        alpha = alpha_cutout(tree, bsdf, alpha, alpha_ref)
    _link(tree, bsdf.inputs["Alpha"], alpha)
    _set_surface_method(material, mode)


# ---------------------------------------------------------------------------
# BMD import (TEV data)
# ---------------------------------------------------------------------------

def build_import_nodes(material, data, images):
    """Rebuild an imported BMD material's preview nodes from TEV data."""
    try:
        material.use_nodes = True
    except Exception:
        pass
    tree = material.node_tree
    if tree is None:
        return
    tree.nodes.clear()
    _output, bsdf = ensure_output_bsdf(tree)

    tex_names = data.get("texNames", [None] * 8)
    images_by_slot = {}
    for slot, texture_name in enumerate(tex_names):
        if texture_name is not None and texture_name in images:
            images_by_slot[slot] = images[texture_name]

    main_slot = min(images_by_slot) if images_by_slot else None
    main_coord = 0
    if main_slot is not None:
        for order in data.get("tevOrder", []):
            if order and order[1] == main_slot and 0 <= order[0] < 8:
                main_coord = order[0]
                break

    uv_index = 0
    tex_coord = data.get("texCoord", [None] * 8)
    if (main_coord < 8 and tex_coord[main_coord]
            and 4 <= tex_coord[main_coord][1] <= 11):
        uv_index = tex_coord[main_coord][1] - 4

    slot_nodes = {}
    for slot in sorted(images_by_slot):
        slot_nodes[slot] = ensure_texture_node(
            material, slot, images_by_slot[slot], uv_index=uv_index,
            label=f"BMD tex{slot}: {tex_names[slot]}",
            attach_uv=(slot == main_slot))

    color_source = alpha_source = None
    if main_slot is not None:
        node = slot_nodes[main_slot]
        color_source = node.outputs["Color"]
        alpha_source = node.outputs["Alpha"]

    vc = None
    if mat3.uses_vertex_colors(data):
        vc = add_vertex_color(tree, bsdf, "Col0")
    apply_color(tree, bsdf, color_source, vc)

    kind, ref = mat3.classify_alpha(data)
    if kind == mat3.RENDER_OPAQUE:
        return

    stage = (data.get("tevStage") or [None])[0]
    alpha_in = stage["alphaIn"] if stage else []
    sources = []
    if alpha_source is not None and (4 in alpha_in or not alpha_in):
        sources.append(alpha_source)
    if vc is not None and 5 in alpha_in:
        sources.append(vc.outputs["Alpha"])
    if not sources and alpha_source is not None:
        sources.append(alpha_source)
    apply_alpha(material, tree, bsdf, sources, kind,
                int(ref) if ref is not None else 128)


# ---------------------------------------------------------------------------
# Interactive material creation / refresh
# ---------------------------------------------------------------------------

def ensure_base(material, owner=None):
    """Create the output/BSDF pair and texture nodes for the material slots."""
    tree = tree_of(material)
    if tree is None:
        return None

    _output, bsdf = ensure_output_bsdf(tree)

    slots = bc.material_texture_slots(material, owner) if owner else [None] * 8
    textures = bc.ensure_texture_list(owner) if owner is not None else ()
    main_slot = None
    for slot, index in enumerate(list(slots)[:8]):
        if index is None or not (0 <= index < len(textures)):
            continue
        image = textures[index].image
        if image is None:
            continue
        if main_slot is None:
            main_slot = slot
    for slot, index in enumerate(list(slots)[:8]):
        if index is None or not (0 <= index < len(textures)):
            continue
        image = textures[index].image
        if image is None:
            continue
        ensure_texture_node(material, slot, image,
                            attach_uv=(slot == main_slot))

    return bsdf


def create_material(material, owner, mode):
    """Build a BMD material preview from scratch for *mode*."""
    if material is None or owner is None:
        return False

    from . import btp_preview

    ensure_base(material, owner)

    begin_sync()
    try:
        material.bmd.render_mode = mode
    finally:
        end_sync()

    btp_preview.prepare_preview(material, owner)
    sync_shading(material)
    return True


def _capture_sources(tree):
    """Remove our generated link nodes, keeping their input sources.

    The vertex colour node itself is kept so toggling the Render Mode does not
    re-create it on every change.
    """
    generated = ROLES - {ROLE_VC}
    captured = {}
    for node in list(tree.nodes):
        role = node.get(ROLE_PROP)
        if role not in generated:
            continue
        if role == ROLE_VC_MIX:
            for socket in node.inputs:
                if socket.type == "RGBA" and socket.is_linked:
                    source = socket.links[0].from_socket
                    if source.node.get(ROLE_PROP) != ROLE_VC:
                        captured.setdefault("color", source)
        elif role in {ROLE_ALPHA_MUL, ROLE_ALPHA_CUTOUT}:
            socket = node.inputs[0]
            if socket.is_linked:
                source = socket.links[0].from_socket
                if source.node.get(ROLE_PROP) != ROLE_VC:
                    captured.setdefault("alpha", source)

    for link in list(tree.links):
        if (link.from_node.get(ROLE_PROP) in generated
                or link.to_node.get(ROLE_PROP) in generated):
            tree.links.remove(link)
    for node in list(tree.nodes):
        if node.get(ROLE_PROP) in generated:
            tree.nodes.remove(node)

    return captured


def sync_shading(material):
    """Rebuild the vertex colour and alpha nodes for the material's settings."""
    if material is None or sync_suppressed():
        return
    tree = material.node_tree
    if tree is None:
        return
    bsdf = _node(tree, "BSDF_PRINCIPLED")
    if bsdf is None:
        return

    mode = material.bmd.render_mode
    # Imported TEV materials keep their hand-built preview until a concrete
    # render mode is selected.
    if mode == "AUTO" and material.get("bmd_material"):
        return

    base_color = bsdf.inputs["Base Color"]
    alpha = bsdf.inputs["Alpha"]
    texture = main_texture_node(material)

    captured = _capture_sources(tree)

    color_source = captured.get("color")
    if color_source is None and base_color.is_linked:
        color_source = base_color.links[0].from_socket
    if color_source is None and texture is not None:
        color_source = texture.outputs["Color"]

    alpha_source = captured.get("alpha")
    if alpha_source is None and alpha.is_linked:
        alpha_source = alpha.links[0].from_socket
    if alpha_source is None and texture is not None:
        alpha_source = texture.outputs["Alpha"]

    if mode not in READY_MODES:
        mode = mat3.RENDER_OPAQUE

    vc = None
    if material.bmd.use_vertex_color:
        vc = add_vertex_color(tree, bsdf, color_attribute_name(material))
    else:
        for node in [node for node in tree.nodes
                     if node.get(ROLE_PROP) == ROLE_VC]:
            tree.nodes.remove(node)

    apply_color(tree, bsdf, color_source, vc)

    sources = []
    if alpha_source is not None:
        sources.append(alpha_source)
    if vc is not None and mode != mat3.RENDER_OPAQUE:
        sources.append(vc.outputs["Alpha"])
    apply_alpha(material, tree, bsdf, sources, mode,
                material.bmd.alpha_ref)
