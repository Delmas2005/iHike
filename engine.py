"""iHike engine - everything that is not interface: places, routes, ideas, weather, storage.

The web interface (app.py + static/) and the classic Gradio interface (classic.py)
both call plan_hikes() and the storage helpers below.

Architecture, in the order a request flows through it:
  1. geocode()         place name -> coordinates            (Photon, then OpenRouteService)
  2. suggest_hikes()   real goals and marked loops nearby   (OpenStreetMap via Overpass)
     plan_variants()   routes to a chosen destination       (OpenRouteService, BRouter for alpine paths)
  3. get_weather()     forecast + safety alerts             (Open-Meteo, no key)
  4. recommend()       rule-based pick for the user's level
  5. ai_briefing()     LLM explanation of the pick          (HF Inference Providers)
  6. community store   shared hikes, persisted to a HF Dataset

Each numbered step is a plain function on purpose: they can become the tools of an agent.
"""
from __future__ import annotations

import difflib
import html
import json
import math
import os
import re
import shutil
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from defusedxml import ElementTree as SafeET

# --------------------------------------------------------------------------
# Configuration - set these in the Space settings, never in the code
# --------------------------------------------------------------------------
ORS_API_KEY = os.environ.get("ORS_API_KEY", "").strip()        # secret
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()              # secret
HIKES_DATASET = os.environ.get("HIKES_DATASET", "").strip()    # variable, e.g. "user/monarch-hikes"
LLM_MODEL = os.environ.get("LLM_MODEL", "meta-llama/Llama-3.3-70B-Instruct").strip()

ORS_URL = "https://api.openrouteservice.org"
METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
BROUTER_URL = "https://brouter.de/brouter"       # second router: accepts alpine paths (T5/T6) that ORS refuses
PHOTON_URL = "https://photon.komoot.io/api/"
# OpenStreetMap query servers, tried in order. They list what is really on the ground.
# Public servers are often busy, and a Space shares its address with many others: try several.
OVERPASS_URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
                 "https://maps.mail.ru/osm/tools/overpass/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
USER_AGENT = {"User-Agent": "iHike/1.0 (Hugging Face Space)"}
EUROPE = {"min_lon": -25.0, "min_lat": 34.0, "max_lon": 45.0, "max_lat": 72.0}

# Pace assumptions per level: flat speed (km/h), climbing and descending rate (m/h)
LEVELS = {
    "Beginner": {"flat_kmh": 3.5, "up_mh": 300, "down_mh": 500},
    "Intermediate": {"flat_kmh": 4.5, "up_mh": 400, "down_mh": 600},
    "Experienced": {"flat_kmh": 5.5, "up_mh": 500, "down_mh": 800},
}
ACTIVITY_SPEED = {"Hiking": 1.0, "Trail running": 1.8}
TRIP_TYPES = ["Suggest hikes from here", "Out and back (A → B → A)", "One way (A → B)"]
EFFORT_LIMITS = {"Beginner": 16, "Intermediate": 25, "Experienced": 40}  # effort-km worth trying per level
NETWORKS = {"iwn": "international", "nwn": "national", "rwn": "regional", "lwn": "local"}

# Technical difficulty of the path itself: the SAC hiking scale, as tagged on OpenStreetMap.
SAC_TAGS = {"hiking": 1, "mountain_hiking": 2, "demanding_mountain_hiking": 3,
            "alpine_hiking": 4, "demanding_alpine_hiking": 5, "difficult_alpine_hiking": 6}
SAC_LABELS = {0: "not graded on the map", 1: "T1 hiking", 2: "T2 mountain hiking", 3: "T3 demanding mountain hiking",
              4: "T4 alpine hiking", 5: "T5 demanding alpine hiking", 6: "T6 difficult alpine hiking"}
GRADES = ["Easy", "Moderate", "Hard", "Very hard", "Alpine"]
# What we are willing to recommend to each level: (hardest path grade, hardest overall grade)
LEVEL_LIMITS = {"Beginner": (2, "Moderate"), "Intermediate": (3, "Hard"), "Experienced": (4, "Very hard")}
DAY_CHOICES = ["Today", "Tomorrow", "In 2 days", "In 3 days", "In 4 days", "In 5 days", "In 6 days"]
VARIANT_COLORS = ["#C2702D", "#2F6B4F", "#3E6A8A", "#7A4A6B"]  # ochre, forest, slate, plum
MAX_GPX_BYTES = 5 * 1024 * 1024

WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow",
    73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Light showers", 81: "Showers",
    82: "Violent showers", 85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Thunderstorm with heavy hail",
}
STORM_CODES = {95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
ICE_CODES = {48, 56, 57, 66, 67}


class PlannerError(Exception):
    """An error we can show to the user as-is."""


# --------------------------------------------------------------------------
# Community store: a JSONL file, synced to a HF Dataset when configured
# --------------------------------------------------------------------------
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
HIKES_FILE = DATA_DIR / "hikes.jsonl"
PHOTOS_DIR = DATA_DIR / "photos"          # published with a shared hike (synced to the dataset)
PHOTOS_DIR.mkdir(exist_ok=True)
PENDING_DIR = Path("pending_photos")      # uploaded but not yet shared (never synced)
PENDING_DIR.mkdir(exist_ok=True)
_local_lock = threading.Lock()
scheduler = None
persistence_error = ""

if HF_TOKEN and HIKES_DATASET:
    try:
        import tempfile

        from huggingface_hub import CommitScheduler, snapshot_download

        try:  # restore what previous runs of the Space saved (hikes and their photos)
            with tempfile.TemporaryDirectory() as tmp:
                snapshot_download(HIKES_DATASET, repo_type="dataset", token=HF_TOKEN, local_dir=tmp,
                                  allow_patterns=["hikes.jsonl", "photos/*"])
                if (Path(tmp) / "hikes.jsonl").exists():
                    shutil.copy(Path(tmp) / "hikes.jsonl", HIKES_FILE)
                for photo in (Path(tmp) / "photos").glob("*.jpg"):
                    shutil.copy(photo, PHOTOS_DIR / photo.name)
        except Exception as exc:
            print("Nothing restored:", exc)  # first run: the dataset does not exist yet
        scheduler = CommitScheduler(
            repo_id=HIKES_DATASET, repo_type="dataset", folder_path=DATA_DIR,
            every=5, token=HF_TOKEN, private=True,
        )
    except Exception as exc:  # the app must still start without persistence
        persistence_error = f"{type(exc).__name__}: {exc}".splitlines()[0][:200]
        print("Persistence disabled:", exc)


def load_hikes() -> list[dict]:
    if not HIKES_FILE.exists():
        return []
    hikes = []
    for line in HIKES_FILE.read_text(encoding="utf-8").splitlines():
        try:
            hikes.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return hikes


def save_hike(record: dict) -> None:
    lock = scheduler.lock if scheduler else _local_lock
    with lock:
        with HIKES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------
# Geometry and effort model
# --------------------------------------------------------------------------
def haversine_km(a, b) -> float:
    """Distance between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def climb(eles, threshold=5.0):
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


def decimate(points, max_n):
    if len(points) <= max_n:
        return list(points)
    step = (len(points) - 1) / (max_n - 1)
    return [points[round(i * step)] for i in range(max_n)]


def in_europe(lat, lon) -> bool:
    return EUROPE["min_lat"] <= lat <= EUROPE["max_lat"] and EUROPE["min_lon"] <= lon <= EUROPE["max_lon"]


def estimate_hours(dist_km, ascent_m, descent_m, level, activity) -> float:
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


def build_track(coords, distance_m=None, ascent=None, descent=None, sac=0) -> dict:
    """Turn [[lon, lat, ele?], ...] into a track with its statistics.

    `sac` is the hardest path grade met on the way (0 = unknown). The final grade is the
    physical effort, raised when the terrain is technical: a short T5 ridge is not "Easy".
    """
    latlon = [(c[1], c[0]) for c in coords]
    raw_eles = [c[2] if len(c) > 2 else None for c in coords]
    eles = raw_eles if all(e is not None for e in raw_eles) else None

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
        "latlon": latlon, "eles": eles, "km": km,
        "distance_km": round(dist_km, 1), "ascent_m": round(ascent), "descent_m": round(descent),
        "max_ele": round(max(eles)) if eles else None,
        "effort": round(effort, 1), "grade": grade, "sac": sac, "terrain": SAC_LABELS.get(sac, SAC_LABELS[0]),
        "loop": haversine_km(latlon[0], latlon[-1]) < 0.3,
    }


# --------------------------------------------------------------------------
# Step 1 - geocoding
# --------------------------------------------------------------------------
def require_ors():
    if not ORS_API_KEY:
        raise PlannerError("The routing key is missing: add ORS_API_KEY in the Space secrets.")


_NUMBER = r"[-+]?\d+(?:[.,]\d+)?"
_DMS = re.compile(r"""(\d{1,3})\s*°\s*(\d{1,2})\s*['′’]\s*(\d{1,2}(?:[.,]\d+)?)\s*(?:"|″|”|'')?\s*([NSEW])""")
_STOPWORDS = {"de", "la", "le", "les", "du", "des", "d", "l", "the", "of", "di", "del", "della",
              "el", "los", "las", "von", "der", "den", "im", "am"}


def parse_coordinates(text: str):
    """Read coordinates in the formats people actually paste. Returns (lat, lon) or None.

    Accepted: '45.4897, 6.9853' · '45.4897 6.9853' · '45,4897 6,9853' · '45.4897° N, 6.9853° E'
    · '45°29'23"N 6°59'07"E'. Returns None when the text is not coordinates (a place name).
    """
    t = (text or "").upper().strip()

    dms = _DMS.findall(t)
    if len(dms) == 2 and not _DMS.sub("", t).strip(" ,;"):
        found = {}
        for deg, minutes, seconds, hemi in dms:
            value = int(deg) + int(minutes) / 60 + float(seconds.replace(",", ".")) / 3600
            found["lat" if hemi in "NS" else "lon"] = -value if hemi in "SW" else value
        return (found["lat"], found["lon"]) if len(found) == 2 else None

    numbers = re.findall(_NUMBER, t)
    rest = re.sub(r"LATITUDE|LONGITUDE|LAT|LON|LNG", "", re.sub(_NUMBER, "", t))
    hemis = re.findall(r"[NSEW]", rest)
    if len(numbers) != 2 or len(hemis) not in (0, 2) or re.sub(r"[NSEW°\s,;:=()\[\]]", "", rest):
        return None
    a, b = (float(n.replace(",", ".")) for n in numbers)

    if hemis:
        a = -abs(a) if hemis[0] in "SW" else abs(a)
        b = -abs(b) if hemis[1] in "SW" else abs(b)
        lat, lon = (b, a) if hemis[0] in "EW" else (a, b)
    else:
        lat, lon = a, b
        # 'longitude, latitude' pasted by mistake: swap when only the swapped pair is in Europe
        if not in_europe(lat, lon) and in_europe(lon, lat):
            lat, lon = lon, lat
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise PlannerError("These coordinates are out of range. Write them as 'latitude, longitude'.")
    return lat, lon


def _words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return [w for w in re.findall(r"[a-z0-9]+", plain) if w not in _STOPWORDS]


def match_score(query: str, candidate: str) -> float:
    """Share of the query's words found in the candidate (accents ignored, small typos allowed)."""
    q, c = _words(query), _words(candidate)
    if not q:
        return 0.0
    hits = sum(1 for w in q if any(w == u or difflib.SequenceMatcher(None, w, u).ratio() >= 0.8 for u in c))
    return hits / len(q)


def search_places(text: str) -> list[tuple]:
    """Candidate places as (lat, lon, label), from Photon first and ORS as a second opinion."""
    found = []
    try:
        resp = requests.get(
            PHOTON_URL, timeout=15,
            params={"q": text, "limit": 6, "lang": "en",
                    "bbox": f"{EUROPE['min_lon']},{EUROPE['min_lat']},{EUROPE['max_lon']},{EUROPE['max_lat']}"},
            headers=USER_AGENT,
        )
        if resp.status_code == 200:
            for f in resp.json().get("features") or []:
                p = f.get("properties") or {}
                if not p.get("name"):
                    continue
                lon, lat = f["geometry"]["coordinates"][:2]
                kind = str(p.get("osm_value") or "").replace("_", " ")
                parts = [p.get("city"), p.get("county"), p.get("state"), p.get("country")]
                where = ", ".join(dict.fromkeys(x for x in parts if x and x != p["name"]))
                label = p["name"] + (f" ({kind})" if kind and kind != "yes" else "") + (f", {where}" if where else "")
                found.append((lat, lon, label))
    except Exception as exc:
        print("Photon search error:", exc)

    good = any(match_score(text, c[2]) >= 1.0 for c in found)
    if ORS_API_KEY and not good:
        try:
            params = {"api_key": ORS_API_KEY, "text": text, "size": 5}
            params.update({f"boundary.rect.{k}": v for k, v in EUROPE.items()})
            resp = requests.get(f"{ORS_URL}/geocode/search", params=params, timeout=20)
            if resp.status_code == 200:
                for f in resp.json().get("features") or []:
                    lon, lat = f["geometry"]["coordinates"][:2]
                    found.append((lat, lon, (f.get("properties") or {}).get("label", text)))
        except Exception as exc:
            print("ORS search error:", exc)
    return found


def geocode(text: str):
    """Accepts a place name or coordinates. Returns (lat, lon, label, other_matches)."""
    text = (text or "").strip()
    if not text:
        raise PlannerError("Please enter a place name or coordinates.")

    coords = parse_coordinates(text)
    if coords:
        return coords[0], coords[1], f"{coords[0]:.5f}, {coords[1]:.5f}", []

    candidates = search_places(text)
    if not candidates:
        raise PlannerError(f"Could not find '{text}' in Europe. Check the spelling, add the country, or use coordinates.")
    ranked = sorted(candidates, key=lambda c: -match_score(text, c[2]))  # stable: provider order breaks ties
    best = ranked[0]
    if match_score(text, best[2]) < 0.6:
        # Never route from a place that only shares one word with what was asked.
        closest = "; ".join(c[2] for c in ranked[:3])
        raise PlannerError(f"No place really matches '{text}'. Closest results: {closest}. "
                           "Try a more precise name, or paste coordinates.")
    others = [c[2] for c in ranked[1:] if c[2] != best[2]][:3]
    return best[0], best[1], best[2], others


# --------------------------------------------------------------------------
# Step 2 - route variants
# --------------------------------------------------------------------------
def _ors_error(resp) -> str:
    try:
        err = resp.json().get("error")
        if isinstance(err, dict):
            return err.get("message") or str(err)
        if err:
            return str(err)
    except Exception:
        pass
    return f"HTTP {resp.status_code}"


def ors_route(coordinates, round_trip=None, alternatives=0) -> list[dict]:
    """One call to ORS hiking directions. Returns GeoJSON features (one per route)."""
    require_ors()
    body = {"coordinates": coordinates, "elevation": True, "instructions": False,
            "extra_info": ["traildifficulty"]}
    if round_trip:
        body["options"] = {"round_trip": round_trip}
    if alternatives > 1:
        body["alternative_routes"] = {"target_count": alternatives, "weight_factor": 1.6, "share_factor": 0.6}

    def send():
        return requests.post(f"{ORS_URL}/v2/directions/foot-hiking/geojson",
                             json=body, headers={"Authorization": ORS_API_KEY}, timeout=45)

    resp = send()
    if resp.status_code != 200 and "within a radius" in _ors_error(resp):
        # By default ORS only looks 350 m around a point for a path. Summits and lakes are
        # often further than that from the nearest mapped trail: retry with the widest search.
        body["radiuses"] = [-1] * len(coordinates)
        resp = send()
    if resp.status_code != 200:
        if "within a radius" in _ors_error(resp):
            raise PlannerError("No hiking path was found near this point. Pick a start closer to a trail, "
                               "for example the car park or village where the hike begins.")
        raise PlannerError(f"Routing failed: {_ors_error(resp)}")
    return resp.json().get("features") or []


def _ors_sac(props) -> int:
    """Hardest SAC grade met for at least 50 m, from the ORS 'traildifficulty' extra."""
    summary = ((props.get("extras") or {}).get("traildifficulty") or {}).get("summary") or []
    grades = [int(x.get("value", 0)) for x in summary if x.get("distance", 0) >= 50]
    return max((g for g in grades if 1 <= g <= 6), default=0)


def brouter_route(start, end):
    """Route with BRouter's alpine hiking profile.

    Returns (coords, sac, None) on success, or (None, 0, reason) so the reason can be shown.
    """
    try:
        resp = requests.get(BROUTER_URL, timeout=(5, 20), params={
            "lonlats": f"{start[1]:.6f},{start[0]:.6f}|{end[1]:.6f},{end[0]:.6f}",
            "profile": "hiking-mountain", "alternativeidx": 0, "format": "geojson",
        }, headers=USER_AGENT)
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


def _from_ors(feature) -> tuple:
    props = feature.get("properties", {})
    return (feature["geometry"]["coordinates"], props.get("summary", {}).get("distance"),
            props.get("ascent"), props.get("descent"), _ors_sac(props), "ors")


def _make_track(route, level, activity, end=None, out_and_back=False) -> dict:
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


# ---- Suggestions built from what is really on the map -------------------------------
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


def get_elevations(points) -> list:
    """Ground elevation for a list of (lat, lon), in one request per 100 points. None where unknown."""
    out = []
    for i in range(0, len(points), 100):
        chunk = points[i:i + 100]
        try:
            resp = requests.get(ELEVATION_URL, timeout=20, params={
                "latitude": ",".join(f"{p[0]:.5f}" for p in chunk),
                "longitude": ",".join(f"{p[1]:.5f}" for p in chunk)})
            values = resp.json()["elevation"]
            out += [float(v) for v in values] if len(values) == len(chunk) else [None] * len(chunk)
        except Exception as exc:
            print("Elevation error:", exc)
            out += [None] * len(chunk)
    return out


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


def path_km(points) -> float:
    return sum(haversine_km(a, b) for a, b in zip(points, points[1:]))


def stitch_ways(ways):
    """Join a trail's pieces end to end. Returns (ordered points, share of the total length used)."""
    pieces = [list(w) for w in ways if len(w) >= 2]
    if not pieces:
        return [], 0.0
    total = sum(path_km(w) for w in pieces) or 1.0
    touch = lambda a, b: haversine_km(a, b) < 0.02
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
        chain = chain[nearest:] + chain[1:nearest + 1]  # same loop, starting at the point closest to the user
        step = max(1, len(chain) // 10)
        waypoints = chain[::step][:10] + [chain[0]]
        if approach > 0.15:
            waypoints = [start] + waypoints + [start]
        loops.append({"name": trail["name"], "waypoints": waypoints, "length_km": length + 2 * approach,
                      "score": abs(length / LEVELS[level]["flat_kmh"] - hours)})
    return sorted(loops, key=lambda l: l["score"])[:limit]


KIND_PLURAL = {"summit": "Summits", "lake": "Lakes", "hut": "Huts", "viewpoint": "Viewpoints",
               "waterfall": "Waterfalls", "pass": "Passes"}


def out_of_reach_hint(goals, chosen, start, kinds, hours, level) -> str:
    """When the hiker asked for lakes (say) and none fits, name the closest ones instead of staying silent."""
    wanted = [k for k in (kinds or []) if k in KIND_PLURAL]
    if not wanted or any(g["kind"] in wanted for g in chosen):
        return ""
    near = sorted((g for g in goals if g["kind"] in wanted), key=lambda g: haversine_km(start, (g["lat"], g["lon"])))[:3]
    if not near:
        return f"No {KIND_PLURAL[wanted[0]].lower()} were found on the map within reach of this start."
    names = ", ".join(f"{g['name']} ({haversine_km(start, (g['lat'], g['lon'])):.1f} km away)" for g in near)
    return (f"{KIND_PLURAL[wanted[0]]} nearby are too far for {hours:g} h at {level.lower()} level: {names}. "
            "Ask for one by name to plan a there-and-back to it anyway.")


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
        track.update(title=f"{g['name']} ({g['kind']})", kind=f"Out and back to a {g['kind']}", far_label=g["name"],
                     place=g["name"])
        tracks.append(track)

    for loop in ([] if kinds and "loop" not in kinds else marked_loops(trails, start, hours, level, activity)):
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
    tracks = tracks[:len(VARIANT_COLORS)]
    for i, t in enumerate(tracks):
        t["name"] = f"{'ABCD'[i]} · {t['title']}"
        t["color"] = VARIANT_COLORS[i]
    return tracks, trails


def plan_variants(start, end, hours, level, activity, out_and_back=False) -> list[dict]:
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
                routes += [from_ors(f) for f in ors_route(
                    [[start[1], start[0]]],
                    round_trip={"length": length_m, "points": 3, "seed": seed},
                )[:1]]
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


# --------------------------------------------------------------------------
# Step 3 - weather and alerts
# --------------------------------------------------------------------------
def get_weather(lat, lon, day_index: int):
    """Daily forecast from Open-Meteo. Returns None if the service is unreachable."""
    daily = ["weather_code", "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
             "precipitation_probability_max", "wind_gusts_10m_max", "sunrise", "sunset", "daylight_duration"]
    try:
        resp = requests.get(METEO_URL, timeout=20, params={
            "latitude": round(lat, 4), "longitude": round(lon, 4),
            "daily": ",".join(daily), "timezone": "auto", "forecast_days": 7,
        })
        d = resp.json()["daily"]
        i = min(int(day_index or 0), len(d["time"]) - 1)
        code = d["weather_code"][i]
        return {
            "date": d["time"][i], "code": code, "summary": WMO.get(code, "Unknown"),
            "tmax": d["temperature_2m_max"][i], "tmin": d["temperature_2m_min"][i],
            "precip_mm": d["precipitation_sum"][i] or 0,
            "precip_prob": d["precipitation_probability_max"][i] or 0,
            "gusts": d["wind_gusts_10m_max"][i] or 0,
            "sunrise": d["sunrise"][i][-5:], "sunset": d["sunset"][i][-5:],
            "daylight_h": round((d["daylight_duration"][i] or 0) / 3600, 1),
        }
    except Exception as exc:
        print("Weather error:", exc)
        return None


def terrain_alerts(tracks, end=None) -> list[str]:
    """Warnings that come from the ground, not the sky."""
    alerts = []
    hardest = max(t["sac"] for t in tracks)
    if hardest >= 5:
        alerts.append(f"🧗 **Alpine terrain ({SAC_LABELS[hardest]})** - scrambling and exposed passages, possibly "
                      "snow or glacier. This is mountaineering ground, not hiking: experience and equipment required.")
    elif hardest == 4:
        alerts.append("🧗 **T4 alpine hiking on the way** - hands needed in places, exposed passages, "
                      "path sometimes hard to follow.")
    top = max((t["max_ele"] for t in tracks if t["max_ele"]), default=0)
    if top >= 2500:
        alerts.append(f"🏔️ **High mountain** (up to {top} m) - snow or ice can remain or arrive in any season; "
                      "check recent conditions and expect to move slower than the estimate.")
    if any(t.get("router") == "brouter" for t in tracks):
        alerts.append("ℹ️ The standard hiking router does not go all the way to this destination (usually because the "
                      "last section is graded too difficult). This route was computed with an alpine profile instead.")
    short = min((t["end_gap_km"] for t in tracks if "end_gap_km" in t), default=0)
    if end is not None and short > 0.3:
        t = min((t for t in tracks if "end_gap_km" in t), key=lambda t: t["end_gap_km"])
        missing, climb_left = "", 0
        target = get_elevations([end])[0]
        if target is not None and t["eles"]:
            reached = t["eles"][t["latlon"].index(t["far"])]
            if target - reached > 50:
                climb_left = target - reached
                missing = f" and about {round(climb_left, -1):.0f} m below it"
        legs = 2 if t.get("far_label") == "Turnaround point" else 1
        full_km = t["distance_km"] + short * 1.3 * legs
        alerts.append(f"🛑 **The route stops before your destination**: it ends {short:.1f} km away{missing}. "
                      "The rest is not mapped as a path, or is beyond hiking grade, so it is not drawn and not "
                      f"counted. Including it, the outing would be roughly {full_km:.0f} km and "
                      f"+{round(t['ascent_m'] + climb_left, -1):.0f} m."
                      + (f" (Alpine router: {t['alpine_note']}.)" if t.get("alpine_note") else ""))
    return alerts


def weather_alerts(w, longest_hours: float) -> list[str]:
    if not w:
        return []
    alerts = []
    if w["code"] in STORM_CODES:
        alerts.append("⛈️ **Thunderstorms forecast** - stay off ridges and summits, or pick another day.")
    if w["code"] in SNOW_CODES:
        alerts.append("❄️ **Snow forecast** - paths may be hidden and slippery; winter equipment needed.")
    if w["code"] in ICE_CODES:
        alerts.append("🧊 **Freezing rain or fog** - icy ground and poor visibility.")
    if w["precip_prob"] >= 60 or w["precip_mm"] >= 10:
        alerts.append(f"🌧️ **Rain likely** ({w['precip_prob']} %, {w['precip_mm']} mm) - waterproofs, slippery rock.")
    if w["gusts"] >= 60:
        alerts.append(f"💨 **Strong wind** (gusts {round(w['gusts'])} km/h) - avoid exposed sections.")
    if w["tmax"] is not None and w["tmax"] >= 30:
        alerts.append(f"🥵 **Heat** (up to {round(w['tmax'])} °C) - start early, carry extra water.")
    if w["tmin"] is not None and w["tmin"] <= 0:
        alerts.append(f"🥶 **Frost** (down to {round(w['tmin'])} °C) - possible ice in the morning.")
    if w["daylight_h"] and longest_hours > 0.7 * w["daylight_h"]:
        alerts.append(f"🌙 **Short daylight** ({w['daylight_h']} h, sunset {w['sunset']}) - "
                      "the longest variant leaves little margin; take a headlamp.")
    return alerts


# --------------------------------------------------------------------------
# Step 4 - rule-based recommendation
# --------------------------------------------------------------------------
def suits(track, level) -> bool:
    max_sac, max_grade = LEVEL_LIMITS[level]
    return track["sac"] <= max_sac and GRADES.index(track["grade"]) <= GRADES.index(max_grade)


def recommend(tracks, level, hours):
    """The suitable variant closest to the wished duration, or None when nothing suits the level."""
    pool = [t for t in tracks if suits(t, level)]
    return min(pool, key=lambda t: abs(t["hours"] - hours)) if pool else None


def nearby_hikes(lat, lon, radius_km=30, limit=5) -> list[dict]:
    found = []
    for h in load_hikes():
        try:
            d = haversine_km((lat, lon), h["start"])
        except Exception:
            continue
        if d <= radius_km:
            found.append({**h, "away_km": round(d, 1)})
    return sorted(found, key=lambda h: h["away_km"])[:limit]


# --------------------------------------------------------------------------
# Step 5 - LLM briefing (optional: the app works without it)
# --------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a careful mountain guide writing a short pre-hike briefing in English. "
    "Use ONLY the data in the user's message. Never invent trail names, huts, water points "
    "or facts about the area. In at most 130 words: say which variant suits this hiker and why, "
    "mention the weather and every alert, and give two practical tips adapted to their level. "
    "If the alerts make the outing unwise, say so plainly. If pre_selected_variant is null, no route "
    "suits this hiker's level: say that clearly, explain why from the data, and do not recommend one. "
    "Plain text, no headings."
)


def ai_briefing(context: dict):
    if not HF_TOKEN:
        return None
    try:
        from huggingface_hub import InferenceClient

        client = InferenceClient(token=HF_TOKEN, timeout=60)
        out = client.chat_completion(
            model=LLM_MODEL, max_tokens=300, temperature=0.4,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
        )
        return (out.choices[0].message.content or "").strip() or None
    except Exception as exc:  # no credits left, model unavailable, ...
        print("LLM error:", exc)
        return None


def fallback_briefing(rec, level, alerts, tracks) -> str:
    if rec is None:
        easiest = tracks[0]
        return (f"None of these routes is a good match at {level.lower()} level. Even the easiest one is "
                f"{easiest['distance_km']} km with {easiest['ascent_m']} m of ascent, about {easiest['hours']} h, "
                f"graded {easiest['grade']} (terrain: {easiest['terrain']}). Choose a closer or lower goal.")
    text = (f"At {level.lower()} level, variant **{rec['name']}** is the best match: "
            f"{rec['distance_km']} km, {rec['ascent_m']} m of ascent, about {rec['hours']} h ({rec['grade']}).")
    if alerts:
        text += " Read the alerts above before deciding to go."
    return text


# --------------------------------------------------------------------------
# GPX files
# --------------------------------------------------------------------------
def write_gpx(track, folder) -> str:
    eles = track["eles"] or [None] * len(track["latlon"])
    points = "".join(
        f'<trkpt lat="{lat:.6f}" lon="{lon:.6f}">' + (f"<ele>{ele:.1f}</ele>" if ele is not None else "") + "</trkpt>"
        for (lat, lon), ele in zip(track["latlon"], eles)
    )
    name = html.escape(f"iHike - {track['name']}")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<gpx version="1.1" creator="iHike" xmlns="http://www.topografix.com/GPX/1/1">'
           f"<trk><name>{name}</name><trkseg>{points}</trkseg></trk></gpx>")
    path = Path(folder) / f"variant_{track['name'][0]}.gpx"
    path.write_text(xml, encoding="utf-8")
    return str(path)


def parse_gpx(path) -> list:
    if os.path.getsize(path) > MAX_GPX_BYTES:
        raise PlannerError("This GPX file is larger than 5 MB.")
    try:
        root = SafeET.parse(path).getroot()
    except Exception:
        raise PlannerError("This file is not a readable GPX file.")
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
                try:
                    ele = float(child.text)
                except (TypeError, ValueError):
                    pass
        coords.append([lon, lat, ele])
    if len(coords) < 2:
        raise PlannerError("No track points found in this GPX file.")
    return coords


def _record(track, title, notes, dog, camping, source) -> dict:
    return {
        "id": uuid.uuid4().hex[:12], "source": source,
        "title": (title or "").strip()[:80] or "Untitled hike",
        "notes": (notes or "").strip()[:500],
        "dog": dog, "camping": camping,
        "distance_km": track["distance_km"], "ascent_m": track["ascent_m"], "grade": track["grade"],
        "loop": track["loop"],
        "start": [round(track["latlon"][0][0], 5), round(track["latlon"][0][1], 5)],
        "path": [[round(lat, 5), round(lon, 5)] for lat, lon in decimate(track["latlon"], 300)],
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# One call that does everything (used by both interfaces)
# --------------------------------------------------------------------------
MODES = ("ideas", "out_and_back", "one_way")


def plan_hikes(start_text, mode="ideas", end_text="", activity="Hiking", level="Beginner",
               hours=3.0, day_index=0, kinds=None) -> dict:
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
    end, e_label, e_others = None, "", []
    out_and_back = mode == "out_and_back"
    if mode != "ideas":
        e_lat, e_lon, e_label, e_others = geocode(end_text)
        if not in_europe(e_lat, e_lon):
            raise PlannerError("The destination is outside Europe.")
        end = (e_lat, e_lon)

    tracks, trails, note, hints = [], [], "", []
    if auto_start:
        # No start given: find where people leave the car and offer an easy, a medium and a hard way.
        require_ors()
        tracks = plan_from_car_parks(end, e_label, hours, level, activity, out_and_back)
        first = tracks[0]["start_point"]
        s_lat, s_lon, s_label, s_others = first["lat"], first["lon"], first["label"], []
    if mode == "ideas":
        require_ors()
        tracks, trails = suggest_hikes((s_lat, s_lon), hours, level, activity, kinds=kinds, hints=hints)
        if tracks is None:
            note = "OpenStreetMap could not be searched just now, so these are computed loops, not known hikes."
        elif not tracks:
            note = ("No named goal or waymarked loop within reach could be routed from here, "
                    "so these are computed loops, not known hikes. Try a longer time or a start nearer the trails.")
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

    briefing = ai_briefing({
        "hiker": {"level": level, "activity": activity, "wished_duration_h": hours},
        "start": s_label,
        "trip": ("ideas found on the map" if ideas else "computed loop") if end is None
        else ("out and back, figures are for the full return trip" if out_and_back else "one way"),
        "variants": [{**{k: t[k] for k in ("name", "kind", "distance_km", "ascent_m", "descent_m", "max_ele",
                                            "hours", "grade", "terrain")}, "suits_this_hiker": suits(t, level)}
                     for t in tracks],
        "pre_selected_variant": rec["name"] if rec else None,
        "weather": weather, "alerts": alerts,
    })
    ai_used = briefing is not None

    return {
        "start": {"lat": s_lat, "lon": s_lon, "label": s_label, "others": s_others},
        "end": {"lat": end[0], "lon": end[1], "label": e_label, "others": e_others} if end else None,
        "mode": mode, "out_and_back": out_and_back, "ideas": ideas, "note": note, "hint": " ".join(hints),
        "auto_start": auto_start,
        "level": level, "activity": activity, "hours": hours,
        "tracks": tracks, "recommended": rec, "trails": trails or [],
        "weather": weather, "weather_where": w_where, "alerts": alerts,
        "briefing": briefing or fallback_briefing(rec, level, alerts, tracks), "ai_briefing": ai_used,
        "start_offset_m": haversine_km((s_lat, s_lon), tracks[0]["latlon"][0]) * 1000,
        "nearby": nearby_hikes(s_lat, s_lon),
    }


# --------------------------------------------------------------------------
# Plain-data versions for the web interface
# --------------------------------------------------------------------------
_ALERT_RE = re.compile(r"^(\S+)\s+(?:\*\*(.+?)\*\*)?\s*(.*)$", re.S)
_DANGER_ICONS = {"🛑", "⛈️", "🧊"}
_INFO_ICONS = {"ℹ️"}


def alert_data(text: str) -> dict:
    """'⛈️ **Thunderstorms forecast** - stay off ridges' -> {severity, title, detail}."""
    m = _ALERT_RE.match(text.strip())
    icon, title, detail = (m.group(1), m.group(2) or "", m.group(3)) if m else ("", "", text)
    detail = re.sub(r"^[\s:\-–]+", "", detail.replace("**", "")).strip()
    if title and detail.startswith("("):  # '(80 %, 14 mm) - waterproofs' belongs to the title
        figures, _, detail = detail.partition(")")
        title = f"{title} {figures})"
        detail = re.sub(r"^[\s:\-–]+", "", detail)
    detail = detail[:1].upper() + detail[1:]
    severity = ("danger" if icon in _DANGER_ICONS or "T5" in title or "T6" in title
                else "info" if icon in _INFO_ICONS else "warning")
    if not title:  # information lines have no bold title: use their first sentence
        first, _, rest = detail.partition(". ")
        title, detail = first.rstrip("."), rest
    return {"severity": severity, "title": title, "detail": detail}


def track_data(t: dict, level: str, rec=None, max_points=1500, title=None, kind=None) -> dict:
    eles = t["eles"] or [None] * len(t["latlon"])
    pts = decimate(list(zip(t["latlon"], eles)), max_points)
    name = t.get("name", "")
    return {
        "id": name[:1] or "A", "name": name,
        "title": title or t.get("place") or t.get("title") or name.split(" · ", 1)[-1],
        "kind": kind or t.get("kind", ""), "color": t.get("color", VARIANT_COLORS[0]),
        "distance_km": t["distance_km"], "ascent_m": t["ascent_m"], "descent_m": t["descent_m"],
        "max_ele": t["max_ele"], "hours": t["hours"], "grade": t["grade"], "sac": t["sac"],
        "terrain": t["terrain"], "loop": t["loop"], "router": t.get("router", "ors"),
        "recommended": t is rec, "suits": suits(t, level),
        "far": {"lat": t["far"][0], "lon": t["far"][1], "label": t.get("far_label", "")} if t.get("far") else None,
        "end_gap_km": round(t.get("end_gap_km", 0.0), 2),
        "start_point": t.get("start_point"),
        "geometry": [[round(lon, 6), round(lat, 6)] + ([round(e, 1)] if e is not None else [])
                     for (lat, lon), e in pts],
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
        kind = _KIND_TEXT.get(t.get("kind"), t.get("kind", ""))
        return {"title": where, "kind": f"{kind}, {variant.lower()} way" if many else kind}
    return {"title": {"Easiest": "Short loop", "Balanced": "Medium loop", "Most demanding": "Long loop",
                      "Easier": "Short loop", "Harder": "Long loop"}.get(variant, "Loop"),
            "kind": "Computed loop, not a known hike"}


def plan_data(result: dict) -> dict:
    """plan_hikes() output -> JSON-ready dict."""
    rec = result["recommended"]
    named = list({t["name"]: t for t in result["trails"] if t["name"] != "Unnamed trail"}.values())[:6]
    return {
        "start": result["start"], "end": result["end"], "mode": result["mode"],
        "ideas": result["ideas"], "note": result["note"], "hint": result.get("hint", ""),
        "auto_start": result.get("auto_start", False),
        "out_and_back": result["out_and_back"],
        "level": result["level"], "hours": result["hours"],
        "hikes": [track_data(t, result["level"], rec, **_names(t, result)) for t in result["tracks"]],
        "no_match": rec is None,
        "weather": dict(result["weather"], where=result["weather_where"]) if result["weather"] else None,
        "alerts": [alert_data(a) for a in result["alerts"]],
        "briefing": {"text": result["briefing"].replace("**", ""), "ai": result["ai_briefing"]},
        "start_offset_m": round(result["start_offset_m"]),
        "trails": [{"id": t["id"], "name": t["name"], "network": t["network"],
                    "url": f"https://hiking.waymarkedtrails.org/#route?id={t['id']}"} for t in named],
        "nearby": [public_hike(h, with_path=False) | {"away_km": h["away_km"]} for h in result["nearby"]],
    }


def config_status() -> dict:
    return {
        "routing": bool(ORS_API_KEY), "ai_briefing": bool(HF_TOKEN),
        "persistence": scheduler is not None,
        "persistence_error": "" if scheduler else (
            "no HIKES_DATASET" if not HIKES_DATASET else "HIKES_DATASET needs HF_TOKEN" if not HF_TOKEN
            else f"could not open dataset {HIKES_DATASET} - {persistence_error}"),
    }


# --------------------------------------------------------------------------
# Community: photos and shared hikes
# --------------------------------------------------------------------------
MAX_PHOTO_BYTES = 12 * 1024 * 1024
MAX_PHOTOS_PER_HIKE = 6
_PHOTO_ID = re.compile(r"^[0-9a-f]{16}$")


def save_photo(raw: bytes):
    """Store an uploaded picture as a clean JPEG. Returns (photo_id, PIL image) or raises PlannerError.

    Re-encoding drops all metadata (camera, exact GPS position, owner) before anything is published.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(raw) > MAX_PHOTO_BYTES:
        raise PlannerError("This photo is larger than 12 MB.")
    try:
        import io

        img = Image.open(io.BytesIO(raw))
        if img.width * img.height > 60_000_000:
            raise PlannerError("This photo has too many pixels.")
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise PlannerError("This file is not a readable image.")
    photo_id = uuid.uuid4().hex[:16]
    big = img.copy()
    big.thumbnail((1600, 1600))
    big.save(PENDING_DIR / f"{photo_id}.jpg", "JPEG", quality=85, optimize=True)
    small = img.copy()
    small.thumbnail((480, 480))
    small.save(PENDING_DIR / f"{photo_id}_s.jpg", "JPEG", quality=80, optimize=True)
    return photo_id, img


def photo_path(name: str):
    """Path of a stored photo file ('<id>.jpg' or '<id>_s.jpg'), or None."""
    m = re.fullmatch(r"([0-9a-f]{16})(_s)?\.jpg", name or "")
    if not m:
        return None
    for folder in (PHOTOS_DIR, PENDING_DIR):
        if (folder / name).exists():
            return folder / name
    return None


def publish_photos(photos) -> list[dict]:
    """Move uploaded photos from pending to the published folder. `photos` = [{id, species}]."""
    kept = []
    for p in (photos or [])[:MAX_PHOTOS_PER_HIKE]:
        pid = str((p or {}).get("id", ""))
        if not _PHOTO_ID.match(pid):
            continue
        moved = False
        lock = scheduler.lock if scheduler else _local_lock
        with lock:
            for suffix in (".jpg", "_s.jpg"):
                src, dst = PENDING_DIR / f"{pid}{suffix}", PHOTOS_DIR / f"{pid}{suffix}"
                if src.exists():
                    shutil.move(src, dst)
                    moved = True
                elif dst.exists():
                    moved = True
        if moved:
            species = [{"name": str(x.get("name", ""))[:60], "scientific": str(x.get("scientific", ""))[:60],
                        "score": round(float(x.get("score", 0)), 2)}
                       for x in (p.get("species") or [])[:3] if isinstance(x, dict)]
            kept.append({"id": pid, "species": species})
    return kept


def track_from_geometry(geometry) -> dict:
    """Validate [[lon, lat, ele?], ...] sent by a browser and rebuild its statistics server-side."""
    if not isinstance(geometry, list) or not 2 <= len(geometry) <= 5000:
        raise PlannerError("The route is missing or too long.")
    coords = []
    for p in geometry:
        try:
            lon, lat = float(p[0]), float(p[1])
            ele = float(p[2]) if len(p) > 2 and p[2] is not None else None
        except (TypeError, ValueError, IndexError):
            raise PlannerError("The route contains invalid points.")
        if not (math.isfinite(lon) and math.isfinite(lat) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise PlannerError("The route contains invalid points.")
        if ele is not None and not (math.isfinite(ele) and -500 < ele < 9000):
            ele = None
        coords.append([lon, lat, ele])
    if any(c[2] is None for c in coords):
        coords = [c[:2] for c in coords]
    track = build_track(coords)
    if not in_europe(*track["latlon"][0]):
        raise PlannerError("This route starts outside Europe, which is the only area covered for now.")
    return track


def share_track(track, title, notes, dog, camping, source, photos=None, sac=None) -> dict:
    record = _record(track, title, notes, dog if dog in ("Yes", "No") else "Unknown",
                     camping if camping in ("Yes", "No") else "Unknown", source)
    record["max_ele"] = track["max_ele"]
    if sac:
        record["sac"] = int(sac) if str(sac).isdigit() and 0 < int(sac) <= 6 else 0
    record["photos"] = publish_photos(photos)
    save_hike(record)
    return record


def public_hike(h: dict, with_path=True) -> dict:
    """A stored hike as sent to browsers: only known fields, photos as URLs."""
    out = {k: h.get(k) for k in ("id", "title", "notes", "dog", "camping", "distance_km", "ascent_m",
                                 "grade", "loop", "start", "created", "source", "max_ele")}
    if with_path:
        out["path"] = h.get("path") or []
    photos = []
    for p in h.get("photos") or []:
        if isinstance(p, dict) and _PHOTO_ID.match(str(p.get("id", ""))):
            photos.append({"url": f"/photos/{p['id']}.jpg", "thumb": f"/photos/{p['id']}_s.jpg",
                           "species": p.get("species") or []})
    out["photos"] = photos
    seen = []
    for p in photos:
        for sp in p["species"][:1]:
            if sp.get("name") and sp["name"] not in seen:
                seen.append(sp["name"])
    out["species"] = seen
    return out


# --------------------------------------------------------------------------
# Where to leave the car: trailheads and car parks near a place
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# Destination only: start from the car parks around it
# --------------------------------------------------------------------------
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
        name = p["name"] if p["named"] else (f"a {'trailhead' if p['kind'] == 'trailhead' else 'car park'}"
                                              + (f" at {p['ele']} m" if p.get("ele") else ""))
        track.update(place=f"From {name}", start_point={"lat": p["lat"], "lon": p["lon"], "label": name},
                     far_label=where)
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
