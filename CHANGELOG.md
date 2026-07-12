# Changelog

## v1.0.0 — 2026-07-11

First public release.

- Reads Bambu Studio / OrcaSlicer project files with their real structure:
  referenced object files, component transforms, build items, plates, and
  per-part extruder assignments.
- Writes PrusaSlicer-native output: one merged mesh per object with
  `firstid`/`lastid` volume ranges, so part names and extruder assignments
  actually survive the import.
- Per-triangle painting carries over. The paint bitstream is decoded and
  re-encoded when filament slots are renumbered, so painted regions follow
  their filament to the new extruder.
- Filament mapping: sparse AMS slots (say, 1 and 3) get compacted to
  sequential extruders so a two-color model prints on a two-tool machine.
  The CLI and GUI both show the file's filaments (with colors) and ask how
  to map them; `--map` and `--keep-extruders` cover scripted use.
- Plate selection (`--plate 1`), file inspection (`--info`), Tkinter GUI,
  and a standalone Windows exe built with PyInstaller.

Known limitations:

- Slicing itself isn't tested end to end (PrusaSlicer `--info` load checks
  only); slice your first converted file with supports/preview on and eyeball
  it before committing a long print.
- Object/volume print settings beyond name and extruder (custom layer
  heights, modifiers, per-part profiles) aren't carried over.
