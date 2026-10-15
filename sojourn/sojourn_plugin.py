"""Mars Sojourn — main plugin class.

A small floating panel with a checkbox per mission (the Perseverance and
Curiosity rovers, and the Ingenuity helicopter's flights) and one for Mars
imagery. Ticking a box adds that mission's layers to a "Mars Rovers" group in
the Layers panel; unticking removes them. The plugin checks NASA for a new rover
position every hour.
"""

import html
import os
import time

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QIcon
from qgis.PyQt.QtWidgets import (
    QAction, QDockWidget, QFrame, QScrollArea, QSplitter, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QToolButton, QPushButton, QDialog, QDialogButtonBox,
)
from qgis.core import (
    QgsCoordinateTransform, QgsGeometry, QgsMessageLog, QgsPointXY, QgsProject, QgsRectangle,
    QgsSettings, Qgis,
)
from qgis.gui import QgsMapToolEmitPoint, QgsVertexMarker

from .feed import MissionFeed
from .layers import layer_valid, add_layer, other_layers, remove_layers
from .mars import (
    IMAGERY, geographic_crs, imagery_layer, project_is_on_mars, put_project_on_mars, to_project,
)
from .missions import MISSIONS, ROVER, photo_status
from .rovers import RANK_GLOBAL, MissionLayers, group_path
from .sources_info import rover_html, heli_html, imagery_html
from .viewer import PhotoPanel

CHECK_MS = 60 * 60 * 1000    # rovers move every few days at most
ZOOM_HALF_WIDTH_M = 400      # "Zoom to" shows about 800 m around the rover
plugin_dir = os.path.dirname(__file__)


def _info_button(tooltip, handler):
    btn = QToolButton()
    btn.setText("ⓘ")
    btn.setAutoRaise(True)
    btn.setToolTip(tooltip)
    btn.clicked.connect(lambda _=False: handler())
    return btn


def _grey_label(indent, lines):
    """Grey text that always reserves ``lines`` lines, so the panel keeps the
    same layout whether a box is ticked or not."""
    lbl = QLabel("")
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(f"color: gray; margin-left: {indent}px;")
    lbl.setMinimumHeight(lbl.fontMetrics().lineSpacing() * lines + 2)
    return lbl


class _PickTool(QgsMapToolEmitPoint):
    """Map tool for "Pick a stop": a click picks, Esc cancels."""

    def __init__(self, canvas, cancel):
        super().__init__(canvas)
        self._cancel = cancel

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)


class SojournPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        self.sets = {key: MissionLayers(key) for key in MISSIONS}
        self.feed = MissionFeed()
        self.feed.loaded.connect(self._on_loaded)
        self.feed.unchanged.connect(self._on_unchanged)
        self.feed.error.connect(self._on_error)
        self.feed.photo.connect(self._on_photo)
        self.timer = None
        self.boxes = {}          # mission key -> QCheckBox
        self.labels = {}         # mission key -> status QLabel
        self.zoom_buttons = {}   # mission key -> QToolButton
        self.photo_buttons = {}  # rover key -> QToolButton
        self.photos = None       # the photo viewer in the panel
        self._pick_tool = None   # (map tool, rover key, previous tool) while picking a stop
        self._marker = None      # map marker on the spot the photos on show were taken
        self._photo_check_failed = set()   # missions whose newest-photo check timed out
        self.imagery = {}        # imagery key -> layer, when shown
        self.chk_imagery = None
        self.btn_mars = None
        self.lbl_footer = None
        self._checked = None     # clock time of the last successful check
        self._error = None
        self._zoom_when_loaded = None
        self._project_hooked = False

    # --- plugin lifecycle ------------------------------------------------
    def initGui(self):
        icon = QIcon(os.path.join(plugin_dir, "icon.svg"))
        self.action = QAction(icon, "Mars Sojourn", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.toggled.connect(self._toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("Mars Sojourn", self.action)

    def unload(self):
        if self._project_hooked:
            QgsProject.instance().layersRemoved.disconnect(self._on_layers_removed)
            QgsProject.instance().crsChanged.disconnect(self._update_status)
            self._project_hooked = False
        if self.timer is not None:
            self.timer.stop()
            self.timer = None
        self.feed.stop()
        self._end_pick()
        self._clear_marker()
        if self.photos is not None:
            self.photos.stop()
        for layers in self.sets.values():
            layers.remove()
        self._remove_imagery()
        self._redraw_map()
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu("Mars Sojourn", self.action)
            self.action = None

    def _toggle_dock(self, checked):
        if checked:
            first = self.dock is None
            if first:
                self.dock = self._build_dock()
                self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
                self._dock_width(self.dock, self._start_width())
                QgsProject.instance().layersRemoved.connect(self._on_layers_removed)
                QgsProject.instance().crsChanged.connect(self._update_status)
                self._project_hooked = True
            self.dock.show()
            if first:
                self._zoom_when_loaded = "m20"
                self.chk_imagery.setChecked(True)
                for key, mission in MISSIONS.items():
                    if mission["kind"] == ROVER:
                        self.boxes[key].setChecked(True)
        elif self.dock is not None:
            self.dock.hide()

    # --- UI --------------------------------------------------------------
    def _mission_rows(self, layout, key, text, info, lines, indent=0):
        row = QHBoxLayout()
        row.setSpacing(4)
        if indent:
            row.addSpacing(indent)
        chk = QCheckBox(text)
        chk.toggled.connect(lambda on, k=key: self._on_mission_toggled(k, on))
        self.boxes[key] = chk
        row.addWidget(chk, 1)
        zoom = QToolButton()
        zoom.setFixedWidth(64)
        zoom.setText("Zoom to")
        zoom.setToolTip(f"Zoom the map to {MISSIONS[key]['name']}")
        zoom.setEnabled(False)
        zoom.clicked.connect(lambda _=False, k=key: self._zoom_to(k))
        self.zoom_buttons[key] = zoom
        row.addWidget(zoom)
        photos = QToolButton()
        photos.setFixedWidth(64)
        photos.setText("Photos")
        photos.setToolTip(f"Show {MISSIONS[key]['name']}'s own raw photos below, newest first "
                          "(press again to hide them)")
        photos.setCheckable(True)
        photos.clicked.connect(lambda _=False, k=key: self._toggle_photos(k))
        self.photo_buttons[key] = photos
        row.addWidget(photos)
        row.addWidget(_info_button(f"About the {MISSIONS[key]['name']} data",
                                   lambda: self._show_info(*info())))
        layout.addLayout(row)
        self.labels[key] = _grey_label(22 + indent, lines)
        layout.addWidget(self.labels[key])

    def _build_dock(self):
        dock = QDockWidget("Mars Sojourn", self.iface.mainWindow())
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        # Two parts with a draggable divider between them: the rover controls
        # (scrollable, so they can be squeezed) and, once a Photos button is
        # pressed, the photos underneath.
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(3)
        layout.setContentsMargins(10, 8, 10, 8)

        self._mission_rows(layout, "m20", "Perseverance", lambda: rover_html("m20"), 1)
        # Ingenuity flew from Perseverance, so it sits under it, indented
        self._mission_rows(layout, "heli", "Ingenuity helicopter", heli_html, 1, indent=18)
        layout.addSpacing(4)
        self._mission_rows(layout, "msl", "Curiosity", lambda: rover_html("msl"), 1)

        layout.addSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.chk_imagery = QCheckBox("Mars imagery")
        self.chk_imagery.toggled.connect(self._on_imagery_toggled)
        row.addWidget(self.chk_imagery)
        row.addWidget(_info_button("About the imagery and the Mars map projection",
                                   lambda: self._show_info(*imagery_html())))
        row.addStretch(1)
        layout.addLayout(row)

        self.btn_mars = QPushButton("Switch map to Mars")
        self.btn_mars.setToolTip(
            "This project's map is in an Earth coordinate system, so Mars layers cannot "
            "be drawn. Switching puts the map in Mars Mercator; Earth layers in this "
            "project will then stop drawing.")
        self.btn_mars.clicked.connect(lambda _=False: self._switch_to_mars())
        layout.addWidget(self.btn_mars)

        self.lbl_footer = _grey_label(0, 1)
        layout.addWidget(self.lbl_footer)
        layout.addStretch(1)   # spare height goes below the controls

        controls = QScrollArea()
        controls.setWidget(panel)
        controls.setWidgetResizable(True)
        controls.setFrameShape(QFrame.Shape.NoFrame)
        controls.setMinimumHeight(60)
        controls.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.controls = controls

        self.photos = PhotoPanel(
            self._latest_photo_sol, self._where, self._show_spot, self._start_pick,
            self._close_photos, self._mark_spot, self._nav_sols)
        self.photos.setContentsMargins(8, 0, 8, 6)
        self.photos.hide()   # until a rover's Photos button is pressed

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(controls)
        self.splitter.addWidget(self.photos)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        self.splitter.setHandleWidth(9)
        self.splitter.setStyleSheet(
            "QSplitter::handle { background: palette(midlight); "
            "border-top: 1px solid palette(mid); border-bottom: 1px solid palette(mid); }"
            "QSplitter::handle:hover { background: palette(highlight); }")
        self.splitter.handle(1).setToolTip("Drag to give the photos more or less room")
        self.splitter.splitterMoved.connect(self._remember_split)
        dock.setWidget(self.splitter)
        self._update_status()  # fill every slot before the window is sized
        return dock

    def _start_width(self):
        """Opening width: as narrow as the panel goes without cutting anything off."""
        panel = self.controls.widget()
        return max(330, panel.minimumSizeHint().width() + 4, self.photos.minimumSizeHint().width())

    def _dock_width(self, dock, width):
        try:
            self.iface.mainWindow().resizeDocks([dock], [width], Qt.Orientation.Horizontal)
        except (AttributeError, TypeError):
            dock.resize(width, dock.height())

    def _show_info(self, title, body):
        dlg = QDialog(self.iface.mainWindow())
        dlg.setWindowTitle(f"About — {title}")
        dlg.setMinimumWidth(460)
        v = QVBoxLayout(dlg)
        lbl = QLabel(body)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setWordWrap(True)
        lbl.setOpenExternalLinks(True)
        v.addWidget(lbl)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        v.addWidget(buttons)
        dlg.exec()

    # --- Mars or Earth ---------------------------------------------------
    def _our_layer_ids(self):
        ids = {lyr.id() for layers in self.sets.values() for lyr in layers.layers()}
        ids.update(lyr.id() for lyr in self.imagery.values() if layer_valid(lyr))
        return ids

    def _ensure_mars(self):
        """Put the map on Mars if that cannot disturb anything: only when the
        project holds no layers of the user's own. Otherwise the panel offers a
        "Switch map to Mars" button and leaves the choice to the user."""
        if project_is_on_mars():
            return
        if not other_layers(self._our_layer_ids()):
            self._switch_to_mars()

    def _switch_to_mars(self):
        put_project_on_mars()
        self.iface.messageBar().pushMessage(
            "Mars Sojourn", "The map is now in a Mars projection (Mars Mercator).",
            Qgis.MessageLevel.Info, 6)
        self._update_status()
        focus = next((k for k in MISSIONS if self.sets[k].loaded()), None)
        if focus:
            self._zoom_to(focus)

    # --- the checkboxes --------------------------------------------------
    def _on_mission_toggled(self, key, on):
        if on:
            self._ensure_mars()
            self.feed.load(key)
            self.feed.check_photos(key)
            self._ensure_timer()
        else:
            self.feed.stop(key)
            self.sets[key].remove()
            if self.photos is not None and self.photos.key == key:
                self._close_photos()
        self._sync_imagery()
        self._redraw_map()
        self._update_status()

    def _on_imagery_toggled(self, on):
        if on:
            self._ensure_mars()
        self._sync_imagery()
        self._redraw_map()
        self._update_status()

    def _sync_imagery(self):
        """Imagery follows the boxes: the global mosaic whenever "Mars imagery"
        is ticked, and each rover's close-up imagery, inside that rover's group,
        only while the rover is ticked too."""
        on = self.chk_imagery is not None and self.chk_imagery.isChecked()
        for i, spec in enumerate(IMAGERY):
            mission = spec.get("mission")
            # Jezero's imagery serves Perseverance and Ingenuity alike
            users = [mission] + [k for k, m in MISSIONS.items() if m.get("carrier") == mission]
            want = on and (mission is None or any(self.boxes[k].isChecked() for k in users))
            layer = self.imagery.get(spec["key"])
            have = layer_valid(layer)
            if want and not have:
                layer = imagery_layer(spec)
                if layer is None:
                    QgsMessageLog.logMessage(f"could not open imagery: {spec['title']}",
                                             "Mars Sojourn", Qgis.MessageLevel.Warning)
                    continue
                if mission is None:   # the planet-wide layers, at the very bottom
                    add_layer(layer, RANK_GLOBAL - i)
                else:   # within the rover's group, below its rover, stops and route
                    add_layer(layer, 90 + len(IMAGERY) - i, group_path(mission))
                self.imagery[spec["key"]] = layer
            elif have and not want:
                remove_layers([layer])
                self.imagery.pop(spec["key"], None)

    def _remove_imagery(self):
        remove_layers(list(self.imagery.values()))
        self.imagery = {}

    def _on_layers_removed(self, _layer_ids):
        # Keep the boxes in step if the user deletes layers in the Layers panel.
        for key, chk in self.boxes.items():
            if chk.isChecked() and not self.feed.busy(key) and not self.sets[key].layers():
                chk.setChecked(False)
        self.imagery = {k: lyr for k, lyr in self.imagery.items() if layer_valid(lyr)}

    def _redraw_map(self):
        # Removing layers updates the canvas's layer list but doesn't always
        # repaint it, leaving the removed symbols on screen until the next redraw.
        self.iface.mapCanvas().refresh()

    def _zoom_to(self, key):
        position = self.sets[key].position
        if position is None:
            return
        centre = to_project(position["lon"], position["lat"])
        if centre is None:
            return   # the project is on Earth; the panel says so
        half = ZOOM_HALF_WIDTH_M
        canvas = self.iface.mapCanvas()
        canvas.setExtent(QgsRectangle(centre.x() - half, centre.y() - half,
                                      centre.x() + half, centre.y() + half))
        canvas.refresh()
        self._select_rover(key)

    def _select_rover(self, key):
        """Select the rover's layer in the Layers panel, so the selection matches
        the rover on screen (QGIS's Identify tool uses the selected
        layer). A layer of the user's own that is selected is left alone."""
        layer = self.sets[key].position_layer()
        if layer is None:
            return
        current = self.iface.activeLayer()
        if current is None or not layer_valid(current) or current.id() in self._our_layer_ids():
            self.iface.setActiveLayer(layer)

    # --- photos ----------------------------------------------------------
    def show_photos(self, key, sol=None):
        """Show the rover's photos in the panel at ``sol`` (default: the newest
        sol with photos). Also called by the "Show photos from this sol" action
        on stops and on the rover."""
        if key not in MISSIONS:
            return
        if self.dock is None or not self.dock.isVisible():
            self.action.setChecked(True)
        self.dock.raise_()
        if not self.photos.isVisible():
            self.photos.show()
            self._restore_split()
        self._update_photo_buttons(key)
        if self.sets[key].photo is None:
            self._photo_check_failed.discard(key)
            self.feed.check_photos(key)   # learn the newest sol
            QTimer.singleShot(25000, lambda k=key: self._photo_check_timeout(k))
        try:
            sol = None if sol is None else int(sol)
        except (TypeError, ValueError):
            sol = None
        self.photos.show_rover(key, sol)

    def _close_photos(self):
        self._end_pick()
        if self.photos is None:
            return
        self.photos.stop()
        self._clear_marker()
        self.photos.hide()

        self._update_photo_buttons(None)

    def _restore_split(self):
        """Give the photos most of the height (or the share the user last chose
        by dragging the divider); the controls above scroll if squeezed."""
        total = sum(self.splitter.sizes()) or self.dock.height()
        try:
            share = float(QgsSettings().value("sojourn/controls_share", 0.0))
        except (TypeError, ValueError):
            share = 0.0
        if not 0.05 <= share <= 0.9:
            # first time: the photos get at least 70% of the height; the
            # controls scroll if they need more than the rest
            need = self.controls.widget().sizeHint().height()
            share = max(60, min(need, int(total * 0.3))) / total
        top = int(total * share)
        self.splitter.setSizes([top, total - top])

    def _remember_split(self, _pos, _index):
        if self.photos is not None and self.photos.isVisible():
            sizes = self.splitter.sizes()
            if sum(sizes):
                QgsSettings().setValue("sojourn/controls_share", sizes[0] / sum(sizes))

    def _update_photo_buttons(self, showing):
        """The rover whose photos are showing has its Photos button pressed in."""
        for key, btn in self.photo_buttons.items():
            btn.setChecked(key == showing)

    def _toggle_photos(self, key):
        if self.photos is not None and self.photos.isVisible() and self.photos.key == key:
            self._close_photos()
        else:
            self.show_photos(key)

    def _start_pick(self, _key=None):
        """Let the user click any stop on the map (either rover's, or one of
        Ingenuity's landing spots); the photos from that sol then show below.
        Esc, or choosing another map tool, cancels."""
        if not any(layers.stops for layers in self.sets.values()):
            self.iface.messageBar().pushMessage(
                "Mars Sojourn", "Tick a rover first, so its stops are on the map.",
                Qgis.MessageLevel.Info, 5)
            return
        self._end_pick()
        canvas = self.iface.mapCanvas()
        tool = _PickTool(canvas, self._end_pick)
        tool.canvasClicked.connect(lambda point, _button: self._on_pick(point))
        tool.deactivated.connect(self._end_pick)
        self._pick_tool = (tool, None, canvas.mapTool())
        canvas.setMapTool(tool)
        self.photos.set_picking(True)

    def _end_pick(self):
        if self._pick_tool is None:
            return
        tool, _key, previous = self._pick_tool
        self._pick_tool = None
        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is tool:
            if previous is not None:
                canvas.setMapTool(previous)
            else:
                canvas.unsetMapTool(tool)
        if self.photos is not None:
            self.photos.set_picking(False)

    def _on_pick(self, point):
        tolerance = 12 * self.iface.mapCanvas().mapUnitsPerPixel()
        found = self.nearest_stop(point, tolerance)
        self._end_pick()
        if found is None:
            self.iface.messageBar().pushMessage(
                "Mars Sojourn", "No stop there: click right on one of the dots.",
                Qgis.MessageLevel.Info, 4)
            return
        key, stop = found
        self.show_photos(key, stop["sol"])

    def nearest_stop(self, point, tolerance):
        """(mission key, stop) nearest a map point (project CRS) over every mission
        on the map, if within tolerance; else None."""
        project = QgsProject.instance()
        if not project_is_on_mars():
            return None
        to_map = QgsCoordinateTransform(geographic_crs(), project.crs(), project)
        best, best_d = None, tolerance
        for key, layers in self.sets.items():
            if not layers.layers():
                continue
            for stop in layers.stops:
                if stop.get("sol") is None:
                    continue
                d = to_map.transform(QgsPointXY(stop["lon"], stop["lat"])).distance(point)
                if d <= best_d:
                    best, best_d = (key, stop), d
        return best

    def _latest_photo_sol(self, key):
        """The newest sol NASA has posted photos for (None until known: the
        panel waits, with a spinner, rather than open on an older sol)."""
        layers = self.sets[key]
        if layers.photo:
            return layers.photo["sol"]
        if key in self._photo_check_failed and layers.position:
            return layers.position["sol"]   # NASA's newest-photo check failed: best we know
        return None

    def _nav_sols(self, key):
        """The sols the photo panel's ◀ ▶ step through: Ingenuity's flights;
        every sol for the rovers (None)."""
        if key != "heli":
            return None
        layers = self.sets[key]
        return sorted({s["sol"] for s in layers.stops if s.get("sol") is not None}) or None

    def _heli_spot(self, sol):
        """Where Ingenuity was on a sol: the landing spot of its last flight up to
        then (or where it was set down, before its first flight)."""
        stops = [s for s in self.sets["heli"].stops if s.get("sol") is not None]
        before = [s for s in stops if s["sol"] <= sol]
        if before:
            return max(before, key=lambda s: (s["sol"], s.get("flight") or 0))
        return min(stops, key=lambda s: s["sol"]) if stops else None

    def _where(self, key, site, drive):
        """Words for a photo's location, and whether it is on the map. For
        Ingenuity ``site`` is "sol" and ``drive`` the sol number."""
        layers = self.sets[key]
        if key == "heli":
            spot = self._heli_spot(drive)
            if spot is None:
                return "tick Ingenuity to see where it was", False
            if spot["flight"] == 0:
                return "where Perseverance set it down", True
            return f"after flight {spot['flight']} ({spot['date']})", True
        position = layers.position
        if position and (position["site"], position["drive"]) == (site, drive):
            return "at the rover's current spot", True
        stop = layers.find_stop(site, drive)
        if stop is not None:
            return f"at its stop of {stop['date']} (sol {stop['sol']})", True
        if not layers.stops:
            return f"at site {site}, drive {drive} (tick the rover to see it on the map)", False
        return "at a spot NASA has not mapped yet", False

    def _spot(self, key, site, drive):
        """The rover's position or stop stamped with this site and drive (for
        Ingenuity: where it was on sol ``drive``), or None."""
        if key == "heli":
            return self._heli_spot(drive)
        layers = self.sets[key]
        position = layers.position
        if position and (position["site"], position["drive"]) == (site, drive):
            return position
        return layers.find_stop(site, drive)

    def _mark_spot(self, key, site, drive):
        """Centre the map (keeping its scale) on where the photos on show were
        taken, and mark it with a ring until the photos are hidden."""
        spot = self._spot(key, site, drive)
        centre = None if spot is None else to_project(spot["lon"], spot["lat"])
        if centre is None:
            self._clear_marker()   # not on the map (or the map is not on Mars)
            return
        canvas = self.iface.mapCanvas()
        canvas.setCenter(centre)
        canvas.refresh()
        if self._marker is None:
            self._marker = QgsVertexMarker(canvas)
            self._marker.setIconType(QgsVertexMarker.IconType.ICON_CIRCLE)
            self._marker.setIconSize(26)
            self._marker.setPenWidth(4)
            self._marker.setColor(QColor("#ff3b30"))
            self._marker.setFillColor(QColor(255, 59, 48, 40))
        self._marker.setCenter(centre)
        self._marker.show()
        self._flash(spot)

    def _clear_marker(self):
        if self._marker is not None:
            try:
                self.iface.mapCanvas().scene().removeItem(self._marker)
            except RuntimeError:
                pass
            self._marker = None

    def _flash(self, spot):
        try:
            self.iface.mapCanvas().flashGeometries(
                [QgsGeometry.fromPointXY(QgsPointXY(spot["lon"], spot["lat"]))], geographic_crs())
        except (AttributeError, TypeError):   # flashing is a nicety
            pass

    def _show_spot(self, key, site, drive):
        spot = self._spot(key, site, drive)
        if spot is None:
            return
        centre = to_project(spot["lon"], spot["lat"])
        if centre is None:
            self._update_status()   # the footer explains the Earth/Mars problem
            return
        half = 150
        canvas = self.iface.mapCanvas()
        canvas.setExtent(QgsRectangle(centre.x() - half, centre.y() - half,
                                      centre.x() + half, centre.y() + half))
        canvas.refresh()
        self._flash(spot)

    def _photo_check_timeout(self, key):
        """If NASA has not said what its newest sol is within 25 s, fall back to
        the sol of the mission's last known position rather than wait forever."""
        if self.sets[key].photo is None:
            self._photo_check_failed.add(key)
            self._on_photo_for_viewer(key)

    def _on_photo_for_viewer(self, key):
        if self.photos is None or self.photos.key != key:
            return
        if self.photos.waiting_for_latest():
            self.photos.go_to_sol(None)   # opened before the newest sol was known
        else:
            self.photos.update_latest()   # NASA may have posted a newer sol

    # --- loading ---------------------------------------------------------
    def _ensure_timer(self):
        if self.timer is None:
            self.timer = QTimer()
            self.timer.setInterval(CHECK_MS)
            self.timer.timeout.connect(self._check)
            self.timer.start()

    def _check(self):
        """Ask NASA whether either rover has moved. Ingenuity never will."""
        for key, mission in MISSIONS.items():
            if mission["kind"] != ROVER or not self.boxes[key].isChecked():
                continue
            position = self.sets[key].position
            if position is None:
                self.feed.load(key)
            else:
                self.feed.check(key, position["id"])
            self.feed.check_photos(key)

    def _on_loaded(self, key, data):
        if key not in self.boxes or not self.boxes[key].isChecked():
            return   # late reply after the box was unticked
        moved = self.sets[key].position is not None
        self.sets[key].build(data)
        self._checked = time.strftime("%H:%M")
        self._error = None
        if moved:
            self.iface.messageBar().pushMessage(
                "Mars Sojourn", f"{MISSIONS[key]['name']} has a new position from NASA.",
                Qgis.MessageLevel.Info, 8)
        if self._zoom_when_loaded == key:
            self._zoom_when_loaded = None
            self._zoom_to(key)
        self._update_status()

    def _on_photo(self, key, photo):
        self.sets[key].set_photo(photo)
        self._on_photo_for_viewer(key)
        if key in self.boxes and self.boxes[key].isChecked():
            self._update_status()

    def _on_unchanged(self, _key):
        self._checked = time.strftime("%H:%M")
        self._error = None
        self._update_status()

    def _on_error(self, _key, text):
        QgsMessageLog.logMessage(text, "Mars Sojourn", Qgis.MessageLevel.Warning)
        self._error = text
        self._update_status()

    # --- status text -----------------------------------------------------
    def _mission_text(self, key):
        layers = self.sets[key]
        position = layers.position
        if not self.boxes[key].isChecked():
            if MISSIONS[key]["kind"] != ROVER:
                return "Perseverance's helicopter: 72 flights, 2021 to 2024"
            return f"Tick to add {MISSIONS[key]['name']} to the map"
        if position is None:
            return "Loading…" if self.feed.busy(key) else "Not loaded"
        if layers.is_heli:
            return (f"Perseverance's helicopter: {position['flight']} flights, "
                    f"{position['total_km']:.2f} km, last on {html.escape(position['date'])}")
        text = (f"Last drive {position['date']} (sol {position['sol']}) · "
                f"{position['total_km']:.2f} km driven")
        state, photos = photo_status(position, layers.photo)
        if state == "moved":
            text += f"<br/><span style='color:#c0392b'>{photos}</span>"
        return text

    def _update_status(self, *_args):
        if self.lbl_footer is None:
            return
        # Every slot is always shown (a hint when unticked), so the panel's
        # layout and size never jump around.
        for key, label in self.labels.items():
            label.setText(self._mission_text(key))
            self.zoom_buttons[key].setEnabled(self.sets[key].position is not None)

        showing = any(chk.isChecked() for chk in self.boxes.values()) \
            or self.chk_imagery.isChecked()
        on_earth = showing and not project_is_on_mars()
        self.btn_mars.setVisible(on_earth)
        if on_earth:
            footer = ("<span style='color:#c0392b'>This project's map is on Earth, so "
                      "Mars layers cannot be drawn.</span>")
        elif self._error:
            footer = f"<span style='color:#c0392b'>{html.escape(self._error)}</span>"
        elif self._checked:
            footer = f"Checked {self._checked} · checks NASA every hour"
        else:
            footer = "Checks NASA every hour"
        self.lbl_footer.setText(footer)
