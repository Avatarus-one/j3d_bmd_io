"""SuperBMD material presets (``materials.json``) <-> MAT3 data.

SuperBMD (RenolY2/LagoLunatic forks) dumps the material block of a BMD as a
plain JSON array of ``Material`` objects (Newtonsoft serialisation of
``SuperBMDLib.Materials.Material``) and loads the same shape back with
``--mat``.  The objects are the MAT3 struct itself: typed enums, floats 0..1 for
colours, 4x4/2x3 matrices, and textures referenced **by name** instead of by
TEX1 index.

This module converts between that shape and the dict the add-on keeps in
``material["bmd_material"]`` (the same MAT3 data with the raw file bytes).  No
Blender dependency: the UI layer passes plain dicts.

Enums are written as numbers.  SuperBMD's ``StringEnumConverter`` accepts
integer values (``AllowIntegerValues`` defaults to true) and writes the names
again when it dumps the file, so nothing is lost and the exporter never has to
guess a member name.
"""

from __future__ import annotations

from . import material as mat3

TEXTURE_SLOTS = 8
TEV_STAGES = 16
TEV_ORDERS = 16
TEX_COORDS = 8
TEX_MATRICES = 10
POST_TEX_MATRICES = 20
KONST_COLORS = 4
SWAP_MODES = 16
SWAP_TABLES = 4

# Indirect texturing block, as laid out by SuperBMD (0x138 bytes per material).
IND_HEADER = 4
IND_ORDER = 4
IND_MATRIX = 28            # six floats + exponent + 3 pad bytes
IND_SCALE = 4
IND_STAGE = 12
IND_ORDERS = 4
IND_MATRICES = 3
IND_SCALES = 4
IND_STAGES = 16
IND_SIZE = (IND_HEADER + IND_ORDERS * IND_ORDER
            + IND_MATRICES * IND_MATRIX + IND_SCALES * IND_SCALE
            + IND_STAGES * IND_STAGE)


# --------------------------------------------------------------------------
# small value converters
# --------------------------------------------------------------------------

def _color_out(values):
    """``[r, g, b, a]`` file bytes -> ``{"R", "G", "B", "A"}`` floats."""
    if values is None:
        return None
    r, g, b, a = (int(v) for v in list(values)[:4])
    return {"R": r / 255.0, "G": g / 255.0, "B": b / 255.0, "A": a / 255.0}


def _color_in(value, default=(0, 0, 0, 255)):
    if value is None:
        return list(default)
    r = int(round(float(value.get("R", 0)) * 255))
    g = int(round(float(value.get("G", 0)) * 255))
    b = int(round(float(value.get("B", 0)) * 255))
    a = int(round(float(value.get("A", 1)) * 255))
    return [r, g, b, a]


def _colors_out(values, count):
    values = list(values or [])
    return [_color_out(values[i]) if i < len(values) else None
            for i in range(count)]


def _colors_in(values, count, default=None):
    values = list(values or [])
    result = []
    for i in range(count):
        if i < len(values) and values[i] is not None:
            result.append(_color_in(values[i], default or (0, 0, 0, 255)))
        else:
            result.append(None)
    return result


def _bool(value):
    return bool(int(value)) if value is not None else False


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _rotation_out(raw):
    return _int(raw) * (180.0 / 32768.0)


def _rotation_in(degrees):
    return int(round(_float(degrees) * (32768.0 / 180.0)))


def _matrix_out(values):
    values = [_float(v) for v in list(values or [])][:16]
    values += [0.0] * (16 - len(values))
    return [values[i * 4:i * 4 + 4] for i in range(4)]


def _matrix_in(rows):
    flat = []
    for row in list(rows or [])[:4]:
        flat.extend(_float(v) for v in list(row or [])[:4])
    flat += [0.0] * (16 - len(flat))
    return flat[:16]


# --------------------------------------------------------------------------
# indirect texturing
# --------------------------------------------------------------------------

def _indirect_out(block):
    """Raw 0x138 block -> SuperBMD ``IndTextEntry`` structure."""
    data = bytearray(block or b"")
    if len(data) < IND_SIZE:
        data.extend(b"\0" * (IND_SIZE - len(data)))
    offset = 0
    result = {
        "HasLookup": bool(data[0]),
        "IndTexStageNum": int(data[1]),
    }
    offset = IND_HEADER
    orders = []
    for _ in range(IND_ORDERS):
        orders.append({"TexCoord": int(data[offset]),
                       "TexMap": int(data[offset + 1])})
        offset += IND_ORDER
    result["TevOrders"] = orders

    matrices = []
    for _ in range(IND_MATRICES):
        rows = []
        for row in range(2):
            start = offset + row * 12
            rows.append([_float(v) for v in _unpack_floats(data, start, 3)])
        matrices.append({"Matrix": rows,
                         "Exponent": int(data[offset + 24])})
        offset += IND_MATRIX
    result["Matrices"] = matrices

    scales = []
    for _ in range(IND_SCALES):
        scales.append({"ScaleS": int(data[offset]),
                       "ScaleT": int(data[offset + 1])})
        offset += IND_SCALE
    result["Scales"] = scales

    stages = []
    for _ in range(IND_STAGES):
        stages.append({
            "TevStage": int(data[offset]),
            "IndTexFormat": int(data[offset + 1]),
            "IndTexBiasSel": int(data[offset + 2]),
            "IndTexMtxId": int(data[offset + 3]),
            "IndTexWrapS": int(data[offset + 4]),
            "IndTexWrapT": int(data[offset + 5]),
            "AddPrev": bool(data[offset + 6]),
            "UtcLod": bool(data[offset + 7]),
            "AlphaSel": int(data[offset + 8]),
        })
        offset += IND_STAGE
    result["TevStages"] = stages
    return result


def _indirect_in(entry):
    """SuperBMD ``IndTextEntry`` structure -> raw 0x138 block."""
    import struct

    entry = entry or {}
    data = bytearray(IND_SIZE)
    data[0] = 1 if entry.get("HasLookup") else 0
    data[1] = _int(entry.get("IndTexStageNum")) & 0xFF
    data[2:4] = b"\xff\xff"

    offset = IND_HEADER
    for order in list(entry.get("TevOrders") or [])[:IND_ORDERS]:
        order = order or {}
        data[offset] = _int(order.get("TexCoord"), 0xFF) & 0xFF
        data[offset + 1] = _int(order.get("TexMap"), 0xFF) & 0xFF
        data[offset + 2:offset + 4] = b"\xff\xff"
        offset += IND_ORDER
    for _ in range(max(0, IND_ORDERS - len(list(entry.get("TevOrders") or [])))):
        data[offset:offset + 4] = b"\xff\xff\xff\xff"
        offset += IND_ORDER

    matrices = list(entry.get("Matrices") or [])
    for index in range(IND_MATRICES):
        matrix = matrices[index] if index < len(matrices) else {}
        matrix = matrix or {}
        rows = list(matrix.get("Matrix") or [])
        values = []
        for row in range(2):
            row_values = list(rows[row]) if row < len(rows) else []
            row_values = [_float(v) for v in row_values][:3]
            row_values += [0.0] * (3 - len(row_values))
            values.extend(row_values)
        data[offset:offset + 24] = struct.pack(">6f", *values)
        data[offset + 24] = _int(matrix.get("Exponent"), 1) & 0xFF
        data[offset + 25:offset + 28] = b"\xff\xff\xff"
        offset += IND_MATRIX

    scales = list(entry.get("Scales") or [])
    for index in range(IND_SCALES):
        scale = (scales[index] if index < len(scales) else {}) or {}
        data[offset] = _int(scale.get("ScaleS"), 0) & 0xFF
        data[offset + 1] = _int(scale.get("ScaleT"), 0) & 0xFF
        data[offset + 2:offset + 4] = b"\xff\xff"
        offset += IND_SCALE

    stages = list(entry.get("TevStages") or [])
    for index in range(IND_STAGES):
        stage = (stages[index] if index < len(stages) else {}) or {}
        data[offset] = _int(stage.get("TevStage"), index) & 0xFF
        data[offset + 1] = _int(stage.get("IndTexFormat"), 0) & 0xFF
        data[offset + 2] = _int(stage.get("IndTexBiasSel"), 0) & 0xFF
        data[offset + 3] = _int(stage.get("IndTexMtxId"), 0) & 0xFF
        data[offset + 4] = _int(stage.get("IndTexWrapS"), 0) & 0xFF
        data[offset + 5] = _int(stage.get("IndTexWrapT"), 0) & 0xFF
        data[offset + 6] = 1 if stage.get("AddPrev") else 0
        data[offset + 7] = 1 if stage.get("UtcLod") else 0
        data[offset + 8] = _int(stage.get("AlphaSel"), 0) & 0xFF
        data[offset + 9:offset + 12] = b"\xff\xff\xff"
        offset += IND_STAGE

    return bytes(data)


def _unpack_floats(data, offset, count):
    import struct

    return struct.unpack_from(">%df" % count, data, offset)


# --------------------------------------------------------------------------
# MAT3 -> SuperBMD JSON
# --------------------------------------------------------------------------

def to_superbmd(data, name=None, texture_names=None):
    """One ``bmd_material`` dict -> a SuperBMD material JSON object.

    The result is plain SuperBMD data: the add-on writes no extra preview keys,
    so the file can be used directly with SuperBMD and re-imported without
    restoring any preview-only state.
    """
    data = data or {}

    tex_names = list(texture_names or data.get("texNames") or [])
    tex_names = [tex_names[i] if i < len(tex_names) else None
                 for i in range(TEXTURE_SLOTS)]

    coords = list(data.get("texCoord") or [])
    coords2 = list(data.get("texCoord2") or [])
    matrices = list(data.get("texMtx") or [])
    post_matrices = list(data.get("postTexMtx") or [])
    orders = list(data.get("tevOrder") or [])
    stages = list(data.get("tevStage") or [])
    swap_modes = list(data.get("tevSwapMode") or [])
    swap_tables = list(data.get("tevSwapTable") or [])
    color_chans = list(data.get("colorChan") or [])
    k_color_sel = [_int(v) for v in (data.get("kColorSel") or [])]
    k_alpha_sel = [_int(v) for v in (data.get("kAlphaSel") or [])]
    alpha_comp = [_int(v) for v in (data.get("alphaComp") or [])][:8]
    blend = [_int(v) for v in (data.get("blend") or [])][:4]
    z_mode = [_int(v) for v in (data.get("zMode") or [])][:4]
    fog = data.get("fog") or {}
    nbt = data.get("nbtScale") or {}

    indirect = data.get("indirect")
    if isinstance(indirect, str):
        indirect = bytes.fromhex(indirect)
    elif indirect is None:
        indirect = mat3.default_indirect()

    def tex_coord(entry):
        if not entry:
            return None
        entry = list(entry)
        return {"Type": _int(entry[0]),
                "Source": _int(entry[1]) if len(entry) > 1 else 0,
                "TexMatrixSource": _int(entry[2]) if len(entry) > 2 else 0}

    def tex_matrix(entry):
        if not entry:
            return None
        return {
            "Projection": _int(entry.get("projection")),
            "Type": _int(entry.get("type")),
            "EffectTranslation": [_float(v) for v in (entry.get("center") or [0, 0, 0])],
            "Scale": [_float(v) for v in (entry.get("scale") or [1, 1])],
            "Rotation": _rotation_out(entry.get("rotation")),
            "Translation": [_float(v) for v in (entry.get("translation") or [0, 0])],
            "ProjectionMatrix": _matrix_out(entry.get("matrix")),
        }

    def tev_order(entry):
        if not entry:
            return None
        entry = list(entry)
        return {"TexCoord": _int(entry[0]),
                "TexMap": _int(entry[1]) if len(entry) > 1 else 0,
                "ChannelId": _int(entry[2]) if len(entry) > 2 else 0}

    def tev_stage(entry):
        if not entry:
            return None
        color_in = [_int(v) for v in (entry.get("colorIn") or [0, 0, 0, 0])]
        alpha_in = [_int(v) for v in (entry.get("alphaIn") or [0, 0, 0, 0])]
        color_in += [0] * (4 - len(color_in))
        alpha_in += [0] * (4 - len(alpha_in))
        return {
            "ColorInA": color_in[0], "ColorInB": color_in[1],
            "ColorInC": color_in[2], "ColorInD": color_in[3],
            "ColorOp": _int(entry.get("colorOp")),
            "ColorBias": _int(entry.get("colorBias")),
            "ColorScale": _int(entry.get("colorScale")),
            "ColorClamp": _bool(entry.get("colorClamp")),
            "ColorRegId": _int(entry.get("colorReg")),
            "AlphaInA": alpha_in[0], "AlphaInB": alpha_in[1],
            "AlphaInC": alpha_in[2], "AlphaInD": alpha_in[3],
            "AlphaOp": _int(entry.get("alphaOp")),
            "AlphaBias": _int(entry.get("alphaBias")),
            "AlphaScale": _int(entry.get("alphaScale")),
            "AlphaClamp": _bool(entry.get("alphaClamp")),
            "AlphaRegId": _int(entry.get("alphaReg")),
        }

    def channel(entry):
        if not entry:
            return None
        entry = [_int(v) for v in entry]
        entry += [0] * (8 - len(entry))
        return {"Enable": bool(entry[0]),
                "MaterialSrcColor": entry[1],
                "LitMask": entry[2],
                "DiffuseFunction": entry[3],
                "AttenuationFunction": entry[4],
                "AmbientSrcColor": entry[5]}

    def swap_mode(entry):
        if not entry:
            return None
        entry = [_int(v) for v in entry]
        entry += [0] * (2 - len(entry))
        return {"RasSel": entry[0], "TexSel": entry[1]}

    def swap_table(entry):
        if not entry:
            return None
        entry = [_int(v) for v in entry]
        entry += [0] * (4 - len(entry))
        return {"R": entry[0], "G": entry[1], "B": entry[2], "A": entry[3]}

    result = {
        "Name": str(name or data.get("name") or "material"),
        "Flag": _int(data.get("flags")),
        "CullMode": _int(data.get("cull")),
        "ZCompLoc": _bool(data.get("zCompLoc")),
        "Dither": _bool(data.get("dither")),
        "TextureNames": [None if value in (None, "") else str(value)
                         for value in tex_names],
        "IndTexEntry": _indirect_out(indirect),
        "MaterialColors": _colors_out(data.get("matColor"), 2),
        "ChannelControls": [channel(color_chans[i]) if i < len(color_chans) else None
                            for i in range(4)],
        "AmbientColors": _colors_out(data.get("ambColor"), 2),
        "LightingColors": _colors_out(data.get("light"), 8),
        "TexCoord1Gens": [tex_coord(coords[i]) if i < len(coords) else None
                          for i in range(TEX_COORDS)],
        "PostTexCoordGens": [tex_coord(coords2[i]) if i < len(coords2) else None
                             for i in range(TEX_COORDS)],
        "TexMatrix1": [tex_matrix(matrices[i]) if i < len(matrices) else None
                       for i in range(TEX_MATRICES)],
        "PostTexMatrix": [tex_matrix(post_matrices[i]) if i < len(post_matrices) else None
                          for i in range(POST_TEX_MATRICES)],
        "TevOrders": [tev_order(orders[i]) if i < len(orders) else None
                      for i in range(TEV_ORDERS)],
        "ColorSels": [_int(k_color_sel[i]) if i < len(k_color_sel) else 0
                      for i in range(TEV_STAGES)],
        "AlphaSels": [_int(k_alpha_sel[i]) if i < len(k_alpha_sel) else 0
                      for i in range(TEV_STAGES)],
        "TevColors": _colors_out(data.get("tevColor"), 4),
        "KonstColors": _colors_out(data.get("kColor"), KONST_COLORS),
        "TevStages": [tev_stage(stages[i]) if i < len(stages) else None
                      for i in range(TEV_STAGES)],
        "SwapModes": [swap_mode(swap_modes[i]) if i < len(swap_modes) else None
                      for i in range(SWAP_MODES)],
        "SwapTables": [swap_table(swap_tables[i]) if i < len(swap_tables) else None
                       for i in range(SWAP_TABLES)],
        "FogInfo": {
            "Type": _int(fog.get("type")),
            "Enable": _bool(fog.get("enable")),
            "Center": _int(fog.get("center")),
            "StartZ": _float(fog.get("startZ")),
            "EndZ": _float(fog.get("endZ")),
            "NearZ": _float(fog.get("nearZ")),
            "FarZ": _float(fog.get("farZ")),
            "Color": _color_out(fog.get("color") or [0, 0, 0, 0]),
            "RangeAdjustmentTable": [_float(v) / 256.0
                                     for v in (fog.get("adjTable") or [0] * 10)][:10],
        },
        "AlphCompare": {
            "Comp0": alpha_comp[0] if len(alpha_comp) > 0 else 7,
            "Reference0": alpha_comp[1] if len(alpha_comp) > 1 else 0,
            "Operation": alpha_comp[2] if len(alpha_comp) > 2 else 1,
            "Comp1": alpha_comp[3] if len(alpha_comp) > 3 else 7,
            "Reference1": alpha_comp[4] if len(alpha_comp) > 4 else 0,
        },
        "BMode": {
            "Type": blend[0] if len(blend) > 0 else 0,
            "SourceFact": blend[1] if len(blend) > 1 else 1,
            "DestinationFact": blend[2] if len(blend) > 2 else 0,
            "Operation": blend[3] if len(blend) > 3 else 3,
        },
        "ZMode": {
            "Enable": bool(z_mode[0]) if len(z_mode) > 0 else True,
            "Function": z_mode[1] if len(z_mode) > 1 else 3,
            "UpdateEnable": bool(z_mode[2]) if len(z_mode) > 2 else True,
        },
        "NBTScale": {
            "Unknown1": _int(nbt.get("enable")),
            "Scale": [_float(v) for v in (nbt.get("scale") or [1, 1, 1])][:3],
        },
    }
    return result


# --------------------------------------------------------------------------
# SuperBMD JSON -> MAT3
# --------------------------------------------------------------------------

def from_superbmd(obj, texture_indices=None):
    """A SuperBMD material JSON object -> ``(bmd_material, texture_indices)``.

    *texture_indices* maps the preset's ``TextureNames`` entries to TEX1
    indices of the target model; names that are not in the map stay ``None``.
    The second return value is the per-slot TEX1 index list for the material's
    texture slots.
    """
    obj = obj or {}
    tex_names = list(obj.get("TextureNames") or [])
    tex_names = [tex_names[i] if i < len(tex_names) else None
                 for i in range(TEXTURE_SLOTS)]
    resolved = []
    for i, name in enumerate(tex_names):
        index = None
        if name not in (None, "") and texture_indices:
            index = texture_indices.get(str(name))
        resolved.append(index)

    def tex_coord(entry):
        if not entry:
            return None
        return [_int(entry.get("Type")),
                _int(entry.get("Source")),
                _int(entry.get("TexMatrixSource")),
                0xFF]

    def tex_matrix(entry):
        if not entry:
            return None
        return {
            "projection": _int(entry.get("Projection")),
            "type": _int(entry.get("Type")),
            "center": [_float(v) for v in (entry.get("EffectTranslation") or [0, 0, 0])][:3],
            "scale": [_float(v) for v in (entry.get("Scale") or [1, 1])][:2],
            "rotation": _rotation_in(entry.get("Rotation")),
            "translation": [_float(v) for v in (entry.get("Translation") or [0, 0])][:2],
            "matrix": _matrix_in(entry.get("ProjectionMatrix")),
        }

    def tev_order(entry):
        if not entry:
            return None
        return [_int(entry.get("TexCoord")), _int(entry.get("TexMap")),
                _int(entry.get("ChannelId")), 0xFF]

    def tev_stage(entry):
        if not entry:
            return None
        return {
            "u0": 0xFF,
            "colorIn": [_int(entry.get("ColorInA")), _int(entry.get("ColorInB")),
                        _int(entry.get("ColorInC")), _int(entry.get("ColorInD"))],
            "colorOp": _int(entry.get("ColorOp")),
            "colorBias": _int(entry.get("ColorBias")),
            "colorScale": _int(entry.get("ColorScale")),
            "colorClamp": 1 if entry.get("ColorClamp") else 0,
            "colorReg": _int(entry.get("ColorRegId")),
            "alphaIn": [_int(entry.get("AlphaInA")), _int(entry.get("AlphaInB")),
                        _int(entry.get("AlphaInC")), _int(entry.get("AlphaInD"))],
            "alphaOp": _int(entry.get("AlphaOp")),
            "alphaBias": _int(entry.get("AlphaBias")),
            "alphaScale": _int(entry.get("AlphaScale")),
            "alphaClamp": 1 if entry.get("AlphaClamp") else 0,
            "alphaReg": _int(entry.get("AlphaRegId")),
            "u19": 0xFF,
        }

    def channel(entry):
        if not entry:
            return None
        return [1 if entry.get("Enable") else 0,
                _int(entry.get("MaterialSrcColor")),
                _int(entry.get("LitMask")),
                _int(entry.get("DiffuseFunction")),
                _int(entry.get("AttenuationFunction")),
                _int(entry.get("AmbientSrcColor")),
                0xFF, 0xFF]

    def swap_mode(entry):
        if not entry:
            return None
        return [_int(entry.get("RasSel")), _int(entry.get("TexSel")), 0xFF, 0xFF]

    def swap_table(entry):
        if not entry:
            return None
        return [_int(entry.get("R")), _int(entry.get("G")),
                _int(entry.get("B")), _int(entry.get("A"))]

    coords = list(obj.get("TexCoord1Gens") or [])
    coords2 = list(obj.get("PostTexCoordGens") or [])
    matrices = list(obj.get("TexMatrix1") or [])
    post_matrices = list(obj.get("PostTexMatrix") or [])
    orders = list(obj.get("TevOrders") or [])
    stages = list(obj.get("TevStages") or [])
    channels = list(obj.get("ChannelControls") or [])
    swap_modes = list(obj.get("SwapModes") or [])
    swap_tables = list(obj.get("SwapTables") or [])
    color_sels = list(obj.get("ColorSels") or [])
    alpha_sels = list(obj.get("AlphaSels") or [])
    fog = obj.get("FogInfo") or {}
    alpha_comp = obj.get("AlphCompare") or {}
    blend = obj.get("BMode") or {}
    z_mode = obj.get("ZMode") or {}
    nbt = obj.get("NBTScale") or {}

    result = {
        "flags": _int(obj.get("Flag")),
        "cull": _int(obj.get("CullMode")),
        "colorChanCount": 1,
        "matColor": _colors_in(obj.get("MaterialColors"), 2),
        "colorChan": [channel(channels[i]) if i < len(channels) else None
                      for i in range(4)],
        "ambColor": _colors_in(obj.get("AmbientColors"), 2),
        "light": _colors_in(obj.get("LightingColors"), 8),
        "texGenCount": sum(1 for entry in coords[:TEX_COORDS] if entry),
        "texCoord": [tex_coord(coords[i]) if i < len(coords) else None
                     for i in range(TEX_COORDS)],
        "texCoord2": [tex_coord(coords2[i]) if i < len(coords2) else None
                      for i in range(TEX_COORDS)],
        "texMtx": [tex_matrix(matrices[i]) if i < len(matrices) else None
                   for i in range(TEX_MATRICES)],
        "postTexMtx": [tex_matrix(post_matrices[i]) if i < len(post_matrices) else None
                       for i in range(POST_TEX_MATRICES)],
        "texNames": [None if name in (None, "") else str(name)
                     for name in tex_names],
        "kColor": _colors_in(obj.get("KonstColors"), KONST_COLORS),
        "kColorSel": [_int(color_sels[i]) if i < len(color_sels) else 0
                      for i in range(TEV_STAGES)],
        "kAlphaSel": [_int(alpha_sels[i]) if i < len(alpha_sels) else 0
                      for i in range(TEV_STAGES)],
        "tevOrder": [tev_order(orders[i]) if i < len(orders) else None
                     for i in range(TEV_ORDERS)],
        "tevColor": _colors_in(obj.get("TevColors"), 4),
        "tevStageCount": sum(1 for entry in stages[:TEV_STAGES] if entry),
        "tevStage": [tev_stage(stages[i]) if i < len(stages) else None
                     for i in range(TEV_STAGES)],
        "tevSwapMode": [swap_mode(swap_modes[i]) if i < len(swap_modes) else None
                        for i in range(SWAP_MODES)],
        "tevSwapTable": [swap_table(swap_tables[i]) if i < len(swap_tables) else None
                         for i in range(SWAP_TABLES)],
        "fog": {
            "type": _int(fog.get("Type")),
            "enable": 1 if fog.get("Enable") else 0,
            "center": _int(fog.get("Center")),
            "startZ": _float(fog.get("StartZ")),
            "endZ": _float(fog.get("EndZ")),
            "nearZ": _float(fog.get("NearZ")),
            "farZ": _float(fog.get("FarZ")),
            "color": _color_in(fog.get("Color"), (0, 0, 0, 0)),
            "adjTable": [int(round(_float(v) * 256)) & 0xFFFF
                         for v in (fog.get("RangeAdjustmentTable") or [0] * 10)][:10],
        },
        "unknown12": [0xFFFF] * 12,
        "dither": 1 if obj.get("Dither") else 0,
        "alphaComp": [
            _int(alpha_comp.get("Comp0"), 7),
            _int(alpha_comp.get("Reference0")),
            _int(alpha_comp.get("Operation"), 1),
            _int(alpha_comp.get("Comp1"), 7),
            _int(alpha_comp.get("Reference1")),
            0xFF, 0xFF, 0xFF,
        ],
        "blend": [_int(blend.get("Type")), _int(blend.get("SourceFact"), 1),
                  _int(blend.get("DestinationFact")), _int(blend.get("Operation"), 3)],
        "zMode": [1 if z_mode.get("Enable", True) else 0,
                  _int(z_mode.get("Function"), 3),
                  1 if z_mode.get("UpdateEnable", True) else 0,
                  0xFF],
        "zCompLoc": 1 if obj.get("ZCompLoc") else 0,
        "nbtScale": {
            "enable": _int(nbt.get("Unknown1")),
            "scale": [_float(v) for v in (nbt.get("Scale") or [1, 1, 1])][:3],
        },
    }
    indirect = obj.get("IndTexEntry")
    result["indirect"] = (_indirect_in(indirect).hex() if indirect else None)
    return result, resolved
