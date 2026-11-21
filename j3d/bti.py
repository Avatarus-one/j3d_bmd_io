"""Read and write standalone GX texture files (``.bti``)."""

from __future__ import annotations

from . import gx_texture as gx
from .common import J3DError, align

_PALETTE_FORMATS = gx.PALETTE_FORMATS


def parse_bti(data, name="texture"):
    """Parse a standalone ``.bti`` file into a :class:`j3d.model.Texture`."""
    from .model import Texture

    data = bytes(data)

    if len(data) < gx.TEXTURE_HEADER_SIZE:
        raise J3DError("Texture file is too small")

    header = gx.unpack_texture_header(data)
    fmt = header.format
    width, height = header.width, header.height
    mip_count = max(1, header.mip_count)

    if width == 0 or height == 0:
        raise J3DError("Texture has zero dimensions")

    if header.data_offset == 0:
        raise J3DError("Texture file has no image data")

    if fmt not in gx.BLOCK:
        raise J3DError(f"Unsupported texture format: {fmt}")

    rgba, raw, raw_palette = gx.decode_texture_payload(
        data, header, base=0, label="Texture file")

    texture = Texture(name)
    texture.format = fmt
    texture.transparency = header.transparency
    texture.wrap_s = header.wrap_s
    texture.wrap_t = header.wrap_t
    texture.min_filter = header.min_filter
    texture.mag_filter = header.mag_filter
    texture.min_lod = header.min_lod
    texture.max_lod = header.max_lod
    texture.mip_count = mip_count
    texture.lod_bias = header.lod_bias
    texture.flags10 = header.flags10
    texture.rgba = rgba
    texture.raw_data = raw
    texture.raw_palette = raw_palette
    texture.raw_bti = data
    texture.pal_enabled = header.pal_enabled
    texture.pal_format = header.pal_format
    texture.pal_count = header.pal_count
    texture.external = False
    return texture


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
        data_offset = align(0x20 + len(palette), 32)
    else:
        pal_enabled, pal_format, pal_count = 0, 0, 0
        pal_offset = 0
        data_offset = 0x20

    width = int(texture.rgba.shape[1])
    height = int(texture.rgba.shape[0])
    header = gx.TextureHeader(
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
        _flags10(texture),
        int(getattr(texture, "min_filter", 1)),
        int(getattr(texture, "mag_filter", 1)),
        max(-128, min(127, int(getattr(texture, "min_lod", 0)))),
        max(-128, min(127, int(getattr(texture, "max_lod", 0)))),
        mips,
        0,
        max(-32768, min(32767, int(getattr(texture, "lod_bias", 0)))),
        data_offset,
    )
    out = bytearray(gx.pack_texture_header(header))
    if palette:
        out += palette
        out += b"\0" * (data_offset - len(out))
    out += image
    return bytes(out)
