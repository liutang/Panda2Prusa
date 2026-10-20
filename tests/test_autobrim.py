"""Bambu / Orca automatic brim, approximated for PrusaSlicer."""

import zipfile

import lxml.etree as ET
import pytest

from panda2prusa.autobrim import Footprint, FootprintBuilder, brim_width, material
from panda2prusa.convert import convert_file
from test_volume_types import _make_sample


def _box(x0, y0, x1, y1, z0, z1, inward=False):
    """Triangles of a box, counter-clockwise seen from outside (or from inside)."""
    v = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    tris = []
    for a, b, c, d in quads:
        for t in ((a, b, c), (a, c, d)):
            tris.append(tuple(v[i] for i in (reversed(t) if inward else t)))
    return tris


def _cut(tris, z):
    fp = FootprintBuilder(z)
    for t in tris:
        fp.add_triangle(*t)
    return fp.result()


def test_footprint_of_a_box():
    fp = _cut(_box(0, 0, 20, 10, 0, 5), 0.1)
    assert fp.area == pytest.approx(200)
    assert fp.perimeter == pytest.approx(60)
    assert fp.ixx == pytest.approx(20 * 10 ** 3 / 12)
    assert fp.iyy == pytest.approx(10 * 20 ** 3 / 12)
    assert (fp.width_x, fp.width_y) == pytest.approx((20, 10))


def test_footprint_holes_subtract():
    # A 20 x 20 tube with a 10 x 10 hole: the hole's walls face into the hole.
    fp = _cut(_box(0, 0, 20, 20, 0, 5) + _box(5, 5, 15, 15, 0, 5, inward=True), 0.1)
    assert fp.area == pytest.approx(300)
    assert fp.ixx == pytest.approx((20 ** 4 - 10 ** 4) / 12)


def test_mirrored_winding_is_handled_by_the_caller():
    # Clockwise triangles would give a negative area: no footprint.
    assert _cut(_box(0, 0, 20, 10, 0, 5, inward=True), 0.1) is None


def _square(side):
    return Footprint(area=side * side, perimeter=4 * side, ixx=side ** 4 / 12, iyy=side ** 4 / 12,
                     width_x=side, width_y=side)


def test_brim_width_by_material():
    # A 70.7 mm square base (100 mm diagonal), 30 mm tall, printed slowly.
    base = _square(100 / 2 ** 0.5)
    pla, petg = material("PLA"), material("PETG")
    assert brim_width(base, 30, 50, pla[0], pla[1], 0.45) == 0  # 4 mm: too small, omitted
    assert brim_width(base, 30, 50, petg[0], petg[1], 0.45) == pytest.approx(15.3)  # 16 mm in whole line pairs
    assert material("Unknown") == (1.0, 200.0)


def test_brim_width_is_capped():
    # A large ABS base would want more than 18 mm.
    abs_ = material("ABS")
    assert brim_width(_square(200), 30, 50, abs_[0], abs_[1], 0.5) == pytest.approx(18)


def _auto_brim_conversion(tmp_path, project_settings, scale=1, enable_support="0"):
    src, out = tmp_path / "in.3mf", tmp_path / "out.3mf"
    _make_sample(src, brim="auto_brim", scale=scale, enable_support=enable_support)
    with zipfile.ZipFile(src, "a") as z:
        z.writestr("Metadata/project_settings.config", project_settings)
    result = convert_file(str(src), str(out))
    config = ET.fromstring(zipfile.ZipFile(out).read("Metadata/Slic3r_PE_model.config"))
    obj = {m.get("key"): m.get("value") for m in config.find("object").findall("metadata")}
    return result, obj


def test_auto_brim_in_pla_needs_no_brim(tmp_path):
    # The sample's printed parts are a row of 10 mm cubes.
    result, obj = _auto_brim_conversion(tmp_path, '{"filament_type": ["PLA"], "initial_layer_print_height": "0.2"}')
    assert obj["brim_type"] == "no_brim" and "brim_width" not in obj
    assert any("auto_brim worked out to no brim" in w for w in result.stats.warnings)


def test_auto_brim_in_petg_gets_an_outer_brim(tmp_path):
    # Scaled 5x: two 50 mm cubes 50 mm apart, a 150 x 50 mm footprint. The warping estimate
    # is 158 * 8 / 100 * 2 (PETG) = 25 mm, capped at 18 and rounded down to whole pairs of
    # 0.457 mm brim lines (0.5 mm wide at 0.2 mm first layer): 19 pairs = 17.37 mm.
    result, obj = _auto_brim_conversion(
        tmp_path, '{"filament_type": ["PETG"], "initial_layer_print_height": "0.2", "initial_layer_line_width": "0.5"}',
        scale=5)
    assert obj["brim_type"] == "outer_only"
    assert float(obj["brim_width"]) == pytest.approx(17.37, abs=0.01)
    assert any(f"a {obj['brim_width']} mm outer brim" in w for w in result.stats.warnings)


_PETG = '{"filament_type": ["PETG"], "initial_layer_print_height": "0.2", "initial_layer_line_width": "0.5"'


def test_auto_brim_is_left_out_for_an_object_with_supports(tmp_path):
    # Orca clears the brim where support covers the first layer.
    # The object's own setting wins over the project's.
    result, obj = _auto_brim_conversion(tmp_path, _PETG + ', "support_type": "normal(auto)"}', scale=5, enable_support="1")
    assert obj["brim_type"] == "no_brim"
    assert any("left out because the object has supports" in w for w in result.stats.warnings)


def test_auto_brim_is_left_out_for_an_object_with_a_raft(tmp_path):
    result, obj = _auto_brim_conversion(tmp_path, _PETG + ', "raft_layers": "2"}', scale=5)
    assert obj["brim_type"] == "no_brim"


def test_manual_supports_need_an_enforcer(tmp_path):
    # The sample object has a support enforcer volume, so manual supports are printed.
    result, obj = _auto_brim_conversion(tmp_path, _PETG + ', "support_type": "tree(manual)"}', scale=5, enable_support="1")
    assert obj["brim_type"] == "no_brim"


def test_auto_brim_is_kept_with_supports_off(tmp_path):
    result, obj = _auto_brim_conversion(tmp_path, _PETG + ', "enable_support": "1"}', scale=5, enable_support="0")
    assert obj["brim_type"] == "outer_only"
