"""
Environment bootstrap for the Catchment Runoff pipeline.

`qgis`, `processing` (a QGIS plugin), and `pcraster` are installed inside the
`geo_env` conda environment, but a plain Python process cannot see them
unless the environment has been activated first (conda's `activate.d`
scripts set PYTHONPATH/PATH/QGIS_PREFIX_PATH for `geo_env` specifically).
VS Code does this automatically for its integrated terminal when `geo_env`
is the selected interpreter, which is why running a script "through VS
Code" works -- but the exact same script fails with
`ModuleNotFoundError: No module named 'qgis'` (or a misleading PCRaster
"Visual C++ Redistributable" error, which is really just missing DLL search
paths) when launched any other way: a different editor, a plain double
click, a non-activated shell, or another tool driving the interpreter
directly.

Importing this module before any `qgis`/`processing`/`pcraster` import sets
up the same paths explicitly, so the pipeline no longer depends on how (or
by what) it was launched -- only on which interpreter is running it
(it must be `geo_env`'s own python.exe).

Usage: put `import env_bootstrap` as the very first import in any script
that (directly or indirectly, e.g. via importing CatchmentRunoff) ends up
importing `qgis`, `processing`, or `pcraster`.
"""
import os
import sys

_done = False


def _setup():
    global _done
    if _done:
        return
    _done = True

    env_root = sys.prefix
    library_dir = os.path.join(env_root, "Library")
    qgis_python_dir = os.path.join(library_dir, "python")
    qgis_plugins_dir = os.path.join(qgis_python_dir, "plugins")
    lib_bin_dir = os.path.join(library_dir, "bin")
    mingw_bin_dir = os.path.join(library_dir, "mingw-w64", "bin")

    if not os.path.isdir(qgis_python_dir):
        raise RuntimeError(
            f"Expected QGIS's python bindings at {qgis_python_dir!r} but that "
            f"folder doesn't exist. This assumes the running interpreter is "
            f"the 'geo_env' conda environment's own python.exe -- current "
            f"interpreter is {sys.executable!r}. Select the geo_env "
            f"interpreter and try again."
        )

    for path in (qgis_python_dir, qgis_plugins_dir):
        if path not in sys.path:
            sys.path.insert(0, path)

    os.environ.setdefault("QGIS_PREFIX_PATH", library_dir.replace("\\", "/"))

    # Without these, GDAL/pyogrio can't find their own bundled data files
    # (warns "Could not detect GDAL data files") and PROJ falls back to
    # whatever GDAL_DATA/PROJ_DATA happen to be set to elsewhere on the
    # machine -- exactly the kind of cross-environment leak this module
    # exists to prevent. PROJ_DATA (not PROJ_LIB) is the current name;
    # PROJ_LIB is deprecated.
    gdal_data_dir = os.path.join(library_dir, "share", "gdal")
    proj_data_dir = os.path.join(library_dir, "share", "proj")
    if os.path.isdir(gdal_data_dir):
        os.environ.setdefault("GDAL_DATA", gdal_data_dir.replace("\\", "/"))
    if os.path.isdir(proj_data_dir):
        os.environ.setdefault("PROJ_DATA", proj_data_dir.replace("\\", "/"))

    if hasattr(os, "add_dll_directory"):
        for dll_dir in (lib_bin_dir, mingw_bin_dir):
            if os.path.isdir(dll_dir):
                os.add_dll_directory(dll_dir)

    # add_dll_directory alone isn't enough for pcraster: some of the DLLs it
    # depends on (its mingw runtime) are only found via PATH.
    os.environ["PATH"] = os.pathsep.join(
        p for p in (lib_bin_dir, mingw_bin_dir, os.environ.get("PATH", "")) if p
    )


_setup()
