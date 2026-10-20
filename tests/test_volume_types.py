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


def _add_to_sample(path, entries):
    with zipfile.ZipFile(path, "a") as z:
        for name, data in entries.items():
            z.writestr(name, data)


def _converted(tmp_path, entries):
    src, out = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _make_sample(src)
    _add_to_sample(src, entries)
    result = convert_file(str(src), str(out))
    return result, zipfile.ZipFile(out)


def test_layer_height_profile_that_fits_is_converted(tmp_path):
    # The printed parts are 10 mm cubes; the first layer matches the project's.
    result, z = _converted(tmp_path, {
        "Metadata/layer_heights_profile.txt": "object_id=1|0.000000;0.200000;5.000000;0.100000;10.000000;0.100000\n",
        "Metadata/project_settings.config": '{"initial_layer_print_height": "0.2"}',
    })
    assert z.read("Metadata/Slic3r_PE_layer_heights_profile.txt").decode() == (
        "object_id=1|0.000000;0.200000;5.000000;0.100000;10.000000;0.100000\n")
    assert not any("layer height" in w for w in result.stats.warnings)


def test_layer_height_profile_for_another_first_layer_is_kept_with_a_warning(tmp_path):
    result, z = _converted(tmp_path, {
        "Metadata/layer_heights_profile.txt": "object_id=1|0;0.2;5;0.1;10;0.1\n",
        "Metadata/project_settings.config": '{"initial_layer_print_height": "0.25"}',
    })
    assert "Metadata/Slic3r_PE_layer_heights_profile.txt" in z.namelist()
    assert any("set the first layer height to 0.2 mm" in w for w in result.stats.warnings)


def test_layer_height_profile_that_does_not_fit_is_left_out(tmp_path):
    result, z = _converted(tmp_path, {"Metadata/layer_heights_profile.txt": "object_id=1|0;0.2;6;0.1;12;0.1\n"})
    assert "Metadata/Slic3r_PE_layer_heights_profile.txt" not in z.namelist()
    assert any("12 mm tall, the object is 10 mm" in w for w in result.stats.warnings)


def test_height_ranges_are_converted_with_translated_settings(tmp_path):
    result, z = _converted(tmp_path, {"Metadata/layer_config_ranges.xml": (
        '<?xml version="1.0" encoding="utf-8"?><objects><object id="1"><range min_z="0" max_z="4">'
        '<option opt_key="extruder">0</option><option opt_key="layer_height">0.12</option>'
        '<option opt_key="wall_loops">4</option></range></object></objects>')})
    ranges = ET.fromstring(z.read("Metadata/Prusa_Slicer_layer_config_ranges.xml"))
    rng = ranges.find("object").find("range")
    assert ranges.find("object").get("id") == "1"
    assert (rng.get("min_z"), rng.get("max_z")) == ("0", "4")
    assert {o.get("opt_key"): o.text for o in rng.findall("option")} == {
        "extruder": "0", "layer_height": "0.12", "perimeters": "4"}
