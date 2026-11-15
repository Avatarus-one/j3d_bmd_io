# J3D BMD / BCK / BLS / BLK Import & Export for Blender (Zelda: Twilight Princess / Dusklight)

Imports and exports Nintendo J3D models (`.bmd`, `.bdl` read-only), joint animations (`.bck`),
collision (`.kcl`), shape keys (`.bls`) and shape-key animations (`.blk`).

## AI disclosure and disclaimer
The source code of this add-on was written with the help of an AI model (Claude by Anthropic), working
from the requirements, test files and feedback of me. The maintainer directed the
project and tested the results in-game, but the code has not been independently audited.

The add-on is provided "as is", without warranty of any kind (see the GPL-3.0-or-later license).
It reads and writes file formats that were analysed from publicly available information and the
open-source Dusklight code. Always keep backups of your original game files; exported files can
still crash the game or behave unexpectedly, especially collision (`.kcl`) edits.

This is an unofficial fan tool. It is not affiliated with or endorsed by Nintendo, Anthropic, or the
Dusklight / Twilit Realm project. No game assets are included.

## Installation (Blender 4.2 - 5.x)
Download this code as ZIP.

In Blender:

Edit -> Preferences -> Get Extensions -> drop-down (top right) -> *Install from Disk...* and pick
the ZIP. The operators appear under File -> Import / Export:
*J3D Model (.bmd)*, *J3D Animation (.bck)*, *TP Collision (.kcl)*,
*J3D Shape Keys (.bls)* and *J3D Shape Key Animation (.blk)*. Files can also be
dragged into the viewport.

All import/export commands are also available in `3D View > Sidebar > J3D >
Import / Export`.

## Models
1. **Import** with **Convert Units** enabled (default): the model is scaled to
   `0.01` (100 J3D units = 1 m). You get an armature (one bone per joint),
   materials and images.
   The model is always imported as one mesh object whose vertices keep the
   original BMD `VTX1` position order (`vertex i` is source position `i`).
2. Edit meshes / textures. Append new bones rather than re-ordering existing ones.
3. **Export** with the armature and meshes selected (*Selected Only*). The export
   uses the shared **Convert Units** setting (see below) and writes J3D units
   back (`100`) when it is enabled.

### What is preserved
* Joint order, hierarchy and parameters of unchanged bones, INF1 flags (Maya mode on Uri). Animations
  address joints by index, so the order matters.
* The original vertex order is kept through a BMD -> Blender -> BMD round trip:
  exported positions are written in the imported order and distinct vertices that
  share the same coordinates are no longer merged. Export also restores the source
  shape nodes (one shape per original INF1 node) using a small per-face node index.
* Normals use a canonical layout instead of being globally sorted. Every corner
  is addressed by its `(vertex, normal)` pair, so the normal array keeps a
  stable order and index.
* The importer stores the original per-corner normal indices, position order
  information and a topology hash on the mesh (`bmd_normal_index`,
  `bmd_normal_meta`). BMD export updates this metadata to the normal layout of
  the newly written file. With *Apply Modifiers*, metadata is kept only when the
  evaluated mesh has identical topology and vertex positions; otherwise it is
  invalidated.
* **All** textures in their original order with wrap / filter / LOD settings, including textures no
  material uses (e.g. eye textures swapped by texture animations). Duplicate names are allowed.
* The complete material block (TEV stages, colour channels, texture matrices, fog, blend ...) is kept
  as JSON in the material custom property `bmd_material`.

#### Index stability (important for the game)
The game and its animations address textures, shapes and materials **by index**, so the exporter keeps
them stable: shapes are written in their original shape-index order, materials in their original
order, every texture of the imported model is written in its original position (even those no
material uses, such as the eye frames), and shapes stay attached to their original joint.
Blender deletes images that have no user when a file is saved, so imported images get a *Fake User*.
If a texture is still missing at export time, it is restored from the original BMD (the path is
remembered at import, or set *Original BMD* in the export options); otherwise the export stops with
an error instead of silently writing a shifted texture list that crashes the game.

### Skinning layout (DRW1 / EVP1)
The game's loader computes the number of draw matrices as `DRW1 entries - envelopes` and fills the
weighted ones as `view * envelope[k]`. The exporter therefore writes DRW1 exactly like the original
tools: all rigid entries first, then one weighted entry per envelope in order, then the same
envelopes once more as filler. Every export is checked against these rules before it is written
(`validate_engine_layout`); an export that would crash the game raises an error instead.

### Vertex formats
Positions, normals and UVs have separate dropdowns with two choices each: **Float32** and **s16**.
When **s16** is selected, a **Fraction** field appears next to it.

**Positions**

| Option | Format |
| --- | --- |
| Float32 | float32 |
| s16 | signed 16-bit; fraction defaults to 6 |

s16 positions are not compatible with BLS: `J3DDeformer` only deforms float32 positions. If the
coordinates do not fit the selected fraction, the exporter falls back to float32 for that array.

**Normals**

| Option | Format |
| --- | --- |
| Float32 | float32 |
| s16 | signed 16-bit; fraction defaults to 14 |

BLS normal deformation additionally requires float32 normals; positions can still deform with s16
normals.

**UVs**

| Option | Format |
| --- | --- |
| Float32 | float32 |
| s16 | signed 16-bit; fraction defaults to 14 |

The UV choice applies to every UV set in the mesh. Colors are always written as `u8` (`RGBA8`).

The exporter still uses triangle **strips** only, stores shape bounds in model space (radius measured
from the origin) and puts a bounding box on the root joint only.

## Textures and HD texture packs
The importer stores the original encoded texture bytes (all mips, palettes) on each image. On export,
images that still match the imported pixels are written back **byte-identically**, so texture packs
that match textures by content keep working. Only edited images, or images whose format / mip setting
you changed, are re-encoded (CMPR, RGB5A3, RGBA32, RGB565, IA8, IA4, I8, I4). Palette formats are
decoded on import and re-encoded as CMPR / RGB5A3 only if you change the image.

### External stage textures (`STG_00/texc`)
Some stage models keep their texture pixels outside the BMD. In `TEX1`, such an entry has
`imageOffset = 0`; the game loads the picture at runtime from the stage's `texc` folder
(for example `STG_00/texc/I_Kusa_A.bti`). The BMD still stores the sampling settings
(wrap mode, filters, LOD, size), the texture name and the material bindings.

On import the add-on searches for the textures automatically:
* `STG_00/texc`
* `STG_00/../texc`

If some textures are still missing, the import opens **Import Settings** first. Its
**External Textures** section lists the BMD file(s) and their external textures, plus
a **texc Path** field.

Imported external images show the real pixels and remember their source file
(`bmd_external_path`, shown in *Image Editor -> Image -> J3D (BMD) Texture*). On export
they stay **external**: the BMD gets a header-only `TEX1` entry again unless you untick
**External Texture** (for example to make the room self-contained).

When several imported BMDs contain the same texture name, the importer reuses an
existing image if its pixels and sampling settings are identical. Same-named textures
that differ get a separate image (and Blender's `.001` suffix). Imported external
textures keep the original `.bti` bytes, so an unchanged texture exports exactly as it
was imported.

Any image can be converted into an external texture: select it in the Image Editor and
tick **External Texture** in the *J3D (BMD) Texture* panel. Use *File > Export > J3D
Textures (.bti)* to write the external textures to a folder.
Checkboxes of textures that do not need writing (`Up to date`) or cannot be written
(`Conflict`, `Error`) are disabled. Different textures that would use the same `.bti`
file name are shown as `conflict` and are not overwritten. The exporter uses the
texture name as a lowercase `.bti` file name and only overwrites files whose contents
changed. The BTI keeps the image format, mip count, wrap/filter settings and palette
data when they can be reused.

### Dusklight warning

Dusklight resolves external textures from the archive's file table. It replaces entries
that already exist in an archive; it cannot add a new file entry. To add a newly
externalized texture, rebuild and replace the whole .arc archive with the `.bti`
included in its `texc` folder.

### Runtime textures (placeholders)

Some texture slots in stage and character models contain no image data and no matching
external file: their data offset in the TEX1 header is 0 and the game fills the slot at
runtime (for example texture pattern animations). The importer detects them, creates a
grey placeholder image and ticks **External Texture** in the image panel. The exporter
writes them back as header-only entries (data offset 0). Never untick the option for
such an image unless you really want to ship a fixed texture there - otherwise the game
shows the embedded picture instead of the runtime one (in older versions of this add-on
this showed up as green/black checkerboard patterns, because header bytes were read as
pixels).

### Materials
* Material tab -> *J3D (BMD) Material*: **Render Mode** (Auto keeps imported values; Opaque / Cutout /
  Translucent), Alpha Reference, Backface Culling.
* Alpha test and blending are detected from the material's `alphaComp` / `blend` data and wired into
  the shader (alpha test via "Greater Than", blending as *Blended*).
* GX wraps per axis (Clamp / Repeat / Mirror). If the two axes differ, the importer builds a
  Separate XYZ -> Math -> Combine XYZ chain in front of the image node.
* To swap a texture, set the image of node `bmd_tex0` (`bmd_tex1` ...). Per-image format and mip count:
  Image Editor -> sidebar -> *Image* -> *J3D (BMD) Texture*.
* New materials (without `bmd_material`) become simple unlit "texture x vertex colour" materials.
* UV layers are named `UV0`..`UV7`, vertex colours `Col0` / `Col1`.

## Shape keys (.bls)

**Import**
1. Import the BMD first.
2. Select the target mesh, then `File > Import > J3D Shape Keys (.bls)` or
   `J3D Shape Keys from Folder (.bls)`. The same commands are in the `J3D`
   sidebar, **Import / Export** panel, under **Shape Keys (.bls)**.
3. In the dialog choose the target mesh, open each file, uncheck or rename keys
   as needed, and press OK.

Keys are imported as BLS position deltas relative to the BLS base key, rotated
from J3D Y-up to Blender Z-up and added to the target mesh's Basis coordinates.
**Convert Units** must stay enabled for meshes imported with the same setting
turned on.

Most game BLS files contain a `<cluster>__orig` key with zero displacement. It
is the Maya "original" blend-shape target; its BLK track holds the remainder of
the normalized weights (so it can be negative) and does not move any vertex.
Zero-displacement keys are skipped during import. The BLS exporter recreates an
`orig` shape key automatically.

The dialog marks keys as `OK`, `Duplicate`, `Conflict`, `Base` or
`Incompatible`. Duplicate/conflict detection compares the actual vertex
displacement, not file names or slot counts. A key that references a vertex
outside the target mesh is `Incompatible` and cannot be imported.

**Export**
1. Select the mesh that contains the shape keys.
2. `File > Export > J3D Shape Keys (.bls)`.
3. Select the keys and press **Export**.

The exporter writes one BLS cluster containing the selected keys, an
automatically created zero-delta original key `orig`, and a trailing base key,
including generated base/key normals and the normal blend table. Normal
destinations use the `bmd_normal_index` / `bmd_normal_meta` metadata written by
the BMD importer/exporter. If the mesh topology or vertex positions changed,
re-export the BMD first; the dialog warns when the metadata is stale.

Requirements and limits:

* BLS vertex indices must match the target mesh topology.
* The matching BMD must be exported with **Float32** positions if edited; the
  `s16` position option is not compatible with BLS. Normal deformation
  additionally requires **Float32** normals.
* Export the matching BMD from the same mesh alone. A BMD exported from several
  mesh objects gives each object its own normal-index range.
* **Apply Modifiers** preserves the normal metadata only when the evaluated
  mesh has identical topology and vertex positions. Apply topology- or
  position-changing modifiers before creating or exporting shape keys.
* The BLS normal pool is limited to 8192 distinct vectors by the 13-bit index
  field. Very large multi-key clusters may be exported with slightly reduced
  normal precision; positions and normal destinations are not affected.
* The export dialog validates the file before writing. Position-pool overflow,
  vertex-index or cluster-size overflow, normal-blend overflow, an oversized
  name table, non-Shift-JIS names, a missing key and base-name conflicts are
  listed below the key tree; resolve them before pressing **Export**.
* BLS normals do not participate in Blender shape-key creation: Blender stores
  vertex coordinates and recalculates normals from the geometry.
* An original BLS is only guaranteed to work with the original BMD it was made
  for. If the BMD is modified or re-exported, re-export the BLS as well.
* A modified BLS can still target the original BMD when the mesh was imported
  with this add-on.

## Shape key animations (.blk)

**Import**: select the mesh with shape keys and use `File > Import > J3D Shape
Key Animation (.blk)` or the **Shape Key Animation (.blk)** box in the `J3D`
sidebar. After choosing a file, a dialog lists the BLK tracks; tracks are
auto-assigned in the current shape-key order.

**Export**: select the mesh with the shape-key action, then
`File > Export > J3D Shape Key Animation (.blk)`.
Only checked keys are exported, in the current shape-key order, so the tracks
match the keys written by the BLS export dialog. An `orig` track is always
prepended; its weights are recalculated as `1 - sum(other selected weights)`.

## Animations (.bck)
**Import**: select the imported armature, then File -> Import -> *J3D Animation (.bck)*. A new action
is created (named after the file, with a fake user) and keyed on every frame with linear
interpolation; channels that never change get a single key. Frame `n` of the file is Blender frame
`n`; the scene range can be set to the animation length. The loop mode is stored on the action
(`bmd_loop_mode`).

**Export**: make the armature active, assign the action, then File -> Export -> *J3D Animation (.bck)*.
Every frame is sampled, converted back to joint-local scale / rotation / translation, and fitted
with cubic Hermite curves using as few keys as the tolerances allow (rotation 2e-4 rad, translation
0.01 units, scale 1e-4). Options: loop mode (Auto uses the stored one), action or scene frame range,
and the **Convert Units** setting.

Notes
* The joint count of the animation equals the number of bones; bones keep their `bmd_index`.
* Joints with a non-uniform rest scale (e.g. the eyebrows of Uri, 1.6 on X) are handled: the stored
  scale is folded into the rest transform so the animation is not scaled twice. In Maya mode a joint
  that does not inherit its parent's scale gets `Inherit Scale: None` on its bone.
* The Blender pose reproduces the game's skinning (checked: skinned vertices match to 1e-4 units).

## Collision (.kcl)

Stage collision (`room.kcl`, used together with `room.plc`) is imported and exported as well:
*File > Import/Export > TP Collision (.kcl)*. The game derives **Link's shadow, standing height,
walls and slopes from the collision mesh, not from `model.bmd`**, so a changed visual mesh needs a
matching collision mesh.

* Import it with **Convert Units** enabled so both line up in Blender.
* Every collision attribute is a material called `KCL_<n>`, where `n` is the row of `room.plc`.
  The material of a face *is* its attribute: paint faces with another `KCL_<n>` material to change
  how they behave. Faces without such a material get the export option *Default Attribute*.
* Export the collision mesh only (*Selected Only*) with the **Convert Units** setting. The file
  contains the triangles, normals and the octree the game uses for every query.
* `room.plc` is **not** rewritten; keep the original file next to the new `room.kcl`. Attribute
  numbers beyond the number of rows in the PLC are reported as a warning.
* Limits: about 12000 triangles per file (the game asserts a 16-bit prism index), and the triangle
  winding defines the face normal, so make sure floors face upwards.

## Known limitations
* No TEV shading in Blender; the Blender materials are only a preview.
* Joint scales other than 1 are exact only for unchanged bones.
* By default all bone influences are preserved (the BMD `EVP1` limit is 255 per vertex). Enable
  **Limit 4 Weights per Vertex** on export to cap skinning at 4 influences, which may be required by
  engines that assume standard 4-bone skinning. Shapes are written as triangle lists (no strips), so
  files are larger than the originals. MDL3 (BDL display lists) is not written.
* Only `bck` (joint animations) is supported, not `btk` / `btp` / `bpk` / `brk` / `bca`.
