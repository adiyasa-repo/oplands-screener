"""
Oplands-screener API: accepts a polygon (drawn or selected on the map),
runs the real catchment/bluespot analysis pipeline, and returns the
result layers as GeoJSON.

Each job runs in its own subprocess (run_job.py), not a background thread
in this process -- PyQGIS/GDAL/PCRaster are not safe to touch from a
worker thread sharing a process with the FastAPI/uvicorn event loop
(confirmed by testing: doing so segfaults mid-run). This API process
itself never imports QGIS/GDAL.

Run locally:
    python -m uvicorn app:app --reload --port 8000
(must be run with the geo_env conda environment's own python.exe, since
that's also the interpreter used to launch each job's subprocess)
"""
import os
import sys
import json
import uuid
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BACKEND_DIR, "workdir", "jobs")
RUN_JOB_SCRIPT = os.path.join(BACKEND_DIR, "run_job.py")
os.makedirs(JOBS_DIR, exist_ok=True)

app = FastAPI(title="Oplands-screener API")

# The deployed frontend shares its origin with this API (Caddy proxies
# opland.adiyasa.dk/api/* here), so production traffic never triggers CORS
# at all -- this only matters for local development, where the frontend's
# preview server and this API run on two different ports. allow_origin_regex
# covers that without hardcoding a port number: the frontend dev server's
# port is picked dynamically (see .claude/launch.json's autoPort), so a
# fixed allow_origins entry would silently stop matching whenever it moves.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://opland.adiyasa.dk"],
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Hard concurrency guard: the pipeline does real, CPU-heavy geoprocessing
# against a shared working directory, so only one job may run at a time.
# Anyone else gets told to wait rather than queued.
_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}


# --- Basemap tile proxy --------------------------------------------------
#
# The aerial-photo basemap comes from Dataforsyningen (Danish public
# geodata), which requires an API key. That key is a credential -- already
# treated as one in Scripts/utils.py (kept out of source control) -- so it
# must never reach the public frontend JS or the public repo. This
# endpoint holds the key server-side and proxies WMS tile requests: the
# frontend points Leaflet's tileLayer.wms at this URL instead of at
# Dataforsyningen directly, and the key never leaves the server.

SCRIPTS_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "Scripts")
DATAFORSYNINGEN_ORTOFOTO_WMS = "https://wms.datafordeler.dk/GeoDanmarkOrto/orto_foraar/1.0.0/WMS"


def _dataforsyningen_apikey() -> str:
    # Same lookup order as Scripts/utils.py's get_dataforsyningen_apikey(),
    # reimplemented rather than imported so this lightweight API process
    # never pulls in qgis.core/pcraster/geopandas at import time (see the
    # module docstring on why that's deliberately avoided here).
    env_file = os.path.join(SCRIPTS_DIR, ".env")
    if os.path.isfile(env_file):
        with open(env_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "DATAFORSYNINGEN_API_KEY" and value.strip():
                    return value.strip()

    env_value = os.environ.get("DATAFORSYNINGEN_API_KEY")
    if env_value:
        return env_value

    # Deliberately no QGIS-settings fallback here (unlike utils.py's tier
    # 3): this process never runs env_bootstrap, so `import qgis` isn't
    # reachable from it -- set DATAFORSYNINGEN_API_KEY in Scripts/.env or
    # as a real environment variable instead.
    raise HTTPException(
        status_code=503,
        detail="Ortofoto-baggrundskort er ikke konfigureret (mangler Dataforsyningen API-nøgle på serveren).",
    )


@app.get("/tiles/ortofoto")
def ortofoto_tile(request: Request):
    apikey = _dataforsyningen_apikey()
    params = dict(request.query_params)
    params["apikey"] = apikey
    url = f"{DATAFORSYNINGEN_ORTOFOTO_WMS}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            content = resp.read()
            content_type = resp.headers.get("Content-Type", "image/png")
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Kunne ikke hente ortofoto-tile: {e}")
    return Response(content=content, media_type=content_type)


def _extract_geometry(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    if body.get("type") == "Feature" and isinstance(body.get("geometry"), dict):
        return body["geometry"]
    if body.get("type") in ("Polygon", "MultiPolygon") and "coordinates" in body:
        return body
    raise ValueError("Request body must be a GeoJSON Polygon/MultiPolygon geometry or Feature")


def _run_job(job_id: str, geometry: dict):
    geometry_path = os.path.join(JOBS_DIR, f"{job_id}_geometry.json")
    result_path = os.path.join(JOBS_DIR, f"{job_id}_result.json")
    with open(geometry_path, "w") as f:
        json.dump(geometry, f)

    try:
        proc = subprocess.run(
            [sys.executable, RUN_JOB_SCRIPT, geometry_path, result_path],
            capture_output=True, text=True,
        )
        if os.path.isfile(result_path):
            with open(result_path) as f:
                _jobs[job_id] = json.load(f)
        else:
            _jobs[job_id] = {
                "status": "error",
                "error": "Job process produced no result.",
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-4000:],
            }
    except Exception as e:
        _jobs[job_id] = {"status": "error", "error": str(e)}
    finally:
        _lock.release()


@app.post("/analyze")
def analyze(body: dict):
    try:
        geometry = _extract_geometry(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not _lock.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="En analyse kører allerede. Vent til den er færdig, og prøv igen.",
        )

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "running"}
    threading.Thread(target=_run_job, args=(job_id, geometry), daemon=True).start()
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job


@app.get("/health")
def health():
    return {"status": "ok"}
