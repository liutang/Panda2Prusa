# Panda2Prusa 🐼

**Bambu Studio → PrusaSlicer, nothing lost in translation.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Downloads](https://img.shields.io/github/downloads/CupsOhJoe/Panda2Prusa/total?label=downloads)](https://github.com/CupsOhJoe/Panda2Prusa/releases)
[![Stars](https://img.shields.io/github/stars/CupsOhJoe/Panda2Prusa?style=flat&label=stars)](https://github.com/CupsOhJoe/Panda2Prusa/stargazers)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-support-ffdd00?logo=buymeacoffee&logoColor=black)](https://www.buymeacoffee.com/cupsohjoe)

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

It also takes **PrusaSlicer** `.3mf` projects, to move a print onto different tool heads
without redoing the assignment in the slicer or at the printer — see
[Remapping a PrusaSlicer project](#remapping-a-prusaslicer-project).

This is a ground-up rewrite of the approach in
[raistlinJ/3mf_bambu2prusa](https://github.com/raistlinJ/3mf_bambu2prusa), fixing its core
limitations (hardcoded transform, ignored component structure, regex-based XML surgery).

## Why the rewrite

The original tool stamped **every** object with a single hardcoded transform copied from one
specific model (a ~0.8× scale at a fixed bed position), ignored Bambu's component/assembly
structure entirely, and rebuilt models with fragile string substitution. Any file other than the
author's sample came out mis-scaled and mis-placed, with multi-object plates collapsing on top of
each other. This version reads the real structure with a namespace-aware XML parser and preserves it.

## Remapping a PrusaSlicer project

Give the converter a `.3mf` saved by PrusaSlicer 2.x and it remaps the tool heads instead
of converting:

```
python -m panda2prusa project.3mf project_remapped.3mf --map 1=5,8=2
```

The file is copied as it is, and only what names a tool is rewritten: object, volume and
modifier extruders, multi-material painting, per-layer-range extruders, tool changes at a
layer, and the print profile's default extruders.

The filaments follow. Moving tool 1 to tool 5 also moves tool 1's filament preset, color,
temperatures and purge volumes to slot 5, and the filament that was in slot 5 takes slot 1,
so the print comes out the same from different tool heads. Tool-head hardware settings
(nozzle diameter, retraction, offsets) stay with the tool.

Limits: the file still has to be sliced in PrusaSlicer afterwards (already-sliced G-code
isn't touched), tool numbers inside custom G-code aren't rewritten, and projects saved by
PrusaSlicer 3 aren't supported yet.

## Install

```
pip install -r requirements.txt
```

(Requires Python 3.9+. `tkinter` ships with the standard Python installer for the GUI.)

## Web app (Docker / homelab)

A browser front end runs in a container on port **8543**. Drop in a `.3mf`, pick plates,
map filaments to extruders (with color swatches), and download the converted file. A
PrusaSlicer `.3mf` gets the same mapping list, sized to the project's own tool count.

A prebuilt multi-arch image (amd64/arm64) is published to GHCR on every push to `main`:

```
docker run -d --name panda2prusa -p 8543:8543 --restart unless-stopped ghcr.io/liutang/panda2prusa:latest
# then open http://<host>:8543
```

Or with the included `compose.yaml` (just that file is enough on the host):

```
docker compose up -d
```

To build the image yourself instead: `docker build -t panda2prusa .`

The app is stateless: uploads are converted in a temp directory and deleted as soon as
the response is sent. There's no authentication, so keep it on your LAN or put it behind a
reverse proxy that handles auth.

| Env var              | Default | Purpose                                                  |
|----------------------|---------|----------------------------------------------------------|
| `P2P_PORT`           | `8543`  | Port to listen on                                        |
| `P2P_MAX_UPLOAD_MB`  | `300`   | Largest `.3mf` accepted (MB)                             |
| `P2P_MAX_CONCURRENT` | `1`     | Conversions run at once; others wait their turn          |

Memory: a conversion needs roughly 15–20x the uncompressed size of the model's mesh data
(a 36 MB, heavily painted project peaks around 3.5 GB). `compose.yaml` mounts `/tmp` as a
300 MB tmpfs, which also counts toward the container's memory; each request keeps the
upload there twice plus the converted file while it's being processed.

Endpoints: `GET /healthz`, `POST /api/inspect`, `POST /api/convert`; interactive API
docs are at `/api/docs`.

To run it without Docker:

```
pip install -r requirements-web.txt
python -m panda2prusa.web
```

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
[buy me a coffee](https://www.buymeacoffee.com/cupsohjoe). ☕
