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
import time
import uuid
import threading
import subprocess
import tempfile
import urllib.request
import urllib.parse
import urllib.error
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BACKEND_DIR, "workdir", "jobs")
UPLOADS_DIR = os.path.join(BACKEND_DIR, "workdir", "uploads")
RUN_JOB_SCRIPT = os.path.join(BACKEND_DIR, "run_job.py")
PARSE_UPLOAD_SCRIPT = os.path.join(BACKEND_DIR, "parse_upload.py")
os.makedirs(JOBS_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

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
# When each job started, kept separately rather than as a field inside the
# job dict itself -- that dict is returned to the client as-is from
# GET /jobs/{id}, and an internal timestamp has no business leaking into
# that response.
_job_created: Dict[str, float] = {}

# _jobs never had anything removed from it, so every result ever computed
# -- full GeoJSON layers included, tens of thousands of features per job
# -- stayed resident in memory for the server's entire uptime. Confirmed
# on the live server, not a hypothetical: a few days of ordinary testing
# alone reached 2.3GB resident on a 4GB box while completely idle. The
# matching geometry/result files under JOBS_DIR had the same problem on
# disk (261MB across 29 jobs at the time this was found).
#
# A single job here takes 30-60+ seconds and the concurrency guard above
# already limits this app to one at a time, so realistic job throughput
# is at most ~100/hour even under nonstop use -- an hour's worth of
# results is a generous window for any client to still be polling a job
# it started, and short enough that ordinary testing/demo use can no
# longer accumulate without bound the way it did before.
_JOB_TTL_SECONDS = 60 * 60


def _evict_old_jobs():
    """Drops any job -- in-memory result and its on-disk geometry/result
    files -- older than _JOB_TTL_SECONDS. Called opportunistically from
    /analyze rather than on a timer: this app already funnels every job
    through that one endpoint under the hard concurrency lock, so a call
    from there happens exactly as often as eviction could ever matter,
    without needing a second execution context (a background thread/timer)
    competing with the same event loop for no real benefit.
    """
    now = time.time()
    expired = [jid for jid, created in _job_created.items() if now - created > _JOB_TTL_SECONDS]
    for jid in expired:
        _jobs.pop(jid, None)
        _job_created.pop(jid, None)
        for suffix in ("_geometry.json", "_result.json"):
            path = os.path.join(JOBS_DIR, f"{jid}{suffix}")
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


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

    _evict_old_jobs()

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {"status": "running"}
    _job_created[job_id] = time.time()
    threading.Thread(target=_run_job, args=(job_id, geometry), daemon=True).start()
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job


# --- Upload-your-own-polygon --------------------------------------------
#
# A third way to pick an analysis area, alongside clicking a reference
# layer or free-drawing: upload a GeoJSON/GeoPackage/zipped-Shapefile
# containing one or more polygons. Parsing happens in its own subprocess
# (parse_upload.py) for the same reason run_job.py does -- geopandas/GDAL
# aren't safe to touch from a thread sharing a process with the
# FastAPI/uvicorn event loop. This is much lighter-weight than an
# analysis job though (no QGIS/PCRaster, just reading and reprojecting a
# file), finishes in well under a second, and the client waits on it
# synchronously -- no job_id/polling needed the way /analyze has.
#
# Mirrors parse_upload.py's own SUPPORTED_EXTENSIONS -- duplicated rather
# than imported from there, deliberately: importing anything from
# parse_upload.py would pull geopandas/GDAL into this process at import
# time, the exact thing this module's own docstring says never to do. A
# short, rarely-changed list is a fine thing to keep in sync by hand
# instead of sharing a live import for.
UPLOAD_ALLOWED_EXTENSIONS = {".geojson", ".json", ".gpkg", ".zip"}
UPLOAD_MAX_BYTES = 40 * 1024 * 1024  # 40MB -- middle of the 20-50MB range asked for


@app.post("/upload-polygon")
def upload_polygon(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in UPLOAD_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Filtypen {ext or '(ingen)'} understøttes ikke. "
                "Understøttede formater: GeoJSON (.geojson/.json), "
                "GeoPackage (.gpkg), eller en zippet Shapefile (.zip)."
            ),
        )

    # The client-supplied filename is used only to read its extension
    # above -- never to construct a path. The file on disk gets a fresh
    # UUID name, so nothing about a crafted filename (path traversal, a
    # sneaky "../../" or similar) can reach the filesystem.
    upload_id = str(uuid.uuid4())
    input_path = os.path.join(UPLOADS_DIR, f"{upload_id}{ext}")
    result_path = os.path.join(UPLOADS_DIR, f"{upload_id}_result.json")

    # Enforced here in code, not just trusted from the Content-Length
    # header (which can be missing or wrong on a malformed/crafted
    # request) -- read in bounded chunks into our own file, and abort the
    # moment the real cumulative size crosses the cap, rather than fully
    # writing an oversized upload to disk first and checking afterward.
    total = 0
    try:
        with open(input_path, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Filen er for stor. Maks {UPLOAD_MAX_BYTES // (1024 * 1024)} MB pr. fil.",
                    )
                out.write(chunk)
    except HTTPException:
        if os.path.isfile(input_path):
            os.remove(input_path)
        raise
    finally:
        file.file.close()

    try:
        subprocess.run(
            [sys.executable, PARSE_UPLOAD_SCRIPT, input_path, result_path],
            capture_output=True, text=True,
        )
        if not os.path.isfile(result_path):
            raise HTTPException(status_code=500, detail="Filen kunne ikke behandles.")
        with open(result_path, encoding="utf-8") as f:
            result = json.load(f)
    finally:
        # Nothing to keep around afterward -- unlike analysis jobs (polled
        # later via GET /jobs/{id}, hence the TTL-based eviction earlier
        # in this file), the client is waiting on this response directly.
        for p in (input_path, result_path):
            if os.path.isfile(p):
                os.remove(p)

    if result.get("status") != "done":
        raise HTTPException(status_code=400, detail=result.get("error", "Filen kunne ikke læses."))

    return result


@app.get("/health")
def health():
    return {"status": "ok"}
