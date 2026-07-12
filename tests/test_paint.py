"""Paint (multicolor) round-trip tests against a real painted Bambu sample.

Bambu Studio forked PrusaSlicer's TriangleSelector, so `paint_color` and
`slic3rpe:mmu_segmentation` share the same RLE bitstream format and the
conversion is a pure attribute rename. These tests verify that on real data:
the strings survive byte-for-byte in triangle order, and every string decodes
cleanly under PrusaSlicer's deserialization grammar.

Skip automatically if no painted sample is present in samples/.
"""

import os
import zipfile

import lxml.etree as ET
import pytest

from panda2prusa.convert import convert_file, describe, suggest_extruder_map, used_filaments
from panda2prusa.paint import transform_states, used_states

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.environ.get("B2P_SAMPLES", os.path.join(HERE, "samples"))

PAINT_ATTRS = ("paint_color", "mmu_segmentation")


def _paint_seq(path):
    """Ordered (v1, v2, v3, paint) per triangle across all .model parts."""
    seq = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(n for n in z.namelist() if n.endswith(".model")):
            root = ET.fromstring(z.read(name))
            for tri in root.iter("{*}triangle"):
                paint = None
                for k, v in tri.attrib.items():
                    if k.split("}")[-1] in PAINT_ATTRS:
                        paint = v
                seq.append((tri.get("v1"), tri.get("v2"), tri.get("v3"), paint))
    return seq


def _painted_samples():
    if not os.path.isdir(SAMPLES_DIR):
        return []
    found = []
    for name in sorted(os.listdir(SAMPLES_DIR)):
        if not name.lower().endswith(".3mf") or name.startswith("out_"):
            continue
        path = os.path.join(SAMPLES_DIR, name)
        try:
            with zipfile.ZipFile(path) as z:
                # Only Bambu-painted inputs qualify (not converted/Prusa outputs).
                bambu_painted = any(
                    b"paint_color" in z.read(n)
                    for n in z.namelist()
                    if n.endswith(".model")
                )
        except (zipfile.BadZipFile, KeyError):
            continue
        if bambu_painted:
            found.append(path)
    return found


PAINTED = _painted_samples()


@pytest.fixture(params=PAINTED, ids=[os.path.basename(p) for p in PAINTED])
def painted_sample(request):
    return request.param


def decode_states(code):
    """Decode one triangle's paint RLE per PrusaSlicer TriangleSelector::deserialize.

    Nibbles are read from the END of the hex string, bits LSB-first within each
    nibble. Node grammar: 2 bits split-count; 0 -> leaf with 2-bit state
    (0b11 -> 4 more bits, state = ext + 3); else 2 bits special-side then
    split-count+1 child nodes. Returns the set of leaf states.
    """
    bits = []
    for ch in reversed(code):
        n = int(ch, 16)
        bits.extend((n >> i) & 1 for i in range(4))
    pos = 0

    def take(k):
        nonlocal pos
        if pos + k > len(bits):
            raise ValueError("bitstream underrun")
        v = sum(bits[pos + i] << i for i in range(k))
        pos += k
        return v

    states = set()

    def node():
        split = take(2)
        if split == 0:
            s = take(2)
            if s == 0b11:
                s = take(4) + 3
            states.add(s)
        else:
            take(2)  # special side index
            for _ in range(split + 1):
                node()

    node()
    if any(bits[pos:]) and len(bits) - pos >= 4:
        raise ValueError("leftover data bits after root node")
    return states


def test_paint_survives_byte_for_byte(painted_sample, tmp_path):
    # Vertex indices shift when component meshes are merged, but triangle order is
    # preserved, so the per-triangle paint sequence must match exactly.
    # (extruder_map={} keeps original slot numbers, i.e. no bitstream rewriting.)
    out = tmp_path / "out.3mf"
    convert_file(painted_sample, str(out), extruder_map={})
    src, dst = _paint_seq(painted_sample), _paint_seq(str(out))
    assert len(src) == len(dst)
    assert [t[3] for t in src] == [t[3] for t in dst], "paint strings changed"
    assert any(t[3] for t in dst)


def test_paint_attr_is_renamed_and_namespaced(painted_sample, tmp_path):
    out = tmp_path / "out.3mf"
    convert_file(painted_sample, str(out))
    with zipfile.ZipFile(out) as z:
        root = ET.fromstring(z.read("3D/3dmodel.model"))
    slic3rpe = "{http://schemas.slic3r.org/3mf/2017/06}mmu_segmentation"
    painted = [t for t in root.iter("{*}triangle") if t.get(slic3rpe)]
    assert painted, "no slic3rpe:mmu_segmentation triangles in output"
    for tri in painted:
        assert "paint_color" not in tri.attrib


def test_volume_ranges_and_extruders_survive(painted_sample, tmp_path):
    """Per-part extruders must land in Slic3r_PE_model.config as firstid/lastid volumes.

    PrusaSlicer only honors part extruders in its merged-mesh volume format; this is
    what makes multicolor parts (e.g. text on a sign) keep their filament assignment.
    """
    out = tmp_path / "out.3mf"
    convert_file(painted_sample, str(out))
    with zipfile.ZipFile(out) as z:
        cfg = ET.fromstring(z.read("Metadata/Slic3r_PE_model.config"))
        model = ET.fromstring(z.read("3D/3dmodel.model"))

    tri_count = sum(1 for _ in model.iter("{*}triangle"))
    for obj in cfg.findall("object"):
        vols = obj.findall("volume")
        assert vols, "object has no volume entries"
        expected_first = 0
        for vol in vols:
            first, last = int(vol.get("firstid")), int(vol.get("lastid"))
            assert first == expected_first, "volume ranges must be contiguous from 0"
            assert last >= first
            expected_first = last + 1
        assert expected_first == tri_count, "volume ranges must cover every triangle"

    extruders = {
        m.get("value")
        for m in cfg.iter("metadata")
        if m.get("type") == "volume" and m.get("key") == "extruder"
    }
    assert extruders, "no per-volume extruder assignments survived conversion"


def test_paint_transcoder_unit():
    """'0C' encodes a whole-triangle leaf painted with state 3 (extended encoding)."""
    assert used_states("0C") == {3}
    # 3 -> 2 shrinks to the short 2-bit state form: bits 00 (leaf) + state 2
    # LSB-first (0,1) = nibble 0b1000 = "8"
    assert transform_states("0C", {3: 2}) == "8"
    assert used_states("8") == {2}
    # identity mapping is a byte-for-byte no-op
    assert transform_states("0C", {}) == "0C"


def test_paint_transcoder_identity_on_real_strings(painted_sample):
    """Re-encoding every real paint string with no remap must reproduce it exactly."""
    codes = {p for *_v, p in _paint_seq(painted_sample) if p}
    assert codes
    for code in codes:
        assert transform_states(code, {}) == code


def test_extruder_compaction_rewrites_paint_and_volumes(painted_sample, tmp_path):
    """Default conversion maps sparse filaments (e.g. {1,3}) onto extruders 1..K."""
    used = used_filaments(describe(painted_sample))
    mapping = suggest_extruder_map(used)
    out = tmp_path / "out.3mf"
    convert_file(painted_sample, str(out))

    # Paint states must land exactly on the compacted extruder numbers.
    paint_states = set()
    for *_v, code in _paint_seq(str(out)):
        if code:
            paint_states |= used_states(code)
    assert paint_states - {0} <= set(mapping.values())

    # Volume extruder assignments must be remapped the same way.
    with zipfile.ZipFile(out) as z:
        cfg = ET.fromstring(z.read("Metadata/Slic3r_PE_model.config"))
    vol_extruders = {
        int(m.get("value"))
        for m in cfg.iter("metadata")
        if m.get("key") == "extruder"
    }
    assert vol_extruders <= set(mapping.values())
    # Everything used fits in the first K tools.
    assert (paint_states - {0}) | vol_extruders <= set(range(1, len(used) + 1))


def test_paint_decodes_under_prusa_grammar(painted_sample, tmp_path):
    out = tmp_path / "out.3mf"
    convert_file(painted_sample, str(out))
    states = set()
    for *_v, paint in _paint_seq(str(out)):
        if paint:
            states |= decode_states(paint)  # raises on malformed input
    painted_states = states - {0}
    assert painted_states, "paint decodes but selects no non-base filament"
    assert all(0 < s <= 16 for s in painted_states)
