"""Standalone .bti texture import as a regular Blender image."""

from __future__ import annotations

import os

import bpy

from . import blender_import
from .j3d import bti as bti_parser


def _show_in_image_editor(context, image):
    screens = []
    current = getattr(context, "screen", None)

    if current is not None:
        screens.append(current)

    for window in bpy.context.window_manager.windows:
        if window.screen is not None and window.screen not in screens:
            screens.append(window.screen)

    for screen in screens:
        for area in screen.areas:
            if area.type != "IMAGE_EDITOR":
                continue

            for space in area.spaces:
                if space.type == "IMAGE_EDITOR":
                    space.image = image
                    return


def _add_image_node(context, image):
    """Create an Image Texture node when the import runs from a shader editor."""
    space = getattr(context, "space_data", None)

    if space is None or getattr(space, "type", None) != "NODE_EDITOR":
        return

    if getattr(space, "tree_type", None) != "ShaderNodeTree":
        return

    tree = getattr(space, "edit_tree", None)

    if tree is None:
        return

    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    node.location = getattr(space, "cursor_location", (0.0, 0.0))

    for other in tree.nodes:
        other.select = False

    node.select = True
    tree.nodes.active = node


def import_bti(context, filepath, report=None):
    """Import one ``.bti`` file and return the created image datablock."""
    with open(filepath, "rb") as file:
        data = file.read()

    name = os.path.splitext(os.path.basename(filepath))[0] or "texture"
    texture = bti_parser.parse_bti(data, name=name)
    image = blender_import.create_texture_image(texture, name=name, path=filepath)

    _add_image_node(context, image)
    _show_in_image_editor(context, image)

    if report:
        width, height = image.size
        fmt = image.get("bmd_orig_format", "AUTO")
        report({"INFO"}, f"Imported texture '{image.name}' ({width}x{height}, {fmt})")

    return image
