"""Mars coordinate systems and imagery.

QGIS cannot transform between Earth and Mars coordinate systems (PROJ refuses to
mix celestial bodies), so everything here is on Mars: the rover data is in
planetocentric longitude/latitude on the IAU 2015 Mars sphere (radius 3,396,190 m)
and the map is put in Mars Mercator.

NASA's rover-site imagery is published as ordinary web-map tiles (the same tiling
maths as Earth web maps, applied to Mars longitude/latitude), numbered from the
bottom (TMS). QGIS's own XYZ layers are fixed to Earth's Web Mercator, so the
tiles are loaded through GDAL's tile driver instead, which lets the projection be
stated as Mars Mercator.

A tile service always spans the whole planet, so each close-up layer is clipped
to the area NASA actually covers (a small GDAL VRT around the tile service).
Without that, every map redraw asks NASA for thousands of tiles that do not
exist, which made redraws take tens of seconds and could stall QGIS on exit.
"""

import contextlib
import math
import os
from html import escape

from osgeo import gdal

from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsEllipsoidUtils,
    QgsMessageLog, QgsPointXY, QgsProject, QgsRasterLayer, Qgis,
)

from .layers import NoLegend

MARS_RADIUS = 3396190.0
MARS_ELLIPSOID = "IAU_2015:49900"
_USER_AGENT = "Mars Sojourn QGIS plugin (+https://github.com/PeterCotroneo/Mars-Sojourn)"
_MMGIS = "https://mars.nasa.gov/mmgis-maps"
_TREK = "https://trek.nasa.gov/tiles/Mars/EQ"

# Imagery layers, bottom first. ``grid`` is "mercator" (NASA rover-site tiles) or
# "degrees" (NASA Mars Trek global mosaic). ``levels`` is (lowest, highest) zoom
# the server holds; ``max_scale`` hides a layer when zoomed out past the point
# where it has no tiles (None = always shown). The lowest level is not passed to
# GDAL: capping its overviews there makes QGIS read thousands of tiles on load.
IMAGERY = [
    {
        "key": "global", "title": "Mars - global colour mosaic (232 m)", "mission": None,
        "grid": "degrees", "ext": "jpg", "bands": 3, "levels": (0, 7), "max_scale": None,
        "url": _TREK + "/Mars_Viking_MDIM21_ClrMosaic_global_232m/1.0.0/default/default028mm",
        "credit": "NASA/JPL-Caltech/USGS",
    },
    {
        # NASA Mars Trek's mosaic of Context Camera images around Jezero: fills the
        # region round Perseverance's colour close-up at about 10 m per pixel
        "key": "jezero_region", "title": "Jezero region imagery (CTX)", "mission": "m20",
        "bbox": (75.1537692, 15.9058484, 78.8717121, 19.4453003),
        "grid": "degrees", "ext": "png", "bands": 4, "levels": (0, 12), "max_scale": 6000000,
        "url": _TREK + "/Jezero_CTX_BlockAdj_dd/1.0.0/default/default028mm",
        "credit": "NASA/JPL/MSSS (NASA Mars Trek)",
    },
    {
        "key": "gale_region", "title": "Gale crater imagery (6 m)", "mission": "msl",
        "bbox": (135.97876221689464, -7.84409099977539, 139.48590357790334, -3.39706499423008),
        "grid": "mercator", "ext": "png", "bands": 4, "levels": (6, 13), "max_scale": 6000000,
        "url": _MMGIS + "/MSL/Layers/MSL_CTX_ortho_mosaic_6m",
        "credit": "NASA/JPL-Caltech/MSSS",
    },
    {
        "key": "gale_site", "title": "Route imagery (HiRISE colour)", "mission": "msl",
        "bbox": (137.32672354505786, -4.87560429259321, 137.4785574965306, -4.55506331169111),
        "grid": "mercator", "ext": "png", "bands": 4, "levels": (10, 18), "max_scale": 400000,
        "url": _MMGIS + "/MSL/Layers/MSL_Gale_HiRISE-LRGB_78quads",
        "credit": "NASA/JPL/University of Arizona",
    },
    {
        "key": "jezero_site", "title": "Route imagery (HiRISE colour, 25 cm)", "mission": "m20",
        "bbox": (77.15751489, 18.23712426, 77.51146566, 18.52055001),
        "grid": "mercator", "ext": "png", "bands": 4, "levels": (10, 19), "max_scale": 400000,
        "url": _MMGIS + "/M20/Layers/M20_HiRISE_RGB_CTX_mosaic_25cm_v7_Merge_LERC_clip",
        "credit": "NASA/JPL/University of Arizona",
    },
]


def geographic_crs():
    """Mars longitude/latitude (planetocentric, east-positive), as NASA publishes."""
    crs = QgsCoordinateReferenceSystem("IAU_2015:49900")
    if not crs.isValid():   # older PROJ databases lack the IAU 2015 codes
        crs = QgsCoordinateReferenceSystem.fromProj(f"+proj=longlat +R={MARS_RADIUS} +no_defs")
    return crs


def mercator_crs():
    """Mars Mercator: the projection NASA's rover-site tiles are cut in."""
    crs = QgsCoordinateReferenceSystem("IAU_2015:49990")
    if not crs.isValid():
        crs = QgsCoordinateReferenceSystem.fromProj(
            f"+proj=merc +lon_0=0 +x_0=0 +y_0=0 +R={MARS_RADIUS} +units=m +no_defs")
    return crs


def _is_mars(crs):
    """True if ``crs`` is defined on Mars, judged by the size of its ellipsoid.
    (Building an Earth-to-Mars transform to find out would make QGIS show a
    "No transform available" error.)"""
    if not crs.isValid():
        return False
    params = QgsEllipsoidUtils.ellipsoidParameters(crs.ellipsoidAcronym())
    return params.valid and abs(params.semiMajor - MARS_RADIUS) < 20000


def project_is_on_mars():
    """True if the project's map can show Mars data (its CRS is a Mars one)."""
    return _is_mars(QgsProject.instance().crs())


def put_project_on_mars():
    """Switch the map to Mars Mercator and measure distances on the Mars sphere."""
    project = QgsProject.instance()
    project.setCrs(mercator_crs())
    project.setEllipsoid(MARS_ELLIPSOID)


def to_project(lon, lat):
    """A Mars longitude/latitude as a point in the project's CRS (None if the
    project is not on Mars)."""
    project = QgsProject.instance()
    if not _is_mars(project.crs()):
        return None
    transform = QgsCoordinateTransform(geographic_crs(), project.crs(), project)
    return transform.transform(QgsPointXY(lon, lat))


def _tiles_xml(spec):
    """A GDAL tile-service description for one imagery layer."""
    high = spec["levels"][1]
    block = 256
    if spec["grid"] == "mercator":
        half = math.pi * MARS_RADIUS
        window = (-half, half, half, -half)
        count_x, origin, order, proj = 1, "bottom", "${z}/${x}/${y}", mercator_crs().toProj()
    else:   # NASA Mars Trek: two 180-degree tiles at level 0, numbered from the top
        window = (-180.0, 90.0, 180.0, -90.0)
        count_x, origin, order, proj = 2, "top", "${z}/${y}/${x}", geographic_crs().toProj()
    suffix = f".{spec['ext']}" if spec["ext"] else ""
    return (
        "<GDAL_WMS>"
        f'<Service name="TMS"><ServerUrl>{escape(spec["url"])}/{order}{suffix}'
        "</ServerUrl></Service>"
        "<DataWindow>"
        f"<UpperLeftX>{window[0]!r}</UpperLeftX><UpperLeftY>{window[1]!r}</UpperLeftY>"
        f"<LowerRightX>{window[2]!r}</LowerRightX><LowerRightY>{window[3]!r}</LowerRightY>"
        f"<TileLevel>{high}</TileLevel><TileCountX>{count_x}</TileCountX>"
        f"<TileCountY>1</TileCountY><YOrigin>{origin}</YOrigin>"
        "</DataWindow>"
        f"<Projection>{escape(proj)}</Projection>"
        f"<BlockSizeX>{block}</BlockSizeX><BlockSizeY>{block}</BlockSizeY>"
        f"<BandsCount>{spec['bands']}</BandsCount>"
        # NASA answers 403 (not 404) for tiles outside the imagery: draw nothing there
        "<ZeroBlockHttpCodes>403,404</ZeroBlockHttpCodes>"
        "<ZeroBlockOnServerException>true</ZeroBlockOnServerException>"
        # No <Cache>: GDAL's disk cache logs an error for every tile NASA answers
        # 403 for. Tiles already drawn are still kept in memory for the session.
        f"<UserAgent>{escape(_USER_AGENT)}</UserAgent><Timeout>15</Timeout>"
        "<MaxConnections>10</MaxConnections>"
        "</GDAL_WMS>"
    )


def _imagery_dir():
    path = os.path.join(QgsApplication.qgisSettingsDirPath(), "sojourn", "imagery")
    os.makedirs(path, exist_ok=True)
    return path


def _source(spec):
    """What QGIS opens for one imagery layer: the tile service itself for the
    global mosaic, or a VRT clipping it to the area NASA covers."""
    xml = _tiles_xml(spec)
    if not spec.get("bbox"):
        return xml
    folder = _imagery_dir()
    service = os.path.join(folder, spec["key"] + ".xml")
    with open(service, "w", encoding="utf-8") as f:
        f.write(xml)
    west, south, east, north = spec["bbox"]
    if spec["grid"] == "mercator":   # the clip box goes in the layer's own coordinates
        to_merc = QgsCoordinateTransform(geographic_crs(), mercator_crs(), QgsProject.instance())
        top_left = to_merc.transform(QgsPointXY(west, north))
        bottom_right = to_merc.transform(QgsPointXY(east, south))
    else:
        top_left, bottom_right = QgsPointXY(west, north), QgsPointXY(east, south)
    clipped = os.path.join(folder, spec["key"] + ".vrt")
    # GDAL errors as Python exceptions here only, without changing the setting
    # for the rest of QGIS (and silencing GDAL's warning about it)
    manager = getattr(gdal, "ExceptionMgr", None)
    with manager(useExceptions=True) if manager else contextlib.nullcontext():
        result = gdal.Translate(clipped, service, format="VRT",
                                projWin=[top_left.x(), top_left.y(),
                                         bottom_right.x(), bottom_right.y()])
    if result is None:
        return None
    result = None   # closes the VRT so it is written out
    return clipped


def imagery_layer(spec):
    """One imagery layer, or None if GDAL could not open it."""
    try:
        source = _source(spec)
    except (OSError, RuntimeError) as exc:
        QgsMessageLog.logMessage(f"imagery {spec['key']}: {exc}", "Mars Sojourn", Qgis.MessageLevel.Warning)
        source = None
    if source is None:
        return None
    layer = QgsRasterLayer(source, spec["title"], "gdal")
    if not layer.isValid():
        return None
    layer.setCrs(mercator_crs() if spec["grid"] == "mercator" else geographic_crs())
    layer.setLegend(NoLegend())
    try:
        layer.serverProperties().setAttribution(spec["credit"])   # QGIS 3.38+
    except AttributeError:
        layer.setAttribution(spec["credit"])
    if spec["max_scale"]:
        layer.setScaleBasedVisibility(True)
        layer.setMinimumScale(spec["max_scale"])   # QGIS: "minimum scale" = most zoomed out
    return layer
