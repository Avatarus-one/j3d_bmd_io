"""J3D texture-pattern animations (``.btp``, chunk ``TPT1``).

A BTP entry names a material and stores one texture index per frame.  The
index is the global TEX1 texture index of the model, so a clip swaps whole
textures (eyes, facial expressions, ...), not palette entries.
"""

from __future__ import annotations

import struct

from . import anim
from .common import (
    J3DError,
    j3d_file_header,
    pad_to,
    read_string_table,
    u16,
    u32,
    write_string_table,
)


class BtpEntry:
    def __init__(self, name="", frames=None):
        self.name = name
        self.frames = list(frames or [])


class Btp:
    def __init__(self):
        self.loop_mode = anim.LOOP_LOOP
        self.length = 0
        self.unknown = 0
        self.entries = []


def parse_btp(data):
    """Parse a ``.btp`` file."""
    data = bytes(data)
    if data[:3] != b"J3D" or data[4:8] != b"btp1":
        raise J3DError("Not a J3D btp1 animation file")

    count = u32(data, 12)
    off = 32
    chunk = None
    for _ in range(count):
        if off + 8 > len(data):
            raise J3DError("Truncated BTP file")
        tag = data[off:off + 4]
        size = u32(data, off + 4)
        if size < 8:
            raise J3DError("Invalid BTP chunk size")
        if tag in (b"TPT1", b"TTP1"):
            chunk = off
        off += size
    if chunk is None:
        raise J3DError("Missing TPT1 chunk")

    loop_mode = data[chunk + 8]
    length = u16(data, chunk + 0x0A)
    entry_count = u16(data, chunk + 0x0C)
    unknown = u16(data, chunk + 0x0E)
    entries_off = u32(data, chunk + 0x10) + chunk
    bank_off = u32(data, chunk + 0x14) + chunk
    names_off = u32(data, chunk + 0x1C) + chunk
    names = (read_string_table(data, names_off) if names_off else
             [f"material{i}" for i in range(entry_count)])

    btp = Btp()
    btp.loop_mode = loop_mode
    btp.length = length
    btp.unknown = unknown
    for i in range(entry_count):
        e = entries_off + 8 * i
        if e + 8 > len(data):
            raise J3DError("Truncated BTP entry table")
        frame_count, first = struct.unpack_from(">HH", data, e)
        frames = [u16(data, bank_off + 2 * (first + j))
                  for j in range(frame_count)]
        btp.entries.append(BtpEntry(
            names[i] if i < len(names) else f"material{i}", frames))
    return btp


def write_btp(btp):
    """Serialise a :class:`Btp` to ``.btp`` bytes."""
    entries = list(btp.entries)
    names = [entry.name for entry in entries]
    frame_counts = [len(entry.frames) for entry in entries]
    max_frames = max(frame_counts) if frame_counts else 0
    total_frames = sum(frame_counts)
    length = int(btp.length) if btp.length else max_frames
    unknown = int(btp.unknown) if btp.unknown else total_frames

    body = bytearray(b"TPT1\0\0\0\0")
    body += struct.pack(">BBHHH", int(btp.loop_mode) & 0xFF, 0xFF,
                        length, len(entries), unknown)
    # entry table, bank, remap, names
    body += b"\0" * 16
    entries_off = len(body)
    body += b"\0" * (8 * len(entries))
    pad_to(body, 4)
    bank_off = len(body)
    for entry in entries:
        for index in entry.frames:
            body += struct.pack(">H", int(index) & 0xFFFF)
    pad_to(body, 4)
    remap_off = len(body)
    body += struct.pack(">I", 1)
    names_off = len(body)
    body += write_string_table(names)
    pad_to(body, 32)

    struct.pack_into(">I", body, 4, len(body))
    struct.pack_into(">4I", body, 0x10,
                     entries_off, bank_off, remap_off, names_off)

    first = 0
    for i, entry in enumerate(entries):
        e = entries_off + 8 * i
        struct.pack_into(">HHHH", body, e, len(entry.frames), first,
                         0x00FF, 0xFFFF)
        first += len(entry.frames)

    return j3d_file_header(b"J3D1btp1", len(body)) + bytes(body)


def btp_duration(btp):
    """Number of played frames (the longest entry)."""
    return max((len(entry.frames) for entry in btp.entries), default=0)


def frame_end(btp):
    """Last played frame from the explicit TPT1 ``length`` field.

    ONCE-style files store the last frame index there, looping files store the
    loop length (number of frames).  Falls back to the longest entry when the
    field is missing.
    """
    length = int(getattr(btp, "length", 0) or 0)
    if length <= 0:
        return max((len(entry.frames) for entry in btp.entries),
                   default=1) - 1
    if int(btp.loop_mode) in (anim.LOOP_LOOP, anim.LOOP_MIRROR_LOOP):
        return length - 1
    return length


def header_length(frame_end, loop_mode):
    """TPT1 ``length`` value for *frame_end* (the inverse of :func:`frame_end`).

    *loop_mode* may be the integer mode or its name.
    """
    mode = loop_mode
    if isinstance(mode, str):
        try:
            mode = anim.LOOP_NAMES.index(mode)
        except ValueError:
            mode = anim.LOOP_ONCE
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        mode = anim.LOOP_ONCE
    if mode in (anim.LOOP_LOOP, anim.LOOP_MIRROR_LOOP):
        return int(frame_end) + 1
    return int(frame_end)
