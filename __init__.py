"""J3D BMD Import/Export for Blender (Zelda: Twilight Princess / Dusklight).

Includes BMD/BDL models, BCK animations, BLS shape keys and KCL collision.
"""

import json
import os

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Operator, OperatorFileListElement, Panel, PropertyGroup, UIList
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import blender_common as bc

_PENDING_BMD_IMPORT = None


_BTI_STATUS = {
    "new": ("New", "EXPORT"),
    "update": ("Modified", "FILE_REFRESH"),
    "unchanged": ("Up to date", "FILE_TICK"),
    "conflict": ("Conflict", "ERROR"),
    "error": ("Error", "ERROR"),
    "no_folder": ("Select folder", "INFO"),
}

MAX_BTI_TREE_ROWS = 12


@bpy.app.handlers.persistent
def _load_post_initialize_vertex_formats(*_args):
    try:
        bc.initialize_vertex_formats()
    except Exception:
        pass


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
    import_shape_keys: BoolProperty(
        name="Import Shape Keys", default=True,
        description="Import compatible BLS shape keys found next to the model")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "bone_axis")
        layout.prop(self, "import_textures")
        layout.prop(self, "import_shape_keys")

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
                        textures = j3d.parse_textures(handle.read(), load_data=False)
                    names, folder = blender_bti.check_external_textures(textures, p)
                except (OSError, J3DError) as exc:
                    self.report({"ERROR"}, f"{os.path.basename(p)}: {exc}")
                    return {"CANCELLED"}

                external_names = list(dict.fromkeys(
                    t.name for t in textures if t.external))
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
                    "import_shape_keys": self.import_shape_keys,
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
                arm = blender_import.import_bmd(
                    context, p, scale, self.bone_axis,
                    self.import_textures, report=self.report)
            except J3DError as exc:
                self.report({"ERROR"}, f"{os.path.basename(p)}: {exc}")
                return {"CANCELLED"}

            if self.import_shape_keys:
                from . import bls_import
                bls_import.import_shape_keys_for_bmd(
                    context, arm, p, scale, report=self.report)

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
                arm = blender_import.import_bmd(
                    context, path, pending["scale"], pending["bone_axis"],
                    pending["import_textures"], report=self.report,
                    texc_dir=texc_dir)
            except J3DError as exc:
                _PENDING_BMD_IMPORT = None
                self.report({"ERROR"}, f"{os.path.basename(path)}: {exc}")
                return {"CANCELLED"}

            if pending.get("import_shape_keys", True):
                from . import bls_import
                bls_import.import_shape_keys_for_bmd(
                    context, arm, path, pending["scale"],
                    report=self.report)

        _PENDING_BMD_IMPORT = None
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
    allow_missing_textures: BoolProperty(name="Allow Missing Textures", default=False,
                                         description="Export even if textures of the imported model "
                                                     "are gone (shifts texture indices!)")

    def invoke(self, context, _event):
        self.convert_units = bc.saved_convert_units(context)
        mesh = bc.export_target_mesh(context, "bmd")
        last = bc.last_export_path(mesh, "bmd")
        fallback = mesh.name if mesh is not None else "model"
        self.filepath = last or bpy.path.ensure_ext(fallback, self.filename_ext)
        result = ExportHelper.invoke(self, context, _event)
        self.convert_units = bc.saved_convert_units(context)
        return result

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "convert_units")
        layout.prop(self, "use_selection")
        layout.prop(self, "apply_modifiers")
        layout.prop(self, "source_file")
        layout.prop(self, "allow_missing_textures")

    def execute(self, context):
        from . import blender_export
        from .j3d.common import J3DError
        scale = 100.0 if self.convert_units else 1.0
        arm_obj, meshes = bc.export_model_objects(context, self.use_selection)
        owner = arm_obj or (meshes[0] if meshes else None)
        formats = bc.ensure_vertex_formats(owner) if owner is not None else None
        if formats is not None:
            position_format = formats.position_format
            normal_format = formats.normal_format
            uv_format = formats.uv_format
            position_frac = formats.position_frac
            normal_frac = formats.normal_frac
            uv_frac = formats.uv_frac
        else:
            position_format = normal_format = uv_format = "AUTO"
            position_frac, normal_frac, uv_frac = 6, 14, 14
        try:
            blender_export.export_bmd(
                context, self.filepath, scale, self.use_selection,
                self.apply_modifiers, self.source_file,
                self.allow_missing_textures, True,
                self.report, False, False,
                position_format, normal_format, uv_format,
                position_frac, normal_frac, uv_frac)
        except (blender_export.ExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
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
        if self.use_selection:
            active = context.active_object
            arm = active if active is not None and active.type == "ARMATURE" else None
            if arm is None:
                arm = next((o for o in context.selected_objects
                            if o.type == "ARMATURE"), None)
            mesh = bc.model_mesh(context, arm)
        else:
            mesh = next((o for o in context.scene.objects
                         if o.type == "MESH" and o.visible_get()), None)
        last = bc.last_export_path(mesh, "kcl")
        fallback = mesh.name if mesh is not None else "room"
        self.filepath = last or bpy.path.ensure_ext(fallback, self.filename_ext)
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

    def execute(self, context):
        from . import bck_import_ui

        paths = []

        if self.files:
            directory = self.directory or os.path.dirname(self.filepath)
            for element in self.files:
                if element.name:
                    paths.append(os.path.join(directory, element.name))
        elif self.filepath:
            paths.append(self.filepath)

        if not paths:
            self.report({"ERROR"}, "No BCK files selected")
            return {"CANCELLED"}

        return bck_import_ui.begin_import(context, paths, self)


class EXPORT_OT_bck(Operator, ExportHelper):
    """Export the active action of the active armature as a J3D .bck animation"""
    bl_idname = "export_anim.bck"
    bl_label = "Export BCK"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".bck"
    filter_glob: StringProperty(default="*.bck", options={"HIDDEN"})

    def invoke(self, context, event):
        from . import bck_common

        try:
            arm = bck_common.find_armature(context)
        except bck_common.AnimError:
            arm = None

        action = arm.animation_data.action if arm and arm.animation_data else None
        last = bc.last_export_path(action, "bck")

        if action is not None:
            self.filepath = bpy.path.ensure_ext(action.name, self.filename_ext)
        elif last:
            self.filepath = last
        elif arm is not None:
            mesh = bc.model_mesh(context, arm)
            fallback = mesh.name if mesh is not None else "animation"
            self.filepath = bpy.path.ensure_ext(fallback, self.filename_ext)
        else:
            self.filepath = bpy.path.ensure_ext("animation", self.filename_ext)

        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        from . import bck_export_ui, bck_common

        settings = context.scene.bmd_bck_export_settings
        settings.filepath = self.filepath

        try:
            settings.target = bck_common.find_armature(context)
        except bck_common.AnimError:
            settings.target = None

        bpy.app.timers.register(
            bck_export_ui.open_export_dialog, first_interval=0.05)
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
        from . import blk_import_ui

        if not self.filepath:
            self.report({"ERROR"}, "No BLK file selected")
            return {"CANCELLED"}

        return blk_import_ui.begin_import(context, self.filepath, self)


class EXPORT_OT_blk(Operator, ExportHelper):
    """Export the active shape-key action as a J3D .blk animation"""
    bl_idname = "export_anim.blk"
    bl_label = "Export BLK"
    bl_options = {"REGISTER", "PRESET"}

    filename_ext = ".blk"
    filter_glob: StringProperty(default="*.blk", options={"HIDDEN"})

    def invoke(self, context, event):
        target = bc.export_target_mesh(context, "blk")
        action = None
        if target is not None and target.data and target.data.shape_keys:
            animation_data = target.data.shape_keys.animation_data
            action = animation_data.action if animation_data else None
        last = bc.last_export_path(action, "blk")
        if action is not None:
            self.filepath = bpy.path.ensure_ext(action.name, self.filename_ext)
        elif last:
            self.filepath = last
        elif target is not None:
            self.filepath = bpy.path.ensure_ext(target.name, self.filename_ext)
        else:
            self.filepath = bpy.path.ensure_ext("animation", self.filename_ext)
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        from . import blk_export_ui, bls_export_ui

        if not self.filepath:
            self.report({"ERROR"}, "No BLK file selected")
            return {"CANCELLED"}

        settings = bls_export_ui._get_export_settings(context)

        if settings is None:
            self.report({"ERROR"}, "Export settings are unavailable")
            return {"CANCELLED"}

        target = bc.export_target_mesh(context, "blk")

        if target is not None:
            settings.target = target

        settings.blk_filepath = self.filepath
        settings.show_base = False
        bls_export_ui._populate_export_keys(settings, settings.target)

        bpy.app.timers.register(blk_export_ui.open_export_dialog,
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
        owner = _texture_owner(context)
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
            box.label(text="No textures", icon="INFO")
        if owner is None:
            box.label(text="No BMD texture list for this material", icon="INFO")
        elif not items:
            box.label(text="Assign textures in BMD Textures first", icon="INFO")
        elif len(slots) >= 8:
            box.label(text="All 8 texture slots are used", icon="INFO")
        else:
            add = box.operator(BMD_OT_material_texture_menu.bl_idname,
                               text="Add Texture", icon="ADD")
            add.owner_name = owner.name
            add.add = True


def _draw_bmd_prop(layout, data, prop, label, factor=0.42, alert=False):
    """Label + property field with a fixed-width label column."""
    row = layout.split(factor=factor)
    row.alert = alert
    row.label(text=label)
    row.prop(data, prop, text="")


def _draw_bmd_image_settings(layout, img, hint=False):
    _draw_bmd_prop(layout, img.bmd, "format", "GX Format", 0.3)
    layout.prop(img.bmd, "mip_count")
    layout.prop(img.bmd, "external")
    if hint:
        layout.label(text="Order and assignment: J3D sidebar > BMD Textures",
                     icon="INFO")
    box = layout.box()
    box.label(text="Sampling", icon="TEXTURE")
    _draw_bmd_prop(box, img.bmd, "wrap_s", "Wrap S", 0.3)
    _draw_bmd_prop(box, img.bmd, "wrap_t", "Wrap T", 0.3)
    _draw_bmd_prop(box, img.bmd, "min_filter", "Min Filter", 0.3)
    _draw_bmd_prop(box, img.bmd, "mag_filter", "Mag Filter", 0.3)
    _draw_bmd_prop(box, img.bmd, "min_lod", "Min LOD", 0.3)
    _draw_bmd_prop(box, img.bmd, "max_lod", "Max LOD", 0.3)
    _draw_bmd_prop(box, img.bmd, "lod_bias", "LOD Bias", 0.3)


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
        _draw_bmd_image_settings(self.layout, img, hint=True)
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

        _draw_format_box(layout, "Shape Keys (.bls)", (
            ("bmd_shape_keys.import_files", "Import Shape Keys", "IMPORT"),
            ("bmd_shape_keys.import_folder", "Import Shape Keys (Folder)", "IMPORT"),
            ("bmd_shape_keys.export_files", "Export Shape Keys", "EXPORT"),
        ))

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


def _selected_model(context):
    """The BMD model targeted by both sidebar panels.

    A mesh with an armature resolves to the armature.  Selecting an unrelated
    object shows no model; clearing the selection keeps the last valid model.
    """
    global _LAST_MODEL
    active = getattr(context, "active_object", None)
    if active is not None:
        if active.type == "ARMATURE":
            _LAST_MODEL = active
            return active
        if active.type == "MESH":
            model = bc.target_armature(active) or active
            _LAST_MODEL = model
            return model
        return None
    selected = list(getattr(context, "selected_objects", ()) or ())
    for obj in selected:
        if obj.type == "ARMATURE":
            _LAST_MODEL = obj
            return obj
        if obj.type == "MESH":
            model = bc.target_armature(obj) or obj
            _LAST_MODEL = model
            return model
    if selected:
        return None
    model = _valid_model(_LAST_MODEL)
    if model is None:
        _LAST_MODEL = None
    return model


def _format_owner(context):
    return _selected_model(context)


def _texture_owner(context):
    return _selected_model(context)


def _active_material(context):
    mat = getattr(context, "material", None)
    if mat is not None:
        return mat
    obj = getattr(context, "active_object", None)
    if obj is not None:
        return obj.active_material
    return None


def _populate_slot_maps():
    """Fill missing material->BMD texture slot maps outside UI draw callbacks."""
    try:
        owner = _texture_owner(bpy.context)
        if owner is not None:
            for material in bpy.data.materials:
                if not material.get("bmd_texture_slots"):
                    bc.material_texture_slots(material, owner)
    except Exception:
        pass
    return 1.0


def _texture_items(owner):
    """[(BMD texture index, slot, image)] for slots that have an image."""
    if owner is None:
        return []
    coll = bc.ensure_texture_list(owner)
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


def _ensure_texture_node(mat, slot):
    if mat.node_tree is None:
        mat.use_nodes = True
    tree = mat.node_tree
    node = tree.nodes.get(f"bmd_tex{slot}")
    if node is not None and node.type != "TEX_IMAGE":
        tree.nodes.remove(node)
        node = None
    if node is None:
        node = tree.nodes.new("ShaderNodeTexImage")
        node.name = f"bmd_tex{slot}"
        node.label = f"bmd_tex{slot}"
        node.location = (-300, 300 - 280 * slot)
        bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is not None and not bsdf.inputs["Base Color"].is_linked:
            tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    return node


def _set_material_texture(mat, slot, image, texture_index, owner=None):
    node = _ensure_texture_node(mat, slot)
    node.image = image
    _set_material_texname(mat, slot, bc.texture_key(image))
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
    bl_options = {"REGISTER", "UNDO"}

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
    bl_options = {"REGISTER", "UNDO"}

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
    bl_options = {"REGISTER", "UNDO"}

    slot: IntProperty(name="Slot", default=0, min=0, max=7)

    def execute(self, context):
        mat = _active_material(context)
        if mat is None:
            return {"CANCELLED"}
        owner = _texture_owner(context)
        tree = mat.node_tree
        if tree is not None:
            node = tree.nodes.get(f"bmd_tex{self.slot}")
            if node is not None:
                tree.nodes.remove(node)
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
    bl_label = "Show BMD Texture Settings"
    bl_description = "Select this texture in the BMD Textures panel"
    bl_options = {"REGISTER", "UNDO"}

    owner_name: StringProperty(name="Owner", default="")
    texture_index: IntProperty(name="Texture Index", default=-1)

    def execute(self, context):
        owner = (bpy.data.objects.get(self.owner_name)
                 if self.owner_name else _texture_owner(context))
        if owner is None:
            return {"CANCELLED"}
        coll = bc.ensure_texture_list(owner)
        if not (0 <= self.texture_index < len(coll)):
            return {"CANCELLED"}
        owner.bmd_texture_index = self.texture_index
        obj = getattr(context, "object", None)
        if obj is None or obj.type != "MESH":
            obj = owner if owner.type == "MESH" else _texture_mesh(context, owner)
        if obj is not None:
            context.view_layer.objects.active = obj
            obj.select_set(True)
        screen = getattr(context, "screen", None)
        area = (next((a for a in screen.areas if a.type == "VIEW_3D"), None)
                if screen is not None else None)
        if area is None:
            self.report({"INFO"}, "Open a 3D View to see the BMD Textures panel")
            return {"FINISHED"}
        area.spaces.active.show_region_ui = True
        _activate_j3d_sidebar(area)
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
        items = _texture_items(owner)
        if not items:
            self.report({"INFO"}, "No textures assigned in BMD Textures")
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
        key = bc.texture_slot_key(item)
        duplicate = bc.texture_is_duplicate(data, key)
        row = layout.row(align=True)
        row.alert = duplicate
        index_col = row.column()
        index_col.ui_units_x = 2.0
        index_col.label(text=f"{index:02d}")
        if duplicate:
            row.label(text="", icon="ERROR")
        name = item.name or (item.image.name if item.image is not None else "(no image)")
        row.label(text=name)


class BMD_OT_texture_add(Operator):
    bl_idname = "bmd.texture_add"
    bl_label = "Add BMD Texture"
    bl_description = "Add an empty texture slot to the exported BMD texture list"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        arm = _texture_owner(context)
        if arm is None:
            self.report({"ERROR"}, "Select the imported armature first")
            return {"CANCELLED"}
        coll = bc.ensure_texture_list(arm)
        slot = coll.add()
        slot.name = f"texture{len(coll) - 1}"
        arm.bmd_texture_index = len(coll) - 1
        bc.sync_texture_legacy(arm)
        return {"FINISHED"}


class BMD_OT_texture_remove(Operator):
    bl_idname = "bmd.texture_remove"
    bl_label = "Remove BMD Texture"
    bl_description = "Remove the selected texture from the BMD texture list"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        arm = _texture_owner(context)
        if arm is None:
            return {"CANCELLED"}
        coll = bc.ensure_texture_list(arm)
        index = self.index if self.index >= 0 else int(arm.bmd_texture_index)
        if not (0 <= index < len(coll)):
            return {"CANCELLED"}
        for material in bpy.data.materials:
            bc.material_texture_slots(material, arm)
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
    bl_options = {"REGISTER", "UNDO"}

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
    bl_options = {"REGISTER", "UNDO"}

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
        for material in bpy.data.materials:
            bc.material_texture_slots(material, arm)
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
    bl_order = 1
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
            layout.label(text="Select a BMD Model", icon="INFO")
            return
        formats = bc.ensure_vertex_formats(owner, read_only=True)
        if formats is None:
            layout.label(text="Select a BMD Model", icon="INFO")
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


class BMD_PT_textures(Panel):
    bl_label = "BMD Textures"
    bl_order = 2
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
            layout.label(text="Select a BMD Model", icon="INFO")
            return
        coll = bc.ensure_texture_list(owner)
        row = layout.row(align=True)
        row.operator(BMD_OT_texture_add.bl_idname, text="Add", icon="ADD")
        row.operator(BMD_OT_texture_remove.bl_idname, text="Remove", icon="REMOVE")
        row.operator(BMD_OT_texture_move.bl_idname, text="", icon="TRIA_UP").direction = "UP"
        row.operator(BMD_OT_texture_move.bl_idname, text="", icon="TRIA_DOWN").direction = "DOWN"
        if not len(coll):
            layout.label(text="No textures in this model", icon="INFO")
            return
        layout.template_list("BMD_UL_textures", "", owner, "bmd_texture_list",
                             owner, "bmd_texture_index", rows=8)
        duplicates = bc.texture_duplicates(owner)
        if duplicates:
            box = layout.box()
            box.alert = True
            box.label(text="Duplicate textures", icon="ERROR")
            for key, indices in list(duplicates.items())[:6]:
                first = coll[indices[0]]
                name = first.name or (first.image.name if first.image is not None else key)
                box.label(text=f"{name}: slots {', '.join(str(i) for i in indices)}")
            if len(duplicates) > 6:
                box.label(text=f"... and {len(duplicates) - 6} more")
        index = min(max(int(owner.bmd_texture_index), 0), len(coll) - 1)
        slot = coll[index]
        key = bc.texture_slot_key(slot)
        duplicate = bc.texture_is_duplicate(owner, key)
        box = layout.box()
        box.label(text=f"Texture {index}", icon="TEXTURE")
        if slot.image is None:
            box.label(text="No image (skipped on export)", icon="ERROR")
        if duplicate:
            box.label(text="Also used by another slot", icon="ERROR")
        _draw_bmd_prop(box, slot, "name", "BMD Name", 0.3, alert=duplicate)
        image_col = box.column()
        image_col.alert = duplicate
        if slot.image is not None:
            try:
                slot.image.preview_ensure()
            except Exception:
                pass
        image_col.template_ID_preview(slot, "image", new="", open="",
                                      unlink=BMD_OT_texture_clear_image.bl_idname)
        if key:
            box.label(text=f"Used by {bc.material_slot_users(owner, index)} material slot(s)",
                      icon="MATERIAL")
        if slot.image is not None:
            settings = box.box()
            settings.label(text="Texture Settings", icon="PREFERENCES")
            _draw_bmd_image_settings(settings, slot.image)


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
            IMAGE_PT_bmd, BMD_PT_j3d, BMD_UL_textures,
            BMD_OT_texture_add, BMD_OT_texture_remove,
            BMD_OT_texture_clear_image, BMD_OT_texture_move,
            BMD_OT_material_set_texture, BMD_OT_material_add_texture,
            BMD_OT_material_remove_texture, BMD_OT_material_texture_menu,
            BMD_OT_texture_focus, BMD_PT_formats, BMD_PT_textures)
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
    try:
        bc.initialize_vertex_formats()
    except Exception:
        pass
    if _load_post_initialize_vertex_formats not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post_initialize_vertex_formats)
    for c in _classes:
        bpy.utils.register_class(c)
    try:
        if not bpy.app.timers.is_registered(_populate_slot_maps):
            bpy.app.timers.register(_populate_slot_maps, first_interval=1.0,
                                    persistent=True)
    except Exception:
        pass

    from . import (bck_export_ui, bck_import_ui, blk_export_ui,
                   blk_import_ui, bls_import_ui, bls_export_ui)
    for c in bck_export_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in bck_import_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in bls_import_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in bls_export_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in blk_import_ui.CLASSES:
        bpy.utils.register_class(c)
    for c in blk_export_ui.CLASSES:
        bpy.utils.register_class(c)
    blk_export_ui.register_hooks()
    bpy.types.Scene.bmd_shape_keys_settings = PointerProperty(
        type=bls_import_ui.BMD_SK_Settings)
    bpy.types.Scene.bmd_bck_import_settings = PointerProperty(
        type=bck_import_ui.BMD_ANIM_BckImportSettings)
    bpy.types.Scene.bmd_bck_export_settings = PointerProperty(
        type=bck_export_ui.BMD_ANIM_BCKExportSettings)
    bpy.types.Scene.bmd_shape_keys_export_settings = PointerProperty(
        type=bls_export_ui.BMD_SK_ExportSettings)
    bpy.types.Scene.bmd_blk_import_settings = PointerProperty(
        type=blk_import_ui.BMD_SK_BlkImportSettings)
    bpy.types.Scene.bmd_blk_export_settings = PointerProperty(
        type=blk_export_ui.BMD_SK_BlkExportSettings)

    _file_handlers = _make_file_handlers()
    for fh in _file_handlers:
        bpy.utils.register_class(fh)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)
    bpy.types.IMAGE_MT_image.append(menu_image_import_bti)
    bpy.types.NODE_MT_context_menu.append(menu_node_import_bti)


def unregister():
    global _file_handlers
    try:
        if bpy.app.timers.is_registered(_populate_slot_maps):
            bpy.app.timers.unregister(_populate_slot_maps)
    except Exception:
        pass
    try:
        bpy.app.handlers.load_post.remove(_load_post_initialize_vertex_formats)
    except ValueError:
        pass
    bpy.types.NODE_MT_context_menu.remove(menu_node_import_bti)
    bpy.types.IMAGE_MT_image.remove(menu_image_import_bti)
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    for fh in _file_handlers:
        bpy.utils.unregister_class(fh)
    _file_handlers = []

    from . import (bck_export_ui, bck_import_ui, blk_export_ui,
                   blk_import_ui, bls_import_ui, bls_export_ui)
    if hasattr(bpy.types.Scene, "bmd_blk_import_settings"):
        del bpy.types.Scene.bmd_blk_import_settings
    if hasattr(bpy.types.Scene, "bmd_blk_export_settings"):
        del bpy.types.Scene.bmd_blk_export_settings
    if hasattr(bpy.types.Scene, "bmd_bck_export_settings"):
        del bpy.types.Scene.bmd_bck_export_settings
    if hasattr(bpy.types.Scene, "bmd_bck_import_settings"):
        del bpy.types.Scene.bmd_bck_import_settings
    if hasattr(bpy.types.Scene, "bmd_shape_keys_export_settings"):
        del bpy.types.Scene.bmd_shape_keys_export_settings
    if hasattr(bpy.types.Scene, "bmd_shape_keys_settings"):
        del bpy.types.Scene.bmd_shape_keys_settings
    for c in reversed(bck_export_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(bck_import_ui.CLASSES):
        bpy.utils.unregister_class(c)
    blk_export_ui.unregister_hooks()
    for c in reversed(blk_export_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(blk_import_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(bls_export_ui.CLASSES):
        bpy.utils.unregister_class(c)
    for c in reversed(bls_import_ui.CLASSES):
        bpy.utils.unregister_class(c)

    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
    bc.unregister_classes()
