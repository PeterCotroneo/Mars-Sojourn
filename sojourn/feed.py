"""Fetches a mission's files from NASA.

``load`` fetches all three files (latest position, stops, route). ``check``
fetches only the tiny latest-position file and re-fetches the rest only if the
rover has moved, so the hourly check costs about a kilobyte.

Fetched asynchronously through QGIS's network manager (respects the user's proxy
settings), off the GUI thread. QtNetwork is used via ``qgis.PyQt`` with fully
scoped enums for Qt6.
"""

import json

from qgis.PyQt.QtCore import QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkRequest, QNetworkReply
from qgis.core import QgsNetworkAccessManager, QgsMessageLog, Qgis

from .missions import MISSIONS, parse_latest_photo, parse_position, parse_stops, parse_route, photos_url

_USER_AGENT = "Mars Sojourn QGIS plugin (+https://github.com/PeterCotroneo/Mars-Sojourn)"
_ALL = ("current", "stops", "route")


class MissionFeed(QObject):
    # mission key, {"position", "stops", "route", "published" (epoch seconds or None)}
    loaded = pyqtSignal(str, dict)
    unchanged = pyqtSignal(str)      # checked: the rover has not moved
    error = pyqtSignal(str, str)     # mission key, message
    photo = pyqtSignal(str, dict)    # mission key, newest raw photo (see parse_latest_photo)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs = {}
        self._photo_replies = {}
        self._warm = set()           # requests that only warm NASA's cache

    def load(self, key):
        self._start(key, _ALL, None)

    def check(self, key, known_id):
        """Re-load ``key`` only if its latest position differs from ``known_id``."""
        if key not in self._jobs:
            self._start(key, ("current",), known_id)

    def check_photos(self, key):
        """Fetch the rover's newest raw photo: it shows, day by day, whether the
        rover is still where the map has it. Failures are only logged; the panel
        simply says nothing about photos."""
        url = MISSIONS[key].get("photos")
        if not url or key in self._photo_replies:
            return
        req = QNetworkRequest(QUrl(url))
        req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, _USER_AGENT)
        req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                         QNetworkRequest.CacheLoadControl.AlwaysNetwork)
        reply = QgsNetworkAccessManager.instance().get(req)
        self._photo_replies[key] = reply
        reply.finished.connect(lambda k=key, r=reply: self._on_photo(k, r))

    def _on_photo(self, key, reply):
        if self._photo_replies.get(key) is not reply:
            reply.deleteLater()
            return
        del self._photo_replies[key]
        name = MISSIONS[key]["name"]
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                QgsMessageLog.logMessage(f"{name} photos: {reply.errorString()}", "Mars Sojourn",
                                         Qgis.MessageLevel.Warning)
                return
            raw = bytes(reply.readAll())
        finally:
            reply.deleteLater()
        try:
            latest = parse_latest_photo(key, json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            QgsMessageLog.logMessage(f"{name} photos: could not read the feed ({exc})",
                                     "Mars Sojourn", Qgis.MessageLevel.Warning)
            return
        if latest is not None:
            self._warm_up(key, latest["sol"])
            self.photo.emit(key, latest)

    def _warm_up(self, key, sol):
        """Ask NASA for the newest sol's photo list in the background. NASA's
        server takes about 15 seconds the first time a sol is asked for and is
        quick after that, so pressing Photos is then near-instant."""
        req = QNetworkRequest(QUrl(photos_url(key, sol)))
        req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, _USER_AGENT)
        reply = QgsNetworkAccessManager.instance().get(req)
        self._warm.add(reply)
        reply.finished.connect(lambda r=reply: (self._warm.discard(r), r.deleteLater()))

    def busy(self, key):
        return key in self._jobs

    def stop(self, key=None):
        for k in [key] if key else list(MISSIONS):
            job = self._jobs.pop(k, None)
            replies = list((job or {}).get("replies", {}).values())
            photo_reply = self._photo_replies.pop(k, None)
            if photo_reply is not None:
                replies.append(photo_reply)
            for reply in replies:
                try:
                    reply.abort()
                except RuntimeError:   # already deleted
                    continue

    # --- internals -------------------------------------------------------
    def _start(self, key, names, known_id):
        self.stop(key)
        self._jobs[key] = {"replies": {}, "docs": {}, "known": known_id, "published": None}
        self._fetch(key, names)

    def _fetch(self, key, names):
        job = self._jobs[key]
        for name in names:
            req = QNetworkRequest(QUrl(MISSIONS[key]["files"][name]))
            req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, _USER_AGENT)
            if name == "current":   # never answer the "has it moved?" check from cache
                req.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                                 QNetworkRequest.CacheLoadControl.AlwaysNetwork)
            reply = QgsNetworkAccessManager.instance().get(req)
            job["replies"][name] = reply
            reply.finished.connect(lambda k=key, n=name, r=reply: self._on_finished(k, n, r))

    def _fail(self, key, text):
        self.stop(key)
        self.error.emit(key, f"{MISSIONS[key]['name']}: {text}")

    def _on_finished(self, key, name, reply):
        job = self._jobs.get(key)
        if job is None or job["replies"].get(name) is not reply:
            reply.deleteLater()   # stopped or superseded
            return
        del job["replies"][name]
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self._fail(key, reply.errorString())
                return
            raw = bytes(reply.readAll())
            if name == "current":
                stamp = reply.header(QNetworkRequest.KnownHeaders.LastModifiedHeader)
                if stamp is not None and stamp.isValid():
                    job["published"] = stamp.toSecsSinceEpoch()
        finally:
            reply.deleteLater()
        try:
            job["docs"][name] = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._fail(key, f"could not read NASA's {name} file ({exc})")
            return

        if name == "current" and job["known"] is not None:
            position = parse_position(key, job["docs"]["current"])
            if position is not None and position["id"] == job["known"]:
                del self._jobs[key]
                self.unchanged.emit(key)
                return
            job["known"] = None           # it moved: fetch the stops and route too
            self._fetch(key, ("stops", "route"))
            return
        if job["replies"]:
            return                        # still waiting for the other files
        del self._jobs[key]
        try:
            docs = job["docs"]
            data = {
                "position": parse_position(key, docs["current"]),
                "stops": parse_stops(key, docs["stops"]),
                "route": parse_route(key, docs["route"]),
                "published": job["published"],
            }
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self.error.emit(key, f"{MISSIONS[key]['name']}: unexpected file layout ({exc})")
            return
        if data["position"] is None:
            self.error.emit(key, f"{MISSIONS[key]['name']}: NASA's file has no position")
            return
        QgsMessageLog.logMessage(
            f"{MISSIONS[key]['name']}: {len(data['stops'])} stops, "
            f"{len(data['route'])} route pieces", "Mars Sojourn", Qgis.MessageLevel.Info)
        self.loaded.emit(key, data)
