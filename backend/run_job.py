"""
Runs one analysis job in its own process, invoked as a subprocess by
app.py. This isolation is deliberate, not incidental: PyQGIS/GDAL/PCRaster
are not safe to touch from a background Python thread inside the same
process as the FastAPI/uvicorn event loop (confirmed by testing -- doing
so segfaults mid-run). A subprocess per job means the API server itself
never imports QGIS/GDAL at all.

Usage:
    <geo_env python.exe> run_job.py <input_geometry.json> <output_result.json>

input_geometry.json: a GeoJSON Polygon/MultiPolygon geometry, WGS84.
output_result.json: written on completion --
    {"status": "done", "layers": {...}} or {"status": "error", "error": "..."}
"""
import os
import sys
import shutil
import json
import tempfile
import traceback

import geopandas as gpd
from shapely.geometry import shape

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
SCRIPTS_DIR = os.path.join(REPO_ROOT, "Scripts")
BASE_DATA_INPUT = os.path.join(REPO_ROOT, "Data", "Input")

# Deliberately NOT inside the repo by default: on a machine where the repo
# lives in a cloud-synced folder (OneDrive, Dropbox, ...), files the
# pipeline creates/deletes here get caught mid-sync and locked -- hit this
# twice in local testing (a shutil.rmtree "Access is denied" on a folder
# OneDrive was still indexing). A plain temp directory sidesteps that
# entirely and works identically on the production server (no sync client
# there at all). Override with OPLANDS_WORKDIR if you want it elsewhere.
WORKDIR = os.environ.get("OPLANDS_WORKDIR") or os.path.join(tempfile.gettempdir(), "oplands-screener-workdir")

sys.path.insert(0, SCRIPTS_DIR)

import env_bootstrap  # noqa: E402  (must be imported before any qgis/pcraster import)
from qgis.core import QgsApplication  # noqa: E402
from qgis.analysis import QgsNativeAlgorithms  # noqa: E402

PIPELINE_CRS = "EPSG:25832"
WEB_CRS = "EPSG:4326"

_FIXED_INPUT_FILES = [
    "id15.gpkg",
    "DTM_ID15_BURN_CLIP.tif",
    "DTM_ID15_BURN_CLIP.tif.aux.xml",
    "LDD25832.map",
    "LDD25832.map.aux.xml",
]

# Pipeline output -> web-facing layer name. Names match the QGIS project's
# own layer names exactly, so the map legend and the desktop project agree.
_RESULT_LAYERS = {
    "Opland": ("Output", "cloudburst_catchment.gpkg"),
    "Vandveje (ID15)": ("Output", "resampled_channels_points_60.gpkg"),
    "Vandveje (Opland)": ("Output", "cloudburst_streams_60.gpkg"),
    "Bluespot": ("Output", "Bluespot_Opland.gpkg"),
    "Selected ID15": ("Temporary", "selected_ID15.gpkg"),
}


def _seed_workdir():
    input_dir = os.path.join(WORKDIR, "Input")
    os.makedirs(input_dir, exist_ok=True)

    for name in _FIXED_INPUT_FILES:
        src = os.path.join(BASE_DATA_INPUT, name)
        dst = os.path.join(input_dir, name)
        if os.path.isfile(src) and not os.path.isfile(dst):
            shutil.copy2(src, dst)

    qml_dst = os.path.join(input_dir, "qml_styles")
    if not os.path.isdir(qml_dst):
        shutil.copytree(os.path.join(BASE_DATA_INPUT, "qml_styles"), qml_dst)


def _write_aoi(geometry: dict, input_dir: str) -> str:
    gdf = gpd.GeoDataFrame(geometry=[shape(geometry)], crs=WEB_CRS)
    gdf = gdf.to_crs(PIPELINE_CRS)
    aoi_path = os.path.join(input_dir, "Input_polygon.gpkg")
    gdf.to_file(aoi_path, driver="GPKG")
    return aoi_path


def _layer_to_geojson(path: str) -> dict:
    gdf = gpd.read_file(path)
    gdf = gdf.to_crs(WEB_CRS)
    return json.loads(gdf.to_json())


def main(geometry_path: str, result_path: str):
    with open(geometry_path) as f:
        geometry = json.load(f)

    QgsApplication.setPrefixPath(os.environ["QGIS_PREFIX_PATH"], True)
    qgs = QgsApplication([], False)
    qgs.initQgis()
    QgsApplication.processingRegistry().addProvider(QgsNativeAlgorithms())

    try:
        _seed_workdir()
        input_dir = os.path.join(WORKDIR, "Input")
        aoi_path = _write_aoi(geometry, input_dir)

        from CatchmentRunoff import run_geospatial_analysis

        run_geospatial_analysis(
            input_directory=WORKDIR,
            num_top_stream_order_values_1=4,
            num_top_stream_order_values_2=4,
            selected_rainfall_scenario=60,
            crs=PIPELINE_CRS,
            qgis_project_name="analysis",
            shp_extract_path=aoi_path,
            shp_id15_path=os.path.join(input_dir, "id15.gpkg"),
            input_raster_path=os.path.join(input_dir, "LDD25832.map"),
            dtm_raster_path=os.path.join(input_dir, "DTM_ID15_BURN_CLIP.tif"),
        )

        layers = {}
        for layer_name, (subdir, filename) in _RESULT_LAYERS.items():
            path = os.path.join(WORKDIR, subdir, filename)
            layers[layer_name] = _layer_to_geojson(path) if os.path.isfile(path) else None

        with open(result_path, "w") as f:
            json.dump({"status": "done", "layers": layers}, f)

    except Exception as e:
        with open(result_path, "w") as f:
            json.dump({"status": "error", "error": str(e), "traceback": traceback.format_exc()}, f)
    finally:
        qgs.exitQgis()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
