"""Simulated web services for the tests.

`FakeWeb` stands in for `requests.get` / `requests.post`. Its answers are shaped like the
real ones from OpenRouteService, Photon, Overpass, BRouter and Open-Meteo. Attributes switch
scenarios on and off; `calls` records every request so tests can count them.
"""

from __future__ import annotations

import math
from typing import Any

import requests

HOME = (45.9237, 6.8694)  # Chamonix
START, SUMMIT, ORS_STOP = (45.4885, 6.9680), (45.50472, 6.99972), (45.4990, 6.9860)  # Grande Sassière case


def ground(lat: float, lon: float) -> float:
    """Synthetic terrain: rises to the north, 400 m per 0.01 degree."""
    return 1040 + (lat - HOME[0]) * 40000


def line(a, b, ele_a, ele_b, n=60):
    return [
        [a[1] + (b[1] - a[1]) * i / n, a[0] + (b[0] - a[0]) * i / n, ele_a + (ele_b - ele_a) * i / n]
        for i in range(n + 1)
    ]


def loop_feature(length_km: float, climb_m: float, n: int = 120) -> dict:
    """A loop around Chamonix with a single hill of `climb_m`."""
    r = length_km / (2 * math.pi) / 111.0
    coords = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        coords.append(
            [
                HOME[1] + r * math.cos(a) * 1.4 - r * 1.4,
                HOME[0] + r * math.sin(a),
                1040 + climb_m * math.sin(a / 2) ** 2,
            ]
        )
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {"summary": {"distance": length_km * 1000, "duration": 1}, "ascent": climb_m, "descent": climb_m},
    }


def node(i: int, lat: float, lon: float, **tags) -> dict:
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags}


class Resp:
    def __init__(self, payload: Any, status: int = 200, text: str = ""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not JSON")
        return self._payload


PHOTON = {
    "sassiere": [
        ((6.3, 44.56), {"name": "Aiguille", "osm_value": "hamlet", "city": "Embrun", "country": "France"}),
        (
            (6.9858, 45.4892),
            {"name": "Aiguille de la Grande Sassière", "osm_value": "peak", "county": "Savoie", "country": "France"},
        ),
    ],
    "chamonix": [
        (
            (6.8694, 45.9237),
            {"name": "Chamonix-Mont-Blanc", "osm_value": "town", "state": "Auvergne-Rhône-Alpes", "country": "France"},
        )
    ],
}

WEATHER_DAYS = ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13"]
WEATHER = {
    "daily": {
        "time": WEATHER_DAYS,
        "weather_code": [95, 3, 0, 0, 0, 0, 0],
        "temperature_2m_max": [12.0] * 7,
        "temperature_2m_min": [-1.0] * 7,
        "precipitation_sum": [14.0, 0, 0, 0, 0, 0, 0],
        "precipitation_probability_max": [80, 5, 0, 0, 0, 0, 0],
        "wind_gusts_10m_max": [72.0, 10, 10, 10, 10, 10, 10],
        "sunrise": [d + "T07:42" for d in WEATHER_DAYS],
        "sunset": [d + "T18:58" for d in WEATHER_DAYS],
        "daylight_duration": [40560.0] * 7,
    }
}


class FakeWeb:
    def __init__(self, haversine):
        self.haversine = haversine
        self.calls: list[tuple] = []
        self.overpass: list | None = None  # None = server down; otherwise the elements returned
        self.grid = False  # router draws straight lines between requested points
        self.short_ors = False  # ORS stops 500 m below the summit
        self.brouter: bool | str = False  # False = refuses; True = routes; "second try" = refuses the exact summit
        self.far_from_path = False  # ORS cannot snap without a wider radius
        self.brouter_timeout = False

    # ------------------------------------------------------------------ POST
    def post(self, url, json=None, headers=None, timeout=None, data=None):
        if "overpass" in url:
            self.calls.append(("OVERPASS", url, data))
            assert data["data"].startswith("[out:json]") and headers["User-Agent"]
            return Resp({"elements": self.overpass}) if self.overpass is not None else Resp("busy", 504)
        self.calls.append(("POST", url, json))
        assert url.endswith("/v2/directions/foot-hiking/geojson"), url
        assert headers["Authorization"] == "test-key"
        if self.grid and "options" not in json and "alternative_routes" not in json:
            return Resp({"features": [self._straight(json["coordinates"])]})
        if self.far_from_path and "radiuses" not in json:
            return Resp(
                {
                    "error": {
                        "code": 2010,
                        "message": "Could not find routable point within a radius of "
                        "350.0 meters of specified coordinate 0: 6.9858000 45.4892000.",
                    }
                },
                404,
            )
        assert json["extra_info"] == ["traildifficulty"]
        if self.short_ors and len(json["coordinates"]) == 2:
            coords = line(START, ORS_STOP, 2300, 3250)
            return Resp(
                {
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {"type": "LineString", "coordinates": coords},
                            "properties": {
                                "summary": {"distance": 2400.0},
                                "ascent": 950.0,
                                "descent": 0.0,
                                "extras": {
                                    "traildifficulty": {
                                        "summary": [
                                            {"value": 2, "distance": 1800.0, "amount": 75.0},
                                            {"value": 3, "distance": 600.0, "amount": 25.0},
                                            {"value": 6, "distance": 10.0, "amount": 0.1},
                                        ]
                                    }
                                },
                            },
                        }
                    ]
                }
            )
        round_trip = (json.get("options") or {}).get("round_trip")
        if round_trip:
            km = round_trip["length"] / 1000
            return Resp({"features": [loop_feature(km, km * 70)]})
        if json.get("alternative_routes"):
            return Resp({"features": [loop_feature(14, 900), loop_feature(9, 300), loop_feature(11, 600)]})
        return Resp({"features": [loop_feature(9, 300)]})

    def _straight(self, pts):
        if any(abs(p[0] - 6.99) < 1e-6 for p in pts):  # the "cliff" goal: the router stops 2 km short
            pts = pts[:-1] + [[pts[-1][0] - 0.03, pts[-1][1]]]
        coords: list = []
        for a, b in zip(pts, pts[1:]):
            seg = [[a[0] + (b[0] - a[0]) * i / 20, a[1] + (b[1] - a[1]) * i / 20] for i in range(21)]
            coords += [[x, y, ground(y, x)] for x, y in (seg if not coords else seg[1:])]
        eles = [c[2] for c in coords]
        up = sum(max(0, b - a) for a, b in zip(eles, eles[1:]))
        down = sum(max(0, a - b) for a, b in zip(eles, eles[1:]))
        dist = sum(self.haversine((a[1], a[0]), (b[1], b[0])) for a, b in zip(coords, coords[1:])) * 1000
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "summary": {"distance": dist},
                "ascent": up,
                "descent": down,
                "extras": {"traildifficulty": {"summary": [{"value": 2, "distance": dist, "amount": 100}]}},
            },
        }

    # ------------------------------------------------------------------- GET
    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(("GET", url, params))
        if "brouter" in url:
            return self._brouter(url, params)
        if "elevation" in url:
            lats, lons = params["latitude"].split(","), params["longitude"].split(",")
            if abs(float(lats[0]) - SUMMIT[0]) < 1e-4:
                return Resp({"elevation": [3747.0]})
            return Resp({"elevation": [ground(float(a), float(b)) for a, b in zip(lats, lons)]})
        if "photon" in url:
            assert headers and "User-Agent" in headers
            hits = next((v for k, v in PHOTON.items() if k in params["q"].lower()), [])
            return Resp({"features": [{"geometry": {"coordinates": list(c)}, "properties": p} for c, p in hits]})
        if "geocode" in url:
            if "wrongonly" in params["text"]:
                return Resp(
                    {
                        "features": [
                            {
                                "geometry": {"coordinates": [6.3, 44.56]},
                                "properties": {"label": "Aiguille, Embrun, France"},
                            }
                        ]
                    }
                )
            if "nowhere" in params["text"]:
                return Resp({"features": []})
            return Resp(
                {
                    "features": [
                        {
                            "geometry": {"coordinates": [6.8694, 45.9237]},
                            "properties": {"label": "Chamonix-Mont-Blanc, France"},
                        }
                    ]
                }
            )
        if "open-meteo" in url:
            return Resp(WEATHER)
        raise AssertionError(f"unexpected GET {url}")

    def _brouter(self, url, params):
        self.calls.append(("BROUTER", url, params))
        if self.brouter_timeout:
            raise requests.exceptions.ReadTimeout("read timed out")
        assert params["profile"] == "hiking-mountain" and params["lonlats"].startswith("6.968000,45.488500|")
        refused = Resp("error", 400, text="to-position not mapped in existing datafile\n")
        if not self.brouter:
            return refused
        if self.brouter == "second try" and params["lonlats"].endswith("|6.999720,45.504720"):
            return refused
        lon, lat = map(float, params["lonlats"].split("|")[1].split(","))
        return Resp(
            {
                "features": [
                    {
                        "geometry": {"type": "LineString", "coordinates": line(START, (lat, lon), 2300, 3747)},
                        "properties": {
                            "track-length": "3600",
                            "messages": [
                                ["Longitude", "Latitude", "Elevation", "Distance", "CostPerKm", "WayTags"],
                                [
                                    "6968000",
                                    "45488500",
                                    "2300",
                                    "900",
                                    "1200",
                                    "highway=path sac_scale=mountain_hiking",
                                ],
                                [
                                    "6990000",
                                    "45500000",
                                    "3400",
                                    "700",
                                    "1200",
                                    "highway=path sac_scale=demanding_alpine_hiking trail_visibility=bad",
                                ],
                            ],
                        },
                    }
                ]
            }
        )

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c[0] == kind)
