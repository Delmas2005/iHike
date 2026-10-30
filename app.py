"""iHike - hike ideas from the map, anywhere in Europe (Hugging Face Space).

This file is the web server:
  /                 the iHike interface (static/index.html + static/app.js)
  /api/...          JSON endpoints used by that interface
  /photos/<file>    community photos

The planning logic lives in engine.py, the in-Space AI models in ai.py.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import ai
import engine
from engine import PlannerError

STATIC = Path(__file__).parent / "static"
MAX_AUDIO_BYTES = 6 * 1024 * 1024

api = FastAPI(title="iHike", docs_url="/api/docs", redoc_url=None)


def _fail(exc: Exception):
    """Turn engine errors into clean HTTP answers the interface can show."""
    if isinstance(exc, PlannerError):
        raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, requests.RequestException):
        raise HTTPException(status_code=503, detail="A map service did not answer. Please try again in a minute.")
    raise exc


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
class PlanRequest(BaseModel):
    start: str = Field("", max_length=200)  # may be empty with a destination: iHike picks car parks
    mode: str = "ideas"
    destination: str = Field("", max_length=200)
    activity: str = "Hiking"
    level: str = "Beginner"
    hours: float = 3.0
    day: int = 0
    kinds: list[str] = Field(default_factory=list, max_length=8)


@api.get("/api/config")
def config():
    return {**engine.config_status(), **ai.available()}


@api.post("/api/plan")
def plan(req: PlanRequest):
    try:
        result = engine.plan_hikes(req.start, req.mode, req.destination, req.activity, req.level,
                                   req.hours, req.day, kinds=set(req.kinds) or None)
    except Exception as exc:  # noqa: BLE001 - mapped to HTTP errors
        _fail(exc)
    return engine.plan_data(result)


@api.get("/api/trailheads")
def trailheads(lat: float, lon: float):
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not engine.in_europe(lat, lon):
        raise HTTPException(status_code=400, detail="This point is outside Europe.")
    places = engine.find_trailheads(lat, lon)
    if places is None:
        raise HTTPException(status_code=503, detail="OpenStreetMap could not be searched just now.")
    return {"places": places}


class AskRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


@api.post("/api/ask")
def ask(req: AskRequest):
    return ai.understand(req.text)


@api.post("/api/voice")
def voice(audio: UploadFile = File(...)):
    if not ai.available()["voice"]:
        raise HTTPException(status_code=501, detail="Voice input is not installed on this server.")
    raw = audio.file.read(MAX_AUDIO_BYTES + 1)
    if len(raw) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="This recording is too long.")
    suffix = Path(audio.filename or "voice.webm").suffix[:8] or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(raw)
        tmp.flush()
        try:
            text = ai.transcribe(tmp.name)
        except Exception as exc:  # noqa: BLE001
            print("Voice error:", exc)
            raise HTTPException(status_code=500, detail="The recording could not be understood.")
    return {"text": text}


# --------------------------------------------------------------------------
# Community
# --------------------------------------------------------------------------
@api.get("/api/hikes")
def hikes():
    return {"hikes": [engine.public_hike(h) for h in reversed(engine.load_hikes())]}


@api.post("/api/photos")
def photos(image: UploadFile = File(...)):
    raw = image.file.read(engine.MAX_PHOTO_BYTES + 1)
    try:
        photo_id, img = engine.save_photo(raw)
    except PlannerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result = {"verdict": "unavailable", "species": []}
    if ai.available()["species"]:
        try:
            result = ai.identify(img)
        except Exception as exc:  # noqa: BLE001 - a failed recognition must not lose the photo
            print("Species error:", exc)
    return {"id": photo_id, "url": f"/photos/{photo_id}.jpg", "thumb": f"/photos/{photo_id}_s.jpg", **result}


class PhotoRef(BaseModel):
    id: str
    species: list[dict] = Field(default_factory=list, max_length=3)


class ShareRequest(BaseModel):
    geometry: list[list[float | None]] = Field(..., max_length=5000)
    title: str = Field("", max_length=80)
    notes: str = Field("", max_length=500)
    dog: str = "Unknown"
    camping: str = "Unknown"
    sac: int = 0
    photos: list[PhotoRef] = Field(default_factory=list, max_length=engine.MAX_PHOTOS_PER_HIKE)


@api.post("/api/share")
def share(req: ShareRequest):
    try:
        track = engine.track_from_geometry(req.geometry)
        record = engine.share_track(track, req.title, req.notes, req.dog, req.camping, "planned",
                                    [p.model_dump() for p in req.photos], sac=req.sac)
    except PlannerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return engine.public_hike(record)


@api.post("/api/import")
def import_gpx(gpx: UploadFile = File(...), title: str = Form(""), notes: str = Form(""),
               dog: str = Form("Unknown"), camping: str = Form("Unknown"), photos: str = Form("[]")):
    import json

    raw = gpx.file.read(engine.MAX_GPX_BYTES + 1)
    if len(raw) > engine.MAX_GPX_BYTES:
        raise HTTPException(status_code=413, detail="This GPX file is larger than 5 MB.")
    try:
        refs = json.loads(photos or "[]")
        refs = refs if isinstance(refs, list) else []
    except json.JSONDecodeError:
        refs = []
    with tempfile.NamedTemporaryFile(suffix=".gpx") as tmp:
        tmp.write(raw)
        tmp.flush()
        try:
            track = engine.build_track(engine.parse_gpx(tmp.name))
            if not engine.in_europe(*track["latlon"][0]):
                raise PlannerError("This track starts outside Europe, which is the only area covered for now.")
            record = engine.share_track(track, title[:80], notes[:500], dog, camping, "gpx", refs)
        except PlannerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return engine.public_hike(record)


@api.get("/photos/{name}")
def photo_file(name: str):
    path = engine.photo_path(name)
    if not path:
        raise HTTPException(status_code=404, detail="No such photo.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
MISSING_STATIC = """<!doctype html><meta charset="utf-8"><title>iHike - setup</title>
<body style="font-family:system-ui;max-width:640px;margin:60px auto;padding:0 16px;line-height:1.5">
<h1>The interface files are missing</h1>
<p>The Space has no <code>static/</code> folder, or it is incomplete. Upload the whole <code>static</code> folder
(with <code>index.html</code>, <code>app.js</code>, <code>styles.css</code> and <code>vendor/</code> inside) next to
<code>app.py</code>.</p></body>"""


@api.get("/", include_in_schema=False)
def index():
    if not (STATIC / "index.html").exists():
        return HTMLResponse(MISSING_STATIC, status_code=503)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


if STATIC.is_dir():
    api.mount("/static", StaticFiles(directory=STATIC), name="static")
else:  # never crash the whole Space over a missing folder: say what is wrong instead
    print("WARNING: the static/ folder is missing - upload it next to app.py. Serving a setup page at /.")
app = api

if __name__ == "__main__":
    # Hugging Face sends visitors to port 7860 (GRADIO_SERVER_PORT on Gradio Spaces).
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("GRADIO_SERVER_PORT", 7860)))
