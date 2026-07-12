"""Command-line interface: python -m panda2prusa ..."""

from __future__ import annotations

import argparse
import sys

from .convert import (
    convert_file,
    describe,
    filament_label,
    suggest_extruder_map,
    used_filaments,
)
from . import ns


def _cmd_info(args) -> int:
    project = describe(args.input)
    print(f"File:      {args.input}")
    print(f"Producer:  {project.producer or 'unknown'}")
    print(f"Objects:   {len(project.object_trees) + 1} model part(s)")
    print(f"Build items: {len(project.build_items)}")
    if project.plate_ids:
        print(f"Plates:    {', '.join(str(p) for p in project.plate_ids)}")
        # per-plate object counts
        counts: dict = {}
        for oid, plate in project.plate_of_object.items():
            counts[plate] = counts.get(plate, 0) + 1
        for p in project.plate_ids:
            print(f"   plate {p}: {counts.get(p, 0)} object(s)")
    used = used_filaments(project)
    if used:
        print(f"Filaments used: {', '.join(filament_label(project, f) for f in used)}")
    # painting summary
    painted = 0
    for tree in [project.root_tree, *project.object_trees.values()]:
        for tri in tree.getroot().iter(ns.q(ns.CORE, "triangle")):
            if "paint_color" in tri.attrib:
                painted += 1
    print(f"Painted triangles: {painted}")
    return 0


def _parse_map(text: str) -> dict:
    """Parse '3=2,1=1' (also accepts '3:2' and '3->2') into {3: 2, 1: 1}."""
    mapping = {}
    for pair in text.replace("->", "=").replace(":", "=").replace(";", ",").split(","):
        pair = pair.strip()
        if not pair:
            continue
        try:
            old, new = pair.split("=")
            mapping[int(old)] = int(new)
        except ValueError:
            raise SystemExit(f"Bad extruder mapping {pair!r}; expected e.g. 3=2,1=1")
    return mapping


def _prompt_extruder_map(project) -> dict | None:
    """Show the file's filaments and ask how to map them. None = auto-compact."""
    used = used_filaments(project)
    if len(used) < 2 and used == [1]:
        return None  # single color on slot 1 — nothing to decide
    print(f"This file uses {len(used)} filament(s):")
    for f in used:
        print(f"  filament {filament_label(project, f)}")
    suggestion = suggest_extruder_map(used)
    sug = ", ".join(f"{k}->{v}" for k, v in suggestion.items())
    try:
        resp = input(
            f"Map to Prusa extruders [{sug}] (Enter=accept, 'keep' for no change, "
            "or e.g. 3=2,1=1): "
        ).strip()
    except EOFError:
        return None
    if not resp:
        return suggestion
    if resp.lower() in ("keep", "k", "none"):
        return {}
    return _parse_map(resp)


def _cmd_convert(args) -> int:
    plates = None
    if args.plate:
        plates = []
        for chunk in args.plate:
            plates.extend(int(p) for p in str(chunk).replace(",", " ").split())

    extruder_map = None
    if args.map:
        extruder_map = _parse_map(args.map)
    elif args.keep_extruders:
        extruder_map = {}
    elif sys.stdin.isatty() and sys.stdout.isatty():
        extruder_map = _prompt_extruder_map(describe(args.input))

    result = convert_file(
        args.input, args.output, plates=plates, extruder_map=extruder_map
    )
    s = result.stats
    print(f"Converted: {args.input}")
    print(f"       ->  {args.output}")
    print(f"Producer:  {result.producer or 'unknown'}")
    if result.plates_available:
        shown = plates if plates else "all"
        print(f"Plates:    available {result.plates_available}, converted {shown}")
    print(f"Objects written:      {s.objects_written}")
    print(f"Volumes written:      {s.volumes_written}")
    print(f"Build items written:  {s.build_items_written}")
    print(f"Painted triangles:    {s.painted_triangles}")
    if s.extruder_map:
        remapped = ", ".join(f"{k}->{v}" for k, v in sorted(s.extruder_map.items()))
        print(f"Extruders remapped:   {remapped}")
    if s.objects_written == 0:
        print("WARNING: no objects were written — check plate selection.", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="panda2prusa",
        description="Convert Bambu Studio / OrcaSlicer .3mf files to PrusaSlicer .3mf.",
    )
    p.add_argument("--info", metavar="FILE", help="inspect a 3mf and exit")
    p.add_argument("input", nargs="?", help="input Bambu/Orca .3mf")
    p.add_argument("output", nargs="?", help="output Prusa .3mf")
    p.add_argument(
        "--plate",
        action="append",
        help="only convert these plate number(s), e.g. --plate 1 or --plate 1,2",
    )
    p.add_argument(
        "--map",
        metavar="MAP",
        help="map Bambu filament slots to Prusa extruders, e.g. --map 3=2 or --map 1=2,3=1",
    )
    p.add_argument(
        "--keep-extruders",
        action="store_true",
        help="keep original filament slot numbers (no compaction, no prompt)",
    )
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.info:
        args.input = args.info
        return _cmd_info(args)
    if not args.input or not args.output:
        build_parser().print_help()
        return 1
    try:
        return _cmd_convert(args)
    except Exception as exc:  # surface a clean message, not a traceback
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
