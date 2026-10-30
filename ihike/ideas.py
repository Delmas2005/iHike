from __future__ import annotations

from .advice import suits
from .config import ACTIVITY_SPEED, EFFORT_LIMITS, LEVELS, VARIANT_COLORS
from .errors import PlannerError
from .geo import estimate_hours, haversine_km, path_km, stitch_ways
from .osm import find_goals_and_trails, find_trailheads
from .routing import _from_ors, _make_track, ors_route
from .weather import get_elevations


def pick_goals(goals, start, start_ele, hours, level, activity, limit=5, kinds=None) -> list[dict]:
    """Keep the goals whose out-and-back roughly fills the wished time, with some variety.

    `kinds` (e.g. {"lake", "hut"}) narrows the choice when the hiker asked for something specific.
    """
    if kinds:
        wanted = [g for g in goals if g["kind"] in kinds]
        goals = wanted or goals
    scored = []
    for g in goals:
        path_km = haversine_km(start, (g["lat"], g["lon"])) * 1.35  # trails wind: add a third to the straight line
        if path_km < 0.5:
            continue
        climb_m = abs(g["ele"] - start_ele) if g["ele"] is not None and start_ele is not None else 0
        est = estimate_hours(2 * path_km, climb_m, climb_m, level, activity)
        if est > hours * 1.25 or 2 * path_km + climb_m / 100 > EFFORT_LIMITS[level]:
            continue
        scored.append((abs(est - hours * 0.9), g))
    scored.sort(key=lambda x: x[0])

    chosen = []
    for _, g in scored:
        same_kind = sum(1 for c in chosen if c["kind"] == g["kind"])
        too_close = any(haversine_km((c["lat"], c["lon"]), (g["lat"], g["lon"])) < 0.5 for c in chosen)
        if same_kind < 2 and not too_close:
            chosen.append(g)
        if len(chosen) == limit:
            break
    return chosen


def marked_loops(trails, start, hours, level, activity, limit=2) -> list[dict]:
    """Waymarked circular trails that pass near the start and fit in the wished time."""
    loops = []
    for trail in trails:
        chain, used = stitch_ways(trail["ways"])
        if len(chain) < 4 or used < 0.8 or haversine_km(chain[0], chain[-1]) > 0.15:
            continue  # not a clean closed loop: do not guess
        length = path_km(chain)
        if length < 1.5 or length / (LEVELS[level]["flat_kmh"] * ACTIVITY_SPEED[activity]) > hours * 1.3:
            continue
        nearest = min(range(len(chain)), key=lambda i: haversine_km(start, chain[i]))
        approach = haversine_km(start, chain[nearest])
        if approach > 1.5:
            continue
        chain = chain[nearest:] + chain[1 : nearest + 1]  # same loop, starting at the point closest to the user
        step = max(1, len(chain) // 10)
        waypoints = chain[::step][:10] + [chain[0]]
        if approach > 0.15:
            waypoints = [start] + waypoints + [start]
        loops.append(
            {
                "name": trail["name"],
                "waypoints": waypoints,
                "length_km": length + 2 * approach,
                "score": abs(length / LEVELS[level]["flat_kmh"] - hours),
            }
        )
    return sorted(loops, key=lambda l: l["score"])[:limit]


KIND_PLURAL = {
    "summit": "Summits",
    "lake": "Lakes",
    "hut": "Huts",
    "viewpoint": "Viewpoints",
    "waterfall": "Waterfalls",
    "pass": "Passes",
}


def out_of_reach_hint(goals, chosen, start, kinds, hours, level) -> str:
    """When the hiker asked for lakes (say) and none fits, name the closest ones instead of staying silent."""
    wanted = [k for k in (kinds or []) if k in KIND_PLURAL]
    if not wanted or any(g["kind"] in wanted for g in chosen):
        return ""
    near = sorted((g for g in goals if g["kind"] in wanted), key=lambda g: haversine_km(start, (g["lat"], g["lon"])))[
        :3
    ]
    if not near:
        return f"No {KIND_PLURAL[wanted[0]].lower()} were found on the map within reach of this start."
    names = ", ".join(f"{g['name']} ({haversine_km(start, (g['lat'], g['lon'])):.1f} km away)" for g in near)
    return (
        f"{KIND_PLURAL[wanted[0]]} nearby are too far for {hours:g} h at {level.lower()} level: {names}. "
        "Ask for one by name to plan a there-and-back to it anyway."
    )


def suggest_hikes(start, hours, level, activity, kinds=None, hints=None):
    """Real hike ideas from `start`. Returns (tracks, trails) - tracks is [] when nothing was found,
    and both are None when OpenStreetMap could not be queried."""
    reach_km = hours * LEVELS[level]["flat_kmh"] * ACTIVITY_SPEED[activity] / 2
    goals, trails = find_goals_and_trails(start[0], start[1], min(max(reach_km * 0.8, 1.5), 12.0) * 1000)
    if goals is None:
        return None, None

    unknown = [g for g in goals if g["ele"] is None]
    heights = get_elevations([start] + [(g["lat"], g["lon"]) for g in unknown])
    for g, h in zip(unknown, heights[1:]):
        g["ele"] = h

    tracks = []
    chosen = pick_goals(goals, start, heights[0], hours, level, activity, kinds=kinds)
    if hints is not None:
        hint = out_of_reach_hint(goals, chosen, start, kinds, hours, level)
        if hint:
            hints.append(hint)
    for g in chosen:
        target = (g["lat"], g["lon"])
        try:
            features = ors_route([[start[1], start[0]], [g["lon"], g["lat"]]])
        except PlannerError:
            continue
        if not features:
            continue
        track = _make_track(_from_ors(features[0]), level, activity, target, out_and_back=True)
        # A lake is marked at its centre, so the shore path is allowed to end further from the point.
        if track["end_gap_km"] > (0.8 if g["kind"] == "lake" else 0.3):
            continue  # no accepted path reaches it: not something to suggest
        track.update(
            title=f"{g['name']} ({g['kind']})",
            kind=f"Out and back to a {g['kind']}",
            far_label=g["name"],
            place=g["name"],
        )
        tracks.append(track)

    for loop in [] if kinds and "loop" not in kinds else marked_loops(trails, start, hours, level, activity):
        try:
            features = ors_route([[p[1], p[0]] for p in loop["waypoints"]])
        except PlannerError:
            continue
        if not features:
            continue
        track = _make_track(_from_ors(features[0]), level, activity)
        if not 0.7 <= track["distance_km"] / loop["length_km"] <= 1.5:
            continue  # the router did not follow the marked trail
        track.update(title=f"{loop['name']} (marked loop)", kind="Waymarked loop", place=loop["name"])
        tracks.append(track)

    tracks.sort(key=lambda t: (not suits(t, level), abs(t["hours"] - hours)))
    tracks = tracks[: len(VARIANT_COLORS)]
    for i, t in enumerate(tracks):
        t["name"] = f"{'ABCD'[i]} · {t['title']}"
        t["color"] = VARIANT_COLORS[i]
    return tracks, trails


def plan_from_car_parks(end, end_label, hours, level, activity, out_and_back=True) -> list[dict]:
    """Routes to `end` from the car parks and trailheads around it: the easiest, a middle one, the hardest."""
    places = find_trailheads(end[0], end[1], radius_m=9000, limit=24, order="distance")
    if places is None:
        raise PlannerError("OpenStreetMap could not be searched for car parks just now. Enter a start, or try again.")
    places = [p for p in places if p["away_km"] >= 0.3]  # a car park next to the goal is not a hike
    if not places:
        raise PlannerError("No car park or trailhead was found on the map near this destination. Enter a start.")

    # Route from the nearest few and from a spread of further ones: at most 6 routing requests.
    picks = places[:3]
    rest = places[3:]
    for f in (0.35, 0.65, 1.0):
        if rest:
            p = rest[min(len(rest) - 1, int(f * (len(rest) - 1)))]
            if p not in picks:
                picks.append(p)
    where = end_label.split(",")[0] or "the destination"
    tracks = []
    for p in picks:
        try:
            features = ors_route([[p["lon"], p["lat"]], [end[1], end[0]]])
        except PlannerError:
            continue
        if not features:
            continue
        track = _make_track(_from_ors(features[0]), level, activity, end, out_and_back)
        name = (
            p["name"]
            if p["named"]
            else (
                f"a {'trailhead' if p['kind'] == 'trailhead' else 'car park'}"
                + (f" at {p['ele']} m" if p.get("ele") else "")
            )
        )
        track.update(
            place=f"From {name}", start_point={"lat": p["lat"], "lon": p["lon"], "label": name}, far_label=where
        )
        tracks.append(track)
    if not tracks:
        raise PlannerError(f"No route to {where} could be found from the car parks around it. Enter a start.")

    tracks = [t for t in tracks if t["distance_km"] >= 1.0] or tracks
    reaching = [t for t in tracks if t["end_gap_km"] <= 0.5] or sorted(tracks, key=lambda t: t["end_gap_km"])[:1]
    reaching.sort(key=lambda t: t["effort"])
    distinct = []  # two car parks side by side give the same hike: keep one
    for t in reaching:
        if all(abs(t["effort"] - d["effort"]) > max(1.0, 0.08 * d["effort"]) for d in distinct):
            distinct.append(t)
    chosen = distinct if len(distinct) <= 3 else [distinct[0], distinct[len(distinct) // 2], distinct[-1]]
    labels = {1: ["Only way found"], 2: ["Easier", "Harder"], 3: ["Easiest", "Medium", "Hardest"]}[len(chosen)]
    trip = "there and back" if out_and_back else "one way"
    for i, t in enumerate(chosen):
        t["name"] = f"{'ABC'[i]} · {t['place']}"
        t["color"] = VARIANT_COLORS[i]
        t["kind"] = f"{labels[i]}, {trip} to {where}"
    return chosen
