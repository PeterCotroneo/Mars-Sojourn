"""Map layers for one mission: the latest position, every stop and the route.

Three memory layers per mission in Mars longitude/latitude. The rover marker is
turned to the heading NASA reports for the end of its last drive. Nothing is
estimated: a rover is drawn where NASA last located it and stays there until
NASA publishes a new position.
"""

import datetime
import os

from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtGui import QColor, QFont
from qgis.core import (
    QgsAction, QgsVectorLayer, QgsFeature, QgsField, QgsGeometry, QgsPointXY, QgsMarkerSymbol,
    QgsSimpleMarkerSymbolLayer, QgsSvgMarkerSymbolLayer, QgsLineSymbol,
    QgsSimpleLineSymbolLayer, QgsSingleSymbolRenderer, QgsProperty, QgsSymbolLayer,
    QgsPalLayerSettings, QgsTextFormat, QgsTextBufferSettings, QgsVectorLayerSimpleLabeling,
    QgsMessageLog, Qgis,
)

from .layers import add_layer, hide_fields, layer_valid, remove_layers
from .mars import geographic_crs
from .missions import MISSIONS, HELICOPTER, format_date, photo_status

plugin_dir = os.path.dirname(__file__)

# order in the Layers panel (lower ranks sit above): within a mission's group,
# rover above stops above route, then a carried helicopter's group (50), then
# that mission's imagery (90 and up); the groups themselves in mission order,
# with the global mosaic (99) at the bottom
RANK_POSITION, RANK_STOPS, RANK_ROUTE = 10, 20, 30
RANK_CARRIED = 50
GROUP_RANKS = {"m20": 1, "msl": 3}
RANK_GLOBAL = 99


def group_name(key):
    """The Layers-panel group for a mission ("Ingenuity" says whose it was)."""
    mission = MISSIONS[key]
    carrier = mission.get("carrier")
    if carrier:
        return f"{mission['name']} ({MISSIONS[carrier]['name']}'s helicopter)"
    return mission["name"]


def group_path(key):
    """Where the mission's layers go: [(group name, rank), ...], outermost first.
    A helicopter's group sits inside its rover's, above the rover's imagery."""
    carrier = MISSIONS[key].get("carrier")
    if carrier:
        return group_path(carrier) + [(group_name(key), RANK_CARRIED)]
    return [(group_name(key), GROUP_RANKS[key])]


_S, _D, _I = QVariant.String, QVariant.Double, QVariant.Int

_POSITION_FIELDS = [
    ("name", _S, "Name"), ("sol", _I, "Sol"), ("date", _S, "Earth date"),
    ("total_km", _D, "Distance since landing (km)"), ("elev_m", _D, "Elevation (m)"),
    ("heading", _D, "Heading (degrees from north)"), ("tilt", _D, "Tilt (degrees)"),
    ("published", _S, "Position published by NASA"),
    ("photos", _S, "NASA's latest photos"),
    ("lon", _D, "Longitude (east)"), ("lat", _D, "Latitude"), ("tip", _S, "tip"),
]
_STOP_FIELDS = [
    ("sol", _I, "Sol"), ("date", _S, "Earth date"), ("kind", _S, "Type of stop"),
    ("drive_m", _D, "Drive to here (m)"), ("total_km", _D, "Distance since landing (km)"),
    ("elev_m", _D, "Elevation (m)"), ("heading", _D, "Heading (degrees from north)"),
    ("tilt", _D, "Tilt (degrees)"), ("site", _I, "Site"), ("drive", _I, "Drive"),
    ("lon", _D, "Longitude (east)"), ("lat", _D, "Latitude"), ("tip", _S, "tip"),
]
_ROUTE_FIELDS = [
    ("sol", _I, "Sol"), ("date", _S, "Earth date"),
    ("length_m", _D, "Length (m)"), ("tip", _S, "tip"),
]
_FLIGHT_STOP_FIELDS = [
    ("flight", _I, "Flight"), ("sol", _I, "Sol"), ("date", _S, "Earth date"),
    ("drive_m", _D, "Flight distance (m)"), ("total_km", _D, "Total flown (km)"),
    ("max_alt_m", _D, "Maximum altitude (m)"), ("duration_s", _D, "Duration (s)"),
    ("from", _S, "From"), ("to", _S, "To"), ("elev_m", _D, "Ground elevation (m)"),
    ("lon", _D, "Longitude (east)"), ("lat", _D, "Latitude"), ("tip", _S, "tip"),
]
_FLIGHT_ROUTE_FIELDS = [
    ("flight", _I, "Flight"), ("sol", _I, "Sol"), ("length_m", _D, "Length (m)"), ("tip", _S, "tip"),
]


def _fmt(value, digits, unit=""):
    return "" if value is None else f"{value:,.{digits}f}{unit}"


def _memory_layer(geometry, title, fields, rank, group):
    layer = QgsVectorLayer(geometry, title, "memory")
    layer.setCrs(geographic_crs())
    layer.dataProvider().addAttributes([QgsField(name, qtype) for name, qtype, _alias in fields])
    layer.updateFields()
    for name, _qtype, alias in fields:
        layer.setFieldAlias(layer.fields().indexOf(name), alias)
    hide_fields(layer, ["tip"])
    layer.setMapTipTemplate('[% "tip" %]')
    add_layer(layer, rank, group)
    return layer


def _fill(layer, fields, rows):
    """Replace the layer's features. Each row is (geometry, {field: value})."""
    dp = layer.dataProvider()
    existing = [f.id() for f in layer.getFeatures()]
    if existing:
        dp.deleteFeatures(existing)
    feats = []
    for geom, values in rows:
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat.setAttributes([values.get(name) for name, _qtype, _alias in fields])
        feats.append(feat)
    if feats:
        dp.addFeatures(feats)
    layer.updateExtents()
    layer.triggerRepaint()


class MissionLayers:
    def __init__(self, key):
        self.key = key
        self.mission = MISSIONS[key]
        self.is_heli = self.mission["kind"] == HELICOPTER
        self.position = None      # the latest position dict, once loaded
        self.published = None     # epoch seconds NASA last changed the position file
        self.photo = None         # the rover's newest raw photo (sol, day, site, drive)
        self.stops = []           # every stop, oldest first
        self.stop_count = 0
        self.data = None          # NASA's latest data, kept so layers can be drawn later
        self._position = self._stops = self._route = None

    # --- lifecycle -------------------------------------------------------
    def layers(self):
        return [lyr for lyr in (self._position, self._stops, self._route) if layer_valid(lyr)]

    def position_layer(self):
        return self._position if layer_valid(self._position) else None

    def loaded(self):
        return self.position is not None and bool(self.layers())

    def remove(self):
        remove_layers(self.layers())
        self._position = self._stops = self._route = None
        self.position = None
        self.published = None
        self.photo = None
        self.stops = []
        self.stop_count = 0
        self.data = None

    def _titles(self):
        if self.is_heli:
            return "Final position", "Landing spots", "Flights"
        return "Rover (latest position)", "Stops", "Route"

    def _ensure_layers(self):
        t_position, t_stops, t_route = self._titles()
        group = group_path(self.key)
        if not layer_valid(self._route):
            self._route = _memory_layer(
                "MultiLineString", t_route,
                _FLIGHT_ROUTE_FIELDS if self.is_heli else _ROUTE_FIELDS, RANK_ROUTE,
                group)
            self._safely(self._style_route)
        if not layer_valid(self._stops):
            self._stops = _memory_layer(
                "Point", t_stops,
                _FLIGHT_STOP_FIELDS if self.is_heli else _STOP_FIELDS, RANK_STOPS,
                group)
            self._safely(self._style_stops)
        if not layer_valid(self._position):
            self._position = _memory_layer(
                "Point", t_position,
                _FLIGHT_STOP_FIELDS if self.is_heli else _POSITION_FIELDS, RANK_POSITION,
                group)
            self._safely(self._style_position)
        if not self.is_heli:
            for layer in (self._stops, self._position):
                self._safely(lambda lyr=layer: self._add_photo_action(lyr))

    def _add_photo_action(self, layer):
        """A "Show photos from this sol" action, offered by the Identify results
        and the Run Feature Action tool for a stop or the rover."""
        code = ("from qgis.utils import plugins\n"
                "plugin = plugins.get('sojourn')\n"
                "if plugin is not None:\n"
                f"    plugin.show_photos('{self.key}', [% \"sol\" %])\n")
        action = QgsAction(QgsAction.ActionType.GenericPython, "Show photos from this sol",
                           code, False)
        action.setActionScopes({"Feature", "Canvas"})
        layer.actions().addAction(action)

    def find_stop(self, site, drive):
        """The last stop stamped with this site and drive, or None."""
        for stop in reversed(self.stops):
            if (stop.get("site"), stop.get("drive")) == (site, drive):
                return stop
        return None

    def drop_layers(self):
        """Take the mission's layers off the map, keeping its data (the map has
        left Mars). ``draw()`` puts them back."""
        remove_layers(self.layers())
        self._position = self._stops = self._route = None

    def forget_layers(self):
        """The project was closed, taking the layers with it: keep the data."""
        self._position = self._stops = self._route = None

    # --- data ------------------------------------------------------------
    def store(self, data):
        """Keep NASA's latest data (the panel's "Last drive" line reads it)
        without drawing anything: the map may not be on Mars."""
        self.data = data
        self.position = data["position"]
        self.published = data.get("published")
        self.stops = data["stops"]
        self.stop_count = len(data["stops"])

    def draw(self):
        """Draw the stored data on the map (which must be on Mars)."""
        if self.data is None:
            return
        self._ensure_layers()
        if self.is_heli:
            self._build_flights(self.data)
        else:
            self._build_rover(self.data)

    def build(self, data):
        self.store(data)
        self.draw()

    def published_text(self):
        if not self.published:
            return ""
        day = datetime.datetime.fromtimestamp(self.published, datetime.timezone.utc).date()
        return format_date(day)

    def set_photo(self, photo):
        """Record the rover's newest raw photo and refresh the position's hover text."""
        self.photo = photo
        if self.position is not None and layer_valid(self._position) and not self.is_heli:
            self._fill_position()

    def _fill_position(self):
        name = self.mission["name"]
        p = self.position
        _state, photos = photo_status(p, self.photo)
        tip = (f"<b>{name}</b> · last drive {p['date']} (sol {p['sol']})<br/>"
               f"{_fmt(p['total_km'], 2)} km driven since landing<br/>"
               f"Elevation {_fmt(p['elev_m'], 0)} m · tilt {_fmt(p['tilt'], 1)}°")
        if photos:
            tip += f"<br/>{photos}"
        row = dict(p, name=name, published=self.published_text(), photos=photos, tip=tip)
        _fill(self._position, _POSITION_FIELDS,
              [(QgsGeometry.fromPointXY(QgsPointXY(p["lon"], p["lat"])), row)])

    def _build_rover(self, data):
        name = self.mission["name"]
        self._fill_position()

        rows = []
        for s in data["stops"]:
            tip = (f"<b>{name}</b> stop · {s['date']} (sol {s['sol']})<br/>{s['kind']}"
                   f"<br/>Drive to here: {_fmt(s['drive_m'], 1)} m · "
                   f"{_fmt(s['total_km'], 2)} km since landing")
            rows.append((QgsGeometry.fromPointXY(QgsPointXY(s["lon"], s["lat"])),
                         dict(s, tip=tip)))
        _fill(self._stops, _STOP_FIELDS, rows)

        rows = []
        for piece in data["route"]:
            if piece["sol"] is not None:
                tip = (f"<b>{name}</b> drive · {piece['date']} (sol {piece['sol']})"
                       f"<br/>{_fmt(piece['length_m'], 1)} m")
            else:   # Curiosity's route file carries no sol numbers
                tip = f"<b>{name}</b> route"
            rows.append((self._line(piece), dict(piece, tip=tip)))
        _fill(self._route, _ROUTE_FIELDS, rows)

    def _build_flights(self, data):
        name = self.mission["name"]

        def flight_tip(s, heading):
            return (f"<b>{name}</b> {heading} · flight {s['flight']}<br/>{s['date']} "
                    f"(sol {s['sol']})<br/>Flew {_fmt(s['drive_m'], 0)} m in "
                    f"{_fmt(s['duration_s'], 0)} s, up to {_fmt(s['max_alt_m'], 0)} m high"
                    f"<br/>{_fmt(s['total_km'], 2)} km flown in total")

        p = data["position"]
        _fill(self._position, _FLIGHT_STOP_FIELDS,
              [(QgsGeometry.fromPointXY(QgsPointXY(p["lon"], p["lat"])),
                dict(p, tip=flight_tip(p, "final position")))])
        _fill(self._stops, _FLIGHT_STOP_FIELDS,
              [(QgsGeometry.fromPointXY(QgsPointXY(s["lon"], s["lat"])),
                dict(s, tip=flight_tip(s, "landing spot"))) for s in data["stops"]])
        rows = []
        for piece in data["route"]:
            tip = (f"<b>{name}</b> flight {piece['flight']} · sol {piece['sol']}"
                   f"<br/>{_fmt(piece['length_m'], 0)} m")
            rows.append((self._line(piece), dict(piece, tip=tip)))
        _fill(self._route, _FLIGHT_ROUTE_FIELDS, rows)

    @staticmethod
    def _line(piece):
        return QgsGeometry.fromMultiPolylineXY(
            [[QgsPointXY(lon, lat) for lon, lat in line] for line in piece["parts"]])

    # --- styling ---------------------------------------------------------
    @staticmethod
    def _safely(style):
        try:
            style()
        except Exception as exc:  # noqa: BLE001 - styling must never block data
            QgsMessageLog.logMessage(f"styling skipped: {exc}", "Mars Sojourn",
                                     Qgis.MessageLevel.Warning)

    def _style_position(self):
        colour = QColor(self.mission["colour"])
        if self.is_heli:   # Ingenuity seen from above: its two rotors and solar panel
            marker = QgsSvgMarkerSymbolLayer(os.path.join(plugin_dir, "helicopter.svg"))
            marker.setSize(10.0)   # thin blades: a little larger than the rover to read as well
            marker.setFillColor(colour)
            marker.setStrokeColor(QColor("#101010"))
            marker.setStrokeWidth(0.3)
        else:
            marker = QgsSvgMarkerSymbolLayer(os.path.join(plugin_dir, "rover.svg"))
            marker.setSize(9.0)
            marker.setFillColor(colour)
            marker.setStrokeColor(QColor("#101010"))
            marker.setStrokeWidth(0.3)
            # nose of the marker points the way the rover is facing
            marker.setDataDefinedProperty(QgsSymbolLayer.Property.Angle,
                                          QgsProperty.fromExpression('coalesce("heading", 0)'))
        sym = QgsMarkerSymbol()
        sym.changeSymbolLayer(0, marker)
        self._position.setRenderer(QgsSingleSymbolRenderer(sym))
        if not self.is_heli:
            self._label(self._position, "name")

    def _style_stops(self):
        circle = QgsSimpleMarkerSymbolLayer()   # default shape is a circle
        circle.setSize(1.7)
        circle.setColor(QColor(self.mission["colour"]))
        circle.setStrokeColor(QColor("#101010"))
        circle.setStrokeWidth(0.2)
        sym = QgsMarkerSymbol()
        sym.changeSymbolLayer(0, circle)
        self._stops.setRenderer(QgsSingleSymbolRenderer(sym))

    def _style_route(self):
        casing = QgsSimpleLineSymbolLayer()
        casing.setColor(QColor(16, 16, 16, 170))
        casing.setWidth(1.3)
        line = QgsSimpleLineSymbolLayer()
        line.setColor(QColor(self.mission["colour"]))
        line.setWidth(0.6)
        sym = QgsLineSymbol()
        sym.changeSymbolLayer(0, casing)
        sym.appendSymbolLayer(line)
        self._route.setRenderer(QgsSingleSymbolRenderer(sym))

    @staticmethod
    def _label(layer, field):
        text = QgsTextFormat()
        font = QFont()
        font.setBold(True)
        text.setFont(font)
        text.setSize(10)
        text.setColor(QColor("#ffffff"))
        halo = QgsTextBufferSettings()
        halo.setEnabled(True)
        halo.setSize(1.0)
        halo.setColor(QColor("#101010"))
        text.setBuffer(halo)
        settings = QgsPalLayerSettings()
        settings.fieldName = field
        settings.setFormat(text)
        settings.dist = 5.0   # clear of the rover marker
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
