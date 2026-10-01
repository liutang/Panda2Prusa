"""Write a PrusaSlicer-compatible .3mf from a BambuProject.

Strategy: emit one *merged mesh* per build object — exactly the shape PrusaSlicer's
own writer produces. Each Bambu part (component) becomes a contiguous triangle range
in the merged mesh, recorded as a ``<volume firstid=.. lastid=..>`` entry in
``Metadata/Slic3r_PE_model.config`` carrying the part's name and extruder. This is
the only representation in which PrusaSlicer honors per-part extruder assignments;
component-based objects collapse to a single volume on import and lose them.

Component transforms are baked into the vertex coordinates (volumes therefore need
no matrix metadata), build-item transforms are preserved verbatim, and per-triangle
paint data travels with its triangle: ``paint_color`` values are kept byte-for-byte
under the ``slic3rpe:mmu_segmentation`` name, since Bambu Studio forked PrusaSlicer's
TriangleSelector and the RLE bitstream format is shared.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

import lxml.etree as ET

from . import ns
from .paint import transform_states, used_states, PaintError
from .reader import BambuProject, Part, ROOT_MODEL
from .transform import Transform

# Attributes to strip from triangles (production + bambu extensions).
_DROP_ATTRS = {
    ns.q(ns.PRODUCTION, "UUID"),
    ns.q(ns.PRODUCTION, "path"),
    "paint_seam",
    "paint_supports",
}

CONTENT_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
    ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
    ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
    ' <Default Extension="png" ContentType="image/png"/>\n'
    "</Types>\n"
)


@dataclass
class WriteStats:
    objects_written: int = 0
    volumes_written: int = 0
    build_items_written: int = 0
    painted_triangles: int = 0
    plates_included: Optional[list] = None
    extruder_map: Optional[dict] = None  # {bambu filament -> prusa extruder} if remapped


@dataclass
class _Volume:
    firstid: int
    lastid: int
    leaf_oid: str
    name: str = ""
    extruder: Optional[str] = None


@dataclass
class _MergedObject:
    """A build object planned for merging; its mesh is streamed out by ``_write_object``."""

    name: str
    leaves: list  # [(leaf_key, cumulative Transform)]
    volumes: list = field(default_factory=list)
    painted: int = 0
    paint_codes: set = field(default_factory=set)  # distinct non-empty paint strings


def _key(path: str, objectid: str) -> tuple[str, str]:
    return (path or ROOT_MODEL, objectid)


def _collect_objects(project: BambuProject):
    """Map (model_path, object_id) -> lxml <object> element across all files."""
    objs = {}
    src_path = {}
    trees = {ROOT_MODEL: project.root_tree}
    trees.update(project.object_trees)
    for path, tree in trees.items():
        for obj in tree.getroot().iter(ns.q(ns.CORE, "object")):
            oid = obj.get("id")
            if oid is not None:
                objs[_key(path, oid)] = obj
                src_path[_key(path, oid)] = path
    return objs, src_path


def _component_target(comp, current_path: str) -> tuple[str, str]:
    p = comp.get(ns.q(ns.PRODUCTION, "path"))
    from .reader import _normalize_path

    path = _normalize_path(p) if p else current_path
    return _key(path, comp.get("objectid"))


def _flatten_leaves(key, objs, src_path, cum=None, _depth=0):
    """DFS a component tree into mesh leaves: [(leaf_key, cumulative Transform)]."""
    if cum is None:
        cum = Transform.identity()
    obj = objs.get(key)
    if obj is None or _depth > 16:
        return []
    leaves = []
    if obj.find(ns.q(ns.CORE, "mesh")) is not None:
        leaves.append((key, cum))
    comps = obj.find(ns.q(ns.CORE, "components"))
    if comps is not None:
        for comp in comps.findall(ns.q(ns.CORE, "component")):
            t = Transform.parse(comp.get("transform"))
            leaves.extend(
                _flatten_leaves(
                    _component_target(comp, src_path[key]),
                    objs,
                    src_path,
                    t.compose(cum),
                    _depth + 1,
                )
            )
    return leaves


def _fmt(v: float) -> str:
    """Shortest round-trip decimal for a coordinate."""
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


def _plan_object(root_key, objs, src_path, parts: list) -> Optional[_MergedObject]:
    """Plan merging a build object's component tree into a single mesh.

    Records the triangle range, name and extruder of each volume plus the paint codes
    used, without copying any geometry; ``_write_object`` streams the merged mesh.
    """
    leaves = _flatten_leaves(root_key, objs, src_path)
    if not leaves:
        return None

    # Match Bambu parts (name/extruder) to leaves: by id when possible, else by order.
    part_by_id = {p.id: p for p in parts}
    def part_for(leaf_idx: int, leaf_oid: str) -> Optional[Part]:
        if leaf_oid in part_by_id:
            return part_by_id[leaf_oid]
        if len(parts) == len(leaves):
            return parts[leaf_idx]
        return None

    merged = _MergedObject(name=objs[root_key].get("name") or "", leaves=leaves)
    tri_count = 0
    for leaf_idx, (leaf_key, _xf) in enumerate(leaves):
        leaf = objs[leaf_key]
        tris = leaf.find(_MESH).find(_TRIANGLES)

        firstid = tri_count
        if tris is not None:
            for t in tris.iterchildren(_TRIANGLE):
                code = t.get("paint_color")
                if code is not None:
                    merged.painted += 1
                    if code:
                        merged.paint_codes.add(code)
                tri_count += 1

        if tri_count > firstid:
            part = part_for(leaf_idx, leaf_key[1])
            merged.volumes.append(
                _Volume(
                    firstid=firstid,
                    lastid=tri_count - 1,
                    leaf_oid=leaf_key[1],
                    name=(part.name if part else "") or leaf.get("name", ""),
                    extruder=part.extruder if part else None,
                )
            )
    return merged


# -- streaming XML output -----------------------------------------------------
# The merged meshes are written straight into the zip entry instead of being built
# as a second lxml tree: for large painted models that copy (plus its serialized
# bytes) cost more memory than the parsed source itself. The output is formatted
# exactly as lxml's pretty printer would format the equivalent tree.
_MESH = ns.q(ns.CORE, "mesh")
_VERTICES = ns.q(ns.CORE, "vertices")
_VERTEX = ns.q(ns.CORE, "vertex")
_TRIANGLES = ns.q(ns.CORE, "triangles")
_TRIANGLE = ns.q(ns.CORE, "triangle")
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_RESOURCES_MARK = "panda2prusa:resources"

_ATTR_ESCAPES = str.maketrans(
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "\n": "&#10;", "\r": "&#13;", "\t": "&#9;"}
)
_needs_escape = re.compile('[&<>"\n\r\t]').search


def _esc(value: str) -> str:
    """Escape an attribute value the way libxml2 serializes it."""
    return value.translate(_ATTR_ESCAPES) if _needs_escape(value) else value


class _ChunkedWriter:
    """Batch many small string writes into large UTF-8 writes on a binary stream."""

    def __init__(self, raw, limit: int = 1 << 20):
        self._raw, self._limit = raw, limit
        self._buf: list = []
        self._size = 0

    def write(self, text: str) -> None:
        self._buf.append(text)
        self._size += len(text)
        if self._size >= self._limit:
            self.flush()

    def flush(self) -> None:
        if self._buf:
            self._raw.write("".join(self._buf).encode("utf-8"))
            self._buf.clear()
            self._size = 0


def _extra_triangle_attrs(t, recode) -> tuple[str, str]:
    """Serialize a triangle's attributes beyond v1..v3 as (xmlns declarations, attributes)."""
    out: dict = {}
    for attr, val in t.attrib.items():
        if attr in ("v1", "v2", "v3") or attr in _DROP_ATTRS:
            continue
        if attr.startswith("{" + ns.BAMBU + "}"):
            continue
        if attr == "paint_color":
            out[_paint_attr()] = val
        else:
            out[attr] = val
    if recode is not None and out.get(_paint_attr()):
        out[_paint_attr()] = recode(out[_paint_attr()])

    decls, parts = [], []
    declared: dict = {}
    for attr, val in out.items():
        if attr.startswith("{"):
            uri, local = attr[1:].split("}", 1)
            if uri == ns.SLIC3RPE:
                prefix = "slic3rpe"
            elif uri == _XML_NS:
                prefix = "xml"
            else:
                prefix = declared.get(uri)
                if prefix is None:
                    prefix = declared[uri] = f"ns{len(declared)}"
                    decls.append(f' xmlns:{prefix}="{_esc(uri)}"')
            attr = f"{prefix}:{local}"
        parts.append(f' {attr}="{_esc(val)}"')
    return "".join(decls), "".join(parts)


def _write_object(w, mo: _MergedObject, oid: str, objs, recode) -> None:
    """Stream one merged <object>: all leaves' vertices, then all their triangles."""
    name = f' name="{_esc(mo.name)}"' if mo.name else ""
    w(f'    <object type="model"{name} id="{oid}">\n      <mesh>\n')

    vbases = []
    vbase = 0
    opened = False
    for leaf_key, xf in mo.leaves:
        vbases.append(vbase)
        verts = objs[leaf_key].find(_MESH).find(_VERTICES)
        if verts is None:
            continue
        identity = xf.is_identity(tol=1e-9)
        for v in verts.iterchildren(_VERTEX):
            if not opened:
                w("        <vertices>\n")
                opened = True
            if identity:
                x, y, z = _esc(v.get("x", "0")), _esc(v.get("y", "0")), _esc(v.get("z", "0"))
            else:
                x, y, z = (
                    _fmt(c)
                    for c in xf.apply_point(
                        float(v.get("x", "0")), float(v.get("y", "0")), float(v.get("z", "0"))
                    )
                )
            w(f'          <vertex x="{x}" y="{y}" z="{z}"/>\n')
            vbase += 1
    w("        </vertices>\n" if opened else "        <vertices/>\n")

    opened = False
    for (leaf_key, xf), base in zip(mo.leaves, vbases):
        tris = objs[leaf_key].find(_MESH).find(_TRIANGLES)
        if tris is None:
            continue
        flip = xf.det3() < 0  # reflection: reverse winding to keep outward normals
        for t in tris.iterchildren(_TRIANGLE):
            if not opened:
                w("        <triangles>\n")
                opened = True
            v1, v2, v3 = t.get("v1"), t.get("v2"), t.get("v3")
            if flip:
                v1, v2 = v2, v1
            idx = f'v1="{int(v1) + base}" v2="{int(v2) + base}" v3="{int(v3) + base}"'
            if len(t.attrib) > 3:
                decls, extra = _extra_triangle_attrs(t, recode)
                w(f"          <triangle{decls} {idx}{extra}/>\n")
            else:
                w(f"          <triangle {idx}/>\n")
    w("        </triangles>\n" if opened else "        <triangles/>\n")
    w("      </mesh>\n    </object>\n")


def _paint_attr():
    return ns.q(ns.SLIC3RPE, "mmu_segmentation")


def _used_filaments(project: BambuProject, merged: dict) -> set[int]:
    """Filament slots the planned objects use: object/volume extruders and paint states."""
    used: set[int] = set()
    paint_strings: set[str] = set()
    for key, mo in merged.items():
        so = project.settings_objects.get(key[1])
        if so and so.extruder:
            try:
                used.add(int(so.extruder))
            except ValueError:
                pass
        for vol in mo.volumes:
            if vol.extruder:
                try:
                    used.add(int(vol.extruder))
                except ValueError:
                    pass
        paint_strings |= mo.paint_codes
    for code in paint_strings:
        try:
            used |= used_states(code) - {0}
        except PaintError:
            pass
    used.discard(0)
    return used


def _compute_extruder_map(used: set[int]) -> Optional[dict]:
    """Map the filaments actually used to sequential extruders 1..K.

    Bambu AMS slots are sparse (a two-color print may use filaments 1 and 3); Prusa
    profiles only have extruders 1..N, so anything past the physical tool count is
    silently unprintable. Compacting {1, 3} -> {1: 1, 3: 2} makes a K-color model
    land on the first K tools. Returns None when already sequential.
    """
    if not used:
        return None
    mapping = {old: i for i, old in enumerate(sorted(used), start=1)}
    if all(old == new for old, new in mapping.items()):
        return None
    return mapping


def extruder_conflicts(used, mapping: dict) -> list[str]:
    """Describe extruders that more than one used filament would be mapped to.

    Filaments missing from ``mapping`` keep their own number, so mapping 2 -> 4 also
    conflicts with an unmapped filament 4.
    """
    by_extruder: dict[int, list[int]] = {}
    for f in sorted(used):
        by_extruder.setdefault(int(mapping.get(f, f)), []).append(f)
    problems = []
    for ext, fils in sorted(by_extruder.items()):
        if len(fils) > 1:
            names = ", ".join(map(str, fils[:-1])) + f" and {fils[-1]}"
            both = "both" if len(fils) == 2 else "all"
            problems.append(f"Filaments {names} are {both} mapped to extruder {ext}.")
    return problems


def _apply_extruder_map(merged: dict, mapping: dict):
    """Rewrite volume extruders; return a cached recoder for paint bitstreams."""
    for mo in merged.values():
        for vol in mo.volumes:
            if vol.extruder:
                try:
                    vol.extruder = str(mapping.get(int(vol.extruder), int(vol.extruder)))
                except ValueError:
                    pass

    recoded: dict[str, str] = {}

    def recode(code: str) -> str:
        if code not in recoded:
            try:
                recoded[code] = transform_states(code, mapping)
            except PaintError:
                recoded[code] = code  # leave unreadable paint untouched
        return recoded[code]

    return recode


def write_prusa_3mf(
    project: BambuProject,
    out_path: str,
    plates: Optional[list] = None,
    extruder_map: Optional[dict] = None,
    compact_extruders: bool = True,
) -> WriteStats:
    objs, src_path = _collect_objects(project)

    # Which build items (and thus root objects) to include, filtered by plate.
    selected_items = []
    for bi in project.build_items:
        plate = project.plate_of_object.get(bi.objectid)
        if plates is None or plate is None or plate in plates:
            selected_items.append(bi)

    # Plan each distinct build object once, preserving first-seen order.
    merged: dict[tuple[str, str], _MergedObject] = {}
    id_map: dict[tuple[str, str], str] = {}
    for bi in selected_items:
        key = _key(ROOT_MODEL, bi.objectid)
        if key in merged or key not in objs:
            continue
        so = project.settings_objects.get(bi.objectid)
        mo = _plan_object(key, objs, src_path, so.parts if so else [])
        if mo is not None:
            merged[key] = mo
            id_map[key] = str(len(id_map) + 1)

    # Renumber filament slots: an explicit map wins; otherwise compact sparse
    # slots so a K-color model lands on extruders 1..K.
    if extruder_map is not None:
        extruder_map = {int(k): int(v) for k, v in extruder_map.items()}
        problems = extruder_conflicts(_used_filaments(project, merged), extruder_map)
        if problems:
            raise ValueError(" ".join(problems))
        extruder_map = {k: v for k, v in extruder_map.items() if k != v} or None
    elif compact_extruders:
        extruder_map = _compute_extruder_map(_used_filaments(project, merged))
    recode = _apply_extruder_map(merged, extruder_map) if extruder_map else None

    # Build the small parts of the model as a tree, with a marker where the
    # streamed objects go.
    nsmap = {None: ns.CORE, "slic3rpe": ns.SLIC3RPE}
    model = ET.Element(ns.q(ns.CORE, "model"), nsmap=nsmap)
    model.set("unit", "millimeter")
    model.set("{http://www.w3.org/XML/1998/namespace}lang", "en-US")

    _add_metadata(model, project)

    resources = ET.SubElement(model, ns.q(ns.CORE, "resources"))
    if merged:
        resources.append(ET.Comment(_RESOURCES_MARK))

    build = ET.SubElement(model, ns.q(ns.CORE, "build"))
    items_written = 0
    for bi in selected_items:
        key = _key(ROOT_MODEL, bi.objectid)
        if key not in id_map:
            continue
        item = ET.SubElement(build, ns.q(ns.CORE, "item"))
        item.set("objectid", id_map[key])
        if bi.transform.strip():
            item.set("transform", bi.transform)
        item.set("printable", "1" if bi.printable else "0")
        items_written += 1

    skeleton = ET.tostring(
        model, xml_declaration=True, encoding="UTF-8", pretty_print=True
    ).decode("utf-8")
    head, mark, tail = skeleton.partition(f"    <!--{_RESOURCES_MARK}-->\n")

    model_config = _build_model_config(project, merged, id_map, extruder_map)

    # Assemble the package.
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        z.writestr("_rels/.rels", _rels_xml())
        # The model's size isn't known up front, so allow it to exceed 2 GiB.
        with z.open(ROOT_MODEL, "w", force_zip64=True) as raw:
            out = _ChunkedWriter(raw)
            out.write(head)
            if mark:
                for key, mo in merged.items():
                    _write_object(out.write, mo, id_map[key], objs, recode)
            out.write(tail)
            out.flush()
        if model_config:
            z.writestr("Metadata/Slic3r_PE_model.config", model_config)
        # Pass through a thumbnail if we have a plausible one.
        thumb = _pick_thumbnail(project)
        if thumb:
            z.writestr("Metadata/thumbnail.png", thumb)

    return WriteStats(
        objects_written=len(merged),
        volumes_written=sum(len(mo.volumes) for mo in merged.values()),
        build_items_written=items_written,
        painted_triangles=sum(mo.painted for mo in merged.values()),
        plates_included=plates,
        extruder_map=extruder_map,
    )


def _add_metadata(model, project: BambuProject) -> None:
    def meta(name, text):
        m = ET.SubElement(model, ns.q(ns.CORE, "metadata"))
        m.set("name", name)
        m.text = text

    meta("slic3rpe:Version3mf", "1")
    meta("slic3rpe:MmPaintingVersion", "1")
    # Carry over a title if present.
    title = ""
    for md in project.root_tree.getroot().findall(ns.q(ns.CORE, "metadata")):
        if md.get("name") == "Title" and md.text:
            title = md.text
    if title:
        meta("Title", title)
    meta("Application", "panda2prusa")


def _cfg_meta(parent, mtype: str, mkey: str, value: str) -> None:
    m = ET.SubElement(parent, "metadata")
    m.set("type", mtype)
    m.set("key", mkey)
    m.set("value", value)


def _build_model_config(
    project: BambuProject, merged: dict, id_map: dict, extruder_map: Optional[dict] = None
) -> Optional[bytes]:
    """Emit PrusaSlicer object/volume config: names + per-volume extruders.

    Volumes are identified by firstid/lastid triangle ranges over the merged mesh,
    matching PrusaSlicer's native writer, so extruder assignments survive import.
    """
    root = ET.Element("config")
    for key, mo in merged.items():
        old_id = key[1]
        so = project.settings_objects.get(old_id)
        obj_el = ET.SubElement(root, "object")
        obj_el.set("id", id_map[key])
        name = (so.name if so else "") or mo.name
        if name:
            _cfg_meta(obj_el, "object", "name", name)
        if so and so.extruder:
            ext = so.extruder
            if extruder_map:
                try:
                    ext = str(extruder_map.get(int(ext), int(ext)))
                except ValueError:
                    pass
            _cfg_meta(obj_el, "object", "extruder", ext)
        for vol in mo.volumes:
            vol_el = ET.SubElement(obj_el, "volume")
            vol_el.set("firstid", str(vol.firstid))
            vol_el.set("lastid", str(vol.lastid))
            _cfg_meta(vol_el, "volume", "name", vol.name or name or f"part {vol.leaf_oid}")
            _cfg_meta(vol_el, "volume", "volume_type", "model_part")
            if vol.extruder:
                _cfg_meta(vol_el, "volume", "extruder", vol.extruder)
    if len(root) == 0:
        return None
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Relationships xmlns="{ns.RELS}">\n'
        f' <Relationship Target="/{ROOT_MODEL}" Id="rel-1" Type="{ns.REL_TYPE_STARTPART}"/>\n'
        "</Relationships>\n"
    )


def _pick_thumbnail(project: BambuProject) -> Optional[bytes]:
    # Prefer a plate thumbnail, then any thumbnail.
    for name, data in project.raw_entries.items():
        low = name.lower()
        if "plate_1.png" in low and "small" not in low and "no_light" not in low:
            return data
    for name, data in project.raw_entries.items():
        if "thumbnail" in name.lower():
            return data
    return None
