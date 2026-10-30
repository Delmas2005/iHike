from __future__ import annotations

import requests

from .config import ELEVATION_URL, METEO_URL


WMO = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Violent showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with heavy hail",
}
STORM_CODES = {95, 96, 99}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
ICE_CODES = {48, 56, 57, 66, 67}


def get_elevations(points) -> list:
    """Ground elevation for a list of (lat, lon), in one request per 100 points. None where unknown."""
    out = []
    for i in range(0, len(points), 100):
        chunk = points[i : i + 100]
        try:
            resp = requests.get(
                ELEVATION_URL,
                timeout=20,
                params={
                    "latitude": ",".join(f"{p[0]:.5f}" for p in chunk),
                    "longitude": ",".join(f"{p[1]:.5f}" for p in chunk),
                },
            )
            values = resp.json()["elevation"]
            out += [float(v) for v in values] if len(values) == len(chunk) else [None] * len(chunk)
        except Exception as exc:
            print("Elevation error:", exc)
            out += [None] * len(chunk)
    return out


def get_weather(lat, lon, day_index: int):
    """Daily forecast from Open-Meteo. Returns None if the service is unreachable."""
    daily = [
        "weather_code",
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_sum",
        "precipitation_probability_max",
        "wind_gusts_10m_max",
        "sunrise",
        "sunset",
        "daylight_duration",
    ]
    try:
        resp = requests.get(
            METEO_URL,
            timeout=20,
            params={
                "latitude": round(lat, 4),
                "longitude": round(lon, 4),
                "daily": ",".join(daily),
                "timezone": "auto",
                "forecast_days": 7,
            },
        )
        d = resp.json()["daily"]
        i = min(int(day_index or 0), len(d["time"]) - 1)
        code = d["weather_code"][i]
        return {
            "date": d["time"][i],
            "code": code,
            "summary": WMO.get(code, "Unknown"),
            "tmax": d["temperature_2m_max"][i],
            "tmin": d["temperature_2m_min"][i],
            "precip_mm": d["precipitation_sum"][i] or 0,
            "precip_prob": d["precipitation_probability_max"][i] or 0,
            "gusts": d["wind_gusts_10m_max"][i] or 0,
            "sunrise": d["sunrise"][i][-5:],
            "sunset": d["sunset"][i][-5:],
            "daylight_h": round((d["daylight_duration"][i] or 0) / 3600, 1),
        }
    except Exception as exc:
        print("Weather error:", exc)
        return None
