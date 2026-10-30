---
title: iHike
emoji: ⛰️
colorFrom: green
colorTo: yellow
sdk: gradio
sdk_version: 5.49.1
app_file: app.py
pinned: false
license: apache-2.0
short_description: Real hike ideas from the map, anywhere in Europe
---

# iHike

Plan a hike anywhere in Europe. Enter a start point and the app suggests real
outings from there: out-and-back walks to named summits, lakes, huts and
viewpoints, and waymarked loops, all taken from OpenStreetMap. You can also give
a destination yourself. Each idea comes with distance, ascent, estimated time,
path grade (T1 to T6), a weather forecast with safety alerts, and a short
briefing matched to your level. Routes can be downloaded as GPX and shared on a
community map.

## Setup (Space settings → Variables and secrets)

| Name | Type | Purpose |
|---|---|---|
| `ORS_API_KEY` | Secret | OpenRouteService key (routing and place search). Required. |
| `HF_TOKEN` | Secret | Hugging Face token with "Inference Providers" and write access. Enables the AI briefing and persistence. |
| `HIKES_DATASET` | Variable | Dataset repo that stores shared hikes, e.g. `your-username/monarch-hikes`. Created automatically. |
| `LLM_MODEL` | Variable | Optional. Chat model used for the briefing. |

The app starts with none of these set and tells you what is switched off.

## How it is built

- `app.py`: starts the web server on port 7860.
- `ihike/web.py`: the FastAPI app. Serves the interface and the `/api/...` endpoints.
- `ihike/`: places, routes, hike ideas, weather, alerts and the community store, one module each (see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
- `ihike/ai/`: models that run inside the Space on its CPU: Whisper for voice requests, BioCLIP for plants and animals in photos, and a request reader (rules, refined by an LLM when `HF_TOKEN` is set).
- `static/`: the interface (HTML, CSS, JavaScript) with a 3D relief map (MapLibre) and contour lines (maplibre-contour), both bundled in `static/vendor/`.

The first voice request and the first photo after a restart are slow (the models download and load); later ones take a few seconds.

## Data sources

- Routing and elevation: OpenRouteService (OpenStreetMap data); BRouter for alpine paths the first one refuses
- Hike goals and waymarked trails: OpenStreetMap, through the Overpass API
- Place search: Photon by Komoot (OpenStreetMap data), with OpenRouteService as fallback
- Weather: Open-Meteo
- Map tiles: OpenFreeMap; relief: Terrain Tiles on AWS
- Briefing: Hugging Face Inference Providers
- Voice: Whisper (faster-whisper); species: BioCLIP (imageomics)

Times and grades are estimates. Always check local conditions before you go.
