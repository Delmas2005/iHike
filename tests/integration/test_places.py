"""Place search against simulated Photon and OpenRouteService geocoders."""
from __future__ import annotations

import pytest

from ihike import errors, places


def test_coordinates_skip_the_place_search(web):
    assert places.geocode("45.9237, 6.8694")[:2] == (45.9237, 6.8694)
    assert not web.calls


def test_the_right_peak_wins_over_a_place_sharing_one_word(web):
    lat, lon, label, others = places.geocode("aiguille de la grande sassiere")
    assert (lat, lon) == pytest.approx((45.4892, 6.9858), abs=1e-3)
    assert "peak" in label and any("Embrun" in o for o in others)


def test_match_score():
    assert places.match_score("aiguille de la grande sassiere", "Aiguille (hamlet), Embrun, France") < 0.6
    assert places.match_score("chamonnix", "Chamonix-Mont-Blanc (town), France") == 1.0


def test_a_weak_match_is_refused_with_the_closest_results(web):
    with pytest.raises(errors.PlannerError, match="Closest results: Aiguille, Embrun"):
        places.geocode("aiguille wrongonly sommet")


def test_unknown_place(web):
    with pytest.raises(errors.PlannerError, match="Could not find"):
        places.geocode("nowhere")


def test_empty_place(web):
    with pytest.raises(errors.PlannerError, match="Please enter"):
        places.geocode("   ")
