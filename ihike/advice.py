from __future__ import annotations

import json

from .config import GRADES, HF_TOKEN, LEVEL_LIMITS, LLM_MODEL


def suits(track, level) -> bool:
    max_sac, max_grade = LEVEL_LIMITS[level]
    return track["sac"] <= max_sac and GRADES.index(track["grade"]) <= GRADES.index(max_grade)


def recommend(tracks, level, hours):
    """The suitable variant closest to the wished duration, or None when nothing suits the level."""
    pool = [t for t in tracks if suits(t, level)]
    return min(pool, key=lambda t: abs(t["hours"] - hours)) if pool else None


SYSTEM_PROMPT = (
    "You are a careful mountain guide writing a short pre-hike briefing in English. "
    "Use ONLY the data in the user's message. Never invent trail names, huts, water points "
    "or facts about the area. In at most 130 words: say which variant suits this hiker and why, "
    "mention the weather and every alert, and give two practical tips adapted to their level. "
    "If the alerts make the outing unwise, say so plainly. If pre_selected_variant is null, no route "
    "suits this hiker's level: say that clearly, explain why from the data, and do not recommend one. "
    "Plain text, no headings."
)


def ai_briefing(context: dict):
    if not HF_TOKEN:
        return None
    try:
        from huggingface_hub import InferenceClient

        client = InferenceClient(token=HF_TOKEN, timeout=60)
        out = client.chat_completion(
            model=LLM_MODEL,
            max_tokens=300,
            temperature=0.4,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
        )
        return (out.choices[0].message.content or "").strip() or None
    except Exception as exc:  # no credits left, model unavailable, ...
        print("LLM error:", exc)
        return None


def fallback_briefing(rec, level, alerts, tracks) -> str:
    if rec is None:
        easiest = tracks[0]
        return (
            f"None of these routes is a good match at {level.lower()} level. Even the easiest one is "
            f"{easiest['distance_km']} km with {easiest['ascent_m']} m of ascent, about {easiest['hours']} h, "
            f"graded {easiest['grade']} (terrain: {easiest['terrain']}). Choose a closer or lower goal."
        )
    text = (
        f"At {level.lower()} level, variant **{rec['name']}** is the best match: "
        f"{rec['distance_km']} km, {rec['ascent_m']} m of ascent, about {rec['hours']} h ({rec['grade']})."
    )
    if alerts:
        text += " Read the alerts above before deciding to go."
    return text
