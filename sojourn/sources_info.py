"""Text for the panel's "ⓘ" buttons."""

from .mars import IMAGERY
from .missions import MISSIONS

_CREDIT = (
    '<p style="color:gray">Data courtesy NASA/JPL-Caltech. This plugin is not '
    "affiliated with or endorsed by NASA, JPL or Caltech.</p>"
)


def _how(text):
    return f"<b>How positions are known</b><br/>{text}<br/><br/>"


def rover_html(key):
    m = MISSIONS[key]
    name = m["name"]
    return name, (
        _how(
            f"After each drive, NASA's team works out where {name} ended up by matching "
            "the rover's own camera views to orbital imagery, and publishes the result "
            f'for its public <a href="{m["map"]}">Where is {name}?</a> map. This plugin '
            "reads the same files."
        )
        + f"{name} landed in {m['place']} on {m['landed']}. The rover marker is drawn "
        "exactly where NASA last located it and turned to the heading NASA reports. A "
        "rover only moves when it drives, and it often stays in one place for days or "
        "weeks while it studies or drills a rock, so the date of the last drive can be "
        "well in the past while the position is still correct."
        "<br/><br/>"
        "<b>Has it driven since?</b><br/>"
        "A rover's photos are stamped with two counters (site and drive) that change "
        "only when it drives. Each hour the plugin compares the newest photo NASA has "
        "posted with the mapped position. If the rover has driven and NASA has not "
        "mapped the new spot yet, the panel says so in red; the map updates as soon "
        "as NASA publishes it."
        "<br/><br/>"
        "<b>On the map</b><br/>"
        "<b>Stops</b> are the places NASA located the rover: mostly the end of each "
        "drive, with some mid-drive points. On Perseverance a few are marked "
        "<i>Interpolated (not measured)</i>, which is NASA's own label for points "
        "filled in between two measured ones. The <b>route</b> is the path driven "
        "between stops. Click anything with QGIS's Identify tool for the sol (Mars day since landing), "
        "distance driven and elevation. Earth dates are worked out from the sol "
        "number, at 13:00 Mars time, in UTC. (QGIS shows Identify results for the "
        "selected layer only, unless the Identify panel's Mode is set to Top down.)"
        "<br/><br/>"
        "<b>Photos</b><br/>"
        "The photos at the bottom of this panel are the rover's raw photos, newest sol "
        "first, straight from NASA's raw-image feed: every camera, usually within hours "
        "of reaching Earth. Step through sols, filter by camera, and click a thumbnail "
        "to see it in the preview (zoom with the mouse wheel, drag to pan, arrow keys "
        "for the next photo, ⤢ to see it large over the whole QGIS window). Each photo says where it was "
        "taken, and <b>Pick a stop</b> shows the photos from any stop you click on the map."
        "<br/><br/>" + _CREDIT
    )


def heli_html():
    m = MISSIONS["heli"]
    return "Ingenuity", (
        _how(
            "Ingenuity's landing spots and flight paths were worked out by NASA's team "
            'after each flight and published on the <a href="' + m["map"] + '">Where is '
            "Perseverance?</a> map. This plugin reads the same files."
        )
        + "Ingenuity was the helicopter carried to Mars by Perseverance. It made 72 "
        "flights between April 2021 and January 2024, when rotor damage on landing "
        "ended the mission. It has not moved since, so this layer is a record and "
        "will not change. The star marks where it rests; the dots are the landing "
        "spots and the lines are the flights. Click a landing spot with the Identify tool for "
        "the flight number, date, distance, duration and height."
        "<br/><br/>"
        "<b>Photos</b> shows Ingenuity's own photos, from its navigation and colour "
        "cameras, from NASA's Ingenuity raw-image feed. ◀ ▶ step from flight to "
        "flight. Some flights' photos reached Earth only on a later sol, so a flight "
        "sol can have none."
        "<br/><br/>" + _CREDIT
    )


def imagery_html():
    rows = "".join(
        f"<li>{spec['title']} <span style='color:gray'>({spec['credit']})</span></li>"
        for spec in reversed(IMAGERY))
    return "Mars imagery", (
        "Orbital imagery of Mars, loaded as map tiles as you pan and zoom:"
        f"<ul>{rows}</ul>"
        "Each rover's colour close-up sits in that rover's group in the Layers panel "
        "and covers the area around its route; it appears as you zoom in, and only "
        "while the rover is ticked. Around it, a black and white mosaic from the Context "
        "Camera (about 6 to 10 m per pixel) covers the wider Jezero and Gale regions. "
        "Everywhere else on Mars only the global colour mosaic (232 m per pixel) is "
        "available, so it looks soft when zoomed in."
        "<br/><br/>"
        "<b>The map is on Mars</b><br/>"
        "QGIS cannot mix Earth and Mars coordinates, so the plugin puts the map in a "
        "Mars projection (Mars Mercator, IAU 2015) and measures on the Mars sphere, "
        "which means the scale bar and the Measure tool give real Martian distances. "
        "Earth layers cannot be drawn on the same map; use a separate project for those."
        "<br/><br/>"
        '<p style="color:gray">Global colour mosaic from NASA Mars Trek; rover-site '
        "imagery from NASA/JPL's rover location maps; Jezero's CTX mosaic from NASA "
        "Mars Trek. All are NASA data, used under NASA's media guidelines (not "
        "copyrighted; credit NASA). HiRISE images are public domain, credit "
        "NASA/JPL/University of Arizona. This plugin is not affiliated with or "
        "endorsed by NASA, JPL or Caltech.</p>"
    )
