import env_bootstrap  # noqa: F401  (must be imported before any qgis/pcraster import)
import geopandas as gpd
import pcraster as pcr
import time
import os
from osgeo import gdal, ogr, osr
import processing
from utils import record_time, dissolve_polygon, raster_to_points, resample_points, delineate_streams
from create_qgis import create_qgis_project
import shutil

def run_geospatial_analysis(input_directory, num_top_stream_order_values_1, num_top_stream_order_values_2, selected_rainfall_scenario, crs, qgis_project_name, shp_extract_path, shp_id15_path, input_raster_path, dtm_raster_path):
    try:
        # Normalize paths
        input_directory = os.path.normpath(input_directory)
        shp_extract_path = os.path.normpath(shp_extract_path)
        shp_id15_path = os.path.normpath(shp_id15_path)
        input_raster_path = os.path.normpath(input_raster_path)
        dtm_raster_path = os.path.normpath(dtm_raster_path)

        # Validate paths
        if not os.path.exists(input_directory):
            raise FileNotFoundError(f"Input directory does not exist: {input_directory}")
        if not os.path.exists(shp_extract_path):
            raise FileNotFoundError(f"Input polygon file does not exist: {shp_extract_path}")
        if not os.path.exists(shp_id15_path):
            raise FileNotFoundError(f"ID15 file does not exist: {shp_id15_path}")
        if not os.path.exists(input_raster_path):
            raise FileNotFoundError(f"LDD file does not exist: {input_raster_path}")
        if not os.path.exists(dtm_raster_path):
            raise FileNotFoundError(f"DTM file does not exist: {dtm_raster_path}")

        # Set rainfall scenarios matching bluespots
        rainfall_scenarios = [15, 30, 45, 60, 75, 90]

        # Initialize directories and paths
        temporary_directory = os.path.join(input_directory, "Temporary")
        output_directory = os.path.join(input_directory, "Output")
        result_directory = os.path.join(input_directory, "Results")
        qml_directory = os.path.join(input_directory, "Input", "qml_styles")

        # Remove existing directories if they exist
        if os.path.exists(temporary_directory):
            shutil.rmtree(temporary_directory)
        if os.path.exists(output_directory):
            shutil.rmtree(output_directory)
        if os.path.exists(result_directory):
            shutil.rmtree(result_directory)

        # Create directories
        os.makedirs(temporary_directory, exist_ok=True)
        os.makedirs(output_directory, exist_ok=True)
        os.makedirs(result_directory, exist_ok=True)
        os.makedirs(qml_directory, exist_ok=True)

        # Define output file names
        selected_ID15_path = os.path.join(temporary_directory, "selected_ID15.gpkg")
        output_raster_path = os.path.join(temporary_directory, "LDD_ID15.tif")
        translated_raster_path = os.path.join(temporary_directory, "LDD_ID15.map")
        output_pcraster_path = os.path.join(temporary_directory, "LDD_ID15_Repaired.map")
        ID15_channels_output_path = os.path.join(temporary_directory, f"channels_L{num_top_stream_order_values_1}.map")
        # CLOUDBURST CATCHMENT
        spatial_ones_path = os.path.join(temporary_directory, "spatial_ones.map")
        sym_diff_polygon_path = os.path.join(temporary_directory, "sym_diff_polygon.gpkg")
        clipped_ones_path = os.path.join(temporary_directory, "clipped_ones.tif")
        spatial_zeros_path = os.path.join(temporary_directory, "spatial_zeros.map")
        combined_path = os.path.join(temporary_directory, 'combined.tif')
        combined_boolean_path = os.path.join(temporary_directory, 'combined_boolean.map')
        unique_id_path = os.path.join(temporary_directory, "unique_id.map")
        unique_id_nominal_path = os.path.join(temporary_directory, "unique_id_nominal.map")
        covered_map_path = os.path.join(temporary_directory, "covered_map.map")
        catchments_output_path = os.path.join(temporary_directory, "catchments.map")
        selected_catchments_path = os.path.join(temporary_directory, "selected_catchments.gpkg")
        clipped_LDD_output_path = os.path.join(temporary_directory, "LDD_clipped.tif")
        clipped_translated_raster_path = os.path.join(temporary_directory, "LDD_clipped.map")
        clipped_output_pcraster_path = os.path.join(temporary_directory, "LDD_clipped_Repaired.map")
        catchments_vector_output_path = os.path.join(temporary_directory, "catchments_vector.gpkg")
        # ID15 ACCUMULATION
        flow_accumulation_path = os.path.join(temporary_directory, f"flow_accumulation_{selected_rainfall_scenario}.tif")
        # VISUALIZATION
        channels_points_path = os.path.join(input_directory, "Temporary", "channels_points.gpkg")
        upstream_channels_points_path = os.path.join(input_directory, "Temporary", "upstream_channels_points.gpkg")
        resampled_points_path = os.path.join(output_directory, f"resampled_channels_points_{selected_rainfall_scenario}.gpkg")
        upstream_catchment_path = os.path.join(output_directory, "cloudburst_catchment.gpkg")
        upstream_resampled_points_path = os.path.join(output_directory, f"cloudburst_streams_{selected_rainfall_scenario}.gpkg")
        bluespot_path = os.path.join(qml_directory, "Bluespot.gpkg")
        bluespot_clip_path = os.path.join(output_directory, "Bluespot_Opland.gpkg")
        # RESULTS
        qgis_project_path = os.path.join(result_directory, f'{qgis_project_name}.qgz')

        # Define paths to QML style files
        qml_styles = {
            'resampled_points': os.path.join(qml_directory, "ID15Streams.qml"),
            'upstream_resampled_points': os.path.join(qml_directory, "CloudburstStreams.qml"),
            'input_polygon': os.path.join(qml_directory, "InputArea.qml"),
            'upstream_catchment': os.path.join(qml_directory, "UpstreamArea.qml"),
            'selected_ID15': os.path.join(qml_directory, "ID15.qml"),
            'DTM': os.path.join(qml_directory, "DTM.qml")
        }

        # Record the start time for the script
        start_time_script = record_time()
        print("Script started.")

        # Step 1: Read input polygon layers
        print("Step 1/44: Reading input polygon layers...")
        id15_data = gpd.read_file(shp_id15_path)
        overlay_data = gpd.read_file(shp_extract_path)
        print("reading input polygon layers")

        # Step 2: Perform spatial join
        print("Step 2/44: Performing spatial join...")
        extracted_data = gpd.sjoin(id15_data, overlay_data, how='inner', predicate='intersects')
        extracted_data.reset_index(drop=True, inplace=True)
        print("spatial join")

        # Step 3: Save extracted polygons
        print("Step 3/44: Saving extracted polygons...")
        extracted_data = extracted_data.set_crs(crs, allow_override=True)
        extracted_data.to_file(selected_ID15_path, driver="GPKG")
        print("saving extracted polygons")

        # Step 4: Mask the raster
        print("Step 4/44: Masking the raster...")
        warp_options = gdal.WarpOptions(
            srcSRS="EPSG:25832", dstSRS="EPSG:25832",
            cutlineDSName=selected_ID15_path, cropToCutline=True
        )
        gdal.Warp(output_raster_path, input_raster_path, options=warp_options)
        print("masking the raster")

        # Step 5: Translate the raster
        print("Step 5/44: Translating the raster...")
        translate_options = gdal.TranslateOptions(
            format="PCRaster", outputType=gdal.GDT_Byte, noData="none",
            metadataOptions=["PCRASTER_VALUESCALE=VS_LDD"]
        )
        gdal.Translate(translated_raster_path, output_raster_path, options=translate_options)
        print("translating the raster")

        # Step 6: Read translated raster
        print("Step 6/44: Reading translated raster...")
        pcraster_raster = pcr.readmap(translated_raster_path)
        print("reading translated raster")

        # Step 7: Repair the LDD raster
        print("Step 7/44: Repairing the LDD raster...")
        repaired_raster = pcr.lddrepair(pcraster_raster)
        print("repairing the LDD raster")

        # Step 8: Save repaired raster
        print("Step 8/44: Saving the repaired raster...")
        pcr.report(repaired_raster, output_pcraster_path)
        print("saving the repaired raster")

        # Step 9: Delineate streams
        print("Step 9/44: Delineating streams...")
        ID15_channels_output_path = delineate_streams(
            output_pcraster_path, temporary_directory, num_top_stream_order_values_1
        )
        print("delineating streams")

        # Step 10: Create Spatial 1's Nominal
        print("Step 10/44: Creating Spatial 1's Nominal...")
        ldd_raster = pcr.readmap(input_raster_path)
        spatial_ones = pcr.nominal(1)
        pcr.report(spatial_ones, spatial_ones_path)
        print("creating spatial 1's nominal")

        # Step 11: Create Inner Polygon Buffer and Symmetrical Difference
        print("Step 11/44: Creating Inner Polygon Buffer and Symmetrical Difference...")
        input_polygon = gpd.read_file(shp_extract_path)
        inner_buffer = input_polygon.buffer(-2)  # 2 meters inward buffer
        sym_diff_polygon = input_polygon.symmetric_difference(inner_buffer)
        sym_diff_polygon = sym_diff_polygon.set_crs(crs, allow_override=True)
        sym_diff_polygon.to_file(sym_diff_polygon_path, driver="GPKG")
        print("creating inner polygon buffer and symmetrical difference")

        # Step 12: Clip Spatial 1's to Symmetrical Difference Polygon
        print("Step 12/44: Clipping Spatial 1's to Symmetrical Difference Polygon...")
        warp_options = gdal.WarpOptions(
            srcSRS=crs, dstSRS=crs,
            cutlineDSName=sym_diff_polygon_path, cropToCutline=True
        )
        gdal.Warp(clipped_ones_path, spatial_ones_path, options=warp_options)
        print("clipping spatial 1's to symmetrical difference polygon")

        # Step 13: Create Spatial 0's Nominal and set CRS
        print("Step 13/44: Creating Spatial 0's Nominal...")
        spatial_zeros = pcr.nominal(0)
        pcr.report(spatial_zeros, spatial_zeros_path)
        print("creating spatial 0's nominal")
        spatial_zeros_dataset = gdal.Open(spatial_zeros_path, gdal.GA_Update)
        spatial_zeros_dataset.SetProjection(crs)
        spatial_zeros_dataset = None  # Close the dataset to save changes

        # Step 14: Calculate Spatial 0's + Clipped 1's, Save as GeoTIFF
        print("Step 14/44: Calculating Spatial 0's + Clipped 1's...")
        spatial_zeros_dataset = gdal.Open(spatial_zeros_path)
        spatial_zeros_raster = spatial_zeros_dataset.ReadAsArray()
        spatial_zeros_geotransform = spatial_zeros_dataset.GetGeoTransform()
        spatial_zeros_projection = spatial_zeros_dataset.GetProjection()

        clipped_ones_dataset = gdal.Open(clipped_ones_path)
        clipped_ones_raster = clipped_ones_dataset.ReadAsArray()
        clipped_ones_geotransform = clipped_ones_dataset.GetGeoTransform()
        clipped_ones_projection = clipped_ones_dataset.GetProjection()

        # Ensure projections match
        assert spatial_zeros_projection == clipped_ones_projection, "Rasters must have the same projection"

        rows, cols = clipped_ones_raster.shape
        x_offset = int((clipped_ones_geotransform[0] - spatial_zeros_geotransform[0]) / spatial_zeros_geotransform[1])
        y_offset = int((clipped_ones_geotransform[3] - spatial_zeros_geotransform[3]) / spatial_zeros_geotransform[5])

        combined_raster = spatial_zeros_raster.copy()

        for i in range(rows):
            for j in range(cols):
                if clipped_ones_raster[i, j] != 0:
                    combined_raster[y_offset + i, x_offset + j] = clipped_ones_raster[i, j]

        driver = gdal.GetDriverByName("GTiff")
        outdata = driver.Create(combined_path, combined_raster.shape[1], combined_raster.shape[0], 1, gdal.GDT_Int16)
        outdata.SetGeoTransform(spatial_zeros_geotransform)
        outdata.SetProjection(spatial_zeros_projection)
        outdata.GetRasterBand(1).WriteArray(combined_raster)
        outdata.FlushCache()
        outdata = None
        print("calculating spatial 0's + clipped 1's")

        # Step 15: Convert to PCRaster Boolean
        print("Step 15/44: Converting to PCRaster Boolean...")
        translate_options = gdal.TranslateOptions(
            format="PCRaster", outputType=gdal.GDT_Byte, noData="none",
            metadataOptions=["PCRASTER_VALUESCALE=VS_BOOLEAN"]
        )
        gdal.Translate(combined_boolean_path, combined_path, options=translate_options)
        print("converting to PCRaster boolean")

        # Step 16: Run Unique ID
        print("Step 16/44: Running Unique ID...")
        combined_boolean = pcr.readmap(combined_boolean_path)
        unique_id_map = pcr.uniqueid(combined_boolean)
        pcr.report(unique_id_map, unique_id_path)
        print("running unique ID")

        # Step 17: Convert Layer Type to Nominal
        print("Step 17/44: Converting Layer Type to Nominal...")
        unique_id_nominal = pcr.nominal(unique_id_map)
        pcr.report(unique_id_nominal, unique_id_nominal_path)
        print("converting layer type to nominal")

        # Step 18: Cover Nominal with Spatial 0's Nominal
        print("Step 18/44: Covering Nominal with Spatial 0's Nominal...")
        spatial_zeros = pcr.readmap(spatial_zeros_path)
        unique_id_nominal = pcr.readmap(unique_id_nominal_path)
        covered_map = pcr.cover(unique_id_nominal, spatial_zeros)
        pcr.report(covered_map, covered_map_path)
        print("covering nominal with spatial 0's nominal")

        # Step 19: Calculate Catchments
        print("Step 19/44: Calculating Catchments...")
        ldd_map = pcr.readmap(output_pcraster_path)
        catchments_map = pcr.catchment(ldd_map, covered_map)
        print("calculating catchments")

        # Step 20: Save Catchments Map
        print("Step 20/44: Saving Catchments Map...")
        pcr.report(catchments_map, catchments_output_path)
        print("saving catchments map")

        # Step 21: Convert Catchments Map to Vector Layer Using OGR
        print("Step 21/44: Converting Catchments Map to Vector Layer...")
        ds = gdal.Open(catchments_output_path)
        drv = ogr.GetDriverByName("GPKG")
        dst_ds = drv.CreateDataSource(catchments_vector_output_path)
        spatial_ref = osr.SpatialReference()
        spatial_ref.ImportFromEPSG(25832)  # Use the EPSG code directly from the crs variable
        dst_layer = dst_ds.CreateLayer("catchments", srs=spatial_ref)
        field_defn = ogr.FieldDefn("DN", ogr.OFTInteger)
        dst_layer.CreateField(field_defn)
        gdal.Polygonize(ds.GetRasterBand(1), None, dst_layer, 0, [], callback=None)
        ds = None
        dst_ds = None
        print("converting catchments map to vector layer")

        # Step 22: Exclude the Largest Catchment Polygon and Select by Location
        print("Step 22/44: Excluding the Largest Catchment Polygon and Selecting by Location...")
        input_polygon = gpd.read_file(shp_extract_path)
        catchments_vector = gpd.read_file(catchments_vector_output_path)
        print("reading input and catchments vector")

        # Step 23: Calculate area and find the largest polygon
        print("Step 23/44: Calculating area and finding the largest polygon...")
        catchments_vector['area'] = catchments_vector.geometry.area
        largest_polygon = catchments_vector.nlargest(1, 'area')
        print("calculating area and finding the largest polygon")

        # Step 24: Exclude the largest polygon
        print("Step 24/44: Excluding the largest polygon...")
        filtered_catchments = catchments_vector[~catchments_vector.index.isin(largest_polygon.index)]
        print("excluding the largest polygon")

        # Step 25: Perform spatial join to select catchments that intersect with the input polygon
        print("Step 25/44: Performing spatial join to select catchments that intersect with the input polygon...")
        selected_catchments = gpd.sjoin(filtered_catchments, input_polygon, how='inner', predicate='intersects')
        selected_catchments.reset_index(drop=True, inplace=True)
        print("performing spatial join")

        # Step 26: Save the selected catchments
        print("Step 26/44: Saving the selected catchments...")
        selected_catchments = selected_catchments.set_crs(crs, allow_override=True)
        selected_catchments.to_file(selected_catchments_path, driver="GPKG")
        print("saving the selected catchments")

        # Step 27: Dissolve selected catchments
        print("Step 27/44: Dissolving selected catchments to skybrudsoplande.gpkg...")
        dissolve_polygon(selected_catchments_path, upstream_catchment_path)
        print("dissolving selected catchments")

        # Step 28: Ensure the dissolved output has the correct CRS
        print("Step 28/44: Ensuring the dissolved output has the correct CRS...")
        dissolved = gpd.read_file(upstream_catchment_path)
        dissolved = dissolved.set_crs(crs)
        dissolved.to_file(upstream_catchment_path, driver="GPKG")
        print("ensuring the dissolved output has the correct CRS")

        # Step 29: Mask the raster using the same method as Step 4
        print("Step 29/44: Masking the raster...")
        warp_options = gdal.WarpOptions(
            srcSRS="EPSG:25832", dstSRS="EPSG:25832",
            cutlineDSName=upstream_catchment_path, cropToCutline=True
        )
        gdal.Warp(clipped_LDD_output_path, output_raster_path, options=warp_options)
        print("masking the raster")

        # Step 30: Translate the clipped LDD raster to PCRaster format
        print("Step 30/44: Translating the clipped LDD raster...")
        translate_options = gdal.TranslateOptions(
            format="PCRaster", outputType=gdal.GDT_Byte, noData="none",
            metadataOptions=["PCRASTER_VALUESCALE=VS_LDD"]
        )
        gdal.Translate(clipped_translated_raster_path, clipped_LDD_output_path, options=translate_options)
        print("translating the clipped LDD raster")

        # Step 31: Set Clone Map to Match Clipped LDD
        print("Step 31/44: Setting Clone Map to Match Clipped LDD...")
        pcr.setclone(clipped_translated_raster_path)
        print("setting clone map to match clipped LDD")

        # Step 32: Read translated clipped LDD raster
        print("Step 32/44: Reading translated clipped LDD raster...")
        clipped_pcraster_raster = pcr.readmap(clipped_translated_raster_path)
        print("reading translated clipped LDD raster")

        # Step 33: Repair the clipped LDD raster
        print("Step 33/44: Repairing the clipped LDD raster...")
        clipped_repaired_raster = pcr.lddrepair(clipped_pcraster_raster)
        print("repairing the clipped LDD raster")

        # Step 34: Save repaired clipped LDD raster
        print("Step 34/44: Saving the repaired clipped LDD raster...")
        pcr.report(clipped_repaired_raster, clipped_output_pcraster_path)
        print("saving the repaired clipped LDD raster")

        # Step 35: Delineate streams based on the repaired clipped LDD
        print("Step 35/44: Delineating streams based on the repaired clipped LDD...")
        clipped_channels_output_path = delineate_streams(
            clipped_output_pcraster_path, temporary_directory, num_top_stream_order_values_2
        )
        print("delineating streams based on the repaired clipped LDD")

        # Dictionary to store maximum values for each scenario
        max_values = {}

        for rainfall_value in rainfall_scenarios:
            print(f"Processing scenario with {rainfall_value} mm of rain...")

            # Paths specific to each scenario
            material_layer_path = os.path.join(temporary_directory, f"scenario{rainfall_value}.map")
            flow_accumulation_path = os.path.join(temporary_directory, f"flow_accumulation_{rainfall_value}.map")
            flow_accumulation_tif_path = os.path.join(temporary_directory, f"flow_accumulation_{rainfall_value}.tif")
            clipped_flow_accumulation_path = os.path.join(temporary_directory, f"clipped_flow_accumulation_{rainfall_value}.tif")

            # Step 36: Create a constant layer for the rainfall scenario
            start_time = record_time()
            print(f"Step 36/44: Creating a constant layer for the rainfall scenario {rainfall_value} mm...")
            pcr.setclone(output_pcraster_path)
            material_layer = pcr.spatial(pcr.scalar(rainfall_value))
            pcr.report(material_layer, material_layer_path)
            print(f"creating a constant layer for the rainfall scenario {rainfall_value} mm")

            # Step 37: Calculate flow accumulation
            start_time = record_time()
            print(f"Step 37/44: Calculating flow accumulation for {rainfall_value} mm...")
            ldd_map = pcr.readmap(output_pcraster_path)
            flow_accumulation_raster = pcr.accuflux(ldd_map, material_layer)
            pcr.report(flow_accumulation_raster, flow_accumulation_path)
            translate_options = gdal.TranslateOptions(outputSRS="EPSG:25832")
            gdal.Translate(flow_accumulation_tif_path, flow_accumulation_path, options=translate_options)
            print(f"calculating flow accumulation for {rainfall_value} mm")

            # Step 38: Clip flow accumulation to input polygon
            start_time = record_time()
            print(f"Step 38/44: Clipping flow accumulation to input polygon for {rainfall_value} mm...")
            input_polygon_path = shp_extract_path
            options = gdal.WarpOptions(cutlineDSName=input_polygon_path, cropToCutline=True, dstNodata=0)
            gdal.Warp(clipped_flow_accumulation_path, flow_accumulation_tif_path, options=options)
            print(f"clipping flow accumulation to input polygon for {rainfall_value} mm")

            # Find maximum value
            dataset = gdal.Open(clipped_flow_accumulation_path)
            band = dataset.GetRasterBand(1)
            stats = band.GetStatistics(True, True)
            max_flow_accumulation_cells = stats[1]  # Maximum flow accumulation in raster cells
            rainfall_meters = rainfall_value / 1000  # Convert rainfall from mm to meters
            max_volume_cubic_meters = max_flow_accumulation_cells * rainfall_meters  # Calculate the maximum volume in cubic meters
            print(f"Maximum flow accumulation for {rainfall_value} mm: {max_volume_cubic_meters} cubic meters")
            max_values[rainfall_value] = max_volume_cubic_meters

        # Log the maximum values for all scenarios
        print("Maximum values for each rainfall scenario (in cubic meters):")
        for rainfall, max_val in max_values.items():
            print(f"{rainfall} mm: {max_val} cubic meters")

        # Step 39: Convert upstream catchment channels raster to points
        print("Step 39/44: Converting channels raster to points...")
        channels_points_gdf = raster_to_points(ID15_channels_output_path, channels_points_path, crs)
        print("converting channels raster to points")

        # Step 40: Resample ID15 channel points with the chosen flow accumulation layer
        print(f"Step 40/44: Resampling points with flow accumulation layer for {selected_rainfall_scenario} mm of rain...")
        resampled_points_gdf = resample_points(channels_points_path, flow_accumulation_path, resampled_points_path, crs)
        print("resampling points with flow accumulation layer")

        # Step 41: Convert ID15 scale channels raster to points
        print("Step 41/44: Converting channels raster to points...")
        upstream_channels_points_gdf = raster_to_points(clipped_channels_output_path, upstream_channels_points_path, crs)
        print("converting channels raster to points")

        # Step 42: Resample ID15 channel points with the chosen flow accumulation layer
        print(f"Step 42/44: Resampling points with flow accumulation layer for {selected_rainfall_scenario} mm of rain...")
        resampled_points_gdf = resample_points(upstream_channels_points_path, flow_accumulation_path, upstream_resampled_points_path, crs)
        print("resampling points with flow accumulation layer")

        # Step 43: Clip Bluespot to the upstream catchment (Opland) area
        print("Step 43/44: Clipping Bluespot to Opland...")
        processing.run('native:clip', {
            'INPUT': bluespot_path,
            'OVERLAY': upstream_catchment_path,
            'OUTPUT': bluespot_clip_path
        })
        print("clipping bluespot to opland")

        # Step 44: Create QGIS project
        create_qgis_project(
            resampled_points_path,
            upstream_resampled_points_path,
            shp_extract_path,
            upstream_catchment_path,
            selected_ID15_path,
            bluespot_clip_path,
            dtm_raster_path,
            qgis_project_path,
            crs,
            qml_styles
        )
        print("Step 44/44: creating qgis project")

        print("Script completed.")
        print(f"Total execution time: {time.time() - start_time_script:.2f} seconds.")

    except Exception as e:
        print(f"An error occurred: {e}")
        raise

if __name__ == "__main__":
    run_geospatial_analysis()