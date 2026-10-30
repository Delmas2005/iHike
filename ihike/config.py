from __future__ import annotations

import os

from .errors import PlannerError


ORS_API_KEY = os.environ.get("ORS_API_KEY", "").strip()  # secret
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip()  # secret
HIKES_DATASET = os.environ.get("HIKES_DATASET", "").strip()  # variable, e.g. "user/monarch-hikes"
LLM_MODEL = os.environ.get("LLM_MODEL", "meta-llama/Llama-3.3-70B-Instruct").strip()
ORS_URL = "https://api.openrouteservice.org"
METEO_URL = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
BROUTER_URL = "https://brouter.de/brouter"  # second router: accepts alpine paths (T5/T6) that ORS refuses
PHOTON_URL = "https://photon.komoot.io/api/"
# OpenStreetMap query servers, tried in order. They list what is really on the ground.
# Public servers are often busy, and a Space shares its address with many others: try several.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = {"User-Agent": "iHike/1.0 (Hugging Face Space)"}
EUROPE = {"min_lon": -25.0, "min_lat": 34.0, "max_lon": 45.0, "max_lat": 72.0}
# Pace assumptions per level: flat speed (km/h), climbing and descending rate (m/h)
LEVELS = {
    "Beginner": {"flat_kmh": 3.5, "up_mh": 300, "down_mh": 500},
    "Intermediate": {"flat_kmh": 4.5, "up_mh": 400, "down_mh": 600},
    "Experienced": {"flat_kmh": 5.5, "up_mh": 500, "down_mh": 800},
}
ACTIVITY_SPEED = {"Hiking": 1.0, "Trail running": 1.8}
EFFORT_LIMITS = {"Beginner": 16, "Intermediate": 25, "Experienced": 40}  # effort-km worth trying per level
NETWORKS = {"iwn": "international", "nwn": "national", "rwn": "regional", "lwn": "local"}
# Technical difficulty of the path itself: the SAC hiking scale, as tagged on OpenStreetMap.
SAC_TAGS = {
    "hiking": 1,
    "mountain_hiking": 2,
    "demanding_mountain_hiking": 3,
    "alpine_hiking": 4,
    "demanding_alpine_hiking": 5,
    "difficult_alpine_hiking": 6,
}
SAC_LABELS = {
    0: "not graded on the map",
    1: "T1 hiking",
    2: "T2 mountain hiking",
    3: "T3 demanding mountain hiking",
    4: "T4 alpine hiking",
    5: "T5 demanding alpine hiking",
    6: "T6 difficult alpine hiking",
}
GRADES = ["Easy", "Moderate", "Hard", "Very hard", "Alpine"]
# What we are willing to recommend to each level: (hardest path grade, hardest overall grade)
LEVEL_LIMITS = {"Beginner": (2, "Moderate"), "Intermediate": (3, "Hard"), "Experienced": (4, "Very hard")}
VARIANT_COLORS = ["#C2702D", "#2F6B4F", "#3E6A8A", "#7A4A6B"]  # ochre, forest, slate, plum


def require_ors():
    if not ORS_API_KEY:
        raise PlannerError("The routing key is missing: add ORS_API_KEY in the Space secrets.")
