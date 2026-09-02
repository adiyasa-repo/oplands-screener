# Oplands-screener

A geospatial screening tool for cloudburst/drainage risk: given a
catchment area of interest, it delineates the upstream drainage network,
computes flow accumulation under several rainfall scenarios, identifies
low-lying "bluespot" ponding areas within the catchment, and produces a
styled QGIS project with the results.

Built on GDAL, PCRaster, and PyQGIS (headless QGIS processing — no desktop
GUI required to run the analysis).

## Status

The core analysis pipeline is complete and runs end-to-end (desktop/CLI).
A browser-based version — pick a plan area on a map, run the same analysis,
see results live — is in progress; see the project board / commit history
for current state.

## Data

`Data/Input/` contains the source layers the pipeline needs:
- `DTM_ID15_BURN_CLIP.tif` — digital terrain model
- `LDD25832.map` — local drainage direction raster
- `id15.gpkg` — landscape-level catchment classification
- `Input_polygon.gpkg` — an example area of interest
- `Kloakoplande.gpkg` — municipal drainage-plan areas (Vejle Kommune),
  merged from public planning data; each feature carries a `navn1201` plan
  code (e.g. `VL009`)
- `qml_styles/` — QGIS styling for the output layers

## Running it

Requires a conda environment with `qgis`, `gdal`, `pcraster`, and
`geopandas` (see `Scripts/env_bootstrap.py` for how the pipeline locates
QGIS's Python bindings from within that environment).

```
python Scripts/GUI.py
```

Pick an input directory containing an `Input/` folder shaped like the one
above, set the analysis parameters, and run.
