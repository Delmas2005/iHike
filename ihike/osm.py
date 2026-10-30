from __future__ import annotations

import re

import requests

from .config import NETWORKS, OVERPASS_URLS, USER_AGENT
from .geo import haversine_km
from .weather import get_elevations


def overpass(query: str):
    """Run an OpenStreetMap (Overpass) query. Returns the list of elements, or None if unreachable."""
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(url, data={"data": query}, headers=USER_AGENT, timeout=(5, 30))
            if resp.status_code == 200:
                return resp.json().get("elements") or []
            print("Overpass", url, "HTTP", resp.status_code)
        except Exception as exc:
            print("Overpass error:", url, exc)
    return None


def _goal_kind(tags: dict):
    if tags.get("natural") in ("peak", "volcano"):
        return "summit"
    if tags.get("natural") == "saddle" or tags.get("mountain_pass") == "yes":
        return "pass"
    if tags.get("tourism") in ("alpine_hut", "wilderness_hut"):
        return "hut"
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    if tags.get("waterway") == "waterfall":
        return "waterfall"
    if tags.get("natural") == "water" and tags.get("water") not in (
            "river", "stream", "canal", "ditch", "drain", "wastewater", "basin", "moat", "fountain"):
        return "lake"
    return None


def find_goals_and_trails(lat, lon, radius_m):
    """Named places worth walking to, and waymarked trails passing by the start.

    Returns (goals, trails), or (None, None) when OpenStreetMap could not be queried.
    """
    a = f"(around:{int(radius_m)},{lat:.5f},{lon:.5f})"
    near = f"(around:1500,{lat:.5f},{lon:.5f})"
    elements = overpass(
        "[out:json][timeout:30];("
        f'node{a}[natural~"^(peak|saddle|volcano)$"][name];'
        f'node{a}[mountain_pass=yes][name];'
        f'node{a}[tourism~"^(viewpoint|alpine_hut|wilderness_hut)$"][name];'
        f'way{a}[tourism~"^(alpine_hut|wilderness_hut)$"][name];'
        f'node{a}[waterway=waterfall][name];'
        f'way{a}[natural=water][name];'
        f'relation{a}[natural=water][name];'
        ");out center tags 250;"
        f'relation{near}[route~"^(hiking|foot)$"]->.r;.r out tags;'
        '(relation.r[network=lwn];relation.r[roundtrip=yes];);out geom 6;'
    )
    if elements is None:
        return None, None

    goals, trails, seen = [], {}, set()
    for el in elements:
        tags = el.get("tags") or {}
        if el.get("type") == "relation" and tags.get("route") in ("hiking", "foot"):
            trail = trails.setdefault(el["id"], {
                "id": el["id"], "name": tags.get("name") or tags.get("ref") or "Unnamed trail",
                "ref": tags.get("ref", ""), "network": NETWORKS.get(tags.get("network", ""), ""), "ways": []})
            for member in el.get("members") or []:
                if member.get("type") == "way" and member.get("role") in (None, "", "main", "forward", "backward"):
                    pts = [(g["lat"], g["lon"]) for g in member.get("geometry") or [] if g]
                    if len(pts) >= 2:
                        trail["ways"].append(pts)
            continue
        kind, name = _goal_kind(tags), (tags.get("name") or "").strip()
        point = el.get("center") or el
        if not kind or not name or "lat" not in point:
            continue
        key = (kind, name.lower())
        if key in seen:
            continue
        seen.add(key)
        ele = re.match(r"-?\d+(?:\.\d+)?", str(tags.get("ele", "")).replace(",", "."))
        goals.append({"name": name, "kind": kind, "lat": point["lat"], "lon": point["lon"],
                      "ele": float(ele.group()) if ele else None})
    return goals, list(trails.values())


_URBAN_PARKING = {"underground", "multi-storey", "rooftop", "garage_boxes", "carports"}
_CLOSED_ACCESS = {"private", "no", "customers", "delivery", "permit"}


def find_trailheads(lat: float, lon: float, radius_m: int = 5000, limit: int = 8, order: str = "best"):
    """Trailheads and public car parks around a point, best first. None if OpenStreetMap is unreachable."""
    a = f"(around:{int(radius_m)},{lat:.5f},{lon:.5f})"
    elements = overpass(
        "[out:json][timeout:25];("
        f"node{a}[highway=trailhead];way{a}[highway=trailhead];"
        f"node{a}[amenity=parking];way{a}[amenity=parking];"
        ");out center tags 300;"
    )
    if elements is None:
        return None
    found, seen = [], set()
    for el in elements:
        tags = el.get("tags") or {}
        point = el.get("center") or el
        if "lat" not in point:
            continue
        trailhead = tags.get("highway") == "trailhead"
        if not trailhead and tags.get("amenity") != "parking":
            continue
        if not trailhead:
            if tags.get("access") in _CLOSED_ACCESS or tags.get("parking") in _URBAN_PARKING:
                continue
        name = (tags.get("name") or "").strip()
        key = (round(point["lat"], 3), round(point["lon"], 3))
        if key in seen:
            continue
        seen.add(key)
        capacity = tags.get("capacity", "")
        found.append({
            "name": name or ("Trailhead" if trailhead else "Car park"), "named": bool(name),
            "kind": "trailhead" if trailhead else "parking",
            "lat": round(point["lat"], 6), "lon": round(point["lon"], 6),
            "away_km": round(haversine_km((lat, lon), (point["lat"], point["lon"])), 1),
            "fee": {"yes": True, "no": False}.get(tags.get("fee")),
            "capacity": int(capacity) if capacity.isdigit() else None,
        })
    # Named trailheads first, then named car parks, then the rest; nearest first within each group.
    if order == "distance":
        found.sort(key=lambda p: p["away_km"])
    else:
        found.sort(key=lambda p: (p["kind"] != "trailhead", not p["named"], p["away_km"]))
    found = found[:limit]
    for p, ele in zip(found, get_elevations([(p["lat"], p["lon"]) for p in found]) if found else []):
        p["ele"] = round(ele) if ele is not None else None
    return found
