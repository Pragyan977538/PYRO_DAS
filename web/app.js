/* PYRO_DAS map: vanilla JS on MapLibre, everything served locally (works offline).
 *
 * Layers, bottom to top: cloud cover, incidents (risk), fire detections by
 * category (vector tiles; binned below zoom 7), alert rings, known sources.
 * Click anything for the detail panel: class, confidence, temperature, FRP against
 * the site's own baseline, the nearest named facility, the alert tier -- and the
 * reason for the call.
 *
 * URL parameters drive the demo: ?date=2024-11-10&days=7&z=6&lat=30&lon=75.5
 * &open=event:123 | detection:456 | source:78
 */
"use strict";

const CATEGORIES = {
  industrial: { label: "Industrial", color: "#2a78d6" },
  agricultural: { label: "Agricultural (cropland)", color: "#eb6834" },
  forest: { label: "Forest", color: "#1baf7a" },
  other_natural: { label: "Other vegetation", color: "#eda100" },
  unclassified: { label: "Unclassified", color: "#898781" },
};
const SOURCES = {
  oil_gas: { label: "Oil & gas", color: "#4a3aa7" },
  heavy_industry: { label: "Heavy industry", color: "#1f2937" },
  mining: { label: "Mining & coal fires", color: "#8a5a00" },
  provisional: { label: "New persistent sites (alerting)", color: "#c2477a" },
};
const ROADS = { 1: "Road A · no known source", 2: "Road B · normal at a known source",
                3: "Road C · alert" };
const ALERT_WORDS = { confirmed: "Confirmed anomaly", provisional: "Provisional anomaly",
                      new_source: "New persistent site" };
const DAY_MS = 86400000;
const CLASS_NAMES = { oil_gas: "Oil & gas", heavy_industry: "Heavy industry", mining: "Mining",
  industrial: "Industrial", kiln: "Kiln", offshore: "Offshore", forest: "Forest",
  agricultural: "Agricultural", vegetation: "Vegetation", unclassified: "Unclassified",
  other_natural: "Other vegetation" };
const className = (c) => CLASS_NAMES[c] || words(c);

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("en-IN");
/* FRP to one decimal: the baselines are medians of MW-scale readings. */
const mw = (v) => (v == null ? "–" : Number(v).toFixed(1));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const words = (s) => String(s ?? "").replace(/_/g, " ");
const iso = (d) => d.toISOString().slice(0, 10);
const nice = (d) => d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric",
                                                     timeZone: "UTC" });

const params = new URLSearchParams(location.search);
const state = {
  first: null, last: null, day: null, days: Number(params.get("days")) || 7,
  categories: Object.fromEntries(Object.keys(CATEGORIES).map((k) => [k, true])),
  sources: Object.fromEntries(Object.keys(SOURCES).map((k) => [k, true])),
  alerts: true, events: true, minRisk: Number(params.get("risk")) || 60, cloud: false,
  playing: null,
};
let map;
let refreshTimer = null;

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

function toast(text, ms = 2600) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, ms);
}

function windowDates() {
  const end = new Date(state.day.getTime());
  const start = new Date(end.getTime() - (state.days - 1) * DAY_MS);
  return { start, end };
}

/* ---------------------------------------------------------------- controls */

function buildToggles() {
  const cat = $("category-toggles");
  for (const [key, c] of Object.entries(CATEGORIES)) {
    const li = document.createElement("li");
    li.innerHTML = `<label class="toggle"><input type="checkbox" data-cat="${key}" checked>
      <span class="swatch" style="background:${c.color}" aria-hidden="true"></span>${c.label}
      <span class="count" id="count-${key}"></span></label>`;
    cat.appendChild(li);
  }
  cat.addEventListener("change", (e) => {
    const key = e.target.dataset.cat;
    state.categories[key] = e.target.checked;
    map.setLayoutProperty(`det-${key}`, "visibility", e.target.checked ? "visible" : "none");
  });
  const src = $("source-toggles");
  for (const [key, c] of Object.entries(SOURCES)) {
    const li = document.createElement("li");
    li.innerHTML = `<label class="toggle"><input type="checkbox" data-src="${key}" checked>
      <span class="swatch square" style="background:${c.color}" aria-hidden="true"></span>${c.label}
      <span class="count" id="count-src-${key}"></span></label>`;
    src.appendChild(li);
  }
  src.addEventListener("change", (e) => {
    const key = e.target.dataset.src;
    state.sources[key] = e.target.checked;
    map.setLayoutProperty(`src-${key}`, "visibility", e.target.checked ? "visible" : "none");
  });
  $("alerts").addEventListener("change", (e) => {
    state.alerts = e.target.checked;
    map.setLayoutProperty("det-alerts", "visibility", state.alerts ? "visible" : "none");
  });
  $("events").addEventListener("change", (e) => {
    state.events = e.target.checked;
    map.setLayoutProperty("events", "visibility", state.events ? "visible" : "none");
  });
  $("min-risk").value = String(state.minRisk);
  $("min-risk-out").textContent = state.minRisk;
  $("min-risk").addEventListener("input", (e) => {
    state.minRisk = Number(e.target.value);
    $("min-risk-out").textContent = state.minRisk;
    scheduleRefresh();
  });
  $("cloud").addEventListener("change", (e) => {
    state.cloud = e.target.checked;
    map.setLayoutProperty("cloud", "visibility", state.cloud ? "visible" : "none");
    if (state.cloud) loadCloud();
  });
  $("day").addEventListener("input", (e) => {
    state.day = new Date(state.first.getTime() + Number(e.target.value) * DAY_MS);
    showWindow();
    scheduleRefresh();
  });
  $("days").value = String(state.days);
  $("days").addEventListener("change", (e) => {
    state.days = Number(e.target.value);
    showWindow();
    scheduleRefresh();
  });
  $("play").addEventListener("click", togglePlay);
  $("menu").addEventListener("click", () => $("side").classList.toggle("open"));
  $("close").addEventListener("click", () => { $("panel").hidden = true; });
}

function showWindow() {
  const { start, end } = windowDates();
  $("window-label").textContent = state.days === 1 ? nice(end) : `${nice(start)} – ${nice(end)}`;
  $("day").value = String(Math.round((state.day - state.first) / DAY_MS));
}

function togglePlay() {
  const btn = $("play");
  if (state.playing) {
    clearInterval(state.playing);
    state.playing = null;
    btn.setAttribute("aria-pressed", "false");
    btn.innerHTML = "&#9654; Play";
    return;
  }
  btn.setAttribute("aria-pressed", "true");
  btn.innerHTML = "&#10074;&#10074; Pause";
  state.playing = setInterval(() => {
    const next = new Date(state.day.getTime() + DAY_MS);
    state.day = next > state.last ? new Date(state.first.getTime() + (state.days - 1) * DAY_MS) : next;
    showWindow();
    refresh();
  }, 1600);
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 250);
}

/* --------------------------------------------------------------------- map */

function basemapStyle() {
  const url = (name) => new URL(`basemap/${name}.geojson`, location.href).href;
  return {
    version: 8,
    sources: {
      countries: { type: "geojson", data: url("countries") },
      states: { type: "geojson", data: url("states") },
      rivers: { type: "geojson", data: url("rivers") },
    },
    layers: [
      { id: "sea", type: "background", paint: { "background-color": "#dce8f2" } },
      { id: "land", type: "fill", source: "countries",
        paint: { "fill-color": ["case", ["==", ["get", "iso3"], "IND"], "#f8f7f3", "#ebe9e2"] } },
      { id: "rivers", type: "line", source: "rivers",
        paint: { "line-color": "#a9c6e0", "line-width": ["interpolate", ["linear"], ["zoom"], 4, 0.5, 9, 1.5] } },
      { id: "states", type: "line", source: "states",
        paint: { "line-color": "#c9c6bc", "line-width": 0.7 } },
      { id: "borders", type: "line", source: "countries",
        paint: { "line-color": "#86837a", "line-width": 1.1 } },
    ],
  };
}

function tileUrl() {
  const { start, end } = windowDates();
  return new URL(`api/tiles/detections/{z}/{x}/{y}.mvt?start=${iso(start)}&end=${iso(end)}`,
                 location.href).href.replace("%7Bz%7D", "{z}").replace("%7Bx%7D", "{x}")
                 .replace("%7By%7D", "{y}");
}

function addLayers() {
  map.addSource("cloud", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "cloud", type: "fill", source: "cloud", layout: { visibility: "none" },
                 paint: { "fill-color": "#5b6b7a",
                          "fill-opacity": ["*", ["get", "cloud_frac"], 0.55] } });

  map.addSource("events", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "events", type: "circle", source: "events",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["get", "risk"], 40, 7, 60, 11, 85, 17],
      "circle-color": ["interpolate", ["linear"], ["get", "risk"], 40, "#fbd9b8", 60, "#e8743b", 80, "#8f1d00"],
      "circle-opacity": 0.55, "circle-stroke-color": "#8f1d00", "circle-stroke-width": 1,
    } });

  map.addSource("det", { type: "vector", tiles: [tileUrl()], minzoom: 0, maxzoom: 12 });
  for (const [key, c] of Object.entries(CATEGORIES)) {
    map.addLayer({ id: `det-${key}`, type: "circle", source: "det", "source-layer": "detections",
      filter: ["==", ["get", "category"], key],
      paint: {
        "circle-color": c.color,
        "circle-opacity": key === "industrial" ? 0.95 : 0.7,
        // Binned features (below zoom 7) carry "n": size by count; points by zoom.
        "circle-radius": ["interpolate", ["linear"], ["zoom"],
          3, ["case", ["has", "n"], ["interpolate", ["linear"], ["ln", ["+", 1, ["get", "n"]]], 0, 1.5, 8, 7], 2],
          7, ["case", ["has", "n"], ["interpolate", ["linear"], ["ln", ["+", 1, ["get", "n"]]], 0, 1.8, 8, 9], 2.4],
          12, ["case", ["has", "n"], 9, 5.5]],
      } });
  }
  map.addLayer({ id: "det-alerts", type: "circle", source: "det", "source-layer": "detections",
    filter: ["any", ["has", "alert"], [">", ["coalesce", ["get", "alerts"], 0], 0]],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, ["case", ["has", "n"], 8, 6],
                        7, ["case", ["has", "n"], 9, 6], 12, 10],
      "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-color": ["match", ["get", "alert"], "new_source", "#c2477a", "#d03b3a"],
      "circle-stroke-width": 2,
    } });

  map.addSource("sources", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  for (const [key, c] of Object.entries(SOURCES)) {
    const filter = key === "provisional" ? ["==", ["get", "provisional"], true]
      : ["all", ["==", ["get", "cls"], key], ["!=", ["get", "provisional"], true]];
    map.addLayer({ id: `src-${key}`, type: "circle", source: "sources", filter,
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 3.5, 10, 8],
        "circle-color": key === "provisional" ? "#ffffff" : c.color,
        "circle-stroke-color": key === "provisional" ? c.color : "#ffffff",
        "circle-stroke-width": key === "provisional" ? 2.5 : 1.5,
      } });
  }
}

async function loadCities() {
  try {
    const places = await getJSON(new URL("basemap/places.geojson", location.href).href);
    const big = places.features.filter((f) => f.properties.pop_max >= 2000000
      || /Admin-0 capital/.test(f.properties.featurecla || ""));
    for (const f of big) {
      const el = document.createElement("div");
      el.className = "city";
      el.textContent = f.properties.name;
      new maplibregl.Marker({ element: el, anchor: "left" }).setLngLat(f.geometry.coordinates).addTo(map);
    }
  } catch (err) {
    console.warn("no city labels", err);
  }
}

async function loadSources() {
  const fc = await getJSON("api/sources?limit=20000");
  map.getSource("sources").setData(fc);
  const counts = { oil_gas: 0, heavy_industry: 0, mining: 0, provisional: 0 };
  for (const f of fc.features) {
    const p = f.properties;
    if (p.provisional) counts.provisional += 1;
    else if (counts[p.cls] !== undefined) counts[p.cls] += 1;
  }
  for (const [k, n] of Object.entries(counts)) $(`count-src-${k}`).textContent = fmt.format(n);
}

async function loadCloud() {
  if (!state.cloud) return;
  try {
    map.getSource("cloud").setData(await getJSON(`api/observability?date=${iso(state.day)}`));
  } catch (err) {
    toast("No cloud record for this day");
  }
}

async function refresh() {
  const { start, end } = windowDates();
  const q = `start=${iso(start)}&end=${iso(end)}`;
  map.getSource("det").setTiles([tileUrl()]);
  loadCloud();
  try {
    const [events, summary] = await Promise.all([
      getJSON(`api/events?${q}&min_risk=${state.minRisk}&limit=3000`),
      getJSON(`api/summary?${q}`),
    ]);
    map.getSource("events").setData(events);
    showSummary(summary);
  } catch (err) {
    toast(`Could not load this window: ${err.message}`);
  }
}

function showSummary(s) {
  let total = 0;
  for (const key of Object.keys(CATEGORIES)) {
    const n = s.categories[key] || 0;
    total += n;
    $(`count-${key}`).textContent = fmt.format(n);
  }
  const c = s.roads["3"] || {};
  const alerts = (c.confirmed || 0) + (c.provisional || 0) + (c.new_source || 0);
  $("count-alerts").textContent = fmt.format(alerts);
  const incidents = Object.values(s.events || {}).reduce((a, b) => a + b, 0);
  $("chips").innerHTML = `
    <span class="chip">Detections <b>${fmt.format(total)}</b></span>
    <span class="chip">Industrial <b>${fmt.format(s.categories.industrial || 0)}</b></span>
    <span class="chip alert">Alerts <b>${fmt.format(alerts)}</b></span>
    <span class="chip">Incidents <b>${fmt.format(incidents)}</b></span>`;
  const top = $("top-events");
  top.innerHTML = "";
  if (!s.top_events.length) top.innerHTML = '<li class="muted">No incidents in this window</li>';
  for (const e of s.top_events) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="risk">${e.risk ?? "–"}</span>
      <span class="why">${esc(e.reason)}</span>`;
    li.addEventListener("click", () => {
      map.flyTo({ center: [e.lon, e.lat], zoom: 10 });
      openEvent(e.event_id);
    });
    top.appendChild(li);
  }
}

/* ------------------------------------------------------------------ panels */

function openPanel(html) {
  $("panel-body").innerHTML = html;
  $("panel").hidden = false;
  $("panel").scrollTop = 0;
}

function badges(p) {
  const out = [];
  if (p.road) out.push(`<span class="badge">${ROADS[p.road]}</span>`);
  if (p.alert) out.push(`<span class="badge ${p.alert}">${ALERT_WORDS[p.alert]}</span>`);
  return `<div class="badges">${out.join("")}</div>`;
}

function fact(label, value) {
  return value === null || value === undefined || value === "" ? ""
    : `<dt>${label}</dt><dd>${value}</dd>`;
}

function baselineGauge(b) {
  if (!b || b.p99 == null) return "";
  const top = Math.max(b.pass_max_frp, b.p99 * 1.6, 1);
  const pct = (v) => `${Math.min(100, (v / top) * 100).toFixed(1)}%`;
  const verdict = b.alert ? `<b class="badge ${b.alert}">${ALERT_WORDS[b.alert]}</b>`
    : b.breach ? "breaches (one pass does not confirm)" : "within this site's normal envelope";
  return `<h4>FRP against this site's own baseline</h4>
    <div class="gauge" role="img" aria-label="Hottest pixel ${mw(b.pass_max_frp)} MW; median ${mw(b.med)}; p99 ${mw(b.p99)}">
      <div class="bar" style="width:${pct(b.pass_max_frp)}"></div>
      <div class="mark" style="left:${pct(b.med)}" title="median"></div>
      <div class="mark p99" style="left:${pct(b.p99)}" title="p99"></div>
    </div>
    <div class="gauge-legend"><span>median ${mw(b.med)} MW</span><span>p99 ${mw(b.p99)} MW</span></div>
    <dl class="facts">${fact("This pass", `${mw(b.pass_max_frp)} MW (hottest pixel)`)}
      ${fact("Robust z", b.z)}${fact("Baseline", esc(b.baseline_key))}${fact("Verdict", verdict)}</dl>`;
}

async function openDetection(id) {
  const d = await getJSON(`api/detections/${id}`);
  const p = d.properties;
  const t = p.temperature || {};
  const temp = t.vnf_k ? `${Math.round(t.vnf_k)} K (VIIRS Nightfire)`
    : `not measured: ${esc(t.note || "")}${t.bt4_k ? `. Brightness ${Math.round(t.bt4_k)} K (4 µm) / ${Math.round(t.bt5_k)} K (11 µm)` : ""}`;
  const fac = p.nearest_named_facility;
  const cat = CATEGORIES[p.category] || { label: words(p.category) };
  openPanel(`
    <h3>${esc(cat.label)} fire</h3>${badges(p)}
    <p class="reason ${p.alert ? "alerting" : ""}">${esc(p.reason)}</p>
    <dl class="facts">
      ${fact("Detected", `${esc(p.acq_datetime.replace("T", " ").slice(0, 16))} UTC`)}
      ${fact("Satellite", `${esc(p.sensor)} (${esc(p.instrument)}, ${p.daynight === "N" ? "night" : "day"} pass)`)}
      ${fact("FRP", `${mw(p.frp)} MW`)}
      ${fact("Class", `${esc(className(p.pred_class))}${p.pred_conf != null ? ` (Model 1, confidence ${(p.pred_conf * 100).toFixed(0)}%)` : " (Road A rule)"}`)}
      ${fact("Temperature", temp)}
      ${fact("Nearest named facility", fac ? `${esc(fac.name)} (${esc(words(fac.label_group))}), ${fmt.format(fac.distance_m)} m` : "none within 5 km")}
      ${fact("Incident", p.event ? `<button class="link" data-event="${p.event.event_id}">${esc(words(p.event.kind))}, risk ${p.event.risk ?? "–"}</button>` : "")}
      ${fact("Known source", p.source_id ? `<button class="link" data-source="${p.source_id}">#${p.source_id}${p.source && p.source.provisional ? " (provisional)" : ""}</button>` : "")}
    </dl>
    ${baselineGauge(p.baseline)}
    <div id="chart-slot"></div>`);
  wireLinks();
  if (p.source_id) drawSourceChart(p.source_id, $("chart-slot"));
}

async function openSource(id) {
  const s = await getJSON(`api/sources/${id}`);
  const p = s.properties;
  const fp = p.fingerprint || {};
  const cls = p.provisional ? "New persistent site" : (SOURCES[p.cls] || { label: words(p.cls) }).label;
  openPanel(`
    <h3>${esc(cls)} · source #${p.source_id}</h3>
    <div class="badges">
      <span class="badge">${p.provisional ? "provisional: alerts on every pass" : "registry"}</span>
      ${p.cls_conf != null ? `<span class="badge">Model 1 confidence ${(p.cls_conf * 100).toFixed(0)}%</span>` : ""}
    </div>
    <dl class="facts">
      ${fact("Mapped as", p.evidence_name ? `${esc(p.evidence_name)} (${esc(words(p.label_group))})` : (p.label_group ? esc(words(p.label_group)) : "no mapped facility"))}
      ${fact("Detections", fmt.format(p.n_detections))}
      ${fact("Seen", `${esc(p.first_seen)} to ${esc(p.last_seen)}`)}
      ${fact("Night persistence", fp.persistence_night != null ? `${(fp.persistence_night * 100).toFixed(0)}% of clear nights` : "")}
      ${fact("Typical FRP", fp.frp_med != null ? `${fp.frp_med.toFixed(1)} MW (median)` : "")}
      ${fact("Footprint", fp.width_km != null ? `${fp.width_km.toFixed(1)} km across` : "")}
    </dl>
    <div id="chart-slot"></div>
    <h4>Incidents here</h4>
    <ul>${(p.events || []).slice(0, 8).map((e) => `<li><button class="link" data-event="${e.event_id}">${esc(e.reason)}</button></li>`).join("") || '<li class="muted">none</li>'}</ul>`);
  wireLinks();
  drawSourceChart(id, $("chart-slot"));
}

async function openEvent(id) {
  const e = await getJSON(`api/events/${id}`);
  const p = e.properties;
  const b = e.risk_breakdown || {};
  const term = (name, t, raw) => t ? `<div class="term"><div class="head"><span>${name}</span><b>${t.score.toFixed(2)}</b></div>
      <div class="track"><div class="fill" style="width:${(t.score * 100).toFixed(0)}%"></div></div>
      <div class="raw">${raw}</div></div>` : "";
  const h = b.hazard, x = b.exposure, v = b.vulnerability;
  openPanel(`
    <h3>${esc(p.kind === "fire" ? `${className(p.event_class || p.category)} fire`
      : p.kind === "anomaly" ? `Anomaly at a known ${className(p.event_class).toLowerCase()} site`
      : `New persistent ${className(p.event_class).toLowerCase()} site`)}</h3>
    <div class="badges"><span class="badge">${esc(p.status)}</span>
      ${p.alert ? `<span class="badge ${p.alert}">${ALERT_WORDS[p.alert]}</span>` : ""}</div>
    <p class="reason ${p.alert ? "alerting" : ""}">${esc(p.reason)}</p>
    <h4>Risk</h4>
    <div class="riskbig">${p.risk_score != null ? p.risk_score.toFixed(0) : "–"}<span class="muted" style="font-size:14px"> / 100</span></div>
    <div class="muted">${esc(b.formula || "")}</div>
    ${term("Hazard", h, h ? `peak ${h.peak_frp_mw} MW (above ${(h.frp_percentile * 100).toFixed(0)}% of India's fires), ${h.pixels} pixel${h.pixels === 1 ? "" : "s"}, ${esc(h.growth)}` : "")}
    ${term("Exposure", x, x ? `${fmt.format(x.pop_5km)} people within 5 km; ${x.pop_downwind_10km != null ? `${fmt.format(x.pop_downwind_10km)} downwind (towards ${x.downwind_bearing_deg}°)` : "no wind record"}; ${x.assets_10km} critical asset${x.assets_10km === 1 ? "" : "s"} within 10 km` : "")}
    ${term("Vulnerability", v, v ? (v.nearest ? `${esc(v.nearest)} (${esc(words(v.nearest_type))}), ${fmt.format(v.distance_m)} m` : "no critical asset within 2 km") + (v.own_class ? `; the source itself: ${esc(words(v.own_class))}` : "") : "")}
    <dl class="facts">
      ${fact("First seen", esc(p.first_seen.slice(0, 16).replace("T", " ")))}
      ${fact("Last seen", esc(p.last_seen.slice(0, 16).replace("T", " ")))}
      ${fact("Detections", fmt.format(p.n_detections))}
      ${fact("Known source", p.source_id ? `<button class="link" data-source="${p.source_id}">#${p.source_id}</button>` : "")}
    </dl>
    <h4>Timeline</h4><div id="chart-slot"></div>`);
  wireLinks();
  drawChart($("chart-slot"), (p.timeline || []).map((r) => ({ day: r.day, v: r.max_frp, n: r.n, alert: r.alerts > 0 })), {});
}

function wireLinks() {
  for (const el of document.querySelectorAll("[data-event]")) {
    el.addEventListener("click", () => openEvent(Number(el.dataset.event)));
  }
  for (const el of document.querySelectorAll("[data-source]")) {
    el.addEventListener("click", () => openSource(Number(el.dataset.source)));
  }
}

async function drawSourceChart(id, slot) {
  const ts = await getJSON(`api/sources/${id}/timeseries`);
  const b = ts.baselines || {};
  const key = b["VIIRS|N"] ? "VIIRS|N" : (b["VIIRS|D"] ? "VIIRS|D" : "*");
  const base = b[key] || null;
  slot.innerHTML = `<h4>FRP history (daily hottest pixel)</h4>`;
  const box = document.createElement("div");
  slot.appendChild(box);
  drawChart(box, ts.daily.map((r) => ({ day: r.day, v: r.max_frp, n: r.n, alert: r.alerts > 0 })),
            base ? { med: base.med, p99: base.p99, label: key } : {});
}

/* A small, dependency-free chart: log FRP over time, the site's median and p99
   as reference lines, alert days in red, and a hover crosshair. */
function drawChart(slot, rows, ref) {
  if (!rows.length) { slot.insertAdjacentHTML("beforeend", '<p class="muted">No history.</p>'); return; }
  const W = 340, H = 190, L = 34, R = 8, T = 10, B = 22;
  const days = rows.map((r) => new Date(r.day + "T00:00:00Z").getTime());
  const x0 = Math.min(...days), x1 = Math.max(...days) + DAY_MS;
  const vals = rows.map((r) => Math.max(r.v, 0.1)).concat(ref.p99 ? [ref.p99] : []);
  const lo = Math.log10(Math.max(0.1, Math.min(...vals))), hi = Math.log10(Math.max(...vals) * 1.2);
  const X = (t) => L + ((t - x0) / Math.max(x1 - x0, DAY_MS)) * (W - L - R);
  const Y = (v) => T + (1 - (Math.log10(Math.max(v, 0.1)) - lo) / Math.max(hi - lo, 0.1)) * (H - T - B);
  const pts = rows.map((r, i) => [X(days[i]), Y(r.v)]);
  const ticks = [0.1, 1, 10, 100, 1000, 10000].filter((v) => Math.log10(v) >= lo - 0.01 && Math.log10(v) <= hi);
  const svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="FRP over time">
    <line class="axis" x1="${L}" x2="${W - R}" y1="${H - B}" y2="${H - B}"/>
    ${ticks.map((v) => `<line class="axis" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" opacity=".5"/>
      <text x="${L - 4}" y="${Y(v) + 3}" text-anchor="end">${v}</text>`).join("")}
    <text x="${L}" y="${H - 6}">${esc(rows[0].day)}</text>
    <text x="${W - R}" y="${H - 6}" text-anchor="end">${esc(rows[rows.length - 1].day)}</text>
    ${ref.p99 ? `<line class="p99" x1="${L}" x2="${W - R}" y1="${Y(ref.p99)}" y2="${Y(ref.p99)}"/>
      <text x="${W - R}" y="${Y(ref.p99) - 3}" text-anchor="end">p99 ${mw(ref.p99)} MW</text>` : ""}
    ${ref.med ? `<line class="med" x1="${L}" x2="${W - R}" y1="${Y(ref.med)}" y2="${Y(ref.med)}"/>` : ""}
    ${rows.length > 1 ? `<polyline class="line" points="${pts.map((p) => p.join(",")).join(" ")}"/>` : ""}
    ${pts.map((p, i) => `<circle class="${rows[i].alert ? "alert" : "dot"}" cx="${p[0]}" cy="${p[1]}" r="${rows[i].alert ? 3.5 : rows.length > 200 ? 1.2 : 2.2}"/>`).join("")}
    <line class="cross" id="cross" x1="0" x2="0" y1="${T}" y2="${H - B}" visibility="hidden"/>
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent" id="hit"/>
  </svg><div class="tip" id="tip">Hover for values. MW, log scale${ref.label ? `; baseline ${esc(ref.label)}` : ""}.</div>`;
  slot.insertAdjacentHTML("beforeend", svg);
  const hit = slot.querySelector("#hit"), cross = slot.querySelector("#cross"), tip = slot.querySelector("#tip");
  const el = slot.querySelector("svg");
  hit.addEventListener("mousemove", (ev) => {
    const r = el.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    let best = 0;
    for (let i = 1; i < pts.length; i++) if (Math.abs(pts[i][0] - px) < Math.abs(pts[best][0] - px)) best = i;
    cross.setAttribute("x1", pts[best][0]);
    cross.setAttribute("x2", pts[best][0]);
    cross.setAttribute("visibility", "visible");
    const row = rows[best];
    tip.textContent = `${row.day}: hottest pixel ${row.v} MW, ${row.n} detection${row.n === 1 ? "" : "s"}${row.alert ? ", alert" : ""}`;
  });
  hit.addEventListener("mouseleave", () => cross.setAttribute("visibility", "hidden"));
}

/* ------------------------------------------------------------------- clicks */

function wireMapClicks() {
  const detLayers = Object.keys(CATEGORIES).map((k) => `det-${k}`).concat(["det-alerts"]);
  const srcLayers = Object.keys(SOURCES).map((k) => `src-${k}`);
  const clickable = srcLayers.concat(detLayers, ["events"]);
  map.on("click", (e) => {
    const box = [[e.point.x - 4, e.point.y - 4], [e.point.x + 4, e.point.y + 4]];
    const hits = map.queryRenderedFeatures(box, { layers: clickable.filter((l) => map.getLayer(l)) });
    if (!hits.length) return;
    const f = hits.find((h) => srcLayers.includes(h.layer.id))
      || hits.find((h) => detLayers.includes(h.layer.id)) || hits[0];
    const p = f.properties;
    if (srcLayers.includes(f.layer.id)) return openSource(p.source_id);
    if (f.layer.id === "events") return openEvent(p.event_id);
    if (p.n !== undefined) {
      toast(`${fmt.format(p.n)} detections here (${words(p.category)} mostly) — zooming in`);
      return map.easeTo({ center: e.lngLat, zoom: Math.max(map.getZoom() + 2, 7) });
    }
    return openDetection(p.id);
  });
  for (const l of clickable) {
    map.on("mouseenter", l, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", l, () => { map.getCanvas().style.cursor = ""; });
  }
}

/* --------------------------------------------------------------------- init */

async function init() {
  buildToggles();
  const meta = await getJSON("api/meta");
  const w = meta.window;
  state.first = new Date(w.window_start.slice(0, 10) + "T00:00:00Z");
  state.last = new Date(new Date(w.window_end.slice(0, 10) + "T00:00:00Z").getTime() - DAY_MS);
  const asked = params.get("date") ? new Date(params.get("date") + "T00:00:00Z") : null;
  state.day = asked && asked >= state.first && asked <= state.last ? asked : state.last;
  $("day").max = String(Math.round((state.last - state.first) / DAY_MS));
  showWindow();

  map = new maplibregl.Map({
    container: "map", style: basemapStyle(), attributionControl: false,
    center: [Number(params.get("lon")) || 80.5, Number(params.get("lat")) || 22.5],
    zoom: Number(params.get("z")) || 4.1, minZoom: 3, maxZoom: 14,
    maxBounds: [[55, -2], [110, 42]],
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
  map.on("load", async () => {
    addLayers();
    wireMapClicks();
    loadCities();
    await Promise.all([loadSources(), refresh()]);
    const open = params.get("open");
    if (open) {
      const [kind, id] = open.split(":");
      ({ event: openEvent, detection: openDetection, source: openSource }[kind] || (() => {}))(Number(id));
    }
  });
}

init().catch((err) => toast(`PYRO_DAS could not start: ${err.message}`, 8000));
