# Audit of the imported code (Phase 0)

Snapshot of the problems found when the code was imported into Git, October 2026. Each item says what is wrong, how it was observed, and which phase of the plan addresses it. Items are removed from this list as they are fixed, with the fixing commit in the changelog.

Priorities:
- **P0**: security, data loss or cost exposure on the public Space. Fix first.
- **P1**: wrong results or failures a user will meet.
- **P2**: maintainability, reproducibility, polish.

## P0

1. **An OpenRouteService key is in the Space's Git history.** The first version of `app.py` (spring 2026) had the key in clear. The current code reads it from `ORS_API_KEY`, and the new GitHub repository starts from a clean import, but the Hugging Face Space repository still contains the old commit. *Action: revoke that key at openrouteservice.org (if not done) and decide whether to recreate the Space or rewrite its history before automatic deployment. Phase 0 decision, enforced in Phase 4.*

2. **No rate limiting on the public API.** One `POST /api/plan` can make up to about 10 OpenRouteService requests (5 goals, 2 loops, retries) and 1 to 4 Overpass requests. Anyone can exhaust the free ORS daily quota, the HF inference credits (`/api/ask`, briefing) or the CPU (`/api/voice`, `/api/photos`) with a simple loop. *Phase 3: per-IP limits on costly endpoints.*

3. **Uploaded photos are never deleted unless shared.** `POST /api/photos` writes two JPEGs to `pending_photos/` for every upload; nothing removes them when the hike is not shared. The Space disk can fill up. Observed by reading `save_photo` / `publish_photos`: there is no cleanup path. *Phase 3: expiry of pending photos, upload quota.*

4. **The `HF_TOKEN` in the Space has more rights than needed.** The README asks for a classic "Write" token, which can write to every repository of the account. If the Space were compromised, so would be the account. *Phase 0 decision: use a fine-grained token limited to the dataset and to Inference Providers.*

## P1

5. **`transcribe()` and `identify()` raise instead of degrading.** The module docstring of the old `ai.py` promised graceful degradation, but both functions raise if their library is missing or the model cannot be downloaded. The web layer catches it today (it checks `available()` first and wraps the calls), so the app does not crash, but the contract is broken and the user only gets a generic message. `available()` only checks that the package is installed, not that the model loads. *Phase 3, with tests in Phase 2.*

6. **A network failure is reported as "place not found".** With Photon unreachable, `POST /api/plan {"start": "Chamonix"}` answers `400 Could not find 'Chamonix' in Europe` (observed in the local run: Photon refused, ORS geocoder disabled without a key). `search_places` swallows every exception and returns an empty list. *Phase 3: distinguish "unavailable" (503) from "not found" (400).*

7. **No retry with backoff on any external call, and a very long worst case.** Each call has a timeout, but failures are not retried (except trying the next Overpass mirror), and timeouts add up: in the worst case one `/api/plan` waits roughly 15 min (Photon 15 s, ORS geocoder 20 s, 4 Overpass mirrors × 35 s, 7 ORS routes × 2 × 45 s, Open-Meteo, LLM 60 s). There is no overall deadline. Typical requests take 5 to 20 s. *Phase 3: retries with backoff, an overall time budget, Phase 6: parallel calls and caching.*

8. **Request reader errors found in a quick probe** (`ihike.ai.understand`, rules only):

   | Input | Result | Problem |
   |---|---|---|
   | `rando de 2h30 depuis Chamonix` | no `hours` | `2h30` and `1h30` are not read at all |
   | `hike 3 hours from Zermatt to the Gornergrat` | start = `Zermatt To the Gornergrat`, no destination | "to the X" after a start is not a destination |
   | `I want to see marmots near Pralognan` | kinds = `lake` | the English verb "see" is in the lake word list (German *See*) |
   | `une boucle de 3h près d Annecy` | no start | "près d" without an apostrophe is not a start clue |
   | `randonée facil prés de grenoble` | no level | no tolerance for typos in level words |
   | `Aiguille de la Grande Sassière` | `Sassiere` | accents are dropped from place names (geocoding still works) |
   | `trail 15 km` | nothing | distances are not understood |

   *Phase 2 (tests) and Phase 5 (ParseBench) will measure these properly; fixes only on the tuning set.*

9. **Models load inside the request lock.** The first voice request or photo after a restart downloads and loads the model (hundreds of MB) while holding the lock; every other request of that kind waits, and the browser request can time out. *Phase 6: warm-up at start-up.*

10. ~~**`engine.py` does work at import time.**~~ Fixed: importing `ihike` has no side effects; `community.init_storage()` runs when the server starts (tested in `tests/integration/test_startup.py`).

11. **Server date versus user date.** `day` is an index from "today" on the server (UTC on the Space). Around midnight in Europe, "today" in the browser and on the server differ by one day. *Phase 3.*

## P2

12. **Dependencies are not pinned.** `requirements.txt` lists names only (`torch`, `gradio` via the SDK, etc.), so two builds of the Space can differ. *Phase 1: `pyproject.toml` with ranges, generated pinned `requirements.txt`.*

13. ~~**`engine.py` is 1,450 lines with eight responsibilities.**~~ Split into the `ihike/` package (13 modules plus `ihike/ai/`), code moved without changes in behaviour.

14. ~~**`classic.py` duplicates presentation logic.**~~ Removed: the Gradio fallback is gone, with Folium and pandas.

15. **Logging is `print()`.** 14 calls across the modules, no levels, no request context. *Phase 3: `logging` with levels, no personal data (no coordinates of users' homes, no free text).*

16. **No automated tests in the repository.** Partly fixed: a first pytest suite (95 tests, all external services simulated) is in `tests/`. *Phase 2: coverage targets and the remaining cases.*

17. **Privacy of shared GPX tracks.** A track recorded from home publishes its start and end point. The interface warns, but does not trim. *Possible feature: hide the first and last few hundred metres, as tracking apps do.*

18. **The interface loads Google Fonts from Google's servers**, which sends each visitor's IP address to Google (a known GDPR concern in the EU). *Phase 7 or earlier: self-host the two fonts.*

19. **Python version is not pinned on the Space.** `README.md` has no `python_version`, so the Space uses the SDK default. *Phase 1: set 3.11 to match `pyproject.toml`.*

20. **Public endpoints accept anonymous publication.** Anyone can add hikes and photos to the community map; there is no moderation or deletion path. *Product decision, not scheduled.*
