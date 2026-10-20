"""Part subtypes (modifiers, negative volumes, support blockers...) survive conversion."""

import zipfile

import lxml.etree as ET

from panda2prusa.convert import convert_file

C = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
P = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def _cube(offset):
    v = "".join(
        f'<vertex x="{x + offset}" y="{y}" z="{z}"/>' for x in (0, 10) for y in (0, 10) for z in (0, 10)
    )
    tris = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
            (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in tris)
    return f"<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh>"


# (subtype attribute or None, expected PrusaSlicer volume_type)
PARTS = [
    (None, "ModelPart"),
    ("normal_part", "ModelPart"),
    ("modifier_part", "ParameterModifier"),
    ("negative_part", "NegativeVolume"),
    ("support_enforcer", "SupportEnforcer"),
    ("support_blocker", "SupportBlocker"),
    ("precise_seam_center", "ParameterModifier"),
]


def _make_sample(path):
    leaves = "".join(f'<object id="{i + 1}" type="model">{_cube(i * 20)}</object>' for i in range(len(PARTS)))
    objs = f'<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{C}"><resources>{leaves}</resources><build/></model>'
    comps = "".join(
        f'<component p:path="/3D/Objects/object_1.model" objectid="{i + 1}"/>' for i in range(len(PARTS))
    )
    root = f'''<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{C}" xmlns:p="{P}" requiredextensions="p">
    <metadata name="Application">BambuStudio-01.10.00.00</metadata><resources>
    <object id="100" type="model"><components>{comps}</components></object>
    </resources><build><item objectid="100" transform="1 0 0 0 1 0 0 0 1 100 100 0" printable="1"/></build></model>'''
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/Objects/object_1.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    parts = "".join(
        f'<part id="{i + 1}"' + (f' subtype="{subtype}"' if subtype else "") + f'><metadata key="name" value="p{i + 1}"/>'
        + ('<metadata key="sparse_infill_density" value="40%"/><metadata key="wall_loops" value="5"/>' if subtype == "modifier_part" else "")
        + "</part>"
        for i, (subtype, _) in enumerate(PARTS)
    )
    ms = f'''<?xml version="1.0"?><config>
    <object id="100"><metadata key="name" value="Obj"/><metadata key="extruder" value="1"/>
    <metadata key="enable_support" value="0"/><metadata key="brim_type" value="brim_ears"/>{parts}</object>
    <plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="100"/></model_instance></plate></config>'''
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", root)
        z.writestr("3D/Objects/object_1.model", objs)
        z.writestr("3D/_rels/3dmodel.model.rels", rels)
        z.writestr("Metadata/model_settings.config", ms)


def test_part_subtypes_become_prusa_volume_types(tmp_path):
    src, out = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _make_sample(src)
    convert_file(str(src), str(out))
    with zipfile.ZipFile(out) as z:
        config = ET.fromstring(z.read("Metadata/Slic3r_PE_model.config"))
    types = [
        md.get("value")
        for vol in config.iter("volume")
        for md in vol.findall("metadata")
        if md.get("key") == "volume_type"
    ]
    assert types == [expected for _, expected in PARTS]


def test_object_and_modifier_settings_are_converted(tmp_path):
    src, out = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _make_sample(src)
    result = convert_file(str(src), str(out))
    with zipfile.ZipFile(out) as z:
        config = ET.fromstring(z.read("Metadata/Slic3r_PE_model.config"))

    def settings(el, kind):
        return {md.get("key"): md.get("value") for md in el.findall("metadata") if md.get("type") == kind}

    obj = config.find("object")
    assert settings(obj, "object")["support_material"] == "0"
    assert settings(obj, "object")["brim_type"] == "no_brim"
    modifier = [v for v in obj.iter("volume") if settings(v, "volume")["volume_type"] == "ParameterModifier"][0]
    assert settings(modifier, "volume")["fill_density"] == "40%"
    assert settings(modifier, "volume")["perimeters"] == "5"
    assert any("brim_ears" in w for w in result.stats.warnings)
