"""J3D blend-shape animations (``.blk``, chunk ``CLK1``): parsing and writing.

No Blender dependency.  Values are BLS key weights; tangents are value units per
frame and use the same Hermite convention as BCK tracks.
"""

from __future__ import annotations

import struct

from .anim import Track
from .common import J3DError, align, j3d_file_header, u16, u32

BLK_MAGIC = b"J3D1blk1"
BLK_CHUNK = b"CLK1"
BLK_PADDING = b"This is padding data to align"


class Blk:
    def __init__(self):
        self.loop_mode = 2
        self.duration = 0
        self.tracks = []
        self.tan_type = 0


def _padding(length):
    if length <= 0:
        return b""
    repeats = (length + len(BLK_PADDING) - 1) // len(BLK_PADDING)
    return (BLK_PADDING * repeats)[:length]


def parse_blk(data):
    """Parse a complete ``J3D1blk1`` file into a :class:`Blk`."""
    data = bytes(data)

    if data[:8] != BLK_MAGIC:
        raise J3DError("Not a J3D blk1 animation file")

    chunks = u32(data, 12)
    off = 32
    clk = None

    for _ in range(chunks):
        tag = data[off:off + 4]
        size = u32(data, off + 4)

        if tag == BLK_CHUNK:
            clk = off

        off += size

    if clk is None:
        raise J3DError("Missing CLK1 chunk")

    loop = data[clk + 8]
    duration = u16(data, clk + 0x0A)
    scales_count = u16(data, clk + 0x0E)
    cluster_off = u32(data, clk + 0x10)
    scales_off = u32(data, clk + 0x14)

    scales = list(struct.unpack_from(
        ">" + str(scales_count) + "f", data, clk + scales_off))

    tracks = []
    pos = clk + cluster_off

    while pos + 6 <= len(data) and data[pos:pos + 2] != b"Th":
        count = u16(data, pos)
        offset = u16(data, pos + 2)
        tan_type = u16(data, pos + 4)
        pos += 6

        if count == 0:
            keys = [(0.0, 0.0, 0.0, 0.0)]
        elif count == 1:
            keys = [(0.0, float(scales[offset]), 0.0, 0.0)]
        elif tan_type == 0:
            keys = []
            for index in range(count):
                base = offset + index * 3
                time, value, tangent = scales[base:base + 3]
                keys.append((float(time), float(value),
                             float(tangent), float(tangent)))
        elif tan_type == 1:
            keys = []
            for index in range(count):
                base = offset + index * 4
                time, value, tin, tout = scales[base:base + 4]
                keys.append((float(time), float(value),
                             float(tin), float(tout)))
        else:
            raise J3DError(f"Unknown BLK tangent type {tan_type}")

        tracks.append(Track(keys))

    animation = Blk()
    animation.loop_mode = loop
    animation.duration = duration
    animation.tracks = tracks
    return animation


def _find_sequence(scales, sequence):
    """Return the offset of *sequence* in *scales*, or ``-1``."""
    n = len(sequence)

    if n == 0:
        return -1

    for start in range(len(scales) - n + 1):
        if scales[start:start + n] == sequence:
            return start

    return -1


def write_blk(animation):
    """Serialise *animation* into a complete ``J3D1blk1`` file (bytes)."""
    scales = []
    entries = []

    for track in animation.tracks:
        keys = track.keys

        if len(keys) == 1:
            sequence = [float(keys[0][1])]
        else:
            sequence = []
            for time, value, tin, _tout in keys:
                sequence.extend([float(time), float(value), float(tin)])

        offset = _find_sequence(scales, sequence)

        if offset < 0:
            offset = len(scales)
            scales.extend(sequence)

        entries.append((len(keys), offset, 0))

    cluster_off = 0x20
    entries_size = 6 * len(entries)
    scales_off = align(cluster_off + entries_size, 32)
    scales_size = 4 * len(scales)
    total_size = align(scales_off + scales_size, 32)

    chunk = bytearray(b"CLK1")
    chunk += struct.pack(
        ">IBbHHHII", 0, animation.loop_mode, 0, animation.duration,
        1, len(scales), cluster_off, scales_off)
    chunk += _padding(cluster_off - len(chunk))

    for count, offset, tan_type in entries:
        chunk += struct.pack(">HHH", count, offset, tan_type)

    chunk += _padding(scales_off - len(chunk))

    for value in scales:
        chunk += struct.pack(">f", value)

    chunk += _padding(total_size - len(chunk))
    struct.pack_into(">I", chunk, 4, len(chunk))
    return j3d_file_header(BLK_MAGIC, len(chunk)) + bytes(chunk)
