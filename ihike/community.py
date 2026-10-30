from __future__ import annotations

import json
import math
import re
import shutil
import threading
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path

from .config import HF_TOKEN, HIKES_DATASET
from .errors import PlannerError
from .geo import build_track, decimate, haversine_km, in_europe


DATA_DIR = Path("data")
HIKES_FILE = DATA_DIR / "hikes.jsonl"
PHOTOS_DIR = DATA_DIR / "photos"  # published with a shared hike (synced to the dataset)
PENDING_DIR = Path("pending_photos")  # uploaded but not yet shared (never synced)
_local_lock = threading.Lock()
scheduler = None
persistence_error = ""


def ensure_folders() -> None:
    for folder in (DATA_DIR, PHOTOS_DIR, PENDING_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def init_storage() -> None:
    """Create the folders and, when configured, restore and sync the community dataset.

    Called once when the web server starts (it used to run at import time).
    """
    global scheduler, persistence_error
    ensure_folders()
    if not (HF_TOKEN and HIKES_DATASET) or scheduler is not None:
        return
    try:
        import tempfile

        from huggingface_hub import CommitScheduler, snapshot_download

        try:  # restore what previous runs of the Space saved (hikes and their photos)
            with tempfile.TemporaryDirectory() as tmp:
                snapshot_download(
                    HIKES_DATASET,
                    repo_type="dataset",
                    token=HF_TOKEN,
                    local_dir=tmp,
                    allow_patterns=["hikes.jsonl", "photos/*"],
                )
                if (Path(tmp) / "hikes.jsonl").exists():
                    shutil.copy(Path(tmp) / "hikes.jsonl", HIKES_FILE)
                for photo in (Path(tmp) / "photos").glob("*.jpg"):
                    shutil.copy(photo, PHOTOS_DIR / photo.name)
        except Exception as exc:
            print("Nothing restored:", exc)  # first run: the dataset does not exist yet
        scheduler = CommitScheduler(
            repo_id=HIKES_DATASET,
            repo_type="dataset",
            folder_path=DATA_DIR,
            every=5,
            token=HF_TOKEN,
            private=True,
        )
    except Exception as exc:  # the app must still start without persistence
        persistence_error = f"{type(exc).__name__}: {exc}".splitlines()[0][:200]
        print("Persistence disabled:", exc)


def load_hikes() -> list[dict]:
    if not HIKES_FILE.exists():
        return []
    hikes = []
    for line in HIKES_FILE.read_text(encoding="utf-8").splitlines():
        try:
            hikes.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return hikes


def save_hike(record: dict) -> None:
    ensure_folders()
    lock = scheduler.lock if scheduler else _local_lock
    with lock:
        with HIKES_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def nearby_hikes(lat, lon, radius_km=30, limit=5) -> list[dict]:
    found = []
    for h in load_hikes():
        try:
            d = haversine_km((lat, lon), h["start"])
        except Exception:
            continue
        if d <= radius_km:
            found.append({**h, "away_km": round(d, 1)})
    return sorted(found, key=lambda h: h["away_km"])[:limit]


def _record(track, title, notes, dog, camping, source) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "source": source,
        "title": (title or "").strip()[:80] or "Untitled hike",
        "notes": (notes or "").strip()[:500],
        "dog": dog,
        "camping": camping,
        "distance_km": track["distance_km"],
        "ascent_m": track["ascent_m"],
        "grade": track["grade"],
        "loop": track["loop"],
        "start": [round(track["latlon"][0][0], 5), round(track["latlon"][0][1], 5)],
        "path": [[round(lat, 5), round(lon, 5)] for lat, lon in decimate(track["latlon"], 300)],
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


MAX_PHOTO_BYTES = 12 * 1024 * 1024
MAX_PHOTOS_PER_HIKE = 6
_PHOTO_ID = re.compile(r"^[0-9a-f]{16}$")


def save_photo(raw: bytes):
    """Store an uploaded picture as a clean JPEG. Returns (photo_id, PIL image) or raises PlannerError.

    Re-encoding drops all metadata (camera, exact GPS position, owner) before anything is published.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(raw) > MAX_PHOTO_BYTES:
        raise PlannerError("This photo is larger than 12 MB.")
    try:
        import io

        img = Image.open(io.BytesIO(raw))
        if img.width * img.height > 60_000_000:
            raise PlannerError("This photo has too many pixels.")
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise PlannerError("This file is not a readable image.")
    photo_id = uuid.uuid4().hex[:16]
    big = img.copy()
    big.thumbnail((1600, 1600))
    ensure_folders()
    big.save(PENDING_DIR / f"{photo_id}.jpg", "JPEG", quality=85, optimize=True)
    small = img.copy()
    small.thumbnail((480, 480))
    small.save(PENDING_DIR / f"{photo_id}_s.jpg", "JPEG", quality=80, optimize=True)
    return photo_id, img


def photo_path(name: str):
    """Path of a stored photo file ('<id>.jpg' or '<id>_s.jpg'), or None."""
    m = re.fullmatch(r"([0-9a-f]{16})(_s)?\.jpg", name or "")
    if not m:
        return None
    for folder in (PHOTOS_DIR, PENDING_DIR):
        if (folder / name).exists():
            return folder / name
    return None


def publish_photos(photos) -> list[dict]:
    """Move uploaded photos from pending to the published folder. `photos` = [{id, species}]."""
    ensure_folders()
    kept = []
    for p in (photos or [])[:MAX_PHOTOS_PER_HIKE]:
        pid = str((p or {}).get("id", ""))
        if not _PHOTO_ID.match(pid):
            continue
        moved = False
        lock = scheduler.lock if scheduler else _local_lock
        with lock:
            for suffix in (".jpg", "_s.jpg"):
                src, dst = PENDING_DIR / f"{pid}{suffix}", PHOTOS_DIR / f"{pid}{suffix}"
                if src.exists():
                    shutil.move(src, dst)
                    moved = True
                elif dst.exists():
                    moved = True
        if moved:
            species = [
                {
                    "name": str(x.get("name", ""))[:60],
                    "scientific": str(x.get("scientific", ""))[:60],
                    "score": round(float(x.get("score", 0)), 2),
                }
                for x in (p.get("species") or [])[:3]
                if isinstance(x, dict)
            ]
            kept.append({"id": pid, "species": species})
    return kept


def track_from_geometry(geometry) -> dict:
    """Validate [[lon, lat, ele?], ...] sent by a browser and rebuild its statistics server-side."""
    if not isinstance(geometry, list) or not 2 <= len(geometry) <= 5000:
        raise PlannerError("The route is missing or too long.")
    coords = []
    for p in geometry:
        try:
            lon, lat = float(p[0]), float(p[1])
            ele = float(p[2]) if len(p) > 2 and p[2] is not None else None
        except (TypeError, ValueError, IndexError):
            raise PlannerError("The route contains invalid points.")
        if not (math.isfinite(lon) and math.isfinite(lat) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise PlannerError("The route contains invalid points.")
        if ele is not None and not (math.isfinite(ele) and -500 < ele < 9000):
            ele = None
        coords.append([lon, lat, ele])
    if any(c[2] is None for c in coords):
        coords = [c[:2] for c in coords]
    track = build_track(coords)
    if not in_europe(*track["latlon"][0]):
        raise PlannerError("This route starts outside Europe, which is the only area covered for now.")
    return track


def share_track(track, title, notes, dog, camping, source, photos=None, sac=None) -> dict:
    record = _record(
        track,
        title,
        notes,
        dog if dog in ("Yes", "No") else "Unknown",
        camping if camping in ("Yes", "No") else "Unknown",
        source,
    )
    record["max_ele"] = track["max_ele"]
    if sac:
        record["sac"] = int(sac) if str(sac).isdigit() and 0 < int(sac) <= 6 else 0
    record["photos"] = publish_photos(photos)
    save_hike(record)
    return record


def public_hike(h: dict, with_path=True) -> dict:
    """A stored hike as sent to browsers: only known fields, photos as URLs."""
    out = {
        k: h.get(k)
        for k in (
            "id",
            "title",
            "notes",
            "dog",
            "camping",
            "distance_km",
            "ascent_m",
            "grade",
            "loop",
            "start",
            "created",
            "source",
            "max_ele",
        )
    }
    if with_path:
        out["path"] = h.get("path") or []
    photos = []
    for p in h.get("photos") or []:
        if isinstance(p, dict) and _PHOTO_ID.match(str(p.get("id", ""))):
            photos.append(
                {
                    "url": f"/photos/{p['id']}.jpg",
                    "thumb": f"/photos/{p['id']}_s.jpg",
                    "species": p.get("species") or [],
                }
            )
    out["photos"] = photos
    seen = []
    for p in photos:
        for sp in p["species"][:1]:
            if sp.get("name") and sp["name"] not in seen:
                seen.append(sp["name"])
    out["species"] = seen
    return out
