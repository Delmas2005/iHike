/* iHike interface. Vanilla JS, no build step. Talks to the Python engine through /api/*. */
/* global maplibregl, mlcontour */

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Build DOM safely: strings always become text, never HTML. */
function h(tag, attrs = {}, ...children) {
  const svgTags = new Set(["svg", "path", "line", "circle", "rect", "g", "text", "polyline"]);
  const el = svgTags.has(tag) ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("--")) el.style.setProperty(k, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const fmtKm = (km) => (km >= 10 ? Math.round(km) : Math.round(km * 10) / 10);
const fmtH = (hrs) => {
  const m = Math.round(hrs * 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${String(m % 60).padStart(2, "0")}` : ""}`;
};
const fmtM = (m) => (m == null ? "–" : Math.round(m).toLocaleString("en-GB"));
const LEVEL_NAME = { Beginner: "beginner", Intermediate: "regular hiker", Experienced: "seasoned hiker" };

async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.json !== undefined) {
    opts.method = opts.method || "POST";
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new Error("iHike could not be reached. Check your connection and try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "Something went wrong on the server.";
    throw new Error(detail);
  }
  return data;
}

let toastTimer;
function toast(message, ms = 4200) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

function haversine(a, b) { // [lon, lat] pairs -> km
  const R = 6371, rad = Math.PI / 180;
  const dLat = (b[1] - a[1]) * rad, dLon = (b[0] - a[0]) * rad;
  const s = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(s));
}
function cumulative(geom) {
  const d = [0];
  for (let i = 1; i < geom.length; i++) d.push(d[i - 1] + haversine(geom[i - 1], geom[i]));
  return d;
}
function bounds(coords) {
  const b = new maplibregl.LngLatBounds(coords[0], coords[0]);
  coords.forEach((c) => b.extend([c[0], c[1]]));
  return b;
}

// -------------------------------------------------------------------- state
const state = {
  config: {}, result: null, selected: null, community: null,
  view: "plan", terrain: true, contours: true, markers: [], communityMarkers: [],
  flight: null, cursor: null, profile: null,
  startName: null, picking: null, pickMarkers: {}, placeMarkers: [],
};
const gmapsDirections = (lat, lon) =>
  `https://www.google.com/maps/dir/?api=1&destination=${lat.toFixed(6)},${lon.toFixed(6)}&travelmode=driving`;

// ---------------------------------------------------------------------- map
const DEM_TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
const VECTOR = "https://tiles.openfreemap.org/planet";
const GLYPHS = "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf";
const C = {
  paper: "#EEF0E8", ink: "#1D2B2A", sepia: "#9A6B3F", water: "#A9CBE2", waterLine: "#6E9FC4",
  wood: "#CFDDBD", grass: "#E1E8D1", rock: "#E2DED6", ice: "#F7FBFD", path: "#7A5230",
};

let demSource = null;
try {
  demSource = new mlcontour.DemSource({ url: DEM_TILES, encoding: "terrarium", maxzoom: 13, worker: true });
  demSource.setupMaplibre(maplibregl);
} catch (err) {
  console.warn("Contour lines unavailable:", err);
}

function mapStyle() {
  const sources = {
    omt: { type: "vector", url: VECTOR, attribution: "© OpenStreetMap contributors, OpenFreeMap" },
    hillshade: { type: "raster-dem", tiles: [DEM_TILES], encoding: "terrarium", tileSize: 256, maxzoom: 13 },
    terrain: { type: "raster-dem", tiles: [DEM_TILES], encoding: "terrarium", tileSize: 256, maxzoom: 13,
               attribution: "Relief: Terrain Tiles (Mapzen, AWS)" },
  };
  if (demSource) {
    sources.contours = {
      type: "vector", maxzoom: 15,
      tiles: [demSource.contourProtocolUrl({
        thresholds: { 10: [200, 1000], 11: [100, 500], 12: [50, 250], 13: [20, 100], 14: [10, 50] },
        contourLayer: "contours", elevationKey: "ele", levelKey: "level", multiplier: 1,
      })],
    };
  }
  const lineW = (a, b) => ["interpolate", ["linear"], ["zoom"], 10, a, 16, b];
  const layers = [
    { id: "bg", type: "background", paint: { "background-color": C.paper } },
    { id: "landcover-wood", type: "fill", source: "omt", "source-layer": "landcover",
      filter: ["==", ["get", "class"], "wood"], paint: { "fill-color": C.wood, "fill-opacity": 0.85 } },
    { id: "landcover-grass", type: "fill", source: "omt", "source-layer": "landcover",
      filter: ["in", ["get", "class"], ["literal", ["grass", "farmland", "wetland"]]],
      paint: { "fill-color": C.grass, "fill-opacity": 0.7 } },
    { id: "landcover-rock", type: "fill", source: "omt", "source-layer": "landcover",
      filter: ["in", ["get", "class"], ["literal", ["rock", "sand"]]], paint: { "fill-color": C.rock } },
    { id: "landcover-ice", type: "fill", source: "omt", "source-layer": "landcover",
      filter: ["==", ["get", "class"], "ice"], paint: { "fill-color": C.ice, "fill-outline-color": "#9CC3DD" } },
    { id: "landuse-town", type: "fill", source: "omt", "source-layer": "landuse",
      filter: ["in", ["get", "class"], ["literal", ["residential", "suburb", "neighbourhood", "commercial", "industrial"]]],
      paint: { "fill-color": "#E4E1D9" } },
    { id: "hillshade", type: "hillshade", source: "hillshade",
      paint: { "hillshade-shadow-color": "#4B3B2A", "hillshade-highlight-color": "#FFFDF6",
               "hillshade-accent-color": "#5E4B36", "hillshade-exaggeration": 0.42 } },
    { id: "water", type: "fill", source: "omt", "source-layer": "water",
      paint: { "fill-color": C.water, "fill-outline-color": C.waterLine } },
    { id: "waterway", type: "line", source: "omt", "source-layer": "waterway",
      paint: { "line-color": C.waterLine, "line-width": lineW(0.5, 2) } },
    { id: "park-edge", type: "line", source: "omt", "source-layer": "park",
      paint: { "line-color": "#4F7A4A", "line-opacity": 0.55, "line-width": 1.2, "line-dasharray": [3, 2] } },
  ];
  if (demSource) {
    layers.push(
      { id: "contour-lines", type: "line", source: "contours", "source-layer": "contours",
        paint: { "line-color": C.sepia, "line-opacity": ["match", ["get", "level"], 1, 0.55, 0.3],
                 "line-width": ["match", ["get", "level"], 1, 1, 0.55] } },
      { id: "contour-labels", type: "symbol", source: "contours", "source-layer": "contours",
        filter: [">", ["get", "level"], 0],
        layout: { "symbol-placement": "line", "text-size": 10, "text-field": ["concat", ["number-format", ["get", "ele"], {}], ""],
                  "text-font": ["Noto Sans Regular"], "symbol-spacing": 320 },
        paint: { "text-color": "#80562F", "text-halo-color": C.paper, "text-halo-width": 1.4 } },
    );
  }
  layers.push(
    { id: "roads-major-casing", type: "line", source: "omt", "source-layer": "transportation",
      filter: ["in", ["get", "class"], ["literal", ["motorway", "trunk", "primary", "secondary"]]],
      paint: { "line-color": "#B79A57", "line-width": lineW(1.6, 7) } },
    { id: "roads-major", type: "line", source: "omt", "source-layer": "transportation",
      filter: ["in", ["get", "class"], ["literal", ["motorway", "trunk", "primary", "secondary"]]],
      paint: { "line-color": "#F4E3AE", "line-width": lineW(1, 5.5) } },
    { id: "roads-minor", type: "line", source: "omt", "source-layer": "transportation", minzoom: 11,
      filter: ["in", ["get", "class"], ["literal", ["tertiary", "minor", "service"]]],
      paint: { "line-color": "#FFFFFF", "line-width": lineW(0.6, 4) } },
    { id: "tracks", type: "line", source: "omt", "source-layer": "transportation", minzoom: 11,
      filter: ["==", ["get", "class"], "track"],
      paint: { "line-color": C.path, "line-width": lineW(0.5, 1.6), "line-dasharray": [4, 1.5] } },
    { id: "paths", type: "line", source: "omt", "source-layer": "transportation", minzoom: 11,
      filter: ["==", ["get", "class"], "path"],
      paint: { "line-color": C.path, "line-width": lineW(0.5, 1.5), "line-dasharray": [2, 1.6] } },
    { id: "buildings", type: "fill", source: "omt", "source-layer": "building", minzoom: 14,
      paint: { "fill-color": "#CFC8BC" } },
    { id: "boundary", type: "line", source: "omt", "source-layer": "boundary",
      filter: ["==", ["get", "admin_level"], 2],
      paint: { "line-color": "#7A6E8A", "line-width": 1.4, "line-dasharray": [4, 2, 1, 2] } },
    { id: "water-names", type: "symbol", source: "omt", "source-layer": "water_name",
      layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Italic"], "text-size": 12 },
      paint: { "text-color": "#2F6A97", "text-halo-color": C.paper, "text-halo-width": 1 } },
    { id: "peaks-dot", type: "circle", source: "omt", "source-layer": "mountain_peak", minzoom: 10,
      filter: ["in", ["get", "class"], ["literal", ["peak", "volcano"]]],
      paint: { "circle-radius": 2.2, "circle-color": "#5A3E25" } },
    { id: "peaks", type: "symbol", source: "omt", "source-layer": "mountain_peak", minzoom: 10,
      filter: ["in", ["get", "class"], ["literal", ["peak", "volcano"]]],
      layout: { "text-field": ["case", ["has", "ele"],
                               ["format", ["get", "name"], {}, "\n", {}, ["concat", ["to-string", ["get", "ele"]], " m"], { "font-scale": 0.85 }],
                               ["get", "name"]],
                "text-font": ["Noto Sans Regular"], "text-size": 11.5, "text-anchor": "top", "text-offset": [0, 0.4],
                "text-max-width": 8 },
      paint: { "text-color": "#5A3E25", "text-halo-color": C.paper, "text-halo-width": 1.4 } },
    { id: "places", type: "symbol", source: "omt", "source-layer": "place",
      filter: ["in", ["get", "class"], ["literal", ["city", "town", "village", "hamlet"]]],
      layout: { "text-field": ["get", "name"], "text-font": ["Noto Sans Regular"],
                "text-size": ["match", ["get", "class"], "city", 16, "town", 14, "village", 12.5, 11] },
      paint: { "text-color": C.ink, "text-halo-color": C.paper, "text-halo-width": 1.6 } },
  );
  return {
    version: 8, glyphs: GLYPHS, sources, layers,
    sky: { "sky-color": "#C9D8E2", "horizon-color": "#EEF0E8", "fog-color": "#EEF0E8",
           "sky-horizon-blend": 0.6, "horizon-fog-blend": 0.6, "fog-ground-blend": 0.35, "atmosphere-blend": 0 },
  };
}

const map = new maplibregl.Map({
  container: "map", style: mapStyle(), center: [6.87, 45.88], zoom: 9.6, pitch: 58, bearing: -18,
  maxPitch: 78, attributionControl: false, hash: false,
});
map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
map.on("click", (e) => {
  const which = state.picking;
  if (!which) return;
  e.originalEvent.ihikePicked = true;
  const value = `${e.lngLat.lat.toFixed(5)}, ${e.lngLat.lng.toFixed(5)}`;
  stopPicking();
  if (which === "start") {
    form.start.value = value;
    state.startName = "the point you picked";
  } else {
    form.destination.value = value;
    if (form.mode.value === "ideas") form.querySelector('input[name=mode][value="out_and_back"]').checked = true;
    syncMode();
  }
  state.pickMarkers[which]?.remove();
  state.pickMarkers[which] = new maplibregl.Marker({ element: h("div", { class: "pick-pin", title: which === "start" ? "Your start" : "Your destination" }) })
    .setLngLat(e.lngLat).addTo(map);
  showForm();
  toast(which === "start" ? "Start placed. Press Find hikes when you are ready." : "Destination placed.");
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true, showZoom: true }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

const empty = { type: "FeatureCollection", features: [] };
map.on("load", () => {
  map.setTerrain({ source: "terrain", exaggeration: 1.25 });
  map.addSource("routes", { type: "geojson", data: empty });
  map.addSource("route-active", { type: "geojson", data: empty });
  map.addSource("community", { type: "geojson", data: empty });
  map.addLayer({ id: "community-line", type: "line", source: "community",
                 paint: { "line-color": "#1D2B2A", "line-width": 2.4, "line-dasharray": [2, 1.2], "line-opacity": 0.8 } });
  map.addLayer({ id: "routes-casing", type: "line", source: "routes", layout: { "line-join": "round", "line-cap": "round" },
                 paint: { "line-color": "#FFFFFF", "line-width": ["case", ["get", "dim"], 5, 8], "line-opacity": ["case", ["get", "dim"], 0.5, 0.95] } });
  map.addLayer({ id: "routes-line", type: "line", source: "routes", layout: { "line-join": "round", "line-cap": "round" },
                 paint: { "line-color": ["get", "color"], "line-width": ["case", ["get", "dim"], 2.6, 4],
                          "line-opacity": ["case", ["get", "dim"], 0.55, 1] } });
  map.addLayer({ id: "route-active-casing", type: "line", source: "route-active", layout: { "line-join": "round", "line-cap": "round" },
                 paint: { "line-color": "#FFFFFF", "line-width": 9 } });
  map.addLayer({ id: "route-active", type: "line", source: "route-active", layout: { "line-join": "round", "line-cap": "round" },
                 paint: { "line-color": ["get", "color"], "line-width": 5 } });

  map.on("mouseenter", "routes-line", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "routes-line", () => { map.getCanvas().style.cursor = ""; });
  map.on("click", "routes-line", (e) => {
    if (e.originalEvent.ihikePicked) return;
    const id = e.features[0]?.properties?.id;
    if (id && state.result) selectHike(id);
  });
  map.on("mousemove", "route-active", (e) => {
    if (!state.profile || state.flight) return;
    setCursor(nearestIndex(state.profile.geom, [e.lngLat.lng, e.lngLat.lat]));
  });
  map.on("mouseleave", "route-active", () => { if (!state.flight) clearCursor(); });
  map.on("click", "community-line", (e) => {
    const id = e.features[0]?.properties?.id;
    const el = id && document.getElementById(`c-${id}`);
    if (el) { el.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" }); el.focus(); }
  });
});

map.on("error", (e) => {
  const msg = String(e?.error?.message || "");
  if (!/Failed to fetch|NetworkError|404|AJAXError/.test(msg)) console.warn("Map:", msg);
});

// Map tools
$("#tool-3d").addEventListener("click", (e) => {
  state.terrain = !state.terrain;
  e.currentTarget.setAttribute("aria-pressed", String(state.terrain));
  map.setTerrain(state.terrain ? { source: "terrain", exaggeration: 1.25 } : null);
  map.easeTo({ pitch: state.terrain ? 55 : 0, duration: reducedMotion ? 0 : 900 });
});
$("#tool-contours").addEventListener("click", (e) => {
  state.contours = !state.contours;
  e.currentTarget.setAttribute("aria-pressed", String(state.contours));
  ["contour-lines", "contour-labels"].forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", state.contours ? "visible" : "none"));
});
$("#tool-fit").addEventListener("click", () => fitAll());

function padding() {
  const profileOpen = !$("#profile").hidden;
  return { top: 60, left: 50, right: 80, bottom: profileOpen ? 250 : 60 };
}
/** Frame coordinates seen from above, then tilt: tilting around the centre keeps the
    route centred even with the 3D relief lifting it off the flat map. */
function frame(coords, maxZoom = 14) {
  if (!coords.length) return;
  const dur = reducedMotion ? 0 : 1100;
  map.fitBounds(bounds(coords), { padding: padding(), maxZoom, pitch: 0, duration: dur });
  // Tilting makes the near half of the frame grow, so step back a little while tilting.
  if (state.terrain) map.once("moveend", () => map.easeTo({ pitch: 48, zoom: map.getZoom() - 0.45, duration: reducedMotion ? 0 : 900 }));
}
function fitAll() {
  if (state.view === "community" && state.community?.length) {
    frame(state.community.flatMap((hk) => hk.path.map(([la, lo]) => [lo, la])), 13);
    return;
  }
  if (!state.result) return;
  frame(state.result.hikes.flatMap((hk) => hk.geometry), 14);
}

// ------------------------------------------------------------------- form
const form = $("#plan-form");
function buildDays() {
  const box = $("#days");
  const today = new Date();
  for (let i = 0; i < 7; i++) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() + i);
    const top = i === 0 ? "Today" : d.toLocaleDateString("en-GB", { weekday: "short" });
    box.append(h("label", {},
      h("input", { type: "radio", name: "day", value: i, checked: i === 0, "aria-label": d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" }) }),
      h("span", {}, top, h("b", {}, d.getDate()))));
  }
}
buildDays();

function syncMode() {
  const mode = form.mode.value;
  $("#dest-field").hidden = mode === "ideas";
  $("#destination").required = mode !== "ideas";
  form.start.required = mode === "ideas";
  form.start.placeholder = mode === "ideas" ? "Chamonix, or 45.9237, 6.8694" : "Optional: leave empty and iHike picks car parks";
}
form.addEventListener("change", (e) => { if (e.target.name === "mode") syncMode(); });
$("#hours").addEventListener("input", (e) => { $("#hours-out").textContent = `${e.target.value} h`; });

function readForm() {
  return {
    start: form.start.value.trim(), mode: form.mode.value, destination: form.destination.value.trim(),
    level: form.level.value, activity: form.activity.value, hours: Number(form.hours.value),
    day: Number(form.day.value || 0), kinds: $("#kinds").value ? $("#kinds").value.split(",") : [],
  };
}
function applyParams(p) {
  if (p.start) form.start.value = p.start;
  if (p.destination) form.destination.value = p.destination;
  if (p.mode) form.querySelector(`input[name=mode][value="${p.mode}"]`).checked = true;
  if (p.level) form.querySelector(`input[name=level][value="${p.level}"]`).checked = true;
  if (p.activity) form.querySelector(`input[name=activity][value="${p.activity}"]`).checked = true;
  if (p.hours) { form.hours.value = p.hours; $("#hours-out").textContent = `${form.hours.value} h`; }
  if (p.day != null) { const r = form.querySelector(`input[name=day][value="${p.day}"]`); if (r) r.checked = true; }
  $("#kinds").value = (p.kinds || []).join(",");
  syncMode();
}

form.addEventListener("submit", (e) => { e.preventDefault(); runPlan(); });

function startPlace(r) {
  return state.startName || (r.start?.label || form.start.value).split(",")[0];
}
function showForm() {
  form.hidden = false;
  $("#plan-summary").hidden = true;
}
function startPicking(which) {
  state.picking = which;
  map.getCanvas().style.cursor = "crosshair";
  $$(".pick-btn").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.pick === which)));
  toast(which === "start" ? "Click the map where you want to start. Esc to cancel." : "Click the map where you want to go. Esc to cancel.", 8000);
}
function stopPicking() {
  state.picking = null;
  map.getCanvas().style.cursor = "";
  $$(".pick-btn").forEach((b) => b.setAttribute("aria-pressed", "false"));
}
$$(".pick-btn").forEach((b) => b.addEventListener("click", () => (state.picking === b.dataset.pick ? stopPicking() : startPicking(b.dataset.pick))));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.picking) { stopPicking(); toast("Cancelled.", 1500); } });
form.start.addEventListener("input", () => { state.startName = null; });

function collapseForm(p, r) {
  const where = r.auto_start ? "the car parks around it" : startPlace(r);
  const dest = r.end ? (r.end.label || p.destination).split(",")[0] : null;
  const day = Number(p.day) === 0 ? "today" : Number(p.day) === 1 ? "tomorrow" : `in ${p.day} days`;
  $("#plan-summary-text").replaceChildren(
    dest ? "To " : "From ", h("b", {}, dest || where), dest ? ` from ${where}` : "",
    `, ${p.hours} h, ${LEVEL_NAME[p.level]}, ${day}`);
  form.hidden = true;
  $("#plan-summary").hidden = false;
}
$("#edit-search").addEventListener("click", () => {
  form.hidden = false;
  $("#plan-summary").hidden = true;
  form.start.focus();
});

// ------------------------------------------------------------ status line
const contourIcon = () => h("svg", { viewBox: "0 0 64 48", "aria-hidden": "true" },
  h("path", { d: "M2 40c10-6 18-8 30-6s20 1 30-6" }), h("path", { d: "M8 30c8-6 14-8 24-6s16 0 24-6" }),
  h("path", { d: "M16 21c6-5 10-6 17-4s11 0 15-5" }), h("path", { d: "M24 13c4-3 7-4 10-3s6 0 8-3" }));
let statusTimer;
function showStatus(lines) {
  const el = $("#status");
  el.className = "status";
  el.hidden = false;
  let i = 0;
  const p = h("p", {}, lines[0]);
  el.replaceChildren(contourIcon(), p);
  clearInterval(statusTimer);
  statusTimer = setInterval(() => { i = Math.min(i + 1, lines.length - 1); p.textContent = lines[i]; }, 2600);
}
function showError(message) {
  clearInterval(statusTimer);
  const el = $("#status");
  el.className = "status error";
  el.hidden = false;
  el.replaceChildren(h("p", {}, message));
}
function hideStatus() { clearInterval(statusTimer); $("#status").hidden = true; }

// --------------------------------------------------------------- planning
async function runPlan() {
  const params = readForm();
  if (!params.start && params.mode === "ideas") { form.start.focus(); return; }
  if (params.mode !== "ideas" && !params.destination) { form.destination.focus(); return; }
  stopFlight();
  $("#find").disabled = true;
  $("#detail").hidden = true;
  $("#results").replaceChildren();
  showStatus(params.mode === "ideas"
    ? ["Finding your start point", "Reading the map for summits, lakes and huts", "Routing each idea on real paths", "Checking the weather up high"]
    : params.start ? ["Finding both places", "Routing on real paths", "Checking the weather up high"]
      : ["Finding your destination", "Looking for car parks and trailheads around it", "Routing from each of them", "Checking the weather up high"]);
  try {
    const result = await api("/api/plan", { json: params });
    hideStatus();
    state.result = result;
    $("#intro").hidden = true;
    collapseForm(params, result);
    Object.values(state.pickMarkers).forEach((m) => m.remove());
    state.pickMarkers = {};
    renderResults();
    drawRoutes();
    fitAll();
    loadStarts(result);
  } catch (err) {
    showError(err.message);
  } finally {
    $("#find").disabled = false;
  }
}

function waymark(sac, terrain, long = false) {
  const label = long ? terrain.replace(/^not graded on the map$/, "Path not graded on the map") : (sac ? `T${sac}` : "Ungraded");
  return h("span", { class: `waymark t${sac || 0}`, title: terrain }, h("i", { "aria-hidden": "true" }), label);
}
function sparkline(geom) {
  const W = 300, H = 30;
  const d = cumulative(geom), total = d[d.length - 1] || 1;
  const eles = geom.map((p) => p[2] ?? 0);
  const lo = Math.min(...eles), hi = Math.max(...eles), span = Math.max(hi - lo, 60);
  const step = Math.max(1, Math.floor(geom.length / 120));
  let line = "";
  for (let i = 0; i < geom.length; i += step) {
    const x = (d[i] / total) * W, y = H - 2 - ((eles[i] - lo) / span) * (H - 4);
    line += `${line ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }
  return h("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" },
    h("path", { class: "area", d: `${line}L${W},${H}L0,${H}Z` }), h("path", { class: "line", d: line }));
}
const contourRule = () => h("svg", { class: "contour-rule", viewBox: "0 0 400 10", preserveAspectRatio: "none", "aria-hidden": "true" },
  h("path", { d: "M0 6 C40 1 70 9 110 5 S180 2 220 6 S300 9 340 4 S385 3 400 6", fill: "none", stroke: "currentColor", "stroke-width": "1" }));

function renderResults() {
  const r = state.result;
  const box = $("#results");
  const place = startPlace(r);
  const dest = (r.end?.label || "").split(",")[0];
  const title = r.auto_start ? `${r.hikes.length} ${r.hikes.length === 1 ? "way" : "ways"} to ${dest}`
    : r.mode === "ideas"
    ? (r.ideas ? `${r.hikes.length} ${r.hikes.length === 1 ? "idea" : "ideas"} from ${place}` : `Loops from ${place}`)
    : `To ${(r.end?.label || "").split(",")[0]}`;
  const head = h("div", { class: "results-head" }, h("h2", {}, title));
  const nodes = [head];
  if (r.auto_start) nodes.push(h("p", { class: "source" }, "No start given, so each way starts at a car park or trailhead found on the map, from the easiest to the hardest."));
  if (r.start.others?.length && !state.startName) nodes.push(h("p", { class: "source" }, `Start read as ${r.start.label}. Not it? Pick a car park below or place the start on the map.`));
  if (r.note) nodes.push(h("p", { class: "note" }, r.note));
  if (r.hint) nodes.push(h("p", { class: "note" }, r.hint));
  if (r.start_offset_m > 300) nodes.push(h("p", { class: "note" }, `The nearest mapped path is about ${Math.round(r.start_offset_m / 10) * 10} m from your start. Routes begin there.`));
  if (r.no_match) nodes.push(h("p", { class: "note" }, `None of these suits a ${LEVEL_NAME[r.level]}. Try a shorter time, a lower goal or another start.`));
  if (r.weather) {
    nodes.push(h("p", { class: "source" }, `${dayName(r.weather.date)}: ${r.weather.summary.toLowerCase()}, ${Math.round(r.weather.tmin)} to ${Math.round(r.weather.tmax)} °C ${r.weather.where}.`
      + (r.alerts.length ? ` ${r.alerts.length} ${r.alerts.length === 1 ? "alert" : "alerts"} in each hike's details.` : "")));
  }
  nodes.push(h("details", { class: "starts", id: "starts", hidden: true },
    h("summary", {}, "Looking for car parks and trailheads nearby…")));
  nodes.push(contourRule());
  const list = h("ul", { class: "hike-list" });
  r.hikes.forEach((hk) => {
    const li = h("li", { class: "hike", tabindex: 0, "data-id": hk.id, "--route": hk.color,
                         onclick: () => selectHike(hk.id),
                         onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectHike(hk.id); } },
                         onmouseenter: () => highlight(hk.id), onmouseleave: () => highlight(null) },
      h("span", { class: "letter", "aria-hidden": "true" }, hk.id),
      h("h3", { class: "hike-title" }, hk.title),
      h("p", { class: "hike-kind" },
        h("span", {}, hk.kind),
        waymark(hk.sac, hk.terrain),
        hk.recommended ? h("span", { class: "pick" }, "Best match") : null,
        !hk.suits ? h("span", { class: "unsuited" }, "Beyond your level") : null),
      h("dl", { class: "hike-figures" },
        h("div", {}, h("dt", {}, "distance"), h("dd", {}, fmtKm(hk.distance_km), h("small", {}, "km"))),
        h("div", {}, h("dt", {}, "climb"), h("dd", {}, fmtM(hk.ascent_m), h("small", {}, "m"))),
        h("div", {}, h("dt", {}, "walking time"), h("dd", {}, fmtH(hk.hours))),
        h("div", {}, h("dt", {}, "grade"), h("dd", {}, hk.grade))),
      hk.geometry[0]?.length > 2 ? sparkline(hk.geometry) : null);
    list.append(li);
  });
  nodes.push(list);
  if (r.trails.length) {
    nodes.push(h("section", { class: "block", style: { marginTop: "26px" } }, h("h3", {}, "Waymarked trails passing here"),
      h("ul", { class: "plain-list" }, r.trails.map((t) => h("li", {},
        h("a", { href: t.url, target: "_blank", rel: "noopener" }, t.name), h("span", {}, t.network ? `${t.network} trail` : ""))))));
  }
  if (r.nearby.length) {
    nodes.push(h("section", { class: "block" }, h("h3", {}, "Shared by people nearby"),
      h("ul", { class: "plain-list" }, r.nearby.map((n) => h("li", {}, n.title, h("span", {}, `${fmtKm(n.distance_km)} km, ${n.away_km} km away`))))));
  }
  box.replaceChildren(...nodes);
  box.hidden = false;
}

function dayName(iso) {
  if (!iso) return "That day";
  const d = new Date(`${iso}T12:00:00`);
  const today = new Date(); today.setHours(12, 0, 0, 0);
  const diff = Math.round((d - today) / 864e5);
  return diff === 0 ? "Today" : diff === 1 ? "Tomorrow" : d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
}

// ------------------------------------------------- car parks and trailheads
const sign = (kind) => h("span", { class: `sign ${kind}`, "aria-hidden": "true" }, kind === "parking" ? "P" : "");
function chooseStart(p) {
  form.start.value = `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}`;
  state.startName = p.named ? p.name : `${p.kind === "trailhead" ? "the trailhead" : "the car park"}${p.ele ? ` at ${fmtM(p.ele)} m` : ""}`;
  runPlan();
}
async function loadStarts(r) {
  clearMarkers(state.placeMarkers);
  const box = $("#starts");
  if (!box) return;
  if (r.auto_start) { box.remove(); return; } // the ways already start from car parks
  let places;
  try {
    ({ places } = await api(`/api/trailheads?lat=${r.start.lat}&lon=${r.start.lon}`));
  } catch {
    box.remove();
    return;
  }
  if (state.result !== r) return; // a newer search replaced this one
  if (!places.length) { box.remove(); return; }
  const here = (p) => Math.abs(p.lat - r.start.lat) < 2e-4 && Math.abs(p.lon - r.start.lon) < 2e-4;
  box.replaceChildren(
    h("summary", {}, places.some(here) ? `Starting from ${state.startName || "a car park"}. ${places.length - 1} other places to leave the car`
                                       : `Not the right start? ${places.length} car parks and trailheads nearby`),
    h("ul", {}, places.map((p) => {
      const facts = [p.ele != null ? `${fmtM(p.ele)} m` : null, `${p.away_km} km away`,
                     p.fee === true ? "paid" : p.fee === false ? "free" : null, p.capacity ? `${p.capacity} spaces` : null].filter(Boolean);
      return h("li", {}, sign(p.kind),
        here(p) ? h("span", { class: "start-choice" }, p.name, h("small", {}, h("span", { class: "current" }, "Current start"), ` ${facts.join(", ")}`))
                : h("button", { class: "start-choice", onclick: () => chooseStart(p) }, p.name, h("small", {}, facts.join(", "))),
        h("a", { href: gmapsDirections(p.lat, p.lon), target: "_blank", rel: "noopener", title: "Driving directions in Google Maps" }, "Directions"));
    })));
  box.hidden = false;
  places.forEach((p) => {
    const el = h("div", { class: `place-pin${here(p) ? " current" : ""}`, title: `${p.name}${p.ele != null ? `, ${fmtM(p.ele)} m` : ""}. Click to start here.`,
                          onclick: (e) => { e.stopPropagation(); if (!here(p)) chooseStart(p); } }, sign(p.kind));
    const m = new maplibregl.Marker({ element: el }).setLngLat([p.lon, p.lat]).addTo(map);
    if (state.view !== "plan") el.style.display = "none";
    state.placeMarkers.push(m);
  });
}

// ------------------------------------------------------------ map drawing
function clearMarkers(list) { list.forEach((m) => m.remove()); list.length = 0; }

function drawRoutes() {
  const r = state.result;
  if (!map.getSource("routes")) { map.once("load", drawRoutes); return; }
  map.getSource("routes").setData({ type: "FeatureCollection", features: r.hikes.map((hk) => ({
    type: "Feature", properties: { id: hk.id, color: hk.color, dim: false },
    geometry: { type: "LineString", coordinates: hk.geometry.map((p) => [p[0], p[1]]) } })) });
  map.getSource("route-active").setData(empty);
  clearMarkers(state.markers);
  const starts = new Set();
  r.hikes.forEach((hk) => {
    const sp = hk.start_point ? [hk.start_point.lon, hk.start_point.lat] : hk.geometry[0].slice(0, 2);
    const key = sp.map((v) => v.toFixed(4)).join();
    if (starts.has(key)) return;
    starts.add(key);
    const el = h("div", { class: "start-pin", title: hk.start_point ? `Start: ${hk.start_point.label}` : "Start" });
    state.markers.push(new maplibregl.Marker({ element: el }).setLngLat(sp).addTo(map));
  });
  const seen = new Set();
  r.hikes.forEach((hk) => {
    if (!hk.far) return;
    const key = `${hk.far.lat.toFixed(4)},${hk.far.lon.toFixed(4)}`;
    if (seen.has(key)) return;
    seen.add(key);
    const el = h("div", { class: "goal-pin", "data-id": hk.id, "--route": hk.color, title: hk.title,
                          onclick: (e) => { e.stopPropagation(); selectHike(hk.id); } },
      h("div", { class: "tag" }, h("b", {}, hk.id), h("span", {}, hk.far.label || hk.title)),
      h("div", { class: "stem" }), h("div", { class: "foot" }));
    state.markers.push(new maplibregl.Marker({ element: el, anchor: "bottom" }).setLngLat([hk.far.lon, hk.far.lat]).addTo(map));
  });
}

function highlight(id) {
  if (!state.result || state.selected) return;
  const src = map.getSource("routes");
  if (!src) return;
  src.setData({ type: "FeatureCollection", features: state.result.hikes.map((hk) => ({
    type: "Feature", properties: { id: hk.id, color: hk.color, dim: Boolean(id && hk.id !== id) },
    geometry: { type: "LineString", coordinates: hk.geometry.map((p) => [p[0], p[1]]) } })) });
  $$(".goal-pin").forEach((p) => p.classList.toggle("dim", Boolean(id && p.dataset.id !== id)));
}

let drawAnim;
function animateRoute(hk) {
  cancelAnimationFrame(drawAnim);
  const coords = hk.geometry.map((p) => [p[0], p[1]]);
  const src = map.getSource("route-active");
  const set = (n) => src.setData({ type: "Feature", properties: { color: hk.color }, geometry: { type: "LineString", coordinates: coords.slice(0, Math.max(2, n)) } });
  if (reducedMotion) { set(coords.length); return; }
  const t0 = performance.now(), dur = 1300;
  const step = (now) => {
    const k = Math.min(1, (now - t0) / dur);
    set(Math.ceil(coords.length * (1 - (1 - k) ** 3)));
    if (k < 1) drawAnim = requestAnimationFrame(step);
  };
  drawAnim = requestAnimationFrame(step);
}

// ----------------------------------------------------------------- detail
function selectHike(id) {
  const r = state.result;
  const hk = r?.hikes.find((x) => x.id === id);
  if (!hk) return;
  stopFlight();
  state.selected = hk;
  map.getSource("routes")?.setData({ type: "FeatureCollection", features: r.hikes.map((x) => ({
    type: "Feature", properties: { id: x.id, color: x.color, dim: true },
    geometry: { type: "LineString", coordinates: x.geometry.map((p) => [p[0], p[1]]) } })) });
  $$(".goal-pin").forEach((p) => p.classList.toggle("dim", p.dataset.id !== id));
  animateRoute(hk);
  renderDetail(hk);
  showProfile(hk);
  frame(hk.geometry, 14.5);
}

function backToList() {
  stopFlight();
  state.selected = null;
  $("#detail").hidden = true;
  $("#results").hidden = false;
  hideProfile();
  map.getSource("route-active")?.setData(empty);
  highlight(null);
  fitAll();
}

const WEATHER_ICONS = {
  sun: "M20 12a8 8 0 1 0 0 .01M20 2v4M20 34v4M2 20h4M34 20h4M7 7l3 3M30 30l3 3M7 33l3-3M30 10l3-3",
  part: "M14 10a6 6 0 0 1 11 2M13 4v2M5 12h2M7 6l1.5 1.5M10 30h20a6 6 0 0 0 0-12 9 9 0 0 0-17 2 5 5 0 0 0-3 10z",
  cloud: "M9 30h22a7 7 0 0 0 0-14 10 10 0 0 0-19 2 6 6 0 0 0-3 12z",
  rain: "M9 24h22a7 7 0 0 0 0-14 10 10 0 0 0-19 2 6 6 0 0 0-3 12zM13 29l-2 5M20 29l-2 5M27 29l-2 5",
  snow: "M9 24h22a7 7 0 0 0 0-14 10 10 0 0 0-19 2 6 6 0 0 0-3 12zM13 31h.1M20 33h.1M27 31h.1M16 36h.1M24 36h.1",
  storm: "M9 24h22a7 7 0 0 0 0-14 10 10 0 0 0-19 2 6 6 0 0 0-3 12zM21 26l-4 6h6l-4 6",
};
function weatherIcon(code) {
  const kind = [95, 96, 99].includes(code) ? "storm" : [71, 73, 75, 77, 85, 86].includes(code) ? "snow"
    : code >= 51 ? "rain" : [3, 45, 48].includes(code) ? "cloud" : code === 2 ? "part" : "sun";
  return h("svg", { viewBox: "0 0 40 40", "aria-hidden": "true" }, h("path", { d: WEATHER_ICONS[kind] }));
}

function renderDetail(hk) {
  const r = state.result;
  const w = r.weather;
  const box = $("#detail");
  const nodes = [
    h("button", { class: "back", onclick: backToList },
      h("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, h("path", { d: "M15 5l-7 7 7 7" })),
      r.hikes.length > 1 ? `All ${r.hikes.length} ${r.mode === "ideas" ? "ideas" : "routes"}` : "Back"),
    h("h2", { class: "detail-title", "--route": hk.color }, hk.title),
    h("p", { class: "detail-kind" }, h("span", {}, hk.kind), waymark(hk.sac, hk.terrain, true),
      hk.recommended ? h("span", { class: "pick" }, "Best match for you") : null,
      !hk.suits ? h("span", { class: "unsuited" }, `Beyond a ${LEVEL_NAME[r.level]}'s level`) : null),
    h("dl", { class: "big-figures" },
      h("div", {}, h("dt", {}, "distance"), h("dd", {}, fmtKm(hk.distance_km), h("small", {}, "km"))),
      h("div", {}, h("dt", {}, "climb"), h("dd", {}, fmtM(hk.ascent_m), h("small", {}, "m"))),
      h("div", {}, h("dt", {}, "walking time"), h("dd", {}, fmtH(hk.hours))),
      h("div", {}, h("dt", {}, "highest point"), h("dd", {}, fmtM(hk.max_ele), h("small", {}, "m")))),
    h("div", { class: "actions" },
      h("button", { class: "primary", id: "fly", onclick: () => toggleFlight(hk) },
        h("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, h("path", { d: "M8 5v14l11-7z" })), "Fly over the route"),
      h("button", { class: "secondary", onclick: () => downloadGpx(hk), title: "Download the route as a GPX file" }, "GPX file"),
      h("button", { class: "secondary", onclick: () => openShare(hk) }, "Share"),
      h("a", { class: "secondary", href: gmapsDirections((hk.start_point || r.start).lat, (hk.start_point || r.start).lon), target: "_blank", rel: "noopener",
               title: "Driving directions to the start, in Google Maps" }, "Drive there")),
  ];
  if (w) {
    nodes.push(h("section", { class: "block" }, h("h3", {}, `${dayName(w.date)}, ${w.where}`),
      h("div", { class: "weather" }, weatherIcon(w.code),
        h("div", { class: "w-main" }, `${w.summary}, ${Math.round(w.tmin)} to ${Math.round(w.tmax)} °C`),
        h("div", { class: "w-sub" },
          h("span", {}, `Rain ${w.precip_mm} mm (${w.precip_prob} %)`), h("span", {}, `Gusts ${Math.round(w.gusts)} km/h`),
          h("span", {}, `Sunrise ${w.sunrise}`), h("span", {}, `Sunset ${w.sunset}`)))));
  } else {
    nodes.push(h("p", { class: "note" }, "The forecast is unavailable right now. Check it yourself before leaving."));
  }
  if (r.alerts.length) {
    nodes.push(h("section", { class: "block" }, h("h3", {}, "Before you go"),
      h("ul", { class: "alerts" }, r.alerts.map((a) => h("li", { class: a.severity }, h("b", {}, a.title), a.detail ? h("span", {}, a.detail) : null)))));
  }
  nodes.push(h("section", { class: "block" }, h("h3", {}, "Briefing"),
    h("p", { class: "briefing" }, r.briefing.text),
    h("p", { class: "source" }, r.briefing.ai ? "Written by a language model from the figures above." : "Written from the figures above.")));
  box.replaceChildren(...nodes);
  $("#results").hidden = true;
  box.hidden = false;
  box.scrollIntoView?.({ block: "start" });
  $(".panel").scrollTop = 0;
}

// ------------------------------------------------------------- GPX export
function downloadGpx(hk) {
  const esc = (s) => String(s).replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;" }[c]));
  const pts = hk.geometry.map((p) => `<trkpt lat="${p[1].toFixed(6)}" lon="${p[0].toFixed(6)}">${p[2] != null ? `<ele>${p[2].toFixed(1)}</ele>` : ""}</trkpt>`).join("");
  const xml = `<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="iHike" xmlns="http://www.topografix.com/GPX/1/1"><trk><name>${esc(hk.title)}</name><trkseg>${pts}</trkseg></trk></gpx>`;
  const a = h("a", { href: URL.createObjectURL(new Blob([xml], { type: "application/gpx+xml" })),
                     download: `${hk.title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "hike"}.gpx` });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

// ---------------------------------------------------------------- profile
function slopeClass(g) { const a = Math.abs(g); return a < 0.08 ? 0 : a < 0.15 ? 1 : a < 0.25 ? 2 : 3; }
const SLOPE_FILL = ["#D7DFC9", "#E3C892", "#CF8F52", "#A9432F"];

function showProfile(hk) {
  if (!(hk.geometry[0]?.length > 2)) { hideProfile(); return; }
  const geom = hk.geometry;
  const dist = cumulative(geom);
  const eles = geom.map((p) => p[2]);
  // slope over a ~120 m window, so GPS noise does not paint everything red
  const slopes = geom.map((_, i) => {
    let j = i; while (j < geom.length - 1 && dist[j] - dist[i] < 0.12) j++;
    let k = i; while (k > 0 && dist[i] - dist[k] < 0.06 && j === geom.length - 1) k--;
    const dd = (dist[j] - dist[k]) * 1000;
    return dd > 0 ? (eles[j] - eles[k]) / dd : 0;
  });
  state.profile = { hk, geom, dist, eles, slopes };
  $("#profile-title").textContent = hk.title;
  $("#profile-readout").textContent = `${fmtKm(hk.distance_km)} km, ${fmtM(hk.ascent_m)} m of climbing`;
  $("#profile").hidden = false;
  drawProfile();
}
function hideProfile() { $("#profile").hidden = true; state.profile = null; clearCursor(); }

function drawProfile() {
  const P = state.profile;
  if (!P) return;
  const svg = $("#profile-svg");
  const W = svg.clientWidth || 600, H = svg.clientHeight || 140;
  const m = { l: 56, r: 14, t: 8, b: 20 };
  const total = P.dist[P.dist.length - 1] || 1;
  const lo = Math.min(...P.eles), hi = Math.max(...P.eles);
  const pad = Math.max(30, (hi - lo) * 0.1);
  const y0 = lo - pad, y1 = hi + pad;
  const X = (d) => m.l + (d / total) * (W - m.l - m.r);
  const Y = (e) => m.t + (1 - (e - y0) / (y1 - y0)) * (H - m.t - m.b);
  P.X = X; P.Y = Y; P.W = W; P.H = H; P.m = m; P.total = total;
  const base = H - m.b;
  const parts = [];
  // grid + axes
  const g = h("g", { class: "grid" }), ax = h("g", { class: "axis" });
  const stepE = [50, 100, 200, 250, 500, 1000].find((s) => (y1 - y0) / s <= 4) || 1000;
  for (let e = Math.ceil(y0 / stepE) * stepE; e <= y1; e += stepE) {
    g.append(h("line", { x1: m.l, x2: W - m.r, y1: Y(e), y2: Y(e) }));
    ax.append(h("text", { x: m.l - 6, y: Y(e) + 4, "text-anchor": "end" }, `${e} m`));
  }
  const stepD = [0.5, 1, 2, 5, 10, 20].find((s) => total / s <= 8) || 20;
  for (let d = 0; d <= total + 1e-9; d += stepD) ax.append(h("text", { x: X(d), y: H - 5, "text-anchor": d === 0 ? "start" : "middle" }, `${d} km`));
  parts.push(g);
  // slope-coloured fill, one polygon per run of the same class
  let i = 0;
  while (i < P.geom.length - 1) {
    const cls = slopeClass(P.slopes[i]);
    let j = i;
    while (j < P.geom.length - 1 && slopeClass(P.slopes[j]) === cls) j++;
    let d = `M${X(P.dist[i]).toFixed(1)},${base}`;
    for (let k = i; k <= j; k++) d += `L${X(P.dist[k]).toFixed(1)},${Y(P.eles[k]).toFixed(1)}`;
    d += `L${X(P.dist[j]).toFixed(1)},${base}Z`;
    parts.push(h("path", { d, fill: SLOPE_FILL[cls] }));
    i = j;
  }
  let line = "";
  P.geom.forEach((_, k) => { line += `${k ? "L" : "M"}${X(P.dist[k]).toFixed(1)},${Y(P.eles[k]).toFixed(1)}`; });
  parts.push(h("path", { class: "line", d: line }), ax);
  P.hair = h("line", { class: "hair", y1: m.t, y2: base, visibility: "hidden" });
  P.dot = h("circle", { class: "dot", r: 4.5, visibility: "hidden" });
  parts.push(P.hair, P.dot);
  svg.replaceChildren(...parts);
}

function setCursor(i) {
  const P = state.profile;
  if (!P || i == null) return;
  const x = P.X(P.dist[i]), y = P.Y(P.eles[i]);
  P.hair.setAttribute("x1", x); P.hair.setAttribute("x2", x); P.hair.setAttribute("visibility", "visible");
  P.dot.setAttribute("cx", x); P.dot.setAttribute("cy", y); P.dot.setAttribute("visibility", "visible");
  const grade = Math.round(P.slopes[i] * 100);
  $("#profile-readout").textContent = `${P.dist[i].toFixed(1)} km in, ${fmtM(P.eles[i])} m high, ${grade > 0 ? "+" : ""}${grade} % slope`;
  const ll = [P.geom[i][0], P.geom[i][1]];
  if (!state.cursor) state.cursor = new maplibregl.Marker({ element: h("div", { class: "cursor-pin" }) }).setLngLat(ll).addTo(map);
  else state.cursor.setLngLat(ll);
}
function clearCursor() {
  state.cursor?.remove(); state.cursor = null;
  const P = state.profile;
  if (P?.hair) { P.hair.setAttribute("visibility", "hidden"); P.dot.setAttribute("visibility", "hidden"); }
}
function nearestIndex(geom, ll) {
  let best = 0, bd = Infinity;
  const k = Math.cos(ll[1] * Math.PI / 180);
  geom.forEach((p, i) => { const d = ((p[0] - ll[0]) * k) ** 2 + (p[1] - ll[1]) ** 2; if (d < bd) { bd = d; best = i; } });
  return best;
}
$("#profile-svg").addEventListener("pointermove", (e) => {
  const P = state.profile;
  if (!P?.X || state.flight) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const d = ((e.clientX - rect.left - P.m.l) / (P.W - P.m.l - P.m.r)) * P.total;
  let lo = 0, hi = P.dist.length - 1;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (P.dist[mid] < d) lo = mid + 1; else hi = mid; }
  setCursor(Math.max(0, Math.min(P.dist.length - 1, lo)));
});
$("#profile-svg").addEventListener("pointerleave", () => { if (!state.flight) { clearCursor(); if (state.profile) $("#profile-readout").textContent = `${fmtKm(state.profile.hk.distance_km)} km, ${fmtM(state.profile.hk.ascent_m)} m of climbing`; } });
window.addEventListener("resize", () => { if (state.profile) drawProfile(); });

// --------------------------------------------------------------- flyover
function toggleFlight(hk) { if (state.flight) stopFlight(); else startFlight(hk); }
function startFlight(hk) {
  const P = state.profile;
  if (!P || P.hk !== hk) return;
  if (!state.terrain) $("#tool-3d").click();
  const total = P.total;
  const duration = Math.min(45, Math.max(16, total * 2.2)) * 1000;
  const btn = $("#fly");
  if (btn) btn.lastChild.textContent = "Stop the flight";
  let bearing = map.getBearing();
  const t0 = performance.now();
  const at = (d) => { // position along the route at distance d (km)
    let i = 1; while (i < P.dist.length - 1 && P.dist[i] < d) i++;
    const a = P.geom[i - 1], b = P.geom[i], seg = P.dist[i] - P.dist[i - 1] || 1, f = Math.min(1, Math.max(0, (d - P.dist[i - 1]) / seg));
    return { ll: [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f], i };
  };
  const frame = (now) => {
    const k = (now - t0) / duration;
    if (k >= 1) { stopFlight(); return; }
    const here = at(k * total), ahead = at(Math.min(total, k * total + 0.35));
    const y = Math.sin((ahead.ll[0] - here.ll[0]) * Math.PI / 180) * Math.cos(ahead.ll[1] * Math.PI / 180);
    const x = Math.cos(here.ll[1] * Math.PI / 180) * Math.sin(ahead.ll[1] * Math.PI / 180)
            - Math.sin(here.ll[1] * Math.PI / 180) * Math.cos(ahead.ll[1] * Math.PI / 180) * Math.cos((ahead.ll[0] - here.ll[0]) * Math.PI / 180);
    const target = Math.atan2(y, x) * 180 / Math.PI;
    let delta = ((target - bearing + 540) % 360) - 180;
    bearing += delta * 0.04;
    map.jumpTo({ center: here.ll, bearing, pitch: 64, zoom: 14.2 });
    setCursor(here.i);
    state.flight = requestAnimationFrame(frame);
  };
  state.flight = requestAnimationFrame(frame);
  const stopOnTouch = () => stopFlight();
  ["mousedown", "wheel", "touchstart"].forEach((ev) => map.getCanvas().addEventListener(ev, stopOnTouch, { once: true }));
}
function stopFlight() {
  if (!state.flight) return;
  cancelAnimationFrame(state.flight);
  state.flight = null;
  const btn = $("#fly");
  if (btn) btn.lastChild.textContent = "Fly over the route";
  clearCursor();
}

// ---------------------------------------------------------- ask and voice
function echo(params, source) {
  const parts = [];
  if (params.start) parts.push(["from ", params.start]);
  if (params.destination) parts.push([params.mode === "one_way" ? "one way to " : "there and back to ", params.destination]);
  if (params.hours) parts.push(["", `${params.hours} h`]);
  if (params.level) parts.push(["", LEVEL_NAME[params.level]]);
  if (params.activity === "Trail running") parts.push(["", "running"]);
  if (params.kinds?.length && !params.destination) parts.push(["with ", params.kinds.map((k) => (k === "loop" ? "loops" : `${k}s`)).join(" and ")]);
  if (params.day != null) parts.push(["", params.day === 0 ? "today" : params.day === 1 ? "tomorrow" : `in ${params.day} days`]);
  const el = $("#ask-echo");
  if (!parts.length) { el.textContent = "Nothing to go on yet. Mention a place, a time or a goal."; return; }
  el.replaceChildren("Read as ", ...parts.flatMap(([pre, val], i) => [i ? ", " : "", pre, h("b", {}, val)]),
    ".");
}

$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#ask-input").value.trim();
  if (!text) return;
  try {
    const { params, source } = await api("/api/ask", { json: { text } });
    applyParams(params);
    echo(params, source);
    const haveStart = params.start || form.start.value.trim();
    if (params.destination || (haveStart && params.mode === "ideas")) runPlan();
    else form.start.focus();
  } catch (err) { toast(err.message); }
});

let recorder = null, recTimer = null;
$("#mic").addEventListener("click", async () => {
  const btn = $("#mic");
  if (recorder?.state === "recording") { recorder.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia) { toast("This browser cannot record audio."); return; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { toast("Microphone access was refused. Allow it in your browser to speak your request."); return; }
  const chunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (ev) => ev.data.size && chunks.push(ev.data);
  recorder.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    clearInterval(recTimer);
    btn.classList.remove("recording");
    $(".rec-time", btn).textContent = "";
    btn.classList.add("busy");
    btn.setAttribute("aria-label", "Listening");
    try {
      const fd = new FormData();
      fd.append("audio", new Blob(chunks, { type: recorder.mimeType || "audio/webm" }), "voice.webm");
      const { text } = await api("/api/voice", { method: "POST", body: fd });
      if (!text) { toast("Nothing was heard. Try again a little closer to the microphone."); return; }
      $("#ask-input").value = text;
      $("#ask-form").requestSubmit();
    } catch (err) { toast(err.message); }
    finally { btn.classList.remove("busy"); btn.setAttribute("aria-label", "Say it"); }
  };
  recorder.start();
  btn.classList.add("recording");
  btn.setAttribute("aria-label", "Stop recording");
  const t0 = Date.now();
  recTimer = setInterval(() => {
    const s = Math.floor((Date.now() - t0) / 1000);
    $(".rec-time", btn).textContent = `0:${String(s).padStart(2, "0")}`;
    if (s >= 15) recorder.stop();
  }, 250);
});

// ------------------------------------------------------- photos and sharing
function photoPicker(dialog) {
  const list = $(".photo-list", dialog);
  const input = $(".photo-input", dialog);
  const items = [];
  input.onchange = () => {
    [...input.files].slice(0, 6 - items.length).forEach((file) => {
      const item = { id: null, species: [], pending: true };
      items.push(item);
      const who = h("div", { class: "who" }, "Looking at the photo…");
      const li = h("li", {}, h("img", { src: URL.createObjectURL(file), alt: "" }), who,
        h("button", { type: "button", "aria-label": "Remove photo", onclick: () => { items.splice(items.indexOf(item), 1); li.remove(); } }, "Remove"));
      list.append(li);
      const fd = new FormData();
      fd.append("image", file);
      api("/api/photos", { method: "POST", body: fd }).then((res) => {
        item.id = res.id; item.species = res.species || []; item.pending = false;
        const top = res.species?.[0];
        if (res.verdict === "match") who.replaceChildren(h("em", {}, top.name), h("small", {}, `${top.scientific}, ${Math.round(top.score * 100)} % sure`));
        else if (res.verdict === "unsure") who.replaceChildren("Perhaps ", h("em", {}, top.name), h("small", {}, `or ${res.species.slice(1).map((s) => s.name).join(", or ")}`));
        else if (res.verdict === "none") who.replaceChildren("No plant or animal recognised");
        else who.replaceChildren("Photo added");
      }).catch((err) => { item.pending = false; item.failed = true; who.textContent = err.message; });
    });
    input.value = "";
  };
  return {
    ready: () => items.every((i) => !i.pending),
    refs: () => items.filter((i) => i.id).map((i) => ({ id: i.id, species: i.species.slice(0, 3) })),
    reset: () => { items.length = 0; list.replaceChildren(); },
  };
}
const sharePhotos = photoPicker($("#share-dialog"));
const importPhotos = photoPicker($("#import-dialog"));
let sharing = null;

function openShare(hk) {
  sharing = hk;
  const f = $("#share-form");
  f.reset(); sharePhotos.reset(); $(".form-error", f).textContent = "";
  f.title.value = hk.title;
  $("#share-dialog").showModal();
}
$("#share-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "share") return;
  e.preventDefault();
  const f = e.currentTarget, err = $(".form-error", f);
  if (!sharePhotos.ready()) { err.textContent = "Wait until every photo has been looked at."; return; }
  try {
    await api("/api/share", { json: { geometry: sharing.geometry, title: f.title.value, notes: f.notes.value,
      dog: f.dog.value, camping: f.camping.value, sac: sharing.sac || 0, photos: sharePhotos.refs() } });
    $("#share-dialog").close();
    state.community = null;
    toast("Shared. It is now on the community map.");
  } catch (ex) { err.textContent = ex.message; }
});

$("#open-import").addEventListener("click", () => {
  const f = $("#import-form");
  f.reset(); importPhotos.reset(); $(".form-error", f).textContent = "";
  $("#import-dialog").showModal();
});
$("#import-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "import") return;
  e.preventDefault();
  const f = e.currentTarget, err = $(".form-error", f);
  if (!importPhotos.ready()) { err.textContent = "Wait until every photo has been looked at."; return; }
  const fd = new FormData();
  fd.append("gpx", f.gpx.files[0]);
  ["title", "notes", "dog", "camping"].forEach((k) => fd.append(k, f[k].value));
  fd.append("photos", JSON.stringify(importPhotos.refs()));
  try {
    const rec = await api("/api/import", { method: "POST", body: fd });
    $("#import-dialog").close();
    state.community = null;
    await showCommunity();
    toast(`Added: ${fmtKm(rec.distance_km)} km and ${fmtM(rec.ascent_m)} m of climbing.`);
  } catch (ex) { err.textContent = ex.message; }
});

// --------------------------------------------------------------- community
async function showCommunity() {
  const box = $("#community-list");
  if (!state.community) {
    box.replaceChildren(h("p", { class: "empty" }, "Loading shared hikes…"));
    try { state.community = (await api("/api/hikes")).hikes; }
    catch (err) { box.replaceChildren(h("p", { class: "empty" }, err.message)); return; }
  }
  const hikes = state.community;
  if (!hikes.length) {
    box.replaceChildren(h("p", { class: "empty" }, "No hike has been shared yet. Plan one and share it, or add one you walked from a GPX file."));
  } else {
    box.replaceChildren(...hikes.map((hk) => h("article", { class: "c-hike", id: `c-${hk.id}`, tabindex: 0,
        onclick: () => frame(hk.path.map(([la, lo]) => [lo, la]), 14) },
      h("h3", {}, hk.title),
      h("p", { class: "c-meta" }, h("span", {}, `${fmtKm(hk.distance_km)} km`), h("span", {}, `${fmtM(hk.ascent_m)} m of climbing`),
        h("span", {}, hk.grade), hk.dog === "Yes" ? h("span", {}, "Dogs welcome") : null, hk.camping === "Yes" ? h("span", {}, "Camping possible") : null),
      hk.notes ? h("p", { class: "c-notes" }, hk.notes) : null,
      hk.photos.length ? h("div", { class: "c-photos" }, hk.photos.map((p) => h("a", { href: p.url, target: "_blank", rel: "noopener", onclick: (e) => e.stopPropagation() },
        h("img", { src: p.thumb, alt: p.species[0] ? p.species[0].name : "Photo from this hike", loading: "lazy" })))) : null,
      hk.species.length ? h("p", { class: "species" }, hk.species.map((s) => h("em", {}, s))) : null)));
  }
  if (!map.getSource("community")) { map.once("load", showCommunity); return; }
  map.getSource("community").setData({ type: "FeatureCollection", features: hikes.filter((hk) => hk.path.length > 1).map((hk) => ({
    type: "Feature", properties: { id: hk.id }, geometry: { type: "LineString", coordinates: hk.path.map(([la, lo]) => [lo, la]) } })) });
  fitAll();
}

function setView(view) {
  state.view = view;
  stopFlight();
  $$(".view-tab").forEach((b) => (b.dataset.view === view ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current")));
  $("#view-plan").hidden = view !== "plan";
  $("#view-community").hidden = view !== "community";
  const planVisible = view === "plan" ? "visible" : "none";
  ["routes-casing", "routes-line", "route-active-casing", "route-active"].forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", planVisible));
  [...state.markers, ...state.placeMarkers, ...Object.values(state.pickMarkers)].forEach((m) => { m.getElement().style.display = view === "plan" ? "" : "none"; });
  if (map.getLayer("community-line")) map.setLayoutProperty("community-line", "visibility", view === "community" ? "visible" : "none");
  if (view === "community") { $("#profile").hidden = true; showCommunity(); }
  else if (state.selected) showProfile(state.selected);
}
$$(".view-tab").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));

// ------------------------------------------------------------------- start
(async () => {
  try {
    state.config = await api("/api/config");
    $("#mic").hidden = !(state.config.voice && window.MediaRecorder);
    if (!state.config.routing) toast("Routing is not configured on this server yet.", 8000);
  } catch { /* the page still works without the config */ }
  const params = new URLSearchParams(location.search);
  if (params.get("q")) { $("#ask-input").value = params.get("q"); $("#ask-form").requestSubmit(); }
})();

// Exposed for debugging from the browser console.
window.ihike = { map, state };
