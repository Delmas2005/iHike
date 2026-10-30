from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TypeVar, cast

from .config import ACTIVITY_SPEED, EUROPE, GRADES, LEVELS, SAC_LABELS

Point = Sequence[float]  # (lat, lon), or [lat, lon, ...] as read from JSON
T = TypeVar("T")


def haversine_km(a: Point, b: Point) -> float:
    """Distance between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def climb(eles: Sequence[float], threshold: float = 5.0) -> tuple[float, float]:
    """Total ascent/descent, ignoring wiggles under `threshold` metres (GPS noise)."""
    up = down = 0.0
    ref = eles[0]
    for e in eles[1:]:
        d = e - ref
        if d >= threshold:
            up, ref = up + d, e
        elif d <= -threshold:
            down, ref = down - d, e
    return up, down


def decimate(points: list[T], max_n: int) -> list[T]:
    if len(points) <= max_n:
        return list(points)
    step = (len(points) - 1) / (max_n - 1)
    return [points[round(i * step)] for i in range(max_n)]


def in_europe(lat: float, lon: float) -> bool:
    return EUROPE["min_lat"] <= lat <= EUROPE["max_lat"] and EUROPE["min_lon"] <= lon <= EUROPE["max_lon"]


def estimate_hours(dist_km: float, ascent_m: float, descent_m: float, level: str, activity: str) -> float:
    """DIN 33466-style estimate: the longer of horizontal/vertical time plus half the shorter."""
    pace, k = LEVELS[level], ACTIVITY_SPEED[activity]
    t_h = dist_km / (pace["flat_kmh"] * k)
    t_v = ascent_m / (pace["up_mh"] * k) + descent_m / (pace["down_mh"] * k)
    return max(t_h, t_v) + min(t_h, t_v) / 2


def grade_for(effort_km: float) -> str:
    """Effort-km = distance + 1 km per 100 m of ascent."""
    if effort_km < 8:
        return "Easy"
    if effort_km < 16:
        return "Moderate"
    if effort_km < 25:
        return "Hard"
    return "Very hard"


def build_track(
    coords: list,
    distance_m: float | None = None,
    ascent: float | None = None,
    descent: float | None = None,
    sac: int = 0,
) -> dict:
    """Turn [[lon, lat, ele?], ...] into a track with its statistics.

    `sac` is the hardest path grade met on the way (0 = unknown). The final grade is the
    physical effort, raised when the terrain is technical: a short T5 ridge is not "Easy".
    """
    latlon = [(c[1], c[0]) for c in coords]
    raw_eles = [c[2] if len(c) > 2 else None for c in coords]
    eles = cast("list[float]", raw_eles) if all(e is not None for e in raw_eles) else None

    km = [0.0]
    for a, b in zip(latlon, latlon[1:]):
        km.append(km[-1] + haversine_km(a, b))

    if eles and (ascent is None or descent is None):
        ascent, descent = climb(eles)
    ascent, descent = float(ascent or 0), float(descent or 0)
    dist_km = (distance_m / 1000) if distance_m else km[-1]
    effort = dist_km + ascent / 100
    floor = {3: "Moderate", 4: "Hard", 5: "Alpine", 6: "Alpine"}.get(sac, "Easy")
    grade = max(grade_for(effort), floor, key=GRADES.index)
    return {
        "latlon": latlon,
        "eles": eles,
        "km": km,
        "distance_km": round(dist_km, 1),
        "ascent_m": round(ascent),
        "descent_m": round(descent),
        "max_ele": round(max(eles)) if eles else None,
        "effort": round(effort, 1),
        "grade": grade,
        "sac": sac,
        "terrain": SAC_LABELS.get(sac, SAC_LABELS[0]),
        "loop": haversine_km(latlon[0], latlon[-1]) < 0.3,
    }


def path_km(points: Sequence[Point]) -> float:
    return sum(haversine_km(a, b) for a, b in zip(points, points[1:]))


def stitch_ways(ways: list[list[Point]]) -> tuple[list[Point], float]:
    """Join a trail's pieces end to end. Returns (ordered points, share of the total length used)."""
    pieces = [list(w) for w in ways if len(w) >= 2]
    if not pieces:
        return [], 0.0
    total = sum(path_km(w) for w in pieces) or 1.0

    def touch(a: Point, b: Point) -> bool:
        return haversine_km(a, b) < 0.02

    chain = pieces.pop(0)
    grew = True
    while pieces and grew:
        grew = False
        for i, w in enumerate(pieces):
            if touch(chain[-1], w[0]):
                chain = chain + w[1:]
            elif touch(chain[-1], w[-1]):
                chain = chain + w[-2::-1]
            elif touch(chain[0], w[-1]):
                chain = w[:-1] + chain
            elif touch(chain[0], w[0]):
                chain = w[:0:-1] + chain
            else:
                continue
            pieces.pop(i)
            grew = True
            break
    return chain, path_km(chain) / total
