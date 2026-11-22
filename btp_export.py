"""Write BTP texture-pattern animations from registry tracks."""

from __future__ import annotations

import os

from .j3d import anim
from .j3d import btp as btp_data


def frames_from_action(action):
    if action is None:
        return []
    for path in ("bmd.btp_texture_index", '["bmd_btp_index"]'):
        fcurve = next((curve for curve in action.fcurves
                       if curve.data_path == path), None)
        if fcurve is not None:
            if not len(fcurve.keyframe_points):
                return []
            points = sorted(fcurve.keyframe_points,
                            key=lambda point: point.co[0])
            last = max(point.co[0] for point in points)
            frames = []
            index = 0
            value = points[0].co[1]
            for frame in range(int(last) + 1):
                while index < len(points) and points[index].co[0] <= frame:
                    value = points[index].co[1]
                    index += 1
                frames.append(int(round(value)))
            desired = max(
                len(frames),
                int(action.get("bmd_duration", 0) or 0) + 1)
            if desired > len(frames):
                frames.extend([frames[-1]] * (desired - len(frames)))
            return frames
    # No BTP curve means no keys (for example after deleting all of them).
    return []


def build_btp(entries, loop_mode="ONCE", length=None, unknown=None):
    """Build a :class:`j3d.btp.Btp` from ``[(name, frames)]``."""
    animation = btp_data.Btp()
    try:
        animation.loop_mode = anim.LOOP_NAMES.index(loop_mode)
    except ValueError:
        animation.loop_mode = anim.LOOP_ONCE
    for name, frames in entries:
        animation.entries.append(btp_data.BtpEntry(name, frames))
    longest = max((len(frames) for _name, frames in entries), default=0)
    total = sum(len(frames) for _name, frames in entries)
    if length is None:
        length = btp_data.header_length(max(0, longest - 1),
                                        animation.loop_mode)
    animation.length = int(length)
    animation.unknown = int(unknown) if unknown is not None else total
    return animation


def header_length(frame_end, loop_mode="ONCE"):
    """TPT1 ``length`` for a clip range (inverse of the import helper)."""
    return btp_data.header_length(frame_end, loop_mode)


def export_btp(filepath, entries, loop_mode="ONCE", report=None,
               length=None, unknown=None):
    """Write one BTP file.  Returns the number of material entries."""
    clean = []
    for name, frames in entries:
        if frames:
            clean.append((str(name), [int(v) & 0xFFFF for v in frames]))
    if not clean:
        raise ValueError("A BTP file needs at least one material track")
    animation = build_btp(clean, loop_mode, length, unknown)
    data = btp_data.write_btp(animation)
    with open(filepath, "wb") as file:
        file.write(data)
    if report:
        report({"INFO"},
               f"Wrote {len(clean)} material track(s) to "
               f"{os.path.basename(filepath)}")
    return len(clean)
