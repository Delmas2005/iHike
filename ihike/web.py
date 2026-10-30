"""The iHike web server.

/                 the iHike interface (static/index.html + static/app.js)
/api/...          JSON endpoints used by that interface
/photos/<file>    community photos
"""

from __future__ import annotations

import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NoReturn

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, ai, community, planner
from .errors import PlannerError
from .geo import build_track, in_europe
from .gpx import MAX_GPX_BYTES, parse_gpx
from .osm import find_trailheads

STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_AUDIO_BYTES = 6 * 1024 * 1024


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    community.init_storage()  # folders, and the community dataset when configured
    yield


api = FastAPI(title="iHike", version=__version__, docs_url="/api/docs", redoc_url=None, lifespan=_lifespan)


def _fail(exc: Exception) -> NoReturn:
    """Turn planning errors into clean HTTP answers the interface can show."""
    if isinstance(exc, PlannerError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
def config() -> dict:
    return {**planner.config_status(), **ai.available()}


@api.post("/api/plan")
def plan(req: PlanRequest) -> dict:
    try:
        result = planner.plan_hikes(
            req.start,
            req.mode,
            req.destination,
            req.activity,
            req.level,
            req.hours,
            req.day,
            kinds=set(req.kinds) or None,
        )
    except Exception as exc:
        _fail(exc)
    return planner.plan_data(result)


@api.get("/api/trailheads")
def trailheads(lat: float, lon: float) -> dict:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or not in_europe(lat, lon):
        raise HTTPException(status_code=400, detail="This point is outside Europe.")
    places = find_trailheads(lat, lon)
    if places is None:
        raise HTTPException(status_code=503, detail="OpenStreetMap could not be searched just now.")
    return {"places": places}


class AskRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)


@api.post("/api/ask")
def ask(req: AskRequest) -> dict:
    return ai.understand(req.text)


@api.post("/api/voice")
def voice(audio: UploadFile = File(...)) -> dict:
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
        except Exception as exc:
            print("Voice error:", exc)
            raise HTTPException(status_code=500, detail="The recording could not be understood.") from exc
    return {"text": text}


# --------------------------------------------------------------------------
# Community
# --------------------------------------------------------------------------
@api.get("/api/hikes")
def hikes() -> dict:
    return {"hikes": [community.public_hike(h) for h in reversed(community.load_hikes())]}


@api.post("/api/photos")
def photos(image: UploadFile = File(...)) -> dict:
    raw = image.file.read(community.MAX_PHOTO_BYTES + 1)
    try:
        photo_id, img = community.save_photo(raw)
    except PlannerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = {"verdict": "unavailable", "species": []}
    if ai.available()["species"]:
        try:
            result = ai.identify(img)
        except Exception as exc:
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
    photos: list[PhotoRef] = Field(default_factory=list, max_length=community.MAX_PHOTOS_PER_HIKE)


@api.post("/api/share")
def share(req: ShareRequest) -> dict:
    try:
        track = community.track_from_geometry(req.geometry)
        record = community.share_track(
            track,
            req.title,
            req.notes,
            req.dog,
            req.camping,
            "planned",
            [p.model_dump() for p in req.photos],
            sac=req.sac,
        )
    except PlannerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return community.public_hike(record)


@api.post("/api/import")
def import_gpx(
    gpx: UploadFile = File(...),
    title: str = Form(""),
    notes: str = Form(""),
    dog: str = Form("Unknown"),
    camping: str = Form("Unknown"),
    photos: str = Form("[]"),
) -> dict:
    import json

    raw = gpx.file.read(MAX_GPX_BYTES + 1)
    if len(raw) > MAX_GPX_BYTES:
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
            track = build_track(parse_gpx(tmp.name))
            if not in_europe(*track["latlon"][0]):
                raise PlannerError("This track starts outside Europe, which is the only area covered for now.")
            record = community.share_track(track, title[:80], notes[:500], dog, camping, "gpx", refs)
        except PlannerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return community.public_hike(record)


@api.get("/photos/{name}")
def photo_file(name: str) -> FileResponse:
    path = community.photo_path(name)
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
def index() -> Response:
    if not (STATIC / "index.html").exists():
        return HTMLResponse(MISSING_STATIC, status_code=503)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


if STATIC.is_dir():
    api.mount("/static", StaticFiles(directory=STATIC), name="static")
else:  # never crash the whole Space over a missing folder: say what is wrong instead
    print("WARNING: the static/ folder is missing - upload it next to app.py. Serving a setup page at /.")
app = api
