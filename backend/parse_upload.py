"""
Parses a user-uploaded vector file (GeoJSON, GeoPackage, or a zipped
Shapefile) into a GeoJSON FeatureCollection the frontend can render and
let the user click through, run as its own subprocess by app.py -- the
same isolation reasoning as run_job.py: this touches geopandas/GDAL, and
that's not safe from a thread sharing a process with the FastAPI/uvicorn
event loop. Deliberately lighter-weight than run_job.py's pipeline
though: no QGIS, no PCRaster, just geopandas reading a file and
reprojecting it -- so this finishes in well under a second for any
file within the upload size cap, not the 30-60s a real analysis takes.

Usage:
    <geo_env python.exe> parse_upload.py <input_file> <output_result.json>

input_file: the uploaded file, saved with its original extension so GDAL's
    own format sniffing (which leans on the extension) works correctly.
    A zipped Shapefile must end in .zip.
output_result.json: written on completion --
    {"status": "done", "features": [...], "feature_count": N,
     "dropped_non_polygon_count": N}
    or {"status": "error", "error": "..."}
"""
import os
import sys
import json
import traceback

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "Scripts")
sys.path.insert(0, SCRIPTS_DIR)

# Doesn't touch qgis/processing/pcraster at all, only geopandas/GDAL -- but
# env_bootstrap is still required first, not optional: without the
# GDAL_DATA/PROJ_DATA it sets, reprojecting an uploaded file's CRS to
# WGS84 can silently fall back to whatever PROJ data happens to be lying
# around elsewhere on the machine instead of erroring, which is exactly
# the cross-environment leak that module exists to prevent (see its own
# docstring). Confirmed by testing: running this without it produces a
# real "Could not detect GDAL data files" warning, not just a theoretical
# risk.
import env_bootstrap  # noqa: E402
import geopandas as gpd  # noqa: E402

SUPPORTED_EXTENSIONS = {".geojson", ".json", ".gpkg", ".zip"}


def _read_source(input_path: str) -> gpd.GeoDataFrame:
    ext = os.path.splitext(input_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Filtypen {ext or '(ingen)'} understøttes ikke. "
            "Understøttede formater: GeoJSON (.geojson/.json), "
            "GeoPackage (.gpkg), eller en zippet Shapefile (.zip)."
        )
    # GDAL's zip virtual filesystem -- geopandas/Fiona/pyogrio all resolve
    # this to whatever vector file is inside the archive automatically, no
    # need to know the Shapefile's own basename in advance.
    read_path = f"zip://{input_path}" if ext == ".zip" else input_path
    try:
        gdf = gpd.read_file(read_path)
    except Exception as e:
        raise ValueError(
            f"Filen kunne ikke læses som {ext.lstrip('.')}. "
            f"Er du sikker på formatet er korrekt? ({e})"
        )
    if gdf.crs is None:
        raise ValueError(
            "Filen har ingen defineret koordinatsystem (CRS), så den kan "
            "ikke placeres korrekt på kortet. Tilføj en .prj-fil (for "
            "Shapefiles) eller gem med et defineret CRS."
        )
    return gdf


def main(input_path: str, result_path: str):
    gdf = _read_source(input_path)

    total_features = len(gdf)
    # Only polygons make sense as an analysis area -- points/lines in an
    # uploaded file (mixed-geometry files are common, e.g. a survey export
    # with markers alongside the plots) are dropped rather than causing a
    # hard failure, but the count is reported so the frontend can tell the
    # user honestly rather than silently showing fewer shapes than expected.
    is_polygonal = gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    dropped = int((~is_polygonal).sum())
    gdf = gdf[is_polygonal].copy()

    if len(gdf) == 0:
        raise ValueError(
            "Filen indeholder ingen polygoner. "
            f"({total_features} objekt(er) fundet, men ingen var polygoner.)"
        )

    if gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")

    # A sensible per-feature label, tried in rough order of how likely a
    # real-world file is to have it -- falls back to a plain index rather
    # than failing, since an uploaded file's schema is unpredictable by
    # definition (unlike Jordstykker/Kloakområder/Lokalplanområder, which
    # each have one known, fixed schema this app already relies on).
    label_field_candidates = [
        "navn", "name", "label", "plannavn", "matrikelnr", "id", "objectid", "fid",
    ]
    lower_cols = {c.lower(): c for c in gdf.columns if c != "geometry"}
    label_field = next((lower_cols[c] for c in label_field_candidates if c in lower_cols), None)

    features = []
    for i, row in enumerate(gdf.itertuples(index=False), start=1):
        row_dict = row._asdict()
        geometry = row_dict.pop("geometry")
        label = str(row_dict.get(label_field)) if label_field and row_dict.get(label_field) not in (None, "") else f"Polygon {i}"
        features.append({
            "type": "Feature",
            "geometry": json.loads(gpd.GeoSeries([geometry]).to_json())["features"][0]["geometry"],
            "properties": {"label": label},
        })

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({
            "status": "done",
            "features": features,
            "feature_count": len(features),
            "dropped_non_polygon_count": dropped,
        }, f)


if __name__ == "__main__":
    input_path, result_path = sys.argv[1], sys.argv[2]
    try:
        main(input_path, result_path)
    except Exception as e:
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump({
                "status": "error",
                "error": str(e),
                "traceback": traceback.format_exc(),
            }, f)
