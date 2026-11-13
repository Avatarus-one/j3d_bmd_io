"""Load BMD TEX1 entries whose image data lives outside the BMD."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .j3d import gx_texture as gx
from .j3d.common import J3DError, u16, u32


def _name_key(name):
    return str(name).casefold()


def _folder_stem(path):
    """STG_00, STG_00.arc, STG_00.arc_dir -> stg_00."""
    return Path(path).name.split(".", 1)[0].casefold()


def _texc_children(folder):
    """Return ``texc`` and ``archive/texc`` folders directly inside *folder*."""
    found = []
    try:
        children = list(Path(folder).iterdir())
    except OSError:
        return found

    for child in children:
        if not child.is_dir():
            continue
        if _folder_stem(child) == "texc":
            found.append(child)
        elif _folder_stem(child) == "archive":
            try:
                grandchildren = list(child.iterdir())
            except OSError:
                continue
            found.extend(
                grandchild for grandchild in grandchildren
                if grandchild.is_dir() and _folder_stem(grandchild) == "texc")
    return found


def _search_root(bmd_path):
    """Return the folder that contains the stage folder.

    ``.../R00_00/bmdr/model.bmd``          -> the folder above ``R00_00``
    ``.../R00_00/archive/bmdr/model.bmd``  -> one level further up, because the
    archive folder adds a level while ``STG_00`` stays next to ``R00_00``.
    """
    parents = Path(bmd_path).resolve().parents
    index = 2
    if len(parents) > 1 and _folder_stem(parents[1]) == "archive":
        index = 3
    if len(parents) <= index:
        return parents[-1] if parents else Path(bmd_path).resolve().parent
    return parents[index]


def find_texc_directories(bmd_path):
    """Find STG_00/texc and STG_00/archive/texc next to the room folder."""
    root = _search_root(bmd_path)
    result = []
    seen = set()

    stages = [root] if _folder_stem(root) == "stg_00" else []
    try:
        stages.extend(
            child for child in root.iterdir()
            if child.is_dir() and _folder_stem(child) == "stg_00")
    except OSError:
        pass

    for stage in stages:
        for candidate in _texc_children(stage):
            try:
                key = str(candidate.resolve()).casefold()
            except OSError:
                key = str(candidate).casefold()
            if key not in seen:
                seen.add(key)
                result.append(candidate)

    return result


def _texture_index(texc_dir):
    try:
        return {
            path.stem.casefold(): path
            for path in Path(texc_dir).iterdir()
            if path.is_file() and path.suffix.casefold() == ".bti"
        }
    except OSError:
        return {}


def find_texture_file(texc_dir, name):
    return _texture_index(texc_dir).get(_name_key(name))


def _candidate_folders(bmd_path, texc_dir=None):
    """Explicit texc folder first, then every auto-discovered one."""
    folders = []
    seen = set()
    explicit = [Path(texc_dir)] if texc_dir else []
    for path in explicit + find_texc_directories(bmd_path):
        try:
            key = str(path.resolve()).casefold()
        except OSError:
            key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            folders.append(path)
    return folders


def _read_bti(path):
    """Return image data and metadata from a .bti file."""
    data = Path(path).read_bytes()
    if len(data) < 0x20:
        raise J3DError(f"Texture file is too small: {path}")

    fmt = data[0]
    width = u16(data, 2)
    height = u16(data, 4)
    mip_count = max(1, data[0x18])
    data_offset = u32(data, 0x1C)
    pal_enabled = data[8]
    pal_format = data[9]
    pal_count = u16(data, 0x0A)
    pal_offset = u32(data, 0x0C)

    if data_offset == 0:
        raise J3DError(f"Texture file has no image data: {path}")

    palette = None
    raw_palette = None
    if fmt in (gx.C4, gx.C8, gx.C14X2) and pal_count:
        raw_palette = data[pal_offset:pal_offset + pal_count * 2]
        palette = gx.decode_palette(pal_format, raw_palette, pal_count)

    level_size = gx.level_size(fmt, width, height)
    total = gx.total_size(fmt, width, height, mip_count)
    image = data[data_offset:data_offset + level_size]
    if len(image) < level_size:
        raise J3DError(f"Texture file is truncated: {path}")

    raw = data[data_offset:data_offset + total]
    if len(raw) != total:
        raw = None

    rgba = gx.decode(fmt, image, width, height, palette)
    return (fmt, width, height, mip_count, pal_enabled, pal_format, pal_count, rgba,
            raw, raw_palette, data)


def apply_texture(texture, texc_dir):
    path = find_texture_file(texc_dir, texture.name)
    if path is None:
        return False

    (fmt, width, height, mip_count, pal_enabled, pal_format, pal_count, rgba,
     raw_data, raw_palette, full_data) = _read_bti(path)

    if texture.external_size:
        expected_width, expected_height = texture.external_size
    else:
        expected_height, expected_width = texture.rgba.shape[:2]
    if (width, height) != (expected_width, expected_height):
        raise J3DError(
            f"Texture dimensions differ from the BMD header: {path} "
            f"({width}x{height} != {expected_width}x{expected_height})")

    texture.format = fmt
    texture.rgba = rgba
    texture.mip_count = mip_count
    texture.pal_enabled = pal_enabled
    texture.pal_format = pal_format
    texture.pal_count = pal_count
    texture.raw_data = raw_data
    texture.raw_palette = raw_palette
    texture.raw_bti = full_data
    texture.external_path = str(path)
    # Keep the sampling fields from the BMD header: they are part of the
    # material setup even when the pixels come from an external .bti.
    return True


def use_placeholder(texture):
    """Replace missing external pixels with a placeholder."""
    if texture.external_size is None:
        texture.external_size = (int(texture.rgba.shape[1]), int(texture.rgba.shape[0]))
    texture.rgba = np.full((1, 1, 4), 255, np.uint8)
    texture.raw_data = None
    texture.raw_palette = None
    texture.raw_bti = None
    texture.external_path = None


def texture_availability(bmd_path, names, texc_dir=None):
    """Map each texture name to the folder that provides it (or ``None``)."""
    folders = _candidate_folders(bmd_path, texc_dir)
    indexes = [(folder, _texture_index(folder)) for folder in folders]
    result = {}
    for name in names:
        key = _name_key(name)
        result[name] = next(
            (folder for folder, index in indexes if key in index), None)
    return result


def check_external_textures(bmd, bmd_path, texc_dir=None):
    """Return (missing_names, suggested_folder) without modifying *bmd*."""
    names = [t.name for t in bmd.textures if t.external]
    if not names:
        return [], None

    folders = _candidate_folders(bmd_path, texc_dir)
    indexes = [(folder, _texture_index(folder)) for folder in folders]
    missing = [
        name for name in names
        if not any(_name_key(name) in index for _folder, index in indexes)
    ]

    if not missing:
        # Prefer a folder that has everything; external textures are normally
        # all stored in a single texc folder.  If they are split between
        # folders the loader still handles them, so do not report them missing.
        for folder, index in indexes:
            if all(_name_key(name) in index for name in names):
                return [], folder
        return [], indexes[0][0] if indexes else None

    suggested = None
    if indexes:
        # Point the user at the folder that already contains the most matches.
        suggested = max(
            indexes,
            key=lambda item: sum(1 for name in names if _name_key(name) in item[1])
        )[0]
    return missing, suggested


def load_for_bmd(bmd, bmd_path, texc_dir=None):
    """Load external textures; return the names replaced by placeholders.

    Missing textures do not abort the import: they get a placeholder
    so the model can still be imported and edited.
    """
    names = [t.name for t in bmd.textures if t.external]
    if not names:
        return []

    folders = _candidate_folders(bmd_path, texc_dir)
    missing = []
    for texture in bmd.textures:
        if not texture.external:
            continue
        for folder in folders:
            if apply_texture(texture, folder):
                break
        else:
            use_placeholder(texture)
            missing.append(texture.name)
    return missing
