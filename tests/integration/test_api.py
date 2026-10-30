"""The HTTP API, through FastAPI's TestClient, with the engine on simulated services."""
from __future__ import annotations

import io

import pytest
from fakes import node
from PIL import Image

import ai


def test_interface_and_static_files_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "<title>iHike" in page.text and "/static/app.js" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/vendor/maplibre-gl.js").status_code == 200


def test_config_reports_features(client):
    cfg = client.get("/api/config").json()
    assert set(cfg) >= {"routing", "ai_briefing", "persistence", "voice", "species"}
    assert cfg["routing"] is True and cfg["ai_briefing"] is False and cfg["persistence"] is False


def test_plan_ideas_json_shape(client, web):
    web.grid = True
    web.overpass = [node(1, 45.9437, 6.8694, natural="peak", name="Tête du Test", ele="1840"),
                    {"type": "way", "id": 6, "center": {"lat": 45.9317, "lon": 6.8594},
                     "tags": {"natural": "water", "name": "Lac Bleu"}}]
    r = client.post("/api/plan", json={"start": "45.9237, 6.8694", "mode": "ideas", "level": "Intermediate",
                                       "hours": 4, "day": 1})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ideas"] and len(d["hikes"]) == 2 and d["start"]["label"].startswith("45.92370")
    hike = d["hikes"][0]
    assert set(hike) >= {"id", "title", "kind", "color", "geometry", "sac", "grade", "suits", "recommended", "far"}
    assert len(hike["geometry"][0]) == 3 and hike["geometry"][0][0] == pytest.approx(6.8694, abs=1e-3)
    assert all({"severity", "title", "detail"} <= set(a) for a in d["alerts"])
    assert d["briefing"]["text"] and d["briefing"]["ai"] is False and "**" not in d["briefing"]["text"]
    assert d["weather"]["where"].startswith("near the highest point")


def test_plan_ideas_filtered_by_kind(client, web):
    web.grid = True
    web.overpass = [node(1, 45.9437, 6.8694, natural="peak", name="Tête du Test", ele="1840"),
                    {"type": "way", "id": 6, "center": {"lat": 45.9317, "lon": 6.8594},
                     "tags": {"natural": "water", "name": "Lac Bleu"}}]
    r = client.post("/api/plan", json={"start": "45.9237, 6.8694", "mode": "ideas", "level": "Intermediate",
                                       "hours": 4, "kinds": ["lake"]})
    assert [(h["title"], h["kind"]) for h in r.json()["hikes"]] == [("Lac Bleu", "Out and back to a lake")]


def test_plan_alpine_destination_json(client, web):
    web.short_ors, web.brouter = True, True
    r = client.post("/api/plan", json={"start": "45.4885, 6.968", "mode": "out_and_back",
                                       "destination": "45.50472, 6.99972", "level": "Beginner", "hours": 6})
    d = r.json()
    assert r.status_code == 200 and d["no_match"] and d["hikes"][0]["sac"] == 5
    assert d["hikes"][0]["title"] == "45.50472" and d["hikes"][0]["kind"] == "There and back"
    assert any(a["severity"] == "danger" and a["title"].startswith("Alpine terrain") for a in d["alerts"])


@pytest.mark.parametrize("body,status,message", [
    ({"start": "40.7, -74.0", "mode": "ideas"}, 400, "outside Europe"),
    ({"start": "Chamonix", "mode": "teleport"}, 400, "Unknown trip type"),
    ({"start": "", "mode": "ideas"}, 400, "Please enter"),
    ({"start": "x" * 500}, 422, None),
])
def test_plan_errors(client, web, body, status, message):
    r = client.post("/api/plan", json=body)
    assert r.status_code == status
    if message:
        assert message in r.json()["detail"]


@pytest.mark.parametrize("text,expected", [
    ("Une boucle de 3h près d'Annecy avec un lac, demain", {"start": "Annecy", "hours": 3.0}),
    ("lac de crop", {"destination": "Lac de Crop", "mode": "out_and_back"}),
    ("une boucle avec un lac", {"mode": "ideas", "kinds": ["lake", "loop"]}),
    ("Lac de Crop depuis Prabert", {"start": "Prabert", "destination": "Lac de Crop"}),
])
def test_ask(client, text, expected):
    r = client.post("/api/ask", json={"text": text}).json()
    assert r["source"] == "rules"
    assert all(r["params"].get(k) == v for k, v in expected.items()), r


def test_ask_does_not_turn_a_wish_into_a_destination(client):
    assert "destination" not in client.post("/api/ask", json={"text": "un lac"}).json()["params"]


def test_trailheads(client, web):
    web.overpass = [
        node(30, 45.9300, 6.8700, amenity="parking", name="Parking du Lac", fee="no", capacity="40"),
        node(31, 45.9250, 6.8690, amenity="parking", access="private", name="Hotel guests"),
        {"type": "way", "id": 32, "center": {"lat": 45.9240, "lon": 6.8695},
         "tags": {"amenity": "parking", "parking": "underground"}},
        node(33, 45.9400, 6.8800, highway="trailhead", name="Départ Sentier des Crêtes"),
        node(34, 45.9238, 6.8696, amenity="parking"),
        node(35, 45.92381, 6.86961, amenity="parking"),  # duplicate of 34
    ]
    r = client.get("/api/trailheads", params={"lat": 45.9237, "lon": 6.8694})
    assert r.status_code == 200
    places = r.json()["places"]
    assert [p["name"] for p in places] == ["Départ Sentier des Crêtes", "Parking du Lac", "Car park"]
    assert places[1]["fee"] is False and places[1]["capacity"] == 40 and isinstance(places[0]["ele"], int)


def test_trailheads_errors(client, web):
    web.overpass = None
    assert client.get("/api/trailheads", params={"lat": 45.9237, "lon": 6.8694}).status_code == 503
    assert client.get("/api/trailheads", params={"lat": 40.7, "lon": -74}).status_code == 400


# ------------------------------------------------------------------ photos
def _jpeg_with_gps() -> bytes:
    img = Image.new("RGB", (2400, 1600), (90, 140, 60))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"
    exif[0x8825] = {2: (45.0, 55.0, 12.0)}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


EDELWEISS = {"verdict": "match", "species": [{"name": "Edelweiss", "scientific": "Leontopodium nivale",
                                              "group": "flower", "score": 0.81}]}


def test_photo_metadata_is_removed_and_species_returned(client, store, monkeypatch):
    raw = _jpeg_with_gps()
    assert b"SecretCam" in raw
    monkeypatch.setattr(ai, "available", lambda: {"voice": False, "species": True})
    monkeypatch.setattr(ai, "identify", lambda im: EDELWEISS)
    r = client.post("/api/photos", files={"image": ("p.jpg", raw, "image/jpeg")})
    assert r.status_code == 200
    photo = r.json()
    assert photo["verdict"] == "match" and photo["species"][0]["name"] == "Edelweiss" and len(photo["id"]) == 16
    stored = (store.PENDING_DIR / f"{photo['id']}.jpg").read_bytes()
    saved = Image.open(io.BytesIO(stored))
    assert b"SecretCam" not in stored and saved.getexif() == {} and max(saved.size) == 1600
    assert client.get(f"/photos/{photo['id']}_s.jpg").status_code == 200


def test_a_failing_species_model_does_not_lose_the_photo(client, monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: {"voice": False, "species": True})

    def broken(image):
        raise RuntimeError("model down")

    monkeypatch.setattr(ai, "identify", broken)
    r = client.post("/api/photos", files={"image": ("p.jpg", _jpeg_with_gps(), "image/jpeg")})
    assert r.status_code == 200 and r.json()["verdict"] == "unavailable"


def test_not_an_image(client):
    assert client.post("/api/photos", files={"image": ("x.jpg", b"not an image", "image/jpeg")}).status_code == 400


@pytest.mark.parametrize("name", ["../hikes.jsonl", "..%2Fhikes.jsonl", "0123456789abcdef.png", "hikes.jsonl"])
def test_photo_paths_cannot_escape_the_folder(client, name):
    assert client.get(f"/photos/{name}").status_code == 404


# ------------------------------------------------------------------ sharing
GEOMETRY = [[6.8694 + i * 0.001, 45.9237 + i * 0.0005, 1000 + i * 10] for i in range(40)]


def test_share_a_route_with_a_photo(client, store, monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: {"voice": False, "species": True})
    monkeypatch.setattr(ai, "identify", lambda im: EDELWEISS)
    photo = client.post("/api/photos", files={"image": ("p.jpg", _jpeg_with_gps(), "image/jpeg")}).json()
    r = client.post("/api/share", json={
        "geometry": GEOMETRY, "title": "Test <script>", "notes": "n", "dog": "Yes", "camping": "Maybe", "sac": 3,
        "photos": [{"id": photo["id"], "species": photo["species"]}, {"id": "../../etc", "species": []}]})
    assert r.status_code == 200
    rec = r.json()
    assert rec["title"] == "Test <script>" and rec["dog"] == "Yes" and rec["camping"] == "Unknown"
    assert rec["species"] == ["Edelweiss"] and len(rec["photos"]) == 1
    assert rec["photos"][0]["url"] == f"/photos/{photo['id']}.jpg"
    assert (store.PHOTOS_DIR / f"{photo['id']}.jpg").exists()
    assert not (store.PENDING_DIR / f"{photo['id']}.jpg").exists()
    assert client.get(rec["photos"][0]["url"]).status_code == 200


@pytest.mark.parametrize("geometry,status", [
    ([[1, 2]], 400),                                   # one point
    ([[-74, 40.7], [-74.01, 40.71]], 400),             # outside Europe
    ([["a", "b"], [1, 2]], 422),                       # not numbers
])
def test_share_rejects_bad_routes(client, geometry, status):
    assert client.post("/api/share", json={"geometry": geometry}).status_code == status


def test_import_gpx_and_list(client, store, tmp_path):
    track = store.build_track(GEOMETRY)
    track["name"] = "A · Test"
    gpx = open(store.write_gpx(track, tmp_path), "rb").read()
    r = client.post("/api/import", files={"gpx": ("t.gpx", gpx, "application/gpx+xml")},
                    data={"title": "Imported", "notes": "", "dog": "No", "camping": "No", "photos": "not json"})
    assert r.status_code == 200 and r.json()["title"] == "Imported"
    bad = client.post("/api/import", files={"gpx": ("t.gpx", b"<gpx></gpx>", "application/gpx+xml")})
    assert bad.status_code == 400
    listing = client.get("/api/hikes").json()["hikes"]
    assert [h["title"] for h in listing] == ["Imported"] and "path" in listing[0]


# -------------------------------------------------------------------- voice
def test_voice_unavailable(client, monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: {"voice": False, "species": False})
    assert client.post("/api/voice", files={"audio": ("v.webm", b"123", "audio/webm")}).status_code == 501


def test_voice_transcribed(client, monkeypatch):
    monkeypatch.setattr(ai, "available", lambda: {"voice": True, "species": False})
    monkeypatch.setattr(ai, "transcribe", lambda path: "A two hour walk near Grenoble")
    r = client.post("/api/voice", files={"audio": ("v.webm", b"123", "audio/webm")})
    assert r.status_code == 200 and r.json()["text"] == "A two hour walk near Grenoble"
