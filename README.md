# J3D BMD / BCK / BLS / BLK / BTP Import & Export for Blender (Zelda: Twilight Princess / Dusklight)

Imports and exports Nintendo J3D models (`.bmd`, `.bdl` read-only), joint animations (`.bck`), texture-pattern animations (`.btp`),
collision (`.kcl`), shape keys (`.bls`) and shape-key animations (`.blk`).

## Features

* **Models**: BMD import/export and BDL import (read-only) with the original vertex, joint, material, shape and texture order.
* **Textures**: GX formats (including C4, C8 and C14X2), external `texc` textures, BTI import/export and byte-identical HD texture packs.
* **Materials**: TEV data in `bmd_material`, BTP texture animation preview and material creation tools.
* **Shape keys**: BLS import/export and BLK animation import/export.
* **Animations**: BCK, BLK and BTP import/export through one animation list.
* **Collision**: KCL import/export.

## Installation (Blender 4.2 - 5.x)

Download this code as ZIP, then in Blender open *Edit -> Preferences -> Get Extensions -> Install from Disk...* and pick the ZIP.
The operators are under *File -> Import / Export* and in *3D View > Sidebar > J3D*.

## Documentation

See the [Wiki](Wiki/Home.md) for installation details and usage.

## Disclaimer

This add-on is provided "as is", without warranty of any kind (GPL-3.0-or-later). It is an unofficial fan tool, not affiliated with Nintendo
or the Dusklight / Twilit Realm project. Always keep backups of your original game files.
