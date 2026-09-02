import env_bootstrap  # noqa: F401  (must be imported before any qgis/pcraster import)
import geopandas as gpd
import pcraster as pcr
import subprocess
import time
import os
from shapely.geometry import Polygon
from osgeo import osr
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsVectorFileWriter,
    QgsProcessingFeedback,
    QgsProject
)
import processing

def raster_to_points(input_raster_path, output_vector_path, crs):    
    params = {
        'INPUT_RASTER': input_raster_path,
        'FIELD_NAME': 'value',
        'OUTPUT': 'TEMPORARY_OUTPUT'
    }    
    feedback = QgsProcessingFeedback()
    result = processing.run('native:pixelstopoints', params, feedback=feedback)

    layer = result['OUTPUT']
    if not layer.isValid():
        raise Exception("Layer is not valid")

    print("Layer is valid. Setting CRS.")
    layer.setCrs(QgsCoordinateReferenceSystem(crs))

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    QgsVectorFileWriter.writeAsVectorFormatV3(layer, output_vector_path, QgsProject.instance().transformContext(), options)

    return output_vector_path

# Convert GeoTIFF to PCRaster format using gdal_translate
def convert_tif_to_map(tif_path, map_path):
    command = ['gdal_translate', '-of', 'PCRaster', tif_path, map_path]
    try:
        subprocess.run(command, check=True)
        print("Conversion successful: GeoTIFF to PCRaster format.")
    except subprocess.CalledProcessError as e:
        print(f"Error during conversion: {e}")
        exit(1) # Stop the script if conversion fails

# Set the coordinate system using gdal_translate
def set_coordinate_system(map_path, epsg_code):
    # Extract the base name and directory from the map_path
    base_name = os.path.basename(map_path)
    dir_name = os.path.dirname(map_path)
    # Create a new output path by appending a suffix to the base name
    output_path = os.path.join(dir_name, base_name.replace('.map', '25832.map'))
    command = ['gdal_translate', '-of', 'PCRaster', '-a_srs', f'EPSG:{epsg_code}', map_path, output_path]
    try:
        subprocess.run(command, check=True)
        return output_path # Return the new output path
    except subprocess.CalledProcessError as e:
        print(f"Error setting coordinate system: {e}")
        exit(1) # Stop the script if setting coordinate system fails

# Function to record the current time
def record_time():
    return time.time()

# Function to print the time taken for a step
def print_step_time(step_name, start_time):
    print(f"Time taken for {step_name}: {time.time() - start_time:.2f} seconds")

# Function to look up stream order in the lookup table
def lookup_stream_order(streamorder_raster_path, lookup_table_path):
    start_time = record_time()
    streamorder_ordinal = pcr.lookupordinal(lookup_table_path, streamorder_raster_path)
    print_step_time("stream order lookup", start_time)
    return streamorder_ordinal

# Function to perform downstream operation
def downstream(ldd_raster_path, channels_raster_path):
    start_time = record_time()
    ldd_raster = pcr.readmap(ldd_raster_path)
    channels_raster = pcr.readmap(channels_raster_path)
    downstream_raster = pcr.downstream(ldd_raster, channels_raster)
    print_step_time("downstream operation", start_time)
    return downstream_raster

# Function to dissolve polygons without holes
def dissolve_polygon(input_path, output_path, verbose=True):
    start_time = record_time()
    input_polygon = gpd.read_file(input_path)
    dissolved_polygon = input_polygon.union_all()
    dissolved_polygon = Polygon(dissolved_polygon.exterior)
    gpd.GeoDataFrame(geometry=[dissolved_polygon]).to_file(output_path)
    if verbose:
        print_step_time("dissolve polygons", start_time)

def resample_points(input_points_path, raster_path, output_path, crs):    
    params = {
        'INPUT': input_points_path,
        'RASTERCOPY': raster_path,
        'COLUMN_PREFIX': 'resampled_',
        'OUTPUT': 'TEMPORARY_OUTPUT'
    }
    
    result = processing.run('qgis:rastersampling', params)

    layer = result['OUTPUT']
    if not layer.isValid():
        raise Exception("Layer is not valid")

    print("Layer is valid. Setting CRS.")
    layer.setCrs(QgsCoordinateReferenceSystem(crs))

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = 'GPKG'
    options.fileEncoding = 'UTF-8'
    QgsVectorFileWriter.writeAsVectorFormatV3(layer, output_path, QgsProject.instance().transformContext(), options)
    
    return output_path

# Function to get the Dataforsyningen API key for the basemap WMS layers
def get_dataforsyningen_apikey():
    # 1. Explicit local override: Scripts/.env (DATAFORSYNINGEN_API_KEY=...).
    #    Kept out of source control (see .gitignore) since it's a credential.
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_file):
        with open(env_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == "DATAFORSYNINGEN_API_KEY" and value.strip():
                    return value.strip()

    # 2. A real environment variable, if set.
    env_value = os.environ.get("DATAFORSYNINGEN_API_KEY")
    if env_value:
        return env_value

    # 3. Fall back to whatever the interactive QGIS desktop app has configured
    #    for the Dataforsyningen plugin. QGIS stores plugin settings in a
    #    profile-specific INI file whose folder name is tied to the QGIS
    #    major version (QGIS3, QGIS4, ...), so scan for whichever exists
    #    rather than hardcoding one -- a QGIS version bump would otherwise
    #    silently break this.
    from qgis.PyQt.QtCore import QSettings
    import glob

    appdata = os.environ.get("APPDATA", "")
    candidates = glob.glob(os.path.join(appdata, "QGIS", "QGIS*", "profiles", "*", "QGIS", "QGIS*.ini"))
    candidates.sort(key=os.path.getmtime, reverse=True)
    for ini_path in candidates:
        settings = QSettings(ini_path, QSettings.IniFormat)
        value = settings.value("plugins/Dataforsyningen/datafordeler_apikey", "")
        if value:
            return value

    raise RuntimeError(
        "Could not find a Dataforsyningen API key. Set DATAFORSYNINGEN_API_KEY "
        f"in {env_file!r}, as an environment variable, or configure it in "
        "QGIS under Settings > Options > Dataforsyningen."
    )

# Function to delineate streams
def delineate_streams(ldd_raster_path, temporary_directory, num_top_stream_order_values):
    # Derive LDD name from the input path
    ldd_name = os.path.splitext(os.path.basename(ldd_raster_path))[0]
    
    # Paths for intermediate files
    output_streamorder_path = os.path.join(temporary_directory, f"streamorder_{ldd_name}_L{num_top_stream_order_values}.map")
    lookup_table_path = os.path.join(temporary_directory, f"lookupchannels_12_{ldd_name}_L{num_top_stream_order_values}.csv")
    channels_output_path = os.path.join(temporary_directory, f"channels_{ldd_name}_L{num_top_stream_order_values}.map")
    
    # Step 1: Run streamorder on the repaired LDD raster
    start_time = record_time()
    print("Running streamorder on the repaired LDD raster...")
    ldd_raster = pcr.readmap(ldd_raster_path)
    streamorder_raster = pcr.streamorder(ldd_raster)
    print_step_time("running streamorder", start_time)

    # Step 2: Save streamorder raster
    start_time = record_time()
    print("Saving the streamorder raster...")
    pcr.report(streamorder_raster, output_streamorder_path)
    print_step_time("saving the streamorder raster", start_time)

    # Step 3: Get top N unique stream order values
    start_time = record_time()
    print(f"Getting top {num_top_stream_order_values} unique stream order values...")
    streamorder_values = pcr.pcr2numpy(streamorder_raster, -9999)
    unique_values = set(streamorder_values.flatten())
    sorted_values = sorted(unique_values, reverse=True)
    top_values = sorted_values[:num_top_stream_order_values]
    print_step_time(f"getting top {num_top_stream_order_values} unique stream order values", start_time)

    # Step 4: Write top N stream order values to CSV
    start_time = record_time()
    print(f"Writing top {num_top_stream_order_values} stream order values to a CSV file...")
    with open(lookup_table_path, "w") as f:
        for index, value in enumerate(reversed(top_values), start=1):
            f.write(f"{value} {index}\n")
    print_step_time(f"writing top {num_top_stream_order_values} stream order values to CSV", start_time)

    # Step 5: Perform stream order lookup
    start_time = record_time()
    print("Performing stream order lookup...")
    streamorder_ordinal = lookup_stream_order(output_streamorder_path, lookup_table_path)
    print_step_time("performing stream order lookup", start_time)

    # Step 6: Save channels map
    start_time = record_time()
    print("Saving channels map...")
    pcr.report(streamorder_ordinal, channels_output_path)
    print_step_time("saving channels map", start_time)
    
    return channels_output_path

