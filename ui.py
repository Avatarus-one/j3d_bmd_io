"""Sidebar panels, material/texture operators and menus.

The import/export operators live in :mod:`j3d_bmd_io`; this module owns every
piece of UI that is drawn in the 3D View sidebar, the Material and Image
Editor tabs, and the File/Image/Node menus.
"""

from __future__ import annotations

import json
import os

import bpy
from bpy.props import (BoolProperty, EnumProperty, IntProperty, PointerProperty,
                       StringProperty)
from bpy.types import Operator, Panel, UIList
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import blender_common as bc


class MATERIAL_PT_bmd(Panel):
    bl_label = "BMD Material"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"

    @classmethod
    def poll(cls, context):
        return context.material is not None

    def draw(self, context):
        layout = self.layout
        mat = context.material
        owner = _texture_owner(context)

        shader_active = bool(mat.get("bmd_shader_active"))
        if not (bc.is_bmd_material(mat) or shader_active):
            layout.label(text="Not a BMD material", icon="INFO")
            row = layout.row(align=True)
            row.operator_context = "INVOKE_DEFAULT"
            op = row.operator(BMD_OT_material_shader_import.bl_idname,
                              text="Import Shader", icon="IMPORT")
            op.material_name = mat.name
            op.owner_name = owner.name if owner is not None else ""
            column = layout.column()
            column.enabled = owner is not None
            column.operator_context = "INVOKE_DEFAULT"
            column.operator("bmd.material_create_menu",
                            text="Create BMD Material", icon="MATERIAL")
            return

        row = layout.row(align=True)
        row.operator_context = "INVOKE_DEFAULT"
        op = row.operator(BMD_OT_material_shader_import.bl_idname,
                          text="Import Shader", icon="IMPORT")
        op.material_name = mat.name
        op.owner_name = owner.name if owner is not None else ""
        export_row = row.row(align=True)
        export_row.enabled = bool(shader_active or "bmd_material" in mat)
        op = export_row.operator(BMD_OT_material_shader_export.bl_idname,
                                 text="Export Shader", icon="EXPORT")
        op.material_name = mat.name
        op.owner_name = owner.name if owner is not None else ""

        reset_row = layout.row(align=True)
        reset_row.enabled = shader_active
        op = reset_row.operator(BMD_OT_material_shader_reset.bl_idname,
                                text="Reset Shader", icon="LOOP_BACK")
        op.material_name = mat.name
        op.owner_name = owner.name if owner is not None else ""

        layout.prop(mat.bmd, "render_mode")
        if mat.bmd.render_mode in {"AUTO", "CUTOUT"}:
            layout.prop(mat.bmd, "alpha_ref")
        layout.prop(mat, "use_backface_culling")

        box = layout.box()
        box.label(text="Textures", icon="IMAGE_DATA")
        slots = _material_texture_slots(mat, owner)
        items = _texture_items(owner)
        if slots:
            for slot in sorted(slots):
                owner_index = slots[slot]
                owner_slot = owner.bmd_texture_list[owner_index]
                key = bc.texture_slot_key(owner_slot)
                label = f"{owner_index}: {owner_slot.name or key}"
                row = box.row(align=True)
                # An empty slot (a texture the imported shader expects) needs an
                # image from the user, so it is highlighted.
                row.alert = owner_slot.image is None
                row.label(text=f"{slot}: {label}")
                op = row.operator(BMD_OT_material_texture_menu.bl_idname,
                                  text="", icon="IMAGE_DATA")
                op.slot = slot
                op.add = False
                op.owner_name = owner.name if owner is not None else ""
                op = row.operator(BMD_OT_texture_focus.bl_idname, text="",
                                  icon="PREFERENCES")
                op.owner_name = owner.name if owner is not None else ""
                op.texture_index = owner_index
                op = row.operator(BMD_OT_material_remove_texture.bl_idname,
                                  text="", icon="X")
                op.slot = slot
        else:
            if owner is not None and not items:
                box.label(text="Add textures in the J3D → Textures first", icon="INFO")
        if owner is None:
            box.label(text="No BMD texture list for this material", icon="INFO")
        elif items:
            if len(slots) < 8:
                add = box.operator(BMD_OT_material_texture_menu.bl_idname, text="Add Texture", icon="ADD")
                add.owner_name = owner.name
                add.add = True

        _draw_btp_box(layout, mat, owner)


def _draw_btp_box(layout, mat, owner):
    box = layout.box()
    box.label(text="Material Animation", icon="ANIM")
    tracks = mat.bmd.tracks
    for index, track in enumerate(tracks):
        row = box.row(align=True)
        if track.type == "BTP":
            row.label(text="Texture Index")
            row.prop(mat.bmd, "btp_texture_index", text="")
        else:
            row.label(text=track.type)
        refresh = row.operator("bmd.material_track_refresh", text="",
                               icon="FILE_REFRESH")
        refresh.index = index
        refresh.material_name = mat.name
        refresh.owner_name = owner.name if owner is not None else ""
        op = row.operator("bmd.material_track_remove", text="", icon="X")
        op.index = index
        op.material_name = mat.name
    if len(tracks) < len(bc.MATERIAL_TRACK_ITEMS):
        box.operator_context = "INVOKE_DEFAULT"
        add = box.operator("bmd.material_track_menu", text="Add Track",
                           icon="ADD")
        add.material_name = mat.name
        add.owner_name = owner.name if owner is not None else ""


def _draw_bmd_prop(layout, data, prop, label, factor=0.42, alert=False):
    """Label + property field with a fixed-width label column."""
    row = layout.split(factor=factor)
    row.alert = alert
    row.alignment = "RIGHT"
    row.label(text=label)
    row.prop(data, prop, text="")


def _draw_bmd_image_settings(layout, img):
    """Image (pixel block) settings: format, mips, external storage."""
    _draw_bmd_prop(layout, img.bmd, "mip_count", "Mip Levels", 0.3)
    row = layout.split(factor=0.3)
    row.label(text="")
    row.prop(img.bmd, "external")


def _draw_slot_sampler(layout, slot):
    """GX sampler state of one TEX1 entry."""
    _draw_bmd_prop(layout, slot, "wrap_s", "Wrap S", 0.3)
    _draw_bmd_prop(layout, slot, "wrap_t", "Wrap T", 0.3)
    _draw_bmd_prop(layout, slot, "min_filter", "Min Filter", 0.3)
    _draw_bmd_prop(layout, slot, "mag_filter", "Mag Filter", 0.3)
    _draw_bmd_prop(layout, slot, "min_lod", "Min LOD", 0.3)
    _draw_bmd_prop(layout, slot, "max_lod", "Max LOD", 0.3)
    _draw_bmd_prop(layout, slot, "lod_bias", "LOD Bias", 0.3)


class IMAGE_PT_bmd(Panel):
    bl_label = "J3D (BMD) Texture"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Image"

    @classmethod
    def poll(cls, context):
        space = context.space_data
        return (space is not None
                and getattr(space, "image", None) is not None)

    def draw(self, context):
        img = context.space_data.image

        if not bc.is_bmd_image(img):
            self.layout.label(text="Not a BMD texture", icon="INFO")
            return

        _draw_bmd_prop(self.layout, img.bmd, "format", "GX Format", 0.3)
        _draw_bmd_image_settings(self.layout, img)
        external_path = img.get("bmd_external_path")
        if external_path:
            self.layout.label(text=f"External: {os.path.basename(external_path)}")


def menu_node_import_bti(self, context):
    space = getattr(context, "space_data", None)

    if (space is None or getattr(space, "type", None) != "NODE_EDITOR"
            or getattr(space, "tree_type", None) != "ShaderNodeTree"):
        return

    self.layout.operator("import_image.bti_node",
                         text="Import J3D Texture (.bti)", icon="IMPORT")


def _draw_format_box(layout, title, items):
    box = layout.box()
    box.label(text=title)
    row = box.row(align=True)
    for idname, text, icon in items:
        row.operator(idname, text=text, icon=icon)


class BMD_PT_j3d(Panel):
    bl_idname = "BMD_PT_j3d"
    bl_label = "Import / Export"
    bl_order = 0
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "J3D"

    def draw(self, context):
        layout = self.layout

        _draw_format_box(layout, "Model (.bmd/.bdl)", (
            ("import_scene.bmd", "Import", "IMPORT"),
            ("export_scene.bmd", "Export", "EXPORT"),
        ))

        _draw_format_box(layout, "Shape Keys (.bls)", (
            ("bmd_shape_keys.import_files", "Import", "IMPORT"),
            ("bmd_shape_keys.export_files", "Export", "EXPORT"),
        ))

        _draw_format_box(layout, "Collision (.kcl)", (
            ("import_scene.kcl", "Import", "IMPORT"),
            ("export_scene.kcl", "Export", "EXPORT"),
        ))


def _custom_meshes(context):
    """Selected meshes that do not belong to a BMD model yet."""
    return [obj for obj in (getattr(context, "selected_objects", ()) or ())
            if obj.type == "MESH"
            and not bc.is_bmd_object(obj)
            and not any(str(key).startswith("kcl_") for key in obj.keys())]


def _custom_kcl_meshes(context):
    """Selected custom meshes that can be initialized as KCL."""
    return [obj for obj in (getattr(context, "selected_objects", ()) or ())
            if obj.type == "MESH"
            and not bc.is_bmd_object(obj)
            and not bc.is_kcl_object(obj)]


class BMD_OT_initialize_bmd(Operator):
    bl_idname = "bmd.initialize_bmd"
    bl_label = "Initialize as BMD"
    bl_description = ("Add the standard BMD data to the selected mesh and "
                      "create a BMD armature when it has none")
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_custom_meshes(context))

    def execute(self, context):
        meshes = _custom_meshes(context)

        if not meshes:
            self.report({"ERROR"}, "Select a custom mesh first")
            return {"CANCELLED"}

        for mesh in meshes:
            try:
                bc.initialize_as_bmd(context, mesh)
            except Exception as exc:
                self.report({"ERROR"}, f"{mesh.name}: {exc}")

        return {"FINISHED"}


class BMD_OT_initialize_kcl(Operator):
    bl_idname = "bmd.initialize_kcl"
    bl_label = "Initialize as KCL"
    bl_description = ("Mark the selected mesh as a KCL collision mesh and "
                      "add the fields needed for KCL export")
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_custom_kcl_meshes(context))

    def execute(self, context):
        meshes = _custom_kcl_meshes(context)

        if not meshes:
            self.report({"ERROR"}, "Select a custom mesh first")
            return {"CANCELLED"}

        from . import blender_kcl

        for mesh in meshes:
            try:
                blender_kcl.initialize_as_kcl(mesh)
            except Exception as exc:
                self.report({"ERROR"}, f"{mesh.name}: {exc}")

        return {"FINISHED"}


class BMD_OT_set_30_fps(Operator):
    bl_idname = "bmd.set_30_fps"
    bl_label = "Set 30 FPS"
    bl_description = "Set the scene frame rate to 30 FPS"
    bl_options = {"UNDO"}

    @classmethod
    def poll(cls, context):
        scene = getattr(context, "scene", None)
        if scene is None:
            return False
        render = scene.render
        return not (int(render.fps) == 30
                    and abs(float(render.fps_base) - 1.0) < 1e-6)

    def execute(self, context):
        context.scene.render.fps = 30
        context.scene.render.fps_base = 1.0
        return {"FINISHED"}


class BMD_PT_utility(Panel):
    bl_label = "Utility"
    bl_order = 1
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "J3D"

    def draw(self, context):
        layout = self.layout
        column = layout.column(align=True)

        initialize_row = column.row(align=True)
        initialize_row.enabled = bool(_custom_meshes(context))
        initialize_row.operator(BMD_OT_initialize_bmd.bl_idname,
                                text="Initialize as BMD", icon="MESH_DATA")

        kcl_row = column.row(align=True)
        kcl_row.enabled = bool(_custom_kcl_meshes(context))
        kcl_row.operator(BMD_OT_initialize_kcl.bl_idname,
                         text="Initialize as KCL", icon="MESH_DATA")

        column.separator()

        column.operator(BMD_OT_set_30_fps.bl_idname, text="Set 30 FPS",
                        icon="TIME")


class BMD_PT_animations(Panel):
    bl_idname = "BMD_PT_animations"
    bl_label = "Animations"
    bl_order = 4
    bl_options = {"DEFAULT_CLOSED"}
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "J3D"

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        from . import animation_registry as anim
        from . import anim_export_ui

        layout = self.layout
        layout.use_property_decorate = False
        owner = _selected_model(context)
        registry = anim.registry_for(owner) if owner is not None else None
        has_animations = anim_export_ui.scene_has_animations(context)

        if registry is None:
            layout.label(text="Select a BMD model", icon="INFO")
            row = layout.row(align=True)
            row.operator("import_anim.j3d", text="Import Animations",
                         icon="IMPORT")
            export_row = row.row(align=True)
            export_row.enabled = has_animations
            export_row.operator("export_anim.j3d", text="Export Animations",
                                icon="EXPORT")
            return

        layout.label(text=f"Owner: {owner.name}", icon="OBJECT_DATA")
        row = layout.row()
        row.template_list(
            "BMD_UL_animations", "",
            registry, "animations",
            registry, "active_index",
            rows=6,
        )
        column = row.column(align=True)
        column.operator("bmd_shape_keys.animation_new", text="", icon="ADD")
        column.operator("bmd_shape_keys.animation_copy", text="",
                        icon="DUPLICATE")
        column.operator("bmd_shape_keys.animation_delete", text="",
                        icon="REMOVE")
        column.separator()
        column.operator("import_anim.j3d", text="", icon="IMPORT")
        export_row = column.row(align=True)
        export_row.enabled = has_animations
        export_row.operator("export_anim.j3d", text="", icon="EXPORT")

        entry = anim._active_entry(registry)

        if entry is None:
            layout.label(text="No animations", icon="INFO")
            return

        box = layout.box()
        anim._draw_prop_row(box, entry, "name", "Name", 0.2)
        row = box.split(factor=0.2)
        row.alignment = "RIGHT"
        row.label(text="Range")
        frames = row.row(align=True)
        frames.prop(entry, "frame_start", text="Start")
        frames.prop(entry, "frame_end", text="End")
        anim._draw_prop_row(box, entry, "loop_mode", "Loop Mode")

        for track in entry.tracks:
            if not anim._track_has_keys(track):
                continue

            icon_name = anim.FORMAT_ICONS.get(track.format, "DOT")
            box.label(text=f"{track.format}: {track.target}",
                      icon=icon_name)


def _texture_mesh(context, owner=None):
    """Selected mesh, or the first mesh deformed by *owner*."""
    obj = context.active_object
    if obj is not None and obj.type == "MESH":
        return obj
    for selected in context.selected_objects:
        if selected.type == "MESH":
            return selected
    if owner is not None:
        for candidate in bpy.data.objects:
            if candidate.type == "MESH" and bc.target_armature(candidate) == owner:
                return candidate
    return None


_LAST_MODEL = None


def _valid_model(obj):
    if obj is None:
        return None
    try:
        if obj.name in bpy.data.objects:
            return obj
    except ReferenceError:
        pass
    return None


def _model_target(obj):
    """BMD owner shown by the panels for *obj* (armature for a skinned mesh)."""
    if obj is None:
        return None
    if obj.type == "ARMATURE":
        return obj if bc.is_bmd_object(obj) else None
    if obj.type == "MESH":
        arm = bc.target_armature(obj)
        if arm is not None and bc.is_bmd_object(arm):
            return arm
        return obj if bc.is_bmd_object(obj) else None
    return None


def _selected_model(context):
    """The BMD model targeted by every sidebar panel.

    The active model has priority (orange outline).  When the active object is
    not a BMD model, the first selected BMD model is used instead (red outline).
    Selecting an unrelated mesh/armature returns ``None`` so the panels can
    show "Select a BMD model" and avoid writing data into it.
    """
    global _LAST_MODEL
    active = getattr(context, "active_object", None)
    model = _model_target(active)
    if model is not None:
        _LAST_MODEL = model
        return model

    if active is not None and active.type in {"MESH", "ARMATURE"}:
        return None

    selected = list(getattr(context, "selected_objects", ()) or ())
    for obj in selected:
        model = _model_target(obj)
        if model is not None:
            _LAST_MODEL = model
            return model

    if active is not None or selected:
        return None

    model = _valid_model(_LAST_MODEL)
    if model is None or not bc.is_bmd_object(model):
        _LAST_MODEL = None
        return None
    return model


def _texture_owner(context):
    return _selected_model(context)


def _format_owner(context):
    return _selected_model(context)


def _active_material(context):
    mat = getattr(context, "material", None)
    if mat is not None:
        return mat
    obj = getattr(context, "active_object", None)
    if obj is not None:
        return obj.active_material
    return None


def _texture_items(owner):
    """[(BMD texture index, slot, image)] for slots that have an image."""
    if owner is None:
        return []
    coll = bc.texture_list(owner)
    return [(i, slot, slot.image) for i, slot in enumerate(coll)
            if slot.image is not None]


def _material_texture_slots(mat, owner=None):
    """{material texture slot: BMD texture index}."""
    indices = bc.material_texture_slots(mat, owner, store=False)
    out = {}
    for n, s in enumerate(indices):
        if (s is not None and owner is not None
                and 0 <= s < len(owner.bmd_texture_list)):
            out[n] = s
    return out


def _texture_label_for_key(owner, key):
    if owner is not None:
        for i, slot in enumerate(owner.bmd_texture_list):
            if bc.texture_slot_key(slot) == key:
                return f"{i}: {slot.name or key}"
    return key


def _set_material_texname(mat, slot, key):
    stored = mat.get("bmd_material")
    if not stored:
        return
    try:
        data = json.loads(stored)
    except Exception:
        return
    names = list(data.get("texNames") or [])
    while len(names) < 8:
        names.append(None)
    names[slot] = key
    data["texNames"] = names
    mat["bmd_material"] = json.dumps(data, separators=(",", ":"))


def _set_material_texture(mat, slot, image, texture_index, owner=None):
    from . import blender_mat
    node = blender_mat.ensure_texture_node(mat, slot, image, attach_uv=True,
                                           link_base=True)
    key = bc.texture_key(image)

    if owner is not None and 0 <= texture_index < len(owner.bmd_texture_list):
        slot_item = owner.bmd_texture_list[texture_index]
        key = str(slot_item.key or "") or key

    _set_material_texname(mat, slot, key)
    slots = bc.material_texture_slots(mat, owner)
    slots[slot] = texture_index
    bc.set_material_texture_slots(mat, slots)


def _first_free_slot(mat, owner=None):
    used = set(_material_texture_slots(mat, owner))
    for i in range(8):
        if i not in used:
            return i
    return None


def _preview_icon(image):
    """Preview icon id for an image, or 0 when no preview is available."""
    try:
        if image.preview is None:
            image.preview_ensure()
        preview = image.preview
        if preview is not None:
            return preview.icon_id
    except Exception:
        pass
    return 0


class BMD_OT_material_set_texture(Operator):
    bl_idname = "bmd.material_set_texture"
    bl_label = "Set Material Texture"
    bl_description = "Change the texture used by this material slot"
    bl_options = {"UNDO"}

    slot: IntProperty(name="Slot", default=0, min=0, max=7)
    owner_name: StringProperty(name="Owner", default="")
    texture: StringProperty(name="Texture", default="")

    def execute(self, context):
        mat = _active_material(context)
        owner = (bpy.data.objects.get(self.owner_name)
                 if self.owner_name else _texture_owner(context))
        if mat is None or owner is None:
            return {"CANCELLED"}
        try:
            index = int(self.texture)
        except (TypeError, ValueError):
            return {"CANCELLED"}
        coll = bc.ensure_texture_list(owner)
        if not (0 <= index < len(coll)) or coll[index].image is None:
            return {"CANCELLED"}
        _set_material_texture(mat, self.slot, coll[index].image, index, owner)
        return {"FINISHED"}


class BMD_OT_material_add_texture(Operator):
    bl_idname = "bmd.material_add_texture"
    bl_label = "Add Material Texture"
    bl_description = "Add a texture from the BMD texture list to this material"
    bl_options = {"UNDO"}

    owner_name: StringProperty(name="Owner", default="")
    texture: StringProperty(name="Texture", default="")

    def execute(self, context):
        mat = _active_material(context)
        owner = (bpy.data.objects.get(self.owner_name)
                 if self.owner_name else _texture_owner(context))
        if mat is None or owner is None:
            return {"CANCELLED"}
        try:
            index = int(self.texture)
        except (TypeError, ValueError):
            return {"CANCELLED"}
        coll = bc.ensure_texture_list(owner)
        if not (0 <= index < len(coll)) or coll[index].image is None:
            return {"CANCELLED"}
        slot = _first_free_slot(mat, owner)
        if slot is None:
            self.report({"ERROR"}, "All 8 material texture slots are used")
            return {"CANCELLED"}
        _set_material_texture(mat, slot, coll[index].image, index, owner)
        return {"FINISHED"}


class BMD_OT_material_remove_texture(Operator):
    bl_idname = "bmd.material_remove_texture"
    bl_label = "Remove Material Texture"
    bl_description = "Remove this texture slot from the material"
    bl_options = {"UNDO"}

    slot: IntProperty(name="Slot", default=0, min=0, max=7)

    def execute(self, context):
        mat = _active_material(context)
        if mat is None:
            return {"CANCELLED"}
        owner = _texture_owner(context)
        tree = mat.node_tree
        if tree is not None:
            for name in (f"bmd_tex{self.slot}", f"bmd_uv_tex{self.slot}",
                         f"bmd_wrap_tex{self.slot}"):
                node = tree.nodes.get(name)
                if node is None:
                    continue
                group = node.node_tree if node.type == "GROUP" else None
                tree.nodes.remove(node)
                if group is not None and group.users == 0:
                    bpy.data.node_groups.remove(group)
        _set_material_texname(mat, self.slot, None)
        slots = bc.material_texture_slots(mat, owner)
        slots[self.slot] = None
        bc.set_material_texture_slots(mat, slots)
        return {"FINISHED"}


def _set_j3d_category(area):
    for region in area.regions:
        if region.type != "UI":
            continue
        try:
            region.active_panel_category = "J3D"
            area.tag_redraw()
            return True
        except Exception:
            pass
    return False


def _activate_j3d_sidebar(area):
    """Open the sidebar and switch it to J3D after the UI region exists."""
    area.spaces.active.show_region_ui = True
    pointer = area.as_pointer()
    attempts = [0]

    def apply_category():
        screen = getattr(bpy.context, "screen", None)
        target = None
        if screen is not None:
            target = next((a for a in screen.areas
                           if a.as_pointer() == pointer), None)
            if target is None:
                target = next((a for a in screen.areas if a.type == "VIEW_3D"), None)
        if target is not None and _set_j3d_category(target):
            return None
        attempts[0] += 1
        return 0.05 if attempts[0] < 10 else None

    bpy.app.timers.register(apply_category, first_interval=0.05)


class BMD_OT_texture_focus(Operator):
    bl_idname = "bmd.texture_focus"
    bl_label = "Show BMD Texture"
    bl_description = "Show this texture's preview and settings"
    bl_options = set()

    owner_name: StringProperty(name="Owner", default="")
    texture_index: IntProperty(name="Texture Index", default=-1)

    def _owner(self, context):
        if self.owner_name:
            return bpy.data.objects.get(self.owner_name)

        return _texture_owner(context)

    def invoke(self, context, _event):
        owner = self._owner(context)

        if owner is None:
            return {"CANCELLED"}

        coll = bc.ensure_texture_list(owner)

        if not (0 <= self.texture_index < len(coll)):
            return {"CANCELLED"}

        return context.window_manager.invoke_popup(self, width=360)

    def draw(self, context):
        owner = self._owner(context)

        if owner is None:
            self.layout.label(text="Texture owner not found", icon="ERROR")
            return

        coll = bc.texture_list(owner)

        if not (0 <= self.texture_index < len(coll)):
            self.layout.label(text="Texture not found", icon="ERROR")
            return

        _draw_texture_details(self.layout, owner, self.texture_index,
                              boxed=False)

    def execute(self, context):
        return {"FINISHED"}


class BMD_OT_material_texture_menu(Operator):
    bl_idname = "bmd.material_texture_menu"
    bl_label = "Select Material Texture"
    bl_description = "Choose a texture from the BMD texture list"
    bl_options = {"INTERNAL"}

    owner_name: StringProperty(name="Owner", default="")
    slot: IntProperty(name="Slot", default=-1)
    add: BoolProperty(name="Add", default=False)

    def invoke(self, context, event):
        mat = _active_material(context)
        owner = (bpy.data.objects.get(self.owner_name)
                 if self.owner_name else _texture_owner(context))
        if mat is None or owner is None:
            return {"CANCELLED"}
        bc.ensure_texture_list(owner)
        items = _texture_items(owner)
        if not items:
            self.report({"INFO"}, "No textures assigned in Textures")
            return {"CANCELLED"}
        slot = self.slot
        add = self.add

        def draw(menu, context):
            for index, texture_slot, image in items:
                label = f"{index}: {texture_slot.name or image.name}"
                op_id = (BMD_OT_material_add_texture.bl_idname if add
                         else BMD_OT_material_set_texture.bl_idname)
                icon_value = _preview_icon(image)
                if icon_value:
                    op = menu.layout.operator(op_id, text=label,
                                              icon_value=icon_value)
                else:
                    op = menu.layout.operator(op_id, text=label, icon="IMAGE_DATA")
                op.owner_name = owner.name
                op.texture = str(index)
                if not add:
                    op.slot = slot

        context.window_manager.popup_menu(draw, title="Texture", icon="IMAGE_DATA")
        return {"FINISHED"}


class BMD_UL_textures(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, *args):
        row = layout.row(align=True)
        # Empty slots (e.g. textures an imported shader expects) need an image.
        row.alert = item.image is None
        index_col = row.column()
        index_col.ui_units_x = 2.0
        index_col.label(text=f"{index:02d}")
        if item.image is None and item.name:
            name = f"{item.name} (no image)"
        else:
            name = item.name or (item.image.name if item.image is not None else "(no image)")
        row.label(text=name)


class BMD_OT_texture_add(Operator):
    bl_idname = "bmd.texture_add"
    bl_label = "Add BMD Texture"
    bl_description = "Add an empty texture slot to the exported BMD texture list"
    bl_options = {"UNDO"}

    def execute(self, context):
        arm = _texture_owner(context)
        if arm is None:
            self.report({"ERROR"}, "Select the imported armature first")
            return {"CANCELLED"}
        coll = bc.ensure_texture_list(arm)
        slot = coll.add()
        # The BMD name belongs to the assigned texture: an empty slot has none.
        slot.name = ""
        arm.bmd_texture_index = len(coll) - 1
        bc.sync_texture_legacy(arm)
        return {"FINISHED"}


class BMD_OT_texture_remove(Operator):
    bl_idname = "bmd.texture_remove"
    bl_label = "Remove BMD Texture"
    bl_description = "Remove the selected texture from the BMD texture list"
    bl_options = {"UNDO"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        arm = _texture_owner(context)
        if arm is None:
            return {"CANCELLED"}
        coll = bc.ensure_texture_list(arm)
        index = self.index if self.index >= 0 else int(arm.bmd_texture_index)
        if not (0 <= index < len(coll)):
            return {"CANCELLED"}
        key = bc.texture_slot_key(coll[index])
        users = bc.material_slot_users(arm, index) if key else 0
        if key:
            bc.retarget_material_textures(key, "", None, index, arm, drop_slot=True)
        coll.remove(index)
        bc.remove_material_texture_slot(arm, index)
        arm.bmd_texture_index = min(index, max(0, len(coll) - 1))
        bc.sync_texture_legacy(arm)
        if users:
            self.report({"WARNING"},
                        f"Removed a texture that was used by {users} material slot(s)")
        return {"FINISHED"}


class BMD_OT_texture_clear_image(Operator):
    bl_idname = "bmd.texture_clear_image"
    bl_label = "Clear BMD Texture Image"
    bl_description = "Remove the image from the selected BMD texture slot"
    bl_options = {"UNDO"}

    def execute(self, context):
        arm = _texture_owner(context)
        if arm is None:
            return {"CANCELLED"}
        coll = bc.ensure_texture_list(arm)
        index = int(arm.bmd_texture_index)
        if not (0 <= index < len(coll)):
            return {"CANCELLED"}
        coll[index].image = None
        return {"FINISHED"}


class BMD_OT_texture_move(Operator):
    bl_idname = "bmd.texture_move"
    bl_label = "Move BMD Texture"
    bl_description = "Move the texture up or down in the exported texture order"
    bl_options = {"UNDO"}

    index: IntProperty(name="Index", default=-1)
    direction: EnumProperty(name="Direction",
                            items=(("UP", "Up", ""), ("DOWN", "Down", "")))

    def execute(self, context):
        arm = _texture_owner(context)
        if arm is None:
            return {"CANCELLED"}
        coll = bc.ensure_texture_list(arm)
        index = self.index if self.index >= 0 else int(arm.bmd_texture_index)
        target = index - 1 if self.direction == "UP" else index + 1
        if not (0 <= index < len(coll) and 0 <= target < len(coll)):
            return {"CANCELLED"}
        order = list(range(len(coll)))
        order.pop(index)
        order.insert(target, index)
        mapping = {old: new for new, old in enumerate(order)}
        coll.move(index, target)
        bc.remap_material_texture_slots(arm, mapping)
        arm.bmd_texture_index = target
        bc.sync_texture_legacy(arm)
        return {"FINISHED"}


class BMD_PT_formats(Panel):
    bl_label = "Vertex Formats"
    bl_order = 2
    bl_options = {"DEFAULT_CLOSED"}
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "J3D"

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        layout = self.layout
        layout.use_property_decorate = False
        owner = _selected_model(context)
        if owner is None:
            layout.label(text="Select a BMD model", icon="INFO")
            return
        formats = bc.ensure_vertex_formats(owner, read_only=True)
        if formats is None:
            layout.label(text="Select a BMD model", icon="INFO")
            return
        _draw_bmd_prop(layout, formats, "position_format", "Positions", 0.3)
        if formats.position_format == "S16":
            _draw_bmd_prop(layout, formats, "position_frac", "Fraction", 0.3)
            layout.label(text="16-bit positions: BLS blend shapes will not deform",
                         icon="ERROR")
        _draw_bmd_prop(layout, formats, "normal_format", "Normals", 0.3)
        if formats.normal_format == "S16":
            _draw_bmd_prop(layout, formats, "normal_frac", "Fraction", 0.3)
        _draw_bmd_prop(layout, formats, "uv_format", "UVs", 0.3)
        if formats.uv_format == "S16":
            _draw_bmd_prop(layout, formats, "uv_frac", "Fraction", 0.3)


def _draw_texture_details(layout, owner, index, boxed=True):
    """Preview and parameters of one texture, shared by the panel and popup."""
    coll = bc.texture_list(owner)

    if not coll:
        layout.label(text="No textures in this model", icon="INFO")
        return

    index = min(max(int(index), 0), len(coll) - 1)
    slot = coll[index]
    target = layout.box() if boxed else layout
    target.label(text=f"Texture {index}", icon="TEXTURE")

    if slot.image is None:
        target.label(text="No image (skipped on export)", icon="ERROR")
    else:
        _draw_bmd_prop(target, slot, "name", "BMD Name", 0.3)
        image_settings = slot.image.bmd
        _draw_bmd_prop(target, image_settings, "format", "GX Format", 0.3)
        _draw_bmd_prop(target, image_settings, "mip_count", "Mip Levels", 0.3)
        _draw_bmd_prop(target, image_settings, "external", "External Texture", 0.3)
    image_col = target.column()

    if slot.image is not None:
        try:
            slot.image.preview_ensure()
        except Exception:
            pass

    image_col.template_ID_preview(
        slot, "image", new="image.new", open="image.open",
        unlink=BMD_OT_texture_clear_image.bl_idname)

    target.label(
        text=f"Used by {bc.material_slot_users(owner, index)} "
             "material slot(s)",
        icon="MATERIAL")

    settings = target.box()
    settings.label(text="Sample Settings", icon="PREFERENCES")
    _draw_slot_sampler(settings, slot)


class BMD_PT_textures(Panel):
    bl_label = "Textures"
    bl_order = 3
    bl_options = {"DEFAULT_CLOSED"}
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "J3D"

    @classmethod
    def poll(cls, context):
        return True

    def draw(self, context):
        layout = self.layout
        owner = _selected_model(context)
        if owner is None:
            layout.label(text="Select a BMD model", icon="INFO")
            row = layout.row(align=True)
            row.operator("import_image.bti", text="Import Textures",
                         icon="IMPORT")
            row.operator("export_scene.bti", text="Export Textures",
                         icon="EXPORT")
            return
        coll = bc.texture_list(owner)
        row = layout.row()
        row.template_list("BMD_UL_textures", "", owner, "bmd_texture_list",
                          owner, "bmd_texture_index", rows=8)
        column = row.column(align=True)
        column.operator(BMD_OT_texture_add.bl_idname, text="", icon="ADD")
        column.operator(BMD_OT_texture_remove.bl_idname, text="", icon="REMOVE")
        column.separator()
        up = column.operator(BMD_OT_texture_move.bl_idname, text="",
                             icon="TRIA_UP")
        up.direction = "UP"
        down = column.operator(BMD_OT_texture_move.bl_idname, text="",
                               icon="TRIA_DOWN")
        down.direction = "DOWN"
        column.separator()
        column.operator("import_image.bti", text="", icon="IMPORT")
        column.operator("export_scene.bti", text="", icon="EXPORT")
        if not len(coll):
            layout.label(text="No textures in this model", icon="INFO")
            return
        index = min(max(int(owner.bmd_texture_index), 0), len(coll) - 1)
        _draw_texture_details(layout, owner, index)


def menu_import(self, context):
    self.layout.operator("import_scene.bmd", text="J3D Model (.bmd/.bdl)")
    self.layout.operator("import_image.bti", text="J3D Textures (.bti)")
    self.layout.operator("import_anim.j3d", text="J3D Animations (.bck/.blk/.btp)")
    self.layout.operator("import_scene.kcl", text="TP Collision (.kcl)")
    self.layout.operator("bmd_shape_keys.import_files", text="J3D Shape Keys (.bls)")
    self.layout.operator("bmd_shape_keys.import_folder", text="J3D Shape Keys from Folder (.bls)")


def menu_image_import_bti(self, context):
    self.layout.operator("import_image.bti", text="Import J3D Texture (.bti)")


def menu_export(self, context):
    self.layout.operator("export_scene.bmd", text="J3D Model (.bmd)")
    self.layout.operator("export_scene.bti", text="J3D Textures (.bti)")
    self.layout.operator("export_anim.j3d", text="J3D Animations (.bck/.blk/.btp)")
    self.layout.operator("export_scene.kcl", text="TP Collision (.kcl)")
    self.layout.operator("bmd_shape_keys.export_files", text="J3D Shape Keys (.bls)")


CREATE_MODE_ITEMS = tuple(item for item in bc.RENDER_ITEMS
                           if item[0] != "AUTO")


def _run_material_create(operator, context, mode):
    from . import blender_mat
    material = (bpy.data.materials.get(operator.material_name)
                if operator.material_name else context.material)
    owner = (bpy.data.objects.get(operator.owner_name)
             if operator.owner_name else _texture_owner(context))
    if material is None or owner is None:
        operator.report({"ERROR"}, "Select a BMD model first")
        return {"CANCELLED"}
    blender_mat.create_material(material, owner, mode)
    return {"FINISHED"}


def _make_create_operator(mode, label, description):
    """One operator per render mode so the menu item gets its own tooltip."""
    class_name = f"BMD_OT_material_create_{mode.lower()}"

    class _CreateOperator(Operator):
        bl_idname = f"bmd.material_create_{mode.lower()}"
        bl_label = label
        bl_description = description
        bl_options = {"REGISTER", "UNDO"}

        material_name: StringProperty(options={"HIDDEN"})
        owner_name: StringProperty(options={"HIDDEN"})

        def execute(self, context):
            return _run_material_create(self, context, mode)

    _CreateOperator.__name__ = class_name
    _CreateOperator.__qualname__ = class_name
    return _CreateOperator


CREATE_OPERATORS = tuple(_make_create_operator(*item)
                         for item in CREATE_MODE_ITEMS)


class BMD_OT_material_create(Operator):
    bl_idname = "bmd.material_create"
    bl_label = "Create BMD Material"
    bl_description = ("Create the BMD preview nodes for this material using "
                      "the selected render mode")
    # The operator edits the node tree and material properties; its own undo
    # step keeps Ctrl+Z from reverting the texture that was just added (which
    # would free the image while the material preview is still rendering it).
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(name="Render Mode", items=CREATE_MODE_ITEMS,
                       default="OPAQUE")
    material_name: StringProperty(options={"HIDDEN"})
    owner_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        return _run_material_create(self, context, self.mode)


def _shader_read_entries(path):
    """Load a materials.json file as a list of material objects."""
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        raise ValueError("no materials in the file")
    return [entry for entry in payload if isinstance(entry, dict)]


class BMD_OT_material_shader_import(Operator, ImportHelper):
    """Import a shader preset into this material"""

    bl_idname = "bmd.material_shader_import"
    bl_label = "Import Shader"
    bl_description = (
        "Apply a shader preset to this material.")
    # The operator rewrites the material's node tree and its MAT3 data; its own
    # undo step keeps Ctrl+Z from restoring a half-rebuilt tree.
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    material_name: StringProperty(options={"HIDDEN"})
    owner_name: StringProperty(options={"HIDDEN"})
    index: IntProperty(default=-1, options={"HIDDEN"})

    def invoke(self, context, event):
        material = (bpy.data.materials.get(self.material_name)
                    if self.material_name else context.material)
        if material is None:
            self.report({"ERROR"}, "Select a material")
            return {"CANCELLED"}
        self.material_name = material.name
        if not self.owner_name:
            owner = _texture_owner(context)
            self.owner_name = owner.name if owner is not None else ""
        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        try:
            entries = _shader_read_entries(self.filepath)
        except Exception as exc:
            self.report({"ERROR"}, f"Cannot read {os.path.basename(self.filepath)}: {exc}")
            return {"CANCELLED"}
        index = self.index
        if not (0 <= index < len(entries)):
            index = self._match_index(entries)
        if index is None:
            self._pick(entries)
            return {"FINISHED"}
        return self._apply(context, entries[index])

    def _match_index(self, entries):
        """Index of the entry named like this material, when there is one."""
        material = bpy.data.materials.get(self.material_name)
        if material is None:
            return 0 if len(entries) == 1 else None
        wanted = bc.material_shader_name(material)
        for index, entry in enumerate(entries):
            if str(entry.get("Name") or "") == wanted:
                return index
        if len(entries) == 1:
            return 0
        return None

    def _pick(self, entries):
        # The popup outlives this operator, so nothing here may reference
        # ``self`` (the operator instance is freed as soon as execute returns).
        names = [str(entry.get("Name") or f"material{i}")
                 for i, entry in enumerate(entries)]
        filepath = str(self.filepath)
        material_name = str(self.material_name)
        owner_name = str(self.owner_name)

        def draw(menu, _context):
            for index, name in enumerate(names):
                item = menu.layout.operator(BMD_OT_material_shader_apply.bl_idname,
                                            text=name)
                item.filepath = filepath
                item.index = index
                item.material_name = material_name
                item.owner_name = owner_name

        window_manager = bpy.context.window_manager
        if window_manager is None:
            self.report({"ERROR"},
                        "Several materials in the file; export one material per "
                        "file or name one of them like the target material")
            return
        try:
            window_manager.popup_menu(draw, title="Import Shader")
        except Exception as exc:
            self.report({"ERROR"}, f"Cannot show the material list: {exc}")

    def _apply(self, context, preset):
        return BMD_OT_material_shader_apply.apply_preset(
            self, context, preset, self.material_name, self.owner_name)


class BMD_OT_material_shader_apply(Operator):
    """Apply one material object from a shader preset file"""

    bl_idname = "bmd.material_shader_apply"
    bl_label = "Apply Shader"
    bl_description = "Apply the selected material from the shader preset file"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(options={"HIDDEN"})
    index: IntProperty(default=0, options={"HIDDEN"})
    material_name: StringProperty(options={"HIDDEN"})
    owner_name: StringProperty(options={"HIDDEN"})

    @staticmethod
    def apply_preset(operator, context, preset, material_name, owner_name):
        from . import blender_mat

        material = (bpy.data.materials.get(material_name)
                    if material_name else context.material)
        if material is None:
            operator.report({"ERROR"}, "Select a material")
            return {"CANCELLED"}
        owner = (bpy.data.objects.get(owner_name)
                 if owner_name else _texture_owner(context))
        try:
            name, created, empty = blender_mat.apply_shader_preset(material, owner, preset)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            operator.report({"ERROR"}, f"Cannot import shader: {exc}")
            return {"CANCELLED"}
        notes = []
        if created:
            notes.append(f"{len(created)} empty texture slot(s) added: "
                         + ", ".join(created[:8]))
        if empty:
            notes.append("still without a texture: " + ", ".join(empty[:8]))
        message = f"Imported shader {name or '(unnamed)'} into {material.name}"
        if notes:
            message += " (" + "; ".join(notes) + ")"
        operator.report({"INFO"}, message)
        return {"FINISHED"}

    def execute(self, context):
        try:
            entries = _shader_read_entries(self.filepath)
        except Exception as exc:
            self.report({"ERROR"}, f"Cannot read {os.path.basename(self.filepath)}: {exc}")
            return {"CANCELLED"}
        index = self.index if 0 <= self.index < len(entries) else 0
        return self.apply_preset(self, context, entries[index],
                                 self.material_name, self.owner_name)


def _shader_export_path(current, material):
    """Default export path: the current material name, previous folder."""
    directory = os.path.dirname(current or "")
    name = bc.material_shader_name(material).strip() or "material"
    # A datablock name may contain characters Windows cannot put in a file
    # name; the JSON "Name" keeps the exact material name.
    for char in '<>:"/\\|?*':
        name = name.replace(char, "_")
    name = name.rstrip(" .") or "material"
    name += ".json"
    return os.path.join(directory, name) if directory else name


class BMD_OT_material_shader_export(Operator, ExportHelper):
    """Export this material's shader as a json preset"""

    bl_idname = "bmd.material_shader_export"
    bl_label = "Export Shader"
    bl_description = (
        "Save the material's MAT3 data as a json preset, so "
        "it can be applied to another model or material. "
        "Textures are referenced by name")

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    material_name: StringProperty(options={"HIDDEN"})
    owner_name: StringProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        material = (bpy.data.materials.get(self.material_name)
                    if self.material_name else context.material)
        if material is None or not (material.get("bmd_material")
                                    or material.get("bmd_shader_active")):
            self.report({"ERROR"}, "Nothing to export: no imported shader")
            return {"CANCELLED"}
        self.material_name = material.name
        if not self.owner_name:
            owner = _texture_owner(context)
            self.owner_name = owner.name if owner is not None else ""
        # Always name the file after the material: Blender keeps the path of the
        # previous export in the operator properties, which must not leak into
        # the next (different) material's file name.
        self.filepath = _shader_export_path(self.filepath, material)
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        material = (bpy.data.materials.get(self.material_name)
                    if self.material_name else context.material)
        if material is None or not (material.get("bmd_material")
                                    or material.get("bmd_shader_active")):
            self.report({"ERROR"}, "Nothing to export: no imported shader")
            return {"CANCELLED"}
        owner = (bpy.data.objects.get(self.owner_name)
                 if self.owner_name else _texture_owner(context))
        payload = [bc.material_preset_payload(material, owner)]
        path = bpy.path.ensure_ext(self.filepath, self.filename_ext)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
        except OSError as exc:
            self.report({"ERROR"}, f"Cannot write {path}: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"},
                    f"Wrote shader preset {os.path.basename(path)}")
        return {"FINISHED"}


class BMD_OT_material_shader_reset(Operator):
    """Drop the imported shader and rebuild the material from its own data"""

    bl_idname = "bmd.material_shader_reset"
    bl_label = "Reset Shader"
    bl_description = ("Remove the imported shader and restore the "
                      "original MAT3 data")
    bl_options = {"REGISTER", "UNDO"}

    material_name: StringProperty(options={"HIDDEN"})
    owner_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        from . import blender_mat

        material = (bpy.data.materials.get(self.material_name)
                    if self.material_name else context.material)
        if material is None:
            self.report({"ERROR"}, "Select a material")
            return {"CANCELLED"}
        owner = (bpy.data.objects.get(self.owner_name)
                 if self.owner_name else _texture_owner(context))
        if not blender_mat.reset_shader(material, owner):
            self.report({"ERROR"}, "No imported shader to reset")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Restored the original shader of {material.name}")
        return {"FINISHED"}


class BMD_OT_material_create_menu(Operator):
    bl_idname = "bmd.material_create_menu"
    bl_label = "Create BMD Material"
    bl_description = "Create the BMD preview nodes for this material"
    bl_options = {"INTERNAL"}

    def invoke(self, context, event):
        material = context.material
        owner = _texture_owner(context)
        if material is None or owner is None:
            self.report({"ERROR"}, "Select a BMD model first")
            return {"CANCELLED"}

        def draw(menu, _context):
            for operator, (_mode, label, _description) in zip(
                    CREATE_OPERATORS, CREATE_MODE_ITEMS):
                item = menu.layout.operator(operator.bl_idname, text=label)
                item.material_name = material.name
                item.owner_name = owner.name

        context.window_manager.popup_menu(
            draw, title="Create BMD Material", icon="MATERIAL")
        return {"FINISHED"}


def _run_material_track_add(operator, context, track_type):
    from . import btp_preview
    material = (bpy.data.materials.get(operator.material_name)
                if operator.material_name else context.material)
    if material is None:
        return {"CANCELLED"}
    bc.ensure_material_track(material, track_type)
    owner = (bpy.data.objects.get(operator.owner_name)
             if operator.owner_name else _texture_owner(context))
    if track_type == "BTP" and owner is not None:
        # Build the BTP texture mapping as soon as the field is added.
        btp_preview.prepare_preview(material, owner)
    return {"FINISHED"}


def _make_track_operator(track_type, label, description):
    """One operator per track type so the menu item gets its own tooltip."""
    class_name = f"BMD_OT_material_track_add_{track_type.lower()}"

    class _AddOperator(Operator):
        bl_idname = f"bmd.material_track_add_{track_type.lower()}"
        bl_label = label
        bl_description = description
        bl_options = {"REGISTER", "UNDO"}

        material_name: StringProperty(options={"HIDDEN"})
        owner_name: StringProperty(options={"HIDDEN"})

        def execute(self, context):
            return _run_material_track_add(self, context, track_type)

    _AddOperator.__name__ = class_name
    _AddOperator.__qualname__ = class_name
    return _AddOperator


TRACK_ADD_OPERATORS = tuple(_make_track_operator(*item)
                            for item in bc.MATERIAL_TRACK_ITEMS)


class BMD_OT_material_track_add(Operator):
    bl_idname = "bmd.material_track_add"
    bl_label = "Add Material Track"
    bl_description = "Add an animatable field to Material Animation"
    bl_options = {"REGISTER", "UNDO"}

    track_type: EnumProperty(name="Track", items=bc.MATERIAL_TRACK_ITEMS,
                             default="BTP")
    material_name: StringProperty(options={"HIDDEN"})
    owner_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        return _run_material_track_add(self, context, self.track_type)


class BMD_OT_material_track_menu(Operator):
    bl_idname = "bmd.material_track_menu"
    bl_label = "Add Material Track"
    bl_description = "Add an animatable field to the Material Animation list"
    bl_options = {"INTERNAL"}

    material_name: StringProperty(options={"HIDDEN"})
    owner_name: StringProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        material = (bpy.data.materials.get(self.material_name)
                    if self.material_name else context.material)
        if material is None:
            return {"CANCELLED"}
        existing = set(bc.material_track_types(material))
        items = [(operator, item) for operator, item
                 in zip(TRACK_ADD_OPERATORS, bc.MATERIAL_TRACK_ITEMS)
                 if item[0] not in existing]
        if not items:
            self.report({"INFO"}, "All material tracks are already added")
            return {"CANCELLED"}
        owner = (bpy.data.objects.get(self.owner_name)
                 if self.owner_name else _texture_owner(context))

        def draw(menu, _context):
            for operator, (_track_type, label, _description) in items:
                item = menu.layout.operator(operator.bl_idname, text=label)
                item.material_name = material.name
                item.owner_name = owner.name if owner is not None else ""

        context.window_manager.popup_menu(draw, title="Add Track", icon="ADD")
        return {"FINISHED"}


class BMD_OT_material_track_refresh(Operator):
    bl_idname = "bmd.material_track_refresh"
    bl_label = "Rebuild Material Mapping"
    bl_description = ("Rebuild this field's BMD mapping from the model's "
                      "texture list and material nodes")
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=-1)
    material_name: StringProperty(options={"HIDDEN"})
    owner_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        from . import btp_preview
        material = (bpy.data.materials.get(self.material_name)
                    if self.material_name else context.material)
        owner = (bpy.data.objects.get(self.owner_name)
                 if self.owner_name else _texture_owner(context))
        if material is None:
            return {"CANCELLED"}
        tracks = material.bmd.tracks
        track_type = "BTP"
        if 0 <= self.index < len(tracks):
            track_type = tracks[self.index].type
        if track_type == "BTP" and owner is not None:
            btp_preview.prepare_preview(material, owner)
        return {"FINISHED"}


class BMD_OT_material_track_remove(Operator):
    bl_idname = "bmd.material_track_remove"
    bl_label = "Remove Material Track"
    bl_description = "Remove this field from the Material Animation list"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=-1)
    material_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        material = (bpy.data.materials.get(self.material_name)
                    if self.material_name else context.material)
        if material is None:
            return {"CANCELLED"}
        tracks = material.bmd.tracks
        if 0 <= self.index < len(tracks):
            tracks.remove(self.index)
        return {"FINISHED"}


class BMD_OT_btp_insert_key(Operator):
    bl_idname = "bmd.btp_insert_key"
    bl_label = "Insert BTP Texture Key"
    bl_description = ("Insert the current Texture Index into the active "
                      "animation at the current frame")
    bl_options = {"UNDO"}

    material_name: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        from . import animation_registry, btp_preview
        material = (bpy.data.materials.get(self.material_name)
                    if self.material_name else context.material)
        owner = _texture_owner(context)
        if owner is None and context.active_object is not None:
            owner = animation_registry.owner_for(context.active_object)
        if material is None:
            self.report({"ERROR"}, "No active material")
            return {"CANCELLED"}

        try:
            value = int(material.bmd.btp_texture_index)
        except Exception:
            value = 0

        if owner is not None:
            btp_preview.prepare_preview(material, owner)
        animation_data = material.animation_data
        if animation_data is not None and animation_data.action is not None:
            btp_preview.migrate_action(animation_data.action)

        btp_preview.set_field(material, value)
        material.keyframe_insert(data_path="bmd.btp_texture_index",
                                 frame=context.scene.frame_current)
        animation_data = material.animation_data
        if animation_data is not None:
            btp_preview.set_step_interpolation(animation_data.action)

        if owner is not None:
            registry = animation_registry.registry_for(owner)
            if registry is not None:
                entry = animation_registry._active_entry(registry)
                if entry is None:
                    entry = animation_registry.ensure_default_animation(
                        context, owner, select=False)
                if entry is not None:
                    animation_registry._attach_active_actions(
                        entry, owner, registry)
                animation_registry.sort_animations(registry)
        return {"FINISHED"}


CLASSES = (
    MATERIAL_PT_bmd,
    IMAGE_PT_bmd,
    BMD_PT_j3d,
    BMD_UL_textures,
    BMD_OT_texture_add,
    BMD_OT_texture_remove,
    BMD_OT_texture_clear_image,
    BMD_OT_texture_move,
    BMD_OT_material_set_texture,
    BMD_OT_material_add_texture,
    BMD_OT_material_remove_texture,
    BMD_OT_material_texture_menu,
    BMD_OT_texture_focus,
    BMD_OT_initialize_bmd,
    BMD_OT_initialize_kcl,
    BMD_OT_set_30_fps,
    BMD_OT_material_create,
    BMD_OT_material_create_menu,
    BMD_OT_material_shader_import,
    BMD_OT_material_shader_apply,
    BMD_OT_material_shader_export,
    BMD_OT_material_shader_reset,
    *CREATE_OPERATORS,
    BMD_OT_material_track_menu,
    BMD_OT_material_track_add,
    *TRACK_ADD_OPERATORS,
    BMD_OT_material_track_refresh,
    BMD_OT_material_track_remove,
    BMD_OT_btp_insert_key,
    BMD_PT_utility,
    BMD_PT_formats,
    BMD_PT_textures,
    BMD_PT_animations,
)


def _register_child_ui():
    from . import (animation_registry, anim_export_ui, anim_import_ui,
                   bti_export_ui, bti_import_ui,
                   bls_import_ui, bls_export_ui)

    for c in animation_registry.CLASSES:
        bpy.utils.register_class(c)
    animation_registry.register_properties()
    for c in anim_import_ui.CLASSES:
        bpy.utils.register_class(c)
    anim_import_ui.register_properties()
    for c in anim_export_ui.CLASSES:
        bpy.utils.register_class(c)
    anim_export_ui.register_properties()
    for c in bti_import_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in bti_export_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in bls_import_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in bls_export_ui.CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.bmd_shape_keys_settings = PointerProperty(
        type=bls_import_ui.BMD_SK_Settings)
    bpy.types.Scene.bmd_shape_keys_export_settings = PointerProperty(
        type=bls_export_ui.BMD_SK_ExportSettings)


def _unregister_child_ui():
    from . import (animation_registry, anim_export_ui, anim_import_ui,
                   bti_export_ui, bti_import_ui,
                   bls_import_ui, bls_export_ui)

    animation_registry.unregister_properties()
    anim_import_ui.unregister_properties()
    anim_export_ui.unregister_properties()
    for c in reversed(anim_export_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(anim_import_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(bti_export_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(bti_import_ui.CLASSES):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.Scene, "bmd_shape_keys_export_settings"):
        del bpy.types.Scene.bmd_shape_keys_export_settings
    if hasattr(bpy.types.Scene, "bmd_shape_keys_settings"):
        del bpy.types.Scene.bmd_shape_keys_settings
    for c in reversed(bls_export_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(bls_import_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(animation_registry.CLASSES):
        bpy.utils.unregister_class(c)


def _tag_redraw():
    screen = getattr(bpy.context, "screen", None)

    if screen is None:
        return

    for area in screen.areas:
        try:
            area.tag_redraw()
        except Exception:
            pass


def register():
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception:
            import traceback
            traceback.print_exc()

    for owner, menu in (
            (bpy.types.TOPBAR_MT_file_import, menu_import),
            (bpy.types.TOPBAR_MT_file_export, menu_export),
            (bpy.types.IMAGE_MT_image, menu_image_import_bti),
            (bpy.types.NODE_MT_context_menu, menu_node_import_bti)):
        try:
            owner.append(menu)
        except Exception:
            pass

    try:
        _register_child_ui()
    except Exception:
        import traceback
        traceback.print_exc()

    _tag_redraw()


def unregister():
    for owner, menu in (
            (bpy.types.NODE_MT_context_menu, menu_node_import_bti),
            (bpy.types.IMAGE_MT_image, menu_image_import_bti),
            (bpy.types.TOPBAR_MT_file_export, menu_export),
            (bpy.types.TOPBAR_MT_file_import, menu_import)):
        try:
            owner.remove(menu)
        except Exception:
            pass

    try:
        _unregister_child_ui()
    except Exception:
        import traceback
        traceback.print_exc()

    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

    _tag_redraw()
