"""Store the BMD normal-index table in Blender mesh metadata.

BLS files address the BMD normal array directly.  Keeping the original
per-corner indices in Blender lets the shape-key exporter target either the
imported BMD or a BMD exported from the same mesh without reopening the BMD.
"""

from __future__ import annotations

import hashlib
import json
import struct

import numpy as np

ATTRIBUTE = "bmd_normal_index"
PROPERTY = "bmd_normal_meta"
VERSION = 1


def topology_hash(mesh):
    """Hash the mesh topology without including positions or normals."""
    digest = hashlib.sha1()
    vertex_count = len(mesh.vertices)
    loop_count = len(mesh.loops)
    polygon_count = len(mesh.polygons)
    digest.update(struct.pack("<III", vertex_count, loop_count, polygon_count))

    loops = np.empty(loop_count, dtype="<i4")
    mesh.loops.foreach_get("vertex_index", loops)
    digest.update(loops.tobytes())

    starts = np.empty(polygon_count, dtype="<i4")
    totals = np.empty(polygon_count, dtype="<i4")
    mesh.polygons.foreach_get("loop_start", starts)
    mesh.polygons.foreach_get("loop_total", totals)
    digest.update(starts.tobytes())
    digest.update(totals.tobytes())
    return digest.hexdigest()


def clear_metadata(mesh):
    attr = mesh.attributes.get(ATTRIBUTE)
    if attr is not None:
        mesh.attributes.remove(attr)
    if PROPERTY in mesh:
        del mesh[PROPERTY]


def write_metadata(mesh, indices, normal_count, source="import",
                   position_offset=0, vertex_order=True):
    """Write the per-corner normal indices and matching integrity metadata."""
    indices = np.asarray(indices, dtype=np.int32).reshape(-1)
    if len(indices) != len(mesh.loops):
        raise ValueError("Normal index count does not match the mesh loop count")
    if normal_count <= 0:
        raise ValueError("Normal count must be positive")
    if len(indices) and (indices.min() < 0 or indices.max() >= normal_count):
        raise ValueError("Normal index is outside the normal array")
    position_offset = int(position_offset)
    if position_offset < 0:
        raise ValueError("Position offset must not be negative")

    attr = mesh.attributes.get(ATTRIBUTE)
    if (attr is None or attr.domain != "CORNER" or attr.data_type != "INT" or
            len(attr.data) != len(mesh.loops)):
        if attr is not None:
            mesh.attributes.remove(attr)
        attr = mesh.attributes.new(ATTRIBUTE, "INT", "CORNER")

    attr.data.foreach_set("value", indices)
    mesh[PROPERTY] = json.dumps({
        "version": VERSION,
        "vertex_count": len(mesh.vertices),
        "loop_count": len(mesh.loops),
        "polygon_count": len(mesh.polygons),
        "topology": topology_hash(mesh),
        "normal_count": int(normal_count),
        "position_offset": position_offset,
        "vertex_order": bool(vertex_order),
        "source": str(source),
    }, separators=(",", ":"))
