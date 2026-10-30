from __future__ import annotations

import re

from .config import SAC_LABELS
from .weather import ICE_CODES, SNOW_CODES, STORM_CODES, get_elevations


def terrain_alerts(tracks, end=None) -> list[str]:
    """Warnings that come from the ground, not the sky."""
    alerts = []
    hardest = max(t["sac"] for t in tracks)
    if hardest >= 5:
        alerts.append(f"🧗 **Alpine terrain ({SAC_LABELS[hardest]})** - scrambling and exposed passages, possibly "
                      "snow or glacier. This is mountaineering ground, not hiking: experience and equipment required.")
    elif hardest == 4:
        alerts.append("🧗 **T4 alpine hiking on the way** - hands needed in places, exposed passages, "
                      "path sometimes hard to follow.")
    top = max((t["max_ele"] for t in tracks if t["max_ele"]), default=0)
    if top >= 2500:
        alerts.append(f"🏔️ **High mountain** (up to {top} m) - snow or ice can remain or arrive in any season; "
                      "check recent conditions and expect to move slower than the estimate.")
    if any(t.get("router") == "brouter" for t in tracks):
        alerts.append("ℹ️ The standard hiking router does not go all the way to this destination (usually because the "
                      "last section is graded too difficult). This route was computed with an alpine profile instead.")
    short = min((t["end_gap_km"] for t in tracks if "end_gap_km" in t), default=0)
    if end is not None and short > 0.3:
        t = min((t for t in tracks if "end_gap_km" in t), key=lambda t: t["end_gap_km"])
        missing, climb_left = "", 0
        target = get_elevations([end])[0]
        if target is not None and t["eles"]:
            reached = t["eles"][t["latlon"].index(t["far"])]
            if target - reached > 50:
                climb_left = target - reached
                missing = f" and about {round(climb_left, -1):.0f} m below it"
        legs = 2 if t.get("far_label") == "Turnaround point" else 1
        full_km = t["distance_km"] + short * 1.3 * legs
        alerts.append(f"🛑 **The route stops before your destination**: it ends {short:.1f} km away{missing}. "
                      "The rest is not mapped as a path, or is beyond hiking grade, so it is not drawn and not "
                      f"counted. Including it, the outing would be roughly {full_km:.0f} km and "
                      f"+{round(t['ascent_m'] + climb_left, -1):.0f} m."
                      + (f" (Alpine router: {t['alpine_note']}.)" if t.get("alpine_note") else ""))
    return alerts


def weather_alerts(w, longest_hours: float) -> list[str]:
    if not w:
        return []
    alerts = []
    if w["code"] in STORM_CODES:
        alerts.append("⛈️ **Thunderstorms forecast** - stay off ridges and summits, or pick another day.")
    if w["code"] in SNOW_CODES:
        alerts.append("❄️ **Snow forecast** - paths may be hidden and slippery; winter equipment needed.")
    if w["code"] in ICE_CODES:
        alerts.append("🧊 **Freezing rain or fog** - icy ground and poor visibility.")
    if w["precip_prob"] >= 60 or w["precip_mm"] >= 10:
        alerts.append(f"🌧️ **Rain likely** ({w['precip_prob']} %, {w['precip_mm']} mm) - waterproofs, slippery rock.")
    if w["gusts"] >= 60:
        alerts.append(f"💨 **Strong wind** (gusts {round(w['gusts'])} km/h) - avoid exposed sections.")
    if w["tmax"] is not None and w["tmax"] >= 30:
        alerts.append(f"🥵 **Heat** (up to {round(w['tmax'])} °C) - start early, carry extra water.")
    if w["tmin"] is not None and w["tmin"] <= 0:
        alerts.append(f"🥶 **Frost** (down to {round(w['tmin'])} °C) - possible ice in the morning.")
    if w["daylight_h"] and longest_hours > 0.7 * w["daylight_h"]:
        alerts.append(f"🌙 **Short daylight** ({w['daylight_h']} h, sunset {w['sunset']}) - "
                      "the longest variant leaves little margin; take a headlamp.")
    return alerts


_ALERT_RE = re.compile(r"^(\S+)\s+(?:\*\*(.+?)\*\*)?\s*(.*)$", re.S)
_DANGER_ICONS = {"🛑", "⛈️", "🧊"}
_INFO_ICONS = {"ℹ️"}


def alert_data(text: str) -> dict:
    """'⛈️ **Thunderstorms forecast** - stay off ridges' -> {severity, title, detail}."""
    m = _ALERT_RE.match(text.strip())
    icon, title, detail = (m.group(1), m.group(2) or "", m.group(3)) if m else ("", "", text)
    detail = re.sub(r"^[\s:\-–]+", "", detail.replace("**", "")).strip()
    if title and detail.startswith("("):  # '(80 %, 14 mm) - waterproofs' belongs to the title
        figures, _, detail = detail.partition(")")
        title = f"{title} {figures})"
        detail = re.sub(r"^[\s:\-–]+", "", detail)
    detail = detail[:1].upper() + detail[1:]
    severity = ("danger" if icon in _DANGER_ICONS or "T5" in title or "T6" in title
                else "info" if icon in _INFO_ICONS else "warning")
    if not title:  # information lines have no bold title: use their first sentence
        first, _, rest = detail.partition(". ")
        title, detail = first.rstrip("."), rest
    return {"severity": severity, "title": title, "detail": detail}
