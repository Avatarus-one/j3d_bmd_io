"""
Blender-side BLS import: base-key mesh matching, automatic BLS discovery for
BMD imports, and shape-key creation.

Each BLS key carries a delta relative to the base key of its cluster.  Shape
key blocks store absolute coordinates, so every key starts as a copy of the
target mesh's Basis coordinates and the BLS delta is added on top.  Deltas are
always rotated from J3D's Y-up space to Blender's Z-up space.  This keeps the
result independent of the object transform and lets the importer work with a
mesh that already has its own base shape.
"""

from __future__ import annotations

import os

import bpy
import numpy as np

from . import blender_common as bc
from . import bls_session
from .j3d import bls


MATCH_MAX_VERTICES = 50
MATCH_TOLERANCE = 2.0
MATCH_MIN_VERTICES = 5
MATCH_MIN_RATIO = 0.1


def _convert_delta(delta):
    """Convert a J3D (Y-up) delta to Blender's Z-up space.

    J3D is Y-up, Blender is Z-up: rotate +90 degrees around X.
    """
    dx, dy, dz = delta
    return dx, -dz, dy


def _build_coordinates(slots, base_coords, vertex_count, delta_scale=0.01):
    """Build a flat coordinate array for one shape key.

    Shape-key blocks store absolute coordinates, so the array starts as a
    copy of the mesh's base coordinates and the BLS deltas are added to it.
    *delta_scale* converts the BLS/model-unit delta to the target mesh's
    units (0.01 for j3d_bmd_io's default import scale, 1.0 for Blemd).
    Returns ``(coords, touched, out_of_range)`` where *touched* is the number
    of slots that referenced an existing mesh vertex.
    """
    coords = list(base_coords)
    touched = 0
    out_of_range = 0

    for slot in slots:
        vertex = slot.vertex

        if vertex < 0 or vertex >= vertex_count:
            out_of_range += 1
            continue

        dx, dy, dz = _convert_delta(slot.delta)
        offset = vertex * 3

        coords[offset] += dx * delta_scale
        coords[offset + 1] += dy * delta_scale
        coords[offset + 2] += dz * delta_scale
        touched += 1

    return coords, touched, out_of_range


def existing_shape_key_displacements(obj):
    """Return ``[(name, {vertex: (dx, dy, dz)})]`` for existing shape keys.

    The Basis key is skipped.  Displacements are in Blender's Z-up space and
    are measured against the Basis coordinates.
    """
    if obj is None or obj.type != 'MESH' or obj.data is None:
        return []

    shape_keys = obj.data.shape_keys

    if shape_keys is None or len(shape_keys.key_blocks) < 2:
        return []

    vertex_count = len(obj.data.vertices)

    if vertex_count == 0:
        return []

    basis = shape_keys.key_blocks[0]
    basis_coords = [0.0] * (vertex_count * 3)
    basis.data.foreach_get('co', basis_coords)

    result = []

    for key_block in shape_keys.key_blocks[1:]:
        coords = [0.0] * (vertex_count * 3)
        key_block.data.foreach_get('co', coords)
        displacement = {}

        for vertex in range(vertex_count):
            offset = vertex * 3
            dx = coords[offset] - basis_coords[offset]
            dy = coords[offset + 1] - basis_coords[offset + 1]
            dz = coords[offset + 2] - basis_coords[offset + 2]

            if max(abs(dx), abs(dy), abs(dz)) > bls.ZERO_DELTA:
                displacement[vertex] = (dx, dy, dz)

        result.append((key_block.name, displacement))

    return result


def session_base_key_name(session):
    """Return the base-key name from the first usable BLS file in the session."""
    if session is None:
        return None

    for file_entry in session.files:
        for key_entry in file_entry.keys:
            if key_entry.record.is_base:
                return key_entry.name

    return None


def session_base_slots(session):
    """Return the base-key slots with the most entries in the session."""
    best = []

    for file_entry in session.files:
        for key_entry in file_entry.keys:
            if (key_entry.is_base and
                    len(key_entry.record.slots) > len(best)):
                best = key_entry.record.slots

    return best


def mesh_base_match(context, mesh, slots):
    """Return ``(ratio, average_distance, scale)`` for a mesh, or None.

    Up to :data:`MATCH_MAX_VERTICES` base-key vertices are compared in the
    mesh's local space.  Both unit conventions are tried because a from-scratch
    mesh has no stored ``bmd_scale``.
    """
    if mesh is None or mesh.type != 'MESH' or mesh.data is None:
        return None

    vertices = mesh.data.vertices
    usable = [slot for slot in slots if 0 <= slot.vertex < len(vertices)]

    if len(usable) < MATCH_MIN_VERTICES:
        return None

    step = max(1, len(usable) // MATCH_MAX_VERTICES)
    sample = usable[::step][:MATCH_MAX_VERTICES]

    if len(sample) < MATCH_MIN_VERTICES:
        return None

    source = np.array(
        [slot.base_position for slot in sample], dtype=np.float64)
    actual = np.array(
        [vertices[slot.vertex].co[:] for slot in sample], dtype=np.float64)

    saved = bc.saved_convert_units(context, mesh)
    scales = (0.01, 1.0) if saved else (1.0, 0.01)
    best = None

    for scale in scales:
        expected = bc.point_to_blender(source, scale)
        distances = np.linalg.norm(expected - actual, axis=1)
        matches = int(np.count_nonzero(
            distances <= MATCH_TOLERANCE * scale))
        ratio = matches / len(sample)
        score = (ratio, -float(distances.mean()))

        if best is None or score > best[0]:
            best = (score, ratio, float(distances.mean()), scale, matches)

    _score, ratio, average, scale, matches = best

    if matches < MATCH_MIN_VERTICES or ratio < MATCH_MIN_RATIO:
        return None

    return ratio, average, scale


def find_mesh_by_base_key(context, session, current=None):
    """Return ``(mesh, scale)`` that best matches the BLS base key.

    A mesh selected by the user wins when it matches the base key, even if
    duplicate meshes in the scene produce a marginally better score.  Without
    a usable selection the previous target and then the rest of the scene are
    checked.
    """
    slots = session_base_slots(session)

    if not slots:
        return None

    selected = context.active_object

    if selected is None or selected.type != 'MESH':
        selected = next(
            (obj for obj in context.selected_objects if obj.type == 'MESH'),
            None,
        )

    if selected is not None:
        try:
            result = mesh_base_match(context, selected, slots)
        except Exception:
            result = None

        if result is not None:
            return selected, result[2]

    candidates = []

    if current is not None and current.type == 'MESH':
        candidates.append(current)

    for obj in context.scene.objects:
        if obj.type == 'MESH' and obj not in candidates:
            candidates.append(obj)

    best = None
    best_score = None

    for mesh in candidates:
        try:
            result = mesh_base_match(context, mesh, slots)
        except Exception:
            result = None

        if result is None:
            continue

        ratio, average, scale = result
        score = (ratio, -average)

        if best_score is None or score > best_score:
            best = (mesh, scale)
            best_score = score

    return best


def existing_keys(target):
    """Build session-side descriptions of the target mesh's shape keys."""
    return [
        bls_session.ExistingKey(name, displacement)
        for name, displacement in existing_shape_key_displacements(target)
    ]


def import_shape_keys_for_bmd(context, arm, bmd_path, delta_scale=0.01,
                              report=None):
    """Import the first compatible BLS next to an imported BMD.

    The BMD folder and its sibling ``bls`` folder are scanned.  Files are
    tried in sorted order; the first one whose base key matches the imported
    mesh is applied and the remaining files are skipped.
    """
    if arm is None or arm.type != "ARMATURE":
        return None

    mesh = next(
        (obj for obj in bpy.data.objects
         if obj.type == "MESH" and bc.target_armature(obj) is arm),
        None,
    )

    if mesh is None or mesh.data is None:
        return None

    bmd_dir = os.path.dirname(os.path.abspath(bmd_path))
    folders = [bmd_dir]
    sibling = os.path.join(os.path.dirname(bmd_dir), "bls")

    if os.path.isdir(sibling):
        folders.append(sibling)

    paths = []
    seen = set()

    for folder in folders:
        if not os.path.isdir(folder):
            continue

        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue

        for name in names:
            if not name.lower().endswith(".bls"):
                continue

            path = os.path.join(folder, name)
            key = os.path.normcase(os.path.abspath(path))

            if key in seen:
                continue

            seen.add(key)
            paths.append(path)

    if not paths:
        return None

    current_keys = existing_keys(mesh)
    vertex_count = len(mesh.data.vertices)

    for path in paths:
        try:
            session = bls_session.build_session(
                [path],
                recursive=False,
                existing_keys=current_keys,
                vertex_count=vertex_count,
                delta_scale=delta_scale,
            )
        except Exception as exc:
            if report:
                report({"WARNING"}, f"{os.path.basename(path)}: {exc}")
            continue

        if not session.files:
            continue

        file_entry = session.files[0]

        if file_entry.error:
            continue

        slots = session_base_slots(session)

        if not slots:
            continue

        try:
            match = mesh_base_match(context, mesh, slots)
        except Exception:
            match = None

        if match is None:
            continue

        keys = [
            key for key in file_entry.keys
            if not key.is_base and not key.is_original
            and key.status in (bls_session.UNIQUE, bls_session.CONFLICT)
        ]

        if not keys:
            continue

        jobs = [
            (key.new_name or key.name, key.record.slots, key.name)
            for key in keys
        ]

        try:
            stats = apply_shape_keys(
                context, mesh, jobs,
                delta_scale=delta_scale,
                base_key_name=session_base_key_name(session),
            )
        except Exception as exc:
            if report:
                report({"ERROR"},
                       f"BLS import failed for {os.path.basename(path)}: {exc}")
            return None

        mesh["bmd_bls_source"] = file_entry.path

        if report:
            report({"INFO"},
                   f"Imported shape keys from {os.path.basename(path)}: "
                   f"{stats['created']} created")

        return path

    return None


def apply_shape_keys(context, obj, jobs, delta_scale=0.01, base_key_name=None):
    """Add shape keys to *obj*.

    Parameters
    ----------
    context:
        Current Blender context.
    obj:
        Target mesh object.
    jobs:
        Iterable of ``(name, slots, source_name)`` tuples.  *slots* is a list
        of :class:`bls.Slot` objects.
    delta_scale:
        Scale applied to BLS position deltas before they are added to the
        mesh.  Use ``0.01`` for meshes imported with ``j3d_bmd_io``'s default
        scale and ``1.0`` for Blemd.
    base_key_name:
        Optional name for the target mesh's basis key when the mesh has no
        shape keys yet.  BLS imports pass the base-key name from the first
        BLS file here.

    A key whose final name already exists is overwritten.  The dialog renames
    imported keys that would collide with existing shape keys, so an overwrite
    only happens when the user deliberately renames a key back.

    BLS deltas are rotated from J3D Y-up to Blender Z-up and multiplied by
    *delta_scale*.
    """
    if obj is None:
        raise ValueError('No target mesh selected')
    if obj.type != 'MESH':
        raise ValueError(f'Object "{obj.name}" is not a mesh')
    if obj.data is None:
        raise ValueError(f'Object "{obj.name}" has no mesh data')

    mesh = obj.data
    vertex_count = len(mesh.vertices)

    if vertex_count == 0:
        raise ValueError(f'Mesh "{obj.name}" has no vertices')

    # Shape keys cannot be edited while the object is in edit mode.
    if obj.mode != 'OBJECT':
        context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='OBJECT')

    stats = {
        'created': 0,
        'updated': 0,
        'skipped': 0,
        'out_of_range': 0,
        'skipped_names': [],
        'created_basis': False,
    }

    shape_keys = mesh.shape_keys

    if shape_keys is None:
        obj.shape_key_add(
            name=(base_key_name or 'Basis'),
            from_mix=False,
        )
        shape_keys = mesh.shape_keys
        stats['created_basis'] = True

    if shape_keys is None:
        raise RuntimeError('Could not create the base shape key')

    shape_keys.use_relative = True

    basis = shape_keys.key_blocks[0] if shape_keys.key_blocks else None

    # Absolute coordinates that every shape key starts from.
    base_coords = [0.0] * (vertex_count * 3)
    if basis is not None:
        basis.data.foreach_get('co', base_coords)
    else:
        mesh.vertices.foreach_get('co', base_coords)

    existing = {}

    for key_block in shape_keys.key_blocks:
        existing.setdefault(key_block.name.casefold(), key_block)

    imported_names = set()

    for name, slots, _source_name in jobs:
        coords, touched, out_of_range = _build_coordinates(
            slots, base_coords, vertex_count, delta_scale
        )
        stats['out_of_range'] += out_of_range

        if touched == 0:
            stats['skipped'] += 1
            stats['skipped_names'].append(name)
            continue

        name_key = name.casefold()

        # The Basis key is protected, and two imported keys cannot share a
        # final name.  The dialog highlights such collisions; only the first
        # key is imported to avoid silently overwriting the other one.
        if name_key == 'basis' or name_key in imported_names:
            stats['skipped'] += 1
            stats['skipped_names'].append(name)
            continue

        imported_names.add(name_key)
        target_block = existing.get(name_key)

        if target_block is not None and target_block != basis:
            stats['updated'] += 1
        else:
            target_block = obj.shape_key_add(name=name, from_mix=False)
            existing[name_key] = target_block
            stats['created'] += 1

        target_block.data.foreach_set('co', coords)

    mesh.update()

    return stats
