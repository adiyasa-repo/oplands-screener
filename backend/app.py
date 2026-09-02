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
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(BACKEND_DIR, "workdir", "jobs")
RUN_JOB_SCRIPT = os.path.join(BACKEND_DIR, "run_job.py")
os.makedirs(JOBS_DIR, exist_ok=True)

app = FastAPI(title="Oplands-screener API")

# Restrict this to the actual frontend origin once it's deployed (e.g.
# "https://adiyasa-repo.github.io") rather than "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Hard concurrency guard: the pipeline does real, CPU-heavy geoprocessing
# against a shared working directory, so only one job may run at a time.
# Anyone else gets told to wait rather than queued.
_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}


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
            detail="A job is already running. Please wait for it to finish and try again.",
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
