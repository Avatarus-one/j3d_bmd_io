"""BTP preview: swaps the material's ``bmd_tex<N>`` image.

Blender does not allow keyframing or driving the ``image`` pointer of a
``ShaderNodeTexImage``.  The BTP frames therefore live on a material action as
an integer property; this handler mirrors the active frame to the material's
own ``bmd_tex<N>`` node.  No extra preview node is created.  If that node is
missing, it is recreated when BTP preview is prepared/imported.

The BMD exporter reads the J3D Textures sidebar, so swapping this node's image
does not change the exported TEX1 data.
"""

from __future__ import annotations

import json

import bpy

from . import blender_common as bc

IMAGES_PROP = "bmd_btp_images"
BASE_PROP = "bmd_btp_base"
SLOT_PROP = "bmd_btp_slot"
OWNER_PROP = "bmd_btp_owner"
_HANDLER = None
_DEPSGRAPH_HANDLER = None
_SUPPRESS = 0
_UPDATING = False


def begin_update():
    global _SUPPRESS
    _SUPPRESS += 1


def end_update():
    global _SUPPRESS
    if _SUPPRESS:
        _SUPPRESS -= 1


def updates_suppressed():
    return _SUPPRESS > 0


def set_field(material, value):
    """Set the Texture Index without triggering automatic key insertion."""
    if material is None:
        return
    begin_update()
    try:
        material.bmd.btp_texture_index = int(value)
    except Exception:
        pass
    finally:
        end_update()


def default_index(material):
    if material is None:
        return 0
    try:
        return int(material.get(BASE_PROP, 0))
    except (TypeError, ValueError):
        return 0


def _owner_meshes(owner):
    if owner is None:
        return []
    if owner.type == "MESH":
        return [owner]
    if owner.type == "ARMATURE":
        return [obj for obj in bpy.data.objects
                if obj.type == "MESH" and bc.target_armature(obj) is owner]
    return []


def _material_slot(material, owner):
    slots = bc.material_texture_slots(material, owner) if owner else [None] * 8
    for slot, index in enumerate(slots[:8]):
        if index is not None:
            return slot, int(index)
    tree = material.node_tree
    if tree is not None:
        for node in tree.nodes:
            if (node.type == "TEX_IMAGE" and node.name.startswith("bmd_tex")
                    and node.name[7:].isdigit()):
                return int(node.name[7:]), None
        node = _main_node(tree, None)
        if node is not None and node.image is not None and owner is not None:
            for index, slot in enumerate(owner.bmd_texture_list):
                if slot.image is node.image:
                    return None, index
    return None, None


def _main_node(tree, slot):
    if tree is None:
        return None
    if slot is not None:
        node = tree.nodes.get(f"bmd_tex{slot}")
        if node is not None and node.type == "TEX_IMAGE":
            return node
    for node in tree.nodes:
        if node.type == "TEX_IMAGE":
            return node
    return None


def texture_node(material, slot=None, fallback=True):
    """The material's own ``bmd_tex<N>`` node, never a preview copy."""
    if material is None or material.node_tree is None:
        return None
    tree = material.node_tree
    if slot is None:
        try:
            slot = int(material.get(SLOT_PROP, -1))
        except (TypeError, ValueError):
            slot = -1
    if slot is not None and slot >= 0:
        node = tree.nodes.get(f"bmd_tex{slot}")
        if node is not None and node.type == "TEX_IMAGE":
            return node
        if not fallback:
            return None
    for node in tree.nodes:
        if (node.type == "TEX_IMAGE" and node.name.startswith("bmd_tex")
                and node.name[7:].isdigit()):
            return node
    return None


def _take_over_links(tree, node):
    """Route an existing image node's Color/Alpha links through *node*."""
    if any(link.from_node == node for link in tree.links):
        return True
    moved = False
    for link in list(tree.links):
        if (link.from_node != node
                and link.from_node.type == "TEX_IMAGE"
                and link.from_socket.name in {"Color", "Alpha"}):
            to_socket = link.to_socket
            tree.links.remove(link)
            tree.links.new(node.outputs[link.from_socket.name], to_socket)
            moved = True
    return moved


def has_texture_node(material):
    return texture_node(material) is not None


def _image_mapping(owner):
    mapping = {}
    if owner is None:
        return mapping
    try:
        slots = bc.ensure_texture_list(owner)
    except Exception:
        return mapping
    for index, slot in enumerate(slots):
        if slot.image is not None:
            mapping[str(index)] = slot.image.name
    return mapping


def _base_image(mapping, base):
    name = mapping.get(str(base)) if base is not None else None
    return bpy.data.images.get(name) if name else None


def _main_image(material, slot):
    tree = material.node_tree
    if tree is None:
        return None
    node = _main_node(tree, slot)
    return node.image if node is not None else None


def prepare_preview(material, owner, frames=None):
    """Use/recreate the material's own ``bmd_tex<N>`` node for BTP.

    Runs without any texture in the model: an empty node is created and a
    texture assigned later (Textures list or material slot) fills it in.
    """
    if material is None or owner is None:
        return None
    if material.node_tree is None:
        try:
            material.use_nodes = True
        except Exception:
            return None
    if material.node_tree is None:
        return None

    slot, slot_base = _material_slot(material, owner)
    base = slot_base if slot_base is not None else (
        frames[0] if frames else 0)
    had_mapping = bool(material.get(IMAGES_PROP))
    mapping = _image_mapping(owner)
    if not mapping:
        raw = material.get(IMAGES_PROP)
        if raw:
            try:
                mapping = json.loads(raw)
            except Exception:
                mapping = {}

    if slot is None or slot < 0:
        slot = 0
    base_image = _base_image(mapping, base)
    if base_image is None:
        base_image = _main_image(material, slot)

    node = texture_node(material, slot, fallback=False)
    created = node is None
    if created:
        from . import blender_mat
        node = blender_mat.ensure_texture_node(material, slot, base_image,
                                               attach_uv=True)
    if node is None:
        return None
    if base_image is not None and node.image is not base_image:
        node.image = base_image

    if not any(link.from_node == node for link in material.node_tree.links):
        if not any(link.from_node.type == "TEX_IMAGE"
                   and link.from_node.name.startswith("bmd_tex")
                   for link in material.node_tree.links):
            if not _take_over_links(material.node_tree, node):
                _connect_fallback(material.node_tree, node)

    # Remove the extra preview nodes created by older versions.
    for legacy in list(material.node_tree.nodes):
        if legacy == node:
            continue
        if legacy.name == "bmd_btp_preview" or legacy.label == "BTP preview":
            material.node_tree.nodes.remove(legacy)

    material[SLOT_PROP] = int(slot)
    if mapping:
        material[IMAGES_PROP] = json.dumps(mapping, separators=(",", ":"))
    else:
        material.pop(IMAGES_PROP, None)
    material[BASE_PROP] = int(base)
    material[OWNER_PROP] = owner.name
    if frames is None and (not had_mapping or created):
        set_field(material, base)

    return node


def ensure_preview(material, owner):
    """Ensure the material's ``bmd_tex<N>`` node exists for BTP."""
    return prepare_preview(material, owner, None)


def show_index(material, index):
    """Show a texture index immediately (used by the material property)."""
    if material is None or material.node_tree is None:
        return
    raw = material.get(IMAGES_PROP)
    if not raw:
        return
    try:
        mapping = json.loads(raw)
    except Exception:
        return
    name = mapping.get(str(index))
    image = bpy.data.images.get(name) if name else None
    if image is None:
        try:
            image = _main_image(material, int(material.get(SLOT_PROP, -1)))
        except (TypeError, ValueError):
            image = None
    node = texture_node(material)
    if image is not None and node is not None and node.image != image:
        node.image = image


def migrate_action(action):
    """Move the old raw custom-property curve to the material property."""
    if action is None:
        return
    try:
        curves = list(action.fcurves)
    except Exception:
        return
    for fcurve in curves:
        if fcurve.data_path == '["bmd_btp_index"]':
            fcurve.data_path = "bmd.btp_texture_index"


def _connect_fallback(tree, preview):
    bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        return
    for socket_name in ("Base Color", "Alpha"):
        socket = bsdf.inputs.get(socket_name)
        if socket is not None and not socket.is_linked:
            tree.links.new(preview.outputs[
                "Alpha" if socket_name == "Alpha" else "Color"], socket)


def _find_btp_curve(action):
    if action is None:
        return None
    for path in ("bmd.btp_texture_index", '["bmd_btp_index"]'):
        for fcurve in action.fcurves:
            if fcurve.data_path == path:
                return fcurve
    return None


def _curve_value(fcurve, frame):
    points = sorted(fcurve.keyframe_points, key=lambda point: point.co[0])
    if not points:
        return None
    value = points[0].co[1]
    for point in points:
        if point.co[0] > frame:
            break
        value = point.co[1]
    return value


def _curve_index(fcurve, frame):
    value = _curve_value(fcurve, frame)
    return None if value is None else int(round(value))


def _frame_index(action, frame, default=None):
    """Frame index from the action's keys only."""
    fcurve = _find_btp_curve(action)
    if fcurve is None:
        return default
    index = _curve_index(fcurve, frame)
    return default if index is None else index


def set_step_interpolation(action):
    """BTP indices are step values, never interpolated."""
    if action is None:
        return
    fcurve = _find_btp_curve(action)
    if fcurve is None:
        return
    try:
        for point in fcurve.keyframe_points:
            if point.interpolation != "CONSTANT":
                point.interpolation = "CONSTANT"
    except Exception:
        pass


def _owner_for_material(material, context):
    try:
        from . import animation_registry
    except Exception:
        return None
    obj = getattr(context, "active_object", None)
    if obj is not None:
        owner = animation_registry.owner_for(obj)
        if owner is not None:
            return owner
    for obj in bpy.data.objects:
        if obj.type not in {"MESH", "ARMATURE"}:
            continue
        if any(slot.material is material for slot in obj.material_slots):
            owner = animation_registry.owner_for(obj)
            if owner is not None:
                return owner
    return None


def auto_key(material, value, context=None):
    """Insert/update a BTP key at the current frame after a UI change."""
    if material is None or updates_suppressed():
        return
    context = context or bpy.context
    scene = getattr(context, "scene", None)
    if scene is None:
        return
    frame = int(scene.frame_current)
    animation_data = material.animation_data
    action = animation_data.action if animation_data is not None else None

    if action is not None:
        migrate_action(action)
        fcurve = _find_btp_curve(action)
        if fcurve is not None:
            current = _curve_index(fcurve, frame)
            if current is not None and current == int(value):
                return

    material.keyframe_insert(data_path="bmd.btp_texture_index", frame=frame)
    animation_data = material.animation_data
    action = animation_data.action if animation_data is not None else None
    set_step_interpolation(action)

    owner = _owner_for_material(material, context)
    if owner is None:
        return
    try:
        from . import animation_registry
        registry = animation_registry.registry_for(owner)
        if registry is None:
            return
        entry = animation_registry._active_entry(registry)
        if entry is None:
            entry = animation_registry.ensure_default_animation(
                context, owner, select=False)
        if entry is not None:
            animation_registry._attach_active_actions(entry, owner, registry)
        animation_registry.sort_animations(registry)
    except Exception:
        pass


def update_material(material, frame):
    global _UPDATING
    if material is None or material.node_tree is None or _UPDATING:
        return
    node = texture_node(material)
    if node is None:
        return
    raw = material.get(IMAGES_PROP)
    if not raw:
        return
    try:
        mapping = json.loads(raw)
    except Exception:
        return

    base = default_index(material)
    index = None
    animation_data = material.animation_data
    action = animation_data.action if animation_data is not None else None
    if action is not None:
        set_step_interpolation(action)
        index = _frame_index(action, frame, base)
    if index is None:
        bmd = getattr(material, "bmd", None)
        if bmd is not None:
            try:
                index = int(bmd.btp_texture_index)
            except (TypeError, ValueError):
                index = None
    if index is None:
        index = base

    _UPDATING = True
    try:
        try:
            if int(material.bmd.btp_texture_index) != int(index):
                set_field(material, index)
        except Exception:
            pass
        name = mapping.get(str(index))
        image = bpy.data.images.get(name) if name else None
        if image is None:
            image = _base_image(mapping, base)
        if image is not None and node.image != image:
            node.image = image
    finally:
        _UPDATING = False


@bpy.app.handlers.persistent
def _frame_change(scene, _depsgraph=None):
    if scene is None:
        scene = bpy.context.scene
    frame = int(scene.frame_current)
    for material in bpy.data.materials:
        if IMAGES_PROP in material:
            update_material(material, frame)


@bpy.app.handlers.persistent
def _depsgraph_update(scene, _depsgraph=None):
    if updates_suppressed():
        return
    if scene is None:
        scene = bpy.context.scene
    frame = int(scene.frame_current)
    for material in bpy.data.materials:
        if IMAGES_PROP not in material:
            continue
        animation_data = material.animation_data
        action = animation_data.action if animation_data is not None else None
        if action is None:
            continue
        if _find_btp_curve(action) is not None:
            update_material(material, frame)


def install():
    global _HANDLER, _DEPSGRAPH_HANDLER
    if _HANDLER is None:
        _HANDLER = _frame_change
    if _DEPSGRAPH_HANDLER is None:
        _DEPSGRAPH_HANDLER = _depsgraph_update
    handlers = bpy.app.handlers.frame_change_post
    if _HANDLER not in handlers:
        handlers.append(_HANDLER)
    depsgraph_handlers = bpy.app.handlers.depsgraph_update_post
    if _DEPSGRAPH_HANDLER not in depsgraph_handlers:
        depsgraph_handlers.append(_DEPSGRAPH_HANDLER)


def uninstall():
    handlers = bpy.app.handlers.frame_change_post
    if _HANDLER in handlers:
        handlers.remove(_HANDLER)
    depsgraph_handlers = bpy.app.handlers.depsgraph_update_post
    if _DEPSGRAPH_HANDLER in depsgraph_handlers:
        depsgraph_handlers.remove(_DEPSGRAPH_HANDLER)


def reset_material(material):
    if material is None or material.node_tree is None:
        return
    node = texture_node(material)
    raw = material.get(IMAGES_PROP)
    if node is None or not raw:
        return
    try:
        mapping = json.loads(raw)
    except Exception:
        return
    try:
        base = int(material.get(BASE_PROP, 0))
    except (TypeError, ValueError):
        base = 0
    set_field(material, base)
    image = _base_image(mapping, base)
    if image is None:
        try:
            slot = int(material.get(SLOT_PROP, -1))
        except (TypeError, ValueError):
            slot = -1
        image = _main_image(material, slot)
    if image is not None:
        node.image = image
