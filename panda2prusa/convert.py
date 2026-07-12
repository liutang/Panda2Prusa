"""High-level conversion orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import ns
from .paint import used_states, PaintError
from .reader import read_bambu_3mf, BambuProject
from .writer import write_prusa_3mf, WriteStats


@dataclass
class ConversionResult:
    input_path: str
    output_path: str
    stats: WriteStats
    producer: str
    plates_available: list


def used_filaments(project: BambuProject) -> list[int]:
    """Sorted 1-based filament slots a project actually uses.

    Union of object/part extruder assignments and every state referenced by
    paint data. Bambu leaves unassigned objects on filament 1.
    """
    used: set[int] = set()
    for so in project.settings_objects.values():
        for ext in [so.extruder, *(p.extruder for p in so.parts)]:
            if ext:
                try:
                    used.add(int(ext))
                except ValueError:
                    pass
    codes: set[str] = set()
    for tree in [project.root_tree, *project.object_trees.values()]:
        for tri in tree.getroot().iter(ns.q(ns.CORE, "triangle")):
            code = tri.get("paint_color")
            if code:
                codes.add(code)
    for code in codes:
        try:
            used |= used_states(code)
        except PaintError:
            pass
    used.discard(0)
    if not used and (project.build_items or codes):
        used.add(1)  # something is printed and it defaults to filament 1
    return sorted(used)


def suggest_extruder_map(used: list[int]) -> dict[int, int]:
    """Compact sparse filament slots to sequential extruders 1..K."""
    return {old: i for i, old in enumerate(sorted(used), start=1)}


def filament_label(project: BambuProject, slot: int) -> str:
    """Human-readable description of a filament slot, e.g. '3 (PLA #8E2929)'."""
    bits = []
    if 0 < slot <= len(project.filament_types):
        bits.append(project.filament_types[slot - 1])
    if 0 < slot <= len(project.filament_colors):
        bits.append(project.filament_colors[slot - 1])
    return f"{slot} ({' '.join(bits)})" if bits else str(slot)


def convert_file(
    input_path: str,
    output_path: str,
    plates: Optional[list] = None,
    extruder_map: Optional[dict] = None,
    compact_extruders: bool = True,
) -> ConversionResult:
    """Convert a Bambu/Orca .3mf at ``input_path`` to a Prusa .3mf at ``output_path``.

    ``plates`` optionally restricts output to the given 1-based plate numbers.
    ``extruder_map`` explicitly maps Bambu filament slots to Prusa extruders
    (``{}`` keeps the original numbering); when None and ``compact_extruders`` is
    true, sparse slots are compacted to sequential extruders automatically.
    """
    project = read_bambu_3mf(input_path)
    stats = write_prusa_3mf(
        project,
        output_path,
        plates=plates,
        extruder_map=extruder_map,
        compact_extruders=compact_extruders,
    )
    return ConversionResult(
        input_path=input_path,
        output_path=output_path,
        stats=stats,
        producer=project.producer,
        plates_available=project.plate_ids,
    )


def describe(input_path: str) -> BambuProject:
    """Read a project for inspection (used by --info and interactive prompts)."""
    return read_bambu_3mf(input_path)
