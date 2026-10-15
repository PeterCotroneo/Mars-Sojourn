"""Photo viewer, built into the Sojourn panel: a rover's raw photos, one sol
at a time.

Laid out like a photo app: a compact header and sol bar, the photo as large as
the panel allows (previous/next, zoom and expand float over it), a slim film
strip of the sol's photos underneath (newest first, filterable by camera) and a
two-line caption. Nothing wraps: text that does not fit is shortened with "…"
and shown in full on hover, so the layout never jumps or gets cut off. Each
photo says where it was taken: rovers stamp their photos with site and drive
counters, which are matched to the stops on the map.

Photos are fetched from NASA's raw-image feeds through QGIS's network manager
(proxy settings respected; QGIS's network cache keeps photos already seen).
"""

import json

from qgis.PyQt.QtCore import Qt, QEvent, QObject, QRectF, QSize, QTimer, QUrl
from qgis.PyQt.QtGui import QColor, QDesktopServices, QIcon, QImage, QPainter, QPen, QPixmap
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QSizePolicy, QSpinBox,
    QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)
from qgis.core import QgsMessageLog, QgsNetworkAccessManager, Qgis

from .missions import MISSIONS, format_date, parse_photos, photos_url

_USER_AGENT = "Mars Sojourn QGIS plugin (+https://github.com/PeterCotroneo/Mars-Sojourn)"
_THUMB = QSize(84, 63)
_MAX_PAGES = 6            # Curiosity's feed is paged; a busy sol is 2 or 3 pages
_PARALLEL = 6             # photo downloads at once
_DARK = "#141414"
_ALL_CAMERAS = "All cameras"


def _request(url, cache_first):
    req = QNetworkRequest(QUrl(url))
    req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, _USER_AGENT)
    req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                     QNetworkRequest.CacheLoadControl.PreferCache if cache_first
                     else QNetworkRequest.CacheLoadControl.PreferNetwork)
    return req


class ImageLoader(QObject):
    """Downloads images a few at a time. ``reset`` drops everything queued or
    in flight, so a superseded sol never fills the grid late."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = []          # (url, callback)
        self._active = {}         # reply -> callback

    def get(self, url, callback, urgent=False):
        if urgent:
            self._queue.insert(0, (url, callback))
        else:
            self._queue.append((url, callback))
        self._pump()

    def reset(self):
        self._queue = []
        for reply in list(self._active):
            self._active.pop(reply, None)
            try:
                reply.abort()
            except RuntimeError:
                pass

    def _pump(self):
        while self._queue and len(self._active) < _PARALLEL:
            url, callback = self._queue.pop(0)
            reply = QgsNetworkAccessManager.instance().get(_request(url, True))
            self._active[reply] = callback
            reply.finished.connect(lambda r=reply: self._done(r))

    def _done(self, reply):
        callback = self._active.pop(reply, None)
        image = None
        try:
            if callback is not None and reply.error() == QNetworkReply.NetworkError.NoError:
                image = QImage.fromData(bytes(reply.readAll()))
        finally:
            reply.deleteLater()
        if callback is not None:
            callback(image if image is not None and not image.isNull() else None)
        self._pump()


_OVERLAY = ("QToolButton { background: rgba(0, 0, 0, 150); color: white; border: none; "
            "border-radius: 4px; padding: 2px 7px; font-weight: bold; }"
            "QToolButton:hover { background: rgba(58, 143, 214, 220); }"
            "QToolButton:disabled { color: rgba(255, 255, 255, 70); background: rgba(0, 0, 0, 90); }")
_LINK = ("QToolButton { border: none; color: palette(link); padding: 0 2px; }"
         "QToolButton:hover { text-decoration: underline; }")


class ElidedLabel(QLabel):
    """One line of text that never wraps or stretches the panel: whatever does
    not fit is shortened with "…", and the full text shows on hover."""

    def __init__(self, text="", bold=False, grey=False, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        if bold:
            font = self.font()
            font.setBold(True)
            self.setFont(font)
        if grey:
            self.setStyleSheet("color: gray;")
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self._elide()

    def full_text(self):
        return self._full

    def _elide(self):
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight,
                                              max(0, self.width() - 2))
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class FilmStrip(QListWidget):
    """A single row of thumbnails. The mouse wheel scrolls it sideways."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setIconSize(_THUMB)
        self.setGridSize(QSize(_THUMB.width() + 6, _THUMB.height() + 6))
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(_THUMB.height() + 18)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            f"QListWidget {{ background: {_DARK}; }}"
            "QListWidget::item { border: 2px solid transparent; }"
            "QListWidget::item:selected { background: transparent; border: 2px solid #3a8fd6; }"
            "QScrollBar:horizontal { height: 6px; background: transparent; margin: 0 4px; }"
            "QScrollBar::handle:horizontal { background: rgba(255, 255, 255, 90); "
            "border-radius: 3px; min-width: 30px; }"
            "QScrollBar::add-line, QScrollBar::sub-line { width: 0; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }")

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - delta)


class ImageView(QGraphicsView):
    """A large photo: fits the panel until zoomed; wheel or +/- zooms, drag pans,
    double-click switches between fit and actual size. Small controls float over
    it (added with ``overlay``) so they take no room of their own."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self._item)
        self.setScene(self._scene)
        self.setBackgroundBrush(QColor(_DARK))
        self.setRenderHints(QPainter.RenderHint.Antialiasing
                            | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._fit = True
        self._overlays = []       # (widget, corner)

    def overlay(self, widget, corner):
        """Float ``widget`` over the photo at a corner: "top-right", "bottom-left",
        "left" or "right" (vertically centred), or "centre"."""
        widget.setParent(self)
        self._overlays.append((widget, corner))
        self._place()

    def _place(self):
        w, h, m = self.width(), self.height(), 8
        for widget, corner in self._overlays:
            widget.adjustSize()
            ww, wh = widget.width(), widget.height()
            x, y = {
                "top-right": (w - ww - m, m),
                "bottom-left": (m, h - wh - m),
                "left": (m, (h - wh) // 2),
                "right": (w - ww - m, (h - wh) // 2),
                "centre": ((w - ww) // 2, (h - wh) // 2),
            }[corner]
            widget.move(max(0, x), max(0, y))
            widget.raise_()

    def set_pixmap(self, pixmap, keep_view=False):
        old = self._item.pixmap()
        self._item.setPixmap(pixmap)
        self._scene.setSceneRect(self._item.boundingRect())
        if keep_view and not old.isNull() and not self._fit and old.width():
            # a sharper copy of the same photo: keep what the user is looking at
            ratio = old.width() / pixmap.width()
            self.scale(ratio, ratio)
        elif self._fit or not keep_view:
            self.fit()

    def clear(self):
        self._item.setPixmap(QPixmap())

    def has_photo(self):
        return not self._item.pixmap().isNull()

    def fit(self):
        self._fit = True
        if not self._item.pixmap().isNull():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def actual_size(self):
        self._fit = False
        self.resetTransform()

    def zoom(self, factor):
        self._fit = False
        self.scale(factor, factor)

    def wheelEvent(self, event):
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.zoom(1.25 ** steps)

    def keyPressEvent(self, event):
        # leave arrows and zoom keys to the panel (previous/next photo)
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Plus,
                           Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            event.ignore()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, _event):
        if self._fit:
            self.actual_size()
        else:
            self.fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit:
            self.fit()
        self._place()


class Spinner(QWidget):
    """A small spinning ring, shown while something loads."""

    def __init__(self, size=34, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._turn)

    def showEvent(self, event):
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _turn(self):
        self._angle = (self._angle + 12) % 360
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = max(3, self.width() // 9)
        rect = QRectF(width, width, self.width() - 2 * width, self.height() - 2 * width)
        painter.setPen(QPen(QColor(255, 255, 255, 50), width))
        painter.drawEllipse(rect)
        pen = QPen(QColor("#3a8fd6"), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, int(-self._angle * 16), 100 * 16)
        painter.end()


class Lightbox(QWidget):
    """The photo laid over the whole QGIS window (not a separate window): ‹ ›
    or the arrow keys step through the sol's photos, Esc or ✕ closes it."""

    def __init__(self, panel, host):
        super().__init__(host)
        self._panel = panel
        self._host = host
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        top = QHBoxLayout()
        self.lbl_caption = ElidedLabel("", bold=True)
        self.lbl_caption.setStyleSheet("color: white;")
        top.addWidget(self.lbl_caption, 1)
        top.addWidget(_tool("✕  Close", "Close the large view (Esc)", self.close_box, _OVERLAY))
        v.addLayout(top)
        self.view = ImageView()
        self.view.setStyleSheet("background: transparent;")
        v.addWidget(self.view, 1)
        self.btn_prev = _tool("‹", "Previous photo (left arrow)", lambda: panel.step(-1), _OVERLAY)
        self.btn_next = _tool("›", "Next photo (right arrow)", lambda: panel.step(1), _OVERLAY)
        for btn in (self.btn_prev, self.btn_next):
            font = btn.font()
            font.setPointSize(font.pointSize() + 14)
            btn.setFont(font)
        self.view.overlay(self.btn_prev, "left")
        self.view.overlay(self.btn_next, "right")
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet("background: rgba(0, 0, 0, 150); color: white; "
                                     "border-radius: 4px; padding: 2px 7px;")
        self.view.overlay(self.lbl_count, "bottom-left")
        self.spinner = Spinner(44)
        self.spinner.hide()
        self.view.overlay(self.spinner, "centre")
        hint = QLabel("Scroll to zoom · drag to pan · double-click to fit · ← → for other photos · Esc to close")
        hint.setStyleSheet("color: rgba(255, 255, 255, 140);")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(hint)
        host.installEventFilter(self)
        self.hide()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(10, 10, 10, 235))
        painter.end()

    def open_box(self):
        self.setGeometry(self._host.rect())
        self.show()
        self.raise_()
        self.setFocus()

    def close_box(self):
        self.hide()

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(self._host.rect())
        return False

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close_box()
        elif key == Qt.Key.Key_Left:
            self._panel.step(-1)
        elif key == Qt.Key.Key_Right:
            self._panel.step(1)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.view.zoom(1.25)
        elif key == Qt.Key.Key_Minus:
            self.view.zoom(1 / 1.25)
        else:
            super().keyPressEvent(event)


def _placeholder():
    pix = QPixmap(_THUMB)
    pix.fill(QColor("#2a2a2a"))
    icon = QIcon(pix)
    icon.addPixmap(pix, QIcon.Mode.Selected)
    return icon


def _thumb_icon(image):
    """Centre a photo in a fixed-size dark tile, so the strip stays even."""
    tile = QPixmap(_THUMB)
    tile.fill(QColor("#202020"))
    scaled = QPixmap.fromImage(image).scaled(
        _THUMB, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    painter = QPainter(tile)
    painter.drawPixmap((_THUMB.width() - scaled.width()) // 2,
                       (_THUMB.height() - scaled.height()) // 2, scaled)
    painter.end()
    icon = QIcon(tile)
    icon.addPixmap(tile, QIcon.Mode.Selected)   # no blue tint on the chosen photo; the frame shows it
    return icon


def _tool(text, tip, handler, style=None):
    btn = QToolButton()
    btn.setText(text)
    btn.setToolTip(tip)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    if style:
        btn.setStyleSheet(style)
    btn.clicked.connect(lambda _=False: handler())
    return btn


class PhotoPanel(QWidget):
    """The photo part of the Sojourn panel. The plugin supplies callbacks:
    ``latest_sol(key)`` the newest sol with photos (or None); ``where(key, site,
    drive)`` words for a stamped location and whether it is on the map;
    ``show_on_map(key, site, drive)``; ``pick_on_map(key)`` to click a stop on the
    map; ``close()`` to hide it; ``located(key, a, b)`` to centre and mark on the
    map where the photos on show were taken; ``nav_sols(key)`` the sols ◀ ▶ step
    through (Ingenuity: its flights), or None for every sol.

    A place is a pair: (site, drive) for a rover's photo, ("sol", n) for
    Ingenuity, whose photos carry no location stamp (it is wherever its last
    flight up to that sol landed)."""

    def __init__(self, latest_sol, where, show_on_map, pick_on_map, close, located,
                 nav_sols, parent=None):
        super().__init__(parent)
        self._nav_sols = nav_sols
        self._lightbox = None
        self._latest_sol = latest_sol
        self._where = where
        self._show_on_map = show_on_map
        self._pick_on_map = pick_on_map
        self._close = close
        self._located = located
        self._marked = None        # (key, site, drive) last shown on the map
        self._pinned = False       # the ring is on a stop picked on the map: leave it
        self._auto_select = False  # the panel, not the user, is choosing a photo
        self._loader = ImageLoader(self)
        self.key = None
        self._sol = None
        self._photos = []          # every photo of the sol, newest first
        self._shown = []           # after the camera filter
        self._index = -1           # photo in the preview
        self._page_reply = None
        self._big_reply = None
        self._thumbs = {}          # photo id -> QImage
        self._build()

    # --- layout ------------------------------------------------------------
    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 6, 0, 0)
        v.setSpacing(6)

        # header: whose photos, and the two actions that act on the panel
        row = QHBoxLayout()
        row.setSpacing(4)
        self.lbl_title = ElidedLabel("Photos", bold=True)
        row.addWidget(self.lbl_title, 1)
        self.btn_pick = _tool("Pick a stop", "Click one of the rover's stops on the map to see "
                              "the photos from that sol", lambda: self._pick_on_map(self.key))
        row.addWidget(self.btn_pick)
        row.addWidget(_tool("✕", "Hide the photos", lambda: self._close()))
        v.addLayout(row)

        # sol bar: one compact row
        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_earlier = _tool("◀", "Earlier sol", lambda: self._step_sol(-1))
        row.addWidget(self.btn_earlier)
        self.spin_sol = QSpinBox()
        self.spin_sol.setPrefix("Sol ")
        self.spin_sol.setRange(0, 0)   # widened once the newest sol is known
        self.spin_sol.setKeyboardTracking(False)   # only load once typing is done
        self.spin_sol.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.spin_sol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spin_sol.setFixedWidth(76)
        self.spin_sol.valueChanged.connect(self._load_sol)
        row.addWidget(self.spin_sol)
        self.btn_later = _tool("▶", "Later sol", lambda: self._step_sol(1))
        row.addWidget(self.btn_later)
        self.btn_latest = _tool("Latest", "The newest sol NASA has posted photos for",
                                lambda: self.go_to_sol(None))
        row.addWidget(self.btn_latest)
        row.addSpacing(6)
        self.cbo_camera = QComboBox()
        self.cbo_camera.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cbo_camera.setMinimumContentsLength(5)
        self.cbo_camera.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.cbo_camera.setToolTip("Show one camera's photos")
        self.cbo_camera.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self.cbo_camera, 1)
        v.addLayout(row)

        self.lbl_sol = ElidedLabel("", grey=True)
        v.addWidget(self.lbl_sol)

        # the photo, the strip and the caption; or a message when there are none
        self.stack = QStackedWidget()
        content = QWidget()
        c = QVBoxLayout(content)
        c.setContentsMargins(0, 0, 0, 0)
        c.setSpacing(4)
        self.view = ImageView()
        self.view.setMinimumHeight(120)
        c.addWidget(self.view, 1)
        self.btn_prev = _tool("‹", "Previous photo (left arrow)", lambda: self.step(-1), _OVERLAY)
        self.btn_next = _tool("›", "Next photo (right arrow)", lambda: self.step(1), _OVERLAY)
        for btn in (self.btn_prev, self.btn_next):
            font = btn.font()
            font.setPointSize(font.pointSize() + 8)
            btn.setFont(font)
        self.view.overlay(self.btn_prev, "left")
        self.view.overlay(self.btn_next, "right")
        tools = QWidget()
        t = QHBoxLayout(tools)
        t.setContentsMargins(0, 0, 0, 0)
        t.setSpacing(3)
        t.addWidget(_tool("−", "Zoom out (-)", lambda: self.view.zoom(1 / 1.25), _OVERLAY))
        t.addWidget(_tool("+", "Zoom in (+, or the mouse wheel)", lambda: self.view.zoom(1.25), _OVERLAY))
        t.addWidget(_tool("Fit", "Fit the photo (double-click it)", lambda: self.view.fit(), _OVERLAY))
        self.btn_expand = _tool("⤢", "View large, over the whole QGIS window (Esc to close)",
                                lambda: self.open_large(), _OVERLAY)
        t.addWidget(self.btn_expand)
        self.view.overlay(tools, "top-right")
        self.lbl_count = QLabel("")
        self.lbl_count.setStyleSheet("background: rgba(0, 0, 0, 150); color: white; "
                                     "border-radius: 4px; padding: 2px 7px;")
        self.view.overlay(self.lbl_count, "bottom-left")
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("background: rgba(0, 0, 0, 150); color: white; "
                                      "border-radius: 4px; padding: 4px 10px;")
        self.lbl_status.hide()
        self.view.overlay(self.lbl_status, "centre")
        self.photo_spinner = Spinner(30)
        self.photo_spinner.hide()
        self.view.overlay(self.photo_spinner, "centre")

        self.strip = FilmStrip()
        self.strip.currentRowChanged.connect(self._on_row)
        c.addWidget(self.strip)

        self.lbl_caption = ElidedLabel("", bold=True)
        c.addWidget(self.lbl_caption)
        row = QHBoxLayout()
        row.setSpacing(2)
        self.lbl_where = ElidedLabel("", grey=True)
        row.addWidget(self.lbl_where, 1)
        self.btn_map = _tool("Map", "Centre the map on where this photo was taken",
                             lambda: self._map_current(), _LINK)
        self.btn_full = _tool("Full size", "Load the original full-size image (can be several MB)",
                              lambda: self._load_big(full=True), _LINK)
        self.btn_nasa = _tool("NASA ↗", "Open this photo's page on NASA's website",
                              lambda: self._open_nasa(), _LINK)
        for btn in (self.btn_map, self.btn_full, self.btn_nasa):
            row.addWidget(btn)
        c.addLayout(row)
        self.stack.addWidget(content)

        # loading or nothing to show: a spinner and/or a short message, centred
        message = QWidget()
        message.setStyleSheet(f"background: {_DARK};")
        m = QVBoxLayout(message)
        m.addStretch(1)
        self.spinner = Spinner(40)
        m.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        self.lbl_empty = QLabel("")
        self.lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_empty.setWordWrap(True)
        self.lbl_empty.setStyleSheet("color: #b0b0b0; padding: 8px 24px;")
        m.addWidget(self.lbl_empty)
        m.addStretch(1)
        self.stack.addWidget(message)
        v.addWidget(self.stack, 1)

        # never squeeze below this: below it widgets would overlap
        self.setMinimumHeight(320)
        self._clear_caption()
        self._enable_nav()

    # --- rover and sol -------------------------------------------------------
    def show_rover(self, key, sol=None, pinned=False):
        """Show a rover's photos at ``sol`` (None: the newest sol with photos).
        ``pinned``: the sol was opened by picking a stop on the map, which is
        already ringed; keep the ring there until the user picks a photo."""
        if key is None:
            return
        if key != self.key:
            self.key = key
            self.lbl_title.setText(f"{MISSIONS[key]['name']} photos")
            self.cbo_camera.blockSignals(True)
            self.cbo_camera.clear()
            self.cbo_camera.blockSignals(False)
        self.go_to_sol(sol, pinned)

    def go_to_sol(self, sol, pinned=False):
        if self.key is None:
            return
        if sol is None:
            sol = self._latest_sol(self.key)
            if sol is None:
                self._sol = None
                self._show_message("", loading=True)
                return
        self.update_latest()
        sol = min(max(0, int(sol)), self.spin_sol.maximum())
        self.spin_sol.blockSignals(True)
        self.spin_sol.setValue(sol)
        self.spin_sol.blockSignals(False)
        self._load_sol(sol, pinned)

    def update_latest(self):
        """Limit the sol box to the newest sol NASA has posted photos for (the
        limit rises by itself when NASA posts a new sol)."""
        latest = self._latest_sol(self.key) if self.key else None
        self.spin_sol.blockSignals(True)
        self.spin_sol.setMaximum(latest if latest is not None else 99999)
        self.spin_sol.blockSignals(False)
        self.spin_sol.setToolTip("Mars day since landing; type a number to jump to it" + (
            f" (newest with photos: {latest})" if latest is not None else ""))
        if self._sol is not None:
            self._enable_sol_buttons()

    def waiting_for_latest(self):
        return self.key is not None and self._sol is None

    def _load_sol(self, sol, pinned=False):
        if self.key is None:
            return
        self._sol = sol
        self._pinned = pinned
        if pinned:
            self._marked = None   # the next photo the user picks moves the ring
        self._loader.reset()
        self._abort(self._page_reply)
        self._abort(self._big_reply)
        self._page_reply = self._big_reply = None
        self._photos, self._shown, self._thumbs, self._index = [], [], {}, -1
        self.strip.blockSignals(True)
        self.strip.clear()
        self.strip.blockSignals(False)
        self.view.clear()
        self._clear_caption()
        self._reset_camera_counts()
        self._show_message("", loading=True)
        self.lbl_sol.setText(f"Sol {sol}")
        self._enable_sol_buttons()
        self._enable_nav()
        self._fetch_page(self.key, sol, 0)

    def _step_sol(self, delta):
        """◀ ▶: the previous or next sol (for Ingenuity, its previous or next flight)."""
        if self._sol is None:
            return
        sols = self._nav_sols(self.key)
        if not sols:
            self.go_to_sol(self._sol + delta)
            return
        later = [s for s in sols if s > self._sol]
        earlier = [s for s in sols if s < self._sol]
        target = (later[0] if later else None) if delta > 0 else (earlier[-1] if earlier else None)
        if target is not None:
            self.go_to_sol(target)

    def _enable_sol_buttons(self):
        latest = self._latest_sol(self.key) if self.key else None
        sols = self._nav_sols(self.key) if self.key else None
        sol = self._sol if self._sol is not None else 0
        if sols:
            self.btn_earlier.setEnabled(any(s < sol for s in sols))
            self.btn_later.setEnabled(any(s > sol for s in sols))
            self.btn_earlier.setToolTip("Previous flight")
            self.btn_later.setToolTip("Next flight")
        else:
            self.btn_earlier.setEnabled(sol > 0)
            self.btn_later.setEnabled(latest is None or sol < latest)
            self.btn_earlier.setToolTip("Earlier sol")
            self.btn_later.setToolTip("Later sol")

    def _fetch_page(self, key, sol, page):
        reply = QgsNetworkAccessManager.instance().get(_request(photos_url(key, sol, page), False))
        self._page_reply = reply
        reply.finished.connect(lambda r=reply, k=key, s=sol, p=page: self._on_page(r, k, s, p))

    def _on_page(self, reply, key, sol, page):
        if reply is not self._page_reply or (key, sol) != (self.key, self._sol):
            reply.deleteLater()
            return
        self._page_reply = None
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self._show_message(f"Could not load photos from NASA:\n{reply.errorString()}")
                self.lbl_sol.setText(f"Sol {sol}")
                return
            raw = bytes(reply.readAll())
        finally:
            reply.deleteLater()
        try:
            photos, more = parse_photos(key, json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            QgsMessageLog.logMessage(f"photos: {exc}", "Mars Sojourn", Qgis.MessageLevel.Warning)
            self._show_message("NASA's photo feed sent something unexpected.")
            return
        # NASA's Ingenuity feed answers a sol with no photos with another sol's,
        # so keep only what was taken on the sol asked for
        self._photos.extend(p for p in photos if p["sol"] in (None, sol))
        if more and page + 1 < _MAX_PAGES:
            self._fetch_page(key, sol, page + 1)
            return
        self._photos.sort(key=lambda p: p["taken"].timestamp() if p["taken"] else 0,
                          reverse=True)
        self._fill_cameras()
        self._describe_sol()

    def _reset_camera_counts(self):
        """While a sol loads, drop the previous sol's photo counts from the camera
        list (keeping the chosen camera), so nothing on screen is stale."""
        current = self.cbo_camera.currentData()
        self.cbo_camera.blockSignals(True)
        self.cbo_camera.clear()
        self.cbo_camera.addItem(_ALL_CAMERAS, _ALL_CAMERAS)
        if current not in (None, _ALL_CAMERAS):
            self.cbo_camera.addItem(current, current)
            self.cbo_camera.setCurrentIndex(1)
        self.cbo_camera.blockSignals(False)

    def _fill_cameras(self):
        current = self.cbo_camera.currentData()
        counts = {}
        for p in self._photos:
            counts[p["camera"]] = counts.get(p["camera"], 0) + 1
        self.cbo_camera.blockSignals(True)
        self.cbo_camera.clear()
        self.cbo_camera.addItem(f"{_ALL_CAMERAS} ({len(self._photos)})", _ALL_CAMERAS)
        for name in sorted(counts):
            self.cbo_camera.addItem(f"{name} ({counts[name]})", name)
        self.cbo_camera.setCurrentIndex(max(0, self.cbo_camera.findData(current)))
        self.cbo_camera.blockSignals(False)
        self._apply_filter()

    def _apply_filter(self):
        camera = self.cbo_camera.currentData()
        self._shown = [p for p in self._photos
                       if camera in (None, _ALL_CAMERAS) or p["camera"] == camera]
        self._loader.reset()
        self.strip.blockSignals(True)
        self.strip.clear()
        self.strip.blockSignals(False)
        self._index = -1
        if not self._photos:
            self._show_message(f"NASA has not posted any photos for sol {self._sol}.")
            self._enable_nav()
            return
        self.stack.setCurrentIndex(0)
        blank = _placeholder()
        for i, p in enumerate(self._shown):
            item = QListWidgetItem(blank, "")
            item.setData(Qt.ItemDataRole.UserRole, i)
            when = p["taken"].strftime("%H:%M UTC") if p["taken"] else ""
            item.setToolTip(f"{p['camera']}\n{when}")
            self.strip.addItem(item)
            if p["id"] in self._thumbs:
                item.setIcon(_thumb_icon(self._thumbs[p["id"]]))
            elif p["thumb"]:
                self._loader.get(p["thumb"], lambda img, it=item, pid=p["id"]:
                                 self._on_thumb(it, pid, img))
        self._auto_select = True
        self.strip.setCurrentRow(0)   # shows the newest photo
        self._auto_select = False

    def _on_thumb(self, item, photo_id, image):
        if image is None:
            return
        self._thumbs[photo_id] = image
        try:
            item.setIcon(_thumb_icon(image))
        except RuntimeError:   # the strip was cleared meanwhile
            return
        if 0 <= self._index < len(self._shown) and self._shown[self._index]["id"] == photo_id \
                and not self.view.has_photo():
            self.view.set_pixmap(QPixmap.fromImage(image))

    def _sol_location(self):
        """Where most of the sol's photos were taken (see the class notes), or None."""
        if self.key == "heli":
            return ("sol", self._sol) if self._sol is not None else None
        counts = {}
        for p in self._photos:
            if p["site"] is not None and p["drive"] is not None:
                counts[(p["site"], p["drive"])] = counts.get((p["site"], p["drive"]), 0) + 1
        return max(counts, key=counts.get) if counts else None

    def _describe_sol(self):
        days = sorted({p["taken"].date() for p in self._photos if p["taken"]})
        n = len(self._photos)
        parts = [f"Sol {self._sol}"]
        if days:
            parts.append(format_date(days[-1]))
        if self.key == "heli":
            location = self._sol_location()
            where, _can_show = self._where(self.key, *location)
            parts.append(where)
        parts.append(f"{n} photo{'s' if n != 1 else ''}")
        self.lbl_sol.setText(" · ".join(parts))
        self._mark(self._sol_location())

    def _mark(self, location):
        """Centre the map on where the photos were taken and mark the spot, but
        only when that place changes, so stepping through photos taken at one
        stop leaves the map alone."""
        if location is None or self._pinned:
            return
        marked = (self.key, location[0], location[1])
        if marked != self._marked:
            self._marked = marked
            self._located(self.key, *location)

    def _show_message(self, text, loading=False):
        self.lbl_empty.setText(text)
        self.lbl_empty.setVisible(bool(text))
        self.spinner.setVisible(loading)
        self.stack.setCurrentIndex(1)

    # --- the photo in the preview --------------------------------------------
    def _on_row(self, index):
        if not self._auto_select:
            self._pinned = False   # the user chose a photo: the ring follows it
        self.open_photo(index)

    def open_photo(self, index):
        if not 0 <= index < len(self._shown):
            return
        self._index = index
        p = self._shown[index]
        if self.strip.currentRow() != index:
            self.strip.blockSignals(True)
            self.strip.setCurrentRow(index)
            self.strip.blockSignals(False)
        self.strip.scrollToItem(self.strip.item(index))
        self._enable_nav()
        thumb = self._thumbs.get(p["id"])
        if thumb is not None:
            self.view.set_pixmap(QPixmap.fromImage(thumb))   # instant, sharpened below
        else:
            self.view.clear()
        self._caption(p)
        if self.key != "heli" and p["site"] is not None and p["drive"] is not None:
            self._mark((p["site"], p["drive"]))   # a photo from mid-drive moves the mark
        self._sync_large()
        self._load_big(full=False)

    def _enable_nav(self):
        n = len(self._shown)
        has = n > 0 and self._index >= 0
        self.lbl_count.setText(f"{self._index + 1} / {n}" if has else "")
        self.lbl_count.setVisible(has)
        self.lbl_count.adjustSize()
        self.btn_prev.setEnabled(self._index > 0)
        self.btn_next.setEnabled(0 <= self._index < n - 1)
        self.btn_pick.setEnabled(self.key is not None)

    def _clear_caption(self):
        self.lbl_caption.setText("")
        self.lbl_where.setText("")
        for btn in (self.btn_map, self.btn_full, self.btn_nasa):
            btn.hide()

    def _location_of(self, p):
        if self.key != "heli" and p["site"] is not None and p["drive"] is not None:
            return p["site"], p["drive"]
        return self._sol_location()

    def _caption(self, p, state=""):
        parts = [p["camera"]]
        if p["taken"]:
            parts.append(f"{format_date(p['taken'].date())}, {p['taken'].strftime('%H:%M')} UTC")
        if p["local"]:
            parts.append(f"{p['local']} Mars time")
        self.lbl_caption.setText(" · ".join(parts))
        self.lbl_caption.setToolTip(
            f"{' · '.join(parts)}\n{p['id']}\nImage credit: {p['credit']}")
        location = self._location_of(p)
        can_show = False
        if location:
            where, can_show = self._where(self.key, *location)
            self.lbl_where.setText(where[0].upper() + where[1:])
        else:
            self.lbl_where.setText("No location stamp on this photo")
        self.btn_map.setVisible(can_show)
        self.btn_full.setVisible(bool(p["full"]) and p["full"] != p["view"])
        self.btn_nasa.setVisible(bool(p["link"]))
        self._status(state)

    def _status(self, text, loading=False):
        self.lbl_status.setText(text)
        self.lbl_status.setVisible(bool(text))
        self.lbl_status.adjustSize()
        self.photo_spinner.setVisible(loading)
        self.view._place()
        if self._lightbox is not None:
            self._lightbox.spinner.setVisible(loading)
            self._lightbox.view._place()

    # --- the large view ------------------------------------------------------
    def open_large(self):
        if not 0 <= self._index < len(self._shown):
            return
        if self._lightbox is None:
            self._lightbox = Lightbox(self, self.window())
        self._lightbox.open_box()
        self._sync_large()
        if self._big_reply is not None:
            self._lightbox.spinner.show()

    def _sync_large(self):
        """Keep the large view showing what the preview shows."""
        box = self._lightbox
        if box is None or not box.isVisible() or not 0 <= self._index < len(self._shown):
            return
        box.lbl_caption.setText(f"{self.lbl_title.full_text()} · {self.lbl_sol.full_text()} · "
                                f"{self.lbl_caption.full_text()}")
        box.lbl_count.setText(f"{self._index + 1} / {len(self._shown)}")
        box.lbl_count.adjustSize()
        box.btn_prev.setEnabled(self._index > 0)
        box.btn_next.setEnabled(self._index < len(self._shown) - 1)
        thumb = self._thumbs.get(self._shown[self._index]["id"])
        pixmap = self.view._item.pixmap()
        if pixmap.isNull() and thumb is not None:
            pixmap = QPixmap.fromImage(thumb)
        if not pixmap.isNull():
            box.view.set_pixmap(pixmap)
        box.view._place()

    def close_large(self):
        if self._lightbox is not None:
            self._lightbox.close_box()

    def _map_current(self):
        if 0 <= self._index < len(self._shown):
            location = self._location_of(self._shown[self._index])
            if location:
                self._show_on_map(self.key, *location)

    def _open_nasa(self):
        if 0 <= self._index < len(self._shown) and self._shown[self._index]["link"]:
            QDesktopServices.openUrl(QUrl(self._shown[self._index]["link"]))

    def _load_big(self, full):
        if not 0 <= self._index < len(self._shown):
            return
        p = self._shown[self._index]
        url = p["full"] if full else p["view"]
        if not url:
            return
        self._abort(self._big_reply)
        reply = QgsNetworkAccessManager.instance().get(_request(url, True))
        self._big_reply = reply
        self._status("", loading=True)
        reply.finished.connect(lambda r=reply, i=self._index, f=full: self._on_big(r, i, f))

    def _on_big(self, reply, index, full):
        if reply is not self._big_reply:
            reply.deleteLater()
            return
        self._big_reply = None
        image = None
        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                image = QImage.fromData(bytes(reply.readAll()))
        finally:
            reply.deleteLater()
        if index != self._index:
            return
        if image is None or image.isNull():
            self._status("Could not load this photo")
            return
        pixmap = QPixmap.fromImage(image)
        self.view.set_pixmap(pixmap, keep_view=True)
        if self._lightbox is not None and self._lightbox.isVisible():
            self._lightbox.view.set_pixmap(pixmap, keep_view=True)
        self._status("")
        if full:
            self.btn_full.hide()

    def step(self, delta):
        self._pinned = False   # stepping through photos: the ring follows them
        self.open_photo(self._index + delta)

    def set_picking(self, on):
        self.btn_pick.setText("Click a stop on the map…" if on else "Pick a stop")
        self.btn_pick.setToolTip("Esc cancels" if on else
                                 "Click any rover's stop (or an Ingenuity landing spot) on the "
                                 "map to see the photos from that sol")
        self.btn_pick.setEnabled(not on)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Left:
            self.step(-1)
        elif key == Qt.Key.Key_Right:
            self.step(1)
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.view.zoom(1.25)
        elif key == Qt.Key.Key_Minus:
            self.view.zoom(1 / 1.25)
        else:
            super().keyPressEvent(event)

    def stop(self):
        self._marked = None
        self.close_large()
        self._loader.reset()
        self._abort(self._page_reply)
        self._abort(self._big_reply)
        self._page_reply = self._big_reply = None

    @staticmethod
    def _abort(reply):
        if reply is not None:
            try:
                reply.abort()
            except RuntimeError:
                pass
