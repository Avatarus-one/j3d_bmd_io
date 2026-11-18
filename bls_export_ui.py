"""Export UI for J3D BLS shape keys."""

from __future__ import annotations

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup, UIList
from bpy_extras.io_utils import ExportHelper

from . import blender_common as bc
from . import bls_export
from . import bmd_metadata


MAX_EXPORT_ROWS = 10


def _get_export_settings(context=None):
    context = context or bpy.context
    scene = getattr(context, 'scene', None)
    return getattr(scene, 'bmd_shape_keys_export_settings', None)


_validation_cache = {'key': None, 'errors': []}


def _validation_key(settings, key_names, metadata_key):
    target = settings.target
    mesh = (target.data
            if target is not None and target.type == 'MESH' else None)
    shape_keys = mesh.shape_keys if mesh is not None else None
    return (
        target.name if target is not None else None,
        mesh.name if mesh is not None else None,
        len(mesh.vertices) if mesh is not None else 0,
        len(shape_keys.key_blocks) if shape_keys is not None else 0,
        tuple(key_names),
        settings.base_name,
        settings.convert_units,
        metadata_key,
    )


def _cached_validation(settings, key_names, metadata_key):
    """Validate the export once per distinct settings combination."""
    key = _validation_key(settings, key_names, metadata_key)

    if _validation_cache['key'] != key:
        try:
            errors = bls_export.validate_export(
                settings.target, key_names, settings.base_name,
                settings.convert_units)
        except Exception as exc:  # pragma: no cover - defensive UI guard
            errors = [f'Validation failed: {exc}']

        _validation_cache['errors'] = errors
        _validation_cache['key'] = key

    return _validation_cache['errors']


def _rebuild_export_tree(settings):
    """Rebuild the scrollable tree: mesh root, optional base key, shape keys."""
    settings.tree_rows.clear()
    mesh_row = settings.tree_rows.add()
    mesh_row.kind = 'mesh'

    if settings.show_base and settings.keys:
        base_row = settings.tree_rows.add()
        base_row.kind = 'base'

    for index, _item in enumerate(settings.keys):
        row = settings.tree_rows.add()
        row.kind = 'key'
        row.key_index = index

    settings.active_tree_row = 0


def _populate_export_keys(settings, target=None):
    """Fill the export key list from the target mesh's shape keys."""
    if settings is None:
        return

    previous = {item.name: item.include for item in settings.keys}
    settings.keys.clear()
    settings.base_include = True
    target = target if target is not None else settings.target

    if target is not None and target.type == 'MESH' and target.data is not None:
        shape_keys = target.data.shape_keys
        if shape_keys is not None:
            for block in shape_keys.key_blocks[1:]:
                item = settings.keys.add()
                item.name = block.name
                item.include = previous.get(block.name, True)

    _rebuild_export_tree(settings)


def _default_base_name(target):
    """Default base key name: the target mesh's basis key, if present."""
    if target is None or target.data is None:
        return ''

    shape_keys = target.data.shape_keys

    if shape_keys is not None and len(shape_keys.key_blocks):
        return shape_keys.key_blocks[0].name

    name = target.data.name or target.name
    return f'{name}__base'

_target_changed_hooks = []


def add_target_changed_hook(callback):
    if callback not in _target_changed_hooks:
        _target_changed_hooks.append(callback)


def remove_target_changed_hook(callback):
    try:
        _target_changed_hooks.remove(callback)
    except ValueError:
        pass


def _export_target_changed(self, context):
    """Repopulate the export list when the target mesh changes."""
    settings = _get_export_settings(context)
    _populate_export_keys(settings, self.target)

    if settings is not None:
        settings.base_name = _default_base_name(self.target)

    for callback in list(_target_changed_hooks):
        try:
            callback(context, self.target)
        except Exception:
            pass


class BMD_SK_ExportKeyItem(PropertyGroup):
    name: StringProperty()
    include: BoolProperty(
        name='Export key',
        description='Include this shape key in the BLS file',
        default=True,
    )


class BMD_SK_ExportTreeRow(PropertyGroup):
    kind: StringProperty()
    key_index: IntProperty(default=0)


class BMD_SK_ExportSettings(PropertyGroup):
    target: PointerProperty(
        name='Target Mesh',
        description='Mesh whose shape keys will be exported',
        type=bpy.types.Object,
        poll=bc.mesh_object_poll,
        update=_export_target_changed,
    )
    filepath: StringProperty(subtype='FILE_PATH')
    blk_filepath: StringProperty(subtype='FILE_PATH')
    show_base: BoolProperty(default=True)
    convert_units: BoolProperty(
        name='Convert Units',
        description='Convert shape key positions to J3D units '
                    '(100 scale). Disable for model-unit meshes',
        default=True,
    )
    base_name: StringProperty(
        name='Base Key Name',
        description='Name of the trailing base key in the exported BLS',
        default='',
        maxlen=63,
    )
    expanded: BoolProperty(
        name='Expanded',
        description='Show the shape keys of the target mesh',
        default=True,
    )
    base_include: BoolProperty(
        name='',
        description='The base key is always included in the exported BLS.',
        default=True,
    )
    keys: CollectionProperty(type=BMD_SK_ExportKeyItem)
    tree_rows: CollectionProperty(type=BMD_SK_ExportTreeRow)
    active_tree_row: IntProperty(default=0, min=0)


class BMD_SK_UL_export_tree(UIList):
    """Scrollable BLS export tree: mesh root, base key and shape keys."""

    bl_idname = 'BMD_SK_UL_export_tree'

    def filter_items(self, context, data, propname):
        rows = getattr(data, propname)
        expanded = data.expanded
        flags = []
        for row in rows:
            if row.kind == 'mesh' or expanded:
                flags.append(self.bitflag_filter_item)
            else:
                flags.append(0)
        return flags, []

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index, flt_flag=0):
        settings = data
        if item.kind == 'mesh':
            row = layout.row(align=True)
            row.alignment = 'LEFT'
            mesh_name = (settings.target.name
                         if settings.target is not None else 'No target mesh')
            row.prop(
                settings, 'expanded', text=mesh_name,
                icon='TRIA_DOWN' if settings.expanded else 'TRIA_RIGHT',
                emboss=False,
            )
            return

        row = layout.row(align=True)
        row.label(text='', icon='BLANK1')
        if item.kind == 'base':
            check = row.row()
            check.enabled = False
            check.prop(settings, 'base_include', text='')
            row.label(text=settings.base_name, icon='SHAPEKEY_DATA')
            return

        key = (settings.keys[item.key_index]
               if 0 <= item.key_index < len(settings.keys) else None)
        if key is None:
            return
        check = row.row()
        check.prop(key, 'include', text='')
        row.label(text=key.name, icon='SHAPEKEY_DATA')


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def open_export_dialog():
    """Timer callback: open the BLS export dialog after the file browser."""
    try:
        bpy.ops.bmd_shape_keys.export_settings('INVOKE_DEFAULT')
    except Exception as exc:  # pragma: no cover - only in unusual UI states
        print(f'BLS exporter: could not open settings dialog: {exc}')

    return None


def _export_mesh_target(context):
    """Selected mesh/armature, last BLS export mesh, or the first scene mesh."""
    return bc.export_target_mesh(context, "bls")


class BMD_SK_OT_export_files(Operator, ExportHelper):
    bl_idname = 'bmd_shape_keys.export_files'
    bl_label = 'Export BLS'
    bl_description = 'Export Blender shape keys to a J3D .bls file'
    bl_options = {'REGISTER'}

    filename_ext = '.bls'
    filter_glob: StringProperty(default='*.bls', options={'HIDDEN'})

    def invoke(self, context, _event):
        target = _export_mesh_target(context)

        if target is not None:
            imported = bc.export_stem(target.get("bmd_bls_source", ""), "")
            last = bc.last_export_path(target, "bls")
            if last:
                self.filepath = last
            elif imported:
                self.filepath = bpy.path.ensure_ext(imported, self.filename_ext)
            else:
                fallback = (target.name
                            or (target.data.name if target.data else ''))
                if fallback:
                    self.filepath = bpy.path.ensure_ext(
                        fallback, self.filename_ext)

        return ExportHelper.invoke(self, context, _event)

    def execute(self, context):
        target = _export_mesh_target(context)

        settings = _get_export_settings(context)

        if settings is None:
            self.report({'ERROR'}, 'Export settings are unavailable')
            return {'CANCELLED'}

        settings.filepath = self.filepath

        if target is not None:
            settings.target = target

        settings.convert_units = bc.saved_convert_units(context, settings.target)
        settings.show_base = True
        _populate_export_keys(settings, settings.target)

        if not settings.base_name.strip():
            settings.base_name = _default_base_name(settings.target)

        bpy.app.timers.register(open_export_dialog, first_interval=0.05)
        return {'FINISHED'}


def _perform_export(settings, operator, context):
    if settings is None or settings.target is None:
        operator.report({'ERROR'}, 'Select a target mesh')
        return {'CANCELLED'}

    if not settings.filepath:
        operator.report({'ERROR'}, 'Select an export file')
        return {'CANCELLED'}

    names = [item.name for item in settings.keys if item.include]

    if not names:
        operator.report({'WARNING'}, 'No shape keys selected for export')
        return {'CANCELLED'}

    try:
        errors = bls_export.validate_export(
            settings.target, names, settings.base_name, settings.convert_units)
    except Exception as exc:
        operator.report({'ERROR'}, f'Validation failed: {exc}')
        return {'CANCELLED'}

    if errors:
        for error in errors:
            operator.report({'ERROR'}, error)
        return {'CANCELLED'}

    try:
        stats = bls_export.export_bls(
            settings.target,
            settings.filepath,
            names,
            convert_units=settings.convert_units,
            base_name=settings.base_name.strip(),
        )
    except Exception as exc:
        operator.report({'ERROR'}, f'Export failed: {exc}')
        return {'CANCELLED'}

    info = (
        f'BLS export - keys: {stats["keys"]}, '
        f'vertices: {stats["vertices"]}, '
        f'file: {stats["bytes"]} bytes'
    )
    context.scene.bmd_shape_keys_last_info = info
    operator.report({'INFO'}, info)
    return {'FINISHED'}


def _draw_export_dialog(layout, settings):
    if settings is None:
        layout.label(text='Export settings are unavailable', icon='ERROR')
        return

    box = layout.box()
    box.label(text='Export settings', icon='PREFERENCES')

    target_row = box.row()
    target_row.alert = settings.target is None
    target_row.prop(settings, 'target')
    box.prop(settings, 'convert_units')

    if settings.target is None:
        layout.label(text='Select a target mesh to see its shape keys',
                     icon='INFO')
        return

    if not settings.keys:
        layout.label(text='No shape keys found', icon='ERROR')
        return

    visible = len(settings.tree_rows) if settings.expanded else 1
    rows = min(max(1, visible), MAX_EXPORT_ROWS)
    layout.template_list(
        'BMD_SK_UL_export_tree', '',
        settings, 'tree_rows',
        settings, 'active_tree_row',
        rows=rows,
        maxrows=MAX_EXPORT_ROWS,
    )

    _indices, normal_count, position_offset, status, message = (
        bmd_metadata.read_normal_metadata(settings.target))

    if status != bmd_metadata.OK:
        layout.label(text=message, icon='ERROR')

    names = [item.name for item in settings.keys if item.include]
    metadata_key = (status, position_offset, normal_count)
    errors = _cached_validation(settings, names, metadata_key)

    for error in errors:
        layout.label(text=error, icon='ERROR')


class BMD_SK_OT_export_settings(Operator):
    bl_idname = 'bmd_shape_keys.export_settings'
    bl_label = 'Export BLS'
    bl_description = 'Choose which shape keys to write'
    bl_options = {'REGISTER'}

    def invoke(self, context, _event):
        settings = _get_export_settings(context)

        if settings is None:
            self.report({'ERROR'}, 'Export settings are unavailable')
            return {'CANCELLED'}

        if not settings.filepath:
            self.report({'ERROR'}, 'Select an export file')
            return {'CANCELLED'}

        settings.convert_units = bc.saved_convert_units(context, settings.target)
        settings.show_base = True
        _populate_export_keys(settings, settings.target)
        settings.expanded = True

        if settings.target is not None and not settings.base_name.strip():
            settings.base_name = _default_base_name(settings.target)

        return context.window_manager.invoke_props_dialog(
            self, width=650, confirm_text='Export')

    def draw(self, context):
        _draw_export_dialog(self.layout, _get_export_settings(context))

    def execute(self, context):
        return _perform_export(_get_export_settings(context), self, context)


class BMD_SK_OT_export_confirm(Operator):
    bl_idname = 'bmd_shape_keys.export_confirm'
    bl_label = 'Export'
    bl_description = 'Write the selected shape keys to the BLS file'
    bl_options = {'REGISTER'}

    def execute(self, context):
        return _perform_export(_get_export_settings(context), self, context)


CLASSES = (
    BMD_SK_ExportTreeRow,
    BMD_SK_ExportKeyItem,
    BMD_SK_ExportSettings,
    BMD_SK_UL_export_tree,
    BMD_SK_OT_export_files,
    BMD_SK_OT_export_settings,
    BMD_SK_OT_export_confirm,
)
