"""Pure functions of the physical model, coordinates and GPX handling."""
from __future__ import annotations

import pytest

from ihike import alerts, errors, geo, gpx, places


def test_haversine_one_degree_of_longitude_at_45n():
    assert geo.haversine_km((45.0, 6.0), (45.0, 7.0)) == pytest.approx(78.6, abs=0.5)


def test_climb_ignores_gps_noise_and_counts_real_changes():
    assert geo.climb([100, 102, 99, 101, 100]) == (0.0, 0.0)
    assert geo.climb([100, 200, 150]) == (100.0, 50.0)


def test_grades_and_europe_bounds():
    assert geo.grade_for(5) == "Easy" and geo.grade_for(30) == "Very hard"
    assert geo.in_europe(45.9, 6.8) and not geo.in_europe(40.7, -74.0)


def test_estimated_time_for_a_beginner():
    assert 4.0 < geo.estimate_hours(10, 600, 600, "Beginner", "Hiking") < 5.5


COORDINATE_FORMATS = [
    "45.4892, 6.9858", "45.4892 6.9858", "45,4892 6,9858", "45,4892, 6,9858", "45.4892,6.9858", "45.4892; 6.9858",
    "45.4892° N, 6.9858° E", "N 45.4892 E 6.9858", "6.9858 E 45.4892 N", "lat 45.4892 lon 6.9858",
    "(45.4892, 6.9858)", "6.9858, 45.4892", "45°29'21.1\"N 6°59'08.9\"E", "45° 29' 21,1'' N, 6° 59' 8,9'' E",
]


@pytest.mark.parametrize("text", COORDINATE_FORMATS)
def test_coordinate_formats(text):
    got = places.parse_coordinates(text)
    assert got == pytest.approx((45.4892, 6.9858), abs=1e-3)


def test_western_and_negative_coordinates():
    assert places.parse_coordinates("40.4168, -3.7038") == pytest.approx((40.4168, -3.7038), abs=1e-3)
    assert places.parse_coordinates("40.4168 N 3.7038 W") == pytest.approx((40.4168, -3.7038), abs=1e-3)


@pytest.mark.parametrize("text", ["Chamonix", "aiguille de la grande sassiere", "Refuge 2000", "GR 20 Corse",
                                  "Route 66 km 4", "N7", ""])
def test_place_names_are_not_coordinates(text):
    assert places.parse_coordinates(text) is None


def test_out_of_range_coordinates_are_refused():
    with pytest.raises(errors.PlannerError):
        places.parse_coordinates("95.0, 200.0")


def test_stitch_ways_rebuilds_a_closed_loop():
    chain, used = geo.stitch_ways([[(0, 0), (0, 0.01)], [(0.01, 0.01), (0, 0.01)],
                                      [(0.01, 0.01), (0.01, 0)], [(0, 0), (0.01, 0)]])
    assert used > 0.99 and len(chain) == 5 and chain[0] == chain[-1]


def test_stitch_ways_does_not_join_disconnected_pieces():
    assert geo.stitch_ways([[(0, 0), (0, 0.01)], [(1, 1), (1, 1.01)]])[1] < 0.6


def test_gpx_round_trip_keeps_distance_and_climb(tmp_path):
    coords = [[6.8694 + i * 0.001, 45.9237 + i * 0.0005, 1000 + i * 10] for i in range(40)]
    track = geo.build_track(coords)
    track["name"] = "A · Test"
    path = gpx.write_gpx(track, tmp_path)
    again = geo.build_track(gpx.parse_gpx(path))
    assert again["distance_km"] == pytest.approx(track["distance_km"], abs=0.05)
    assert again["ascent_m"] == pytest.approx(track["ascent_m"], abs=1)


@pytest.mark.parametrize("content", [
    "not xml",
    '<?xml version="1.0"?><!DOCTYPE g [<!ENTITY a "aaaa">]><gpx><trk>&a;</trk></gpx>',  # entity expansion
    "<gpx></gpx>",
])
def test_unreadable_or_hostile_gpx_is_refused(tmp_path, content):
    path = tmp_path / "bad.gpx"
    path.write_text(content)
    with pytest.raises(errors.PlannerError):
        gpx.parse_gpx(str(path))


@pytest.mark.parametrize("text,expected", [
    ("⛈️ **Thunderstorms forecast** - stay off ridges and summits, or pick another day.",
     {"severity": "danger", "title": "Thunderstorms forecast", "detail": "Stay off ridges and summits, or pick another day."}),
    ("🌧️ **Rain likely** (80 %, 14.0 mm) - waterproofs, slippery rock.",
     {"severity": "warning", "title": "Rain likely (80 %, 14.0 mm)", "detail": "Waterproofs, slippery rock."}),
])
def test_alert_text_becomes_structured_data(text, expected):
    assert alerts.alert_data(text) == expected


def test_information_alerts_and_alpine_terrain_severity():
    info = alerts.alert_data("ℹ️ The standard hiking router does not go all the way. This route was computed instead.")
    assert info["severity"] == "info" and info["title"].startswith("The standard hiking router")
    assert alerts.alert_data("🧗 **Alpine terrain (T5 demanding alpine hiking)** - scrambling")["severity"] == "danger"
