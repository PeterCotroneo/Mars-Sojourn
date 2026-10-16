"""Background tile downloads for the imagery layers.

The imagery layers read tiles only from a local cache (GDAL's offline mode, see
mars.py), so drawing the map never waits on the network. This fetcher keeps the
cache filled: when the map moves it works out which tiles each visible imagery
layer needs, downloads the missing ones through QGIS's network manager (in the
background, a few at a time, cancellable at once) and redraws the layer as they
arrive. Quitting QGIS or unloading the plugin aborts every download in flight.
"""

import math
import os
from collections import deque

from qgis.PyQt.QtCore import QObject, QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import QgsCoordinateTransform, QgsNetworkAccessManager, QgsProject, QgsRectangle

from .layers import layer_valid
from .mars import (
    MARS_RADIUS, _USER_AGENT, geographic_crs, mercator_crs, tile_cache_path, tile_template,
)

_PARALLEL = 8            # downloads at once
_MAX_PER_VIEW = 400      # never queue more than this for one view
_TILE = 256              # tile size in pixels


def _tiles_for_view(spec, canvas):
    """[(url, cache path)] for the tiles ``spec``'s layer needs to draw the
    canvas's current view. GDAL draws from the most detailed level that is not
    finer than the screen needs, which can be the level matching the screen or
    the next coarser one, so both are fetched first (coarser first: it is a
    quarter of the tiles); the next finer level follows if there is room."""
    mercator = spec["grid"] == "mercator"
    layer_crs = mercator_crs() if mercator else geographic_crs()
    half = math.pi * MARS_RADIUS
    map_crs = canvas.mapSettings().destinationCrs()
    # Zoomed right out, the view runs past the edge of the map projection
    # (Mercator never reaches the poles) and can't be transformed: trim it first.
    limit = (QgsRectangle(-180.0, -89.9, 180.0, 89.9) if map_crs.isGeographic()
             else QgsRectangle(-half, -0.99 * half, half, 0.99 * half))
    view = canvas.extent().intersect(limit)
    if view.isEmpty():
        return []
    try:
        to_layer = QgsCoordinateTransform(map_crs, layer_crs, QgsProject.instance())
        view = to_layer.transformBoundingBox(view)
    except Exception:   # noqa: BLE001 - e.g. the map isn't on Mars: nothing to fetch
        return []
    world = (QgsRectangle(-half, -half, half, half) if mercator
             else QgsRectangle(-180.0, -90.0, 180.0, 90.0))
    if spec.get("bbox"):
        west, south, east, north = spec["bbox"]
        if mercator:
            to_merc = QgsCoordinateTransform(geographic_crs(), mercator_crs(), QgsProject.instance())
            world = to_merc.transformBoundingBox(QgsRectangle(west, south, east, north))
        else:
            world = QgsRectangle(west, south, east, north)
    view = view.intersect(world)
    # Real screen pixels: a Retina display draws the map at twice the density,
    # so QGIS asks for a level finer than the window's size suggests.
    pixels = canvas.width() * max(canvas.devicePixelRatioF(), 1.0)
    if view.isEmpty() or pixels <= 0:
        return []
    low, high = spec["levels"]
    span = 2 * half if mercator else 180.0          # width of one tile at level 0
    per_pixel = (view.width() / pixels) or 1e-12
    level = math.ceil(math.log2(max(span / (_TILE * per_pixel), 1e-12)))
    level = min(max(level, low), high)
    template = tile_template(spec)
    tiles = []
    for z in (level - 1, level, level + 1):
        if not low <= z <= high:
            continue
        size = span / 2 ** z
        if mercator:   # numbered from the bottom-left, 2^z x 2^z
            count_x = count_y = 2 ** z
            x0, x1 = (view.xMinimum() + half) / size, (view.xMaximum() + half) / size
            y0, y1 = (view.yMinimum() + half) / size, (view.yMaximum() + half) / size
        else:          # Mars Trek: numbered from the top-left, 2^(z+1) x 2^z
            count_x, count_y = 2 ** (z + 1), 2 ** z
            x0, x1 = (view.xMinimum() + 180.0) / size, (view.xMaximum() + 180.0) / size
            y0, y1 = (90.0 - view.yMaximum()) / size, (90.0 - view.yMinimum()) / size
        xs = range(max(0, math.floor(x0)), min(count_x - 1, math.floor(x1)) + 1)
        ys = range(max(0, math.floor(y0)), min(count_y - 1, math.floor(y1)) + 1)
        for y in ys:
            for x in xs:
                url = template.replace("${z}", str(z)).replace("${x}", str(x)).replace("${y}", str(y))
                tiles.append((url, tile_cache_path(spec, url)))
                if len(tiles) >= _MAX_PER_VIEW:
                    return tiles
    return tiles


class TileFetcher(QObject):
    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self._canvas = canvas
        self._layers = {}            # imagery key -> (layer, spec)
        self._queue = deque()        # (key, url, path) waiting to download
        self._active = {}            # reply -> (key, url, path)
        self._absent = set()         # urls NASA has no tile for (403/404): don't ask again
        self._changed = set()        # keys with new tiles to show
        self._stopped = False
        self._plan_timer = QTimer(self)
        self._plan_timer.setSingleShot(True)
        self._plan_timer.setInterval(300)    # wait for panning/zooming to settle
        self._plan_timer.timeout.connect(self._plan)
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.setInterval(500)  # redraw in batches, not per tile
        self._redraw_timer.timeout.connect(self._redraw)
        canvas.extentsChanged.connect(self.schedule)
        canvas.layersChanged.connect(self.schedule)   # a layer ticked back on

    def add(self, key, layer, spec):
        self._stopped = False
        self._layers[key] = (layer, spec)
        self.schedule()

    def remove(self, key):
        self._layers.pop(key, None)
        self._queue = deque(item for item in self._queue if item[0] != key)

    def schedule(self):
        if not self._stopped and self._layers:
            self._plan_timer.start()

    def stop(self):
        """Abort every download at once (QGIS is quitting or the plugin unloading)."""
        self._stopped = True
        self._plan_timer.stop()
        self._redraw_timer.stop()
        self._queue.clear()
        for reply in list(self._active):
            self._active.pop(reply, None)
            try:
                reply.abort()
            except RuntimeError:   # already deleted
                pass

    def disconnect_canvas(self):
        for signal in (self._canvas.extentsChanged, self._canvas.layersChanged):
            try:
                signal.disconnect(self.schedule)
            except (TypeError, RuntimeError):
                pass

    # --- planning and downloading -----------------------------------------
    def _plan(self):
        """Queue the tiles the current view needs, dropping ones it no longer does."""
        if self._stopped:
            return
        shown = {lyr.id() for lyr in self._canvas.layers()}
        scale = self._canvas.scale()
        busy = {path for _key, _url, path in self._active.values()}
        queue = deque()
        for key, (layer, spec) in list(self._layers.items()):
            if not layer_valid(layer) or layer.id() not in shown or not layer.isInScaleRange(scale):
                continue
            for url, path in _tiles_for_view(spec, self._canvas):
                if url in self._absent or path in busy or os.path.exists(path):
                    continue
                queue.append((key, url, path))
        self._queue = queue
        self._pump()

    def _pump(self):
        nam = QgsNetworkAccessManager.instance()
        while self._queue and len(self._active) < _PARALLEL and not self._stopped:
            key, url, path = self._queue.popleft()
            req = QNetworkRequest(QUrl(url))
            req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, _USER_AGENT)
            reply = nam.get(req)
            self._active[reply] = (key, url, path)
            reply.finished.connect(lambda r=reply: self._done(r))

    def _done(self, reply):
        item = self._active.pop(reply, None)
        try:
            if item is None or self._stopped:
                return
            key, url, path = item
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = bytes(reply.readAll())
                if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    partial = path + ".part"
                    with open(partial, "wb") as fh:
                        fh.write(data)
                    os.replace(partial, path)   # GDAL never sees half a tile
                    self._changed.add(key)
                    self._redraw_timer.start()
            elif status in (403, 404):
                self._absent.add(url)   # outside NASA's imagery: stays blank
        finally:
            reply.deleteLater()
            self._pump()

    def _redraw(self):
        """Show newly arrived tiles: reopen the layer so GDAL sees the new cache
        files, then repaint it."""
        for key in self._changed:
            layer, _spec = self._layers.get(key, (None, None))
            if layer_valid(layer):
                layer.dataProvider().reloadData()
                layer.triggerRepaint()
        self._changed.clear()
