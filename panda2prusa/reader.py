"""Read a Bambu Studio / OrcaSlicer .3mf into an in-memory project.

The reader is deliberately tolerant: it keeps the parsed lxml trees around so the
writer can move ``<mesh>`` XML verbatim (preserving vertices and per-triangle paint
data losslessly), while also extracting the structured bits needed for plate
selection and multi-material config translation.
"""

from __future__ import annotations

import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

import lxml.etree as ET

from . import ns


ROOT_MODEL = "3D/3dmodel.model"


@dataclass
class Part:
    """A part/volume inside a Bambu object, from model_settings.config."""

    id: str
    name: str = ""
    extruder: Optional[str] = None
    matrix: Optional[str] = None


@dataclass
class SettingsObject:
    """A model_settings.config <object> entry (grouping + per-part extruders)."""

    id: str
    name: str = ""
    extruder: Optional[str] = None
    parts: list[Part] = field(default_factory=list)


@dataclass
class BuildItem:
    objectid: str
    transform: str  # raw 12-number string as stored in the source
    printable: bool = True
    element: object = None  # the source lxml <item>


@dataclass
class BambuProject:
    source_path: str
    root_tree: object  # lxml ElementTree of 3D/3dmodel.model
    object_trees: dict  # normalized model path -> lxml ElementTree
    build_items: list  # list[BuildItem]
    plate_of_object: dict  # object_id -> plate index (1-based)
    plate_ids: list  # sorted list of plate indices present
    settings_objects: dict  # object_id -> SettingsObject
    raw_entries: dict  # arcname -> bytes, for passthrough assets (thumbnails, etc.)
    producer: str  # detected producer string ("BambuStudio-...", "OrcaSlicer-...", or "")
    filament_colors: list = field(default_factory=list)  # 1-based slots, "#RRGGBB" strings
    filament_types: list = field(default_factory=list)  # e.g. ["PLA", "PETG", ...]
    source: str = "bambu"

    def object_element(self, path: str, objectid: str):
        """Look up an <object id=..> element in a given model file."""
        tree = self.root_tree if path in (None, ROOT_MODEL) else self.object_trees.get(path)
        if tree is None:
            return None
        root = tree.getroot()
        for obj in root.iter(ns.q(ns.CORE, "object")):
            if obj.get("id") == objectid:
                return obj
        return None


def _normalize_path(target: str, base: str = ROOT_MODEL) -> str:
    """Resolve a (possibly absolute) 3mf part path to a zip arcname."""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), target))


def _parse(data: bytes):
    """Parse model XML tolerantly (some producers emit non-utf8 declarations)."""
    parser = ET.XMLParser(recover=True, huge_tree=True)
    return ET.fromstring(data, parser=parser).getroottree()


def read_bambu_3mf(path: str) -> BambuProject:
    with zipfile.ZipFile(path, "r") as z:
        names = set(z.namelist())
        if ROOT_MODEL not in names:
            raise ValueError(
                f"{path!r} does not contain {ROOT_MODEL}; not a recognizable 3mf project."
            )
        root_tree = _parse(z.read(ROOT_MODEL))

        # Gather every referenced object model file: from the rels and from any
        # component p:path attributes in the root (belt and suspenders).
        object_paths: set[str] = set()
        rels_name = "3D/_rels/3dmodel.model.rels"
        if rels_name in names:
            rels_root = ET.fromstring(z.read(rels_name))
            for rel in rels_root.iter(ns.q(ns.RELS, "Relationship")):
                tgt = rel.get("Target")
                if tgt and tgt.lower().endswith(".model"):
                    object_paths.add(_normalize_path(tgt))
        for comp in root_tree.getroot().iter(ns.q(ns.CORE, "component")):
            p = comp.get(ns.q(ns.PRODUCTION, "path"))
            if p:
                object_paths.add(_normalize_path(p))

        object_trees: dict[str, object] = {}
        for op in sorted(object_paths):
            if op in names and op != ROOT_MODEL:
                object_trees[op] = _parse(z.read(op))

        # Build items (with their real transforms).
        build_items: list[BuildItem] = []
        build_el = root_tree.getroot().find(ns.q(ns.CORE, "build"))
        if build_el is not None:
            for item in build_el.findall(ns.q(ns.CORE, "item")):
                oid = item.get("objectid")
                if oid is None:
                    continue
                build_items.append(
                    BuildItem(
                        objectid=oid,
                        transform=item.get("transform", ""),
                        printable=item.get("printable", "1") not in ("0", "false", "False"),
                        element=item,
                    )
                )

        # Producer detection.
        producer = ""
        for md in root_tree.getroot().findall(ns.q(ns.CORE, "metadata")):
            if md.get("name") == "Application" and md.text:
                producer = md.text.strip()
                break

        # model_settings.config: plates + per-part extruders.
        settings_objects: dict[str, SettingsObject] = {}
        plate_of_object: dict[str, int] = {}
        plate_ids: list[int] = []
        cfg_name = "Metadata/model_settings.config"
        if cfg_name in names:
            settings_objects, plate_of_object, plate_ids = _parse_model_settings(
                z.read(cfg_name)
            )

        # Filament slot colors/types from Bambu's project settings (JSON).
        filament_colors: list[str] = []
        filament_types: list[str] = []
        ps_name = "Metadata/project_settings.config"
        if ps_name in names:
            try:
                ps = json.loads(z.read(ps_name))
                filament_colors = [str(c) for c in ps.get("filament_colour") or []]
                filament_types = [str(t) for t in ps.get("filament_type") or []]
            except (ValueError, TypeError):
                pass

        # Passthrough assets we want to keep (thumbnails); skip Bambu-only config.
        raw_entries: dict[str, bytes] = {}
        for n in names:
            low = n.lower()
            if low.endswith(".png") and ("thumbnail" in low or "plate" in low or "/metadata/" in low):
                raw_entries[n] = z.read(n)

    return BambuProject(
        source_path=path,
        root_tree=root_tree,
        object_trees=object_trees,
        build_items=build_items,
        plate_of_object=plate_of_object,
        plate_ids=plate_ids,
        settings_objects=settings_objects,
        raw_entries=raw_entries,
        producer=producer,
        filament_colors=filament_colors,
        filament_types=filament_types,
    )


def _parse_model_settings(data: bytes):
    """Extract per-object parts/extruders and plate membership."""
    root = ET.fromstring(data, parser=ET.XMLParser(recover=True))
    settings_objects: dict[str, SettingsObject] = {}
    for obj in root.findall("object"):
        oid = obj.get("id")
        if oid is None:
            continue
        so = SettingsObject(id=oid)
        for md in obj.findall("metadata"):
            key, val = md.get("key"), md.get("value")
            if key == "name":
                so.name = val or ""
            elif key == "extruder":
                so.extruder = val
        for part in obj.findall("part"):
            p = Part(id=part.get("id", ""))
            for md in part.findall("metadata"):
                key, val = md.get("key"), md.get("value")
                if key == "name":
                    p.name = val or ""
                elif key == "extruder":
                    p.extruder = val
                elif key == "matrix":
                    p.matrix = val
            so.parts.append(p)
        settings_objects[oid] = so

    plate_of_object: dict[str, int] = {}
    plate_ids: list[int] = []
    for idx, plate in enumerate(root.findall("plate"), start=1):
        plate_no = idx
        for md in plate.findall("metadata"):
            if md.get("key") in ("plater_id", "plate_id", "index") and md.get("value"):
                try:
                    plate_no = int(md.get("value"))
                except ValueError:
                    pass
        plate_ids.append(plate_no)
        for inst in plate.findall("model_instance"):
            for md in inst.findall("metadata"):
                if md.get("key") == "object_id" and md.get("value"):
                    plate_of_object[md.get("value")] = plate_no
    return settings_objects, plate_of_object, sorted(set(plate_ids))
