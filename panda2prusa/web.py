"""Web interface: python -m panda2prusa.web

A small, stateless FastAPI app for running the converter from a browser (e.g. in a
homelab container). The browser keeps the uploaded file and sends it twice: once to
``/api/inspect`` to learn its plates/filaments, then to ``/api/convert`` with the chosen
options. Nothing is kept on disk between requests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from . import __version__, ns
from .convert import convert_file, describe, suggest_extruder_map, used_filaments

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_UPLOAD_MB = int(os.environ.get("P2P_MAX_UPLOAD_MB", "300"))
_CHUNK = 1024 * 1024

app = FastAPI(title="Panda2Prusa", version=__version__, docs_url="/api/docs", redoc_url=None)


# -- helpers ------------------------------------------------------------------
def _save_upload(upload: UploadFile, workdir: str) -> str:
    """Stream an upload into ``workdir`` enforcing the size limit; return its path."""
    path = os.path.join(workdir, "input.3mf")
    limit = MAX_UPLOAD_MB * _CHUNK
    size = 0
    with open(path, "wb") as fh:
        while chunk := upload.file.read(_CHUNK):
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"File exceeds the {MAX_UPLOAD_MB} MB upload limit.")
            fh.write(chunk)
    if size == 0:
        raise HTTPException(400, "Uploaded file is empty.")
    return path


def _read_project(path: str):
    try:
        return describe(path)
    except zipfile.BadZipFile:
        raise HTTPException(400, "Not a valid .3mf file (it is not a zip archive).")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _slot_info(project, slot: int) -> dict:
    color = None
    if 0 < slot <= len(project.filament_colors):
        c = project.filament_colors[slot - 1]
        color = c[:7] if c.startswith("#") and len(c) >= 7 else c
    ftype = project.filament_types[slot - 1] if 0 < slot <= len(project.filament_types) else None
    return {"slot": slot, "color": color, "type": ftype}


def _parse_plates(text: str) -> Optional[list]:
    text = (text or "").strip().lower()
    if not text or text == "all":
        return None
    try:
        plates = [int(p) for p in text.replace(",", " ").split()]
    except ValueError:
        raise HTTPException(400, "Plates must be 'all' or numbers like 1 or 1,2.")
    return plates or None


def _parse_mapping(mode: str, mapping: str) -> Optional[dict]:
    """``auto`` -> None (compact), ``keep`` -> {}, ``custom`` -> parsed JSON map."""
    mode = (mode or "auto").lower()
    if mode == "auto":
        return None
    if mode == "keep":
        return {}
    if mode != "custom":
        raise HTTPException(400, f"Unknown mapping mode {mode!r}.")
    try:
        raw = json.loads(mapping or "{}")
        result = {int(k): int(v) for k, v in raw.items()}
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Extruder mapping must be a JSON object like {\"3\": 2}.")
    if any(k < 1 or v < 1 for k, v in result.items()):
        raise HTTPException(400, "Filament and extruder numbers must be 1 or greater.")
    return result


def _output_name(filename: Optional[str]) -> str:
    base = os.path.splitext(os.path.basename(filename or "model.3mf"))[0] or "model"
    return f"{base}_prusa.3mf"


def _content_disposition(name: str) -> str:
    ascii_name = re.sub(r'[^A-Za-z0-9._-]+', "_", name)
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


# -- routes -------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/config")
def config() -> dict:
    return {"version": __version__, "max_upload_mb": MAX_UPLOAD_MB}


@app.post("/api/inspect")
def inspect(file: UploadFile = File(...)) -> dict:
    with tempfile.TemporaryDirectory(prefix="p2p-") as workdir:
        project = _read_project(_save_upload(file, workdir))

    counts: dict = {}
    for plate in project.plate_of_object.values():
        counts[plate] = counts.get(plate, 0) + 1
    painted = sum(
        1
        for tree in [project.root_tree, *project.object_trees.values()]
        for tri in tree.getroot().iter(ns.q(ns.CORE, "triangle"))
        if "paint_color" in tri.attrib
    )
    used = used_filaments(project)
    return {
        "filename": file.filename,
        "output_name": _output_name(file.filename),
        "producer": project.producer or None,
        "model_parts": len(project.object_trees) + 1,
        "build_items": len(project.build_items),
        "plates": [{"id": p, "objects": counts.get(p, 0)} for p in project.plate_ids],
        "filaments": [_slot_info(project, f) for f in used],
        "suggested_map": {str(k): v for k, v in suggest_extruder_map(used).items()},
        "painted_triangles": painted,
    }


@app.post("/api/convert")
def convert(
    file: UploadFile = File(...),
    plates: str = Form("all"),
    mapping_mode: str = Form("auto"),
    mapping: str = Form(""),
) -> Response:
    plate_list = _parse_plates(plates)
    extruder_map = _parse_mapping(mapping_mode, mapping)

    workdir = tempfile.mkdtemp(prefix="p2p-")
    cleanup = BackgroundTask(_rmtree, workdir)
    try:
        inp = _save_upload(file, workdir)
        _read_project(inp)  # clean 400s for unreadable input
        out = os.path.join(workdir, "output.3mf")
        result = convert_file(inp, out, plates=plate_list, extruder_map=extruder_map)
    except HTTPException:
        _rmtree(workdir)
        raise
    except Exception as exc:
        _rmtree(workdir)
        raise HTTPException(500, f"Conversion failed: {exc}")

    stats = asdict(result.stats)
    if stats.get("extruder_map"):
        stats["extruder_map"] = {str(k): v for k, v in stats["extruder_map"].items()}
    if result.stats.objects_written == 0:
        _rmtree(workdir)
        raise HTTPException(422, "No objects were written — check the plate selection.")

    name = _output_name(file.filename)
    return FileResponse(
        out,
        media_type="application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
        headers={
            "Content-Disposition": _content_disposition(name),
            "X-Panda2Prusa-Stats": json.dumps(stats, separators=(",", ":")),
        },
        background=cleanup,
    )


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def _rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("P2P_HOST", "0.0.0.0"),
        port=int(os.environ.get("P2P_PORT", "8543")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
