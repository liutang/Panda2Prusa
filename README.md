# Panda2Prusa 🐼

**Bambu Studio → PrusaSlicer, nothing lost in translation.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-ffdd00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/cupsohjoe)

Convert **Bambu Studio / OrcaSlicer** `.3mf` project files into **PrusaSlicer**-compatible
`.3mf` files, preserving:

- **Real per-object transforms** (position, rotation, scale) — composed correctly from the
  build items and component references, not a hardcoded matrix.
- **Component / assembly structure** — Bambu splits geometry across `3D/Objects/object_N.model`
  and references them via the 3MF *production extension*; these are resolved and flattened into
  a single PrusaSlicer-style `3D/3dmodel.model`.
- **Per-triangle painting** (`paint_color` → PrusaSlicer `mmu_segmentation`) — the shared
  TriangleSelector bitstream is preserved (and transcoded when extruders are renumbered).
- **Multi-material part / extruder assignments** from `Metadata/model_settings.config` →
  PrusaSlicer merged-mesh volume config (`firstid`/`lastid` ranges).
- **Filament → extruder mapping** — Bambu AMS slots are sparse (a two-color print may use
  slots 1 and 3); the converter asks how to map them (or auto-compacts to extruders 1..K)
  so the model prints on a 2-tool/5-tool Prusa. Paint data is re-encoded to match.
- **Multiple plates** — pick one or convert all.

This is a ground-up rewrite of the approach in
[raistlinJ/3mf_bambu2prusa](https://github.com/raistlinJ/3mf_bambu2prusa), fixing its core
limitations (hardcoded transform, ignored component structure, regex-based XML surgery).

## Why the rewrite

The original tool stamped **every** object with a single hardcoded transform copied from one
specific model (a ~0.8× scale at a fixed bed position), ignored Bambu's component/assembly
structure entirely, and rebuilt models with fragile string substitution. Any file other than the
author's sample came out mis-scaled and mis-placed, with multi-object plates collapsing on top of
each other. This version reads the real structure with a namespace-aware XML parser and preserves it.

## Install

```
pip install -r requirements.txt
```

(Requires Python 3.9+. `tkinter` ships with the standard Python installer for the GUI.)

## Usage

CLI:

```
python -m panda2prusa input.3mf output.3mf          # convert all plates
python -m panda2prusa input.3mf output.3mf --plate 1 # convert only plate 1
python -m panda2prusa --info input.3mf               # inspect a 3mf without converting
python -m panda2prusa input.3mf output.3mf --map 3=2 # Bambu filament 3 -> Prusa extruder 2
python -m panda2prusa input.3mf output.3mf --keep-extruders  # no renumbering, no prompt
```

Run interactively and the CLI shows the filaments the file uses (with their colors) and
asks how to map them; the GUI pops the same question with color swatches.

> **Viewing tip:** PrusaSlicer only renders multi-color assignments under a printer profile
> that actually has multiple extruders/tools (MMU3, XL 2T/5T…). Under a single-extruder
> profile everything legitimately shows as one color.

GUI:

```
python -m panda2prusa.gui
```

Standalone Windows exe (no Python needed to run it):

```
pip install pyinstaller
pyinstaller --onefile --windowed --name Panda2Prusa gui_launcher.py
```

The result lands in `dist\Panda2Prusa.exe` — double-click to launch the GUI. Prebuilt
exes are attached to the [GitHub releases](https://github.com/CupsOhJoe/Panda2Prusa/releases).

## Running the tests

The suite runs against any Bambu/Orca `.3mf` files you drop into `samples/` (kept out of
git), or a directory pointed at by the `B2P_SAMPLES` environment variable. Painted,
multi-plate, and multi-material files give the best coverage; everything skips cleanly
if no samples are present.

```
pip install pytest
pytest
```

## Status

Working end to end — see [CHANGELOG.md](CHANGELOG.md) for what's in the current release
and its known limitations. Issues and PRs welcome, ideally with a sample `.3mf` that
shows the problem.

## Support

If this saved your print (or your sanity), you can
[buy me a coffee](https://buymeacoffee.com/cupsohjoe). ☕
