"""Settings dialog for exporting external J3D textures (``.bti``)."""

from __future__ import annotations

from bpy.props import (
    BoolProperty,
    CollectionProperty,
    IntProperty,
    StringProperty,
)
from bpy.types import Operator, PropertyGroup, UIList

from . import bti_export
from .j3d.common import J3DError


_BTI_STATUS = {
    "new": ("New", "EXPORT"),
    "update": ("Modified", "FILE_REFRESH"),
    "unchanged": ("Up to date", "FILE_TICK"),
    "conflict": ("Conflict", "ERROR"),
    "error": ("Error", "ERROR"),
    "no_folder": ("Select folder", "INFO"),
}

MAX_BTI_TREE_ROWS = 12


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
            check.prop(data.texture_states[item.texture_index], "selected",
                       text="")
        row.label(text=item.label, icon="IMAGE_DATA")
        status = row.row()
        status.alignment = "RIGHT"
        status.label(text=item.status_text, icon=item.status_icon or "INFO")


class EXPORT_OT_bti(Operator):
    """Export textures as .bti files"""

    bl_idname = "export_scene.bti"
    bl_label = "Export BTI"
    # Like the animation export: REGISTER would leave a redo popup with this
    # dialog's layout in the bottom-left corner after exporting.
    bl_options = set()

    directory: StringProperty(
        name="Export Folder", subtype="DIR_PATH", default="",
        description="Folder for the .bti files")
    external_only: BoolProperty(
        name="External Only", default=False,
        description="Show only textures stored outside the BMD")
    group_states: CollectionProperty(type=BMDExportGroupState,
                                     options={"HIDDEN"})
    texture_states: CollectionProperty(type=BMDExportTextureState,
                                       options={"HIDDEN"})
    tree_rows: CollectionProperty(type=BMDExportTreeRow, options={"HIDDEN"})
    active_tree_row: IntProperty(default=0, min=0)

    def invoke(self, context, _event):
        if not self.directory:
            self.directory = bti_export.default_bti_directory(context)
        self._filter = self.external_only
        self._refresh_groups()
        return context.window_manager.invoke_props_dialog(
            self, width=600, confirm_text="Export")

    def _refresh_groups(self):
        """Rebuild the list (and its checkbox state) for the current filter."""
        self._groups = bti_export.scene_bti_groups(self.external_only)
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
                if (index < len(self.texture_states)
                        and self.texture_states[index].selected):
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
        layout = self.layout

        if getattr(self, "_filter", None) != self.external_only:
            self._filter = self.external_only
            self._refresh_groups()
        groups = getattr(self, "_groups", None)
        if groups is None:
            groups = bti_export.scene_bti_groups(self.external_only)
            self._groups = groups

        directory = self.directory.strip()
        key = (directory, bool(self.external_only),
               tuple(tuple(image.name for image in group["images"])
                     for group in groups))
        if getattr(self, "_plan_key", None) != key:
            self._plans = bti_export.bti_scene_plan(
                groups, directory, with_payload=False)
            self._plan_key = key
            self._rebuild_tree_rows(groups, self._plans or [])
        plans = self._plans or []

        self._apply_group_toggles(groups, plans)
        self._sync_texture_states(groups, plans)

        layout.prop(self, "directory", text="Export Folder")
        layout.prop(self, "external_only", text="External Only")

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
            text = ("No external textures found in the scene"
                    if self.external_only else "No textures found in the scene")
            layout.label(text=text, icon="INFO")

    def execute(self, context):
        directory = self.directory.strip()
        if not directory:
            self.report({"ERROR"}, "Select an output folder")
            return {"CANCELLED"}
        groups = getattr(self, "_groups", None)
        if groups is not None:
            key = (directory, bool(self.external_only),
                   tuple(tuple(image.name for image in group["images"])
                         for group in groups))
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
                                           selected=selected,
                                           external_only=self.external_only)
        except (bti_export.ExportError, J3DError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


CLASSES = (
    BMDExportGroupState,
    BMDExportTextureState,
    BMDExportTreeRow,
    BMD_UL_bti_tree,
    EXPORT_OT_bti,
)
