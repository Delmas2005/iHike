from __future__ import annotations

import re
from typing import Any

import requests

from .config import (
    ACTIVITY_SPEED,
    BROUTER_URL,
    LEVELS,
    ORS_API_KEY,
    ORS_URL,
    SAC_TAGS,
    USER_AGENT,
    VARIANT_COLORS,
    require_ors,
)
from .errors import PlannerError
from .geo import Point, build_track, estimate_hours, haversine_km


def _ors_error(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            return err.get("message") or str(err)
        if err:
            return str(err)
    except Exception:  # noqa: S110 - an unreadable error body falls back to the status code
        pass
    return f"HTTP {resp.status_code}"


def ors_route(coordinates: list, round_trip: dict | None = None, alternatives: int = 0) -> list[dict]:
    """One call to ORS hiking directions. Returns GeoJSON features (one per route)."""
    require_ors()
    body = {"coordinates": coordinates, "elevation": True, "instructions": False, "extra_info": ["traildifficulty"]}
    if round_trip:
        body["options"] = {"round_trip": round_trip}
    if alternatives > 1:
        body["alternative_routes"] = {"target_count": alternatives, "weight_factor": 1.6, "share_factor": 0.6}

    def send() -> requests.Response:
        return requests.post(
            f"{ORS_URL}/v2/directions/foot-hiking/geojson",
            json=body,
            headers={"Authorization": ORS_API_KEY},
            timeout=45,
        )

    resp = send()
    if resp.status_code != 200 and "within a radius" in _ors_error(resp):
        # By default ORS only looks 350 m around a point for a path. Summits and lakes are
        # often further than that from the nearest mapped trail: retry with the widest search.
        body["radiuses"] = [-1] * len(coordinates)
        resp = send()
    if resp.status_code != 200:
        if "within a radius" in _ors_error(resp):
            raise PlannerError(
                "No hiking path was found near this point. Pick a start closer to a trail, "
                "for example the car park or village where the hike begins."
            )
        raise PlannerError(f"Routing failed: {_ors_error(resp)}")
    return resp.json().get("features") or []


def _ors_sac(props: dict) -> int:
    """Hardest SAC grade met for at least 50 m, from the ORS 'traildifficulty' extra."""
    summary = ((props.get("extras") or {}).get("traildifficulty") or {}).get("summary") or []
    grades = [int(x.get("value", 0)) for x in summary if x.get("distance", 0) >= 50]
    return max((g for g in grades if 1 <= g <= 6), default=0)


def brouter_route(start: Point, end: Point) -> tuple[list[list[float]] | None, int, str | None]:
    """Route with BRouter's alpine hiking profile.

    Returns (coords, sac, None) on success, or (None, 0, reason) so the reason can be shown.
    """
    try:
        params: dict[str, Any] = {
            "lonlats": f"{start[1]:.6f},{start[0]:.6f}|{end[1]:.6f},{end[0]:.6f}",
            "profile": "hiking-mountain",
            "alternativeidx": 0,
            "format": "geojson",
        }
        resp = requests.get(
            BROUTER_URL,
            timeout=(5, 20),
            params=params,
            headers=USER_AGENT,
        )
        if resp.status_code != 200:
            detail = " ".join((resp.text or "").split())[:120] if hasattr(resp, "text") else ""
            return None, 0, f"HTTP {resp.status_code} {detail}".strip()
        try:
            feature = resp.json()["features"][0]
        except Exception:
            # BRouter answers in plain text when it cannot route (point too far from any path, ...)
            return None, 0, " ".join((getattr(resp, "text", "") or "unreadable answer").split())[:120]
        coords = feature["geometry"]["coordinates"]
        rows = (feature.get("properties") or {}).get("messages") or []
        sac = 0
        if rows and "WayTags" in rows[0]:
            col = rows[0].index("WayTags")
            for row in rows[1:]:
                tag = re.search(r"sac_scale=(\w+)", row[col])
                sac = max(sac, SAC_TAGS.get(tag.group(1), 0) if tag else 0)
        if len(coords) < 2:
            return None, 0, "empty route"
        return coords, sac, None
    except Exception as exc:
        print("BRouter error:", exc)
        return None, 0, f"no answer ({type(exc).__name__})"


def _from_ors(feature: dict) -> tuple:
    props = feature.get("properties", {})
    return (
        feature["geometry"]["coordinates"],
        props.get("summary", {}).get("distance"),
        props.get("ascent"),
        props.get("descent"),
        _ors_sac(props),
        "ors",
    )


def _make_track(route: tuple, level: str, activity: str, end: Point | None = None, out_and_back: bool = False) -> dict:
    """route = (coords, distance_m, ascent, descent, sac, router) -> a track with its statistics."""
    coords, distance_m, ascent, descent, sac, router = route
    far = (coords[-1][1], coords[-1][0])
    if end is not None and out_and_back:
        coords = coords + coords[-2::-1]  # walk it back the same way
        distance_m = distance_m * 2 if distance_m else None
        if ascent is not None and descent is not None:
            ascent = descent = ascent + descent  # every climb is descended, every descent climbed
    track = build_track(coords, distance_m=distance_m, ascent=ascent, descent=descent, sac=sac)
    track["hours"] = round(
        estimate_hours(track["distance_km"], track["ascent_m"], track["descent_m"], level, activity), 1
    )
    track["router"] = router
    if end is not None:
        track["far"] = far
        track["far_label"] = "Turnaround point" if out_and_back else "End"
        track["end_gap_km"] = haversine_km(end, far)
    return track


def plan_variants(
    start: Point, end: Point | None, hours: float, level: str, activity: str, out_and_back: bool = False
) -> list[dict]:
    """start/end are (lat, lon); end=None means a loop. Returns tracks sorted easiest first."""
    routes = []  # (coords, distance_m, ascent, descent, sac, router)
    from_ors = _from_ors

    if end is None:
        # Aim a little under the wished duration: the flat-speed target ignores climbing.
        base_km = hours * LEVELS[level]["flat_kmh"] * ACTIVITY_SPEED[activity] * 0.75
        last_error = None
        for seed, factor in enumerate((0.6, 1.0, 1.35), start=1):
            length_m = int(min(max(base_km * factor, 2.0), 80.0) * 1000)
            try:
                routes += [
                    from_ors(f)
                    for f in ors_route(
                        [[start[1], start[0]]],
                        round_trip={"length": length_m, "points": 3, "seed": seed},
                    )[:1]
                ]
            except PlannerError as exc:
                last_error = exc
        if not routes:
            raise last_error or PlannerError("No loop could be generated from this point.")
    else:
        pair = [[start[1], start[0]], [end[1], end[0]]]
        ors_error = None
        try:
            try:
                routes = [from_ors(f) for f in ors_route(pair, alternatives=3)]
            except PlannerError:
                routes = [from_ors(f) for f in ors_route(pair)]  # alternatives are refused on long routes
        except PlannerError as exc:
            ors_error = exc

        def gap(coords):  # how far from the wished destination the route really ends
            return haversine_km(end, (coords[-1][1], coords[-1][0]))

        best_gap = min((gap(r[0]) for r in routes), default=float("inf"))
        alpine_note = ""
        if best_gap > 0.3:
            # ORS refuses paths graded T5/T6, so it stops where the difficult section begins.
            # Ask a router that accepts them, and keep its answer if it gets clearly closer.
            # Start from where ORS actually joined the path: BRouter refuses points far from any way.
            origin = (routes[0][0][0][1], routes[0][0][0][0]) if routes else start
            stop = min((r[0][-1] for r in routes), key=lambda c: haversine_km(end, (c[1], c[0])), default=None)
            targets = [end]
            if stop:  # if the exact point is refused, aim a third, then two thirds, of the way back
                targets += [(end[0] + (stop[1] - end[0]) * f, end[1] + (stop[0] - end[1]) * f) for f in (1 / 3, 2 / 3)]
            reasons = []
            for target in targets:
                coords, sac, reason = brouter_route(origin, target)
                if coords and gap(coords) < best_gap - 0.3:
                    routes, alpine_note = [(coords, None, None, None, sac, "brouter")], ""
                    break
                reasons.append(reason or f"it also stops {gap(coords):.1f} km short")
                alpine_note = reasons[0]
                if reason and reason.startswith(("no answer", "HTTP 5")):
                    break  # the server is down or overloaded: retrying would only make the hiker wait
        if not routes:
            raise ors_error or PlannerError("No route found between these two points.")

    tracks = [_make_track(r, level, activity, end, out_and_back) for r in routes]
    if end is not None and alpine_note:
        for t in tracks:
            t["alpine_note"] = alpine_note
    kind = "Computed loop" if end is None else ("Out and back" if out_and_back else "One way")
    tracks.sort(key=lambda t: t["effort"])
    labels = {1: ["Route"], 2: ["Easier", "Harder"], 3: ["Easiest", "Balanced", "Most demanding"]}[len(tracks)]
    for i, t in enumerate(tracks):
        t["name"] = f"{'ABC'[i]} · {labels[i]}"
        t["color"] = VARIANT_COLORS[i]
        t["kind"] = kind
    return tracks
