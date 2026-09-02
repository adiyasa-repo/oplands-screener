import env_bootstrap  # noqa: F401  (must be imported before any qgis/pcraster import)
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsRasterLayer,
    QgsCoordinateReferenceSystem,
    QgsReferencedRectangle
)
from utils import get_dataforsyningen_apikey
import sys
import os

def _basemap_uri(wms_url, layer_name, image_format, apikey):
    return (
        f"contextualWMSLegend=0&crs=EPSG:25832&dpiMode=7&featureCount=10"
        f"&format={image_format}&layers={layer_name}&styles"
        f"&url={wms_url}?apikey={apikey}"
    )

def create_qgis_project(resampled_points_path, upstream_resampled_points_path, shp_extract_path, upstream_catchment_path, selected_ID15_path, bluespot_clip_path, dtm_raster_path, qgis_project_path, crs, qml_styles):
    # Define the relative path to the Bluespot files
    base_path = os.path.dirname(qgis_project_path)
    bluespot_path = os.path.join(base_path, '..', 'Input', 'qml_styles', 'Bluespot.gpkg')
    bluespot_style = os.path.join(base_path, '..', 'Input', 'qml_styles', 'Bluespot.qml')

    # Load the vector layers
    layers = {
        'resampled_points': QgsVectorLayer(resampled_points_path, 'Vandveje (ID15)', 'ogr'),
        'upstream_resampled_points': QgsVectorLayer(upstream_resampled_points_path, 'Vandveje (Opland)', 'ogr'),
        'input_polygon': QgsVectorLayer(shp_extract_path, 'Interesse Område', 'ogr'),
        'upstream_catchment': QgsVectorLayer(upstream_catchment_path, 'Opland', 'ogr'),
        'selected_ID15': QgsVectorLayer(selected_ID15_path, 'Selected ID15', 'ogr')
    }

    # Load the DTM raster layer
    dtm_layer = QgsRasterLayer(dtm_raster_path, 'DTM', 'gdal')
    if not dtm_layer.isValid():
        print('DTM layer is not valid!')
        sys.exit(1)

    # Load the bluespot layer (original, unclipped) and its clipped counterpart.
    # Both are named plainly "Bluespot" -- they're told apart by which folder
    # they end up in, not by name.
    bluespot_layer = QgsVectorLayer(bluespot_path, 'Bluespot', 'ogr')
    if not bluespot_layer.isValid():
        print(f'Bluespot layer is not valid! Path: {bluespot_path}')
        sys.exit(1)

    bluespot_id15_layer = QgsVectorLayer(bluespot_clip_path, 'Bluespot', 'ogr')
    if not bluespot_id15_layer.isValid():
        print(f'Bluespot (clipped to Opland) layer is not valid! Path: {bluespot_clip_path}')
        sys.exit(1)

    # Check if layers are valid
    for name, layer in layers.items():
        if not layer.isValid():
            print(f'{name} layer is not valid! Path: {layer.source()}')
            sys.exit(1)

    # Basemap layers from Dataforsyningen (WMS)
    apikey = get_dataforsyningen_apikey()
    skaermkort_layer = QgsRasterLayer(
        _basemap_uri('https://wms.datafordeler.dk/Dkskaermkort/topo_skaermkort/1.0.0/wms', 'dtk_skaermkort', 'image/jpeg', apikey),
        'Skærmkort - Klassisk',
        'wms'
    )
    ortofoto_layer = QgsRasterLayer(
        _basemap_uri('https://wms.datafordeler.dk/GeoDanmarkOrto/orto_foraar/1.0.0/WMS', 'orto_foraar', 'image/png', apikey),
        'Nyeste Ortofoto, Geodanmark',
        'wms'
    )
    for name, layer in (('Skærmkort - Klassisk', skaermkort_layer), ('Nyeste Ortofoto, Geodanmark', ortofoto_layer)):
        if not layer.isValid():
            print(f'Basemap layer is not valid! {name}')
            sys.exit(1)

    project = QgsProject.instance()
    root = project.layerTreeRoot()

    # Apply QML styles up front, before layers are placed into groups.
    if 'DTM' in qml_styles:
        dtm_layer.loadNamedStyle(qml_styles['DTM'])
        dtm_layer.triggerRepaint()
    for name in ('selected_ID15', 'upstream_catchment', 'input_polygon', 'upstream_resampled_points', 'resampled_points'):
        if name in qml_styles:
            layers[name].loadNamedStyle(qml_styles[name])
            layers[name].triggerRepaint()
    if os.path.exists(bluespot_style):
        for bs_layer in (bluespot_layer, bluespot_id15_layer):
            bs_layer.loadNamedStyle(bluespot_style)
            bs_layer.triggerRepaint()
    else:
        print(f'Bluespot QML style not found at path: {bluespot_style}')
        sys.exit(1)

    # Register every layer with the project without auto-placing it at the
    # tree root, so it can be placed into the correct group below instead.
    all_layers = list(layers.values()) + [
        dtm_layer, bluespot_layer, bluespot_id15_layer, skaermkort_layer, ortofoto_layer
    ]
    for layer in all_layers:
        project.addMapLayer(layer, False)

    # Build the three top-level groups. Created in reverse of their intended
    # visual order (insertGroup(0, ...) always puts the newest at the top),
    # so the final top-to-bottom order is: Interesse Opland, ID15, Baggrundskort.
    baggrundskort_group = root.insertGroup(0, 'Baggrundskort')
    id15_group = root.insertGroup(0, 'ID15')
    interesse_opland_group = root.insertGroup(0, 'Interesse Opland')

    # Same reverse-insert trick within each group, so the visible top-to-
    # bottom order in each folder matches the order listed here directly.
    for layer in reversed([layers['upstream_resampled_points'], layers['input_polygon'], layers['upstream_catchment'], bluespot_id15_layer]):
        interesse_opland_group.insertLayer(0, layer)

    for layer in reversed([layers['resampled_points'], layers['selected_ID15'], bluespot_layer, dtm_layer]):
        id15_group.insertLayer(0, layer)

    for layer in reversed([skaermkort_layer, ortofoto_layer]):
        baggrundskort_group.insertLayer(0, layer)

    # Collapse every layer's legend (the expandable symbol/class list under
    # its name in the Layers panel) so the panel opens tidy instead of with
    # categorized/graduated layers (resampled points, DTM, ...) pre-expanded.
    for layer_node in root.findLayers():
        layer_node.setExpanded(False)

    # Set the project CRS
    project.setCrs(QgsCoordinateReferenceSystem(crs))

    # Open the project zoomed to Selected ID15 instead of full extent.
    project.viewSettings().setDefaultViewExtent(
        QgsReferencedRectangle(layers['selected_ID15'].extent(), layers['selected_ID15'].crs())
    )

    # Save the project
    project.write(qgis_project_path)

    print(f"QGIS project created and saved successfully at {qgis_project_path}.")
