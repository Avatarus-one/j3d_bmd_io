"""Write standalone GX texture files (``.bti``)."""

from __future__ import annotations

import struct

from . import gx_texture as gx
from .common import J3DError

_PALETTE_FORMATS = (gx.C4, gx.C8, gx.C14X2)


def _align(value, alignment=32):
    return (value + alignment - 1) // alignment * alignment


def _flags10(texture):
    flags = bytes(getattr(texture, "flags10", b"") or b"")
    if len(flags) < 4:
        flags = flags.ljust(4, b"\0")
    return flags[:4]


def build_bti(texture):
    """Return the bytes of a ``.bti`` file for *texture*.

    The texture format, mip count and max LOD are updated when the pixels are
    re-encoded, so the matching BMD ``TEX1`` header stays in sync.
    """
    raw_bti = getattr(texture, "raw_bti", None)
    if raw_bti is not None:
        return raw_bti
    if texture.rgba is None:
        raise J3DError(f"Texture '{texture.name}' has no pixel data")

    fmt = int(texture.format)
    palette = getattr(texture, "raw_palette", None)
    if texture.raw_data is not None:
        image = texture.raw_data
        mips = max(1, int(texture.mip_count))
    else:
        if fmt not in gx.BLOCK or fmt in _PALETTE_FORMATS:
            # Palette formats cannot be encoded from RGBA without quantising;
            # fall back to a format that can.
            fmt = gx.RGB5A3 if gx.has_partial_alpha(texture.rgba) else gx.CMPR
            texture.format = fmt
        image, mips = gx.encode_with_mips(fmt, texture.rgba, texture.mip_count)
        texture.mip_count = mips
        texture.max_lod = (mips - 1) * 8
        palette = None

    if palette:
        pal_enabled = int(texture.pal_enabled)
        pal_format = int(texture.pal_format)
        pal_count = int(texture.pal_count)
        pal_offset = 0x20
        data_offset = _align(0x20 + len(palette), 32)
    else:
        pal_enabled, pal_format, pal_count = 0, 0, 0
        pal_offset = 0
        data_offset = 0x20

    width = int(texture.rgba.shape[1])
    height = int(texture.rgba.shape[0])
    header = struct.pack(
        ">BBHHBBBBHI4BBBbbBBhI",
        fmt,
        int(getattr(texture, "transparency", 0)),
        width,
        height,
        int(getattr(texture, "wrap_s", 1)),
        int(getattr(texture, "wrap_t", 1)),
        pal_enabled,
        pal_format,
        pal_count,
        pal_offset,
        *_flags10(texture),
        int(getattr(texture, "min_filter", 1)),
        int(getattr(texture, "mag_filter", 1)),
        max(-128, min(127, int(getattr(texture, "min_lod", 0)))),
        max(-128, min(127, int(getattr(texture, "max_lod", 0)))),
        mips,
        0,
        max(-32768, min(32767, int(getattr(texture, "lod_bias", 0)))),
        data_offset,
    )
    out = bytearray(header)
    if palette:
        out += palette
        out += b"\0" * (data_offset - len(out))
    out += image
    return bytes(out)
