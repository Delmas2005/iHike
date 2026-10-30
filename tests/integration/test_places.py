"""Place search against simulated Photon and OpenRouteService geocoders."""
from __future__ import annotations

import pytest


def test_coordinates_skip_the_place_search(engine, web):
    assert engine.geocode("45.9237, 6.8694")[:2] == (45.9237, 6.8694)
    assert not web.calls


def test_the_right_peak_wins_over_a_place_sharing_one_word(engine, web):
    lat, lon, label, others = engine.geocode("aiguille de la grande sassiere")
    assert (lat, lon) == pytest.approx((45.4892, 6.9858), abs=1e-3)
    assert "peak" in label and any("Embrun" in o for o in others)


def test_match_score(engine):
    assert engine.match_score("aiguille de la grande sassiere", "Aiguille (hamlet), Embrun, France") < 0.6
    assert engine.match_score("chamonnix", "Chamonix-Mont-Blanc (town), France") == 1.0


def test_a_weak_match_is_refused_with_the_closest_results(engine, web):
    with pytest.raises(engine.PlannerError, match="Closest results: Aiguille, Embrun"):
        engine.geocode("aiguille wrongonly sommet")


def test_unknown_place(engine, web):
    with pytest.raises(engine.PlannerError, match="Could not find"):
        engine.geocode("nowhere")


def test_empty_place(engine, web):
    with pytest.raises(engine.PlannerError, match="Please enter"):
        engine.geocode("   ")
