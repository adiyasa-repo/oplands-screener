"""
Merges the individual municipal drainage-plan polygons under Data/TestAreas
(each a separate "Input_polygon.gpkg", one per plan area) into a single
GeoPackage layer, "Kloakoplande" ("drainage plan areas"), used as the
selectable plan-areas layer for the web frontend.

Run once (or re-run if TestAreas changes):
    python merge_plandata.py
"""
import env_bootstrap  # noqa: F401  (must be imported before any qgis/pcraster import)
import geopandas as gpd
import pandas as pd
import os


def merge_plandata(testareas_dir, output_path, layer_name="Kloakoplande"):
    entries = sorted(
        (e for e in os.listdir(testareas_dir) if os.path.isdir(os.path.join(testareas_dir, e))),
        key=lambda e: int(e) if e.isdigit() else e,
    )

    gdfs = []
    for entry in entries:
        gpkg_path = os.path.join(testareas_dir, entry, "Input_polygon.gpkg")
        if not os.path.isfile(gpkg_path):
            print(f"Skipping {entry}: no Input_polygon.gpkg found")
            continue
        gdf = gpd.read_file(gpkg_path)
        gdfs.append(gdf)

    if not gdfs:
        raise RuntimeError(f"No Input_polygon.gpkg files found under {testareas_dir}")

    crs = gdfs[0].crs
    for gdf, entry in zip(gdfs, entries):
        if gdf.crs != crs:
            raise RuntimeError(f"CRS mismatch: {entry} is {gdf.crs}, expected {crs}")

    merged = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), geometry="geometry", crs=crs)
    # "fid" is GeoPackage's reserved primary-key column name; the source
    # data already has its own attribute called "fid", so it needs
    # renaming to avoid colliding with GPKG's own feature-id column.
    if "fid" in merged.columns:
        merged = merged.rename(columns={"fid": "fid_orig"})
    merged.to_file(output_path, layer=layer_name, driver="GPKG")
    print(f"Merged {len(merged)} plan areas into {output_path} (layer '{layer_name}')")
    return output_path


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    testareas_dir = os.path.join(base, "..", "Data", "TestAreas")
    output_path = os.path.join(base, "..", "Data", "Input", "Kloakoplande.gpkg")
    merge_plandata(testareas_dir, output_path)
