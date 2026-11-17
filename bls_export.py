"""Blender-side extraction of shape keys for BLS export."""

from __future__ import annotations

import bpy
import numpy as np

from . import bmd_metadata
from . import blender_common as bc
from .j3d import bls_writer


class BLSExportError(ValueError):
    pass


def _read_coords(block, count):
    coords = [0.0] * (count * 3)
    block.data.foreach_get("co", coords)
    return coords


POSITION_POOL_LIMIT = 8192


def _collect_geometry(obj, key_names):
    """Read base/key coordinates and the vertices moved by any selected key.

    Returns ``(basis_coords, key_coords, moved)`` or ``None`` when the object
    has no usable shape keys.  *key_coords* is a list of ``(name, coords)``.
    """
    if obj is None or obj.type != "MESH" or obj.data is None:
        return None

    mesh = obj.data
    shape_keys = mesh.shape_keys

    if shape_keys is None or len(shape_keys.key_blocks) < 2:
        return None

    blocks = list(shape_keys.key_blocks)
    basis = blocks[0]
    by_name = {block.name: block for block in blocks[1:]}
    selected = [by_name[name] for name in key_names if name in by_name]

    if not selected:
        return None

    count = len(mesh.vertices)
    basis_coords = _read_coords(basis, count)
    key_coords = [
        (block.name, _read_coords(block, count))
        for block in selected
    ]

    moved = set()
    for _name, coords in key_coords:
        for vertex in range(count):
            offset = vertex * 3
            dx = coords[offset] - basis_coords[offset]
            dy = coords[offset + 1] - basis_coords[offset + 1]
            dz = coords[offset + 2] - basis_coords[offset + 2]
            if max(abs(dx), abs(dy), abs(dz)) > 1e-9:
                moved.add(vertex)
    if not moved:
        moved.update(range(count))

    return basis_coords, key_coords, moved


def _position_pool(basis_coords, key_coords, indices, scale):
    """Build the shared absolute-position pool used by the BLS writer."""
    pool = set()

    def add_position(coords, vertex):
        offset = vertex * 3
        position = _to_bls(
            (coords[offset], coords[offset + 1], coords[offset + 2]), scale)
        pool.add(tuple(abs(value) for value in position))

    for vertex in indices:
        add_position(basis_coords, vertex)
    for _name, coords in key_coords:
        for vertex in indices:
            add_position(coords, vertex)

    return pool


def position_pool_count(obj, key_names, convert_units=True):
    """Return the number of unique absolute position vectors for a BLS export.

    The BLS writer shares one 13-bit position pool between the base key and all
    selected keys.  This is the same pool the writer would build, counted
    without writing the file.
    """
    geometry = _collect_geometry(obj, key_names)

    if geometry is None:
        return 0

    basis_coords, key_coords, moved = geometry
    scale = 100.0 if convert_units else 1.0
    return len(_position_pool(basis_coords, key_coords, sorted(moved), scale))


def _unique_orig_name(used):
    """Return a zero-key name that does not collide with *used* names."""
    name = "orig"
    counter = 1

    while name.casefold() in used:
        name = f"orig.{counter:03d}"
        counter += 1

    return name


def validate_export(obj, key_names, base_name, convert_units=True):
    """Return short error strings that would stop the BLS writer.

    Only checks that can be evaluated without building the full normal data
    are included.  The normal pool is fitted automatically by the writer.
    """
    errors = []

    if obj is None or obj.type != "MESH" or obj.data is None:
        return ["No target mesh"]

    mesh = obj.data
    shape_keys = mesh.shape_keys

    if shape_keys is None or len(shape_keys.key_blocks) < 2:
        return ["No shape keys found"]

    blocks = list(shape_keys.key_blocks)
    by_name = {block.name: block for block in blocks[1:]}
    key_names = list(key_names)

    if not key_names:
        return ["Select at least one key to export"]

    missing = [name for name in key_names if name not in by_name]

    if missing:
        errors.append(f"Key not found: {missing[0][:24]}")

    if not any(name in by_name for name in key_names):
        return errors or ["Select at least one key to export"]

    base_name = (base_name or "").strip()

    if not base_name:
        errors.append("Base key name is empty")
    elif any(base_name.casefold() == name.casefold() for name in key_names):
        errors.append("Base name conflicts with a key")

    cluster_name = mesh.name or obj.name
    final_base = base_name or f"{cluster_name}__base"
    used = {name.casefold() for name in key_names}

    if final_base.casefold() in used:
        final_base = f"{final_base}_base"

    used.add(final_base.casefold())
    orig_name = _unique_orig_name(used)
    names = [cluster_name, orig_name] + key_names + [final_base]

    for name in names:
        try:
            name.encode("shift_jis")
        except UnicodeEncodeError:
            errors.append(f"Not Shift-JIS: {name[:24]}")
            break

    geometry = _collect_geometry(obj, key_names)

    if geometry is None:
        return errors or ["Select at least one key to export"]

    basis_coords, key_coords, moved = geometry
    indices = sorted(moved)
    scale = 100.0 if convert_units else 1.0
    layout, position_offset = _normal_layout(obj)

    if not indices:
        errors.append("No vertices to export")
    else:
        if len(indices) > 0xFFFF:
            errors.append(f"Vertices: {len(indices)}/65535")

        max_vertex = indices[-1] + position_offset

        if max_vertex > 0xFFFF:
            errors.append(f"Vertex index: {max_vertex}/65535")

        pool = _position_pool(basis_coords, key_coords, indices, scale)

        if len(pool) > POSITION_POOL_LIMIT:
            errors.insert(
                0, f"Position pool exceeded: {len(pool)}/{POSITION_POOL_LIMIT} entries.")

    if layout and len(layout) == len(mesh.loops):
        loop_vertices = [0] * len(mesh.loops)
        mesh.loops.foreach_get("vertex_index", loop_vertices)
        blend_count = len(set(loop_vertices))
    else:
        blend_count = len(mesh.vertices)

    if blend_count > 0xFFFF:
        errors.append(f"Normal blend: {blend_count}/65535")

    table_names = [orig_name] + key_names + [final_base]
    offset = 4 + 4 * len(table_names)

    for name in table_names:
        try:
            encoded = name.encode("shift_jis") + b"\x00"
        except UnicodeEncodeError:
            break

        if offset > 0xFFFF:
            errors.append(f"Name table: {offset}/65535")
            break

        offset += len(encoded)

    return errors


def _to_bls(position, scale):
    """Blender Z-up -> J3D Y-up (and Blender units -> model units)."""
    x, y, z = position
    return (x * scale, z * scale, -y * scale)


def _normal_to_bls(normal):
    """Rotate a Blender-space normal into J3D space."""
    x, y, z = normal
    return (x, z, -y)


def _vertex_normals(coords, triangles, count):
    """Compute area-weighted vertex normals from a position array."""
    accum = [0.0] * (count * 3)

    for i0, i1, i2 in triangles:
        o0 = i0 * 3
        o1 = i1 * 3
        o2 = i2 * 3
        ax = coords[o1] - coords[o0]
        ay = coords[o1 + 1] - coords[o0 + 1]
        az = coords[o1 + 2] - coords[o0 + 2]
        bx = coords[o2] - coords[o0]
        by = coords[o2 + 1] - coords[o0 + 1]
        bz = coords[o2 + 2] - coords[o0 + 2]
        nx = ay * bz - az * by
        ny = az * bx - ax * bz
        nz = ax * by - ay * bx

        for vertex in (i0, i1, i2):
            off = vertex * 3
            accum[off] += nx
            accum[off + 1] += ny
            accum[off + 2] += nz

    normals = []

    for vertex in range(count):
        off = vertex * 3
        nx = accum[off]
        ny = accum[off + 1]
        nz = accum[off + 2]
        length = (nx * nx + ny * ny + nz * nz) ** 0.5

        if length > 1e-12:
            normals.append((nx / length, ny / length, nz / length))
        else:
            normals.append((0.0, 1.0, 0.0))

    return normals


def _mesh_triangles(mesh):
    mesh.calc_loop_triangles()
    count = len(mesh.loop_triangles)

    if count == 0:
        return []

    indices = [0] * (count * 3)
    mesh.loop_triangles.foreach_get("vertices", indices)
    return [
        (indices[index * 3], indices[index * 3 + 1], indices[index * 3 + 2])
        for index in range(count)
    ]


def _corner_normals(mesh):
    count = len(mesh.loops)

    if count == 0:
        return np.zeros((0, 3), np.float64)

    raw = np.empty(count * 3, np.float32)
    mesh.corner_normals.foreach_get("vector", raw)
    return raw.reshape(-1, 3).astype(np.float64)


def _natural_normal_layout(mesh):
    """Fallback canonical layout when BMD metadata is unavailable."""
    indices, _count = bmd_metadata.natural_normal_layout(mesh)
    return indices.tolist()


NORMAL_POOL_LIMIT = 0x2000


def _normal_pool_size(base_normals, key_normals, decimals):
    """Count the shared absolute-value pool the writer would create."""
    pool = set()

    for group in [base_normals] + list(key_normals):
        for vector in group:
            if decimals is None:
                components = tuple(abs(float(value)) for value in vector)
            else:
                components = tuple(
                    round(abs(float(value)), decimals) for value in vector)
            pool.add(components)

    return len(pool)


def _fit_normal_pool(base_normals, key_normals):
    """Keep the normal pool inside the 13-bit BLS index limit.

    BLS stores every normal as a 13-bit reference, so at most 8192 distinct
    absolute vectors fit in one file.  Multi-key face clusters can exceed that
    at full float precision.  Reduce precision only as much as necessary; the
    destinations and blend topology are unaffected.
    """
    if not base_normals:
        return base_normals, key_normals

    groups = [list(base_normals)] + [list(array) for array in key_normals]

    if _normal_pool_size(groups[0], groups[1:], None) <= NORMAL_POOL_LIMIT:
        return base_normals, key_normals

    for decimals in range(6, -1, -1):
        if _normal_pool_size(groups[0], groups[1:], decimals) <= NORMAL_POOL_LIMIT:
            return (
                [
                    tuple(round(float(value), decimals) for value in vector)
                    for vector in groups[0]
                ],
                [
                    [
                        tuple(round(float(value), decimals) for value in vector)
                        for vector in array
                    ]
                    for array in groups[1:]
                ],
            )

    return base_normals, key_normals


def _normal_layout(obj):
    """Return stored normal indices, or a canonical fallback."""
    indices, _normal_count, position_offset, status, _message = (
        bmd_metadata.read_normal_metadata(obj))

    if status == bmd_metadata.OK:
        return indices, position_offset

    return _natural_normal_layout(obj.data), 0


def collect_export_data(obj, key_names, convert_units=True, base_name=None):
    """Return all data needed for a BLS cluster.

    The returned tuple is
    ``(cluster_name, vertex_indices, base_positions, keys, base_normals,
    key_normals, cluster_vertices, base_name)``.

    *base_name* overrides the default ``<mesh>__base`` base-key name.
    BLS destinations use the normal indices stored by the BMD importer/exporter;
    when those are unavailable or stale, a canonical layout is used as fallback.
    """
    if obj is None or obj.type != "MESH" or obj.data is None:
        raise BLSExportError("No target mesh selected")

    mesh = obj.data
    shape_keys = mesh.shape_keys

    if shape_keys is None or len(shape_keys.key_blocks) < 2:
        raise BLSExportError(
            f'Mesh "{obj.name}" has no shape keys to export')

    blocks = list(shape_keys.key_blocks)
    by_name = {block.name: block for block in blocks[1:]}
    missing = [name for name in key_names if name not in by_name]

    if missing:
        raise BLSExportError(f'Shape key not found: "{missing[0]}"')

    if not key_names:
        raise BLSExportError("Select at least one shape key to export")

    geometry = _collect_geometry(obj, key_names)

    if geometry is None:
        raise BLSExportError("Select at least one shape key to export")

    basis_coords, key_coords, moved = geometry
    count = len(mesh.vertices)
    local_vertex_indices = sorted(moved)
    layout, position_offset = _normal_layout(obj)
    vertex_indices = [v + position_offset for v in local_vertex_indices]
    scale = 100.0 if convert_units else 1.0

    base_positions = [
        _to_bls(
            (basis_coords[v * 3], basis_coords[v * 3 + 1],
             basis_coords[v * 3 + 2]),
            scale,
        )
        for v in local_vertex_indices
    ]

    keys = []

    for name, coords in key_coords:
        keys.append((
            name,
            [
                _to_bls(
                    (coords[v * 3], coords[v * 3 + 1], coords[v * 3 + 2]),
                    scale,
                )
                for v in local_vertex_indices
            ],
        ))

    triangles = _mesh_triangles(mesh)
    key_vertex_normals = [
        _vertex_normals(coords, triangles, count)
        for _name, coords in key_coords
    ]

    if layout and len(layout) == len(mesh.loops):
        # Use the same normal indices the BMD exporter writes.  A vertex may
        # own several split-normal destinations; the blend table must list all
        # of them or the runtime would only update one corner normal.
        loop_vertices = [0] * len(mesh.loops)
        mesh.loops.foreach_get("vertex_index", loop_vertices)
        corners = _corner_normals(mesh)
        index_vertex = {}
        index_normal = {}

        for loop, normal_index in enumerate(layout):
            normal_index = int(normal_index)

            if normal_index not in index_vertex:
                index_vertex[normal_index] = int(loop_vertices[loop])
                index_normal[normal_index] = corners[loop]

        ordered = sorted(index_vertex)
        # One normal slot per distinct normal direction.  BLS address fields
        # use 13 bits (8192 pool entries), so one slot per BMD destination would
        # overflow the normal pool on models with many split normals.  Sharing
        # slots by value also matches how the original files are built.
        slot_of = {}
        slot_vertices = []
        slot_normals = []
        slot_lookup = {}

        for normal_index in ordered:
            normal = index_normal[normal_index]
            key = tuple(round(float(value), 6) for value in normal)
            slot = slot_lookup.get(key)

            if slot is None:
                slot = len(slot_vertices)
                slot_lookup[key] = slot
                slot_vertices.append(index_vertex[normal_index])
                slot_normals.append(normal)

            slot_of[normal_index] = slot

        base_normals = [_normal_to_bls(normal) for normal in slot_normals]
        key_normals = [
            [
                _normal_to_bls(normals[vertex])
                for vertex in slot_vertices
            ]
            for normals in key_vertex_normals
        ]

        per_vertex = [set() for _ in range(count)]

        for loop, normal_index in enumerate(layout):
            per_vertex[int(loop_vertices[loop])].add(int(normal_index))

        cluster_vertices = []

        for vertex, normal_indices in enumerate(per_vertex):
            if not normal_indices:
                continue

            destinations = sorted(normal_indices)
            cluster_vertices.append((
                [slot_of[normal_index] for normal_index in destinations],
                destinations,
            ))
    else:
        # Degenerate fallback: one normal slot per vertex, addressed by the
        # vertex index.  Used only when the mesh has no usable loops.
        base_normals = [
            _normal_to_bls(normal)
            for normal in _vertex_normals(basis_coords, triangles, count)
        ]
        key_normals = [
            [_normal_to_bls(normal) for normal in normals]
            for normals in key_vertex_normals
        ]
        cluster_vertices = [([index], [index]) for index in range(count)]

    cluster_name = mesh.name or obj.name
    existing = {name.casefold() for name in key_names}
    explicit_base = base_name.strip() if base_name is not None else ''

    if explicit_base:
        base_name = explicit_base

        if base_name.casefold() in existing:
            raise BLSExportError(
                f'Base key name "{base_name}" is already used by a selected key')
    else:
        base_name = f"{cluster_name}__base"

        if base_name.casefold() in existing:
            base_name = f"{base_name}_base"

    existing.add(base_name.casefold())
    orig_name = _unique_orig_name(existing)
    keys.insert(0, (orig_name, list(base_positions)))

    if key_normals:
        key_normals.insert(0, list(base_normals))

    base_normals, key_normals = _fit_normal_pool(base_normals, key_normals)

    return (cluster_name, vertex_indices, base_positions, keys,
            base_normals, key_normals, cluster_vertices, base_name)


def export_bls(obj, filepath, key_names, convert_units=True, base_name=None):
    """Write selected shape keys of *obj* to *filepath*.

    *base_name* overrides the trailing base-key name; when omitted, the
    ``<mesh>__base`` fallback is used.  Returns a statistics dict with
    ``keys``, ``vertices`` and ``bytes``.
    """
    (cluster_name, vertex_indices, base_positions, keys,
     base_normals, key_normals, cluster_vertices, final_base_name) = (
        collect_export_data(obj, key_names, convert_units, base_name)
    )

    data = bls_writer.write_bls(
        cluster_name,
        vertex_indices,
        base_positions,
        keys,
        base_name=final_base_name,
        base_normals=base_normals,
        key_normals=key_normals,
        cluster_vertices=cluster_vertices,
    )

    with open(filepath, "wb") as file:
        file.write(data)

    bc.remember_export(obj, "bls", filepath)
    return {
        "keys": len(keys),
        "vertices": len(vertex_indices),
        "bytes": len(data),
    }
