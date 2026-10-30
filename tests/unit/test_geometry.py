"""Pure functions of the physical model, coordinates and GPX handling."""
from __future__ import annotations

import pytest


def test_haversine_one_degree_of_longitude_at_45n(engine):
    assert engine.haversine_km((45.0, 6.0), (45.0, 7.0)) == pytest.approx(78.6, abs=0.5)


def test_climb_ignores_gps_noise_and_counts_real_changes(engine):
    assert engine.climb([100, 102, 99, 101, 100]) == (0.0, 0.0)
    assert engine.climb([100, 200, 150]) == (100.0, 50.0)


def test_grades_and_europe_bounds(engine):
    assert engine.grade_for(5) == "Easy" and engine.grade_for(30) == "Very hard"
    assert engine.in_europe(45.9, 6.8) and not engine.in_europe(40.7, -74.0)


def test_estimated_time_for_a_beginner(engine):
    assert 4.0 < engine.estimate_hours(10, 600, 600, "Beginner", "Hiking") < 5.5


COORDINATE_FORMATS = [
    "45.4892, 6.9858", "45.4892 6.9858", "45,4892 6,9858", "45,4892, 6,9858", "45.4892,6.9858", "45.4892; 6.9858",
    "45.4892° N, 6.9858° E", "N 45.4892 E 6.9858", "6.9858 E 45.4892 N", "lat 45.4892 lon 6.9858",
    "(45.4892, 6.9858)", "6.9858, 45.4892", "45°29'21.1\"N 6°59'08.9\"E", "45° 29' 21,1'' N, 6° 59' 8,9'' E",
]


@pytest.mark.parametrize("text", COORDINATE_FORMATS)
def test_coordinate_formats(engine, text):
    got = engine.parse_coordinates(text)
    assert got == pytest.approx((45.4892, 6.9858), abs=1e-3)


def test_western_and_negative_coordinates(engine):
    assert engine.parse_coordinates("40.4168, -3.7038") == pytest.approx((40.4168, -3.7038), abs=1e-3)
    assert engine.parse_coordinates("40.4168 N 3.7038 W") == pytest.approx((40.4168, -3.7038), abs=1e-3)


@pytest.mark.parametrize("text", ["Chamonix", "aiguille de la grande sassiere", "Refuge 2000", "GR 20 Corse",
                                  "Route 66 km 4", "N7", ""])
def test_place_names_are_not_coordinates(engine, text):
    assert engine.parse_coordinates(text) is None


def test_out_of_range_coordinates_are_refused(engine):
    with pytest.raises(engine.PlannerError):
        engine.parse_coordinates("95.0, 200.0")


def test_stitch_ways_rebuilds_a_closed_loop(engine):
    chain, used = engine.stitch_ways([[(0, 0), (0, 0.01)], [(0.01, 0.01), (0, 0.01)],
                                      [(0.01, 0.01), (0.01, 0)], [(0, 0), (0.01, 0)]])
    assert used > 0.99 and len(chain) == 5 and chain[0] == chain[-1]


def test_stitch_ways_does_not_join_disconnected_pieces(engine):
    assert engine.stitch_ways([[(0, 0), (0, 0.01)], [(1, 1), (1, 1.01)]])[1] < 0.6


def test_gpx_round_trip_keeps_distance_and_climb(engine, tmp_path):
    coords = [[6.8694 + i * 0.001, 45.9237 + i * 0.0005, 1000 + i * 10] for i in range(40)]
    track = engine.build_track(coords)
    track["name"] = "A · Test"
    path = engine.write_gpx(track, tmp_path)
    again = engine.build_track(engine.parse_gpx(path))
    assert again["distance_km"] == pytest.approx(track["distance_km"], abs=0.05)
    assert again["ascent_m"] == pytest.approx(track["ascent_m"], abs=1)


@pytest.mark.parametrize("content", [
    "not xml",
    '<?xml version="1.0"?><!DOCTYPE g [<!ENTITY a "aaaa">]><gpx><trk>&a;</trk></gpx>',  # entity expansion
    "<gpx></gpx>",
])
def test_unreadable_or_hostile_gpx_is_refused(engine, tmp_path, content):
    path = tmp_path / "bad.gpx"
    path.write_text(content)
    with pytest.raises(engine.PlannerError):
        engine.parse_gpx(str(path))


@pytest.mark.parametrize("text,expected", [
    ("⛈️ **Thunderstorms forecast** - stay off ridges and summits, or pick another day.",
     {"severity": "danger", "title": "Thunderstorms forecast", "detail": "Stay off ridges and summits, or pick another day."}),
    ("🌧️ **Rain likely** (80 %, 14.0 mm) - waterproofs, slippery rock.",
     {"severity": "warning", "title": "Rain likely (80 %, 14.0 mm)", "detail": "Waterproofs, slippery rock."}),
])
def test_alert_text_becomes_structured_data(engine, text, expected):
    assert engine.alert_data(text) == expected


def test_information_alerts_and_alpine_terrain_severity(engine):
    info = engine.alert_data("ℹ️ The standard hiking router does not go all the way. This route was computed instead.")
    assert info["severity"] == "info" and info["title"].startswith("The standard hiking router")
    assert engine.alert_data("🧗 **Alpine terrain (T5 demanding alpine hiking)** - scrambling")["severity"] == "danger"
