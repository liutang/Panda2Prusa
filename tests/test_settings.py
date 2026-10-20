"""Bambu / Orca object and part settings -> PrusaSlicer settings."""

from panda2prusa.settings import Warnings, translate


def _object(settings, **kw):
    w = Warnings()
    return translate(settings, "Obj", w, True, **kw), w.lines()


def _part(settings, **kw):
    w = Warnings()
    return translate(settings, "Obj", w, False, **kw), w.lines()


def test_renamed_keys_keep_their_values():
    out, warnings = _part({"wall_loops": "4", "sparse_infill_density": "30%", "top_shell_layers": "5",
                           "bridge_flow": "1.3", "ironing_flow": "17%"})
    assert out == {"perimeters": "4", "fill_density": "30%", "top_solid_layers": "5",
                   "bridge_flow_ratio": "1.3", "ironing_flowrate": "17%"}
    assert warnings == []


def test_metadata_that_is_not_a_setting_is_ignored():
    out, warnings = _part({"name": "cube", "matrix": "1 0 0 0", "extruder": "2", "source_object_id": "0"})
    assert out == {} and warnings == []


def test_per_nozzle_values_keep_the_first():
    out, _ = _object({"inner_wall_speed": "150,nil", "outer_wall_speed": "100"})
    assert out == {"perimeter_speed": "150", "external_perimeter_speed": "100"}


def test_infill_patterns():
    out, warnings = _part({"sparse_infill_pattern": "zig-zag", "top_surface_pattern": "monotonicline",
                           "bottom_surface_pattern": "concentric"})
    assert out == {"fill_pattern": "rectilinear", "top_fill_pattern": "monotoniclines",
                   "bottom_fill_pattern": "concentric"}
    assert warnings == []
    assert _part({"sparse_infill_pattern": "tri-hexagon"})[0] == {"fill_pattern": "stars"}
    out, warnings = _part({"sparse_infill_pattern": "crosshatch"})
    assert out == {} and len(warnings) == 1 and "crosshatch" in warnings[0]


def test_brim_types():
    assert _object({"brim_type": "outer_only"}) == ({"brim_type": "outer_only"}, [])
    for value in ("brim_ears", "painted"):
        out, warnings = _object({"brim_type": value})
        assert out == {"brim_type": "no_brim"}
        assert len(warnings) == 1 and "removed" in warnings[0]
    out, warnings = _object({"brim_type": "auto_brim"})
    assert out == {} and len(warnings) == 1 and "print preset" in warnings[0]


def test_ironing_type_also_switches_ironing():
    assert _part({"ironing_type": "topmost"})[0] == {"ironing": "1", "ironing_type": "topmost"}
    assert _part({"ironing_type": "no ironing"})[0] == {"ironing": "0"}


def test_supports():
    assert _object({"enable_support": "1", "support_type": "tree(auto)"})[0] == {
        "support_material": "1", "support_material_auto": "1", "support_material_style": "organic"}
    assert _object({"support_type": "normal(manual)"})[0] == {
        "support_material_auto": "0", "support_material_style": "grid"}
    assert _object({"support_type": "normal(auto)", "support_style": "snug"})[0] == {
        "support_material_auto": "1", "support_material_style": "snug"}
    assert _object({"support_style": "tree_hybrid"})[0] == {"support_material_style": "organic"}
    assert _object({"support_style": "default"})[0] == {}


def test_fuzzy_skin():
    assert _part({"fuzzy_skin": "external"})[0] == {"fuzzy_skin": "external"}
    assert _part({"fuzzy_skin": "all"})[0] == {"fuzzy_skin": "external"}
    assert _part({"fuzzy_skin": "allwalls"})[0] == {"fuzzy_skin": "all"}
    out, warnings = _part({"fuzzy_skin": "hole"})
    assert out == {} and len(warnings) == 1


def test_line_width_percentages_become_mm():
    out, _ = _part({"outer_wall_line_width": "0.45", "inner_wall_line_width": "112.5%"}, nozzle_diameter=0.4)
    assert out == {"external_perimeter_extrusion_width": "0.45", "perimeter_extrusion_width": "0.45"}
    out, warnings = _part({"inner_wall_line_width": "112.5%"})
    assert out == {} and len(warnings) == 1


def test_support_filaments_are_renumbered():
    out, _ = _object({"support_filament": "3", "support_interface_filament": "0"},
                     remap_filament=lambda f: {3: 2}.get(f, f))
    assert out == {"support_material_extruder": "2", "support_material_interface_extruder": "0"}


def test_whole_object_settings_are_not_set_on_parts():
    out, warnings = _part({"enable_support": "1", "wall_loops": "3"})
    assert out == {"perimeters": "3"}
    assert len(warnings) == 1 and "whole objects" in warnings[0]


def test_unknown_settings_are_reported_once_per_message():
    w = Warnings()
    translate({"skin_infill_density": "6%"}, "A", w, True)
    translate({"skin_infill_density": "6%"}, "B", w, True)
    assert w.lines() == ['Setting skin_infill_density = 6% has no PrusaSlicer equivalent, not converted ("A", "B")']
