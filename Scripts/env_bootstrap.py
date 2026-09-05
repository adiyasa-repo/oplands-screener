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
(it must be `geo_env`'s own interpreter).

Windows and Linux conda installs lay the same packages out differently, so
the paths below are resolved per-platform:

    Windows                          Linux
    <env>/Library/                   <env>/
    <env>/Library/python/            <env>/share/qgis/python/
    <env>/Library/bin/  (DLLs)       <env>/lib/        (shared objects)
    <env>/Library/share/gdal/        <env>/share/gdal/

Everything below the first row is derived from it, so the platform
difference is confined to `_platform_paths()`.

Usage: put `import env_bootstrap` as the very first import in any script
that (directly or indirectly, e.g. via importing CatchmentRunoff) ends up
importing `qgis`, `processing`, or `pcraster`.
"""
import os
import sys

_done = False


def _posix(path):
    """QGIS/GDAL env vars want forward slashes, including on Windows."""
    return path.replace(os.sep, "/")


def _platform_paths(env_root):
    """Return (qgis_root, qgis_python_dir, native_lib_dirs) for this OS.

    `qgis_root` is what QGIS_PREFIX_PATH must point at, and the parent of
    the shared `share/gdal` + `share/proj` data directories.
    """
    if os.name == "nt":
        qgis_root = os.path.join(env_root, "Library")
        return (
            qgis_root,
            os.path.join(qgis_root, "python"),
            (
                os.path.join(qgis_root, "bin"),
                os.path.join(qgis_root, "mingw-w64", "bin"),
            ),
        )

    # Linux (and macOS) conda: no Library/ level, and QGIS's python bindings
    # ship under share/qgis rather than beside the libraries.
    return (
        env_root,
        os.path.join(env_root, "share", "qgis", "python"),
        (os.path.join(env_root, "lib"),),
    )


def _setup():
    global _done
    if _done:
        return
    _done = True

    env_root = sys.prefix
    qgis_root, qgis_python_dir, native_lib_dirs = _platform_paths(env_root)
    qgis_plugins_dir = os.path.join(qgis_python_dir, "plugins")

    if not os.path.isdir(qgis_python_dir):
        interpreter = "python.exe" if os.name == "nt" else "python"
        raise RuntimeError(
            f"Expected QGIS's python bindings at {qgis_python_dir!r} but that "
            f"folder doesn't exist. This assumes the running interpreter is "
            f"the 'geo_env' conda environment's own {interpreter} -- current "
            f"interpreter is {sys.executable!r}. Select the geo_env "
            f"interpreter and try again."
        )

    for path in (qgis_python_dir, qgis_plugins_dir):
        if path not in sys.path:
            sys.path.insert(0, path)

    os.environ.setdefault("QGIS_PREFIX_PATH", _posix(qgis_root))

    # Without these, GDAL/pyogrio can't find their own bundled data files
    # (warns "Could not detect GDAL data files") and PROJ falls back to
    # whatever GDAL_DATA/PROJ_DATA happen to be set to elsewhere on the
    # machine -- exactly the kind of cross-environment leak this module
    # exists to prevent. PROJ_DATA (not PROJ_LIB) is the current name;
    # PROJ_LIB is deprecated.
    gdal_data_dir = os.path.join(qgis_root, "share", "gdal")
    proj_data_dir = os.path.join(qgis_root, "share", "proj")
    if os.path.isdir(gdal_data_dir):
        os.environ.setdefault("GDAL_DATA", _posix(gdal_data_dir))
    if os.path.isdir(proj_data_dir):
        os.environ.setdefault("PROJ_DATA", _posix(proj_data_dir))

    if hasattr(os, "add_dll_directory"):
        for dll_dir in native_lib_dirs:
            if os.path.isdir(dll_dir):
                os.add_dll_directory(dll_dir)

    # Windows: add_dll_directory alone isn't enough for pcraster -- some of
    # the DLLs it depends on (its mingw runtime) are only found via PATH.
    # Linux: conda's shared objects are located via RPATH, so lib/ does not
    # belong on PATH; what we want there is the env's bin/, so any pcraster
    # or GDAL command line tool the pipeline shells out to resolves to this
    # environment's copy rather than a system-wide one.
    if os.name == "nt":
        path_prefix_dirs = native_lib_dirs
    else:
        path_prefix_dirs = (os.path.join(env_root, "bin"),)

    os.environ["PATH"] = os.pathsep.join(
        p for p in (*path_prefix_dirs, os.environ.get("PATH", "")) if p
    )


_setup()
