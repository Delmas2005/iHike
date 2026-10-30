"""The classic Gradio interface of iHike, served at /classic. The main interface is static/."""
from __future__ import annotations

import html
import tempfile

import folium
import gradio as gr
import pandas as pd
import requests

from engine import *  # noqa: F401,F403 - the engine's public functions and constants
from engine import _record, scheduler, persistence_error  # noqa: F401
import engine

# --------------------------------------------------------------------------
# Rendering: map, elevation profile, GPX
# --------------------------------------------------------------------------
def render_map(tracks=None, hikes=None) -> str:
    m = folium.Map(location=[50, 10], zoom_start=4, tiles="OpenStreetMap")
    try:
        folium.TileLayer("OpenTopoMap", name="Topographic", show=False).add_to(m)
    except Exception:
        pass
    bounds = []

    for h in hikes or []:
        path = h.get("path") or []
        if len(path) < 2:
            continue
        esc = lambda v: html.escape(str(v))  # user text goes into HTML: always escape
        popup = (f"<b>{esc(h.get('title', 'Hike'))}</b><br>{esc(h.get('distance_km'))} km · "
                 f"+{esc(h.get('ascent_m'))} m · {esc(h.get('grade'))}<br>"
                 f"Dog friendly: {esc(h.get('dog'))} · Camping: {esc(h.get('camping'))}"
                 f"<br><i>{esc(h.get('notes', ''))}</i>")
        folium.PolyLine(path, color="#555555", weight=3, opacity=0.8, dash_array="6",
                        tooltip=esc(h.get("title", "Hike")), popup=folium.Popup(popup, max_width=280)).add_to(m)
        folium.CircleMarker(path[0], radius=5, color="#555555", fill=True, fill_opacity=1,
                            tooltip=esc(h.get("title", "Hike"))).add_to(m)
        if not tracks:
            bounds += path

    for t in reversed(tracks or []):  # draw the easiest one on top
        folium.PolyLine(t["latlon"], color=t["color"], weight=5, opacity=0.9,
                        tooltip=html.escape(f"{t['name']} - {t['distance_km']} km, +{t['ascent_m']} m")).add_to(m)
        bounds += t["latlon"]
    if tracks:
        first = tracks[0]
        dot = dict(radius=8, color="#ffffff", weight=2, fill=True, fill_opacity=1)
        marked = set()
        for t in tracks:
            far = t.get("far") or (None if t["loop"] else t["latlon"][-1])
            spot = (round(far[0], 4), round(far[1], 4)) if far else None
            if spot and spot not in marked:
                marked.add(spot)
                folium.CircleMarker(far, tooltip=html.escape(t.get("far_label", "End")),
                                    fill_color=t["color"] if len(tracks) > 1 and "title" in t else "#c0392b", **dot).add_to(m)
        folium.CircleMarker(first["latlon"][0], tooltip="Start", fill_color="#1e8449", **dot).add_to(m)

    if bounds:
        lats, lons = [p[0] for p in bounds], [p[1] for p in bounds]
        m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]], padding=(20, 20))
    folium.LayerControl().add_to(m)
    page = html.escape(m.get_root().render(), quote=True)
    return f'<iframe srcdoc="{page}" width="100%" height="560" style="border:none;border-radius:8px"></iframe>'


def elevation_frame(tracks):
    rows = []
    for t in tracks:
        if not t["eles"]:
            continue
        for km, ele in decimate(list(zip(t["km"], t["eles"])), 200):
            rows.append({"Distance (km)": round(km, 2), "Elevation (m)": round(ele), "Variant": t["name"]})
    return pd.DataFrame(rows) if rows else None


# --------------------------------------------------------------------------
# Gradio handlers
# --------------------------------------------------------------------------
MODE_OF = {TRIP_TYPES[0]: "ideas", TRIP_TYPES[1]: "out_and_back", TRIP_TYPES[2]: "one_way"}


def plan(start_text, trip_type, end_text, activity, level, hours, day_index):
    empty = (render_map(), None, None, [], gr.update(choices=[], value=None))
    mode = MODE_OF.get(trip_type) or ("ideas" if str(trip_type).startswith(("Suggest", "Loop"))
                                      else "out_and_back" if str(trip_type).startswith("Out") else "one_way")
    try:
        r = plan_hikes(start_text, mode, end_text, activity, level, hours, day_index)
    except PlannerError as exc:
        return (f"### ⚠️ {exc}", *empty)
    except requests.RequestException:
        return ("### ⚠️ The routing service did not answer. Please try again in a minute.", *empty)

    s_label, s_others = r["start"]["label"], r["start"]["others"]
    end = (r["end"]["lat"], r["end"]["lon"]) if r["end"] else None
    e_label, e_others = (r["end"]["label"], r["end"]["others"]) if r["end"] else ("", [])
    tracks, rec, trails, nearby = r["tracks"], r["recommended"], r["trails"], r["nearby"]
    weather, w_where, alerts, note, ideas = r["weather"], r["weather_where"], r["alerts"], r["note"], r["ideas"]
    out_and_back, briefing, ai_used = r["out_and_back"], r["briefing"], r["ai_briefing"]
    off_m = r["start_offset_m"]

    lines = [f"### {'Hike ideas' if ideas else 'Routes'} from {s_label}", ""]
    if note:
        lines += [f"> ℹ️ {note}", ""]
    if ideas:
        lines += ["Each idea leads to a named place or follows a waymarked trail found on OpenStreetMap. "
                  "Out-and-back figures are for the **full return trip**.", ""]
    if s_others:
        lines += [f"<sub>Start matched to **{s_label}**. Wrong place? Other matches: {'; '.join(s_others)}. "
                  "Type the name more precisely or paste coordinates.</sub>", ""]
    if end is not None:
        lines += [f"<sub>Destination matched to **{e_label}**."
                  + (f" Other matches: {'; '.join(e_others)}." if e_others else "") + "</sub>", ""]
    if off_m > 300:
        lines += [f"> ℹ️ The nearest mapped path is about {round(off_m, -1):.0f} m from the point you entered; "
                  "the routes start there.", ""]
    if end is not None and out_and_back:
        lines += ["Out and back: distance, ascent and time are for the **full return trip**.", ""]
    lines += ["| Hike | Type | Distance | Ascent | Highest point | Est. time | Terrain | Grade |", "|---|---|---|---|---|---|---|---|"]
    for t in tracks:
        star = " ⭐ recommended" if t is rec else ""
        top = f"{t['max_ele']} m" if t["max_ele"] else "-"
        lines.append(f"| **{html.escape(t['name'])}**{star} | {t.get('kind', '-')} | {t['distance_km']} km | +{t['ascent_m']} m | {top} | "
                     f"{t['hours']} h | {t['terrain']} | {t['grade']} |")
    lines.append("")
    if rec is None:
        lines += [f"> ⚠️ **No route here suits a {level.lower()} hiker.** See the briefing below.", ""]
    if weather:
        lines.append(f"**Weather on {weather['date']} {w_where}:** {weather['summary']}, "
                     f"{round(weather['tmin'])} to {round(weather['tmax'])} °C, rain {weather['precip_mm']} mm "
                     f"({weather['precip_prob']} %), gusts {round(weather['gusts'])} km/h, "
                     f"sunrise {weather['sunrise']}, sunset {weather['sunset']}.")
    else:
        lines.append("**Weather:** forecast unavailable right now - check it yourself before leaving.")
    lines += ["", "**Alerts**", ""] + ([f"- {a}" for a in alerts] or ["- No alert for that day."])
    lines += ["", f"**{'AI briefing' if ai_used else 'Briefing'}**", "", briefing]
    named = list({t["name"]: t for t in trails or [] if t["name"] != "Unnamed trail"}.values())[:6]
    if named:
        lines += ["", "**Waymarked trails passing near the start**", ""]
        lines += [f"- [{html.escape(t['name'])}](https://hiking.waymarkedtrails.org/#route?id={t['id']})"
                  + (f" - {t['network']} trail" if t["network"] else "") for t in named]
    if nearby:
        lines += ["", "**Hikes shared by the community nearby**", ""]
        lines += [f"- {html.escape(str(h.get('title')))} - {h.get('distance_km')} km, +{h.get('ascent_m')} m, "
                  f"{h.get('grade')} ({h['away_km']} km away)" for h in nearby]
    lines += ["", "<sub>Times and grades are estimates from distance, elevation and the path grade recorded on "
                  "OpenStreetMap when there is one. "
                  "Routes and forecasts are generated automatically: check local conditions before you go.</sub>"]

    folder = tempfile.mkdtemp(prefix="gpx_")
    names = [t["name"] for t in tracks]
    profile = gr.update(value=elevation_frame(tracks), color_map={t["name"]: t["color"] for t in tracks})
    return ("\n".join(lines), render_map(tracks, nearby), profile,
            [write_gpx(t, folder) for t in tracks], tracks, gr.update(choices=names, value=(rec or tracks[0])["name"]))


def share_planned(tracks, choice, title, notes, dog, camping):
    track = next((t for t in tracks or [] if t["name"] == choice), None)
    if not track:
        return "⚠️ Plan a route first, then choose the variant to share."
    save_hike(_record(track, title, notes, dog, camping, "planned"))
    return "✅ Shared. It now appears in the Community tab."


def share_gpx(file_path, title, notes, dog, camping):
    if not file_path:
        return "⚠️ Choose a GPX file first.", *community_view()
    try:
        track = build_track(parse_gpx(file_path))
    except PlannerError as exc:
        return f"⚠️ {exc}", *community_view()
    if not in_europe(*track["latlon"][0]):
        return "⚠️ This track starts outside Europe, which is the only area covered for now.", *community_view()
    save_hike(_record(track, title, notes, dog, camping, "gpx"))
    return f"✅ Imported: {track['distance_km']} km, +{track['ascent_m']} m ({track['grade']}).", *community_view()


COMMUNITY_COLUMNS = ["Title", "Distance (km)", "Ascent (m)", "Grade", "Loop", "Dog friendly", "Camping", "Notes"]


def community_view():
    hikes = load_hikes()
    rows = [[h.get("title", ""), h.get("distance_km"), h.get("ascent_m"), h.get("grade", ""),
             "Yes" if h.get("loop") else "No", h.get("dog", ""), h.get("camping", ""), h.get("notes", "")]
            for h in reversed(hikes)]
    return render_map(hikes=hikes), pd.DataFrame(rows, columns=COMMUNITY_COLUMNS)


def config_notice() -> str:
    notes = []
    scheduler = engine.scheduler
    if not ORS_API_KEY:
        notes.append("routing is off (no `ORS_API_KEY`)")
    if not HF_TOKEN:
        notes.append("AI briefing is off (no `HF_TOKEN`)")
    if not scheduler:
        if not HIKES_DATASET:
            why = "no `HIKES_DATASET`"
        elif not HF_TOKEN:
            why = "`HIKES_DATASET` needs `HF_TOKEN`"
        else:
            why = f"could not open dataset `{HIKES_DATASET}` - {persistence_error}"
        notes.append(f"shared hikes are not persisted ({why})")
    return ("> ⚙️ Setup incomplete: " + "; ".join(notes) + ".") if notes else ""


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------
YES_NO = ["Unknown", "Yes", "No"]

with gr.Blocks(title="iHike classic") as demo:
    gr.Markdown("# iHike (classic)\nHike ideas to real summits, lakes and huts, with weather alerts and a briefing matched to your level - anywhere in Europe.")
    gr.Markdown(config_notice())
    tracks_state = gr.State([])

    with gr.Tab("Plan a hike"):
        with gr.Row():
            with gr.Column(scale=1, min_width=300):
                start_in = gr.Textbox(label="Start", placeholder="Chamonix, France   or   45.9237, 6.8694",
                                      info="A place name, or coordinates as latitude, longitude.")
                trip_in = gr.Radio(TRIP_TYPES, value=TRIP_TYPES[0], label="Trip type",
                                   info="Suggestions look for summits, lakes, huts, viewpoints and marked loops nearby.")
                end_in = gr.Textbox(label="Destination", placeholder="Summit, lake, hut...  or  lat, lon", visible=False)
                activity_in = gr.Radio(list(ACTIVITY_SPEED), value="Hiking", label="Activity")
                level_in = gr.Radio(list(LEVELS), value="Beginner", label="Your level")
                hours_in = gr.Slider(1, 10, value=3, step=0.5, label="Time you want to spend (hours)",
                                     info="Used to choose goals within reach and to pick the recommended hike.")
                day_in = gr.Dropdown(DAY_CHOICES, value="Today", type="index", label="When")
                plan_btn = gr.Button("Plan my hike", variant="primary")
            with gr.Column(scale=2, min_width=420):
                map_out = gr.HTML(render_map())
                profile_out = gr.LinePlot(x="Distance (km)", y="Elevation (m)", color="Variant",
                                          label="Elevation profile", height=260)
        summary_out = gr.Markdown()
        gpx_out = gr.File(label="Download the routes (GPX)", file_count="multiple", interactive=False)

        with gr.Accordion("Share one of these routes with the community", open=False):
            share_choice = gr.Dropdown([], label="Variant")
            share_title = gr.Textbox(label="Name", max_lines=1)
            share_notes = gr.Textbox(label="Notes", placeholder="Water points, huts, tricky sections...", lines=2)
            with gr.Row():
                share_dog = gr.Radio(YES_NO, value="Unknown", label="Dog friendly")
                share_camp = gr.Radio(YES_NO, value="Unknown", label="Camping possible")
            share_btn = gr.Button("Share")
            share_msg = gr.Markdown()

    with gr.Tab("Community hikes") as community_tab:
        community_map = gr.HTML()
        community_table = gr.Dataframe(headers=COMMUNITY_COLUMNS, col_count=(len(COMMUNITY_COLUMNS), "fixed"),
                                       interactive=False, wrap=True)
        with gr.Accordion("Add a hike you have done (GPX file)", open=False):
            gr.Markdown("Export a GPX from your watch or tracking app and upload it here. "
                        "The track becomes visible to every visitor: only share what you are happy to make public.")
            gpx_in = gr.File(label="GPX file", file_types=[".gpx"], type="filepath")
            gpx_title = gr.Textbox(label="Name", max_lines=1)
            gpx_notes = gr.Textbox(label="Notes", lines=2)
            with gr.Row():
                gpx_dog = gr.Radio(YES_NO, value="Unknown", label="Dog friendly")
                gpx_camp = gr.Radio(YES_NO, value="Unknown", label="Camping possible")
            gpx_btn = gr.Button("Add to the community map")
            gpx_msg = gr.Markdown()

    trip_in.change(lambda t: gr.update(visible=not t.startswith("Suggest")), trip_in, end_in)
    plan_btn.click(
        plan,
        [start_in, trip_in, end_in, activity_in, level_in, hours_in, day_in],
        [summary_out, map_out, profile_out, gpx_out, tracks_state, share_choice],
    )
    share_btn.click(share_planned, [tracks_state, share_choice, share_title, share_notes, share_dog, share_camp], share_msg)
    gpx_btn.click(share_gpx, [gpx_in, gpx_title, gpx_notes, gpx_dog, gpx_camp], [gpx_msg, community_map, community_table])
    community_tab.select(community_view, None, [community_map, community_table])

if __name__ == "__main__":
    demo.launch()  # standalone: python classic.py
