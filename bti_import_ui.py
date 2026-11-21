"""File-browser UI for importing J3D textures (``.bti``)."""

from __future__ import annotations

import os

from bpy.props import CollectionProperty, StringProperty
from bpy.types import Operator, OperatorFileListElement
from bpy_extras.io_utils import ImportHelper

from . import bti_import
from .j3d.common import J3DError


class IMPORT_OT_bti(Operator, ImportHelper):
    """Import J3D textures (.bti) as Blender images"""

    bl_idname = "import_image.bti"
    bl_label = "Import BTI"
    bl_options = {"UNDO"}

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
        paths = ([os.path.join(self.directory, file.name)
                  for file in self.files] if self.files else [])

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
    bl_options = {"UNDO"}

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
        paths = ([os.path.join(self.directory, file.name)
                  for file in self.files] if self.files else [])

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


CLASSES = (
    IMPORT_OT_bti,
    IMPORT_OT_bti_node,
)
