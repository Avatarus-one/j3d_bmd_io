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


def natural_normal_layout(mesh):
    """Return ``(per-corner normal indices, normal count)`` from the mesh.

    Corners are grouped by ``(vertex, rounded normal)``.  This is the canonical
    layout used when no BMD metadata is available; both the BMD and the BLS
    exporters use it so they address the same normal array.
    """
    count = len(mesh.loops)

    if not count:
        return np.zeros(0, np.int32), 0

    raw = np.empty(count * 3, np.float32)
    mesh.corner_normals.foreach_get("vector", raw)
    normals = np.round(raw.reshape(-1, 3).astype(np.float64), 6)
    vertices = np.empty(count, np.int32)
    mesh.loops.foreach_get("vertex_index", vertices)
    keys = np.empty((count, 4), np.float64)
    keys[:, 0] = vertices
    keys[:, 1:] = normals
    unique, inverse = np.unique(keys, axis=0, return_inverse=True)
    return inverse.reshape(-1).astype(np.int32), len(unique)


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


OK = "ok"
MISSING = "missing"
STALE = "stale"
INVALID = "invalid"


def read_normal_metadata(obj):
    """Return ``(indices, normal_count, position_offset, status, message)``.

    ``indices`` is a list with one BMD normal index per mesh corner.
    ``position_offset`` is added to mesh vertex indices to address the BMD
    position array.  The metadata is only valid while the mesh topology is
    unchanged.  This is the read-side counterpart of :func:`write_metadata`
    and is used by the BLS shape-key exporter.
    """
    if obj is None or obj.type != "MESH" or obj.data is None:
        return None, 0, 0, MISSING, "No target mesh selected"

    mesh = obj.data
    raw = mesh.get(PROPERTY)

    if not raw:
        return None, 0, 0, MISSING, (
            "Normal metadata is missing. Re-export BMD to fix it first.")

    try:
        meta = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None, 0, 0, INVALID, (
            "Normal metadata is invalid. Re-export BMD to fix it first.")

    if int(meta.get("version", 0)) != VERSION:
        return None, 0, 0, INVALID, (
            "Normal metadata is invalid. Re-export BMD to fix it first.")

    if not bool(meta.get("vertex_order", False)):
        return None, 0, 0, INVALID, (
            "Normal metadata is invalid. Re-export BMD to fix it first.")

    position_offset = int(meta.get("position_offset", 0))
    if position_offset < 0:
        return None, 0, 0, INVALID, (
            "Normal metadata is invalid. Re-export BMD to fix it first.")

    vertex_count = len(mesh.vertices)
    loop_count = len(mesh.loops)
    polygon_count = len(mesh.polygons)

    if (int(meta.get("vertex_count", -1)) != vertex_count or
            int(meta.get("loop_count", -1)) != loop_count or
            int(meta.get("polygon_count", -1)) != polygon_count):
        return None, 0, 0, STALE, (
            "Normal metadata is out-of-date. Re-export BMD to update it first.")

    if meta.get("topology") != topology_hash(mesh):
        return None, 0, 0, STALE, (
            "Normal metadata is out of date. Re-export BMD to update it first.")

    attr = mesh.attributes.get(ATTRIBUTE)
    if (attr is None or attr.domain != "CORNER" or attr.data_type != "INT" or
            len(attr.data) != loop_count):
        return None, 0, 0, INVALID, (
            "Normal metadata is invalid. Re-export BMD to fix it first.")

    indices = np.empty(loop_count, dtype=np.int32)
    attr.data.foreach_get("value", indices)

    normal_count = int(meta.get("normal_count", 0))
    if normal_count <= 0 or (loop_count and
                             (int(indices.min()) < 0 or
                              int(indices.max()) >= normal_count)):
        return None, 0, 0, INVALID, (
            "Normal metadata is invalid. Re-export BMD to update it first.")

    return indices.tolist(), normal_count, position_offset, OK, ""
