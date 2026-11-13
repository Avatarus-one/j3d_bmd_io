"""J3D BMD Import/Export for Blender (Zelda: Twilight Princess / Dusklight)."""

import json
import os

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
                       StringProperty)
from bpy.types import Operator, OperatorFileListElement, Panel, PropertyGroup
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import blender_common as bc

_PENDING_BMD_IMPORT = None


def blender_anim_items():
    from .blender_anim import LOOP_ITEMS
    return LOOP_ITEMS


def _target_armature(obj):
    if obj is None:
        return None
    if obj.parent is not None and obj.parent.type == "ARMATURE":
        return obj.parent
    for mod in obj.modifiers:
        if mod.type == "ARMATURE" and mod.object is not None:
            return mod.object
    return None


def _decode_vtx_formats(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        return {int(attr): tuple(fmt) for attr, fmt in value.items()}
    except (TypeError, ValueError, AttributeError):
        return {}


def _detect_vertex_formats(context):
    """Find formats of the last successful export, then of the imported BMD."""
    preferred = []
    active = context.active_object
    if active is not None:
        preferred.append(active)
    for obj in context.selected_objects:
        if obj not in preferred:
            preferred.append(obj)

    # 1. Formats remembered from a previous successful export.
    for obj in preferred:
        if obj.type == "MESH":
            raw = obj.get("bmd_export_formats")
            if raw:
                formats = _decode_vtx_formats(raw)
                if formats:
                    return formats

    # 2. Formats of the imported BMD (stored on the armature).
    for obj in preferred:
        if obj.type == "ARMATURE":
            raw = obj.get("bmd_vtx_formats")
            if raw:
                formats = _decode_vtx_formats(raw)
                if formats:
                    return formats

    # 3. Imported BMD formats through the armature of a selected mesh.
    for obj in preferred:
        if obj.type == "MESH":
            armature = _target_armature(obj)
            if armature is not None:
                raw = armature.get("bmd_vtx_formats")
                if raw:
                    formats = _decode_vtx_formats(raw)
                    if formats:
                        return formats

    # 4. Nothing selected: use the scene's single remembered export, or its
    #    single imported model.  This covers file-menu invocation with a lost
    #    selection.
    meshes = [
        obj for obj in context.scene.objects
        if obj.type == "MESH" and obj.get("bmd_export_formats")]
    if len(meshes) == 1:
        formats = _decode_vtx_formats(meshes[0].get("bmd_export_formats"))
        if formats:
            return formats

    armatures = [
        obj for obj in context.scene.objects
        if obj.type == "ARMATURE" and obj.get("bmd_vtx_formats")]
    if len(armatures) == 1:
        formats = _decode_vtx_formats(armatures[0].get("bmd_vtx_formats"))
        if formats:
            return formats

    return {}


def _format_choice(fmt, default_frac):
    if not fmt:
        return "F32", default_frac
    _components, typ, frac = fmt
    if typ == 3:
        return "S16", int(frac)
    return "F32", default_frac


class IMPORT_OT_bmd(Operator, ImportHelper):
    """Import a J3D BMD3/BDL4 model"""
    bl_idname = "import_scene.bmd"
    bl_label = "Import J3D BMD"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".bmd"
    filter_glob: StringProperty(default="*.bmd;*.bdl", options={"HIDDEN"})
    files: CollectionProperty(type=OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})

    convert_units: BoolProperty(
        name="Convert Units", default=True,
        description="Convert J3D model units to Blender units (scale 0.01). ")
    bone_axis: EnumProperty(name="Bone Orientation", items=bc.BONE_AXIS_ITEMS, default="X")
    import_textures: BoolProperty(name="Import Textures", default=True)
    single_mesh: BoolProperty(
        name="Import as Single Mesh", default=True,
        description="Import all shapes into one mesh; vertices keep the original BMD VTX1 order")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "bone_axis")
        layout.prop(self, "import_textures")
        layout.prop(self, "single_mesh")

    def execute(self, context):
        from . import blender_import
        from . import external_textures
        from .j3d import model as j3d
        from .j3d.common import J3DError
        global _PENDING_BMD_IMPORT

        scale = 0.01 if self.convert_units else 1.0
        paths = []
        if self.files:
            paths = [os.path.join(self.directory, f.name) for f in self.files]
        if not paths:
            paths = [self.filepath]

        if self.import_textures:
            missing = []
            suggested = None
            files = []
            for p in paths:
                try:
                    with open(p, "rb") as handle:
                        bmd = j3d.parse(handle.read())
                    names, folder = external_textures.check_external_textures(bmd, p)
                except (OSError, J3DError) as exc:
                    self.report({"ERROR"}, f"{os.path.basename(p)}: {exc}")
                    return {"CANCELLED"}

                external_names = list(dict.fromkeys(
                    t.name for t in bmd.textures if t.external))
                files.append({
                    "path": p,
                    "external": external_names,
                    "missing": names,
                })
                for name in names:
                    if name not in missing:
                        missing.append(name)
                if names and suggested is None and folder is not None:
                    suggested = folder

            if missing and not bpy.app.background:
                _PENDING_BMD_IMPORT = {
                    "paths": paths,
                    "scale": scale,
                    "bone_axis": self.bone_axis,
                    "import_textures": self.import_textures,
                    "single_mesh": self.single_mesh,
                    "files": files,
                    "missing": missing,
                    "suggested": suggested,
                }
                try:
                    bpy.ops.import_scene.bmd_texc("INVOKE_DEFAULT")
                except RuntimeError as exc:
                    # The dialog could not be shown; keep going with placeholders.
                    consumed = _PENDING_BMD_IMPORT is None
                    _PENDING_BMD_IMPORT = None
                    if consumed:
                        return {"FINISHED"}
                    self.report({"WARNING"},
                                "Import Settings could not be shown; missing external "
                                f"textures will use placeholders: {exc}")
                else:
                    return {"FINISHED"}

        for p in paths:
            try:
                blender_import.import_bmd(context, p, scale, self.bone_axis,
                                          self.import_textures, report=self.report,
                                          single_mesh=self.single_mesh)
            except J3DError as exc:
                self.report({"ERROR"}, f"{os.path.basename(p)}: {exc}")
                return {"CANCELLED"}
        return {"FINISHED"}


class BMDImportFileState(PropertyGroup):
    expanded: BoolProperty(default=True)


class IMPORT_OT_bmd_texc(Operator):
    bl_idname = "import_scene.bmd_texc"
    bl_label = "Import Settings"
    bl_description = "Choose the folder that contains the external .bti textures"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    texc_dir: StringProperty(
        name="texc Path", subtype="DIR_PATH", default="",
        description="Path to external .bti textures (Normally inside STG_00)")
    file_states: CollectionProperty(type=BMDImportFileState, options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return bool(_PENDING_BMD_IMPORT)

    def invoke(self, context, _event):
        pending = _PENDING_BMD_IMPORT
        self.texc_dir = str(pending["suggested"]) if pending and pending.get("suggested") else ""
        self.file_states.clear()
        if pending:
            files = pending.get("files") or [
                {"path": path} for path in pending.get("paths", ())]
            for _info in files:
                self.file_states.add()
        return context.window_manager.invoke_props_dialog(
            self, width=560, confirm_text="Import")

    def draw(self, context):
        layout = self.layout
        pending = _PENDING_BMD_IMPORT

        if not pending:
            layout.label(text="No pending BMD import", icon="ERROR")
            return

        from . import external_textures

        files = pending.get("files")
        if not files:
            # Compatibility with a pending import created before the tree was added.
            files = [{
                "path": path,
                "external": list(pending.get("missing", ())),
                "missing": list(pending.get("missing", ())),
            } for path in pending.get("paths", ())]

        section = layout.box()
        section.label(text="External Textures", icon="IMAGE_DATA")
        section.label(text="This BMD file references external textures:")

        tree = section.box()
        for index, info in enumerate(files):
            if index:
                tree.separator()

            state = self.file_states[index] if index < len(self.file_states) else None
            expanded = state.expanded if state is not None else True

            header = tree.row(align=True)
            header.alignment = "LEFT"
            if state is not None:
                header.prop(
                    state, "expanded", text=os.path.basename(info["path"]),
                    icon="TRIA_DOWN" if expanded else "TRIA_RIGHT", emboss=False)
            else:
                header.label(
                    text=os.path.basename(info["path"]),
                    icon="TRIA_DOWN" if expanded else "TRIA_RIGHT")
            if not expanded:
                continue

            external = info.get("external") or ()
            if not external:
                row = tree.row(align=True)
                row.label(text="", icon="BLANK1")
                row.label(text="No external textures", icon="INFO")
                continue

            availability = external_textures.texture_availability(
                info["path"], external, self.texc_dir or None)
            for name in external:
                row = tree.row(align=True)
                row.label(text="", icon="BLANK1")
                row.label(text=name, icon="IMAGE_DATA")
                status = row.row()
                status.alignment = "RIGHT"
                if availability.get(name) is not None:
                    status.label(text="OK", icon="CHECKMARK")
                else:
                    status.label(text="missing", icon="ERROR")

        section.separator()
        field = section.column()
        field.use_property_split = True
        field.prop(self, "texc_dir")

    def execute(self, context):
        global _PENDING_BMD_IMPORT
        from . import blender_import
        from .j3d.common import J3DError

        pending = _PENDING_BMD_IMPORT
        if not pending:
            self.report({"ERROR"}, "No pending BMD import")
            return {"CANCELLED"}

        texc_dir = self.texc_dir.strip() or None
        if texc_dir and not os.path.isdir(texc_dir):
            self.report({"WARNING"},
                        f"texc folder not found: {texc_dir}; missing textures will use "
                        "placeholders")
            texc_dir = None

        for path in pending["paths"]:
            try:
                blender_import.import_bmd(
                    context, path, pending["scale"], pending["bone_axis"],
                    pending["import_textures"], report=self.report,
                    single_mesh=pending["single_mesh"], texc_dir=texc_dir)
            except J3DError as exc:
                _PENDING_BMD_IMPORT = None
                self.report({"ERROR"}, f"{os.path.basename(path)}: {exc}")
                return {"CANCELLED"}

        _PENDING_BMD_IMPORT = None
        return {"FINISHED"}

    def cancel(self, context):
        global _PENDING_BMD_IMPORT
        _PENDING_BMD_IMPORT = None


class EXPORT_OT_bmd(Operator, ExportHelper):
    """Export meshes (and their armature) as a J3D BMD3 model"""
    bl_idname = "export_scene.bmd"
    bl_label = "Export J3D BMD"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".bmd"
    filter_glob: StringProperty(default="*.bmd", options={"HIDDEN"})

    convert_units: BoolProperty(
        name="Convert Units", default=True,
        description="Convert Blender units to J3D model units (scale 100). ")
    use_selection: BoolProperty(name="Selected Only", default=True,
                                description="Export only selected objects (select the armature and meshes)")
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=False,
                                  description="Apply modifiers (except armature) before exporting")
    default_format: EnumProperty(name="Texture Format", items=bc.FORMAT_ITEMS, default="AUTO",
                                 description="Format for textures without their own GX Format setting")
    # Plain text field on purpose: a FILE_PATH property adds a browse button, and Blender cannot
    # open a file selector from inside the (already open) export file selector.
    source_file: StringProperty(name="Original BMD", default="",
                                description="Path of the file this model was imported from. Only needed "
                                            "when the path remembered at import no longer exists; used "
                                            "to restore textures that are gone from Blender")
    position_format: EnumProperty(
        name="Positions",
        items=[
            ("F32", "Float32", "Write positions as float32"),
            ("S16", "s16", "Write positions as signed 16-bit"),
        ],
        default="F32")
    position_frac: IntProperty(
        name="Fraction", default=6, min=0, max=15,
        description="Fraction bits for 16-bit positions")
    normal_format: EnumProperty(
        name="Normals",
        items=[
            ("F32", "Float32", "Write normals as float32"),
            ("S16", "s16", "Write normals as signed 16-bit"),
        ],
        default="F32")
    normal_frac: IntProperty(
        name="Fraction", default=14, min=0, max=15,
        description="Fraction bits for 16-bit normals")
    uv_format: EnumProperty(
        name="UVs",
        items=[
            ("F32", "Float32", "Write UVs as float32"),
            ("S16", "s16", "Write UVs as signed 16-bit"),
        ],
        default="F32")
    uv_frac: IntProperty(
        name="Fraction", default=14, min=0, max=15,
        description="Fraction bits for 16-bit UVs")
    limit_weights: BoolProperty(
        name="Limit 4 Weights per Vertex", default=False,
        description="Limit exported vertices to 4 bone influences. Leave off to preserve all "
                    "influences (BMD EVP1 supports up to 255 weights per vertex); more than 4 may "
                    "not work in engines that assume standard 4-bone skinning")
    allow_missing_textures: BoolProperty(name="Allow Missing Textures", default=False,
                                         description="Export even if textures of the imported model "
                                                     "are gone (shifts texture indices!)")

    def _apply_detected_formats(self, context):
        formats = _detect_vertex_formats(context)
        self.position_format, self.position_frac = _format_choice(
            formats.get(9), self.position_frac)
        self.normal_format, self.normal_frac = _format_choice(
            formats.get(10), self.normal_frac)
        uv_attr = next((attr for attr in range(13, 21) if attr in formats), 13)
        self.uv_format, self.uv_frac = _format_choice(
            formats.get(uv_attr), self.uv_frac)

    def invoke(self, context, _event):
        self._apply_detected_formats(context)
        result = ExportHelper.invoke(self, context, _event)
        # File-select operators can restore their last-used properties while
        # opening; apply the detected formats once more before the panel draws.
        self._apply_detected_formats(context)
        return result

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "use_selection")
        layout.prop(self, "apply_modifiers")
        layout.prop(self, "default_format")
        layout.prop(self, "source_file")
        layout.prop(self, "limit_weights")
        layout.prop(self, "allow_missing_textures")

        formats = layout.box()
        formats.label(text="Vertex Formats", icon="MESH_DATA")
        formats.prop(self, "position_format")
        if self.position_format == "S16":
            formats.prop(self, "position_frac")
            formats.label(text="16-bit positions: BLS blend shapes will not deform",
                          icon="ERROR")
        formats.prop(self, "normal_format")
        if self.normal_format == "S16":
            formats.prop(self, "normal_frac")
        formats.prop(self, "uv_format")
        if self.uv_format == "S16":
            formats.prop(self, "uv_frac")

    def execute(self, context):
        from . import blender_export
        from .j3d.common import J3DError
        scale = 100.0 if self.convert_units else 1.0
        try:
            blender_export.export_bmd(
                context, self.filepath, scale, self.use_selection,
                self.apply_modifiers, self.default_format, self.source_file,
                self.allow_missing_textures, True,
                self.report, self.limit_weights, False, False,
                self.position_format, self.normal_format, self.uv_format,
                self.position_frac, self.normal_frac, self.uv_frac)
        except (blender_export.ExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORT_OT_kcl(Operator, ImportHelper):
    """Import a Twilight Princess collision mesh (room.kcl, optionally with room.plc)"""
    bl_idname = "import_scene.kcl"
    bl_label = "Import TP Collision (.kcl)"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".kcl"
    filter_glob: StringProperty(default="*.kcl", options={"HIDDEN"})

    scale: FloatProperty(name="Scale", default=0.01, min=1e-6, soft_max=10.0, precision=4,
                         description="Blender units per game unit (same as the BMD import)")
    plc_file: StringProperty(name="PLC File", default="",
                             description="Attribute table (room.plc). Empty = the .plc next to the "
                                         "file. Only used to describe the attributes")

    def execute(self, context):
        from . import blender_kcl
        from .j3d.common import J3DError
        try:
            blender_kcl.import_kcl(context, self.filepath, self.scale, self.plc_file, self.report)
        except J3DError as exc:
            self.report({"ERROR"}, f"{os.path.basename(self.filepath)}: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class EXPORT_OT_kcl(Operator, ExportHelper):
    """Export meshes as a Twilight Princess collision file (room.kcl). Keep the original room.plc"""
    bl_idname = "export_scene.kcl"
    bl_label = "Export TP Collision (.kcl)"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".kcl"
    filter_glob: StringProperty(default="*.kcl", options={"HIDDEN"})

    scale: FloatProperty(name="Scale", default=100.0, min=1e-6, soft_max=1000.0, precision=3,
                         description="Game units per Blender unit (inverse of the import scale)")
    use_selection: BoolProperty(name="Selected Only", default=True)
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=True)
    default_attr: IntProperty(name="Default Attribute", default=0, min=0, max=65535,
                              description="Attribute (row of room.plc) for faces that have no "
                                          "KCL_<n> material")
    keep_area: BoolProperty(name="Keep Original Area", default=True,
                            description="Reuse the area and block size of the imported file when the "
                                        "new geometry still fits into it")

    def execute(self, context):
        from . import blender_kcl
        from .j3d.common import J3DError
        try:
            blender_kcl.export_kcl(context, self.filepath, self.scale, self.use_selection,
                                   self.apply_modifiers, self.default_attr, self.keep_area, self.report)
        except (blender_kcl.KclExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORT_OT_bck(Operator, ImportHelper):
    """Import a J3D joint animation (.bck) onto the active armature"""
    bl_idname = "import_anim.bck"
    bl_label = "Import J3D Animation"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".bck"
    filter_glob: StringProperty(default="*.bck", options={"HIDDEN"})
    files: CollectionProperty(type=OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})

    use_model_scale: BoolProperty(name="Use Model Scale", default=True,
                                  description="Use the scale the armature was imported with")
    scale: FloatProperty(name="Scale", default=0.01, min=1e-6, soft_max=10.0, precision=4,
                         description="Blender units per J3D unit (only if Use Model Scale is off)")
    set_scene_range: BoolProperty(name="Set Scene Range", default=True,
                                  description="Set the scene frame range to the animation length")

    def execute(self, context):
        from . import blender_anim
        from .j3d.common import J3DError
        paths = [os.path.join(self.directory, f.name) for f in self.files] if self.files else []
        if not paths:
            paths = [self.filepath]
        for p in paths:
            try:
                blender_anim.import_bck(context, p, self.scale, self.use_model_scale,
                                        self.set_scene_range, self.report)
            except (blender_anim.AnimError, J3DError) as exc:
                self.report({"ERROR"}, f"{os.path.basename(p)}: {exc}")
                return {"CANCELLED"}
        return {"FINISHED"}


class EXPORT_OT_bck(Operator, ExportHelper):
    """Export the active action of the active armature as a J3D .bck animation"""
    bl_idname = "export_anim.bck"
    bl_label = "Export J3D Animation"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".bck"
    filter_glob: StringProperty(default="*.bck", options={"HIDDEN"})

    use_model_scale: BoolProperty(name="Use Model Scale", default=True,
                                  description="Use the scale the armature was imported with")
    scale: FloatProperty(name="Scale", default=100.0, min=1e-6, soft_max=1000.0, precision=3,
                         description="J3D units per Blender unit (only if Use Model Scale is off)")
    loop_mode: EnumProperty(name="Loop Mode", items=blender_anim_items(), default="AUTO")
    frame_source: EnumProperty(
        name="Frame Range",
        items=[("ACTION", "Action Range", "Frame range of the keyframes in the action"),
               ("SCENE", "Scene Range", "Scene start / end frame")],
        default="ACTION")

    def execute(self, context):
        from . import blender_anim
        from .j3d.common import J3DError
        try:
            blender_anim.export_bck(context, self.filepath, self.scale, self.use_model_scale,
                                    self.loop_mode, self.frame_source, self.report)
        except (blender_anim.AnimError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class MATERIAL_PT_bmd(Panel):
    bl_label = "J3D (BMD) Material"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "material"

    @classmethod
    def poll(cls, context):
        return context.material is not None

    def draw(self, context):
        layout = self.layout
        mat = context.material
        layout.prop(mat.bmd, "render_mode")
        if mat.bmd.render_mode in {"AUTO", "CUTOUT"}:
            layout.prop(mat.bmd, "alpha_ref")
        layout.prop(mat, "use_backface_culling")
        if "bmd_material" in mat:
            layout.label(text="Imported TEV data kept in 'bmd_material'", icon="INFO")
        if mat.node_tree:
            for node in mat.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.name.startswith("bmd_tex") and node.image:
                    row = layout.row()
                    row.label(text=node.name[4:])
                    row.prop(node.image.bmd, "format", text="")


class IMAGE_PT_bmd(Panel):
    bl_label = "J3D (BMD) Texture"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Image"

    @classmethod
    def poll(cls, context):
        return context.space_data and getattr(context.space_data, "image", None)

    def draw(self, context):
        img = context.space_data.image
        self.layout.prop(img.bmd, "format")
        self.layout.prop(img.bmd, "mip_count")
        self.layout.prop(img.bmd, "external")
        external_path = img.get("bmd_external_path")
        if external_path:
            self.layout.label(text=f"External: {os.path.basename(external_path)}")


def menu_import(self, context):
    self.layout.operator(IMPORT_OT_bmd.bl_idname, text="J3D Model (.bmd/.bdl)")
    self.layout.operator(IMPORT_OT_bck.bl_idname, text="J3D Animation (.bck)")
    self.layout.operator(IMPORT_OT_kcl.bl_idname, text="TP Collision (.kcl)")


def menu_export(self, context):
    self.layout.operator(EXPORT_OT_bmd.bl_idname, text="J3D Model (.bmd)")
    self.layout.operator(EXPORT_OT_bck.bl_idname, text="J3D Animation (.bck)")
    self.layout.operator(EXPORT_OT_kcl.bl_idname, text="TP Collision (.kcl)")


_classes = (IMPORT_OT_bmd, BMDImportFileState, IMPORT_OT_bmd_texc, EXPORT_OT_bmd, IMPORT_OT_bck,
            EXPORT_OT_bck, IMPORT_OT_kcl, EXPORT_OT_kcl, MATERIAL_PT_bmd, IMAGE_PT_bmd)
_file_handlers = []


def _make_file_handlers():
    """Drag & drop support (Blender 4.1+); silently skipped if unavailable."""
    try:
        from bpy_extras.io_utils import poll_file_object_drop

        class BMD_FH_import(bpy.types.FileHandler):
            bl_idname = "BMD_FH_import"
            bl_label = "J3D BMD"
            bl_import_operator = "import_scene.bmd"
            bl_file_extensions = ".bmd;.bdl"

            @classmethod
            def poll_drop(cls, context):
                return poll_file_object_drop(context)

        class BCK_FH_import(bpy.types.FileHandler):
            bl_idname = "BCK_FH_import"
            bl_label = "J3D BCK"
            bl_import_operator = "import_anim.bck"
            bl_file_extensions = ".bck"

            @classmethod
            def poll_drop(cls, context):
                return poll_file_object_drop(context)

        class KCL_FH_import(bpy.types.FileHandler):
            bl_idname = "KCL_FH_import"
            bl_label = "TP Collision KCL"
            bl_import_operator = "import_scene.kcl"
            bl_file_extensions = ".kcl"

            @classmethod
            def poll_drop(cls, context):
                return poll_file_object_drop(context)

        return [BMD_FH_import, BCK_FH_import, KCL_FH_import]
    except Exception:
        return []


def register():
    global _file_handlers
    bc.register_classes()
    for c in _classes:
        bpy.utils.register_class(c)
    _file_handlers = _make_file_handlers()
    for fh in _file_handlers:
        bpy.utils.register_class(fh)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister():
    global _file_handlers
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    for fh in _file_handlers:
        bpy.utils.unregister_class(fh)
    _file_handlers = []
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
    bc.unregister_classes()
