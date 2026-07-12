"""Round-trip structural tests against real sample files.

These skip automatically if no sample files are present, so the suite still runs
on machines without them. Drop Bambu/Orca .3mf files into samples/ (not committed),
or point B2P_SAMPLES at a directory of them.
"""

import glob
import os
import re
import zipfile

import pytest

from panda2prusa.convert import convert_file, describe
from panda2prusa import ns

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLES_DIR = os.environ.get("B2P_SAMPLES", os.path.join(_HERE, "samples"))
AVAILABLE = [
    p
    for p in sorted(glob.glob(os.path.join(_SAMPLES_DIR, "*.3mf")))
    if not os.path.basename(p).startswith("out_")
]


@pytest.fixture(params=AVAILABLE, ids=[os.path.basename(p) for p in AVAILABLE])
def sample(request):
    if not AVAILABLE:
        pytest.skip("no sample .3mf files available")
    return request.param


def _read_model(path):
    with zipfile.ZipFile(path) as z:
        return z.read("3D/3dmodel.model").decode("utf-8", "replace")


def test_output_loads_and_is_wellformed(sample, tmp_path):
    out = tmp_path / "out.3mf"
    result = convert_file(sample, str(out))
    assert out.exists()
    assert result.stats.objects_written > 0
    assert result.stats.build_items_written > 0
    # output must be a valid zip with the core parts
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert "3D/3dmodel.model" in names
        assert "[Content_Types].xml" in names
        assert "_rels/.rels" in names


def test_build_transforms_preserved(sample, tmp_path):
    out = tmp_path / "out.3mf"
    convert_file(sample, str(out))

    def transforms(text):
        return sorted(
            re.search(r'transform="([^"]+)"', m).group(1)
            for m in re.findall(r"<item [^>]*>", text)
            if re.search(r'transform="([^"]+)"', m)
        )

    assert transforms(_read_model(sample)) == transforms(_read_model(str(out)))


def test_no_bambu_or_production_leakage(sample, tmp_path):
    out = tmp_path / "out.3mf"
    convert_file(sample, str(out))
    model = _read_model(str(out))
    assert "paint_color" not in model  # renamed to mmu_segmentation
    assert "paint_seam" not in model
    assert "bambulab.com" not in model
    assert 'p:path' not in model  # components inlined -> no cross-file refs


def test_components_reference_earlier_ids(sample, tmp_path):
    """3MF core spec: a component must reference an object defined earlier in the file."""
    out = tmp_path / "out.3mf"
    convert_file(sample, str(out))
    with zipfile.ZipFile(out) as z:
        import lxml.etree as ET

        root = ET.fromstring(z.read("3D/3dmodel.model")).getroottree().getroot()
    seen = set()
    for obj in root.iter(ns.q(ns.CORE, "object")):
        for comp in obj.iter(ns.q(ns.CORE, "component")):
            assert comp.get("objectid") in seen, (
                f"component references id {comp.get('objectid')} not yet defined"
            )
        seen.add(obj.get("id"))


def test_plate_selection_subsets(sample, tmp_path):
    project = describe(sample)
    if len(project.plate_ids) < 2:
        pytest.skip("sample has fewer than 2 plates")
    first = project.plate_ids[0]
    out_all = tmp_path / "all.3mf"
    out_one = tmp_path / "one.3mf"
    r_all = convert_file(sample, str(out_all))
    r_one = convert_file(sample, str(out_one), plates=[first])
    assert r_one.stats.build_items_written < r_all.stats.build_items_written
    assert r_one.stats.build_items_written > 0
