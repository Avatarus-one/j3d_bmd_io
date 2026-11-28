"""Blender material node trees for J3D materials.

Both the BMD importer (TEV stage data) and the interactive material tools
(custom materials, Render Mode / Use Vertex Color) build the same node layout,
so the layout lives here once:

* Material Output at ``(1100, 0)`` and Principled BSDF at ``(800, 0)``;
* ``bmd_tex<N>`` image nodes left of the BSDF (``-320`` per slot);
* a UV Map node below the main texture, with the GX per-axis wrap chain;
* the vertex colour node at ``bsdf - (580, 240)`` with a multiply Mix at
  ``bsdf - (200, 0)``;
* the alpha chain just left of the BSDF (multiply at ``-380``, Greater Than at
  ``-200``).

Imported BMD materials get one ``BMD TEV <material> S<n>`` node group per TEV
stage, so the preview evaluates every combiner stage with its own selectors,
bias, scale and clamp.  Nodes that future animation tools can target carry a
``bmd_anim_role`` tag: ``btk`` (texture SRT Mapping), ``brk`` (TEV register and
konst colours), ``bpk`` (material colours) and the BTP texture swap on the
ordinary ``bmd_tex<N>`` nodes.

Nodes created by the interactive tools carry a ``bmd_node_role`` tag so a
Render Mode / Use Vertex Color change can rebuild exactly those nodes without
touching the imported TEV setup.
"""

from __future__ import annotations

import json
import math
import struct

import bpy
from mathutils import Matrix

from . import blender_common as bc
from .j3d import material as mat3

ROLE_PROP = "bmd_node_role"
ROLE_VC = "vc"
ROLE_VC_MIX = "vc_mix"
ROLE_ALPHA_MUL = "alpha_mul"
ROLE_ALPHA_CUTOUT = "alpha_cutout"
ROLE_BLEND_ADD = "blend_add"
ROLE_BLEND_MULT = "blend_mult"
ROLES = {ROLE_VC, ROLE_VC_MIX, ROLE_ALPHA_MUL, ROLE_ALPHA_CUTOUT,
         ROLE_BLEND_ADD, ROLE_BLEND_MULT}

READY_MODES = {mat3.RENDER_OPAQUE, mat3.RENDER_CUTOUT,
               mat3.RENDER_TRANSLUCENT}
WRAP_EXT = {0: "EXTEND", 1: "REPEAT", 2: "MIRROR"}

_SUPPRESS = 0


def begin_sync():
    global _SUPPRESS
    _SUPPRESS += 1


def end_sync():
    global _SUPPRESS
    if _SUPPRESS:
        _SUPPRESS -= 1


def sync_suppressed():
    return _SUPPRESS > 0


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def tree_of(material):
    if material is None:
        return None
    if material.node_tree is None:
        try:
            material.use_nodes = True
        except Exception:
            return None
    return material.node_tree


def _node(tree, node_type):
    return next((node for node in tree.nodes if node.type == node_type), None)


def _ensure_output(tree):
    output = _node(tree, "OUTPUT_MATERIAL")
    if output is None:
        output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (1100.0, 0.0)
    return output


def _layout_node(tree):
    """BSDF when present, otherwise the output node (for custom outputs)."""
    return _node(tree, "BSDF_PRINCIPLED") or _node(tree, "OUTPUT_MATERIAL")


# Node bounds used by the layout.  These are the sizes the node editor draws
# (``node.width``/``node.height`` are zero outside a redraw).
_NODE_WIDTHS = {
    "TEX_IMAGE": 500.0, "BSDF_PRINCIPLED": 240.0,
}
_NODE_HEIGHTS = {
    "VALUE": 60.0, "RGB": 160.0, "VERTEX_COLOR": 100.0,
    "TEX_IMAGE": 300.0, "NEW_GEOMETRY": 240.0, "MAPPING": 380.0,
    "SEPARATE_XYZ": 120.0, "COMBINE_XYZ": 120.0,
    "VECT_TRANSFORM": 140.0, "SEPARATE_COLOR": 140.0,
    "COMBINE_COLOR": 140.0, "MATH": 140.0,
    "BSDF_PRINCIPLED": 540.0, "BSDF_TRANSPARENT": 80.0,
    "OUTPUT_MATERIAL": 160.0,
}
NODE_WIDTH = 140.0
NODE_HEIGHT = 140.0
# Horizontal gap between neighbouring nodes, vertical gap between rows.
H_GAP = 60.0
V_GAP = 40.0


def _node_extent(node):
    """Width/height of a node as the node editor draws it (the layout spec)."""
    width = _NODE_WIDTHS.get(node.type, NODE_WIDTH)
    height = _NODE_HEIGHTS.get(node.type, NODE_HEIGHT)
    if node.type == "TEX_IMAGE" and node.get("bmd_simple_texture"):
        # A simple preview has no "Texture to TEV" group in its slot.
        width = 240.0
    if node.type in ("GROUP_INPUT", "GROUP_OUTPUT"):
        # These grow with their sockets; the plain 140 default is too short
        # for a TEV group (9 sockets), which pushed the first branch row into
        # the input node.  Header plus one row per socket.
        sockets = max(len(node.inputs), len(node.outputs))
        height = max(height, 30.0 + 22.0 * sockets)
    if node.type == "GROUP":
        # A group node draws as tall as its interface needs.  Use the height
        # Blender reports, snapped up to the node-editor grid (20), so a node
        # below the group is not pushed down by a stale fixed estimate.
        width = max(width, float(getattr(node, "width", 0.0) or 0.0))
        real_height = float(getattr(node, "height", 0.0) or 0.0)
        if real_height > 0.0:
            height = 20.0 * math.ceil(real_height / 20.0)
        else:
            sockets = len(node.inputs) + len(node.outputs)
            height = 20.0 * math.ceil((30.0 + 22.0 * sockets) / 20.0)
    if node.type == "VECT_MATH":
        # A vector math using a single input is the tall "one argument" shape.
        linked = sum(1 for socket in node.inputs if socket.is_linked)
        height = 200.0 if linked <= 1 else 120.0
    return width, height


def _is_tev_texture(node):
    """True when a texture feeds a TEV stage (directly or via its conversion).

    Indirect-map sources only feed swizzle/scale math before they are used as
    the UV input of another texture; they belong in that branch, not in the
    column that is reserved for the stage's own texture.
    """
    for socket in node.outputs:
        for link in socket.links:
            target = link.to_node
            if target.get("bmd_anim_role") == "tev_stage":
                return True
            if target.name.startswith("bmd_texcol"):
                return True
    return False


def _feeds_texcol(node, consumer):
    """True when *node* is a "Texture to TEV" group feeding *consumer*."""
    if consumer is None or not node.name.startswith("bmd_texcol"):
        return False
    try:
        return any(link.to_node == consumer
                   for link in node.outputs["Color"].links)
    except Exception:
        return False


def _resolve_overlaps(nodes, margin=20.0):
    """Push non-stage nodes apart vertically until their bounds clear.

    ``location`` is the top-left corner of a node, so a node covers
    ``[x, x + width]`` by ``[y - height, y]``.  Comparing left edges as centres
    (as this used to) under- and over-estimated both axes: nodes in adjacent
    columns looked clear while their bodies were on top of each other, and
    stacked columns stayed overlapped by a few dozen pixels.
    """
    fixed = {node for node in nodes
             if node.get("bmd_anim_role") == "tev_stage"}
    for _ in range(32):
        moved = False
        for index, first in enumerate(nodes):
            for second in nodes[index + 1:]:
                first_w, first_h = _node_extent(first)
                second_w, second_h = _node_extent(second)
                # A "Texture to TEV" node is meant to sit right next to the
                # texture and the stage it feeds; keep it on that row instead of
                # pushing it away vertically.
                if (_feeds_texcol(first, second)
                        or _feeds_texcol(second, first)):
                    continue
                if (first.location.x + first_w + margin <= second.location.x
                        or second.location.x + second_w + margin
                        <= first.location.x):
                    continue
                if (first.location.y - first_h - margin >= second.location.y
                        or second.location.y - second_h - margin
                        >= first.location.y):
                    continue
                if first in fixed and second in fixed:
                    continue
                if first in fixed:
                    mover, other, mover_h, other_h = (second, first,
                                                      second_h, first_h)
                elif second in fixed:
                    mover, other, mover_h, other_h = (first, second,
                                                      first_h, second_h)
                elif first.location.y < second.location.y:
                    mover, other, mover_h, other_h = (first, second,
                                                      first_h, second_h)
                else:
                    mover, other, mover_h, other_h = (second, first,
                                                      second_h, first_h)
                if mover.location.y <= other.location.y:
                    target = other.location.y - other_h - margin
                    if mover.location.y > target:
                        mover.location.y = target
                        moved = True
                else:
                    target = other.location.y + mover_h + margin
                    if mover.location.y < target:
                        mover.location.y = target
                        moved = True
        if not moved:
            break


def _place_layer(layer, targets, heights, gap, stage_nodes):
    """Place one layout column near its target rows, then remove overlaps."""
    fixed = [node for node in layer if node in stage_nodes]
    rest = [node for node in layer if node not in stage_nodes]
    for node in fixed:
        node.location.y = 0.0

    if not fixed:
        previous = None
        for node in sorted(rest, key=lambda item: -targets[item]):
            if previous is None:
                node.location.y = targets[node]
            else:
                # ``location`` is the top-left corner: the next node starts
                # below the previous one's full body.
                limit = previous.location.y - heights[previous] - gap
                node.location.y = min(targets[node], limit)
            previous = node
        return

    stage_half = max(heights[node] for node in fixed) / 2.0
    above = sorted((node for node in rest if targets[node] >= 0.0),
                   key=lambda item: targets[item])
    below = sorted((node for node in rest if targets[node] < 0.0),
                   key=lambda item: -targets[item])

    previous = None
    for node in above:
        height = heights[node]
        floor = stage_half + gap + height / 2.0
        if previous is not None:
            floor = max(floor, previous.location.y + heights[previous] / 2.0
                        + gap + height / 2.0)
        node.location.y = max(targets[node], floor)
        previous = node

    previous = None
    for node in below:
        height = heights[node]
        ceiling = -(stage_half + gap + height / 2.0)
        if previous is not None:
            ceiling = min(ceiling,
                          previous.location.y - heights[previous] / 2.0
                          - gap - height / 2.0)
        node.location.y = min(targets[node], ceiling)
        previous = node


def _place_texture_recovery(nodes, tree):
    """Put "Texture to TEV" on the row of the texture it converts."""
    for node in nodes:
        if not node.name.startswith("bmd_texcol"):
            continue
        slot = node.name[len("bmd_texcol"):]
        texture = tree.nodes.get(f"bmd_tex{slot}")
        if texture is not None:
            node.location.y = texture.location.y
            node.location.x = texture.location.x + TEXCOL_OFFSET


# TEV stage row: nothing sits between the stages any more, so their origins
# are exactly this far apart (the drawn stage group is ~140 units wide).
STAGE_ROW_GAP = 200.0
# Vertical distance between two stage blocks (they must read as separate).
STAGE_BLOCK_GAP = 100.0
# Horizontal clearance between two nodes sharing a row.
ROW_GAP = H_GAP


def _row_conflicts(row, node, width):
    """True when *node* overlaps a horizontal range already in a row."""
    start = node.location.x
    end = start + width[node]
    return any(start < other_end + ROW_GAP and other_start < end + ROW_GAP
               for other_start, other_end in row)


def _place_stage_block(block, top, outgoing, stage_set, width, height):
    """Place one block of branches as top-aligned rows, returns its bottom.

    A node continues the row of the node it feeds; when its body would collide
    with that row it starts the next one.  Row heights are the tallest node of
    the row and rows are ``V_GAP`` apart, so nothing is squeezed into the gaps
    of the row above.
    """
    columns = {}
    for node in block:
        columns.setdefault(round(node.location.x), []).append(node)

    assigned = {}
    rows = []

    def desired(node):
        values = [assigned[target] for target in outgoing[node]
                  if target in assigned]
        if any(target in stage_set for target in outgoing[node]):
            values.append(0)
        return min(values) if values else None

    def add_row(node, row, check=True):
        if check:
            while row < len(rows) and _row_conflicts(rows[row], node, width):
                row += 1
        while row >= len(rows):
            rows.append([])
        rows[row].append((node.location.x,
                          node.location.x + width[node]))
        assigned[node] = row

    # The TEV texture (and its conversion) is the backbone of a stage block:
    # it keeps row 0 even when a value node beside it overlaps its column.
    backbone = [node for node in block
                if node.name.startswith("bmd_texcol")
                or (node.type == "TEX_IMAGE" and _is_tev_texture(node))]
    for node in sorted(backbone, key=lambda item: -item.location.x):
        # The conversion lives inside the texture slot, so the two intentionally
        # overlap in X and must still share row 0.
        add_row(node, 0, check=False)

    # Walk from the sink side outwards so every node can continue the row of
    # the node it feeds; branches that collide start a new row.
    for column_x in sorted(columns, reverse=True):
        column = [node for node in columns[column_x] if node not in assigned]
        if not column:
            continue
        column.sort(key=lambda item: (
            desired(item) is None,
            desired(item) if desired(item) is not None else 0,
            -item.location.y))
        for node in column:
            add_row(node, desired(node) or 0)

    rows_nodes = [[node for node in block if assigned[node] == index]
                  for index in range(len(rows))]

    # A row only has to clear the nodes that are actually above it: a tall
    # node far to the left does not push a branch down.  Two nodes interact
    # when their columns are closer than one horizontal gap.
    def above(node):
        for previous in range(assigned[node]):
            for upper in rows_nodes[previous]:
                if (upper.location.x < node.location.x + width[node] + H_GAP
                        and node.location.x
                        < upper.location.x + width[upper] + H_GAP):
                    yield upper

    row_top = []
    for index, nodes in enumerate(rows_nodes):
        if index == 0:
            value = top
        else:
            limit = None
            for node in nodes:
                for upper in above(node):
                    candidate = upper.location.y - height[upper] - V_GAP
                    limit = candidate if limit is None else min(limit, candidate)
            if limit is None:
                limit = row_top[index - 1] - V_GAP
            value = min(limit, row_top[index - 1] - V_GAP)
        row_top.append(value)
        for node in nodes:
            node.location.y = value

        # Split the row into touching groups; a group with nothing tall above
        # it can sit higher than the rest of the row.
        ordered = sorted(nodes, key=lambda item: item.location.x)
        groups = []
        for node in ordered:
            if (groups
                    and node.location.x
                    <= groups[-1][-1].location.x + width[groups[-1][-1]] + H_GAP):
                groups[-1].append(node)
            else:
                groups.append([node])
        for group in groups:
            if len(group) == len(nodes):
                continue
            own = None
            for node in group:
                for upper in above(node):
                    candidate = upper.location.y - height[upper] - V_GAP
                    own = candidate if own is None else min(own, candidate)
            if own is not None and own > value:
                for node in group:
                    node.location.y = own

    bottom = min(node.location.y - height[node] for node in block)
    return bottom


def _layout_stage_blocks(stage_nodes, stage_set, upstream, outgoing,
                         width, height):
    """Stack the branches of every TEV stage under one another.

    Inside a block every node is placed one node-width plus ``H_GAP`` to the
    left of the node it feeds, so a chain reads left-to-right with the same
    spacing everywhere.  A TEV texture occupies its whole 500-wide slot (the
    conversion is created inside it), so the slot ends exactly at the stage.
    """
    blocks = {}
    for node, (distance, index) in upstream.items():
        blocks.setdefault(index, []).append(node)

    # Every block is laid out against the same right edge (the first stage),
    # so the blocks line up instead of stepping to the right with their stage.
    base = stage_nodes[0].location.x

    # The first block starts on the stage row itself; the next one is counted
    # from the bottom of the previous block.
    top = 0.0
    for index in range(len(stage_nodes)):
        block = blocks.get(index)
        if not block:
            continue
        stage = stage_nodes[index]
        block_set = set(block)
        by_distance = {}
        for node in block:
            by_distance.setdefault(upstream[node][0], []).append(node)

        for distance in range(1, max(by_distance) + 1):
            for node in by_distance.get(distance, ()):
                consumers = [target.location.x for target in outgoing[node]
                             if target in block_set]
                if any(target in stage_set for target in outgoing[node]):
                    consumers.append(base)
                x = (min(consumers) - width[node] - H_GAP if consumers
                     else base - width[node] - H_GAP)
                if node.type == "TEX_IMAGE" and _is_tev_texture(node):
                    # The texture slot reserves room for "Texture to TEV"
                    # inside itself, so the whole 500-wide block ends at the
                    # block's right edge and its conversion sits 300 to the
                    # right.
                    x = base - width[node]
                node.location.x = x

        bottom = _place_stage_block(block, top, outgoing, stage_set,
                                    width, height)
        top = bottom - STAGE_BLOCK_GAP


def _layout_downstream_rows(nodes, order, incoming, outgoing, stage_set,
                            upstream, height):
    """Pack the stages and everything after them into columns below the row."""
    rest = [node for node in nodes if node not in upstream]
    columns = {}
    for node in rest:
        columns.setdefault(round(node.location.x), []).append(node)
    ordered = [columns[key] for key in sorted(columns)]
    for column in ordered:
        column.sort(key=lambda item: (-item.location.y, order[item]))

    targets = {node: 0.0 for node in rest}
    for _ in range(8):
        for column in ordered:
            column.sort(key=lambda item: (targets[item], order[item]))
            _place_layer(column, targets, height, V_GAP, stage_set)
        for node in rest:
            if node in stage_set:
                targets[node] = 0.0
                continue
            neighbours = [item for item in (outgoing[node] or incoming[node])
                          if item in targets]
            if neighbours:
                targets[node] = (sum(item.location.y for item in neighbours)
                                 / len(neighbours))


def _place_output_chain(nodes, order, incoming, outgoing, topological,
                        stage_set, upstream, width, height):
    """Place every node after the TEV stages to the right of the stage row.

    Only the output side is touched; the stage blocks themselves are left
    exactly where the import put them.
    """
    downstream = [node for node in topological
                  if node not in stage_set and node not in upstream]
    downstream_set = set(downstream)
    for node in downstream:
        node.location.x = 0.0
    for node in topological:
        for target in outgoing[node]:
            if target not in downstream_set:
                continue
            needed = node.location.x + width[node] + H_GAP
            if target.location.x < needed:
                target.location.x = needed
    for node in downstream:
        if incoming[node]:
            continue
        targets = [target for target in outgoing[node]
                   if target in downstream_set]
        if targets:
            node.location.x = (min(item.location.x for item in targets)
                               - width[node] - H_GAP)

    _layout_downstream_rows(nodes, order, incoming, outgoing, stage_set,
                            upstream, height)
    # Additive/multiply previews put two shaders beside the Add node; keep
    # them stacked instead of overlapping each other.
    _resolve_overlaps([node for node in nodes if node not in upstream])


FRAME_PAD_X = 30.0
FRAME_PAD_TOP = 70.0


def _detach_tev_frames(tree):
    """Unparent the children of the TEV frames, keeping their positions."""
    mapping = {}
    for frame in [node for node in tree.nodes
                  if node.type == "FRAME"
                  and node.get("bmd_tev_frame") is not None]:
        mapping[frame] = [node for node in tree.nodes if node.parent == frame]
        for node in mapping[frame]:
            node.parent = None
    return mapping


def _reattach_tev_frames(mapping):
    for frame, nodes in mapping.items():
        for node in nodes:
            node.parent = frame


def _update_tev_frames(tree, upstream):
    """Wrap every TEV stage block in a frame labelled ``TEV <n>``."""
    frames = {}
    for node in list(tree.nodes):
        if node.type == "FRAME" and node.get("bmd_tev_frame") is not None:
            frames[int(node.get("bmd_tev_frame"))] = node
    for frame in frames.values():
        for node in [child for child in tree.nodes if child.parent == frame]:
            node.parent = None

    blocks = {}
    for node, (_distance, index) in upstream.items():
        blocks.setdefault(index, []).append(node)

    for index, frame in list(frames.items()):
        if index not in blocks:
            tree.nodes.remove(frame)
            del frames[index]

    for index, nodes in blocks.items():
        if not nodes:
            continue
        frame = frames.get(index)
        if frame is None:
            frame = tree.nodes.new("NodeFrame")
            frame.name = f"bmd_tev_frame{index}"
            frame["bmd_tev_frame"] = int(index)
            frame.label_size = 24
        frame.label = f"TEV{index + 1}"
        left = min(node.location.x for node in nodes)
        top = max(node.location.y for node in nodes)
        # Park the frame above-left of the block first: parenting keeps the
        # node in place, so the frame must already sit where it belongs.
        frame.location = (left - FRAME_PAD_X, top + FRAME_PAD_TOP)
        for node in nodes:
            node.parent = frame


def _layout_with_stages(tree, nodes, order, incoming, outgoing, topological,
                        stage_nodes, stage_set, width, height):
    """TEV layout: one stage row on top, one input block per stage below it."""
    cursor = 0.0
    for stage in stage_nodes:
        stage.location.x = cursor
        stage.location.y = 0.0
        cursor += STAGE_ROW_GAP

    # Longest path to each stage decides both the column of a node and the
    # block it belongs to (the first stage it reaches).
    distances = {node: {} for node in nodes}
    for index, stage in enumerate(stage_nodes):
        distances[stage][index] = 0
    for node in reversed(topological):
        if node in stage_set:
            continue
        for target in outgoing[node]:
            for index, distance in distances[target].items():
                distances[node][index] = max(
                    distances[node].get(index, -1), distance + 1)

    upstream = {}
    for node in nodes:
        if node in stage_set or not distances[node]:
            continue
        index = min(distances[node],
                    key=lambda item: (distances[node][item], item))
        upstream[node] = (distances[node][index], index)

    _place_output_chain(nodes, order, incoming, outgoing, topological,
                        stage_set, upstream, width, height)
    _layout_stage_blocks(stage_nodes, stage_set, upstream, outgoing,
                         width, height)
    _place_texture_recovery(nodes, tree)

    left = min(node.location.x for node in nodes)
    if left:
        for node in nodes:
            node.location.x -= left

    _update_tev_frames(tree, upstream)


def _graph_data(tree):
    """Nodes, draw order and the two adjacency maps of a node tree."""
    nodes = [node for node in tree.nodes if node.type != "FRAME"]
    order = {node: index for index, node in enumerate(nodes)}
    incoming = {node: [] for node in nodes}
    outgoing = {node: [] for node in nodes}
    seen = set()
    for link in tree.links:
        first, second = link.from_node, link.to_node
        if first == second or (first, second) in seen:
            continue
        seen.add((first, second))
        outgoing[first].append(second)
        incoming[second].append(first)

    indegree = {node: len(incoming[node]) for node in nodes}
    queue = sorted((node for node in nodes if indegree[node] == 0),
                   key=lambda item: order[item])
    topological = []
    while queue:
        node = queue.pop(0)
        topological.append(node)
        for target in outgoing[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
        queue.sort(key=lambda item: order[item])
    for node in nodes:
        if node not in topological:
            topological.append(node)
    return nodes, order, incoming, outgoing, topological


def _layout_tail(tree):
    """Re-place the output chain after a Render Mode switch.

    The stage blocks are not touched; only the nodes downstream of the stages
    are packed again, so a rebuilt BSDF/linear/additive node stays in the chain
    instead of landing at an old default position far to the left.
    """
    frames = _detach_tev_frames(tree)
    nodes, order, incoming, outgoing, topological = _graph_data(tree)
    if not nodes:
        return
    stage_nodes = sorted(
        (node for node in nodes
         if node.get("bmd_anim_role") == "tev_stage"),
        key=lambda item: int(item.get("bmd_tev_index", 0)))
    if not stage_nodes:
        _reattach_tev_frames(frames)
        return
    stage_set = set(stage_nodes)
    upstream = _ancestors(stage_set, incoming)
    width = {node: _node_extent(node)[0] for node in nodes}
    height = {node: _node_extent(node)[1] for node in nodes}
    _place_output_chain(nodes, order, incoming, outgoing, topological,
                        stage_set, upstream, width, height)
    _reattach_tev_frames(frames)


def _ancestors(stage_set, incoming):
    """Every node that feeds a TEV stage (its block), stages excluded."""
    found = set()
    stack = list(stage_set)
    while stack:
        node = stack.pop()
        for source in incoming.get(node, ()):
            if source in stage_set or source in found:
                continue
            found.add(source)
            stack.append(source)
    return found


def _layout_group_tree(nodes, incoming, outgoing, topological, width, height):
    """One block for a node group: same rows and gaps as a stage block."""
    sink = next((node for node in nodes if node.type == "GROUP_OUTPUT"), None)
    if sink is None:
        endings = [node for node in nodes if not outgoing[node]]
        sink = endings[0] if endings else None
    if sink is None:
        return

    sink.location.x = 0.0
    sink.location.y = 0.0
    placed = {sink}
    for node in reversed(topological):
        if node == sink:
            continue
        consumers = [target.location.x for target in outgoing[node]
                     if target in placed]
        if consumers:
            node.location.x = min(consumers) - width[node] - H_GAP
        else:
            node.location.x = (min(item.location.x for item in placed)
                               - width[node] - H_GAP)
        placed.add(node)

    # Group Input and Output wrap the block from outside: Input sits one gap
    # left of it, Output one gap right of it, so the group reads as
    # input | block | output instead of Input being packed into the block.
    source = next((node for node in nodes if node.type == "GROUP_INPUT"), None)
    block = [node for node in nodes if node is not sink and node is not source]
    if block:
        _place_stage_block(block, 0.0, outgoing, {sink}, width, height)
        left = min(node.location.x for node in block)
    else:
        left = sink.location.x
    if source is not None:
        source.location.x = left - width[source] - H_GAP
        source.location.y = 0.0
        left = min(left, source.location.x)
    if left:
        for node in nodes:
            node.location.x -= left


def _layout_tree(tree):
    """Lay a node tree out left-to-right.

    A TEV material is one compact stage row with a block of input branches per
    stage below it; every other tree (a node group) is a single block built the
    same way.
    """
    _detach_tev_frames(tree)
    nodes, order, incoming, outgoing, topological = _graph_data(tree)
    if not nodes:
        return

    width = {node: _node_extent(node)[0] for node in nodes}
    height = {node: _node_extent(node)[1] for node in nodes}

    stage_nodes = sorted(
        (node for node in nodes
         if node.get("bmd_anim_role") == "tev_stage"),
        key=lambda item: int(item.get("bmd_tev_index", 0)))
    if stage_nodes:
        _layout_with_stages(tree, nodes, order, incoming, outgoing,
                            topological, stage_nodes, set(stage_nodes),
                            width, height)
    else:
        _layout_group_tree(nodes, incoming, outgoing, topological,
                           width, height)


def layout_material_tree(material):
    """Recompute every top-level node position for an imported material."""
    tree = getattr(material, "node_tree", None) if material is not None else None
    if tree is None:
        return
    _layout_tree(tree)


def _link(tree, socket, source):
    if socket.is_linked:
        link = socket.links[0]
        if link.from_socket == source:
            return
        tree.links.remove(link)
    tree.links.new(source, socket)


def _unlink(tree, socket):
    for link in list(socket.links):
        tree.links.remove(link)


def _tex_slot(node):
    name = node.name
    if name.startswith("bmd_tex") and name[7:].isdigit():
        return int(name[7:])
    return None


def _uv_is_shared(node):
    """True when one UV Map node feeds more than one texture."""
    return sum(1 for link in node.outputs["UV"].links
               if link.to_node.type == "TEX_IMAGE") > 1


def node_sampler(node):
    """Sampler state cached on a texture node (None when not set yet)."""
    if node is None:
        return None
    wrap_s, wrap_t = node.get("bmd_wrap_s"), node.get("bmd_wrap_t")
    if wrap_s is None or wrap_t is None:
        return None
    return {"wrap_s": int(wrap_s), "wrap_t": int(wrap_t),
            "mag_filter": int(node.get("bmd_mag_filter", 1))}


def set_node_sampler(node, wrap_s, wrap_t, interpolation=None,
                     mag_filter=None):
    """Cache the slot's sampler on the node and apply Blender's equivalent."""
    node["bmd_wrap_s"] = int(wrap_s)
    node["bmd_wrap_t"] = int(wrap_t)
    if mag_filter is not None:
        node["bmd_mag_filter"] = int(mag_filter)
    if interpolation is not None:
        try:
            node.interpolation = interpolation
        except Exception:
            pass


def _image_sampler(image):
    """Legacy sampler stored on the image (old scenes before migration)."""
    if image is None:
        return None
    try:
        return {"wrap_s": int(image.bmd.wrap_s), "wrap_t": int(image.bmd.wrap_t),
                "mag_filter": int(image.bmd.mag_filter)}
    except Exception:
        return None


def _wrap_modes(node):
    sampler = node_sampler(node)
    if sampler is not None:
        return sampler["wrap_s"], sampler["wrap_t"]
    sampler = _image_sampler(node.image)
    if sampler is not None:
        return sampler["wrap_s"], sampler["wrap_t"]
    return 1, 1


def _set_surface_method(material, mode):
    try:
        if mode in (mat3.RENDER_TRANSLUCENT, mat3.RENDER_ADDITIVE):
            method = ("DITHERED" if mode == mat3.RENDER_TRANSLUCENT
                      and dithered_translucency_enabled() else "BLENDED")
        else:
            method = "DITHERED"
        material.surface_render_method = method
    except Exception:
        pass


def dithered_translucency_enabled():
    """Whether the Utility switch renders translucent materials as Dithered."""
    try:
        return bool(bpy.context.scene.bmd_dithered_translucency)
    except Exception:
        return False


def material_is_translucent(material):
    """True when the material's shader or Render Mode makes it Translucent."""
    bmd = getattr(material, "bmd", None)
    if bmd is None or getattr(material, "node_tree", None) is None:
        return False
    mode = bmd.render_mode
    if mode == mat3.RENDER_TRANSLUCENT:
        return True
    if mode != "AUTO":
        return False
    data = material_data(material) or {}
    kind, _ = mat3.classify_alpha(data)
    if kind != mat3.RENDER_TRANSLUCENT:
        return False
    # A TEV material whose blend equation uses no alpha is previewed opaque.
    return not has_tev_graph(material) or mat3.blend_uses_alpha(data)


def apply_dithered_translucency(enabled):
    """Switch every Translucent material between Dithered and Blended."""
    for material in bpy.data.materials:
        if not material_is_translucent(material):
            continue
        try:
            material.surface_render_method = "DITHERED" if enabled else "BLENDED"
        except Exception:
            pass


def main_texture_node(material):
    """The lowest numbered ``bmd_tex<N>`` node of *material*."""
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return None
    nodes = [node for node in tree.nodes
             if node.type == "TEX_IMAGE" and _tex_slot(node) is not None]
    if not nodes:
        return None
    return min(nodes, key=_tex_slot)


def color_attribute_name(material, owner=None):
    """Colour attribute used by the meshes of *material*.

    Returns an empty name when no mesh has a colour attribute: the Color
    Attribute node then shows an empty field instead of a missing ``Col0``.
    """
    for mesh in bc.material_meshes(material, owner):
        attributes = getattr(mesh, "color_attributes", None)
        if attributes is None:
            continue
        for attribute in attributes:
            if attribute.domain in {"CORNER", "POINT"}:
                return attribute.name
    return ""


def material_for_node(node):
    """Material that owns the shader tree of *node*, if any."""
    tree = getattr(node, "id_data", None)
    if tree is None:
        return None
    owner = getattr(tree, "id_data", None)
    if isinstance(owner, bpy.types.Material):
        return owner
    for material in bpy.data.materials:
        if material.node_tree == tree:
            return material
    return None


def _set_wrap_math(node, mode):
    if mode == 1:
        node.operation = "FRACT"
        node.use_clamp = False
    elif mode == 2:
        node.operation = "PINGPONG"
        node.inputs[1].default_value = 1.0
        node.use_clamp = False
    else:
        node.operation = "ADD"
        node.inputs[1].default_value = 0.0
        node.use_clamp = True


def _ensure_wrap_group(material, slot, wrap_s, wrap_t):
    """Group holding the per-axis GX wrap maths for one texture."""
    if material is None:
        return None
    name = f"BMD Wrap {material.name} tex{slot}"
    group = bpy.data.node_groups.get(name)
    if group is None:
        group = bpy.data.node_groups.new(name, "ShaderNodeTree")
        group["bmd_wrap_owner"] = material.name
        group["bmd_wrap_slot"] = int(slot)
        interface = getattr(group, "interface", None)
        if interface is not None:
            interface.new_socket("UV", in_out="INPUT",
                                 socket_type="NodeSocketVector")
            interface.new_socket("UV", in_out="OUTPUT",
                                 socket_type="NodeSocketVector")
        else:  # Blender < 4.0
            group.inputs.new("NodeSocketVector", "UV")
            group.outputs.new("NodeSocketVector", "UV")

        node_in = group.nodes.new("NodeGroupInput")
        node_out = group.nodes.new("NodeGroupOutput")
        separate = group.nodes.new("ShaderNodeSeparateXYZ")
        separate.name = "Separate UV"
        wrap_s_node = group.nodes.new("ShaderNodeMath")
        wrap_s_node.name = "Wrap S"
        wrap_t_node = group.nodes.new("ShaderNodeMath")
        wrap_t_node.name = "Wrap T"
        combine = group.nodes.new("ShaderNodeCombineXYZ")
        combine.name = "Combine UV"
        group.links.new(node_in.outputs["UV"], separate.inputs["Vector"])
        group.links.new(separate.outputs["X"], wrap_s_node.inputs[0])
        group.links.new(separate.outputs["Y"], wrap_t_node.inputs[0])
        group.links.new(wrap_s_node.outputs[0], combine.inputs["X"])
        group.links.new(wrap_t_node.outputs[0], combine.inputs["Y"])
        combine.inputs["Z"].default_value = 0.0
        group.links.new(combine.outputs["Vector"], node_out.inputs["UV"])
        _layout_tree(group)

    for axis, mode in (("Wrap S", wrap_s), ("Wrap T", wrap_t)):
        node = group.nodes.get(axis)
        if node is not None:
            _set_wrap_math(node, mode)
    return group


def _current_texture_source(node, wrap_node=None):
    if node.inputs["Vector"].is_linked:
        link = node.inputs["Vector"].links[0]
        if link.from_node != wrap_node:
            return link.from_socket
    if wrap_node is not None and wrap_node.inputs["UV"].is_linked:
        return wrap_node.inputs["UV"].links[0].from_socket
    return None


def _abs_xy(node):
    """Node position in the editor, frames included."""
    x, y = node.location.x, node.location.y
    parent = node.parent
    while parent is not None:
        x += parent.location.x
        y += parent.location.y
        parent = parent.parent
    return x, y


def _upstream_chain(node):
    """Every node feeding *node*, itself included."""
    found = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        for socket in current.inputs:
            for link in socket.links:
                if link.from_node not in found:
                    stack.append(link.from_node)
    return found


def _place_uv_node(uv, texture_node):
    """Put a UV Map node on the texture's row, one gap to the left."""
    frame = texture_node.parent
    if frame is not None:
        uv.parent = frame
    uv.location = (texture_node.location.x - _node_extent(uv)[0] - H_GAP,
                   texture_node.location.y)


def _align_wrap_chain(texture_node, wrap_node, source):
    """Shift the branch left of *source* so the node gaps stay ``H_GAP``."""
    if source is None or not hasattr(source, "node"):
        return
    producer = source.node
    width = _node_extent(producer)[0]
    edge = _abs_xy(wrap_node if wrap_node is not None else texture_node)[0]
    delta = edge - H_GAP - (_abs_xy(producer)[0] + width)
    if abs(delta) < 0.5:
        return
    for node in _upstream_chain(producer):
        node.location.x += delta


def apply_wrap_to_node(node, wrap_s, wrap_t, material=None):
    """Apply the GX per-axis wrap (0 clamp, 1 repeat, 2 mirror) to *node*."""
    tree = node.id_data
    if material is None:
        material = material_for_node(node)
    slot = _tex_slot(node)
    suffix = slot if slot is not None else node.name.replace(" ", "_")
    wrap_name = f"bmd_wrap_tex{suffix}"
    wrap_node = tree.nodes.get(wrap_name)
    if wrap_node is not None and wrap_node.type != "GROUP":
        tree.nodes.remove(wrap_node)
        wrap_node = None
    source = _current_texture_source(node, wrap_node)

    if wrap_s == wrap_t:
        if source is None:
            uv = _uv_node(tree, material, 0, slot)
            _place_uv_node(uv, node)
            source = uv.outputs["UV"]
        _link(tree, node.inputs["Vector"], source)
        if wrap_node is not None:
            old_group = wrap_node.node_tree
            tree.nodes.remove(wrap_node)
            if old_group is not None and old_group.users == 0:
                bpy.data.node_groups.remove(old_group)
        _align_wrap_chain(node, None, source)
        node.extension = WRAP_EXT.get(wrap_s, "REPEAT")
        return

    if source is None:
        uv = _uv_node(tree, material, 0, slot)
        _place_uv_node(uv, node)
        source = uv.outputs["UV"]
    group = _ensure_wrap_group(material, slot, wrap_s, wrap_t)
    if group is None:
        return
    if wrap_node is None:
        wrap_node = tree.nodes.new("ShaderNodeGroup")
        wrap_node.name = wrap_name
        wrap_node.label = f"Wrap tex{suffix}"
        wrap_node["bmd_wrap_for"] = node.name
        wrap_node["bmd_wrap_slot"] = int(slot) if slot is not None else -1
    if node.parent is not None and wrap_node.parent != node.parent:
        wrap_node.parent = node.parent
    # Same row as the texture, one wrap-width plus one gap to the left.
    wrap_node.location = (node.location.x - _node_extent(wrap_node)[0] - H_GAP,
                          node.location.y)
    wrap_node.node_tree = group
    wrap_node["bmd_wrap_for"] = node.name
    _link(tree, wrap_node.inputs["UV"], source)
    _link(tree, node.inputs["Vector"], wrap_node.outputs["UV"])
    _align_wrap_chain(node, wrap_node, source)
    node.extension = "EXTEND"




def texture_node_location(tree, slot):
    """Node position the BMD importer gives ``bmd_tex<N>``."""
    bsdf = next((node for node in tree.nodes
                 if node.type == "BSDF_PRINCIPLED"), None)
    x = bsdf.location.x if bsdf is not None else 800.0
    y = bsdf.location.y if bsdf is not None else 0.0

    return (x - 900.0, y - 320.0 * int(slot))


def uv_node_location(texture_node):
    """Node position the BMD importer gives the UV Map node of a texture."""
    x, y = texture_node.location

    return (x - 200.0, y)


# ---------------------------------------------------------------------------
# building blocks shared by the importer and the interactive tools
# ---------------------------------------------------------------------------

def ensure_output_bsdf(tree):
    """Create the Material Output / Principled BSDF pair if missing.

    Imported materials already have a calculated layout; only custom/new
    materials get the fixed default positions.  A pair that is re-created after
    an Additive/Replace preview (which removes the BSDF) is placed next to the
    Material Output that is already in the tree, so switching the Render Mode
    back and forth keeps the nodes where they belong.
    """
    bsdf = _node(tree, "BSDF_PRINCIPLED")
    output = _node(tree, "OUTPUT_MATERIAL")
    material = material_for_node(bsdf or output)
    imported = bool(material is not None
                    and material.get("bmd_material"))
    created_bsdf = bsdf is None
    created_output = output is None
    if created_bsdf:
        bsdf = tree.nodes.new("ShaderNodeBsdfPrincipled")
    if created_output:
        output = tree.nodes.new("ShaderNodeOutputMaterial")
    if created_bsdf:
        home = output.get("bmd_bsdf_home")
        if home is not None and len(home) == 2:
            # The pair was split by an Additive/Replace preview: put the BSDF
            # back exactly where it was, so repeated switches never drift.
            bsdf.location = (home[0], home[1])
        elif not created_output:
            bsdf.location = (output.location.x - 300.0, output.location.y)
        else:
            bsdf.location = (800.0, 0.0)
    elif not imported:
        bsdf.location = (800.0, 0.0)

    if created_output:
        output.location = ((bsdf.location.x + 300.0, bsdf.location.y)
                           if not created_bsdf else (1100.0, 0.0))
    elif not imported:
        output.location = (1100.0, 0.0)

    if not output.inputs["Surface"].is_linked:
        tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])

    for socket, value in (("Roughness", 1.0), ("Specular IOR Level", 0.0),
                          ("Specular", 0.0)):
        if socket in bsdf.inputs:
            bsdf.inputs[socket].default_value = value

    return output, bsdf


def _is_simple_preview(material):
    if material is None:
        return False
    if material.get("bmd_material") or material.get("bmd_shader_active"):
        return False
    return bool(material.get("bmd_simple") or material.get("bmd_simple_textures"))


def _mark_simple_textures(material):
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return
    for node in tree.nodes:
        if node.type == "TEX_IMAGE":
            node["bmd_simple_texture"] = 1
            try:
                node.width = 240.0
            except Exception:
                pass


def ensure_texture_node(material, slot, image=None, uv_index=None,
                        attach_uv=False, link_base=False, sampler=None):
    """Create/update the material's ``bmd_tex<N>`` node.

    *image* may be ``None``: the node is created empty so a texture assigned
    later fills it in.  With *attach_uv* the node gets a UV Map link and the
    GX per-axis wrap chain; otherwise only the single extension mode is set.
    *sampler* is the TEX1 entry's sampler state (wrap/filter); the node keeps
    its own copy so the image can be swapped without losing it.
    """
    tree = tree_of(material)
    if tree is None:
        return None
    slot = 0 if slot is None else max(0, int(slot))
    name = f"bmd_tex{slot}"

    node = tree.nodes.get(name)
    if node is not None and node.type != "TEX_IMAGE":
        tree.nodes.remove(node)
        node = None

    if node is None:
        node = tree.nodes.new("ShaderNodeTexImage")
        node.name = name
        node.location = texture_node_location(tree, slot)
    node.label = f"tex{slot}"

    if image is not None and node.image != image:
        node.image = image
    if sampler is None:
        sampler = node_sampler(node) or _image_sampler(node.image)
    wrap_s = int(sampler["wrap_s"]) if sampler else 1
    wrap_t = int(sampler["wrap_t"]) if sampler else 1
    mag = int(sampler.get("mag_filter", 1)) if sampler else 1
    if node.image is not None:
        try:
            node.interpolation = bc.image_interpolation(mag)
        except Exception:
            pass

    if attach_uv:
        uv = tree.nodes.get(f"bmd_uv_tex{slot}")
        if uv is not None and uv.type != "UVMAP":
            tree.nodes.remove(uv)
            uv = None
        if uv is None:
            linked_uv = None
            if node.inputs["Vector"].is_linked:
                link = node.inputs["Vector"].links[0]
                if link.from_node.type == "UVMAP":
                    linked_uv = link.from_node
            if (linked_uv is not None and not _uv_is_shared(linked_uv)
                    and linked_uv.get("bmd_tex_slot") in (None, slot)):
                uv = linked_uv
                uv.name = f"bmd_uv_tex{slot}"
                uv["bmd_tex_slot"] = slot
            else:
                uv = tree.nodes.new("ShaderNodeUVMap")
                uv.name = f"bmd_uv_tex{slot}"
                uv["bmd_tex_slot"] = slot
                uv.location = uv_node_location(node)
        uv.uv_map = bc.uv_layer_name(material, uv_index or 0)
        source = (node.inputs["Vector"].links[0].from_node
                  if node.inputs["Vector"].is_linked else None)
        if source is None or (source.type == "UVMAP" and source != uv):
            tree.links.new(uv.outputs["UV"], node.inputs["Vector"])
        set_node_sampler(node, wrap_s, wrap_t, mag_filter=mag)
        apply_wrap_to_node(node, wrap_s, wrap_t, material)
    else:
        set_node_sampler(node, wrap_s, wrap_t, mag_filter=mag)
        node.extension = WRAP_EXT.get(wrap_s, "REPEAT")

    if link_base:
        bsdf = _node(tree, "BSDF_PRINCIPLED")
        if bsdf is not None and not bsdf.inputs["Base Color"].is_linked:
            tree.links.new(node.outputs["Color"],
                           bsdf.inputs["Base Color"])

    if _is_simple_preview(material):
        node["bmd_simple_texture"] = 1
        try:
            node.width = 240.0
        except Exception:
            pass
    elif "bmd_simple_texture" in node:
        del node["bmd_simple_texture"]

    return node


def add_vertex_color(tree, bsdf, layer_name=""):
    """Reuse/create the vertex colour node (tagged for later rebuilds)."""
    vc = next((node for node in tree.nodes
               if node.get(ROLE_PROP) == ROLE_VC), None)
    if vc is None:
        vc = tree.nodes.new("ShaderNodeVertexColor")
        vc[ROLE_PROP] = ROLE_VC
        vc.location = (bsdf.location.x - 580.0, bsdf.location.y - 240.0)
    vc.layer_name = layer_name
    return vc


def apply_color(tree, bsdf, color_source, vc=None):
    """Link Base Color, multiplying by the vertex colour when *vc* is given."""
    if vc is not None and color_source is not None:
        mix = tree.nodes.new("ShaderNodeMix")
        mix[ROLE_PROP] = ROLE_VC_MIX
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.inputs[0].default_value = 1.0
        mix.location = (bsdf.location.x - 200.0, bsdf.location.y)
        tree.links.new(color_source, mix.inputs[6])
        tree.links.new(vc.outputs["Color"], mix.inputs[7])
        color_source = mix.outputs[2]
    elif vc is not None:
        color_source = vc.outputs["Color"]

    if color_source is not None:
        _link(tree, bsdf.inputs["Base Color"], color_source)
    else:
        _unlink(tree, bsdf.inputs["Base Color"])

    return color_source


def alpha_multiply(tree, bsdf, first, second):
    node = tree.nodes.new("ShaderNodeMath")
    node[ROLE_PROP] = ROLE_ALPHA_MUL
    node.operation = "MULTIPLY"
    node.location = (bsdf.location.x - 380.0, bsdf.location.y - 240.0)
    tree.links.new(first, node.inputs[0])
    tree.links.new(second, node.inputs[1])
    return node.outputs[0]


def alpha_cutout(tree, bsdf, source, alpha_ref):
    node = tree.nodes.new("ShaderNodeMath")
    node[ROLE_PROP] = ROLE_ALPHA_CUTOUT
    node.operation = "GREATER_THAN"
    node.inputs[1].default_value = max(0.0,
                                       (float(alpha_ref) - 0.5) / 255.0)
    node.location = (bsdf.location.x - 200.0, bsdf.location.y - 240.0)
    tree.links.new(source, node.inputs[0])
    return node.outputs[0]


def apply_alpha(material, tree, bsdf, sources, mode, alpha_ref=128):
    """Build the alpha chain from *sources* for *mode* and link BSDF Alpha."""
    if mode == mat3.RENDER_OPAQUE or not sources:
        _unlink(tree, bsdf.inputs["Alpha"])
        _set_surface_method(material, mat3.RENDER_OPAQUE)
        return

    alpha = sources[0]
    for extra in sources[1:]:
        alpha = alpha_multiply(tree, bsdf, alpha, extra)
    if mode == mat3.RENDER_CUTOUT:
        cutout = alpha_cutout(tree, bsdf, alpha, alpha_ref)
        if material.get("bmd_material") and hasattr(alpha, "node"):
            source = alpha.node
            cutout.node.location = (
                (source.location.x + bsdf.location.x) / 2.0,
                source.location.y - 120.0)
        alpha = cutout
    elif mode == mat3.RENDER_TRANSLUCENT:
        # The game compares the final alpha even for a blended material and
        # discards the fragment before blending; without the test a clamped
        # transparent texel still tints the surface instead of hiding it.
        test_ref = _translucent_alpha_test(material, alpha_ref)
        if test_ref is not None:
            test = alpha_cutout(tree, bsdf, alpha, test_ref)
            if material.get("bmd_material") and hasattr(alpha, "node"):
                source = alpha.node
                test.node.location = (
                    (source.location.x + bsdf.location.x) / 2.0,
                    source.location.y - 120.0)
            alpha = alpha_multiply(tree, bsdf, alpha, test)
    _link(tree, bsdf.inputs["Alpha"], alpha)
    _set_surface_method(material, mode)


def _translucent_alpha_test(material, alpha_ref):
    """Alpha compare reference for a Translucent preview, if any.

    An imported ``AUTO`` material follows the stored GX alpha compare; an
    explicit Translucent mode uses the Alpha Reference field like Cutout.
    """
    bmd = getattr(material, "bmd", None)
    if bmd is None:
        return None
    if bmd.render_mode == "AUTO":
        return mat3.alpha_test_reference(material_data(material) or {})
    if bmd.render_mode == mat3.RENDER_TRANSLUCENT:
        return int(alpha_ref)
    return None


def _link_bsdf_output(material, tree):
    output = _node(tree, "OUTPUT_MATERIAL")
    bsdf = _node(tree, "BSDF_PRINCIPLED")
    if output is None or bsdf is None:
        return
    if not output.inputs["Surface"].is_linked:
        tree.links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])


def blend_preview_kind(material, data):
    """Which special output a material needs: 'additive', 'replace' or None.

    ``AUTO`` follows the material's GX blend equation; an explicit Opaque /
    Cutout / Translucent mode keeps the ordinary Principled path.

    GX blends divide into three cases by the *destination* factor:
    ``dst = ONE``  -> the source is added (additive),
    ``dst = ZERO`` -> the source replaces the framebuffer (opaque),
    anything else  -> the usual alpha blend through the Principled alpha.
    """
    mode = material.bmd.render_mode if getattr(material, "bmd", None) else "AUTO"
    if mode != "AUTO":
        return None
    if mat3.blend_is_additive(data):
        return "additive"
    if mat3.blend_is_source_squared(data):
        return "replace"
    return None


def _additive_terms(tree, color, alpha, source, base=(1240.0, -240.0)):
    """``(emission colour, strength)`` for ``src * srcFactor``.

    GX multiplies the source colour by the source factor per channel, so
    ``SRCCOLOR`` gives ``colour²`` and ``INVSRCCOLOR`` gives
    ``colour·(1 − colour)``; the alpha factors scale every channel equally.
    The helper nodes are placed relative to *base* (the Emission node), so the
    whole block travels with a Render Mode switch.
    """
    def at(dx, dy):
        return (base[0] + dx, base[1] + dy)

    if source == 1:                          # ONE
        return color, 1.0
    if source == 4:                          # SRCALPHA
        return color, alpha if alpha is not None else 1.0
    if source == 5:                          # INVSRCALPHA
        if alpha is None:
            return color, 1.0
        inv = _new_math(tree, "SUBTRACT", 1.0, alpha, at(-240.0, -180.0))
        inv[ROLE_PROP] = ROLE_BLEND_ADD
        return color, inv.outputs[0]
    if color is not None and source in (2, 3):   # SRCCOLOR / INVSRCCOLOR
        factor = color
        if source == 3:
            factor = _vsub(tree, (1.0, 1.0, 1.0, 1.0), color,
                           at(-240.0, -280.0))
            node = getattr(factor, "node", None)
            if node is not None:
                node[ROLE_PROP] = ROLE_BLEND_ADD
        scaled = _vmul(tree, color, factor, at(-80.0, -180.0))
        node = getattr(scaled, "node", None)
        if node is not None:
            node[ROLE_PROP] = ROLE_BLEND_ADD
        return scaled, 1.0
    if source == 0:                          # ZERO
        return (0.0, 0.0, 0.0), 1.0
    return color, 1.0


def _blend_origin(tree, output, anchor=None):
    """Where the Principled BSDF sits, so the blend block can be placed there.

    A Render Mode switch removes the BSDF before building the additive or
    replace output, so the caller passes its location along.  Without an anchor
    the BSDF (or, failing that, one column left of the Material Output) is used.
    """
    if anchor is not None:
        return anchor
    bsdf = _node(tree, "BSDF_PRINCIPLED")
    if bsdf is not None:
        return (bsdf.location.x, bsdf.location.y)
    return (output.location.x - 300.0, output.location.y)


def _build_additive_output(material, tree, color, alpha, data, anchor=None):
    """GX ``dst + src * srcFactor`` (``dstFactor = ONE``).

    The scene behind the surface stays visible and only the TEV colour scaled by
    the stored source factor is added.  The source factor is applied straight to
    the alpha (``SRCALPHA`` is by far the common case); an alpha test is not
    emulated here - the additive pass keeps the soft edge instead of adding a
    second comparison chain for the few texels below the reference.
    """
    output = _node(tree, "OUTPUT_MATERIAL")
    if output is None:
        return False
    transparent = tree.nodes.new("ShaderNodeBsdfTransparent")
    emission = tree.nodes.new("ShaderNodeEmission")
    add = tree.nodes.new("ShaderNodeAddShader")
    for node in (transparent, emission, add):
        node[ROLE_PROP] = ROLE_BLEND_ADD
    # The Add Shader takes the Principled BSDF's place, one column to the right,
    # and the nodes feeding it keep their offset from it.
    origin = _blend_origin(tree, output, anchor)
    add.location = (origin[0] + 170.0, origin[1])
    transparent.location = (add.location.x - 200.0, add.location.y)
    emission.location = (add.location.x - 200.0, add.location.y - 120.0)

    kind, source, _destination = mat3.blend_equation(data)
    emission_color, strength = _additive_terms(
        tree, color, alpha, source,
        (emission.location.x, emission.location.y))
    if emission_color is not None:
        if hasattr(emission_color, "is_linked"):
            _link(tree, emission.inputs["Color"], emission_color)
        else:
            emission.inputs["Color"].default_value = _as_rgb(emission_color)

    if hasattr(strength, "is_linked"):
        _link(tree, emission.inputs["Strength"], strength)
    else:
        emission.inputs["Strength"].default_value = float(strength)

    tree.links.new(transparent.outputs[0], add.inputs[0])
    tree.links.new(emission.outputs[0], add.inputs[1])
    _unlink(tree, output.inputs["Surface"])
    tree.links.new(add.outputs[0], output.inputs["Surface"])
    _set_surface_method(material, mat3.RENDER_ADDITIVE)
    return True


def _build_replace_output(material, tree, color, data, anchor=None):
    """Preview a GX blend with ``dstFactor = ZERO``: the framebuffer is replaced.

    The stage output becomes ``src * srcFactor`` (``SRCCOLOR`` therefore gives
    ``src²``), so the surface is **opaque** — no transparency is invented here.
    It keeps the TEV colour unlit, like every other TEV material.
    """
    if color is None:
        return False
    output = _node(tree, "OUTPUT_MATERIAL")
    if output is None:
        return False
    origin = _blend_origin(tree, output, anchor)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission[ROLE_PROP] = ROLE_BLEND_MULT
    emission.location = (origin[0] + 170.0, origin[1])

    kind, source, _destination = mat3.blend_equation(data)
    scaled = color
    if source == 2:                          # SRCCOLOR -> src²
        scaled = _new_vmath(tree, "MULTIPLY", color, color,
                            (emission.location.x - 400.0, emission.location.y))
        scaled[ROLE_PROP] = ROLE_BLEND_MULT
        scaled = scaled.outputs["Vector"]
    elif source == 0:                        # ZERO
        scaled = (0.0, 0.0, 0.0)
    # The conversion goes between the multiply (or stage colour) and the
    # emission.  It is the same "TEV to linear" node the normal preview uses,
    # so switching back to Auto never leaves a second copy behind.
    linear = _srgb_to_linear_node(tree, scaled)
    tree.nodes["bmd_tev_linear"][ROLE_PROP] = ROLE_BLEND_MULT
    _link(tree, emission.inputs["Color"], linear)
    emission.inputs["Strength"].default_value = 1.0
    _unlink(tree, output.inputs["Surface"])
    tree.links.new(emission.outputs[0], output.inputs["Surface"])
    # GX draws this material with `zMode update = 0`, so it must not write depth:
    # the transparent surfaces that follow it in the material order (the pond
    # bottom) then blend over it, exactly like the game's water pass.
    _set_surface_method(material, mat3.RENDER_TRANSLUCENT)
    return True


# ---------------------------------------------------------------------------
# BMD import (TEV data)
# ---------------------------------------------------------------------------

ANIM_PROP = "bmd_anim_role"
ANIM_TEV_STAGE = "tev_stage"
ANIM_TEV_RAS = "ras"
ANIM_TEV_VC = "ras_vc"
ANIM_BTK = "btk"
ANIM_BRK = "brk"
ANIM_BPK = "bpk"


_KONST_VALUES = (1.0, 223.0 / 255.0, 191.0 / 255.0, 159.0 / 255.0,
                 128.0 / 255.0, 96.0 / 255.0, 64.0 / 255.0, 32.0 / 255.0)
_SCALE_VALUES = {0: 1.0, 1: 2.0, 2: 4.0, 3: 0.5}
_BIAS_VALUES = {0: 0.0, 1: 0.5, 2: -0.5, 3: 0.0}
_KONST_CHANNELS = ("Red", "Green", "Blue")
_IDENTITY_TEXMTX = {"projection": 1, "type": 0, "center": [0.5, 0.5, 0.5],
                    "scale": [1.0, 1.0], "rotation": 0,
                    "translation": [0.0, 0.0],
                    "matrix": [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                               0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]}


def _bmd_component(value, wrap=True):
    """One BMD colour component (0..1).

    The TEV registers are s16 in the file; GX wraps an A/B/C input to 8 bits,
    so a negative component is the low byte of the colour (``-130`` is
    ``126``).  ``wrap=False`` keeps the signed value, which is what the D
    input sees (it may subtract from the result).
    """
    try:
        value = int(round(float(value)))
    except (TypeError, ValueError):
        return 0.0
    if wrap:
        if value < 0:
            value &= 0xFF
        value = max(0, min(255, value))
    else:
        value = max(-255, min(255, value))
    return value / 255.0


def _bmd_rgba(values, wrap=True):
    """Raw BMD colour values (0..1), as used by the TEV combiner."""
    values = list(values or []) + [255] * 4
    return tuple(_bmd_component(values[index], wrap) for index in range(4))


def _alpha_byte(value):
    try:
        return max(0.0, min(1.0, float(value) / 255.0))
    except (TypeError, ValueError):
        return 1.0


def _put(tree, socket, value):
    """Link *value* to *socket*, or use it as the socket's default."""
    if hasattr(value, "is_linked"):
        tree.links.new(value, socket)
        return
    if socket.type == "RGBA":
        if isinstance(value, (int, float)):
            value = (float(value), float(value), float(value), 1.0)
        elif isinstance(value, (list, tuple)):
            values = list(value) + [1.0] * (4 - len(value))
            value = tuple(values[:4])
    elif socket.type == "VALUE" and isinstance(value, (list, tuple)):
        value = float(value[0]) if value else 0.0
    socket.default_value = value


def _new_vmath(tree, operation, first, second, location=(-600.0, 0.0)):
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    node.location = location
    for index, value in ((0, first), (1, second)):
        if isinstance(value, tuple):
            node.inputs[index].default_value = value[:3]
        else:
            tree.links.new(value, node.inputs[index])
    return node


def _new_math(tree, operation, first, second, location=(-400.0, 0.0),
              clamp=False):
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    node.use_clamp = clamp
    node.location = location
    for index, value in ((0, first), (1, second)):
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = float(value)
        else:
            tree.links.new(value, node.inputs[index])
    return node


def _is_const(value):
    return not hasattr(value, "is_linked")


def _as_rgb(value):
    if isinstance(value, (list, tuple)):
        values = list(value) + [1.0] * 4
    else:
        values = [value] * 3 + [1.0]
    return tuple(float(v) for v in values[:4])


def _is_zero(value):
    if isinstance(value, (list, tuple)):
        return all(abs(float(v)) < 1e-6 for v in value[:3])
    if isinstance(value, (int, float)):
        return abs(float(value)) < 1e-6
    return False


def _is_one(value):
    if isinstance(value, (list, tuple)):
        return all(abs(float(v) - 1.0) < 1e-6 for v in value[:3])
    if isinstance(value, (int, float)):
        return abs(float(value) - 1.0) < 1e-6
    return False


def _bounds(value):
    if isinstance(value, (list, tuple)):
        values = value[:3] if len(value) >= 3 else value
        return (min(float(v) for v in values),
                max(float(v) for v in values))
    if isinstance(value, (int, float)):
        return (float(value), float(value))
    return (0.0, 1.0)


def _mul_bounds(first, second):
    values = (first[0] * second[0], first[0] * second[1],
              first[1] * second[0], first[1] * second[1])
    return min(values), max(values)


def _vadd(tree, first, second, location):
    if _is_zero(first):
        return second
    if _is_zero(second):
        return first
    if _is_const(first) and _is_const(second):
        a, b = _as_rgb(first), _as_rgb(second)
        return tuple(a[index] + b[index] for index in range(4))
    return _new_vmath(
        tree, "ADD", _as_rgb(first) if _is_const(first) else first,
        _as_rgb(second) if _is_const(second) else second,
        location).outputs["Vector"]


def _vsub(tree, first, second, location):
    if _is_zero(second):
        return first
    if _is_const(first) and _is_const(second):
        a, b = _as_rgb(first), _as_rgb(second)
        return tuple(a[index] - b[index] for index in range(4))
    return _new_vmath(
        tree, "SUBTRACT", _as_rgb(first) if _is_const(first) else first,
        _as_rgb(second) if _is_const(second) else second,
        location).outputs["Vector"]


def _vmul(tree, first, second, location):
    if _is_zero(first) or _is_zero(second):
        return 0.0, 0.0, 0.0, 1.0
    if _is_one(first):
        return second
    if _is_one(second):
        return first
    if _is_const(first) and _is_const(second):
        a, b = _as_rgb(first), _as_rgb(second)
        return tuple(a[index] * b[index] for index in range(4))
    return _new_vmath(
        tree, "MULTIPLY", _as_rgb(first) if _is_const(first) else first,
        _as_rgb(second) if _is_const(second) else second,
        location).outputs["Vector"]


def _vscale(tree, value, scale, location):
    if scale == 1.0:
        return value
    if _is_const(value):
        a = _as_rgb(value)
        return tuple(a[index] * scale for index in range(4))
    return _new_vmath(tree, "MULTIPLY", value, (scale, scale, scale),
                      location).outputs["Vector"]


def _vclamp(tree, value, bounds, location):
    if bounds[0] < -1e-6:
        value = _new_vmath(tree, "MAXIMUM", value, (0.0, 0.0, 0.0),
                           location).outputs["Vector"]
    if bounds[1] > 1.0 + 1e-6:
        value = _new_vmath(tree, "MINIMUM", value, (1.0, 1.0, 1.0),
                           (location[0] + 100.0, location[1])).outputs["Vector"]
    return value


def _fadd(tree, first, second, location):
    if _is_zero(first):
        return second
    if _is_zero(second):
        return first
    if _is_const(first) and _is_const(second):
        return float(first) + float(second)
    return _new_math(
        tree, "ADD", first if not _is_const(first) else float(first),
        second if not _is_const(second) else float(second),
        location).outputs[0]


def _fsub(tree, first, second, location):
    if _is_zero(second):
        return first
    if _is_const(first) and _is_const(second):
        return float(first) - float(second)
    return _new_math(
        tree, "SUBTRACT", first if not _is_const(first) else float(first),
        second if not _is_const(second) else float(second),
        location).outputs[0]


def _fmul(tree, first, second, location):
    if _is_zero(first) or _is_zero(second):
        return 0.0
    if _is_one(first):
        return second
    if _is_one(second):
        return first
    if _is_const(first) and _is_const(second):
        return float(first) * float(second)
    return _new_math(
        tree, "MULTIPLY", first if not _is_const(first) else float(first),
        second if not _is_const(second) else float(second),
        location).outputs[0]


def _fscale(tree, value, scale, location):
    if scale == 1.0:
        return value
    if _is_const(value):
        return float(value) * scale
    return _new_math(tree, "MULTIPLY", value, scale,
                     location).outputs[0]


def _fclamp(tree, value, bounds, location):
    if bounds[0] >= -1e-6 and bounds[1] <= 1.0 + 1e-6:
        return value
    node = tree.nodes.new("ShaderNodeClamp")
    node.location = location
    node.inputs["Min"].default_value = 0.0
    node.inputs["Max"].default_value = 1.0
    if _is_const(value):
        node.inputs["Value"].default_value = float(value)
    else:
        tree.links.new(value, node.inputs["Value"])
    return node.outputs["Result"]


def _color_socket(tree, value, location):
    if not _is_const(value):
        return value
    node = tree.nodes.new("ShaderNodeRGB")
    node.location = location
    node.outputs[0].default_value = _as_rgb(value)
    return node.outputs[0]


def _alpha_socket(tree, value, location):
    if not _is_const(value):
        return value
    node = tree.nodes.new("ShaderNodeValue")
    node.location = location
    node.outputs[0].default_value = float(value)
    return node.outputs[0]


def _anim_rgb(tree, name, role, values, location, label, table=None,
              index=None, wrap=True):
    node = tree.nodes.get(name)
    if node is None or node.type != "RGB":
        if node is not None:
            tree.nodes.remove(node)
        node = tree.nodes.new("ShaderNodeRGB")
        node.name = name
        node[ANIM_PROP] = role
        node["bmd_anim_component"] = "rgb"
        if table is not None:
            node["bmd_anim_table"] = table
        if index is not None:
            node["bmd_anim_index"] = int(index)
        node.outputs[0].default_value = _bmd_rgba(values, wrap)
    node.label = label
    node.location = location
    return node


def _anim_alpha(tree, name, role, values, location, label, table=None,
                index=None, wrap=True):
    node = tree.nodes.get(name)
    if node is None or node.type != "VALUE":
        if node is not None:
            tree.nodes.remove(node)
        node = tree.nodes.new("ShaderNodeValue")
        node.name = name
        node[ANIM_PROP] = role
        node["bmd_anim_component"] = "alpha"
        if table is not None:
            node["bmd_anim_table"] = table
        if index is not None:
            node["bmd_anim_index"] = int(index)
        node.outputs[0].default_value = _bmd_component(values[3], wrap)
    node.label = label
    node.location = location
    return node


def material_data(material):
    """The material's active TEV data, or ``None``.

    A shader preset imported into the material is stored next to the original
    MAT3 data; while it is active every consumer (preview, export) reads it.
    """
    raw = bc.active_material_json(material) if material is not None else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _used_stages(data):
    entries = data.get("tevStage") or []
    count = min(int(data.get("tevStageCount", 0) or 0), len(entries))
    return [(index, entries[index]) for index in range(count)
            if entries[index]]


def _raster_tainted_stages(stages):
    """Stage indices whose colour depends on the lit raster colour.

    Tracks ``prev`` and the C0-C2 registers through the stage list, so a
    raster-derived value written early and consumed later (possibly by a
    stage that does not name RASC itself) is still detected.
    """
    prev_color = False
    prev_alpha = False
    colors = [False, False, False]
    alphas = [False, False, False]
    tainted = set()

    for index, stage in stages:
        def color_arg(arg):
            arg = int(arg)
            if arg == 0:
                return prev_color
            if arg == 1:
                return prev_alpha
            if 2 <= arg <= 7:
                slot = (arg - 2) // 2
                return alphas[slot] if arg & 1 else colors[slot]
            return arg in (10, 11)

        def alpha_arg(arg):
            arg = int(arg)
            if arg == 0:
                return prev_alpha
            if 1 <= arg <= 3:
                return alphas[arg - 1]
            return arg == 5

        color_in = [int(arg) for arg in (stage.get("colorIn") or [])]
        alpha_in = [int(arg) for arg in (stage.get("alphaIn") or [])]
        if len(color_in) < 4:
            color_in += [15] * (4 - len(color_in))
        first, second, factor, _last = color_in[:4]
        compare = int(stage.get("colorBias", 0) or 0) == 3

        def active(position, dependent):
            if not dependent or compare:
                return dependent
            if position == 0 and factor == 12:      # A * (1 - ONE)
                return False
            if position == 1 and factor == 15:      # B * ZERO
                return False
            if position == 2 and first == second:   # lerp factor is unused
                return False
            return True

        stage_color = any(active(position, color_arg(arg))
                          for position, arg in enumerate(color_in))
        stage_alpha = any(alpha_arg(arg) for arg in alpha_in)
        if stage_color:
            tainted.add(index)

        color_reg = int(stage.get("colorReg", 0) or 0)
        if color_reg in (1, 2, 3):
            colors[color_reg - 1] = stage_color
        else:
            prev_color = stage_color
        alpha_reg = int(stage.get("alphaReg", 0) or 0)
        if alpha_reg in (1, 2, 3):
            alphas[alpha_reg - 1] = stage_alpha
        else:
            prev_alpha = stage_alpha
    return tainted


def _raster_is_lit(data):
    """True when the raster colour of channel 0 comes from GX lighting.

    The first byte of a colour channel entry is GX's lighting-enable flag.  A
    lit raster is multiplied by the ambient and the lights, so the game's value
    is much darker than the full-brightness raster the preview feeds and a TEV
    colour scale compensates it.  An unlit channel outputs its source colour
    (material or vertex colour) directly, so the preview matches the game and
    the scale must stay.
    """
    channels = data.get("colorChan") or []
    channel = channels[0] if channels else None
    if not channel:
        return False
    return bool(int(channel[0]))


def _raster_cancel_stages(stages, data=None):
    """Stages whose stored colour scale compensates the lit raster.

    A stage that multiplies the raster itself (a x2/x4 on RASC) is scaled by
    the game to bring the dim lit raster back to 1, so the preview drops the
    scale.  A x4 also appears on later stages that only see the raster
    through ``prev`` / C0-C2 (e.g. a final brightening) and is dropped there
    too, as before.  A x2 on such a downstream stage is left alone: it is not
    raster compensation, and dropping it would darken colour grades and toon
    ramps.  Materials with an unlit colour channel keep every
    scale: there is no dim lit raster to compensate.
    """
    if data is not None and not _raster_is_lit(data):
        return set()
    tainted = _raster_tainted_stages(stages)
    cancel = set()
    for index, stage in stages:
        scale = int(stage.get("colorScale", 0) or 0)
        if not scale or index not in tainted:
            continue
        if scale == 3:                      # /2 never washes the preview out:
            continue                        # it darkens, and the water uses it
                                            # as the compare threshold
        direct = any(int(arg) in (10, 11)
                     for arg in (stage.get("colorIn") or []))
        if direct or scale == 2:            # any scale directly on RASC, else x4
            cancel.add(index)
    return cancel


def _raster_ambient(data):
    """Raw ambient level of colour channel 0 (0..1)."""
    try:
        return max(0.0, min(1.0, float(((data.get("ambColor") or [[0]])[0][0])) / 255.0))
    except (IndexError, TypeError, ValueError):
        return 0.0


def _stage_adds_bare_raster(stage):
    """True when the stage adds the raster itself (``A`` / ``D`` is RASC)."""
    args = [int(arg) for arg in (stage.get("colorIn") or [])]
    if len(args) < 4:
        return False
    return args[0] == 10 or args[3] == 10


def _stage_is_pure_raster(stage):
    """True when the whole stage output is the raster (nothing else added)."""
    args = [int(arg) for arg in (stage.get("colorIn") or [])]
    if len(args) < 4:
        return False
    return sum(1 for arg in args if arg != 15) == 1 and 10 in args


def _raster_model(stages, data=None):
    """How the preview should feed the raster colour of this material.

    Returns ``(cancel_stages, raster_scale)``.

    The preview feeds a full-brightness raster.  A stage that multiplies the
    raster with a x2/x4 colour scale is drawn with that scale removed, which
    restores the game's result there, so nothing else is needed.

    Some materials also *add* a bare raster operand (``a * (1 - c)`` or the ``d``
    operand) that has no such compensation: it would add a whole white raster
    instead of the dim lit one and the result clips to white, hiding the
    texture.  For those the consistent model is to keep every stored scale and
    feed the raster at the brightness the compensated scale implies, which is
    exact for both the scaled and the added terms.  When there is no scale at
    all the raster is added at the channel's ambient level instead: the game
    lights it, the preview cannot, and a full white raster washes the whole
    material out.
    """
    cancel = _raster_cancel_stages(stages, data)
    if not cancel:
        if data is not None and _raster_is_lit(data):
            added = [stage for _index, stage in stages
                     if _stage_adds_bare_raster(stage)]
            if added and not all(_stage_is_pure_raster(stage)
                                 for stage in added):
                ambient = _raster_ambient(data)
                if 0.0 < ambient < 1.0:
                    return set(), ambient
        return cancel, 1.0
    factor = 1
    for index, stage in stages:
        if index in cancel:
            scale = int(stage.get("colorScale", 0) or 0)
            factor = max(factor, {1: 2, 2: 4}.get(scale, 1))
    if factor <= 1:
        return cancel, 1.0
    additive = set()
    for index, stage in stages:
        if index in cancel:
            continue
        args = [int(arg) for arg in (stage.get("colorIn") or [])]
        if len(args) >= 4 and (args[0] == 10 or args[3] == 10):
            additive.add(index)
    if not additive:
        return cancel, 1.0
    return set(), 1.0 / factor


def _new_tev_group(material, index):
    group = bpy.data.node_groups.new(
        f"BMD TEV {material.name} S{index + 1}", "ShaderNodeTree")
    group["bmd_tev_owner"] = material.name
    interface = getattr(group, "interface", None)
    if interface is not None:
        for label in ("Color A", "Color B", "Color C", "Color D"):
            interface.new_socket(label, in_out="INPUT",
                                 socket_type="NodeSocketColor")
        for label in ("Alpha A", "Alpha B", "Alpha C", "Alpha D"):
            interface.new_socket(label, in_out="INPUT",
                                 socket_type="NodeSocketFloat")
        interface.new_socket("Color", in_out="OUTPUT",
                             socket_type="NodeSocketColor")
        interface.new_socket("Alpha", in_out="OUTPUT",
                             socket_type="NodeSocketFloat")
    else:  # Blender < 4.0
        for label in ("Color A", "Color B", "Color C", "Color D"):
            group.inputs.new("NodeSocketColor", label)
        for label in ("Alpha A", "Alpha B", "Alpha C", "Alpha D"):
            group.inputs.new("NodeSocketFloat", label)
        group.outputs.new("NodeSocketColor", "Color")
        group.outputs.new("NodeSocketFloat", "Alpha")
    return group


def _const_color_arg(argument, stage_index, data):
    try:
        argument = int(argument)
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0, 1.0
    if argument == 15:
        return 0.0, 0.0, 0.0, 1.0
    if argument == 12:
        return 1.0, 1.0, 1.0, 1.0
    if argument == 13:
        return 0.5, 0.5, 0.5, 1.0
    if argument == 14:
        selector = _selection(data.get("kColorSel"), stage_index)
        if 0 <= selector <= 7:
            return _konst_value(selector)
        if 8 <= selector <= 11:
            return 0.0, 0.0, 0.0, 1.0
    return None


def _const_alpha_arg(argument, stage_index, data):
    try:
        argument = int(argument)
    except (TypeError, ValueError):
        return 0.0
    if argument == 7:
        return 0.0
    if argument == 6:
        selector = _selection(data.get("kAlphaSel"), stage_index)
        if 0 <= selector <= 7:
            return _konst_alpha_value(selector)
        if 8 <= selector <= 15:
            return 0.0
    return None


def _red_socket(tree, value, location):
    if _is_const(value):
        return _as_rgb(value)[0]
    node = tree.nodes.new("ShaderNodeSeparateColor")
    node.location = location
    tree.links.new(value, node.inputs["Color"])
    return node.outputs["Red"]


def _compare_condition(tree, first, second, equal, location):
    """R8 / A8 comparison used by the TEV compare bias."""
    a = _red_socket(tree, first, location)
    b = _red_socket(tree, second, (location[0], location[1] - 140.0))
    if equal:
        node = _new_math(tree, "COMPARE", a, b, location)
        node.inputs[2].default_value = 0.001
        return node.outputs[0]
    return _new_math(tree, "GREATER_THAN", a, b, location).outputs[0]


def _tev_color_compare(tree, values, stage):
    a, b, c, d = values
    # Compare ops are 8-15: even values are *_GT, odd values are *_EQ.
    equal = bool(int(stage.get("colorOp", 0) or 0) & 1)
    condition = _compare_condition(tree, a, b, equal, (-500.0, 120.0))

    mix = tree.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.blend_type = "MIX"
    mix.location = (-150.0, 0.0)
    tree.links.new(condition, mix.inputs[0])
    mix.inputs[6].default_value = (0.0, 0.0, 0.0, 1.0)
    if _is_const(c):
        mix.inputs[7].default_value = _as_rgb(c)
    else:
        tree.links.new(c, mix.inputs[7])

    result = _vadd(tree, d, mix.outputs[2], (-20.0, 0.0))
    if int(stage.get("colorClamp", 0) or 0):
        d_b = _bounds(d)
        result = _vclamp(tree, result, (d_b[0], d_b[1] + 1.0),
                         (120.0, 0.0))
    return result


def _tev_alpha_compare(tree, values, stage):
    a, b, c, d = values
    equal = bool(int(stage.get("alphaOp", 0) or 0) & 1)
    condition = _compare_condition(tree, a, b, equal, (-500.0, -320.0))

    mix = tree.nodes.new("ShaderNodeMix")
    mix.data_type = "FLOAT"
    mix.blend_type = "MIX"
    mix.location = (-150.0, -360.0)
    tree.links.new(condition, mix.inputs[0])
    mix.inputs[2].default_value = 0.0
    if _is_const(c):
        mix.inputs[3].default_value = float(c)
    else:
        tree.links.new(c, mix.inputs[3])

    result = _fadd(tree, d, mix.outputs[0], (-20.0, -360.0))
    if int(stage.get("alphaClamp", 0) or 0):
        d_b = _bounds(d)
        result = _fclamp(tree, result, (d_b[0], d_b[1] + 1.0),
                         (120.0, -360.0))
    return result


def _tev_color_expression(tree, values, stage, data, stage_index,
                          cancel_scale=False):
    a, b, c, d = values
    if int(stage.get("colorBias", 0) or 0) == 3:
        return _tev_color_compare(tree, values, stage)
    bias = _BIAS_VALUES.get(int(stage.get("colorBias", 0) or 0), 0.0)
    scale = _SCALE_VALUES.get(int(stage.get("colorScale", 0) or 0), 1.0)
    # A scale above 1 on a raster-dependent stage compensates for the raster
    # being darkened by the game's ambient and lights.  The Blender preview
    # has no scene lighting and feeds a full-brightness raster, so the scale
    # would wash the textures out (or clip them before a toon ramp).  Keep
    # the stored value for export, but omit it here.
    if cancel_scale:
        scale = 1.0
    clamp = bool(int(stage.get("colorClamp", 0) or 0))
    a_b, b_b, c_b, d_b = _bounds(a), _bounds(b), _bounds(c), _bounds(d)

    ac, ac_b = None, (0.0, 0.0)
    if not _is_zero(a):
        inv_c = _vsub(tree, (1.0, 1.0, 1.0, 1.0), c, (-600.0, 160.0))
        ac = _vmul(tree, a, inv_c, (-450.0, 160.0))
        ac_b = _mul_bounds(a_b, (1.0 - c_b[1], 1.0 - c_b[0]))
        if _is_zero(ac):
            ac = None
    bc, bc_b = None, (0.0, 0.0)
    if not _is_zero(b) and not _is_zero(c):
        bc = _vmul(tree, b, c, (-450.0, 0.0))
        bc_b = _mul_bounds(b_b, c_b)
        if _is_zero(bc):
            bc = None

    if ac is None and bc is None:
        lerp, lerp_b = (0.0, 0.0, 0.0, 1.0), (0.0, 0.0)
    elif ac is None:
        lerp, lerp_b = bc, bc_b
    elif bc is None:
        lerp, lerp_b = ac, ac_b
    else:
        lerp = _vadd(tree, ac, bc, (-300.0, 120.0))
        lerp_b = (ac_b[0] + bc_b[0], ac_b[1] + bc_b[1])

    if bias:
        dbias = _vadd(tree, d, (bias, bias, bias, 1.0), (-300.0, -120.0))
        dbias_b = (d_b[0] + bias, d_b[1] + bias)
    else:
        dbias, dbias_b = d, d_b

    if int(stage.get("colorOp", 0) or 0) == 1:
        result = _vsub(tree, dbias, lerp, (-150.0, 0.0))
        result_b = (dbias_b[0] - lerp_b[1], dbias_b[1] - lerp_b[0])
    else:
        result = _vadd(tree, dbias, lerp, (-150.0, 0.0))
        result_b = (dbias_b[0] + lerp_b[0], dbias_b[1] + lerp_b[1])

    if scale != 1.0:
        result = _vscale(tree, result, scale, (-50.0, 0.0))
        result_b = (result_b[0] * scale, result_b[1] * scale)
    if clamp:
        result = _vclamp(tree, result, result_b, (50.0, 0.0))
    return result


def _tev_alpha_expression(tree, values, stage, data, stage_index):
    a, b, c, d = values
    if int(stage.get("alphaBias", 0) or 0) == 3:
        return _tev_alpha_compare(tree, values, stage)
    bias = _BIAS_VALUES.get(int(stage.get("alphaBias", 0) or 0), 0.0)
    scale = _SCALE_VALUES.get(int(stage.get("alphaScale", 0) or 0), 1.0)
    clamp = bool(int(stage.get("alphaClamp", 0) or 0))
    a_b, b_b, c_b, d_b = _bounds(a), _bounds(b), _bounds(c), _bounds(d)

    ac, ac_b = None, (0.0, 0.0)
    if not _is_zero(a):
        inv_c = _fsub(tree, 1.0, c, (-600.0, -300.0))
        ac = _fmul(tree, a, inv_c, (-450.0, -300.0))
        ac_b = _mul_bounds(a_b, (1.0 - c_b[1], 1.0 - c_b[0]))
        if _is_zero(ac):
            ac = None
    bc, bc_b = None, (0.0, 0.0)
    if not _is_zero(b) and not _is_zero(c):
        bc = _fmul(tree, b, c, (-450.0, -440.0))
        bc_b = _mul_bounds(b_b, c_b)
        if _is_zero(bc):
            bc = None

    if ac is None and bc is None:
        lerp, lerp_b = 0.0, (0.0, 0.0)
    elif ac is None:
        lerp, lerp_b = bc, bc_b
    elif bc is None:
        lerp, lerp_b = ac, ac_b
    else:
        lerp = _fadd(tree, ac, bc, (-300.0, -360.0))
        lerp_b = (ac_b[0] + bc_b[0], ac_b[1] + bc_b[1])

    if bias:
        dbias = _fadd(tree, d, bias, (-300.0, -520.0))
        dbias_b = (d_b[0] + bias, d_b[1] + bias)
    else:
        dbias, dbias_b = d, d_b

    if int(stage.get("alphaOp", 0) or 0) == 1:
        result = _fsub(tree, dbias, lerp, (-150.0, -420.0))
        result_b = (dbias_b[0] - lerp_b[1], dbias_b[1] - lerp_b[0])
    else:
        result = _fadd(tree, dbias, lerp, (-150.0, -420.0))
        result_b = (dbias_b[0] + lerp_b[0], dbias_b[1] + lerp_b[1])

    if scale != 1.0:
        result = _fscale(tree, result, scale, (-50.0, -420.0))
        result_b = (result_b[0] * scale, result_b[1] * scale)
    if clamp:
        result = _fclamp(tree, result, result_b, (100.0, -420.0))
    return result


def _build_tev_group(material, index, stage, data, cancel_scale=False):
    """One node group implementing ``dest = (d + bias + lerp(a, b, c)) * scale``."""
    group = _new_tev_group(material, index)
    group_in = group.nodes.new("NodeGroupInput")
    group_in.location = (-800.0, 0.0)
    group_out = group.nodes.new("NodeGroupOutput")
    group_out.location = (800.0, 0.0)

    color_args = list(stage.get("colorIn") or [15, 15, 15, 15])[:4]
    alpha_args = list(stage.get("alphaIn") or [7, 7, 7, 7])[:4]
    color_values = []
    for label, argument in zip("ABCD", color_args):
        value = _const_color_arg(argument, index, data)
        color_values.append(
            group_in.outputs[f"Color {label}"] if value is None else value)
    alpha_values = []
    for label, argument in zip("ABCD", alpha_args):
        value = _const_alpha_arg(argument, index, data)
        alpha_values.append(
            group_in.outputs[f"Alpha {label}"] if value is None else value)

    color = _tev_color_expression(group, color_values, stage, data, index,
                                  cancel_scale)
    group.links.new(_color_socket(group, color, (150.0, 0.0)),
                    group_out.inputs["Color"])
    alpha = _tev_alpha_expression(group, alpha_values, stage, data, index)
    group.links.new(_alpha_socket(group, alpha, (150.0, -300.0)),
                    group_out.inputs["Alpha"])
    _layout_tree(group)
    return group


def _add_stage_node(tree, material, index, stage, anchor, data,
                    cancel_scale=False):
    group = _build_tev_group(material, index, stage, data, cancel_scale)
    node = tree.nodes.new("ShaderNodeGroup")
    node.node_tree = group
    node.name = f"bmd_tev_stage{index}"
    node.label = f"TEV stage {index + 1}"
    node[ANIM_PROP] = ANIM_TEV_STAGE
    node["bmd_tev_index"] = int(index)
    node.location = (anchor.location.x - 740.0 + 180.0 * (index % 4),
                     anchor.location.y - 280.0 * (index // 4))
    return node


def _tex_matrix(data, index):
    try:
        index = int(index)
    except (TypeError, ValueError):
        return None
    if index in (0xFF, 0xFFFF):
        return None
    if 0 <= index < 10:
        table, slot = "texMtx", index
    elif 30 <= index < 60:
        table, slot = "texMtx", (index - 30) // 3
    elif index == 60 or index == 125:      # GX_IDENTITY / GX_DTTIDENTITY
        return _IDENTITY_TEXMTX
    elif 64 <= index < 122:                # GX_DTTMTX0..19
        table, slot = "postTexMtx", (index - 64) // 3
    else:
        return None
    entries = data.get(table) or []
    # An absent table means the game uses the identity matrix for that slot.
    return entries[slot] if 0 <= slot < len(entries) and entries[slot] \
        else _IDENTITY_TEXMTX


def _texgen_effect(matrix):
    """(TexEffect, Maya calc type) from a MAT3 texMtx entry."""
    attributes = int(matrix.get("type", 0) or 0)
    return attributes & 0x7F, bool(attributes & 0x80)


def _texgen_srt(matrix):
    """SRT part of a texMtx, in the same layout as J3DUltra."""
    origin = matrix.get("center") or [0.5, 0.5, 0.5]
    scale = matrix.get("scale") or [1.0, 1.0]
    translation = matrix.get("translation") or [0.0, 0.0]
    rotation = float(matrix.get("rotation") or 0) * math.pi / 32768.0
    maya = bool(int(matrix.get("type", 0) or 0) & 0x80)
    m = Matrix.Identity(4)

    c, s = math.cos(rotation), math.sin(rotation)
    if not maya:
        m[0][0] = scale[0] * c
        m[0][1] = scale[0] * -s
        m[0][3] = (-c * origin[0] + s * origin[1] + origin[0]
                   + translation[0])
        m[1][0] = scale[1] * s
        m[1][1] = scale[1] * c
        m[1][3] = (s * origin[0] - c * origin[1] + origin[1]
                   + translation[1])
    else:
        m[0][0] = scale[0] * c
        m[0][1] = scale[0] * -s
        m[2][0] = ((translation[0] - 0.5) * c
                   - (translation[1] - 0.5 + scale[1]) * s + 0.5)
        m[1][0] = scale[1] * s
        m[1][1] = scale[1] * c
        m[2][1] = (-(translation[0] - 0.5) * s
                   - (translation[1] - 0.5 + scale[1]) * c + 0.5)

    m[2][2] = 1.0
    m[3][3] = 1.0
    return m


def _env_srt_translation(srt):
    """SRT with its V translation in GX texture space (V grows downwards).

    The env NDC to UV bias already turns the normal's up-positive V into the
    texture's down-positive V, so it mirrors the whole SRT placed before it.
    The SRT translation is authored in texture space, though, and must not be
    mirrored: without this the window moves to the opposite side of the map
    and samples the bright part of the highlight texture, washing the surface
    out.
    """
    out = srt.copy()
    out[1][3] = -out[1][3]
    return out


def _texgen_matrix(matrix):
    """Final 4x4 texture matrix for a MAT3 texMtx entry.

    Follows the J3DUltra calculation (TexEffect + SRT + projection matrix).
    Projection effects apply the NDC to UV bias after the SRT, so the 0.5
    offset is not multiplied by the texMtx scale.
    """
    if not matrix:
        return None
    raw = [float(value) for value in (matrix.get("matrix") or [])]
    if len(raw) < 16:
        return None
    projection = Matrix((raw[0:4], raw[4:8], raw[8:12], raw[12:16]))
    srt = _texgen_srt(matrix)
    effect, _maya = _texgen_effect(matrix)
    env = Matrix(((0.5, 0.0, 0.5, 0.0),
                  (0.0, -0.5, 0.5, 0.0),
                  (0.0, 0.0, 1.0, 0.0),
                  (0.0, 0.0, 0.0, 1.0)))
    env_old = Matrix(((0.5, 0.0, 0.0, 0.5),
                      (0.0, -0.5, 0.0, 0.5),
                      (0.0, 0.0, 1.0, 0.0),
                      (0.0, 0.0, 0.0, 1.0)))
    tex = srt.copy()
    tex[0][2] = tex[0][3]
    tex[1][2] = tex[1][3]
    tex[2][2] = 1.0
    tex[0][3] = 0.0
    tex[1][3] = 0.0
    tex[2][3] = 0.0

    if effect == 0:                         # NONE
        return srt
    if effect in (2, 3, 4, 5):              # *_BASIC / EFFECT_4/5
        return srt @ projection
    # The NDC to UV conversion (the 0.5 bias) has to be applied after the SRT
    # scale, exactly like the projection effects below: scaling it as well
    # would park an env map highlight in the texture corner instead of the
    # centre and would move it whenever a BTK animates the scale.
    if effect == 6:                         # ENVMAP_OLD
        return env_old @ _env_srt_translation(srt)
    if effect == 7:                         # ENVMAP
        return env @ tex
    if effect == 9:                         # VIEWPROJMAP (no view here)
        return tex @ projection
    if effect == 10:                        # ENVMAP_OLD_EFFECTMTX
        return env_old @ _env_srt_translation(srt) @ projection
    if effect in (8, 11):                   # PROJMAP / ENVMAP_EFFECTMTX
        # The projection is converted to texture space *after* the SRT, so
        # its 0.5 bias is not multiplied by the texMtx scale.  Scaling the
        # bias as well would place the projection corner at UV 0 instead of
        # its centre and would move the texture when a BTK animates scale.
        return env @ tex @ projection
    return projection


def _texgen_info(data, texcoord_index):
    coords = data.get("texCoord") or []
    try:
        entry = coords[int(texcoord_index)]
    except (IndexError, TypeError, ValueError):
        entry = None
    if entry:
        gen_type = int(entry[0])
        source = int(entry[1])
        uv_index = source - 4 if 4 <= source <= 11 else 0
        matrix_index = int(entry[2]) if len(entry) > 2 else None
    else:
        gen_type, source, uv_index, matrix_index = None, None, 0, None
    return {"gen_type": gen_type, "source": source, "uv_index": uv_index,
            "matrix_index": matrix_index,
            "matrix": _tex_matrix(data, matrix_index)}


_TEXGEN_KINDS = {0: "Pos", 1: "Normal", 2: "Binormal", 3: "Tangent"}
# TexEffects whose input matrix is rot3x3(view * model): the generated
# texcoord follows the camera rotation (GX drops the view translation, so the
# camera position does not matter).
_TEXGEN_VIEW_EFFECTS = (1, 6, 7)        # ENVMAP_BASIC / ENVMAP_OLD / ENVMAP


def _new_pos_texgen_group(material, slot, info, kind="Pos"):
    """Node group evaluating a vertex-vector texMtx 3x4/2x4 matrix.

    The whole chain lives inside the group: the Geometry node, the optional
    camera transform of the view-space effects, the axis conversion and the
    matrix rows.  Only the unit scale is an input.
    """
    matrix = info.get("matrix")
    gen_type = info.get("gen_type")
    if matrix is None or gen_type not in (0, 1):
        return None
    final = _texgen_matrix(matrix)
    if final is None:
        return None
    effect, _maya = _texgen_effect(matrix)
    name = (f"BMD {kind} TexGen {material.name} S{slot} "
            f"M{info.get('matrix_index')} T{gen_type} E{effect}")
    group = bpy.data.node_groups.get(name)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    group["bmd_pos_texgen_owner"] = material.name
    interface = getattr(group, "interface", None)
    if interface is not None:
        interface.new_socket("Unit Scale", in_out="INPUT",
                             socket_type="NodeSocketFloat")
        interface.new_socket("UV", in_out="OUTPUT",
                             socket_type="NodeSocketVector")
    else:  # Blender < 4.0
        group.inputs.new("NodeSocketFloat", "Unit Scale")
        group.outputs.new("NodeSocketVector", "UV")

    node_in = group.nodes.new("NodeGroupInput")
    node_in.location = (-1100.0, 0.0)
    node_out = group.nodes.new("NodeGroupOutput")
    node_out.location = (900.0, 0.0)

    # The imported mesh is stored in Blender axes: the group converts the
    # vector back to J3D axes and scales positions to raw BMD units.
    geometry = group.nodes.new("ShaderNodeNewGeometry")
    geometry.name = "Geometry"
    geometry.location = (-1000.0, 0.0)
    source = int(info.get("source", 0) or 0)
    if source == 2:                          # binormal = normal x tangent
        cross = group.nodes.new("ShaderNodeVectorMath")
        cross.operation = "CROSS_PRODUCT"
        cross.location = (-860.0, 0.0)
        group.links.new(geometry.outputs["Normal"], cross.inputs[0])
        group.links.new(geometry.outputs["Tangent"], cross.inputs[1])
        vector = cross.outputs["Vector"]
    else:
        output = {0: "Position", 1: "Normal", 3: "Tangent"}.get(source)
        if output is None:
            return group
        vector = geometry.outputs[output]

    view = effect in _TEXGEN_VIEW_EFFECTS
    if view:
        # These effects follow the camera rotation.  GX view space is
        # (right, up, backward); Blender's camera space is (right, up, -forward).
        transform = group.nodes.new("ShaderNodeVectorTransform")
        transform.vector_type = "VECTOR"
        transform.convert_from = "WORLD"
        transform.convert_to = "CAMERA"
        transform.location = (-700.0, 0.0)
        group.links.new(vector, transform.inputs["Vector"])
        vector = transform.outputs["Vector"]

    separate = group.nodes.new("ShaderNodeSeparateXYZ")
    separate.location = (-520.0, 0.0)
    group.links.new(vector, separate.inputs["Vector"])
    unit = node_in.outputs["Unit Scale"]

    def scaled(channel, sign=1.0, y=0.0):
        node = _new_math(group, "MULTIPLY", separate.outputs[channel], unit,
                         (-340.0, y))
        if sign != 1.0:
            node = _new_math(group, "MULTIPLY", node.outputs[0], sign,
                             (-200.0, y))
        return node.outputs[0]

    # Axis conversion, fused with the view swizzle: object vectors become
    # (x, z, -y), camera vectors (x, y, -z).
    combine_position = group.nodes.new("ShaderNodeCombineXYZ")
    combine_position.location = (-40.0, 0.0)
    group.links.new(scaled("X", y=120.0), combine_position.inputs["X"])
    if view:
        group.links.new(scaled("Y"), combine_position.inputs["Y"])
        group.links.new(scaled("Z", -1.0, y=-120.0),
                        combine_position.inputs["Z"])
    else:
        group.links.new(scaled("Z"), combine_position.inputs["Y"])
        group.links.new(scaled("Y", -1.0, y=-120.0),
                        combine_position.inputs["Z"])
    position = combine_position.outputs["Vector"]

    def row(index):
        dot = _new_vmath(
            group, "DOT_PRODUCT", position,
            (final[index][0], final[index][1], final[index][2]),
            (100.0, 100.0 - index * 160.0)
        ).outputs["Value"]
        translation = final[index][3]
        if abs(translation) < 1e-6:
            return dot
        return _new_math(
            group, "ADD", dot, translation,
            (280.0, 100.0 - index * 160.0)
        ).outputs[0]

    s = row(0)
    t = row(1)
    if gen_type == 0:                       # Matrix3x4: divide by Q
        q = row(2)
        u = _new_math(group, "DIVIDE", s, q,
                      (440.0, 100.0)).outputs[0]
        v = _new_math(group, "DIVIDE", t, q,
                      (440.0, -60.0)).outputs[0]
    else:                                   # Matrix2x4
        u, v = s, t
    # GX texcoords put V=0 at the top of the texture, imported UV layers are
    # flipped for Blender's bottom-left origin.  Flip the generated V the same
    # way so the projection matches the texture orientation of the game.
    v = _new_math(group, "SUBTRACT", 1.0, v,
                  (580.0, -60.0)).outputs[0]
    combine = group.nodes.new("ShaderNodeCombineXYZ")
    combine.location = (700.0, 0.0)
    group.links.new(u, combine.inputs["X"])
    group.links.new(v, combine.inputs["Y"])
    combine.inputs["Z"].default_value = 0.0
    group.links.new(combine.outputs["Vector"], node_out.inputs["UV"])
    _layout_tree(group)
    return group


def _vector_texgen(material, tree, node, slot, info, unit_scale):
    """UV from position/normal/binormal/tangent through a texMtx group.

    The Geometry node and the camera transform live inside the group, so the
    tree keeps only the group node next to the texture it feeds.
    """
    source = int(info.get("source", 0) or 0)
    kind = _TEXGEN_KINDS.get(source, "Pos")
    group = _new_pos_texgen_group(material, slot, info, kind)
    if group is None:
        return None
    # A slot can be sampled by several TEV stages; reuse the group node.
    suffix = "pos_texgen" if source == 0 else f"{kind.lower()}_texgen"
    name = f"bmd_{suffix}{slot}"
    existing = tree.nodes.get(name)
    if (existing is not None and existing.type == "GROUP"
            and existing.node_tree == group):
        return existing.outputs["UV"]
    if existing is not None:
        tree.nodes.remove(existing)
    group_node = tree.nodes.new("ShaderNodeGroup")
    group_node.node_tree = group
    group_node.name = name
    group_node.label = f"{kind} texGen {info.get('matrix_index')}"
    group_node[ANIM_PROP] = ANIM_BTK
    group_node["bmd_btk_slot"] = int(slot)
    group_node["bmd_btk_matrix"] = int(info.get("matrix_index") or 0)
    group_node.location = (node.location.x - 780.0, node.location.y - 420.0)
    group_node.inputs["Unit Scale"].default_value = (
        float(unit_scale) if source == 0 else 1.0)
    return group_node.outputs["UV"]


def _linear_to_srgb_group():
    """Shared exact linear → sRGB conversion group for texture samples."""
    name = "BMD Linear to sRGB"
    group = bpy.data.node_groups.get(name)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    interface = getattr(group, "interface", None)
    if interface is not None:
        interface.new_socket("Color", in_out="INPUT",
                             socket_type="NodeSocketColor")
        interface.new_socket("Color", in_out="OUTPUT",
                             socket_type="NodeSocketColor")
    else:  # Blender < 4.0
        group.inputs.new("NodeSocketColor", "Color")
        group.outputs.new("NodeSocketColor", "Color")

    node_in = group.nodes.new("NodeGroupInput")
    node_in.location = (-800.0, 0.0)
    node_out = group.nodes.new("NodeGroupOutput")
    node_out.location = (800.0, 0.0)
    separate = group.nodes.new("ShaderNodeSeparateColor")
    separate.location = (-600.0, 0.0)
    group.links.new(node_in.outputs["Color"], separate.inputs["Color"])

    channels = []
    for index, channel in enumerate(("Red", "Green", "Blue")):
        c = separate.outputs[channel]
        y = 120.0 - index * 220.0
        low = _new_math(group, "MULTIPLY", c, 12.92,
                        (-420.0, y)).outputs[0]
        power = _new_math(group, "POWER", c, 1.0 / 2.4,
                          (-420.0, y - 60.0)).outputs[0]
        high = _new_math(group, "MULTIPLY", power, 1.055,
                         (-260.0, y - 60.0)).outputs[0]
        high = _new_math(group, "SUBTRACT", high, 0.055,
                         (-120.0, y - 60.0)).outputs[0]
        mask = _new_math(group, "GREATER_THAN", c, 0.0031308,
                         (-260.0, y + 60.0)).outputs[0]
        mix = group.nodes.new("ShaderNodeMix")
        mix.data_type = "FLOAT"
        mix.blend_type = "MIX"
        mix.location = (60.0, y)
        group.links.new(mask, mix.inputs[0])
        group.links.new(low, mix.inputs[2])
        group.links.new(high, mix.inputs[3])
        channels.append(mix.outputs[0])

    combine = group.nodes.new("ShaderNodeCombineColor")
    combine.location = (300.0, 0.0)
    for channel, value in zip(("Red", "Green", "Blue"), channels):
        group.links.new(value, combine.inputs[channel])
    group.links.new(combine.outputs["Color"], node_out.inputs["Color"])
    _layout_tree(group)
    return group


def _srgb_to_linear_group():
    """Shared exact sRGB → linear conversion group after TEV."""
    name = "BMD sRGB to Linear"
    group = bpy.data.node_groups.get(name)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(name, "ShaderNodeTree")
    interface = getattr(group, "interface", None)
    if interface is not None:
        interface.new_socket("Color", in_out="INPUT",
                             socket_type="NodeSocketColor")
        interface.new_socket("Color", in_out="OUTPUT",
                             socket_type="NodeSocketColor")
    else:  # Blender < 4.0
        group.inputs.new("NodeSocketColor", "Color")
        group.outputs.new("NodeSocketColor", "Color")

    node_in = group.nodes.new("NodeGroupInput")
    node_in.location = (-800.0, 0.0)
    node_out = group.nodes.new("NodeGroupOutput")
    node_out.location = (800.0, 0.0)
    separate = group.nodes.new("ShaderNodeSeparateColor")
    separate.location = (-600.0, 0.0)
    group.links.new(node_in.outputs["Color"], separate.inputs["Color"])

    channels = []
    for index, channel in enumerate(("Red", "Green", "Blue")):
        c = separate.outputs[channel]
        y = 120.0 - index * 240.0
        low = _new_math(group, "DIVIDE", c, 12.92,
                        (-420.0, y)).outputs[0]
        shifted = _new_math(group, "ADD", c, 0.055,
                            (-420.0, y - 60.0)).outputs[0]
        high = _new_math(group, "DIVIDE", shifted, 1.055,
                         (-260.0, y - 60.0)).outputs[0]
        high = _new_math(group, "POWER", high, 2.4,
                         (-120.0, y - 60.0)).outputs[0]
        mask = _new_math(group, "GREATER_THAN", c, 0.04045,
                         (-260.0, y + 60.0)).outputs[0]
        mix = group.nodes.new("ShaderNodeMix")
        mix.data_type = "FLOAT"
        mix.blend_type = "MIX"
        mix.location = (60.0, y)
        group.links.new(mask, mix.inputs[0])
        group.links.new(low, mix.inputs[2])
        group.links.new(high, mix.inputs[3])
        channels.append(mix.outputs[0])

    combine = group.nodes.new("ShaderNodeCombineColor")
    combine.location = (300.0, 0.0)
    for channel, value in zip(("Red", "Green", "Blue"), channels):
        group.links.new(value, combine.inputs[channel])
    group.links.new(combine.outputs["Color"], node_out.inputs["Color"])
    _layout_tree(group)
    return group


def _srgb_to_linear_node(tree, color, name="bmd_tev_linear"):
    node = tree.nodes.get(name)
    if node is None or node.type != "GROUP":
        if node is not None:
            tree.nodes.remove(node)
        node = tree.nodes.new("ShaderNodeGroup")
        node.node_tree = _srgb_to_linear_group()
        node.name = name
        node.label = "sRGB → Linear"
        # A Render Mode switch can build this node after the import layout ran;
        # keep it in the chain (left of the BSDF, or right of the stage it
        # reads) instead of dropping it at the old default position.
        target = tree.nodes.get("Principled BSDF")
        if target is not None:
            node.location = (target.location.x - _node_extent(node)[0] - H_GAP,
                             target.location.y)
        elif hasattr(color, "node") and color.node is not None:
            source = color.node
            node.location = (source.location.x
                             + _node_extent(source)[0] + H_GAP,
                             source.location.y)
        else:
            node.location = (0.0, 0.0)
    if color is None:
        node.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    elif hasattr(color, "is_linked"):
        _link(tree, node.inputs["Color"], color)
    else:
        node.inputs["Color"].default_value = _as_rgb(color)
    return node.outputs["Color"]


# A TEV texture is laid out as a 500 x 300 slot (see _NODE_WIDTHS) that
# reserves room for "Texture to TEV" inside it: the 240-wide image, one gap,
# the 140-wide conversion and one more gap.
TEXCOL_OFFSET = 300.0
_RECOVERY_UPDATING = False
_RECOVERY_SIGNATURE = {}
_UNDO_BUSY = False
_TRANSFER_PROMPTS = set()
_TRANSFER_PENDING = None
_TRANSFER_TIMER = False
_TRANSFER_QUEUE = []
_ASSIGNMENT_CACHE = {}
_ASSIGNMENT_CACHE_READY = False


def _texcol_name(slot):
    return f"bmd_texcol{int(slot)}"


def _ensure_texture_recovery(tree, texture_node, slot):
    """Create/return the "Texture to TEV" group of one texture node."""
    name = _texcol_name(slot)
    group_node = tree.nodes.get(name)
    frame = texture_node.parent
    if group_node is None:
        group_node = tree.nodes.new("ShaderNodeGroup")
        group_node.node_tree = _linear_to_srgb_group()
        group_node.name = name
        group_node.label = "Linear → sRGB"
        if frame is not None:
            group_node.parent = frame
        group_node.location = (texture_node.location.x + TEXCOL_OFFSET,
                               texture_node.location.y)
    if not group_node.inputs["Color"].is_linked:
        tree.links.new(texture_node.outputs["Color"],
                       group_node.inputs["Color"])
    return group_node


def _retarget_texture_links(tree, texture_node, group_node):
    """Feed whatever consumed the raw texture colour from the group instead."""
    for link in list(texture_node.outputs["Color"].links):
        if link.to_node == group_node:
            continue
        socket = link.to_socket
        tree.links.remove(link)
        tree.links.new(group_node.outputs["Color"], socket)


def _drop_texture_recovery(tree, group_node, texture_node):
    """Remove the group and hand its consumers the raw texture colour back."""
    for link in list(group_node.outputs["Color"].links):
        socket = link.to_socket
        tree.links.remove(link)
        if texture_node is not None:
            tree.links.new(texture_node.outputs["Color"], socket)
    for link in list(group_node.inputs["Color"].links):
        tree.links.remove(link)
    tree.nodes.remove(group_node)


def _texture_color(tree, texture_node, slot):
    """Raw TEV colour for a texture node (undo Blender's sRGB sampling).

    Data textures (the I4/I8 intensity masks, imported as Non-Color) are
    already sampled raw, so they go straight into the TEV.  An empty slot has
    no format yet: the conversion is added when a colour texture arrives.
    """
    image = getattr(texture_node, "image", None)
    if image is None:
        return texture_node.outputs["Color"]
    try:
        if image.colorspace_settings.name != "sRGB":
            return texture_node.outputs["Color"]
    except Exception:
        pass
    return _ensure_texture_recovery(tree, texture_node, slot).outputs["Color"]


def _image_key_map():
    """{TEX1 key: image} including every key that shares an image's pixels."""
    mapping = {}
    for image in bpy.data.images:
        key = bc.texture_key(image)
        if key:
            mapping.setdefault(key, image)
        raw = image.get("bmd_keys")
        if not raw:
            continue
        try:
            keys = json.loads(raw)
        except Exception:
            continue
        for name in keys:
            name = str(name)
            if name:
                mapping.setdefault(name, image)
    return mapping


def _native_slot_image(keys, key_images, slot):
    """Image assigned to the material's own texture slot (never a preview swap)."""
    key = keys[slot] if 0 <= slot < len(keys) else None
    return key_images.get(key) if key else None


def _image_needs_recovery(image):
    if image is None:
        return False
    try:
        return image.colorspace_settings.name == "sRGB"
    except Exception:
        return False


def refresh_texture_recovery(material, slot=None):
    """Keep "Texture to TEV" in step with the material's own texture slots.

    The conversion follows the texture assigned to the material (its MAT3
    ``texNames``), not whatever image a ``bmd_tex<N>`` node currently shows: a
    BTP preview frame or a manual node swap must not create or drop the node.
    Colour (sRGB) images need the conversion, the I4/I8/IA4/IA8 data formats do
    not, and an empty slot needs nothing at all.  Consumers of the raw texture
    colour are moved onto the group (or back) so the TEV always sees the value
    the game would sample.
    """
    global _RECOVERY_UPDATING
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return
    if _is_simple_preview(material):
        # Simple unlit previews sample the sRGB image directly; the TEV
        # Linear -> sRGB helper belongs only to shader previews.
        for index in range(8):
            group = tree.nodes.get(_texcol_name(index))
            if group is not None:
                _drop_texture_recovery(tree, group,
                                       tree.nodes.get(f"bmd_tex{index}"))
        return
    keys = bc.material_texture_keys(material)
    key_images = _image_key_map() if keys else {}
    _RECOVERY_UPDATING = True
    try:
        for index in (range(8) if slot is None else [int(slot)]):
            texture = tree.nodes.get(f"bmd_tex{index}")
            group = tree.nodes.get(_texcol_name(index))
            if texture is None:
                if group is not None:
                    _drop_texture_recovery(tree, group, None)
                continue
            image = _native_slot_image(keys, key_images, index)
            consumers = [link for link in texture.outputs["Color"].links
                         if link.to_node is not group]
            if _image_needs_recovery(image):
                if group is None and not consumers:
                    continue        # nothing samples the colour yet
                group = _ensure_texture_recovery(tree, texture, index)
                _retarget_texture_links(tree, texture, group)
            elif group is not None:
                _drop_texture_recovery(tree, group, texture)
    finally:
        _RECOVERY_UPDATING = False


def _recovery_signature(material, key_images=None):
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return None
    keys = bc.material_texture_keys(material)
    if key_images is None:
        key_images = _image_key_map() if keys else {}
    entries = []
    for index in range(8):
        node = tree.nodes.get(f"bmd_tex{index}")
        if node is None:
            entries.append(None)
            continue
        image = _native_slot_image(keys, key_images, index)
        colorspace = ""
        if image is not None:
            try:
                colorspace = image.colorspace_settings.name or ""
            except Exception:
                colorspace = ""
        entries.append((
            keys[index] if index < len(keys) and keys[index] else "",
            colorspace,
            tree.nodes.get(_texcol_name(index)) is not None))
    return tuple(entries)


_GROUP_OWNER_PROPS = ("bmd_tev_owner", "bmd_wrap_owner",
                      "bmd_pos_texgen_owner")


def _group_owner(group):
    """Material name a generated node group was built for, if any."""
    if group is None:
        return None
    for prop in _GROUP_OWNER_PROPS:
        owner = group.get(prop)
        if owner:
            return str(owner)
    return None


def _set_group_owner(group, name):
    for prop in _GROUP_OWNER_PROPS:
        if prop in group:
            group[prop] = name


def _group_copy_name(group, material):
    owner = _group_owner(group) or ""
    name = group.name
    if owner and owner in name:
        name = name.replace(owner, material.name, 1)
    if name == group.name:
        name = f"{name} [{material.name}]"
    return name


def _ensure_material_group(group, material, cache):
    """A copy of *group* that belongs to *material* alone (cached)."""
    if group in cache:
        return cache[group]
    owner = _group_owner(group)
    if owner is None:
        # Stateless conversion helpers are identical for every material.
        cache[group] = group
        return group
    if owner == material.name:
        cache[group] = group
        return group
    copy = group.copy()
    copy.name = _group_copy_name(group, material)
    _set_group_owner(copy, material.name)
    cache[group] = copy
    for node in copy.nodes:
        if node.type == "GROUP" and node.node_tree is not None:
            node.node_tree = _ensure_material_group(
                node.node_tree, material, cache)
    return copy


def isolate_material_groups(material):
    """Give a copied material its own generated TEV/wrap/texgen groups.

    Blender copies the material node tree but keeps its group datablocks
    shared.  A copy would therefore share the TEV stages and other generated
    groups with the original, and a shader import on either material could
    change both.  Reassign every group owned by another material to a copy
    owned by this one.  Stateless helpers (sRGB <-> Linear) stay shared.
    """
    tree = getattr(material, "node_tree", None) if material is not None else None
    if tree is None:
        return False
    cache = {}
    changed = False
    for node in tree.nodes:
        if node.type != "GROUP" or node.node_tree is None:
            continue
        owner = _group_owner(node.node_tree)
        if owner is None or owner == material.name:
            continue
        node.node_tree = _ensure_material_group(
            node.node_tree, material, cache)
        changed = True
    return changed


def _track_material_rename(material):
    """Return ``(first_seen, previous_name)`` for the datablock name."""
    current = str(material.name)
    last = material.get(bc.MATERIAL_LAST_NAME_PROP)
    if not last:
        material[bc.MATERIAL_LAST_NAME_PROP] = current
        return True, None
    if str(last) == current:
        return False, None
    material[bc.MATERIAL_LAST_NAME_PROP] = current
    material["bmd_name"] = current
    return False, str(last)


def _owned_groups_shared(material):
    """True when a generated group is also referenced by another material."""
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return False
    counts = {}
    for node in tree.nodes:
        if node.type != "GROUP" or node.node_tree is None:
            continue
        group = node.node_tree
        if _group_owner(group) is None:
            continue
        counts[group] = counts.get(group, 0) + 1
    return any(group.users > count for group, count in counts.items())


def _retag_material_groups(material, old_name):
    """A renamed material keeps its unique groups; only move the owner tag."""
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return

    def retag(group):
        if _group_owner(group) == old_name:
            _set_group_owner(group, material.name)
        for node in group.nodes:
            if node.type == "GROUP" and node.node_tree is not None:
                retag(node.node_tree)

    for node in tree.nodes:
        if node.type == "GROUP" and node.node_tree is not None:
            retag(node.node_tree)


def ensure_material_isolation():
    """Keep material copies independent from each other.

    The datablock name recorded by the importer is copied with the material, so
    a name change means a copy or a manual rename.  A copy still references the
    original's shared generated groups; those are rebuilt as unique copies.  A
    rename of an already isolated material only moves its owner tags.
    """
    materials = [material for material in bpy.data.materials
                 if material.library is None
                 and (material.get("bmd_material")
                      or material.get("bmd_shader_active"))]
    for material in materials:
        first_seen, old_name = _track_material_rename(material)
        if first_seen:
            if _owned_groups_shared(material):
                # A copy from a file saved before names were tracked.
                material["bmd_name"] = str(material.name)
                isolate_material_groups(material)
            continue
        if old_name is None:
            continue
        if _owned_groups_shared(material):
            isolate_material_groups(material)
        else:
            _retag_material_groups(material, old_name)


def _mesh_bmd_owner(obj):
    arm = bc.target_armature(obj)
    if arm is not None:
        return arm
    if "bmd_textures" in obj or len(obj.bmd_texture_list):
        return obj
    return None


def _bmd_owner_objects():
    owners = []
    seen = set()
    for obj in bpy.data.objects:
        if obj.type == "MESH":
            owner = _mesh_bmd_owner(obj)
        elif obj.type == "ARMATURE" and (
                "bmd_textures" in obj or len(obj.bmd_texture_list)):
            owner = obj
        else:
            continue
        if owner is not None and owner not in seen:
            seen.add(owner)
            owners.append(owner)
    return owners


def texture_transfer_candidates(owner, material):
    """Missing textures of *material* that another model still has."""
    if owner is None or material is None:
        return []
    native = bc.material_texture_keys(material)
    if not any(native):
        return []
    try:
        target_slots = owner.bmd_texture_list
    except Exception:
        target_slots = ()

    filled = set()
    for slot in target_slots:
        if slot.image is not None:
            filled.update(bc._texture_slot_match_names(slot))

    source = {}
    for other in _bmd_owner_objects():
        if other is owner:
            continue
        try:
            slots = other.bmd_texture_list
        except Exception:
            continue
        for index, slot in enumerate(slots):
            image = slot.image
            if image is None:
                continue
            info = {
                "source_owner": other.name,
                "source_index": index,
                "image": image.name,
                "key": bc.texture_slot_key(slot) or bc.texture_key(image),
                "name": slot.name or bc.clean_texture_name(image),
                "sampler": bc.slot_sampler(slot),
            }
            for name in bc._texture_slot_match_names(slot):
                source.setdefault(name, info)

    tree = getattr(material, "node_tree", None)
    if tree is not None:
        for node in tree.nodes:
            image = getattr(node, "image", None)
            if node.type != "TEX_IMAGE" or image is None:
                continue
            names = {
                bc.clean_texture_name(image),
                bc.strip_blender_suffix(bc.texture_key(image)),
            }
            info = {
                "source_owner": "",
                "source_index": -1,
                "image": image.name,
                "key": bc.texture_key(image),
                "name": bc.clean_texture_name(image),
                "sampler": bc.sampler_for_image(image),
            }
            for name in names:
                if name:
                    source.setdefault(bc.strip_blender_suffix(name), info)

    out = []
    for key in native[:8]:
        if not key:
            continue
        name = bc.strip_blender_suffix(str(key))
        if name in filled:
            continue
        info = source.get(name)
        if info is None:
            continue
        out.append(dict(info, key=str(key), label=info.get("name") or name))
    return out


def _schedule_texture_transfer(owner, material, candidates):
    """Queue the missing-texture dialog outside the depsgraph handler."""
    global _TRANSFER_PENDING, _TRANSFER_TIMER
    if _UNDO_BUSY or not candidates or owner is None or material is None:
        return
    signature = (owner.name, material.name,
                 tuple(sorted(item["label"] for item in candidates)))
    if signature in _TRANSFER_PROMPTS:
        return
    if getattr(bpy.context, "window", None) is None:
        return
    _TRANSFER_PROMPTS.add(signature)
    entry = {"owner": owner.name, "material": material.name,
             "candidates": candidates}
    if _TRANSFER_PENDING is not None or _TRANSFER_TIMER:
        _TRANSFER_QUEUE.append(entry)
        return
    _TRANSFER_PENDING = entry
    _TRANSFER_TIMER = True
    bpy.app.timers.register(_open_texture_transfer_dialog,
                            first_interval=0.15)


def prompt_next_transfer():
    """Show the next queued missing-texture dialog, if any."""
    global _TRANSFER_PENDING, _TRANSFER_TIMER
    if _TRANSFER_PENDING is not None or _TRANSFER_TIMER:
        return
    while _TRANSFER_QUEUE:
        entry = _TRANSFER_QUEUE.pop(0)
        if getattr(bpy.context, "window", None) is None:
            return
        _TRANSFER_PENDING = entry
        _TRANSFER_TIMER = True
        bpy.app.timers.register(_open_texture_transfer_dialog,
                                first_interval=0.15)
        return


def _open_texture_transfer_dialog():
    global _TRANSFER_PENDING, _TRANSFER_TIMER
    _TRANSFER_TIMER = False
    pending = _TRANSFER_PENDING
    if pending is None or _UNDO_BUSY:
        _TRANSFER_PENDING = None
        return None
    if getattr(bpy.context, "window", None) is None:
        _TRANSFER_PENDING = None
        return None
    try:
        bpy.ops.bmd.material_transfer_textures("INVOKE_DEFAULT")
    except Exception:
        _TRANSFER_PENDING = None
        prompt_next_transfer()
    return None


def take_texture_transfer_pending():
    global _TRANSFER_PENDING
    pending = _TRANSFER_PENDING
    _TRANSFER_PENDING = None
    return pending


def _set_slot_sampler(slot, sampler):
    if not sampler:
        return
    slot.wrap_s = str(int(sampler.get("wrap_s", 1)))
    slot.wrap_t = str(int(sampler.get("wrap_t", 1)))
    slot.min_filter = str(int(sampler.get("min_filter", 1)))
    slot.mag_filter = str(int(sampler.get("mag_filter", 1)))
    slot.min_lod = int(sampler.get("min_lod", 0))
    slot.max_lod = int(sampler.get("max_lod", 0))
    slot.max_lod_custom = bool(sampler.get("max_lod_custom", False))
    slot.lod_bias = int(sampler.get("lod_bias", 0))
    if "transparency" in sampler:
        slot["bmd_transparency"] = int(sampler["transparency"])
    if sampler.get("flags10"):
        slot["bmd_flags10"] = str(sampler["flags10"])
    slot.sampler_ready = True


def apply_texture_transfer(owner_name, material_name, items):
    """Copy the chosen source textures into the target model's slots."""
    owner = bpy.data.objects.get(owner_name)
    material = bpy.data.materials.get(material_name)
    if owner is None or material is None:
        return 0
    bc.ensure_material_texture_slots(owner, material)
    try:
        textures = bc.ensure_texture_list(owner)
    except Exception:
        return 0

    by_name = {}
    for index, slot in enumerate(textures):
        for name in bc._texture_slot_match_names(slot):
            previous = by_name.get(name)
            if (previous is None
                    or (textures[previous].image is None
                        and slot.image is not None)):
                by_name[name] = index

    applied = 0
    for item in items:
        name = bc.strip_blender_suffix(str(item.get("label") or item.get("key") or ""))
        image = bpy.data.images.get(str(item.get("image") or ""))
        if image is None:
            source_owner = bpy.data.objects.get(str(item.get("source_owner") or ""))
            index = int(item.get("source_index", -1))
            if (source_owner is not None and 0 <= index
                    < len(source_owner.bmd_texture_list)):
                image = source_owner.bmd_texture_list[index].image
        if image is None:
            continue

        target = by_name.get(name)
        if target is None:
            slot = textures.add()
            slot.name = name
            target = len(textures) - 1
            by_name[name] = target
        slot = textures[target]
        slot.image = image
        slot.key = str(item.get("key") or bc.texture_key(image))
        slot.name = str(item.get("name") or bc.clean_texture_name(image))
        _set_slot_sampler(slot, item.get("sampler"))
        slot.auto_created = False
        applied += 1

    if applied:
        bc.retarget_material_texture_slots(owner, material)
        try:
            bc.sync_texture_legacy(owner)
        except Exception:
            pass
    return applied


def ensure_owner_material_copies(owner):
    """Give *owner* its own copy of BMD materials shared with another model.

    A texture-order change belongs to one model.  Copying the shared material
    for that model before the indices move keeps the other models (which were
    not edited) on the original material.
    """
    if owner is None:
        return 0
    material_owners = {}
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        obj_owner = _mesh_bmd_owner(obj)
        if obj_owner is None:
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None or not (material.get("bmd_material")
                                        or material.get("bmd_shader_active")):
                continue
            material_owners.setdefault(material, set()).add(obj_owner)

    copies = {}
    for mesh in bc.owner_meshes(owner):
        for slot in mesh.material_slots:
            material = slot.material
            if material is None or not (material.get("bmd_material")
                                        or material.get("bmd_shader_active")):
                continue
            if len(material_owners.get(material, ())) <= 1:
                continue
            target = copies.get(material)
            if target is None:
                target = material.copy()
                target["bmd_name"] = str(target.name)
                target[bc.MATERIAL_LAST_NAME_PROP] = str(target.name)
                isolate_material_groups(target)
                copies[material] = target
            slot.material = target
            bc.retarget_material_texture_slots(owner, target)
    return len(copies)


def _active_bmd_owner():
    try:
        active = bpy.context.view_layer.objects.active
    except Exception:
        active = None
    candidates = [active]
    try:
        candidates.extend(list(bpy.context.selected_objects or ()))
    except Exception:
        pass
    for obj in candidates:
        if obj is None:
            continue
        if obj.type == "MESH":
            owner = _mesh_bmd_owner(obj)
            if owner is not None:
                return owner
        elif obj.type == "ARMATURE" and (
                "bmd_textures" in obj or len(obj.bmd_texture_list)):
            return obj
    return None


def _forget_transfer_prompt(owner, candidates):
    """A new assignment attempt may ask again after an earlier cancel."""
    labels = tuple(sorted(item["label"] for item in candidates))
    for signature in list(_TRANSFER_PROMPTS):
        if signature[0] == owner.name and signature[2] == labels:
            _TRANSFER_PROMPTS.discard(signature)
    _TRANSFER_QUEUE[:] = [
        entry for entry in _TRANSFER_QUEUE
        if not (entry.get("owner") == owner.name
                and tuple(sorted(item["label"]
                                 for item in entry.get("candidates", ()))) == labels)
    ]


def ensure_foreign_material_slots():
    """Retarget BMD materials assigned to a different model.

    A material keeps one texture-slot table.  When it is assigned to another
    BMD model, its old indices select unrelated textures there.  Empty named
    slots are added for the material's missing textures and its slots are
    pointed at the new owner.  If the same material is still used by another
    model, this slot gets an independent copy so neither model is changed.
    The copy is made only for the active model; an unselected model is never
    modified just because it happens to share the material.
    """
    global _ASSIGNMENT_CACHE_READY
    entries = []
    owners = {}
    owner_objects = set()
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        owner = _mesh_bmd_owner(obj)
        if owner is None:
            continue
        owner_objects.add(owner)
        for index, slot in enumerate(obj.material_slots):
            material = slot.material
            if material is None or not (material.get("bmd_material")
                                        or material.get("bmd_shader_active")):
                continue
            entries.append((obj, index, slot, material, owner))
            owners.setdefault(material, set()).add(owner)

    active_owner = _active_bmd_owner()
    all_owners = set(owner_objects)
    if active_owner is None and len(all_owners) == 1:
        active_owner = next(iter(all_owners))

    current_assignments = {}
    prompt_candidates = {}
    for obj, index, slot, material, owner in entries:
        slot_key = (obj.as_pointer(), index)
        if owner != active_owner:
            continue
        was_new = _ASSIGNMENT_CACHE.get(slot_key) != material.as_pointer()
        if not _ASSIGNMENT_CACHE_READY or not was_new:
            continue
        candidates = texture_transfer_candidates(owner, material)
        if candidates:
            _forget_transfer_prompt(owner, candidates)
            prompt_candidates[(owner, material)] = candidates

    copies = {}
    for _obj, _index, slot, material, owner in entries:
        if owner != active_owner:
            continue
        if bc.material_slots_match_owner(owner, material):
            continue
        target = material
        if len(owners.get(material, ())) > 1:
            key = (material, owner)
            target = copies.get(key)
            if target is None:
                target = material.copy()
                target["bmd_name"] = str(target.name)
                target[bc.MATERIAL_LAST_NAME_PROP] = str(target.name)
                isolate_material_groups(target)
                copies[key] = target
                owners.setdefault(target, set()).add(owner)
            if slot.material != target:
                slot.material = target
        bc.retarget_material_texture_slots(owner, target)

    for _obj, _index, slot, _material, _owner in entries:
        if slot.material is not None:
            current_assignments[(_obj.as_pointer(), _index)] = slot.material.as_pointer()

    for owner, material in prompt_candidates:
        final = material
        for _obj, _index, slot, original, entry_owner in entries:
            if entry_owner == owner and original == material:
                final = slot.material or material
                break
        _schedule_texture_transfer(owner, final,
                                   prompt_candidates[(owner, material)])

    _ASSIGNMENT_CACHE.clear()
    _ASSIGNMENT_CACHE.update(current_assignments)
    _ASSIGNMENT_CACHE_READY = True

    for owner in owner_objects:
        bc.prune_auto_texture_slots(owner)


@bpy.app.handlers.persistent
def _texture_recovery_handler(scene=None, depsgraph=None):
    """Follow slot assignments; preview image swaps are ignored."""
    if _RECOVERY_UPDATING or _UNDO_BUSY:
        return
    ensure_material_isolation()
    ensure_foreign_material_slots()
    key_images = None
    for material in bpy.data.materials:
        if not (material.get("bmd_material")
                or material.get("bmd_shader_active")):
            continue
        if key_images is None:
            key_images = _image_key_map()
        signature = _recovery_signature(material, key_images)
        if signature is None:
            continue
        if _RECOVERY_SIGNATURE.get(material.name) == signature:
            continue
        _RECOVERY_SIGNATURE[material.name] = signature
        refresh_texture_recovery(material)


def _fill_assignment_cache():
    global _ASSIGNMENT_CACHE_READY
    _ASSIGNMENT_CACHE.clear()
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.data is None:
            continue
        for index, slot in enumerate(obj.material_slots):
            if slot.material is not None:
                _ASSIGNMENT_CACHE[(obj.as_pointer(), index)] = (
                    slot.material.as_pointer())
    _ASSIGNMENT_CACHE_READY = True


@bpy.app.handlers.persistent
def _reset_transfer_cache(*_args):
    global _ASSIGNMENT_CACHE_READY, _TRANSFER_PENDING, _TRANSFER_TIMER
    _fill_assignment_cache()
    _TRANSFER_PROMPTS.clear()
    _TRANSFER_PENDING = None
    _TRANSFER_TIMER = False
    _TRANSFER_QUEUE.clear()


@bpy.app.handlers.persistent
def _undo_busy_pre(*_args):
    global _UNDO_BUSY, _ASSIGNMENT_CACHE_READY, _TRANSFER_PENDING, _TRANSFER_TIMER
    _UNDO_BUSY = True
    # Undo restores the scene; never open a texture-transfer dialog from the
    # restored state.  Treat the next update as a fresh scan only.
    _TRANSFER_PENDING = None
    _TRANSFER_TIMER = False
    _TRANSFER_QUEUE.clear()
    _TRANSFER_PROMPTS.clear()
    _ASSIGNMENT_CACHE.clear()
    _ASSIGNMENT_CACHE_READY = False


@bpy.app.handlers.persistent
def _undo_cleanup_auto_slots(*_args):
    """Drop empty assignment slots that no material uses after undo/redo."""
    global _UNDO_BUSY, _ASSIGNMENT_CACHE_READY
    try:
        for owner in bpy.data.objects:
            if owner.type in {"ARMATURE", "MESH"}:
                try:
                    bc.prune_auto_texture_slots(owner)
                except Exception:
                    pass
    finally:
        _fill_assignment_cache()
        _UNDO_BUSY = False


def install_texture_recovery():
    handlers = bpy.app.handlers.depsgraph_update_post
    if _texture_recovery_handler not in handlers:
        handlers.append(_texture_recovery_handler)
    for handlers in (bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        if _undo_busy_pre not in handlers:
            handlers.append(_undo_busy_pre)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _undo_cleanup_auto_slots not in handlers:
            handlers.append(_undo_cleanup_auto_slots)
    if _reset_transfer_cache not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_reset_transfer_cache)
    _reset_transfer_cache()
    _RECOVERY_SIGNATURE.clear()


def uninstall_texture_recovery():
    handlers = bpy.app.handlers.depsgraph_update_post
    if _texture_recovery_handler in handlers:
        handlers.remove(_texture_recovery_handler)
    for handlers in (bpy.app.handlers.undo_pre, bpy.app.handlers.redo_pre):
        if _undo_busy_pre in handlers:
            handlers.remove(_undo_busy_pre)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _undo_cleanup_auto_slots in handlers:
            handlers.remove(_undo_cleanup_auto_slots)
    if _reset_transfer_cache in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_reset_transfer_cache)
    _RECOVERY_SIGNATURE.clear()


def _indirect_exponent(value):
    """Sign-extend the 5-bit indirect matrix exponent stored in a byte."""
    value = int(value) & 0x1F
    return value - 32 if value >= 16 else value


def _indirect_stages(data):
    """Active indirect texture stages from the material's IND block.

    The block keeps one indirect entry per TEV stage (16 of them) and an
    entry is only active when it names an indirect matrix (ids 1-3).  The
    entry itself stores a small indirect-stage slot, the texture format, the
    bias and the matrix id; the texcoord/texture pair used for the lookup
    lives in ``indTexOrder[slot]``.
    """
    raw = data.get("indirect")
    if not raw:
        return []
    try:
        blob = bytes.fromhex(raw)
    except (TypeError, ValueError):
        return []
    if len(blob) < 0x138 or not blob[0]:
        return []
    orders = [(blob[4 + 4 * i], blob[5 + 4 * i]) for i in range(4)]
    scales = [(blob[0x68 + 4 * i], blob[0x69 + 4 * i]) for i in range(4)]
    matrices = []
    for index in range(3):
        offset = 0x14 + 28 * index
        matrices.append((struct.unpack_from(">6f", blob, offset),
                         _indirect_exponent(blob[offset + 24])))

    stages = []
    for tev_stage in range(16):
        offset = 0x78 + 12 * tev_stage
        if offset + 4 > len(blob):
            break
        slot, tex_format, bias, matrix = blob[offset:offset + 4]
        if not 1 <= matrix <= 3 or slot >= 4:
            continue
        texcoord, texmap = orders[slot]
        if texcoord == 0xFF or texmap == 0xFF:
            continue
        values, exponent = matrices[matrix - 1]
        stages.append({
            "tev_stage": tev_stage,
            "format": int(tex_format),
            "bias": int(bias),
            "matrix": int(matrix),
            "texcoord": int(texcoord),
            "texmap": int(texmap),
            "scale_s": int(scales[slot][0]),
            "scale_t": int(scales[slot][1]),
            "ind_matrix": tuple(float(value) for value in values),
            "exponent": int(exponent),
        })
    return stages


def _indirect_offset(tree, texture_node, slot, info, location):
    """UV offset produced by one GX indirect texture lookup.

    The sampled texel is biased, multiplied by the 2x3 indirect matrix (which
    is already scaled by the stored exponent) and then added to the TEV
    texcoord.  For the 8-bit indirect format GX reads the texture as
    ``(A, B, G) = (S, T, U)``; the smaller formats read ``(R, A, B)``.
    """
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    separate.location = location
    separate[ANIM_PROP] = "indirect"
    separate["bmd_indirect_stage"] = int(info["tev_stage"])
    tree.links.new(_texture_color(tree, texture_node, slot),
                   separate.inputs["Color"])

    tex_format = int(info.get("format", 0) or 0)
    if tex_format == 0:                     # GX_ITF_8: (A, B, G) = S, T, U
        s_raw = texture_node.outputs["Alpha"]
        t_raw = separate.outputs["Blue"]
        center = 0.5
    else:                                   # GX_ITF_5/4/3: (R, A, B)
        s_raw = separate.outputs["Red"]
        t_raw = texture_node.outputs["Alpha"]
        center = 0.0

    bias = int(info.get("bias", 0) or 0)
    x = location[0] + 200.0
    y = location[1]
    if center and bias & 1:                 # S
        s_raw = _new_math(tree, "SUBTRACT", s_raw, center, (x, y)).outputs[0]
    if center and bias & 2:                 # T
        t_raw = _new_math(tree, "SUBTRACT", t_raw, center,
                          (x, y - 120.0)).outputs[0]

    values = info.get("ind_matrix") or (0.5, 0.0, 0.0, 0.0, 0.5, 0.0)
    scale = float(2.0 ** int(info.get("exponent", 0) or 0))

    def row(index, base):
        # Column-major 2x3: row 0 = (m00, m01), row 1 = (m10, m11), applied to
        # the (S, T) sample pair.
        terms = []
        for term, socket in enumerate((s_raw, t_raw)):
            factor = values[index + 3 * term] * scale
            if abs(factor) < 1e-9:
                continue
            terms.append(_new_math(
                tree, "MULTIPLY", socket, factor,
                (x + 180.0, base - 60.0 * term)).outputs[0])
        if not terms:
            return 0.0
        result = terms[0]
        for term in terms[1:]:
            result = _new_math(tree, "ADD", result, term,
                               (x + 340.0, base)).outputs[0]
        return result

    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    combine.location = (x + 520.0, y - 60.0)
    combine[ANIM_PROP] = "indirect"
    combine["bmd_indirect_stage"] = int(info["tev_stage"])
    _put(tree, combine.inputs["X"], row(0, y))
    _put(tree, combine.inputs["Y"], row(1, y - 200.0))
    combine.inputs["Z"].default_value = 0.0
    return combine.outputs["Vector"]


def _uv_node(tree, material, index, slot=None):
    """UV Map node dedicated to one texture slot (never shared)."""
    name = f"bmd_uv_tex{slot}" if slot is not None else "bmd_uv_tex"
    node = tree.nodes.get(name)
    if node is not None and node.type != "UVMAP":
        tree.nodes.remove(node)
        node = None
    if node is None:
        node = tree.nodes.new("ShaderNodeUVMap")
        node.name = name
        node["bmd_tex_slot"] = int(slot) if slot is not None else -1
    node["bmd_uv_index"] = int(index)
    node.uv_map = bc.uv_layer_name(material, index)
    return node


def _texmtx_srt(matrix):
    """Mapping node parameters for a MAT3 texture matrix SRT.

    The game transforms GX texture coordinates as
    ``out = R * S * (uv - origin) + origin + translation`` with V=0 at the top
    of the texture.  Blender UVs are V-flipped and the Mapping node evaluates
    ``out = R * S * uv + location``, so the SRT is conjugated with the flip:
    the pivot and the V translation move to the other side and the rotation
    direction reverses.
    """
    origin = matrix.get("center") or [0.5, 0.5, 0.5]
    scale = matrix.get("scale") or [1.0, 1.0]
    rotation = float(matrix.get("rotation") or 0) * math.pi / 32768.0
    translation = matrix.get("translation") or [0.0, 0.0]
    ox = float(origin[0])
    oy = 1.0 - float(origin[1])
    sx = float(scale[0])
    sy = float(scale[1])
    tx = float(translation[0])
    ty = -float(translation[1])
    c, s = math.cos(rotation), math.sin(rotation)
    # R(-rotation) * S * origin, the part the pivot has to cancel out.
    px = c * sx * ox + s * sy * oy
    py = -s * sx * ox + c * sy * oy
    return ((tx + ox - px, ty + oy - py), -rotation, (sx, sy))


def _btk_node(tree, slot, info):
    matrix = info.get("matrix")
    if matrix is None:
        return None
    name = f"bmd_btk_tex{slot}"
    node = tree.nodes.get(name)
    if node is not None:
        return node
    location, rotation, scale = _texmtx_srt(matrix)
    if (abs(location[0]) < 1e-6 and abs(location[1]) < 1e-6
            and abs(scale[0] - 1.0) < 1e-6
            and abs(scale[1] - 1.0) < 1e-6
            and abs(rotation) < 1e-6):
        return None
    node = tree.nodes.new("ShaderNodeMapping")
    node.name = name
    node.label = f"BTK texMtx {info.get('matrix_index')}"
    node[ANIM_PROP] = ANIM_BTK
    node["bmd_anim_component"] = "srt"
    node["bmd_btk_slot"] = int(slot)
    node["bmd_btk_matrix"] = int(info.get("matrix_index") or 0)
    node["bmd_btk_uv"] = int(info.get("uv_index") or 0)
    node.vector_type = "POINT"
    node.inputs["Location"].default_value = (location[0], location[1], 0.0)
    node.inputs["Rotation"].default_value = (0.0, 0.0, rotation)
    node.inputs["Scale"].default_value = (scale[0], scale[1], 1.0)
    return node


def _attach_texgen(material, node, slot, info, offset=None, unit_scale=1.0):
    tree = node.id_data
    source = None
    if info.get("source") in (0, 1, 2, 3):
        # GX_TG_POS / NRM / BIN / TAN: generated from a vertex vector.
        source = _vector_texgen(material, tree, node, slot, info, unit_scale)
    if source is None:
        uv = _uv_node(tree, material, info.get("uv_index", 0), slot)
        uv.location = uv_node_location(node)
        source = uv.outputs["UV"]
        mapping = _btk_node(tree, slot, info)
        if mapping is not None:
            mapping.location = (node.location.x - 520.0,
                                node.location.y - 160.0)
            tree.links.new(source, mapping.inputs["Vector"])
            source = mapping.outputs["Vector"]
    if offset is not None:
        # Indirect UV offset; one node per slot, reused when several stages
        # share the texture.
        name = f"bmd_indirect_uv{slot}"
        add = tree.nodes.get(name)
        if add is None or add.type != "VECT_MATH" or add.operation != "ADD":
            if add is not None:
                tree.nodes.remove(add)
            add = _new_vmath(tree, "ADD", source, offset,
                             (node.location.x - 780.0,
                              node.location.y - 320.0))
            add.name = name
        else:
            tree.links.new(source, add.inputs[0])
            tree.links.new(offset, add.inputs[1])
            add.location = (node.location.x - 780.0,
                            node.location.y - 320.0)
        source = add.outputs["Vector"]
    _link(tree, node.inputs["Vector"], source)
    wrap_s, wrap_t = _wrap_modes(node)
    apply_wrap_to_node(node, wrap_s, wrap_t, material)


def _bpk_rgb(tree, kind, value, location, suffix="", label=None):
    return _anim_rgb(tree, f"bmd_bpk_{kind}{suffix}", ANIM_BPK, value, location,
                     label or f"BPK {kind}Color", table=kind, index=0)


def _bpk_alpha(tree, kind, value, location, suffix="", label=None):
    return _anim_alpha(tree, f"bmd_bpk_{kind}_a{suffix}", ANIM_BPK, value,
                       location, label or f"BPK {kind} alpha",
                       table=kind, index=0)


def _build_raster(material, data, with_alpha=True, with_color=True,
                  stage_index=0):
    """Raster colour/alpha for one TEV stage.

    Each stage that reads the raster gets its own chain (and its own Color
    Attribute node), so every TEV block is self-contained instead of all
    stages sharing one node.  Stage 0 keeps the historical names; later
    stages append their index.

    Materials that only read the raster *alpha* (RASA) would otherwise get an
    unused node from the BPK matColor x vertex colour multiply.  Build each
    half only when the stage actually selects it.
    """
    tree = material.node_tree
    anchor = _layout_node(tree)
    if anchor is None:
        return None, None
    index = int(stage_index)
    suffix = "" if index == 0 else str(index)
    block = index + 1
    channels = data.get("colorChan") or []
    channel = channels[0] if channels else None
    values = data.get("matColor") or []
    value = values[0] if values else [255, 255, 255, 255]
    x = anchor.location.x - 380.0
    y = anchor.location.y + 260.0
    label_suffix = "" if index == 0 else f" {block}"
    rgb = (_bpk_rgb(tree, "mat", value, (x, y), suffix,
                    f"BPK matColor{label_suffix}")
           if with_color else None)
    alpha = (_bpk_alpha(tree, "mat", value, (x, y + 120.0), suffix,
                        f"BPK mat alpha{label_suffix}")
             if with_alpha else None)
    vertex_source = bool(channel and len(channel) > 1 and channel[1] == 1)

    if vertex_source:
        vc = tree.nodes.get(f"bmd_ras_vc{suffix}")
        if vc is None or vc.type != "VERTEX_COLOR":
            if vc is not None:
                tree.nodes.remove(vc)
            vc = tree.nodes.new("ShaderNodeVertexColor")
            vc.name = f"bmd_ras_vc{suffix}"
            vc[ANIM_PROP] = ANIM_TEV_VC
        vc.layer_name = color_attribute_name(material) or "Col0"
        vc.location = (anchor.location.x - 400.0, anchor.location.y - 620.0)
        color = None
        if rgb is not None:
            color = _new_vmath(tree, "MULTIPLY", rgb.outputs[0],
                               vc.outputs["Color"],
                               (anchor.location.x - 600.0,
                                anchor.location.y - 300.0))
            color.name = f"bmd_ras_color{suffix}"
            color[ANIM_PROP] = ANIM_TEV_RAS
            color["bmd_ras_kind"] = "color"
        alpha_out = None
        if alpha is not None:
            alpha_out = _new_math(
                tree, "MULTIPLY", alpha.outputs[0], vc.outputs["Alpha"],
                (anchor.location.x - 600.0, anchor.location.y - 460.0))
            alpha_out.name = f"bmd_ras_alpha{suffix}"
            alpha_out[ANIM_PROP] = ANIM_TEV_RAS
            alpha_out["bmd_ras_kind"] = "alpha"
        return (color.outputs["Vector"] if color is not None else None,
                alpha_out.outputs[0] if alpha_out is not None else None)
    return (rgb.outputs[0] if rgb is not None else None,
            alpha.outputs[0] if alpha is not None else None)


def _material_uses_raster(data):
    """Whether the raster colour of channel 0 can use the vertex colour."""
    channels = data.get("colorChan") or []
    if not channels or not channels[0]:
        return False
    channel = channels[0]
    return len(channel) > 1 and channel[1] == 1


def _raster_needs_alpha(data):
    for _index, stage in _used_stages(data):
        if 5 in (stage.get("alphaIn") or []):
            return True
        if 11 in (stage.get("colorIn") or []):
            return True
    return False


def has_tev_graph(material):
    tree = getattr(material, "node_tree", None)
    return tree is not None and any(
        node.get(ANIM_PROP) == ANIM_TEV_STAGE for node in tree.nodes)


def _final_stage_outputs(tree):
    nodes = [node for node in tree.nodes
             if node.get(ANIM_PROP) == ANIM_TEV_STAGE]
    if not nodes:
        return None, None
    node = max(nodes, key=lambda item: int(item.get("bmd_tev_index", 0)))
    return node.outputs.get("Color"), node.outputs.get("Alpha")


def _selection(values, index):
    if not values:
        return 0
    try:
        return int(values[index])
    except (IndexError, TypeError, ValueError):
        return 0


def _konst_value(selector):
    if 0 <= selector <= 7:
        value = _KONST_VALUES[selector]
        return value, value, value, 1.0
    return 0.0, 0.0, 0.0, 1.0


def _konst_alpha_value(selector):
    return _KONST_VALUES[selector] if 0 <= selector <= 7 else 0.0


class _TevBuilder:
    """Resolve TEV selectors to linked sockets or constant values."""

    def __init__(self, material, data, anchor, slot_nodes, stages,
                 raster_scale=1.0):
        self.tree = material.node_tree
        self.material = material
        self.data = data
        self.anchor = anchor
        self.slot_nodes = slot_nodes
        self.stages = dict(stages)
        self.raster_scale = raster_scale
        self.raster_nodes = {}
        self.raster_cache = {}
        self.prev_color = (0.0, 0.0, 0.0, 1.0)
        self.prev_alpha = 0.0
        # Values a TEV stage wrote to C0-C2 / the colour registers.  A later
        # stage that selects that register must read the written value, not
        # the static MAT3 entry (GX registers start at the material values and
        # are overwritten as the stages run).
        self.reg_color = {}
        self.reg_alpha = {}
        self.brk_color = {}
        self.brk_alpha = {}
        self.k_rgb = {}
        self.k_alpha = {}
        self.k_channels = {}

    def _register(self, index, alpha=False, raw=False):
        written = self.reg_alpha if alpha else self.reg_color
        if index in written:
            return written[index]
        cache = self.brk_alpha if alpha else self.brk_color
        key = (index, raw)
        if key not in cache:
            values = self.data.get("tevColor") or []
            value = (values[index]
                     if index < len(values) else [255, 255, 255, 255])
            location = (self.anchor.location.x - 740.0,
                        self.anchor.location.y + 260.0 + 140.0 * index)
            # A/B/C inputs are wrapped to 8 bits by GX (a negative s16
            # component is the low byte of the colour); the D input keeps the
            # signed value so it can subtract from the result.
            suffix = "_d" if raw else ""
            if alpha:
                node = _anim_alpha(
                    self.tree, f"bmd_brk_c{index}_a{suffix}", ANIM_BRK,
                    value, (location[0], location[1] - 100.0),
                    f"BRK C{index} alpha", table="c", index=index,
                    wrap=not raw)
            else:
                node = _anim_rgb(
                    self.tree, f"bmd_brk_c{index}{suffix}", ANIM_BRK, value,
                    location, f"BRK C{index}", table="c", index=index,
                    wrap=not raw)
            cache[key] = node.outputs[0]
        return cache[key]

    def _k_rgb_node(self, index):
        if index not in self.k_rgb:
            values = self.data.get("kColor") or []
            value = (values[index]
                     if index < len(values) else [255, 255, 255, 255])
            location = (self.anchor.location.x - 560.0,
                        self.anchor.location.y + 260.0 + 140.0 * index)
            self.k_rgb[index] = _anim_rgb(
                self.tree, f"bmd_brk_k{index}", ANIM_BRK, value, location,
                f"BRK K{index}", table="k", index=index)
        return self.k_rgb[index]

    def _k_alpha_node(self, index):
        if index not in self.k_alpha:
            values = self.data.get("kColor") or []
            value = (values[index]
                     if index < len(values) else [255, 255, 255, 255])
            location = (self.anchor.location.x - 560.0,
                        self.anchor.location.y + 160.0 + 140.0 * index)
            self.k_alpha[index] = _anim_alpha(
                self.tree, f"bmd_brk_k{index}_a", ANIM_BRK, value, location,
                f"BRK K{index} alpha", table="k", index=index)
        return self.k_alpha[index]

    def _k_channel(self, index, channel):
        key = (index, channel)
        if key not in self.k_channels:
            rgb = self._k_rgb_node(index)
            name = f"bmd_brk_k{index}_{channel.lower()}"
            node = self.tree.nodes.get(name)
            if node is None:
                node = self.tree.nodes.new("ShaderNodeSeparateColor")
                node.name = name
                node[ANIM_PROP] = ANIM_BRK
                node["bmd_anim_table"] = "k"
                node["bmd_anim_index"] = int(index)
                node.location = (rgb.location.x + 220.0, rgb.location.y)
                self.tree.links.new(rgb.outputs[0], node.inputs["Color"])
            self.k_channels[key] = node.outputs[channel]
        return self.k_channels[key]

    def _konst_color(self, stage_index):
        selector = _selection(self.data.get("kColorSel"), stage_index)
        if selector <= 7:
            return _konst_value(selector)
        if selector <= 11:
            return 0.0, 0.0, 0.0, 1.0
        if selector <= 15:
            return self._k_rgb_node(selector - 12).outputs[0]
        if selector <= 27:
            channel = _KONST_CHANNELS[(selector - 16) // 4]
            return self._k_channel((selector - 16) % 4, channel)
        if selector <= 31:
            return self._k_alpha_node(selector - 28).outputs[0]
        return 0.0, 0.0, 0.0, 1.0

    def _konst_alpha(self, stage_index):
        selector = _selection(self.data.get("kAlphaSel"), stage_index)
        if selector <= 7:
            return _konst_alpha_value(selector)
        if selector <= 15:
            return 0.0
        if selector <= 27:
            channel = _KONST_CHANNELS[(selector - 16) // 4]
            return self._k_channel((selector - 16) % 4, channel)
        if selector <= 31:
            return self._k_alpha_node(selector - 28).outputs[0]
        return 0.0

    def _texture_node(self, stage_index):
        orders = self.data.get("tevOrder") or []
        if stage_index >= len(orders) or not orders[stage_index]:
            return None, None
        slot = orders[stage_index][1]
        return self.slot_nodes.get(slot), slot

    def _texture_color(self, stage_index):
        """sRGB-recovered texture colour; only built when TEXC is selected."""
        node, slot = self._texture_node(stage_index)
        if node is None:
            return 1.0, 1.0, 1.0, 1.0
        return _texture_color(self.tree, node, slot)

    def _texture_alpha(self, stage_index):
        """Raw texture alpha (never gamma converted)."""
        node, _slot = self._texture_node(stage_index)
        if node is None:
            return 1.0
        return node.outputs["Alpha"]

    def _raster_pair(self, stage_index):
        """Raster chain of one TEV stage, built on first use."""
        pair = self.raster_cache.get(stage_index)
        if pair is not None:
            return pair
        stage = self.stages.get(stage_index) or {}
        color_in = [int(arg) for arg in (stage.get("colorIn") or [])]
        alpha_in = [int(arg) for arg in (stage.get("alphaIn") or [])]
        with_color = 10 in color_in
        with_alpha = 5 in alpha_in or 11 in color_in
        if with_color or with_alpha:
            pair = _build_raster(self.material, self.data, with_alpha,
                                 with_color, stage_index)
        else:
            pair = (None, None)
        self.raster_cache[stage_index] = pair
        return pair

    def _raster_alpha(self, stage_index):
        _color, alpha = self._raster_pair(stage_index)
        return alpha if alpha is not None else 1.0

    def _raster_color(self, stage_index):
        """Raster colour, dimmed for materials that add it without a scale."""
        color, _alpha = self._raster_pair(stage_index)
        value = color if color is not None else (1.0, 1.0, 1.0, 1.0)
        factor = self.raster_scale
        if factor == 1.0:
            return value
        if _is_const(value):
            return (value[0] * factor, value[1] * factor, value[2] * factor,
                    value[3])
        node = self.raster_nodes.get(stage_index)
        if node is None:
            node = _vmul(self.tree, value, (factor, factor, factor, 1.0),
                         (self.anchor.location.x - 520.0,
                          self.anchor.location.y + 40.0))
            suffix = "" if int(stage_index) == 0 else str(int(stage_index))
            inner = getattr(node, "node", None)
            if inner is not None:
                inner.name = f"bmd_raster_scale{suffix}"
            self.raster_nodes[stage_index] = node
        return node

    def color(self, argument, stage_index, raw=False):
        argument = int(argument)
        if argument == 0:
            return self.prev_color
        if argument == 1:
            return self.prev_alpha
        if 2 <= argument <= 7:
            return self._register((argument - 2) // 2,
                                  alpha=bool(argument & 1), raw=raw)
        if argument == 8:
            return self._texture_color(stage_index)
        if argument == 9:
            return self._texture_alpha(stage_index)
        if argument in (10, 11):
            if argument == 10:
                return self._raster_color(stage_index)
            return self._raster_alpha(stage_index)
        if argument == 12:
            return 1.0, 1.0, 1.0, 1.0
        if argument == 13:
            return 0.5, 0.5, 0.5, 1.0
        if argument == 14:
            return self._konst_color(stage_index)
        return 0.0, 0.0, 0.0, 1.0

    def alpha(self, argument, stage_index, raw=False):
        argument = int(argument)
        if argument == 0:
            return self.prev_alpha
        if 1 <= argument <= 3:
            return self._register(argument - 1, alpha=True, raw=raw)
        if argument == 4:
            return self._texture_alpha(stage_index)
        if argument == 5:
            return self._raster_alpha(stage_index)
        if argument == 6:
            return self._konst_alpha(stage_index)
        return 0.0


def _open_node_groups():
    """Node groups an editor has open; they must not be freed from Python.

    Removing a group that a Node Editor is currently editing leaves the editor
    with a dangling tree, which crashes (e.g. when an undo step then restores
    the file).
    """
    trees = set()
    try:
        for window in bpy.context.window_manager.windows:
            screen = getattr(window, "screen", None)
            if screen is None:
                continue
            for area in screen.areas:
                if area.type != "NODE_EDITOR":
                    continue
                space = area.spaces.active
                for attr in ("node_tree", "edit_tree"):
                    tree = getattr(space, attr, None)
                    if tree is not None:
                        try:
                            trees.add(tree.as_pointer())
                        except ReferenceError:
                            pass
    except Exception:
        pass
    return trees


def build_import_nodes(material, data, images, scale=0.01, samplers=None):
    """Rebuild an imported BMD material's preview nodes from TEV data."""
    try:
        material.use_nodes = True
    except Exception:
        pass
    tree = material.node_tree
    if tree is None:
        return
    if not material.get(bc.MATERIAL_LAST_NAME_PROP):
        material[bc.MATERIAL_LAST_NAME_PROP] = str(material.name)
    tree.nodes.clear()
    protected = _open_node_groups()
    for group in list(bpy.data.node_groups):
        if (group.get("bmd_tev_owner") == material.name
                or group.get("bmd_pos_texgen_owner") == material.name
                or group.get("bmd_wrap_owner") == material.name):
            if group.users == 0:
                try:
                    if group.as_pointer() in protected:
                        continue
                except ReferenceError:
                    continue
                bpy.data.node_groups.remove(group)
    _ensure_output(tree)
    stages = _used_stages(data)
    blend_kind = blend_preview_kind(material, data)
    special = blend_kind is not None
    bsdf = None
    if not special:
        _output, bsdf = ensure_output_bsdf(tree)
    anchor = _layout_node(tree)

    if not stages:
        if special:
            _output, bsdf = ensure_output_bsdf(tree)
        _build_simple_nodes(material, data, images, samplers)
        if special:
            node = _node(tree, "BSDF_PRINCIPLED")
            if node is not None:
                tree.nodes.remove(node)
        layout_material_tree(material)
        return

    unit_scale = (1.0 / float(scale)) if scale else 100.0
    tex_names = data.get("texNames") or [None] * 8
    # Every texture map a stage samples gets a node, even when the model has no
    # image for it yet (a shader moved from another model): the node stays empty
    # and the user can drop an image in, instead of the stage losing its texture.
    used_maps = set()
    for _index, stage in stages:
        orders = data.get("tevOrder") or []
        if _index < len(orders) and orders[_index]:
            used_maps.add(int(orders[_index][1]))
    # Indirect-map textures are sampled through indTexOrder, not tevOrder:
    # without their slot here a preset whose indirect texture is missing would
    # silently lose the whole indirect branch.
    for entry in _indirect_stages(data):
        used_maps.add(int(entry["texmap"]))
    slot_nodes = {}
    # The material's own slot map is the authority for which slots exist, so
    # an empty preset texture (created by the shader import for a missing
    # image) gets its node exactly like a texture from the BMD itself.
    bound_slots = bc.material_texture_slots(material)
    for slot in range(8):
        texture_name = tex_names[slot] if slot < len(tex_names) else None
        if texture_name is None and slot not in used_maps:
            if slot >= len(bound_slots) or bound_slots[slot] is None:
                continue
        image = images.get(texture_name) if texture_name else None
        slot_nodes[slot] = ensure_texture_node(
            material, slot, image,
            attach_uv=False,
            sampler=(samplers or {}).get(texture_name))

    indirect_offsets = {}
    used_stages = {index for index, _stage in stages}
    for stage in _indirect_stages(data):
        # One entry per TEV stage: the offset belongs to the texture that
        # stage samples, not to the indirect map itself.
        tev_stage = stage["tev_stage"]
        if tev_stage not in used_stages:
            continue
        orders = data.get("tevOrder") or []
        target = (orders[tev_stage][1]
                  if tev_stage < len(orders) and orders[tev_stage] else None)
        if target is None:
            continue
        slot = stage["texmap"]
        node = slot_nodes.get(slot)
        if node is None:
            continue
        info = _texgen_info(data, stage["texcoord"])
        _attach_texgen(material, node, slot, info, unit_scale=unit_scale)
        offset = _indirect_offset(
            tree, node, slot, stage,
            (node.location.x - 200.0, node.location.y - 400.0))
        if target in indirect_offsets:
            indirect_offsets[target] = _vadd(
                tree, indirect_offsets[target], offset,
                (node.location.x + 200.0, node.location.y - 500.0))
        else:
            indirect_offsets[target] = offset

    for stage_index, _stage in stages:
        orders = data.get("tevOrder") or []
        if stage_index >= len(orders) or not orders[stage_index]:
            continue
        slot = orders[stage_index][1]
        node = slot_nodes.get(slot)
        if node is None:
            continue
        info = _texgen_info(data, orders[stage_index][0])
        _attach_texgen(material, node, slot, info,
                       indirect_offsets.get(slot), unit_scale)

    # TEV colour input 10 is the raster colour, 11 is the raster alpha.
    # Each stage builds its own raster chain (with its own Color Attribute
    # node) so the TEV blocks stay self-contained.
    cancel_scales, raster_scale = _raster_model(stages, data)
    builder = _TevBuilder(material, data, anchor, slot_nodes, stages,
                          raster_scale=raster_scale)
    for stage_index, stage in stages:
        node = _add_stage_node(tree, material, stage_index, stage, anchor, data,
                               stage_index in cancel_scales)
        color_args = list(stage.get("colorIn") or [15, 15, 15, 15])
        alpha_args = list(stage.get("alphaIn") or [7, 7, 7, 7])
        for label, argument in zip(("Color A", "Color B", "Color C",
                                    "Color D"), color_args):
            _put(tree, node.inputs[label],
                 builder.color(argument, stage_index,
                               raw=(label == "Color D")))
        for label, argument in zip(("Alpha A", "Alpha B", "Alpha C",
                                    "Alpha D"), alpha_args):
            _put(tree, node.inputs[label],
                 builder.alpha(argument, stage_index,
                               raw=(label == "Alpha D")))

        color = node.outputs["Color"]
        alpha = node.outputs["Alpha"]
        # "prev" is a persistent register: a stage updates it only when its
        # destination is Prev.  Writing to C0-C2 leaves prev untouched.
        color_reg = int(stage.get("colorReg", 0) or 0)
        if color_reg in (1, 2, 3):
            builder.reg_color[color_reg - 1] = color
        else:
            builder.prev_color = color
        alpha_reg = int(stage.get("alphaReg", 0) or 0)
        if alpha_reg in (1, 2, 3):
            builder.reg_alpha[alpha_reg - 1] = alpha
        else:
            builder.prev_alpha = alpha
        last_regs = (color_reg, alpha_reg)

    final_color = builder.prev_color
    final_alpha = builder.prev_alpha
    last_color_reg, last_alpha_reg = last_regs
    if last_color_reg in (1, 2, 3):
        final_color = builder.reg_color[last_color_reg - 1]
    if last_alpha_reg in (1, 2, 3):
        final_alpha = builder.reg_alpha[last_alpha_reg - 1]
    if special:
        if blend_kind == "additive":
            linear_color = _srgb_to_linear_node(tree, final_color)
            _build_additive_output(material, tree, linear_color, final_alpha,
                                   data)
        else:
            _build_replace_output(material, tree, final_color, data)
        layout_material_tree(material)
        return

    linear_color = _srgb_to_linear_node(tree, final_color)
    _link(tree, bsdf.inputs["Base Color"], linear_color)

    render_mode = material.bmd.render_mode
    kind, ref = mat3.classify_alpha(data)
    if (kind == mat3.RENDER_OPAQUE
            or not hasattr(final_alpha, "is_linked")
            or (render_mode == "AUTO" and kind == mat3.RENDER_TRANSLUCENT
                and not mat3.blend_uses_alpha(data))):
        _unlink(tree, bsdf.inputs["Alpha"])
        _set_surface_method(material, mat3.RENDER_OPAQUE)
    else:
        apply_alpha(material, tree, bsdf, [final_alpha], kind,
                    int(ref) if ref is not None else 128)
    layout_material_tree(material)


def clear_simple_uv_maps(material):
    """Leave the UV Map field empty on generated simple previews."""
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return
    for node in tree.nodes:
        if node.type == "UVMAP":
            node.uv_map = ""


def build_simple_import_material(material, data, images, samplers=None):
    """Simple texture preview for a BMD imported without shader data."""
    material.use_nodes = True
    material["bmd_simple"] = 1
    tree = material.node_tree
    if tree is None:
        return
    tree.nodes.clear()
    _output, bsdf = ensure_output_bsdf(tree)

    kind, ref = mat3.classify_alpha(data)
    mode = kind if kind in READY_MODES else mat3.RENDER_OPAQUE
    try:
        material.bmd.render_mode = mode
        alpha = (int(ref) if ref is not None
                 else int((data.get("alphaComp") or [0, 128])[1]))
        material.bmd.alpha_ref = max(0, min(255, alpha))
    except Exception:
        pass
    material.use_backface_culling = (data.get("cull") == 2)

    simple = dict(data)
    simple["colorChanCount"] = 0
    simple["colorChan"] = []
    _build_simple_nodes(material, simple, images, samplers)
    keys = [str(name) if name else None
            for name in (data.get("texNames") or [])][:8]
    keys += [None] * (8 - len(keys))
    material["bmd_simple_textures"] = json.dumps(keys, separators=(",", ":"))
    _mark_simple_textures(material)
    layout_material_tree(material)
    clear_simple_uv_maps(material)


def _build_simple_nodes(material, data, images, samplers=None):
    """Fallback preview for materials without TEV stages."""
    tree = material.node_tree
    bsdf = _node(tree, "BSDF_PRINCIPLED")
    tex_names = data.get("texNames", [None] * 8)
    images_by_slot = {}
    for slot, texture_name in enumerate(tex_names):
        if texture_name is not None and texture_name in images:
            images_by_slot[slot] = images[texture_name]

    main_slot = min(images_by_slot) if images_by_slot else None
    main_coord = 0
    if main_slot is not None:
        for order in data.get("tevOrder", []):
            if order and order[1] == main_slot and 0 <= order[0] < 8:
                main_coord = order[0]
                break

    uv_index = 0
    tex_coord = data.get("texCoord", [None] * 8)
    if (main_coord < 8 and tex_coord[main_coord]
            and 4 <= tex_coord[main_coord][1] <= 11):
        uv_index = tex_coord[main_coord][1] - 4

    slot_nodes = {}
    for slot in sorted(images_by_slot):
        texture_name = tex_names[slot]
        slot_nodes[slot] = ensure_texture_node(
            material, slot, images_by_slot[slot], uv_index=uv_index,
            attach_uv=(slot == main_slot),
            sampler=(samplers or {}).get(texture_name))

    color_source = alpha_source = None
    if main_slot is not None:
        node = slot_nodes[main_slot]
        color_source = node.outputs["Color"]
        alpha_source = node.outputs["Alpha"]

    vc = None
    if mat3.uses_vertex_colors(data):
        vc = add_vertex_color(tree, bsdf, "Col0")
    apply_color(tree, bsdf, color_source, vc)

    additive = blend_preview_kind(material, data)
    kind, ref = mat3.classify_alpha(data)
    forced = (material.bmd.render_mode
              if getattr(material, "bmd", None) else "AUTO")
    if (additive is not None
            or kind == mat3.RENDER_OPAQUE
            or (forced == "AUTO" and kind == mat3.RENDER_TRANSLUCENT
                and not mat3.blend_uses_alpha(data))):
        _unlink(tree, bsdf.inputs["Alpha"])
        _set_surface_method(material, mat3.RENDER_OPAQUE)
        base = bsdf.inputs["Base Color"]
        color = base.links[0].from_socket if base.is_linked else None
        if additive == "additive":
            _build_additive_output(material, tree, color, alpha_source, data)
        elif additive == "replace":
            _build_replace_output(material, tree, color, data)
        return

    stage = (data.get("tevStage") or [None])[0]
    alpha_in = stage["alphaIn"] if stage else []
    sources = []
    if alpha_source is not None and (4 in alpha_in or not alpha_in):
        sources.append(alpha_source)
    if vc is not None and 5 in alpha_in:
        sources.append(vc.outputs["Alpha"])
    if not sources and alpha_source is not None:
        sources.append(alpha_source)
    apply_alpha(material, tree, bsdf, sources, kind,
                int(ref) if ref is not None else 128)
    if additive is not None:
        base = bsdf.inputs["Base Color"]
        color = base.links[0].from_socket if base.is_linked else None
        alpha_socket = bsdf.inputs["Alpha"]
        alpha = (alpha_socket.links[0].from_socket
                 if alpha_socket.is_linked else alpha_source)
        if additive == "additive":
            _build_additive_output(material, tree, color, alpha, data)
        else:
            _build_replace_output(material, tree, color, data)


# ---------------------------------------------------------------------------
# Interactive material creation / refresh
# ---------------------------------------------------------------------------

def ensure_base(material, owner=None):
    """Create the output/BSDF pair and texture nodes for the material slots."""
    tree = tree_of(material)
    if tree is None:
        return None

    _output, bsdf = ensure_output_bsdf(tree)

    slots = bc.material_texture_slots(material, owner) if owner else [None] * 8
    textures = bc.ensure_texture_list(owner) if owner is not None else ()
    main_slot = None
    for slot, index in enumerate(list(slots)[:8]):
        if index is None or not (0 <= index < len(textures)):
            continue
        image = textures[index].image
        if image is None:
            continue
        if main_slot is None:
            main_slot = slot
    for slot, index in enumerate(list(slots)[:8]):
        if index is None or not (0 <= index < len(textures)):
            continue
        image = textures[index].image
        if image is None:
            continue
        ensure_texture_node(material, slot, image,
                            attach_uv=(slot == main_slot),
                            sampler=bc.slot_sampler(textures[index]))

    return bsdf


def create_material(material, owner, mode):
    """Build a BMD material preview from scratch for *mode*."""
    if material is None or owner is None:
        return False

    from . import btp_preview

    if not material.get("bmd_material") and not material.get("bmd_shader_active"):
        material["bmd_simple"] = 1
    ensure_base(material, owner)
    if not material.get(bc.MATERIAL_LAST_NAME_PROP):
        material[bc.MATERIAL_LAST_NAME_PROP] = str(material.name)

    begin_sync()
    try:
        material.bmd.render_mode = mode
    finally:
        end_sync()

    btp_preview.prepare_preview(material, owner)
    sync_shading(material)
    _mark_simple_textures(material)
    clear_simple_uv_maps(material)
    return True


def _capture_sources(tree):
    """Remove our generated link nodes, keeping their input sources.

    The vertex colour node itself is kept so toggling the Render Mode does not
    re-create it on every change.
    """
    generated = ROLES - {ROLE_VC}
    captured = {}
    for node in list(tree.nodes):
        role = node.get(ROLE_PROP)
        if role not in generated:
            continue
        if role == ROLE_VC_MIX:
            for socket in node.inputs:
                if socket.type == "RGBA" and socket.is_linked:
                    source = socket.links[0].from_socket
                    if source.node.get(ROLE_PROP) != ROLE_VC:
                        captured.setdefault("color", source)
        elif role in {ROLE_ALPHA_MUL, ROLE_ALPHA_CUTOUT}:
            socket = node.inputs[0]
            if socket.is_linked:
                source = socket.links[0].from_socket
                if source.node.get(ROLE_PROP) != ROLE_VC:
                    captured.setdefault("alpha", source)

    for link in list(tree.links):
        if (link.from_node.get(ROLE_PROP) in generated
                or link.to_node.get(ROLE_PROP) in generated):
            tree.links.remove(link)
    for node in list(tree.nodes):
        if node.get(ROLE_PROP) in generated:
            tree.nodes.remove(node)

    return captured


def sync_shading(material):
    """Adjust alpha/output links for the material's render mode.

    The imported TEV graph is never rebuilt here; a Render Mode change only
    rewires the final alpha path (or swaps the special blend output).
    """
    if material is None or sync_suppressed():
        return
    tree = material.node_tree
    if tree is None:
        return
    mode = material.bmd.render_mode
    data = material_data(material) or {}
    blend_kind = blend_preview_kind(material, data) if has_tev_graph(material) else None

    if blend_kind is not None:
        _capture_sources(tree)
        bsdf = _node(tree, "BSDF_PRINCIPLED")
        anchor = None
        if bsdf is not None:
            anchor = (bsdf.location.x, bsdf.location.y)
            output = _node(tree, "OUTPUT_MATERIAL")
            if output is not None:
                # Remember where the BSDF lived so switching back does not
                # shift the pair on every mode change.
                output["bmd_bsdf_home"] = [anchor[0], anchor[1]]
            tree.nodes.remove(bsdf)
        color, alpha = _final_stage_outputs(tree)
        if blend_kind == "additive":
            linear_color = _srgb_to_linear_node(tree, color)
            _build_additive_output(material, tree, linear_color, alpha, data,
                                   anchor=anchor)
        else:
            _build_replace_output(material, tree, color, data, anchor=anchor)
        _layout_tail(tree)
        return

    if any(node.get(ROLE_PROP) in (ROLE_BLEND_ADD, ROLE_BLEND_MULT)
           for node in tree.nodes):
        _capture_sources(tree)
    _output, bsdf = ensure_output_bsdf(tree)
    _link_bsdf_output(material, tree)

    # Imported TEV materials keep their TEV node graph.  Only the raster
    # (vertex colour) chain and the final alpha test are adjusted here.
    if has_tev_graph(material):
        if mode == "AUTO":
            kind, ref = mat3.classify_alpha(data)
        else:
            kind = mode if mode in READY_MODES else mat3.RENDER_OPAQUE
            ref = material.bmd.alpha_ref
        if (mode == "AUTO" and kind == mat3.RENDER_TRANSLUCENT
                and not mat3.blend_uses_alpha(data)):
            kind = mat3.RENDER_OPAQUE
        _capture_sources(tree)
        _link_bsdf_output(material, tree)
        color, alpha = _final_stage_outputs(tree)
        linear_color = _srgb_to_linear_node(tree, color)
        _link(tree, bsdf.inputs["Base Color"], linear_color)
        apply_alpha(material, tree, bsdf, [alpha] if alpha else [], kind,
                    int(ref) if ref is not None else material.bmd.alpha_ref)
        _layout_tail(tree)
        return

    # Imported materials without TEV stages keep their hand-built preview
    # until a concrete render mode is selected.
    if mode == "AUTO" and material.get("bmd_material"):
        layout_material_tree(material)
        return

    base_color = bsdf.inputs["Base Color"]
    alpha = bsdf.inputs["Alpha"]
    texture = main_texture_node(material)

    captured = _capture_sources(tree)
    _link_bsdf_output(material, tree)

    color_source = captured.get("color")
    if color_source is None and base_color.is_linked:
        color_source = base_color.links[0].from_socket
    if color_source is None and texture is not None:
        color_source = texture.outputs["Color"]

    alpha_source = captured.get("alpha")
    if alpha_source is None and alpha.is_linked:
        alpha_source = alpha.links[0].from_socket
    if alpha_source is None and texture is not None:
        alpha_source = texture.outputs["Alpha"]

    if mode not in READY_MODES:
        mode = mat3.RENDER_OPAQUE

    vc = None
    if material.get("bmd_material") and mat3.uses_vertex_colors(data):
        vc = add_vertex_color(tree, bsdf, color_attribute_name(material))
    else:
        for node in [node for node in tree.nodes
                     if node.get(ROLE_PROP) == ROLE_VC]:
            tree.nodes.remove(node)

    apply_color(tree, bsdf, color_source, vc)

    sources = []
    if alpha_source is not None:
        sources.append(alpha_source)
    if vc is not None and mode != mat3.RENDER_OPAQUE:
        sources.append(vc.outputs["Alpha"])
    apply_alpha(material, tree, bsdf, sources, mode,
                material.bmd.alpha_ref)
    layout_material_tree(material)


# ---------------------------------------------------------------------------
# shader presets (SuperBMD materials.json)
# ---------------------------------------------------------------------------

def _slot_image(slot):
    """Image of a texture slot, finding it by key when the pointer is lost."""
    if slot is None:
        return None
    image = slot.image
    if image is None:
        key = str(slot.key or "")
        if key:
            image = bc.image_by_key(key)
    return image


def _texture_match_name(value):
    """Normalised texture name used to match preset names to slots."""
    name = str(value or "").strip()
    if not name:
        return ""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    base, sep, suffix = name.rpartition(".")
    if sep and len(suffix) == 3 and suffix.isdigit():
        name = base
    low = name.lower()
    for ext in (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff",
                ".dds", ".webp", ".bti"):
        if low.endswith(ext):
            return name[: -len(ext)].casefold()
    return name.casefold()


def _slot_match_names(slot, image):
    """Every name a preset may use for this texture slot."""
    names = {_texture_match_name(getattr(slot, "name", "")),
             _texture_match_name(getattr(slot, "key", ""))}
    if image is not None:
        names.add(_texture_match_name(image.get("bmd_name")))
        names.add(_texture_match_name(bc.clean_texture_name(image)))
    names.discard("")
    return names


def _owner_texture_maps(owner):
    """(images by key, samplers by key, texture index by BMD name) of a model."""
    images, samplers, by_name = {}, {}, {}
    if owner is None:
        return images, samplers, by_name
    try:
        textures = owner.bmd_texture_list
    except AttributeError:
        return images, samplers, by_name
    for index, slot in enumerate(textures):
        key = str(slot.key or "") or bc.texture_slot_key(slot)
        image = _slot_image(slot)
        if key and image is not None:
            images[key] = image
            samplers[key] = bc.slot_sampler(slot)
        for name in _slot_match_names(slot, image):
            by_name.setdefault(name, index)
    return images, samplers, by_name


def _material_scale(material, owner=None):
    for obj in (owner, material):
        try:
            value = bc.bmd_scale_value(obj)
        except Exception:
            value = None
        if value:
            return float(value)
    return 0.01


def _preset_texture_slots(owner, names):
    """Resolve preset texture names to TEX1 indices of *owner*.

    Missing textures are not an error: a slot for every name the preset needs is
    appended to the model's texture list (empty, with the preset's name), so the
    user can drop an image into it.  Returns ``(mapping, created, empty)``.
    """
    mapping, created, empty = {}, [], []
    if owner is None:
        return mapping, created, empty
    try:
        textures = bc.ensure_texture_list(owner)
    except Exception:
        return mapping, created, empty
    filled, blank = {}, {}
    for index, slot in enumerate(textures):
        image = _slot_image(slot)
        target = filled if image is not None else blank
        for match in _slot_match_names(slot, image):
            target.setdefault(match, index)
    for name in names:
        name = str(name or "")
        if not name or name in mapping:
            continue
        match = _texture_match_name(name)
        if match in filled:
            mapping[name] = filled[match]
            continue
        if match in blank:
            mapping[name] = blank[match]
            empty.append(name)
            continue
        slot = textures.add()
        slot.name = name
        try:
            slot.sampler_ready = True
        except Exception:
            pass
        mapping[name] = len(textures) - 1
        created.append(name)
    if created:
        try:
            bc.sync_texture_legacy(owner)
        except Exception:
            pass
    return mapping, created, empty


def _apply_data_settings(material, data):
    """Preview settings that always come from MAT3 data, not from the preset.

    Render Mode stays *Auto*, Alpha Reference is the preset's alpha test
    reference and Backface Culling comes from the cull-mode table, exactly as
    on a normal BMD import.
    """
    data = data or {}
    comp = data.get("alphaComp") or []
    try:
        alpha = int(comp[1] if len(comp) > 1 else 128)
    except (TypeError, ValueError):
        alpha = 128
    begin_sync()
    try:
        if hasattr(material, "bmd"):
            material.bmd.render_mode = "AUTO"
            material.bmd.alpha_ref = max(0, min(255, alpha))
        material.use_backface_culling = (data.get("cull") == 2)
    finally:
        end_sync()


def apply_shader_preset(material, owner, preset):
    """Replace the material's shader from a SuperBMD preset.

    Returns ``(applied_name, created_slots, empty_slots)``.  Textures that the
    target model does not have are appended as empty slots, so a shader can be
    moved between models with different texture sets.  The imported shader
    replaces the previous MAT3 data; no backup is kept for reset.
    """
    from .j3d import mat3_json

    preset_names = [str(name) for name in (preset.get("TextureNames") or [])
                    if name]
    by_name, created, empty = _preset_texture_slots(owner, preset_names)
    images, samplers, _names = _owner_texture_maps(owner)
    data, slots = mat3_json.from_superbmd(preset, by_name)
    textures = owner.bmd_texture_list if owner is not None else ()

    keys = [None] * 8
    for index, name in enumerate(list(data.get("texNames") or [])[:8]):
        if not name:
            continue
        slot_index = slots[index] if index < len(slots) else None
        if slot_index is None or not (0 <= slot_index < len(textures)):
            continue
        slot = textures[slot_index]
        keys[index] = str(slot.key or "") or bc.texture_slot_key(slot) or None
    data["texNames"] = keys

    # The imported shader is now the material's own data.  Drop leftovers of
    # the old "preset shadows the original" mode so no reset backup remains.
    for key in ("bmd_shader_active", "bmd_shader_material",
                "bmd_texture_slots_backup", "bmd_shader_mode_backup",
                "bmd_shader_alpha_backup", "bmd_shader_cull_backup",
                "bmd_shader_created_slots", "bmd_simple",
                "bmd_simple_textures"):
        if key in material:
            del material[key]
    material["bmd_material"] = json.dumps(data, separators=(",", ":"))
    bc.set_material_texture_slots(material, slots)

    _apply_data_settings(material, data)

    try:
        build_import_nodes(material, data, images,
                           _material_scale(material, owner),
                           samplers=samplers)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise RuntimeError(f"nodes could not be rebuilt: {exc}") from exc
    return str(preset.get("Name") or ""), created, empty


def reset_shader(material, owner=None):
    """Compatibility stub: shader imports no longer keep a reset backup."""
    return False
