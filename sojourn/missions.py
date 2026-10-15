"""The missions and how to read NASA's files for them.

NASA/JPL publish each rover's localisation as GeoJSON for their public "Where is
the rover?" maps: the latest position, every stop (waypoint) and the driven route
(traverse). Coordinates are Mars planetocentric longitude/latitude. The files
change only when a rover drives, so there is no fixed update interval.

Nothing in this module needs QGIS, so the parsing can be tested on its own.
"""

import datetime

_BASE = "https://mars.nasa.gov/mmgis-maps"
SOL_SECONDS = 88775.244   # one Mars solar day
_UTC = datetime.timezone.utc

ROVER = "rover"
HELICOPTER = "helicopter"

# ``sol0`` is local midnight at the landing site at the start of sol 0 (UTC),
# worked out from photos NASA stamps with both UTC and Mars local time. It turns
# a sol number into the Earth date of that sol's early afternoon, when rovers
# usually drive.
MISSIONS = {
    "m20": {
        "name": "Perseverance", "kind": ROVER, "colour": "#35d0ff", "place": "Jezero crater",
        "landed": "18 February 2021",
        "sol0": datetime.datetime(2021, 2, 18, 4, 24, 16, tzinfo=_UTC),
        "files": {
            "current": _BASE + "/M20/Layers/json/M20_waypoints_current.json",
            "stops": _BASE + "/M20/Layers/json/M20_waypoints.json",
            "route": _BASE + "/M20/Layers/json/M20_traverse.json",
        },
        "map": "https://science.nasa.gov/mission/mars-2020-perseverance/location-map/",
        "photos": "https://mars.nasa.gov/rss/api/?feed=raw_images&category=mars2020"
                  "&feedtype=json&num=1&page=0&order=sol+desc",
    },
    "msl": {
        "name": "Curiosity", "kind": ROVER, "colour": "#ffd23f", "place": "Gale crater",
        "landed": "6 August 2012",
        "sol0": datetime.datetime(2012, 8, 5, 13, 49, 59, tzinfo=_UTC),
        "files": {
            "current": _BASE + "/MSL/Layers/json/MSL_waypoints_current.json",
            "stops": _BASE + "/MSL/Layers/json/MSL_waypoints.json",
            "route": _BASE + "/MSL/Layers/json/MSL_traverse.json",
        },
        "map": "https://science.nasa.gov/mission/msl-curiosity/location-map/",
        # only the navigation and hazard cameras stamp their photos with a location
        "photos": "https://mars.nasa.gov/api/v1/raw_image_items/?order=sol+desc,date_taken+desc"
                  "&per_page=1&page=0&condition_1=msl:mission&search=NAV_LEFT_B|NAV_RIGHT_B"
                  "|FHAZ_LEFT_B|FHAZ_RIGHT_B|RHAZ_LEFT_B|RHAZ_RIGHT_B",
    },
    "heli": {
        "name": "Ingenuity", "kind": HELICOPTER, "colour": "#9be564", "place": "Jezero crater",
        "carrier": "m20",   # Perseverance carried Ingenuity to Mars
        # Ingenuity's own cameras have their own NASA feed
        "photos": "https://mars.nasa.gov/rss/api/?feed=raw_images&category=ingenuity"
                  "&feedtype=json&num=1&page=0&order=sol+desc",
        "files": {
            "current": _BASE + "/M20/Layers/json/m20_heli_waypoints_current.json",
            "stops": _BASE + "/M20/Layers/json/m20_heli_waypoints.json",
            "route": _BASE + "/M20/Layers/json/m20_heli_flight_path.json",
        },
        "map": "https://science.nasa.gov/mission/mars-2020-perseverance/location-map/",
    },
}

# NASA's flag on each Perseverance waypoint -> what it means
_M20_KINDS = {"y": "End of drive", "m": "Mid-drive", "i": "Interpolated (not measured)"}


DRIVE_HOUR = 13   # Mars local time used to date a sol (rovers drive around midday)


def sol_date(key, sol):
    """Earth date (UTC) of a mission sol at 13:00 Mars local time, or None."""
    start = MISSIONS[key].get("sol0")
    if start is None or sol is None:
        return None
    return (start + datetime.timedelta(seconds=(sol + DRIVE_HOUR / 24) * SOL_SECONDS)).date()


def format_date(day):
    return f"{day.day} {day.strftime('%b %Y')}" if day else ""


def day_text(day, today=None):
    """"today", "yesterday" or the date, for a datetime.date (UTC)."""
    today = today or datetime.datetime.now(_UTC).date()
    days = (today - day).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    return format_date(day)


def parse_latest_photo(key, doc):
    """The newest raw photo NASA has posted for a rover, as a dict with its
    ``sol``, ``day`` (datetime.date, UTC) and the ``site`` and ``drive`` counters
    the rover stamps on it. Those counters only change when the rover drives, so
    a photo carrying the same pair as the mapped position was taken from that
    spot. Returns None if the feed holds no usable photo."""
    items = doc.get("images") or doc.get("items") or []
    for item in items:
        taken = item.get("date_taken_utc") or item.get("date_taken") or ""
        try:
            day = datetime.date.fromisoformat(taken[:10])
            sol = int(item["sol"])
        except (KeyError, TypeError, ValueError):
            continue
        # Ingenuity's photos carry no usable site/drive stamp
        return {"sol": sol, "day": day, "site": _int(item.get("site")), "drive": _int(item.get("drive"))}
    return None


def photo_status(position, photo):
    """What the newest photo says about the mapped position.

    Returns (state, text): state is "here" (the rover was still at the mapped
    spot when the photo was taken; nothing to say), "moved" (the photo is from
    somewhere NASA has not mapped yet) or None (nothing known).
    """
    if not position or not photo or photo["sol"] < position["sol"] \
            or photo["site"] is None or photo["drive"] is None:
        return None, ""
    when = f"{day_text(photo['day'])} (sol {photo['sol']})"
    if (photo["site"], photo["drive"]) == (position["site"], position["drive"]):
        return "here", ""
    return "moved", (f"Has driven since: photos from {when} are from a new spot "
                     "NASA has not mapped yet")


def _nasa_date(text):
    """NASA's "Jan. 18, 2024" / "April 19, 2021" as "18 Jan 2024" (or as given)."""
    text = (text or "").strip()
    for fmt in ("%b. %d, %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return format_date(datetime.datetime.strptime(text, fmt).date())
        except ValueError:
            continue
    return text


def _mars_time_utc(key, sol, lmst):
    """UTC time of a Mars local time ("13:35:56.5") on a mission sol, or None."""
    start = MISSIONS[key].get("sol0")
    try:
        h, mnt, sec = lmst.split(":")
        hours = int(h) + int(mnt) / 60 + float(sec) / 3600
    except (AttributeError, ValueError):
        return None
    if start is None or sol is None:
        return None
    return start + datetime.timedelta(seconds=(sol + hours / 24) * SOL_SECONDS)


def _num(value):
    """A float, or None for missing values and NASA's -9999 placeholder."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number <= -9999 else number


def _int(value):
    number = _num(value)
    return None if number is None else int(number)


def _point(feature):
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    if len(coords) < 2:
        return None
    return float(coords[0]), float(coords[1])


def _stop(key, feature):
    p = feature.get("properties") or {}
    point = _point(feature)
    if point is None:
        return None
    if MISSIONS[key]["kind"] == HELICOPTER:
        total_m = _num(p.get("Total_m"))
        return {
            "lon": point[0], "lat": point[1],
            "flight": _int(p.get("Flight")), "sol": _int(p.get("Sol")),
            "date": _nasa_date(p.get("Earth_Date")),
            "drive_m": _num(p.get("Dist_m")),
            "total_km": None if total_m is None else total_m / 1000.0,
            "elev_m": _num(p.get("Elev_Geoid")), "max_alt_m": _num(p.get("Max_Alt_m")),
            "duration_s": _num(p.get("Duration_s")),
            "from": p.get("FromAirfld") or "", "to": p.get("ToAirfld") or "",
            "heading": _num(p.get("yaw")),
            "id": f"flight {p.get('Flight')}",
        }
    sol = _int(p.get("sol"))
    total_m = _num(p.get("dist_total_m"))
    kind = _M20_KINDS.get(p.get("final")) or p.get("Note") or "End of drive"
    heading = _num(p.get("yaw"))
    return {
        "lon": point[0], "lat": point[1], "sol": sol,
        "date": format_date(sol_date(key, sol)), "kind": kind,
        "drive_m": _num(p.get("dist_m")),
        "total_km": None if total_m is None else total_m / 1000.0,
        "elev_m": _num(p.get("elev_geoid")), "tilt": _num(p.get("tilt")),
        "heading": None if heading is None else heading % 360.0,
        "site": _int(p.get("site")), "drive": _int(p.get("drive")),
        "id": f"{p.get('RMC')}@{sol}",
    }


def parse_stops(key, doc):
    stops = [_stop(key, f) for f in doc.get("features") or []]
    return [s for s in stops if s is not None]


def parse_position(key, doc):
    """The latest position (the last feature of NASA's "current" file), or None."""
    stops = parse_stops(key, doc)
    return stops[-1] if stops else None


def parse_route(key, doc):
    """Route pieces as dicts: ``parts`` is a list of lines, each a list of (lon, lat)."""
    pieces = []
    for f in doc.get("features") or []:
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if geom.get("type") == "LineString":
            coords = [coords]
        elif geom.get("type") != "MultiLineString":
            continue
        parts = [[(float(c[0]), float(c[1])) for c in line if len(c) >= 2] for line in coords]
        parts = [line for line in parts if len(line) >= 2]
        if not parts:
            continue
        p = f.get("properties") or {}
        sol = _int(p.get("sol") if "sol" in p else p.get("Sol"))
        length = p.get("length", p.get("Length_m", p.get("Length")))
        pieces.append({
            "parts": parts, "sol": sol, "flight": _int(p.get("Flight")),
            "date": format_date(sol_date(key, sol)), "length_m": _num(length),
        })
    return pieces


# --- raw photos ---------------------------------------------------------------
# Each rover's raw images, as NASA posts them for its public raw-image galleries:
# every camera, every sol, usually within hours of reaching Earth.

_PHOTO_PAGE = 200   # Curiosity's feed is paged; Perseverance's returns a whole sol

# instrument code prefix -> camera name shown to people (first match wins)
_CAMERAS = [
    ("NAVCAM_LEFT", "Navcam (left)"), ("NAVCAM_RIGHT", "Navcam (right)"),
    ("NAV_LEFT", "Navcam (left)"), ("NAV_RIGHT", "Navcam (right)"),
    ("MCZ_LEFT", "Mastcam-Z (left)"), ("MCZ_RIGHT", "Mastcam-Z (right)"),
    ("MAST_LEFT", "Mastcam (left)"), ("MAST_RIGHT", "Mastcam (right)"),
    ("FRONT_HAZCAM", "Front Hazcam"), ("FHAZ", "Front Hazcam"),
    ("REAR_HAZCAM", "Rear Hazcam"), ("RHAZ", "Rear Hazcam"),
    ("SHERLOC_WATSON", "WATSON (arm close-up)"), ("SHERLOC_ACI", "SHERLOC context imager"),
    ("PIXL", "PIXL micro-context camera"), ("SUPERCAM", "SuperCam remote micro-imager"),
    ("CHEMCAM", "ChemCam remote micro-imager"), ("MAHLI", "MAHLI (arm close-up)"),
    ("MARDI", "MARDI (descent camera)"), ("SKYCAM", "SkyCam (weather)"),
    ("CACHECAM", "CacheCam (sample cache)"), ("EDL", "Landing cameras"),
    ("HELI_NAV", "Ingenuity navigation camera"), ("HELI_RTE", "Ingenuity colour camera"),
    ("LCAM", "Lander vision camera"),
]


def camera_name(code):
    code = (code or "").upper()
    for prefix, name in _CAMERAS:
        if code.startswith(prefix):
            return name
    return code.replace("_", " ").title() or "Unknown camera"


def photos_url(key, sol, page=0):
    if key in ("m20", "heli"):
        category = "mars2020" if key == "m20" else "ingenuity"
        return (f"https://mars.nasa.gov/rss/api/?feed=raw_images&category={category}"
                f"&feedtype=json&sol={int(sol)}")
    return ("https://mars.nasa.gov/api/v1/raw_image_items/?order=date_taken+desc"
            f"&per_page={_PHOTO_PAGE}&page={int(page)}&condition_1=msl:mission"
            f"&condition_2={int(sol)}:sol:in")


def _when(text):
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "")[:19]).replace(tzinfo=_UTC)
    except (AttributeError, ValueError):
        return None


def _dimension(text):
    try:
        w, h = (int(v) for v in (text or "").strip("()").split(","))
        return w, h
    except ValueError:
        return None


def parse_photos(key, doc):
    """One page of a sol's photos as (photos, more_pages). Each photo is a dict:
    id, sol, taken (UTC datetime), local (Mars local time "13:35" or ""),
    camera, code, thumb, view, full (URLs), link (NASA's page for the photo),
    credit, site, drive, size ((w, h) or None)."""
    photos = []
    if key in ("m20", "heli"):
        for item in doc.get("images") or []:
            files = item.get("image_files") or {}
            ext = item.get("extended") or {}
            size = _dimension(ext.get("dimension"))
            if size and min(size) < 200:
                continue   # 128-pixel engineering subframes: not worth looking at
            code = (item.get("camera") or {}).get("instrument")
            mars = item.get("date_taken_mars") or ""
            sol = _int(item.get("sol"))
            taken = _when(item.get("date_taken_utc"))
            if key == "heli":
                # Ingenuity's feed gives the date the photo reached Earth, often
                # weeks later; the Mars time stamped on it says when it was taken
                taken = _mars_time_utc("m20", sol, mars.split("M")[-1]) or taken
            photos.append({
                "id": item.get("imageid") or "", "sol": sol,
                "taken": taken,
                "local": mars.split("M")[-1][:5] if "M" in mars else "",
                "camera": camera_name(code), "code": code or "",
                "thumb": files.get("small") or files.get("medium"),
                "view": files.get("large") or files.get("medium") or files.get("full_res"),
                "full": files.get("full_res"), "link": item.get("link") or "",
                "credit": item.get("credit") or "NASA/JPL-Caltech",
                "site": _int(item.get("site")), "drive": _int(item.get("drive")),
                "size": size,
            })
        return photos, False
    for item in doc.get("items") or []:
        if item.get("is_thumbnail"):
            continue   # each photo also comes as a tiny thumbnail copy
        url = item.get("https_url") or item.get("url")
        if not url:
            continue
        link = item.get("link") or ""
        photos.append({
            "id": item.get("imageid") or "", "sol": _int(item.get("sol")),
            "taken": _when(item.get("date_taken")), "local": "",
            "camera": camera_name(item.get("instrument")), "code": item.get("instrument") or "",
            "thumb": url, "view": url, "full": None,
            "link": ("https://mars.nasa.gov" + link) if link.startswith("/") else link,
            "credit": item.get("image_credit") or "NASA/JPL-Caltech",
            "site": _int(item.get("site")), "drive": _int(item.get("drive")), "size": None,
        })
    return photos, bool(doc.get("more"))
