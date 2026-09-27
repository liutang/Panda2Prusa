"""Tests for the web API, using a small synthetic Bambu project.

Skips if the optional web dependencies (fastapi, httpx) are not installed.
"""

import io
import json
import zipfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from panda2prusa.web import app  # noqa: E402

def _make_sample(path):
    C="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    P="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    def cube(paint):
        v="".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x in (0,10) for y in (0,10) for z in (0,10))
        tris=[(0,1,3),(0,3,2),(4,6,7),(4,7,5),(0,4,5),(0,5,1),(2,3,7),(2,7,6),(0,2,6),(0,6,4),(1,5,7),(1,7,3)]
        t="".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"'+(f' paint_color="{paint}"' if i<4 else '')+'/>' for i,(a,b,c) in enumerate(tris))
        return f'<mesh><vertices>{v}</vertices><triangles>{t}</triangles></mesh>'
    objs=f'''<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{C}"><resources>
    <object id="1" type="model">{cube("0C")}</object><object id="2" type="model">{cube("4")}</object></resources><build/></model>'''
    root=f'''<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter" xmlns="{C}" xmlns:p="{P}" requiredextensions="p">
    <metadata name="Application">BambuStudio-01.10.00.00</metadata><resources>
    <object id="3" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="1"/></components></object>
    <object id="4" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="2"/></components></object>
    </resources><build>
    <item objectid="3" transform="1 0 0 0 1 0 0 0 1 100 100 0" printable="1"/>
    <item objectid="4" transform="1 0 0 0 1 0 0 0 1 130 100 0" printable="1"/></build></model>'''
    rels='<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/Objects/object_1.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
    ms='''<?xml version="1.0"?><config>
    <object id="3"><metadata key="name" value="Cube A"/><metadata key="extruder" value="1"/><part id="1"><metadata key="name" value="a"/><metadata key="extruder" value="1"/></part></object>
    <object id="4"><metadata key="name" value="Cube B"/><metadata key="extruder" value="3"/><part id="2"><metadata key="name" value="b"/><metadata key="extruder" value="3"/></part></object>
    <plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="3"/></model_instance></plate>
    <plate><metadata key="plater_id" value="2"/><model_instance><metadata key="object_id" value="4"/></model_instance></plate></config>'''
    ps=json.dumps({"filament_colour":["#FFFFFF","#00FF00","#8E2929"],"filament_type":["PLA","PLA","PETG"]})
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", root)
        z.writestr("3D/Objects/object_1.model", objs)
        z.writestr("3D/_rels/3dmodel.model.rels", rels)
        z.writestr("Metadata/model_settings.config", ms)
        z.writestr("Metadata/project_settings.config", ps)


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    path = tmp_path_factory.mktemp("web") / "sample.3mf"
    _make_sample(path)
    return path.read_bytes()


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def _post(client, url, data, **form):
    return client.post(url, files={"file": ("sample.3mf", data)}, data=form)


def test_index_and_health(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert "Panda2Prusa" in client.get("/").text


def test_inspect(client, sample):
    info = _post(client, "/api/inspect", sample).json()
    assert info["output_name"] == "sample_prusa.3mf"
    assert [p["id"] for p in info["plates"]] == [1, 2]
    assert [f["slot"] for f in info["filaments"]] == [1, 3]
    assert info["filaments"][1]["color"] == "#8E2929"
    assert info["suggested_map"] == {"1": 1, "3": 2}
    assert info["painted_triangles"] == 8


def test_convert_custom_map(client, sample):
    r = _post(client, "/api/convert", sample, mapping_mode="custom", mapping='{"1": 1, "3": 2}')
    assert r.status_code == 200
    assert "sample_prusa.3mf" in r.headers["content-disposition"]
    stats = json.loads(r.headers["x-panda2prusa-stats"])
    assert stats["objects_written"] == 2
    assert stats["extruder_map"] == {"3": 2}
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert "mmu_segmentation" in z.read("3D/3dmodel.model").decode()


def test_convert_single_plate_keep(client, sample):
    r = _post(client, "/api/convert", sample, plates="2", mapping_mode="keep")
    stats = json.loads(r.headers["x-panda2prusa-stats"])
    assert stats["objects_written"] == 1
    assert stats["plates_included"] == [2]
    assert not stats["extruder_map"]


@pytest.mark.parametrize(
    "form, status",
    [
        ({"plates": "9"}, 422),
        ({"plates": "x"}, 400),
        ({"mapping_mode": "custom", "mapping": '{"1": 0}'}, 400),
        ({"mapping_mode": "custom", "mapping": "nope"}, 400),
        ({"mapping_mode": "bogus"}, 400),
    ],
)
def test_convert_rejects_bad_options(client, sample, form, status):
    r = _post(client, "/api/convert", sample, **form)
    assert r.status_code == status
    assert r.json()["error"]


def test_rejects_non_zip(client):
    r = _post(client, "/api/inspect", b"not a zip")
    assert r.status_code == 400
