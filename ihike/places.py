from __future__ import annotations

import difflib
import re
import unicodedata

import requests

from .config import EUROPE, ORS_API_KEY, ORS_URL, PHOTON_URL, USER_AGENT
from .errors import PlannerError
from .geo import in_europe


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
