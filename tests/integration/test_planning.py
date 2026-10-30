"""planner.plan_hikes() end to end, with every external service simulated."""

from __future__ import annotations

import pytest
import requests

from fakes import HOME, START, SUMMIT, node
from ihike import community, errors, geo, osm, planner

SASSIERE = ("45.4885, 6.968", "45.50472, 6.99972")


def alerts_text(result) -> str:
    return "\n".join(result["alerts"])


# ------------------------------------------------------------------ loops
def test_computed_loops_on_a_stormy_day(web, store):
    r = planner.plan_hikes("Chamonix", "ideas", level="Beginner", hours=3, day_index=0)  # Overpass down
    tracks = r["tracks"]
    assert len(tracks) == 3 and [t["effort"] for t in tracks] == sorted(t["effort"] for t in tracks)
    assert not r["ideas"] and "could not be searched" in r["note"]
    text = alerts_text(r)
    for word in ("Thunderstorms", "Strong wind", "Frost", "Rain likely"):
        assert word in text
    assert r["recommended"] is not None and not r["ai_briefing"]
    lengths = [c[2]["options"]["round_trip"]["length"] for c in web.calls if c[0] == "POST"]
    assert lengths == sorted(lengths) and len(lengths) == 3


def test_start_far_from_any_path_is_retried_with_a_wider_search(web, store):
    web.far_from_path = True
    r = planner.plan_hikes("46.0, 6.90", "ideas", level="Beginner", hours=3, day_index=1)
    assert len(r["tracks"]) == 3 and r["start_offset_m"] > 300
    assert [c[2].get("radiuses") for c in web.calls if c[0] == "POST"][-1] == [-1]


def test_router_error_is_reported(web, monkeypatch, store):
    from fakes import Resp

    monkeypatch.setattr(
        requests, "post", lambda *a, **k: Resp({"error": {"code": 2004, "message": "Route too long"}}, 400)
    )
    with pytest.raises(errors.PlannerError, match="Route too long"):
        planner.plan_hikes("Chamonix", "ideas")


def test_start_outside_europe(web, store):
    with pytest.raises(errors.PlannerError, match="outside Europe"):
        planner.plan_hikes("40.7, -74.0", "ideas")


# --------------------------------------------------------- start to destination
def test_trip_to_a_destination_on_a_clear_day(web, store):
    r = planner.plan_hikes("Chamonix", "one_way", "45.95, 6.90", "Trail running", "Experienced", 2, 2)
    text = alerts_text(r)
    assert len(r["tracks"]) == 3 and "Thunderstorms" not in text and "Frost" in text


def test_summit_reached_through_the_alpine_router(web, store):
    web.short_ors, web.brouter = True, True
    r = planner.plan_hikes(*SASSIERE[:1], "out_and_back", SASSIERE[1], level="Beginner", hours=6, day_index=1)
    t = r["tracks"][0]
    assert len(r["tracks"]) == 1 and t["router"] == "brouter" and t["end_gap_km"] < 0.05
    assert t["max_ele"] == 3747 and t["ascent_m"] == pytest.approx(1447, abs=10)
    assert t["descent_m"] == pytest.approx(1447, abs=10)
    assert t["distance_km"] == pytest.approx(2 * geo.haversine_km(START, SUMMIT), abs=0.2)
    assert t["loop"] and t["far_label"] == "Turnaround point"
    assert t["sac"] == 5 and t["grade"] == "Alpine" and "T5" in t["terrain"]
    text = alerts_text(r)
    for needle in ("Alpine terrain (T5", "High mountain", "alpine profile"):
        assert needle in text
    assert "stops before your destination" not in text
    assert r["recommended"] is None and "None of these routes" in r["briefing"]


def test_t5_is_never_recommended_even_to_experienced_hikers(web, store):
    web.short_ors, web.brouter = True, True
    r = planner.plan_hikes(SASSIERE[0], "out_and_back", SASSIERE[1], level="Experienced", hours=8, day_index=1)
    assert r["recommended"] is None


def test_route_stopping_short_is_explained(web, store):
    web.short_ors, web.brouter = True, False
    r = planner.plan_hikes(SASSIERE[0], "out_and_back", SASSIERE[1], level="Intermediate", hours=6, day_index=1)
    t = r["tracks"][0]
    assert t["router"] == "ors" and t["sac"] == 3 and t["ascent_m"] == 950 and t["distance_km"] == 4.8
    text = alerts_text(r)
    assert "stops before your destination" in text and "about 500 m below it" in text
    assert "Alpine router: HTTP 400 to-position not mapped in existing datafile." in text
    assert "would be roughly 8 km and +1450 m" in text
    assert web.count("BROUTER") >= 3
    assert r["recommended"] is not None


def test_alpine_router_timeout_is_not_retried(web, store):
    web.short_ors, web.brouter_timeout = True, True
    r = planner.plan_hikes(SASSIERE[0], "out_and_back", SASSIERE[1], level="Intermediate", hours=6, day_index=1)
    assert web.count("BROUTER") == 1
    assert "Alpine router: no answer (ReadTimeout)" in alerts_text(r)


def test_alpine_router_second_attempt_slightly_lower(web, store):
    web.short_ors, web.brouter = True, "second try"
    r = planner.plan_hikes(SASSIERE[0], "out_and_back", SASSIERE[1], level="Experienced", hours=8, day_index=1)
    t = r["tracks"][0]
    assert t["router"] == "brouter" and 0.3 < t["end_gap_km"] < 0.5
    assert "stops before your destination" in alerts_text(r)


def test_one_way_trip_is_not_doubled(web, store):
    web.short_ors = True
    t = planner.plan_hikes(SASSIERE[0], "one_way", SASSIERE[1], level="Intermediate", hours=6, day_index=1)["tracks"][0]
    assert t["distance_km"] == 2.4 and t["descent_m"] == 0 and not t["loop"] and t["far_label"] == "End"


# ------------------------------------------------------------- ideas from the map
SQUARE = [(45.9237, 6.8694), (45.9237, 6.8894), (45.9337, 6.8894), (45.9337, 6.8694)]  # 4.6 km from HOME


def _geom(a, b):
    return [{"lat": a[0] + (b[0] - a[0]) * i / 5, "lon": a[1] + (b[1] - a[1]) * i / 5} for i in range(6)]


MAP = [
    node(1, 45.9437, 6.8694, natural="peak", name="Tête du Test", ele="1840"),  # 2.2 km north, +800 m
    node(2, 45.9287, 6.8794, tourism="alpine_hut", name="Refuge <b>Proche</b>"),  # close, +200 m
    node(3, 45.9237, 6.9900, natural="peak", name="Aiguille Falaise", ele="1100"),  # router stops short
    node(4, 46.2000, 6.8694, natural="peak", name="Trop Loin", ele="4000"),  # out of reach
    node(5, 45.9300, 6.8700, natural="peak"),  # unnamed: ignored
    {
        "type": "way",
        "id": 6,
        "center": {"lat": 45.9317, "lon": 6.8594},
        "tags": {"natural": "water", "name": "Lac Bleu"},
    },
    {
        "type": "way",
        "id": 7,
        "center": {"lat": 45.9250, "lon": 6.8700},
        "tags": {"natural": "water", "water": "river", "name": "L'Arve"},
    },
    {"type": "relation", "id": 900, "tags": {"route": "hiking", "name": "GR Tour du Test", "network": "nwn"}},
    {"type": "relation", "id": 901, "tags": {"route": "hiking", "name": "Boucle du Carré", "network": "lwn"}},
    {
        "type": "relation",
        "id": 901,
        "tags": {"route": "hiking", "name": "Boucle du Carré", "network": "lwn"},
        "members": [
            {"type": "way", "ref": 11, "role": "", "geometry": _geom(SQUARE[1], SQUARE[2])},
            {"type": "way", "ref": 12, "role": "", "geometry": _geom(SQUARE[0], SQUARE[1])},
            {"type": "way", "ref": 13, "role": "", "geometry": _geom(SQUARE[3], SQUARE[2])},
            {"type": "way", "ref": 14, "role": "alternative", "geometry": _geom(SQUARE[0], (45.90, 6.80))},
            {"type": "way", "ref": 15, "role": "", "geometry": _geom(SQUARE[3], SQUARE[0])},
        ],
    },
]


def test_goals_and_trails_are_read_from_overpass(web):
    web.overpass = MAP
    goals, trails = osm.find_goals_and_trails(HOME[0], HOME[1], 5000)
    assert {g["name"] for g in goals} == {
        "Tête du Test",
        "Refuge <b>Proche</b>",
        "Aiguille Falaise",
        "Trop Loin",
        "Lac Bleu",
    }
    assert next(g["ele"] for g in goals if g["kind"] == "summit") == 1840.0
    assert [len(t["ways"]) for t in trails] == [0, 4]


def test_ideas_from_real_goals_and_a_waymarked_loop(web, store):
    web.overpass, web.grid = MAP, True
    r = planner.plan_hikes("45.9237, 6.8694", "ideas", level="Intermediate", hours=4, day_index=1)
    titles = [t["title"] for t in r["tracks"]]
    assert r["ideas"] and not r["note"]
    assert {"Tête du Test (summit)", "Lac Bleu (lake)", "Boucle du Carré (marked loop)"} <= set(titles)
    assert not any("Falaise" in x or "Trop Loin" in x or "Arve" in x for x in titles)
    peak = next(t for t in r["tracks"] if "Tête" in t["title"])
    assert peak["distance_km"] == pytest.approx(4.4, abs=0.2) and peak["ascent_m"] == pytest.approx(800, abs=20)
    assert peak["far_label"] == "Tête du Test"
    loop = next(t for t in r["tracks"] if "Carré" in t["title"])
    assert loop["distance_km"] == pytest.approx(5.3, abs=0.4) and loop["kind"] == "Waymarked loop"
    assert r["recommended"] is not None and len(r["tracks"]) <= 4
    assert web.count("OVERPASS") == 1 and web.count("POST") <= 7
    data = planner.plan_data(r)
    assert {
        "id": 900,
        "name": "GR Tour du Test",
        "network": "national",
        "url": "https://hiking.waymarkedtrails.org/#route?id=900",
    } in data["trails"]


def test_a_big_climb_is_not_offered_for_a_short_beginner_outing(web, store):
    web.overpass, web.grid = MAP, True
    r = planner.plan_hikes("45.9237, 6.8694", "ideas", level="Beginner", hours=2, day_index=1)
    assert not any("Tête" in t["title"] for t in r["tracks"])


def test_nothing_on_the_map_gives_labelled_computed_loops(web, store):
    web.overpass = []
    r = planner.plan_hikes("45.9237, 6.8694", "ideas", level="Beginner", hours=3, day_index=1)
    assert "computed loops, not known hikes" in r["note"] and len(r["tracks"]) == 3
    assert r["tracks"][0]["kind"] == "Computed loop"


def test_requested_lake_out_of_reach_is_named(web, store):
    web.grid = True
    web.overpass = [
        node(1, 45.9437, 6.8694, natural="peak", name="Tête du Test", ele="1840"),
        {
            "type": "way",
            "id": 9,
            "center": {"lat": 46.0300, "lon": 6.8694},
            "tags": {"natural": "water", "name": "Lac Lointain"},
        },
    ]
    r = planner.plan_hikes("45.9237, 6.8694", "ideas", level="Beginner", hours=2, kinds={"lake"})
    assert "Lakes nearby are too far for 2 h at beginner level: Lac Lointain (11.8 km away)" in r["hint"]


# ------------------------------------------------------------ destination only
CAR_PARKS = [
    node(40, 45.9420, 6.8694, amenity="parking", name="Parking du Sommet"),  # 0.19 km: too close, dropped
    node(41, 45.9380, 6.8694, amenity="parking", name="Parking Haut"),  # 0.6 km
    node(42, 45.9382, 6.8696, amenity="parking", name="Parking Haut bis"),  # same hike as 41
    node(43, 45.9300, 6.8694, amenity="parking", name="Parking du Milieu"),  # 1.5 km
    node(44, 45.9150, 6.8694, highway="trailhead", name="Départ du Bas"),  # 3.2 km
    node(45, 45.9436, 6.8695, amenity="parking", name="Parking au sommet"),  # on the goal: dropped
]


def test_destination_only_gives_easiest_medium_hardest_from_car_parks(web, store):
    web.overpass, web.grid = CAR_PARKS, True
    r = planner.plan_hikes("", "out_and_back", "45.9437, 6.8694", level="Intermediate", hours=5)
    data = planner.plan_data(r)
    names = [(h["title"], h["kind"]) for h in data["hikes"]]
    assert data["auto_start"] and len(names) == 3
    assert names[0][0].startswith("From Parking Haut") and sum("Parking Haut" in n for n, _ in names) == 1
    assert names[0][1].startswith("Easiest, there and back to") and names[2][1].startswith("Hardest")
    dists = [h["distance_km"] for h in data["hikes"]]
    assert dists == sorted(dists) and len(set(dists)) == 3
    assert all(h["start_point"] for h in data["hikes"])
    assert not any("sommet" in n.lower() for n, _ in names)
    assert data["start"]["label"] == data["hikes"][0]["start_point"]["label"]


def test_destination_only_without_car_parks(web, store):
    web.overpass, web.grid = [], True
    with pytest.raises(errors.PlannerError, match="No car park"):
        planner.plan_hikes("", "out_and_back", "45.9437, 6.8694")


# --------------------------------------------------------------- community link
def test_nearby_shared_hikes_are_listed(web, store):
    track = geo.build_track([[6.8694 + i * 0.001, 45.9237, 1000] for i in range(20)])
    community.share_track(track, "Shared", "", "Yes", "No", "planned")
    r = planner.plan_hikes("Chamonix", "ideas", day_index=1)
    assert [h["title"] for h in r["nearby"]] == ["Shared"]
