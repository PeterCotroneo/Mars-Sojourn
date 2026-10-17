# Mars Sojourn

NASA's Mars rovers on a Mars map, in QGIS.

Mars Sojourn shows where Perseverance and Curiosity are now, every stop they have made and the routes they have driven since landing, over NASA orbital imagery that sharpens to close-up HiRISE colour around each route. The Ingenuity helicopter's 72 flights are included as a record.

![Perseverance's route over HiRISE imagery in Jezero crater, with the rover's latest photos in the Mars Sojourn panel](docs/img/01-perseverance-photos-2026-10.jpg)

## What you get

A "Mars Rovers" group in the Layers panel:

- **Perseverance** (Jezero crater): latest position, stops and route
- **Curiosity** (Gale crater): latest position, stops and route
- **Ingenuity** (Perseverance's helicopter): final resting place, landing spots, flights (2021 to 2024) and its own photos
- **Mars imagery**: a global colour mosaic, a 6 m mosaic of Gale crater and close-up HiRISE colour along both rover routes

Click a rover, stop or route with QGIS's Identify tool for the sol (Mars day since landing), distance driven, elevation and tilt.

![Curiosity's route climbing through Gale crater's colour imagery](docs/img/02-curiosity-gale-2026-10.jpg)

![Ingenuity's flights over Jezero, with the helicopter's own photo of its shadow after flight 66](docs/img/03-ingenuity-flights-2026-10.jpg)

## Photos

The bottom of the Mars Sojourn panel is a photo viewer with the rovers' raw photos, straight from NASA's raw-image feeds, usually within hours of reaching Earth. It opens on Perseverance's newest photos; the Photos button next to each rover switches to that rover.

- a strip of every photo from a sol, newest first, with a camera filter (Navcam, Mastcam-Z, Hazcams, WATSON, SuperCam and so on)
- earlier and later sols with the arrows, or type any sol
- click a thumbnail to see it in the preview: zoom with the mouse wheel or + and -, drag to pan, double-click to fit, arrow keys for the previous and next photo, ⤢ to see the photo large over the whole QGIS window (Esc closes)
- each photo says where it was taken (rovers stamp their photos with counters that match the stops on the map), and the map follows it with a red ring
- Pick a stop, then click any stop on the map, to see the photos from that sol

![A Perseverance Navcam photo from sol 1947, shown large over the QGIS window, timed in UTC and LMST](docs/img/04-full-screen-photo-2026-10.jpg)

Some raw frames are naturally dark or odd-looking: night-time and calibration shots, and partial frames with black around them. That is how NASA posts them.

## It is not live tracking

A rover's position changes only when it drives and NASA publishes the new spot. That is usually every few days, and sometimes not for weeks while a rover works in one place. Mars Sojourn draws each rover exactly where NASA last located it, turned to the heading NASA reports, and never estimates or animates movement.

Every hour Mars Sojourn also reads the newest photo NASA has posted from each rover. A rover's photos are stamped with counters that change only when it drives, so if the rover has driven and NASA has not mapped the new spot yet, the panel says so in amber.

## The map is on Mars

QGIS cannot mix Earth and Mars coordinates, so Mars Sojourn puts the map in a proper Mars projection (Mars Mercator, IAU 2015) and measures on the Mars sphere. The scale bar and the Measure tool give real Martian distances.

If your project is empty, Mars Sojourn puts the map on Mars for you. If you have an Earth map open, it adds nothing to it (QGIS can't show Mars on an Earth map) and the panel offers **Open a Mars map**, which starts a new project on Mars (QGIS asks first whether to save yours). Open your Earth project again afterwards as usual; the rovers you ticked are remembered. (To put an existing project on Mars instead, set its projection to a Mars one in Project Properties; the rovers then appear in it, and its Earth layers stop drawing.)

## Install

In QGIS: Plugins, Manage and Install Plugins, tick "Show also experimental plugins" in Settings, then search for Mars Sojourn. Works in QGIS 3.28 and later, including QGIS 4.

## Data sources

- Rover and helicopter positions, stops and routes: the GeoJSON files behind NASA's public rover location maps ([Perseverance](https://science.nasa.gov/mission/mars-2020-perseverance/location-map/), [Curiosity](https://science.nasa.gov/mission/msl-curiosity/location-map/)). These are not a documented service and could change without notice, which is why the plugin is marked experimental.
- Global mosaic: Viking colour mosaic (MDIM 2.1) from NASA Mars Trek. Credit NASA/JPL-Caltech/USGS.
- Gale crater mosaic: CTX. Credit NASA/JPL-Caltech/MSSS.
- Close-up imagery: HiRISE (public domain). Credit NASA/JPL/University of Arizona.
- Jezero region mosaic (black and white, about 10 m): CTX, from NASA Mars Trek. Credit NASA/JPL/MSSS.

No account or key is needed. Nothing is bundled with the plugin; everything is fetched from NASA as you use it. All of it is NASA data, used under NASA's images and media guidelines: not subject to copyright, NASA credited as the source, no NASA logos, and no suggestion of NASA endorsement.

Data courtesy NASA/JPL-Caltech. This plugin is not affiliated with or endorsed by NASA, JPL or Caltech.

## Notes

- Earth dates for stops are worked out from the sol number, at 13:00 LMST, in UTC.
- Photo times are shown in UTC and, for Perseverance and Ingenuity, in LMST: the rover's local mean solar time, NASA's own stamp. Mars has no time zones; each place's clock follows its longitude. Curiosity's feed gives UTC only.
- A few Perseverance stops are labelled "Interpolated (not measured)". That is NASA's own flag for points filled in between two measured ones.
- NASA's Curiosity route file carries no sol numbers, so the route there is not dated; the stops are.

## Licence

GPL-2.0-or-later. See [LICENSE](LICENSE).
