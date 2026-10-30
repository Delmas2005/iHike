"""Understanding a request ("a 3 h loop near Annecy with a lake, tomorrow")."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date

from ..config import HF_TOKEN
from ..config import LLM_MODEL as PARSER_MODEL

KIND_WORDS = {
    "lake": ["lake", "lakes", "lac", "lacs", "tarn", "see", "lago", "loch"],
    "summit": ["summit", "summits", "peak", "peaks", "top", "sommet", "sommets", "pic", "pointe", "mont", "cima"],
    "hut": ["hut", "huts", "refuge", "refuges", "cabane", "cabin", "chalet", "rifugio", "hutte"],
    "waterfall": ["waterfall", "waterfalls", "cascade", "cascades", "falls"],
    "viewpoint": ["viewpoint", "view", "views", "panorama", "belvedere", "vue", "lookout"],
    "pass": ["pass", "col", "cols", "saddle"],
    "loop": ["loop", "loops", "circular", "boucle", "boucles", "circuit", "tour"],
}
LEVEL_WORDS = {
    "Beginner": [
        "beginner",
        "beginners",
        "easy",
        "family",
        "kids",
        "children",
        "debutant",
        "debutants",
        "facile",
        "famille",
        "enfants",
        "gentle",
        "leisurely",
        "tranquille",
    ],
    "Intermediate": ["intermediate", "moderate", "medium", "intermediaire", "moyen", "moyenne", "sportive"],
    "Experienced": [
        "experienced",
        "expert",
        "hard",
        "difficult",
        "challenging",
        "confirme",
        "difficile",
        "athletic",
        "tough",
        "dur",
        "costaud",
    ],
}
DAY_WORDS = {
    0: ["today", "aujourd'hui", "aujourdhui", "now", "maintenant"],
    1: ["tomorrow", "demain"],
    2: ["day after tomorrow", "apres-demain", "apres demain"],
}
WEEKDAYS = [
    ["monday", "lundi"],
    ["tuesday", "mardi"],
    ["wednesday", "mercredi"],
    ["thursday", "jeudi"],
    ["friday", "vendredi"],
    ["saturday", "samedi"],
    ["sunday", "dimanche"],
]
_PLACE = r"(?:the\s+|le\s+|la\s+|les\s+|l')?([a-z0-9'\-]+(?:[\s\-][a-z0-9'\-]+){0,5})"
# Strong clues first ("near Annecy"), weak ones only if nothing better ("in Chamonix", "à Tignes").
_START_STRONG = re.compile(
    r"\b(?:near|from|around|close to|starting (?:at|from)|pres de|pres d'|depuis|"
    r"autour de|autour d'|au depart de|au depart d'|a cote de)\s*" + _PLACE
)
_START_WEAK = re.compile(r"(?:\bin|\bat|@@|\bdans|\bvers)\s+" + _PLACE)
_DEST_RE = re.compile(
    r"\b(?:(?:hike|walk|trek|go|run|head|way|climb)\s+(?:up\s+)?to|up to|climb(?:ing)?|reach|summit of|"
    r"jusqu'au|jusqu'a la|jusqu'a|jusqu'@@|monter au|monter @@ la|monter @@|aller au|aller @@ la|aller @@|"
    r"ascension du|"
    r"ascension de la|ascension de|direction)\s+" + _PLACE
)
_STOP_AT = re.compile(
    r"\b(with|avec|for|pour|in|en|during|tomorrow|today|demain|aujourd|this|ce|cette|next|"
    r"and|et|then|puis|near|pres|around|autour|from|depuis|starting|au depart|easy|facile|"
    r"beginner|debutant|loop|boucle|one way|aller simple|out and back|aller-retour|on|this|ce|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|lundi|mardi|mercredi|jeudi|"
    r"vendredi|samedi|dimanche|weekend|week-end|\d)\b"
)
# "lac de Crop", "refuge du Goûter", "col de la Croix": a feature word followed by a name.
_NAMED_PLACE = re.compile(
    r"\b((?:lac|lacs|lake|lago|loch|tarn|refuge|rifugio|cabane|hut|col|pic|pico|pizzo|pointe|aiguille|dent|"
    r"tete|mont|monte|mount|cima|cascade|waterfall|belvedere|croix|glacier|gorges?|chalets?|cirque|plateau)"
    r"(?:\s+(?:de|du|des|la|le|les|of|the|di|del|della|dei)|\s*d'|\s*l')*\s*[a-z][a-z'\-]*(?:[\s\-][a-z][a-z'\-]*){0,3})"
)
_GENERIC = {
    "mountain",
    "mountains",
    "montagne",
    "montagnes",
    "the mountains",
    "nature",
    "forest",
    "foret",
    "hills",
    "hill",
    "countryside",
    "campagne",
    "alps",
    "alpes",
    "go",
    "walk",
    "hike",
}


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


_PARTICLES = {"de", "du", "des", "la", "le", "les", "en", "sur", "sous", "of", "the", "di", "del", "della", "am", "im"}


def _clip_place(raw: str) -> str:
    m = _STOP_AT.search(raw)
    place = raw[: m.start()] if m else raw
    return place.strip(" ,.;-'")


def _title(place: str) -> str:
    """'lac de la grande sassiere' -> 'Lac de la Grande Sassiere' (geocoding ignores accents anyway)."""
    words = place.split(" ")
    return " ".join(
        w
        if (i and w in _PARTICLES)
        else "-".join(
            p if (j and p in _PARTICLES) else p[:2] + p[2:].capitalize() if p[:2] in ("d'", "l'") else p.capitalize()
            for j, p in enumerate(w.split("-"))
        )
        for i, w in enumerate(words)
    )


def parse_request(text: str, today: date | None = None) -> dict:
    """Rule-based reading of a request in English or French. Returns only what it found."""
    t = _plain(re.sub(r"(?i)(?<![\w])à\s", "@@ ", text or ""))
    words = set(re.findall(r"[a-z']+", t))
    out: dict = {}

    m = re.search(r"(\d+(?:[.,]\d+)?)\s*-?\s*(?:h|hr|hrs|hour|hours|heure|heures)\b", t)
    if m:
        out["hours"] = float(m.group(1).replace(",", "."))
    elif re.search(r"(\d+)\s*(?:min|mins|minutes)\b", t):
        out["hours"] = round(int(re.search(r"(\d+)\s*(?:min|mins|minutes)\b", t).group(1)) / 60, 1)
    elif re.search(r"half[- ]day|demi[- ]journee", t):
        out["hours"] = 4.0
    elif re.search(r"full day|whole day|all day|journee|day hike", t):
        out["hours"] = 7.0
    if "hours" in out:
        out["hours"] = min(max(out["hours"], 1.0), 10.0)

    for level, keys in LEVEL_WORDS.items():
        if words & set(keys):
            out["level"] = level
            break
    if re.search(r"\b(run|running|jog|jogging|courir|course a pied|trail running|trail run|trailrun)\b", t):
        out["activity"] = "Trail running"

    kinds = [k for k, keys in KIND_WORDS.items() if words & set(keys)]
    if kinds:
        out["kinds"] = kinds

    today = today or date.today()
    for idx, keys in sorted(DAY_WORDS.items(), reverse=True):
        if any(k in t for k in keys):
            out["day"] = idx
            break
    else:
        for i, names in enumerate(WEEKDAYS):
            if words & set(names):
                out["day"] = (i - today.weekday()) % 7
                break
        else:
            if re.search(r"week[- ]?end", t):
                out["day"] = 0 if today.weekday() >= 5 else 5 - today.weekday()

    not_places = set(sum(KIND_WORDS.values(), [])) | set(sum(LEVEL_WORDS.values(), [])) | _GENERIC

    def usable(place):
        return place and place not in not_places and not re.fullmatch(r"[\d\s.,h]+", place)

    dest = _DEST_RE.search(t)
    dest_span = None
    if dest and usable(_clip_place(dest.group(1))):
        out["destination"] = _clip_place(dest.group(1))
        out["mode"] = "one_way" if re.search(r"one[- ]way|aller simple", t) else "out_and_back"
        dest_span = (dest.start(), dest.start(1) + len(out["destination"]))
    if "destination" not in out:
        named = _NAMED_PLACE.search(t)
        if named:
            place = _clip_place(named.group(1))
            words = [w for w in re.split(r"[\s\-]+", place) if w and w not in _PARTICLES]
            if len(words) >= 2 and usable(place):  # "lac" alone is a wish, "lac de crop" is a place
                out["destination"] = place
                out["mode"] = "one_way" if re.search(r"one[- ]way|aller simple", t) else "out_and_back"
                dest_span = (named.start(), named.start() + len(place))
    for pattern in (_START_STRONG, _START_WEAK):
        for m in pattern.finditer(t):
            if dest_span and dest_span[0] <= m.start() < dest_span[1]:
                continue
            place = _clip_place(m.group(1))
            if usable(place):
                out["start"] = place
                break
        if "start" in out:
            break
    for key in ("start", "destination"):
        if key in out:
            out[key] = _title(out[key])
    if "mode" not in out:
        out["mode"] = "ideas"
    return out


_PARSER_PROMPT = (
    "Extract hiking search parameters from the user's request. Answer with JSON only, no prose, with these keys "
    "(omit a key when the request does not say it): start (place name, keep the original spelling and add the "
    'country if obvious), mode ("ideas" when no precise destination is given, "out_and_back" or "one_way" '
    'when there is one), destination, hours (number), level ("Beginner", "Intermediate" or "Experienced"), '
    'activity ("Hiking" or "Trail running"), kinds (list among lake, summit, hut, waterfall, viewpoint, pass, '
    "loop), day (0 for today up to 6)."
)


def _llm_parse(text: str, today: date):
    if not HF_TOKEN:
        return None
    try:
        from huggingface_hub import InferenceClient

        out = InferenceClient(token=HF_TOKEN, timeout=30).chat_completion(
            model=PARSER_MODEL,
            max_tokens=200,
            temperature=0,
            messages=[
                {"role": "system", "content": _PARSER_PROMPT + f" Today is {today:%A %Y-%m-%d}."},
                {"role": "user", "content": text[:500]},
            ],
        )
        raw = out.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group()) if m else None
    except Exception as exc:
        print("Parser LLM error:", exc)
        return None


def _clean(params: dict) -> dict:
    """Keep only well-formed values."""
    out = {}
    if isinstance(params.get("start"), str) and params["start"].strip():
        out["start"] = params["start"].strip()[:120]
    if isinstance(params.get("destination"), str) and params["destination"].strip():
        out["destination"] = params["destination"].strip()[:120]
    if params.get("mode") in ("ideas", "out_and_back", "one_way"):
        out["mode"] = params["mode"]
    try:
        if params.get("hours") is not None:
            out["hours"] = min(max(float(params["hours"]), 1.0), 10.0)
    except (TypeError, ValueError):
        pass
    if params.get("level") in ("Beginner", "Intermediate", "Experienced"):
        out["level"] = params["level"]
    if params.get("activity") in ("Hiking", "Trail running"):
        out["activity"] = params["activity"]
    if isinstance(params.get("kinds"), list):
        kinds = [k for k in params["kinds"] if k in KIND_WORDS]
        if kinds:
            out["kinds"] = kinds
    try:
        if params.get("day") is not None and 0 <= int(params["day"]) <= 6:
            out["day"] = int(params["day"])
    except (TypeError, ValueError):
        pass
    if out.get("mode") in ("out_and_back", "one_way") and "destination" not in out:
        out["mode"] = "ideas"
    return out


def understand(text: str, today: date | None = None) -> dict:
    """Free text -> {"params": {...}, "source": "ai" | "rules"}."""
    today = today or date.today()
    params = _clean(parse_request(text, today))
    llm = _llm_parse(text, today)
    if llm:
        refined = _clean(llm)
        if refined:
            return {"params": {**params, **refined}, "source": "ai"}
    return {"params": params, "source": "rules"}
