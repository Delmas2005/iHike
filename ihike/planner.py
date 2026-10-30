from __future__ import annotations

from . import community
from .advice import ai_briefing, fallback_briefing, recommend, suits
from .alerts import alert_data, terrain_alerts, weather_alerts
from .community import nearby_hikes, public_hike
from .config import ACTIVITY_SPEED, HF_TOKEN, HIKES_DATASET, LEVELS, ORS_API_KEY, VARIANT_COLORS, require_ors
from .errors import PlannerError
from .geo import decimate, haversine_km, in_europe
from .ideas import plan_from_car_parks, suggest_hikes
from .places import geocode
from .routing import plan_variants
from .weather import get_weather

MODES = ("ideas", "out_and_back", "one_way")


def plan_hikes(
    start_text: str,
    mode: str = "ideas",
    end_text: str = "",
    activity: str = "Hiking",
    level: str = "Beginner",
    hours: float = 3.0,
    day_index: int = 0,
    kinds: set[str] | None = None,
) -> dict:
    """Plan from a start (and a destination unless mode == "ideas"). Raises PlannerError.

    Returns a dict with the tracks and everything needed to present them.
    """
    if mode not in MODES:
        raise PlannerError("Unknown trip type.")
    if level not in LEVELS or activity not in ACTIVITY_SPEED:
        raise PlannerError("Unknown level or activity.")
    hours = min(max(float(hours), 0.5), 12.0)
    day_index = min(max(int(day_index or 0), 0), 6)

    auto_start = mode != "ideas" and not (start_text or "").strip()
    if not auto_start:
        s_lat, s_lon, s_label, s_others = geocode(start_text)
        if not in_europe(s_lat, s_lon):
            raise PlannerError("The start point is outside Europe, which is the only area covered for now.")
    end: tuple[float, float] | None = None
    e_label = ""
    e_others: list[str] = []
    out_and_back = mode == "out_and_back"
    if mode != "ideas":
        e_lat, e_lon, e_label, e_others = geocode(end_text)
        if not in_europe(e_lat, e_lon):
            raise PlannerError("The destination is outside Europe.")
        end = (e_lat, e_lon)

    tracks: list[dict] | None = []
    trails: list[dict] | None = []
    hints: list[str] = []
    note = ""
    if auto_start:
        # No start given: find where people leave the car and offer an easy, a medium and a hard way.
        require_ors()
        assert end is not None  # auto_start implies a destination trip
        tracks = plan_from_car_parks(end, e_label, hours, level, activity, out_and_back)
        first = tracks[0]["start_point"]
        s_lat, s_lon, s_label, s_others = first["lat"], first["lon"], first["label"], []
    if mode == "ideas":
        require_ors()
        tracks, trails = suggest_hikes((s_lat, s_lon), hours, level, activity, kinds=kinds, hints=hints)
        if tracks is None:
            note = "OpenStreetMap could not be searched just now, so these are computed loops, not known hikes."
        elif not tracks:
            note = (
                "No named goal or waymarked loop within reach could be routed from here, "
                "so these are computed loops, not known hikes. Try a longer time or a start nearer the trails."
            )
    if not tracks:
        tracks = plan_variants((s_lat, s_lon), end, hours, level, activity, out_and_back)
    ideas = mode == "ideas" and not note  # real map-based ideas, not computed loops

    # Forecast at the highest point of the outing: that is where weather bites.
    w_lat, w_lon, w_where = s_lat, s_lon, "at the start"
    high = max((t for t in tracks if t["eles"]), key=lambda t: t["max_ele"], default=None)
    if high:
        w_lat, w_lon = high["latlon"][high["eles"].index(max(high["eles"]))]
        w_where = f"near the highest point ({high['max_ele']} m)"
    weather = get_weather(w_lat, w_lon, day_index)
    alerts = terrain_alerts(tracks, end) + weather_alerts(weather, max(t["hours"] for t in tracks))
    rec = recommend(tracks, level, hours)

    briefing = ai_briefing(
        {
            "hiker": {"level": level, "activity": activity, "wished_duration_h": hours},
            "start": s_label,
            "trip": ("ideas found on the map" if ideas else "computed loop")
            if end is None
            else ("out and back, figures are for the full return trip" if out_and_back else "one way"),
            "variants": [
                {
                    **{
                        k: t[k]
                        for k in (
                            "name",
                            "kind",
                            "distance_km",
                            "ascent_m",
                            "descent_m",
                            "max_ele",
                            "hours",
                            "grade",
                            "terrain",
                        )
                    },
                    "suits_this_hiker": suits(t, level),
                }
                for t in tracks
            ],
            "pre_selected_variant": rec["name"] if rec else None,
            "weather": weather,
            "alerts": alerts,
        }
    )
    ai_used = briefing is not None

    return {
        "start": {"lat": s_lat, "lon": s_lon, "label": s_label, "others": s_others},
        "end": {"lat": end[0], "lon": end[1], "label": e_label, "others": e_others} if end else None,
        "mode": mode,
        "out_and_back": out_and_back,
        "ideas": ideas,
        "note": note,
        "hint": " ".join(hints),
        "auto_start": auto_start,
        "level": level,
        "activity": activity,
        "hours": hours,
        "tracks": tracks,
        "recommended": rec,
        "trails": trails or [],
        "weather": weather,
        "weather_where": w_where,
        "alerts": alerts,
        "briefing": briefing or fallback_briefing(rec, level, alerts, tracks),
        "ai_briefing": ai_used,
        "start_offset_m": haversine_km((s_lat, s_lon), tracks[0]["latlon"][0]) * 1000,
        "nearby": nearby_hikes(s_lat, s_lon),
    }


def track_data(
    t: dict,
    level: str,
    rec: dict | None = None,
    max_points: int = 1500,
    title: str | None = None,
    kind: str | None = None,
) -> dict:
    eles = t["eles"] or [None] * len(t["latlon"])
    pts = decimate(list(zip(t["latlon"], eles)), max_points)
    name = t.get("name", "")
    return {
        "id": name[:1] or "A",
        "name": name,
        "title": title or t.get("place") or t.get("title") or name.split(" · ", 1)[-1],
        "kind": kind or t.get("kind", ""),
        "color": t.get("color", VARIANT_COLORS[0]),
        "distance_km": t["distance_km"],
        "ascent_m": t["ascent_m"],
        "descent_m": t["descent_m"],
        "max_ele": t["max_ele"],
        "hours": t["hours"],
        "grade": t["grade"],
        "sac": t["sac"],
        "terrain": t["terrain"],
        "loop": t["loop"],
        "router": t.get("router", "ors"),
        "recommended": t is rec,
        "suits": suits(t, level),
        "far": {"lat": t["far"][0], "lon": t["far"][1], "label": t.get("far_label", "")} if t.get("far") else None,
        "end_gap_km": round(t.get("end_gap_km", 0.0), 2),
        "start_point": t.get("start_point"),
        "geometry": [
            [round(lon, 6), round(lat, 6)] + ([round(e, 1)] if e is not None else []) for (lat, lon), e in pts
        ],
    }


_KIND_TEXT = {"Out and back": "There and back", "One way": "One way", "Computed loop": "Loop"}


def _names(t: dict, result: dict) -> dict:
    """Readable title and type for the web interface ('A · Easiest' is not a hike name)."""
    if t.get("place"):
        return {}
    variant = t.get("name", "").split(" · ", 1)[-1]
    many = len(result["tracks"]) > 1
    if result["end"]:
        where = result["end"]["label"].split(",")[0]
        kind = _KIND_TEXT.get(t.get("kind", ""), t.get("kind", ""))
        return {"title": where, "kind": f"{kind}, {variant.lower()} way" if many else kind}
    return {
        "title": {
            "Easiest": "Short loop",
            "Balanced": "Medium loop",
            "Most demanding": "Long loop",
            "Easier": "Short loop",
            "Harder": "Long loop",
        }.get(variant, "Loop"),
        "kind": "Computed loop, not a known hike",
    }


def plan_data(result: dict) -> dict:
    """plan_hikes() output -> JSON-ready dict."""
    rec = result["recommended"]
    named = list({t["name"]: t for t in result["trails"] if t["name"] != "Unnamed trail"}.values())[:6]
    return {
        "start": result["start"],
        "end": result["end"],
        "mode": result["mode"],
        "ideas": result["ideas"],
        "note": result["note"],
        "hint": result.get("hint", ""),
        "auto_start": result.get("auto_start", False),
        "out_and_back": result["out_and_back"],
        "level": result["level"],
        "hours": result["hours"],
        "hikes": [track_data(t, result["level"], rec, **_names(t, result)) for t in result["tracks"]],
        "no_match": rec is None,
        "weather": dict(result["weather"], where=result["weather_where"]) if result["weather"] else None,
        "alerts": [alert_data(a) for a in result["alerts"]],
        "briefing": {"text": result["briefing"].replace("**", ""), "ai": result["ai_briefing"]},
        "start_offset_m": round(result["start_offset_m"]),
        "trails": [
            {
                "id": t["id"],
                "name": t["name"],
                "network": t["network"],
                "url": f"https://hiking.waymarkedtrails.org/#route?id={t['id']}",
            }
            for t in named
        ],
        "nearby": [public_hike(h, with_path=False) | {"away_km": h["away_km"]} for h in result["nearby"]],
    }


def config_status() -> dict:
    return {
        "routing": bool(ORS_API_KEY),
        "ai_briefing": bool(HF_TOKEN),
        "persistence": community.scheduler is not None,
        "persistence_error": ""
        if community.scheduler
        else (
            "no HIKES_DATASET"
            if not HIKES_DATASET
            else "HIKES_DATASET needs HF_TOKEN"
            if not HF_TOKEN
            else f"could not open dataset {HIKES_DATASET} - {community.persistence_error}"
        ),
    }
