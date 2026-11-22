"""J3D BMD Import/Export for Blender (Zelda: Twilight Princess / Dusklight).

Includes BMD/BDL models, BCK animations, BLS shape keys and KCL collision.
"""

import os

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, IntProperty,
                       StringProperty)
from bpy.types import Operator, OperatorFileListElement, PropertyGroup
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import blender_common as bc

_PENDING_BMD_IMPORT = None


def _select_imported_model(context, arm):
    """Leave the imported model active and selected after a BMD import.

    Every sidebar panel resolves its target from the active/selected model.
    Making the imported armature active (orange outline) instead of only
    selected (red outline) keeps Vertex Formats, Textures and Animations on the
    file that was just imported.
    """
    if arm is None:
        return

    view_layer = context.view_layer
    for obj in view_layer.objects:
        obj.select_set(False)

    view_layer.objects.active = arm
    arm.select_set(True)


@bpy.app.handlers.persistent
def _load_post_initialize_vertex_formats(*_args):
    try:
        bc.initialize_vertex_formats()
    except Exception:
        pass


@bpy.app.handlers.persistent
def _migrate_texture_lists(*_args):
    """Rebuild legacy texture arrays outside UI draw.

    ``ensure_texture_list`` writes to the object, which Blender forbids while
    a panel is drawn.  Saved files may still store only ``bmd_textures`` /
    ``bmd_texture_keys``, so migrate them after loading (and once at add-on
    registration) instead of from the Textures panel.
    """
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            owner = bc.target_armature(obj) or obj
        elif obj.type == "ARMATURE":
            owner = obj
        else:
            continue

        if not bc.is_bmd_object(owner):
            continue

        try:
            bc.ensure_texture_list(owner)
        except Exception:
            pass


class IMPORT_OT_bmd(Operator, ImportHelper):
    """Import a J3D BMD3/BDL4 model"""
    bl_idname = "import_scene.bmd"
    bl_label = "Import BMD"
    bl_options = {"UNDO"}

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

    def invoke(self, context, event):
        self.convert_units = bc.saved_convert_units(context)
        return ImportHelper.invoke(self, context, event)

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

        last_arm = None
        for p in paths:
            try:
                arm = blender_import.import_bmd(
                    context, p, scale, self.bone_axis,
                    self.import_textures, report=self.report)
            except J3DError as exc:
                self.report({"ERROR"}, f"{os.path.basename(p)}: {exc}")
                return {"CANCELLED"}

            last_arm = arm

            if self.import_shape_keys:
                from . import bls_import
                bls_import.import_shape_keys_for_bmd(
                    context, arm, p, scale, report=self.report)

        _select_imported_model(context, last_arm)
        bc.remember_convert_units(context, self.convert_units, last_arm)
        return {"FINISHED"}


class BMDImportFileState(PropertyGroup):
    expanded: BoolProperty(default=True)


class IMPORT_OT_bmd_texc(Operator):
    bl_idname = "import_scene.bmd_texc"
    bl_label = "Import Settings"
    bl_description = "Choose the folder that contains the external .bti textures"
    bl_options = {"UNDO", "INTERNAL"}

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

        last_arm = None
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

            last_arm = arm

            if pending.get("import_shape_keys", True):
                from . import bls_import
                bls_import.import_shape_keys_for_bmd(
                    context, arm, path, pending["scale"],
                    report=self.report)

        _PENDING_BMD_IMPORT = None
        _select_imported_model(context, last_arm)
        bc.remember_convert_units(
            context, pending.get("convert_units", True), last_arm)
        return {"FINISHED"}

    def cancel(self, context):
        global _PENDING_BMD_IMPORT
        _PENDING_BMD_IMPORT = None


class EXPORT_OT_bmd(Operator, ExportHelper):
    """Export meshes (and their armature) as a J3D BMD3 model"""
    bl_idname = "export_scene.bmd"
    bl_label = "Export BMD"
    bl_options = set()

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
        mesh = bc.export_target_mesh(context, "bmd")
        self.convert_units = bc.saved_convert_units(context, mesh)
        last = bc.last_export_path(mesh, "bmd")
        fallback = mesh.name if mesh is not None else "model"
        self.filepath = last or bpy.path.ensure_ext(fallback, self.filename_ext)
        result = ExportHelper.invoke(self, context, _event)
        self.convert_units = bc.saved_convert_units(context, mesh)
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
        armatures, meshes, valid = bc.bmd_export_objects(
            context, self.use_selection)

        if not valid:
            self.report({"ERROR"}, bc.BMD_NO_MESH_ERROR)
            return {"CANCELLED"}

        owner = armatures[0] if armatures else (
            meshes[0] if meshes else None)
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
        bc.remember_convert_units(context, self.convert_units, owner)
        return {"FINISHED"}


class IMPORT_OT_kcl(Operator, ImportHelper):
    """Import a Twilight Princess collision mesh (room.kcl, optionally with room.plc)"""
    bl_idname = "import_scene.kcl"
    bl_label = "Import KCL"
    bl_options = {"UNDO"}

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
            obj = blender_kcl.import_kcl(
                context, self.filepath, scale, self.plc_file, self.report)
        except J3DError as exc:
            self.report({"ERROR"}, f"{os.path.basename(self.filepath)}: {exc}")
            return {"CANCELLED"}

        if obj is not None and "bmd_convert_units" in obj:
            del obj["bmd_convert_units"]

        bc.remember_convert_units(context, self.convert_units, obj)
        return {"FINISHED"}


class EXPORT_OT_kcl(Operator, ExportHelper):
    """Export meshes as a Twilight Princess collision file (room.kcl). Keep the original room.plc"""
    bl_idname = "export_scene.kcl"
    bl_label = "Export KCL"
    bl_options = set()

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
        from . import blender_kcl

        kcl_objs, _valid = blender_kcl.kcl_export_objects(
            context, self.use_selection)
        target = kcl_objs[0] if kcl_objs else None
        self.convert_units = bc.saved_convert_units(context, target)

        if target is not None:
            mesh = target
        else:
            mesh = bc.export_target_mesh(context, "kcl")
        last = bc.last_export_path(mesh, "kcl")
        fallback = mesh.name if mesh is not None else "room"
        self.filepath = last or bpy.path.ensure_ext(fallback, self.filename_ext)
        result = ExportHelper.invoke(self, context, event)
        self.convert_units = bc.saved_convert_units(context, target)
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
        objs, _valid = blender_kcl.kcl_export_objects(
            context, self.use_selection)
        target = objs[0] if objs else None
        try:
            blender_kcl.export_kcl(context, self.filepath, scale, self.use_selection,
                                   self.apply_modifiers, self.default_attr, self.keep_area, self.report)
        except (blender_kcl.KclExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        bc.remember_convert_units(context, self.convert_units, target)
        return {"FINISHED"}


_classes = (IMPORT_OT_bmd, BMDImportFileState, IMPORT_OT_bmd_texc,
            EXPORT_OT_bmd, IMPORT_OT_kcl, EXPORT_OT_kcl)
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
            bl_import_operator = "import_anim.j3d"
            bl_file_extensions = ".bck"

            @classmethod
            def poll_drop(cls, context):
                return poll_file_object_drop(context)

        class BLK_FH_import(bpy.types.FileHandler):
            bl_idname = "BLK_FH_import"
            bl_label = "J3D BLK Shape Key Animation"
            bl_import_operator = "import_anim.j3d"
            bl_file_extensions = ".blk"

            @classmethod
            def poll_drop(cls, context):
                return poll_file_object_drop(context)

        class BTP_FH_import(bpy.types.FileHandler):
            bl_idname = "BTP_FH_import"
            bl_label = "J3D BTP Texture Animation"
            bl_import_operator = "import_anim.j3d"
            bl_file_extensions = ".btp"

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

        return [BMD_FH_import, BCK_FH_import, BLK_FH_import, BTP_FH_import,
                KCL_FH_import, BLS_FH_import, BTI_FH_import]
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
    if _migrate_texture_lists not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_migrate_texture_lists)

    try:
        if not bpy.app.timers.is_registered(_migrate_texture_lists):
            bpy.app.timers.register(_migrate_texture_lists,
                                    first_interval=0.05, persistent=True)
    except Exception:
        pass
    for c in _classes:
        bpy.utils.register_class(c)

    from . import ui
    ui.register()

    from . import btp_preview
    btp_preview.install()

    _file_handlers = _make_file_handlers()
    for fh in _file_handlers:
        try:
            bpy.utils.register_class(fh)
        except Exception:
            import traceback
            traceback.print_exc()


def unregister():
    global _file_handlers
    try:
        from . import btp_preview
        btp_preview.uninstall()
    except Exception:
        pass
    try:
        if bpy.app.timers.is_registered(_migrate_texture_lists):
            bpy.app.timers.unregister(_migrate_texture_lists)
    except Exception:
        pass
    try:
        bpy.app.handlers.load_post.remove(_load_post_initialize_vertex_formats)
    except ValueError:
        pass
    try:
        bpy.app.handlers.load_post.remove(_migrate_texture_lists)
    except ValueError:
        pass
    from . import ui
    ui.unregister()
    for fh in _file_handlers:
        try:
            bpy.utils.unregister_class(fh)
        except Exception:
            pass
    _file_handlers = []

    for c in reversed(_classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    bc.unregister_classes()
