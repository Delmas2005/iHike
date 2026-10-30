from __future__ import annotations

import contextlib
import html
import os
from pathlib import Path

from defusedxml import ElementTree as SafeET

from .errors import PlannerError

MAX_GPX_BYTES = 5 * 1024 * 1024


def write_gpx(track: dict, folder: str | Path) -> str:
    eles = track["eles"] or [None] * len(track["latlon"])
    points = "".join(
        f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}">' + (f"<ele>{ele:.1f}</ele>" if ele is not None else "") + "</trkpt>"
        for (lat, lon), ele in zip(track["latlon"], eles)
    )
    name = html.escape(f"iHike - {track['name']}")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="iHike" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>{name}</name><trkseg>{points}</trkseg></trk></gpx>"
    )
    path = Path(folder) / f"variant_{track['name'][0]}.gpx"
    path.write_text(xml, encoding="utf-8")
    return str(path)


def parse_gpx(path: str | Path) -> list[list[float | None]]:
    if os.path.getsize(path) > MAX_GPX_BYTES:
        raise PlannerError("This GPX file is larger than 5 MB.")
    try:
        root = SafeET.parse(path).getroot()
    except Exception:
        raise PlannerError("This file is not a readable GPX file.") from None
    coords = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] not in ("trkpt", "rtept"):
            continue
        try:
            lat, lon = float(el.get("lat")), float(el.get("lon"))
        except (TypeError, ValueError):
            continue
        ele = None
        for child in el:
            if child.tag.rsplit("}", 1)[-1] == "ele":
                with contextlib.suppress(TypeError, ValueError):
                    ele = float(child.text)
        coords.append([lon, lat, ele])
    if len(coords) < 2:
        raise PlannerError("No track points found in this GPX file.")
    return coords
