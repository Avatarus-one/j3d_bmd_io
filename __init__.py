"""J3D BMD Import/Export for Blender (Zelda: Twilight Princess / Dusklight).

Includes BMD/BDL models, BCK animations, BLS shape keys and KCL collision.
"""

import json
import math
import os

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Operator, OperatorFileListElement, Panel, PropertyGroup, UIList
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import blender_common as bc

_PENDING_BMD_IMPORT = None


def _export_convert_units_changed(self, context):
    """Keep the shared scene setting in sync with any export dialog."""
    bc.store_convert_units(context, self.convert_units)


_BTI_STATUS = {
    "new": ("New", "EXPORT"),
    "update": ("Modified", "FILE_REFRESH"),
    "unchanged": ("Up to date", "FILE_TICK"),
    "conflict": ("Conflict", "ERROR"),
    "error": ("Error", "ERROR"),
    "no_folder": ("Select folder", "INFO"),
}

MAX_BTI_TREE_ROWS = 12


def blender_anim_items():
    from .blender_anim import LOOP_ITEMS
    return [item for item in LOOP_ITEMS if item[0] != "AUTO"]


def _bck_action(context):
    from . import blender_anim
    try:
        arm = blender_anim.find_armature(context)
    except blender_anim.AnimError:
        return None
    ad = arm.animation_data
    return ad.action if ad else None


def _bck_loop_mode(context):
    from . import blender_anim
    from .j3d import anim
    action = _bck_action(context)
    if action is None:
        return "ONCE"
    try:
        mode = int(action.get("bmd_loop_mode", anim.LOOP_ONCE))
    except (TypeError, ValueError):
        return "ONCE"
    if 0 <= mode < len(anim.LOOP_NAMES):
        return anim.LOOP_NAMES[mode]
    return "ONCE"


def _bck_sync_frame_range(operator, context):
    if operator.frame_source == "SCENE":
        operator.custom_start = context.scene.frame_start
        operator.custom_end = context.scene.frame_end
    elif operator.frame_source == "ACTION":
        action = _bck_action(context)
        if action is None:
            operator.custom_start = context.scene.frame_start
            operator.custom_end = context.scene.frame_end
        else:
            start, end = action.frame_range
            operator.custom_start = int(math.floor(start))
            operator.custom_end = int(math.ceil(end))


def _bck_frame_source_changed(operator, context):
    _bck_sync_frame_range(operator, context)


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
            armature = bc.target_armature(obj)
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
    bl_label = "Import BMD"
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

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "bone_axis")
        layout.prop(self, "import_textures")

    def execute(self, context):
        from . import blender_import
        from . import blender_bti
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
                    names, folder = blender_bti.check_external_textures(bmd, p)
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
                    "convert_units": self.convert_units,
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
                                          self.import_textures, report=self.report)
            except J3DError as exc:
                self.report({"ERROR"}, f"{os.path.basename(p)}: {exc}")
                return {"CANCELLED"}
        bc.store_convert_units(context, self.convert_units)
        return {"FINISHED"}


class BMDImportFileState(PropertyGroup):
    expanded: BoolProperty(default=True)


class BMDExportGroupState(PropertyGroup):
    expanded: BoolProperty(default=True)
    selected: BoolProperty(default=True)


class BMDExportTextureState(PropertyGroup):
    selected: BoolProperty(default=True)


class BMDExportTreeRow(PropertyGroup):
    kind: StringProperty()
    group_index: IntProperty()
    texture_index: IntProperty()
    label: StringProperty()
    enabled: BoolProperty(default=True)
    status_text: StringProperty()
    status_icon: StringProperty(default="INFO")


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

        from . import blender_bti

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

            availability = blender_bti.texture_availability(
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
                    texc_dir=texc_dir)
            except J3DError as exc:
                _PENDING_BMD_IMPORT = None
                self.report({"ERROR"}, f"{os.path.basename(path)}: {exc}")
                return {"CANCELLED"}

        _PENDING_BMD_IMPORT = None
        bc.store_convert_units(context, pending["convert_units"])
        return {"FINISHED"}

    def cancel(self, context):
        global _PENDING_BMD_IMPORT
        _PENDING_BMD_IMPORT = None


class EXPORT_OT_bmd(Operator, ExportHelper):
    """Export meshes (and their armature) as a J3D BMD3 model"""
    bl_idname = "export_scene.bmd"
    bl_label = "Export BMD"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".bmd"
    filter_glob: StringProperty(default="*.bmd", options={"HIDDEN"})

    convert_units: BoolProperty(
        name="Convert Units", default=True,
        update=_export_convert_units_changed,
        description="Convert Blender units to J3D model units (scale 100). ")
    use_selection: BoolProperty(name="Selected Only", default=True,
                                description="Export only selected objects (select the armature and meshes)")
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=False,
                                  description="Apply modifiers (except armature) before exporting")
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
        self.convert_units = bc.saved_convert_units(context)

        result = ExportHelper.invoke(self, context, _event)
        # File-select operators can restore their last-used properties while
        # opening; apply the detected formats once more before the panel draws.
        self._apply_detected_formats(context)
        self.convert_units = bc.saved_convert_units(context)
        return result

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "use_selection")
        layout.prop(self, "apply_modifiers")
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
                self.apply_modifiers, self.source_file,
                self.allow_missing_textures, True,
                self.report, self.limit_weights, False, False,
                self.position_format, self.normal_format, self.uv_format,
                self.position_frac, self.normal_frac, self.uv_frac)
        except (blender_export.ExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        bc.store_convert_units(context, self.convert_units)
        return {"FINISHED"}


class BMD_UL_bti_tree(UIList):
    """Flattened BTI texture tree with scrolling."""

    bl_idname = "BMD_UL_bti_tree"

    def filter_items(self, context, data, propname):
        rows = getattr(data, propname)
        flags = []
        for row in rows:
            if row.kind == "group":
                flags.append(self.bitflag_filter_item)
                continue
            if (row.group_index < len(data.group_states) and
                    not data.group_states[row.group_index].expanded):
                flags.append(0)
            else:
                flags.append(self.bitflag_filter_item)
        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag=0):
        if item.kind == "group":
            state = (data.group_states[item.group_index]
                     if item.group_index < len(data.group_states) else None)
            row = layout.row(align=True)
            row.alignment = "LEFT"
            if state is not None:
                check = row.row()
                check.prop(state, "selected", text="")
                row.prop(state, "expanded", text=item.label,
                         icon="TRIA_DOWN" if state.expanded else "TRIA_RIGHT",
                         emboss=False)
            else:
                row.label(text=item.label, icon="TRIA_DOWN")
            return

        group_selected = True
        if item.group_index < len(data.group_states):
            group_selected = data.group_states[item.group_index].selected
        active = bool(group_selected and item.enabled)
        row = layout.row(align=True)
        row.active = active
        row.label(text="", icon="BLANK1")
        check = row.row()
        check.enabled = active
        if item.texture_index < len(data.texture_states):
            check.prop(data.texture_states[item.texture_index], "selected", text="")
        row.label(text=item.label, icon="IMAGE_DATA")
        status = row.row()
        status.alignment = "RIGHT"
        status.label(text=item.status_text, icon=item.status_icon or "INFO")


class IMPORT_OT_bti(Operator, ImportHelper):
    """Import J3D textures (.bti) as Blender images"""
    bl_idname = "import_image.bti"
    bl_label = "Import BTI"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".bti"
    filter_glob: StringProperty(default="*.bti", options={"HIDDEN"})
    filepath: StringProperty(
        name="File Path",
        description="Filepath used for importing the file",
        maxlen=1024,
        subtype="FILE_PATH",
        options={"SKIP_SAVE", "HIDDEN"},
    )
    files: CollectionProperty(type=OperatorFileListElement,
                              options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})

    def invoke(self, context, event):
        if self.filepath:
            return self.execute(context)

        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        from . import bti_import
        from .j3d.common import J3DError
        paths = ([os.path.join(self.directory, file.name) for file in self.files]
                 if self.files else [])

        if not paths:
            paths = [self.filepath]

        if not paths[0]:
            self.report({"ERROR"}, "No texture file selected")
            return {"CANCELLED"}

        for path in paths:
            try:
                bti_import.import_bti(context, path, self.report)
            except (J3DError, OSError) as exc:
                self.report({"ERROR"}, f"{os.path.basename(path)}: {exc}")
                return {"CANCELLED"}

        return {"FINISHED"}


class IMPORT_OT_bti_node(Operator, ImportHelper):
    """Import a .bti texture and assign it to the active Image Texture node"""
    bl_idname = "import_image.bti_node"
    bl_label = "Import BTI"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".bti"
    filter_glob: StringProperty(default="*.bti", options={"HIDDEN"})
    filepath: StringProperty(
        name="File Path",
        description="Filepath used for importing the file",
        maxlen=1024,
        subtype="FILE_PATH",
        options={"SKIP_SAVE", "HIDDEN"},
    )
    files: CollectionProperty(type=OperatorFileListElement,
                              options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})

    def invoke(self, context, event):
        space = getattr(context, "space_data", None)
        self._node_tree = None
        self._node_name = ""
        self._node_location = (0.0, 0.0)

        if (space is not None and getattr(space, "type", None) == "NODE_EDITOR"
                and getattr(space, "tree_type", None) == "ShaderNodeTree"):
            tree = getattr(space, "edit_tree", None)

            if tree is not None:
                self._node_tree = tree
                self._node_location = getattr(
                    space, "cursor_location", (0.0, 0.0))
                node = tree.nodes.active
                self._node_name = node.name if node is not None else ""

        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        from . import bti_import
        from .j3d.common import J3DError
        paths = ([os.path.join(self.directory, file.name) for file in self.files]
                 if self.files else [])

        if not paths:
            paths = [self.filepath]

        if not paths[0]:
            self.report({"ERROR"}, "No texture file selected")
            return {"CANCELLED"}

        image = None

        for path in paths:
            try:
                image = bti_import.import_bti(context, path, self.report)
            except (J3DError, OSError) as exc:
                self.report({"ERROR"}, f"{os.path.basename(path)}: {exc}")
                return {"CANCELLED"}

        tree = getattr(self, "_node_tree", None)

        if tree is not None and image is not None:
            node = (tree.nodes.get(self._node_name)
                    if self._node_name else None)

            if node is None or node.type != "TEX_IMAGE":
                node = tree.nodes.new("ShaderNodeTexImage")
                node.location = getattr(
                    self, "_node_location", (0.0, 0.0))

            node.image = image

            for other in tree.nodes:
                other.select = False

            node.select = True
            tree.nodes.active = node

        return {"FINISHED"}


class EXPORT_OT_bti(Operator):
    """Export external textures of the whole scene as .bti files"""
    bl_idname = "export_scene.bti"
    bl_label = "Export BTI"
    bl_options = {"REGISTER"}

    directory: StringProperty(
        name="Folder", subtype="DIR_PATH", default="",
        description="Folder for the .bti files")
    group_states: CollectionProperty(type=BMDExportGroupState, options={"HIDDEN"})
    texture_states: CollectionProperty(type=BMDExportTextureState, options={"HIDDEN"})
    tree_rows: CollectionProperty(type=BMDExportTreeRow, options={"HIDDEN"})
    active_tree_row: IntProperty(default=0, min=0)

    def invoke(self, context, _event):
        from . import bti_export
        if not self.directory:
            self.directory = bti_export.default_bti_directory(context)
        self._groups = bti_export.scene_bti_groups()
        self.group_states.clear()
        self.texture_states.clear()
        total = 0
        for group in self._groups:
            self.group_states.add()
            total += len(group["images"])
            for _image in group["images"]:
                self.texture_states.add()
        self._last_group_selected = [True] * len(self._groups)
        self._last_texture_selected = [True] * total
        self._plan_key = None
        self._plans = None
        return context.window_manager.invoke_props_dialog(
            self, width=600, confirm_text="Export")

    @staticmethod
    def _texture_offsets(groups):
        offsets = []
        index = 0
        for group in groups:
            offsets.append((index, index + len(group["images"])))
            index += len(group["images"])
        return offsets

    def _rebuild_tree_rows(self, groups, plans):
        offsets = self._texture_offsets(groups)
        self.tree_rows.clear()
        for gi, group in enumerate(groups):
            group_row = self.tree_rows.add()
            group_row.kind = "group"
            group_row.group_index = gi
            group_row.label = group["label"]
            entries = plans[gi] if gi < len(plans) else []
            start = offsets[gi][0]
            for ti, entry in enumerate(entries):
                row = self.tree_rows.add()
                row.kind = "texture"
                row.group_index = gi
                row.texture_index = start + ti
                row.label = entry["name"]
                row.enabled = self._entry_enabled(entry)
                text, icon = _BTI_STATUS.get(entry["status"], ("?", "INFO"))
                row.status_text = text
                row.status_icon = icon
        if self.active_tree_row >= len(self.tree_rows):
            self.active_tree_row = max(0, len(self.tree_rows) - 1)

    @staticmethod
    def _entry_enabled(entry):
        return entry["status"] in ("new", "update")

    def _apply_group_toggles(self, groups, plans):
        last_values = getattr(self, "_last_group_selected", None)
        if last_values is None or len(last_values) != len(groups):
            last_values = [True] * len(groups)
            self._last_group_selected = last_values
        total = sum(len(entries) for entries in plans)
        last_textures = getattr(self, "_last_texture_selected", None)
        if last_textures is None or len(last_textures) != total:
            last_textures = [True] * total
            self._last_texture_selected = last_textures
        offsets = self._texture_offsets(groups)
        for index, _group in enumerate(groups):
            if index >= len(self.group_states):
                continue
            state = self.group_states[index]
            start, end = offsets[index]
            if state.selected == last_values[index]:
                if not state.selected:
                    # A disabled file keeps all its texture checkboxes off.
                    for ti in range(start, min(end, len(self.texture_states))):
                        self.texture_states[ti].selected = False
                        if ti < len(last_textures):
                            last_textures[ti] = False
                continue
            # The user toggled the file checkbox: selecting it enables all
            # exportable textures, clearing it disables every texture of this
            # file without touching identical textures in other files.
            entries = plans[index] if index < len(plans) else []
            for ti in range(start, min(end, len(self.texture_states))):
                entry = entries[ti - start] if ti - start < len(entries) else None
                value = bool(state.selected and entry is not None
                             and self._entry_enabled(entry))
                self.texture_states[ti].selected = value
                if ti < len(last_textures):
                    last_textures[ti] = value
            last_values[index] = state.selected

    def _sync_texture_states(self, groups, plans):
        flat_entries = [entry for entries in plans for entry in entries]
        last_values = getattr(self, "_last_texture_selected", None)
        if last_values is None or len(last_values) != len(flat_entries):
            last_values = [True] * len(flat_entries)
            self._last_texture_selected = last_values
        offsets = self._texture_offsets(groups)
        file_enabled = []
        for index, _group in enumerate(groups):
            enabled = (index < len(self.group_states)
                       and self.group_states[index].selected)
            start, end = offsets[index]
            file_enabled.extend([enabled] * (end - start))
        # Excluded files and non-exportable textures are inactive and off.
        for index, entry in enumerate(flat_entries):
            active = (index < len(file_enabled) and file_enabled[index]
                      and self._entry_enabled(entry))
            if not active:
                if index < len(self.texture_states) and self.texture_states[index].selected:
                    self.texture_states[index].selected = False
                if index < len(last_values):
                    last_values[index] = False
        # Identical textures sync only while their files are enabled.
        identities = {}
        for index, entry in enumerate(flat_entries):
            identity = entry.get("identity")
            if (identity is not None and index < len(file_enabled)
                    and file_enabled[index] and self._entry_enabled(entry)):
                identities.setdefault(identity, []).append(index)
        for indices in identities.values():
            new_value = None
            for index in indices:
                if (index < len(self.texture_states) and index < len(last_values)
                        and self.texture_states[index].selected != last_values[index]):
                    new_value = self.texture_states[index].selected
                    break
            if new_value is None:
                continue
            for index in indices:
                if index < len(self.texture_states):
                    self.texture_states[index].selected = new_value
                if index < len(last_values):
                    last_values[index] = new_value

    def _selected_image_names(self, groups):
        if groups is None or len(self.texture_states) == 0:
            return None
        selected = set()
        index = 0
        for gi, group in enumerate(groups):
            file_enabled = (self.group_states[gi].selected
                            if gi < len(self.group_states) else True)
            for image in group["images"]:
                if (file_enabled and index < len(self.texture_states)
                        and self.texture_states[index].selected):
                    selected.add(image.name)
                index += 1
        return selected

    def draw(self, context):
        from . import bti_export
        layout = self.layout

        groups = getattr(self, "_groups", None)
        if groups is None:
            groups = bti_export.scene_bti_groups()
            self._groups = groups

        directory = self.directory.strip()
        key = (directory,
               tuple(tuple(image.name for image in group["images"]) for group in groups))
        if getattr(self, "_plan_key", None) != key:
            self._plans = bti_export.bti_scene_plan(
                groups, directory, with_payload=False)
            self._plan_key = key
            self._rebuild_tree_rows(groups, self._plans or [])
        plans = self._plans or []

        self._apply_group_toggles(groups, plans)
        self._sync_texture_states(groups, plans)

        if groups:
            visible = 0
            for gi, _group in enumerate(groups):
                visible += 1
                state = (self.group_states[gi]
                         if gi < len(self.group_states) else None)
                if state is None or state.expanded:
                    visible += len(plans[gi]) if gi < len(plans) else 0
            rows = min(max(1, visible), MAX_BTI_TREE_ROWS)
            layout.template_list(
                "BMD_UL_bti_tree", "",
                self, "tree_rows",
                self, "active_tree_row",
                rows=rows,
                maxrows=MAX_BTI_TREE_ROWS,
            )
        else:
            layout.label(text="No external textures found in the scene", icon="INFO")

        layout.separator()
        field = layout.column()
        field.use_property_split = True
        field.prop(self, "directory")
        hint = layout.row()
        hint.active = False
        hint.label(text="Unchanged .bti files are not overwritten", icon="INFO")

    def execute(self, context):
        from . import bti_export
        from .j3d.common import J3DError
        directory = self.directory.strip()
        if not directory:
            self.report({"ERROR"}, "Select an output folder")
            return {"CANCELLED"}
        groups = getattr(self, "_groups", None)
        if groups is not None:
            key = (directory,
                   tuple(tuple(image.name for image in group["images"]) for group in groups))
            if getattr(self, "_plan_key", None) != key:
                self._plans = bti_export.bti_scene_plan(
                    groups, directory, with_payload=False)
                self._plan_key = key
            plans = self._plans or []
            self._apply_group_toggles(groups, plans)
            self._sync_texture_states(groups, plans)
        selected = self._selected_image_names(groups)
        try:
            bti_export.export_bti_textures(context, directory, self.report,
                                           selected=selected)
        except (bti_export.ExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class IMPORT_OT_kcl(Operator, ImportHelper):
    """Import a Twilight Princess collision mesh (room.kcl, optionally with room.plc)"""
    bl_idname = "import_scene.kcl"
    bl_label = "Import KCL"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ".kcl"
    filter_glob: StringProperty(default="*.kcl", options={"HIDDEN"})

    convert_units: BoolProperty(
        name="Convert Units", default=True,
        description="Convert J3D units to Blender units (scale 0.01)")
    plc_file: StringProperty(name="PLC File", default="",
                             description="Attribute table (room.plc). Empty = the .plc next to the "
                                         "file. Only used to describe the attributes")

    def invoke(self, context, event):
        self.convert_units = bc.saved_convert_units(context)
        result = ImportHelper.invoke(self, context, event)
        self.convert_units = bc.saved_convert_units(context)
        return result

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "plc_file")

    def execute(self, context):
        from . import blender_kcl
        from .j3d.common import J3DError
        scale = 0.01 if self.convert_units else 1.0
        try:
            blender_kcl.import_kcl(context, self.filepath, scale, self.plc_file, self.report)
        except J3DError as exc:
            self.report({"ERROR"}, f"{os.path.basename(self.filepath)}: {exc}")
            return {"CANCELLED"}
        bc.store_convert_units(context, self.convert_units)
        return {"FINISHED"}


class EXPORT_OT_kcl(Operator, ExportHelper):
    """Export meshes as a Twilight Princess collision file (room.kcl). Keep the original room.plc"""
    bl_idname = "export_scene.kcl"
    bl_label = "Export KCL"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".kcl"
    filter_glob: StringProperty(default="*.kcl", options={"HIDDEN"})

    convert_units: BoolProperty(
        name="Convert Units", default=True,
        update=_export_convert_units_changed,
        description="Convert Blender units to J3D units (scale 100)")
    use_selection: BoolProperty(name="Selected Only", default=True)
    apply_modifiers: BoolProperty(name="Apply Modifiers", default=True)
    default_attr: IntProperty(name="Default Attribute", default=0, min=0, max=65535,
                              description="Attribute (row of room.plc) for faces that have no "
                                          "KCL_<n> material")
    keep_area: BoolProperty(name="Keep Original Area", default=True,
                            description="Reuse the area and block size of the imported file when the "
                                        "new geometry still fits into it")

    def invoke(self, context, event):
        self.convert_units = bc.saved_convert_units(context)

        result = ExportHelper.invoke(self, context, event)
        self.convert_units = bc.saved_convert_units(context)
        return result

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "use_selection")
        layout.prop(self, "apply_modifiers")
        layout.prop(self, "default_attr")
        layout.prop(self, "keep_area")

    def execute(self, context):
        from . import blender_kcl
        from .j3d.common import J3DError
        scale = 100.0 if self.convert_units else 1.0
        try:
            blender_kcl.export_kcl(context, self.filepath, scale, self.use_selection,
                                   self.apply_modifiers, self.default_attr, self.keep_area, self.report)
        except (blender_kcl.KclExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        bc.store_convert_units(context, self.convert_units)
        return {"FINISHED"}


class IMPORT_OT_bck(Operator, ImportHelper):
    """Import a J3D joint animation (.bck) onto the active armature"""
    bl_idname = "import_anim.bck"
    bl_label = "Import BCK"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".bck"
    filter_glob: StringProperty(default="*.bck", options={"HIDDEN"})
    files: CollectionProperty(type=OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN"})

    convert_units: BoolProperty(
        name="Convert Units", default=True,
        description="Convert J3D units to Blender units (scale 0.01)")
    set_scene_range: BoolProperty(name="Set Scene Range", default=True,
                                  description="Set the scene frame range to the animation length")

    def invoke(self, context, event):
        self.convert_units = bc.saved_convert_units(context)
        result = ImportHelper.invoke(self, context, event)
        self.convert_units = bc.saved_convert_units(context)
        return result

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "set_scene_range")

    def execute(self, context):
        from . import blender_anim
        from .j3d.common import J3DError
        scale = 0.01 if self.convert_units else 1.0
        paths = [os.path.join(self.directory, f.name) for f in self.files] if self.files else []
        if not paths:
            paths = [self.filepath]
        for p in paths:
            try:
                blender_anim.import_bck(context, p, scale, self.set_scene_range, self.report)
            except (blender_anim.AnimError, J3DError) as exc:
                self.report({"ERROR"}, f"{os.path.basename(p)}: {exc}")
                return {"CANCELLED"}
        return {"FINISHED"}


class EXPORT_OT_bck(Operator, ExportHelper):
    """Export the active action of the active armature as a J3D .bck animation"""
    bl_idname = "export_anim.bck"
    bl_label = "Export BCK"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".bck"
    filter_glob: StringProperty(default="*.bck", options={"HIDDEN"})

    convert_units: BoolProperty(
        name="Convert Units", default=True,
        update=_export_convert_units_changed,
        description="Convert Blender units to J3D units (scale 100)")
    loop_mode: EnumProperty(name="Loop Mode", items=blender_anim_items(), default="ONCE")
    frame_source: EnumProperty(
        name="Frame Range",
        items=[("ACTION", "Action Range", "Frame range of the keyframes in the action"),
               ("SCENE", "Scene Range", "Scene start / end frame"),
               ("CUSTOM", "Custom Range", "Use the Start Frame and End Frame fields")],
        default="ACTION",
        update=_bck_frame_source_changed)
    custom_start: IntProperty(name="Start Frame", default=0)
    custom_end: IntProperty(name="End Frame", default=0)

    def invoke(self, context, event):
        self.convert_units = bc.saved_convert_units(context)

        result = ExportHelper.invoke(self, context, event)
        self.convert_units = bc.saved_convert_units(context)
        self.loop_mode = _bck_loop_mode(context)
        if self.frame_source != "CUSTOM":
            _bck_sync_frame_range(self, context)
        return result

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "loop_mode")
        layout.prop(self, "frame_source")
        start_row = layout.row()
        start_row.enabled = self.frame_source == "CUSTOM"
        start_row.prop(self, "custom_start")
        end_row = layout.row()
        end_row.enabled = self.frame_source == "CUSTOM"
        end_row.prop(self, "custom_end")
        if (self.frame_source == "CUSTOM"
                and self.custom_end < self.custom_start):
            layout.label(text="End Frame must not be before Start Frame",
                         icon="ERROR")

    def execute(self, context):
        from . import blender_anim
        from .j3d.common import J3DError
        scale = 100.0 if self.convert_units else 1.0
        custom_range = ((self.custom_start, self.custom_end)
                        if self.frame_source == "CUSTOM" else None)
        try:
            blender_anim.export_bck(context, self.filepath, scale,
                                    self.loop_mode, self.frame_source, self.report,
                                    custom_range=custom_range)
        except (blender_anim.AnimError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        bc.store_convert_units(context, self.convert_units)
        return {"FINISHED"}


class IMPORT_OT_blk(Operator, ImportHelper):
    """Import a J3D shape-key animation (.blk) onto the active mesh"""
    bl_idname = "import_anim.blk"
    bl_label = "Import BLK"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".blk"
    filter_glob: StringProperty(default="*.blk", options={"HIDDEN"})
    filepath: StringProperty(
        name="File Path",
        description="Filepath used for importing the file",
        maxlen=1024,
        subtype="FILE_PATH",
        options={"SKIP_SAVE", "HIDDEN"},
    )

    def invoke(self, context, event):
        if self.filepath:
            return self.execute(context)

        return ImportHelper.invoke(self, context, event)

    def execute(self, context):
        from . import blk_import

        if not self.filepath:
            self.report({"ERROR"}, "No BLK file selected")
            return {"CANCELLED"}

        return blk_import.begin_import(context, self.filepath, self)


class EXPORT_OT_blk(Operator, ExportHelper):
    """Export the active shape-key action as a J3D .blk animation"""
    bl_idname = "export_anim.blk"
    bl_label = "Export BLK"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".blk"
    filter_glob: StringProperty(default="*.blk", options={"HIDDEN"})

    def execute(self, context):
        from . import blk_export, bls_export_ui

        if not self.filepath:
            self.report({"ERROR"}, "No BLK file selected")
            return {"CANCELLED"}

        settings = bls_export_ui._get_export_settings(context)

        if settings is None:
            self.report({"ERROR"}, "Export settings are unavailable")
            return {"CANCELLED"}

        target = context.active_object

        if target is None or target.type != "MESH":
            target = next(
                (obj for obj in context.selected_objects if obj.type == "MESH"),
                None)

        if target is not None:
            settings.target = target

        settings.blk_filepath = self.filepath
        settings.show_base = False
        bls_export_ui._populate_export_keys(settings, settings.target)

        bpy.app.timers.register(blk_export.open_export_dialog,
                                first_interval=0.05)
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
            box = layout.box()
            box.label(text="Image nodes", icon="IMAGE_DATA")
            for node in mat.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.name.startswith("bmd_tex") and node.image:
                    row = box.row()
                    row.label(text=f"{node.name[4:]}: {bc.texture_key(node.image)}")
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
        self.layout.prop(img.bmd, "key")
        self.layout.prop(img.bmd, "mip_count")
        self.layout.prop(img.bmd, "external")
        external_path = img.get("bmd_external_path")
        if external_path:
            self.layout.label(text=f"External: {os.path.basename(external_path)}")


def menu_node_import_bti(self, context):
    space = getattr(context, "space_data", None)

    if (space is None or getattr(space, "type", None) != "NODE_EDITOR"
            or getattr(space, "tree_type", None) != "ShaderNodeTree"):
        return

    self.layout.operator(IMPORT_OT_bti_node.bl_idname,
                         text="Import J3D Texture (.bti)", icon="IMPORT")


def _draw_format_box(layout, title, items):
    box = layout.box()
    box.label(text=title)
    column = box.column(align=True)
    for idname, text, icon in items:
        column.operator(idname, text=text, icon=icon)


class BMD_PT_j3d(Panel):
    bl_idname = "BMD_PT_j3d"
    bl_label = "Import / Export"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "J3D"

    def draw(self, context):
        layout = self.layout

        _draw_format_box(layout, "Model (.bmd/.bdl)", (
            (IMPORT_OT_bmd.bl_idname, "Import Model", "IMPORT"),
            (EXPORT_OT_bmd.bl_idname, "Export Model", "EXPORT"),
        ))

        _draw_format_box(layout, "Animation (.bck)", (
            (IMPORT_OT_bck.bl_idname, "Import Animation", "IMPORT"),
            (EXPORT_OT_bck.bl_idname, "Export Animation", "EXPORT"),
        ))

        from . import bls_import_ui
        bls_items = [
            ("bmd_shape_keys.import_files", "Import Shape Keys", "IMPORT"),
            ("bmd_shape_keys.import_folder", "Import Shape Keys (Folder)", "IMPORT"),
            ("bmd_shape_keys.export_files", "Export Shape Keys", "EXPORT"),
        ]
        if bls_import_ui.has_pending_import():
            bls_items.append(
                ("bmd_shape_keys.settings", "Import Settings", "PREFERENCES"))
        _draw_format_box(layout, "Shape Keys (.bls)", bls_items)

        _draw_format_box(layout, "Shape Key Animation (.blk)", (
            (IMPORT_OT_blk.bl_idname, "Import Animation", "IMPORT"),
            (EXPORT_OT_blk.bl_idname, "Export Animation", "EXPORT"),
        ))

        _draw_format_box(layout, "Textures (.bti)", (
            (IMPORT_OT_bti.bl_idname, "Import Textures", "IMPORT"),
            (EXPORT_OT_bti.bl_idname, "Export Textures", "EXPORT"),
        ))

        _draw_format_box(layout, "Collision (.kcl)", (
            (IMPORT_OT_kcl.bl_idname, "Import Collision", "IMPORT"),
            (EXPORT_OT_kcl.bl_idname, "Export Collision", "EXPORT"),
        ))


def menu_import(self, context):
    self.layout.operator(IMPORT_OT_bmd.bl_idname, text="J3D Model (.bmd/.bdl)")
    self.layout.operator(IMPORT_OT_bck.bl_idname, text="J3D Animation (.bck)")
    self.layout.operator(IMPORT_OT_bti.bl_idname, text="J3D Texture (.bti)")
    self.layout.operator(IMPORT_OT_kcl.bl_idname, text="TP Collision (.kcl)")
    self.layout.operator("bmd_shape_keys.import_files", text="J3D Shape Keys (.bls)")
    self.layout.operator("bmd_shape_keys.import_folder",
                         text="J3D Shape Keys from Folder (.bls)")
    self.layout.operator(IMPORT_OT_blk.bl_idname,
                         text="J3D Shape Key Animation (.blk)")


def menu_image_import_bti(self, context):
    self.layout.operator(IMPORT_OT_bti.bl_idname, text="Import J3D Texture (.bti)")


def menu_export(self, context):
    self.layout.operator(EXPORT_OT_bmd.bl_idname, text="J3D Model (.bmd)")
    self.layout.operator(EXPORT_OT_bti.bl_idname, text="J3D Textures (.bti)")
    self.layout.operator(EXPORT_OT_bck.bl_idname, text="J3D Animation (.bck)")
    self.layout.operator(EXPORT_OT_kcl.bl_idname, text="TP Collision (.kcl)")
    self.layout.operator("bmd_shape_keys.export_files", text="J3D Shape Keys (.bls)")
    self.layout.operator(EXPORT_OT_blk.bl_idname,
                         text="J3D Shape Key Animation (.blk)")


_classes = (IMPORT_OT_bmd, BMDImportFileState, IMPORT_OT_bmd_texc,
            BMDExportGroupState, BMDExportTextureState, BMDExportTreeRow,
            BMD_UL_bti_tree, EXPORT_OT_bmd, IMPORT_OT_bti,
            IMPORT_OT_bti_node, EXPORT_OT_bti,
            IMPORT_OT_bck, EXPORT_OT_bck, IMPORT_OT_blk, EXPORT_OT_blk,
            IMPORT_OT_kcl, EXPORT_OT_kcl, MATERIAL_PT_bmd,
            IMAGE_PT_bmd, BMD_PT_j3d)
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

        class BLK_FH_import(bpy.types.FileHandler):
            bl_idname = "BLK_FH_import"
            bl_label = "J3D BLK Shape Key Animation"
            bl_import_operator = "import_anim.blk"
            bl_file_extensions = ".blk"

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

        class BLS_FH_import(bpy.types.FileHandler):
            bl_idname = "BLS_FH_import"
            bl_label = "J3D Shape Keys"
            bl_import_operator = "bmd_shape_keys.import_files"
            bl_file_extensions = ".bls"

            @classmethod
            def poll_drop(cls, context):
                return poll_file_object_drop(context)

        class BTI_FH_import(bpy.types.FileHandler):
            bl_idname = "BTI_FH_import"
            bl_label = "J3D BTI Texture"
            bl_import_operator = "import_image.bti"
            bl_file_extensions = ".bti"

            @classmethod
            def poll_drop(cls, context):
                area = context.area

                if area is not None:
                    if area.type == "IMAGE_EDITOR":
                        return True

                    if area.type == "NODE_EDITOR":
                        space = context.space_data
                        return (space is not None and
                                getattr(space, "tree_type", None) == "ShaderNodeTree")

                return poll_file_object_drop(context)

        return [BMD_FH_import, BCK_FH_import, BLK_FH_import, KCL_FH_import,
                BLS_FH_import, BTI_FH_import]
    except Exception:
        return []


def register():
    global _file_handlers
    bc.register_classes()
    for c in _classes:
        bpy.utils.register_class(c)

    from . import blk_export, blk_import, bls_import_ui, bls_export_ui
    for c in bls_import_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in bls_export_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in blk_import.CLASSES:
        bpy.utils.register_class(c)
    for c in blk_export.CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.bmd_shape_keys_settings = PointerProperty(
        type=bls_import_ui.BMD_SK_Settings)
    bpy.types.Scene.bmd_shape_keys_export_settings = PointerProperty(
        type=bls_export_ui.BMD_SK_ExportSettings)
    bpy.types.Scene.bmd_blk_import_settings = PointerProperty(
        type=blk_import.BMD_SK_BlkImportSettings)
    bpy.types.Scene.bmd_convert_units = BoolProperty(
        name="Convert Units",
        default=True,
        description="Last used Convert Units setting for imports and exports")
    bpy.types.Scene.bmd_shape_keys_last_info = StringProperty(
        name="Last BLS import", default="")

    _file_handlers = _make_file_handlers()
    for fh in _file_handlers:
        bpy.utils.register_class(fh)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)
    bpy.types.IMAGE_MT_image.append(menu_image_import_bti)
    bpy.types.NODE_MT_context_menu.append(menu_node_import_bti)


def unregister():
    global _file_handlers
    bpy.types.NODE_MT_context_menu.remove(menu_node_import_bti)
    bpy.types.IMAGE_MT_image.remove(menu_image_import_bti)
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    for fh in _file_handlers:
        bpy.utils.unregister_class(fh)
    _file_handlers = []

    from . import blk_export, blk_import, bls_import_ui, bls_export_ui
    if hasattr(bpy.types.Scene, "bmd_shape_keys_last_info"):
        del bpy.types.Scene.bmd_shape_keys_last_info
    if hasattr(bpy.types.Scene, "bmd_convert_units"):
        del bpy.types.Scene.bmd_convert_units
    if hasattr(bpy.types.Scene, "bmd_blk_import_settings"):
        del bpy.types.Scene.bmd_blk_import_settings
    if hasattr(bpy.types.Scene, "bmd_shape_keys_export_settings"):
        del bpy.types.Scene.bmd_shape_keys_export_settings
    if hasattr(bpy.types.Scene, "bmd_shape_keys_settings"):
        del bpy.types.Scene.bmd_shape_keys_settings
    for c in reversed(blk_export.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(blk_import.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(bls_export_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(bls_import_ui.CLASSES):
        bpy.utils.unregister_class(c)

    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
    bc.unregister_classes()
