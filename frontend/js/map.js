import { api } from "./api.js";
import { esc, hhmm } from "./util.js";

/* -------------------------------------------------------------------------
 * Map adapters: Google Maps (when a browser key is configured) or Leaflet /
 * OpenStreetMap as fallback. The rendering code below only uses this API.
 * ---------------------------------------------------------------------- */

const PALETTE = ["#1f6fd1", "#d1495b", "#2a9d5b", "#9b51e0", "#e08a00", "#00a3a3", "#c2185b", "#5d6d7e", "#7cb342", "#8d6e63", "#3949ab", "#ef6c00"];
export const colorFor = (i) => PALETTE[i % PALETTE.length];

class LeafletAdapter {
  constructor(el, center) {
    this.kind = "leaflet";
    this.map = L.map(el, { preferCanvas: false }).setView(center, 12);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors · synthetic people &amp; addresses",
    }).addTo(this.map);
    this.layer = L.layerGroup().addTo(this.map);
  }
  clear() { this.layer.clearLayers(); }
  setView(c, z) { this.map.setView(c, z); }
  resize() { setTimeout(() => this.map.invalidateSize(), 50); }
  fit(pts) { if (pts.length) this.map.fitBounds(L.latLngBounds(pts).pad(0.12)); }
  dot(p, o) {
    const m = L.circleMarker(p, { radius: o.r || 4, color: o.stroke || o.color, weight: o.weight ?? 1, fillColor: o.color, fillOpacity: o.opacity ?? 0.85 });
    if (o.tip) m.bindTooltip(o.tip);
    if (o.onClick) m.on("click", o.onClick);
    m.addTo(this.layer);
  }
  line(path, o) {
    L.polyline(path, { color: o.color, weight: o.weight || 3, opacity: o.opacity ?? 0.9, dashArray: o.dashed ? "6 6" : null }).addTo(this.layer);
    if (o.arrows) for (const [a, b] of arrowSegments(path)) this._arrow(a, b, o.color);
  }
  _arrow(a, b, color) {
    const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    const ang = (Math.atan2((b[1] - a[1]) * Math.cos((a[0] * Math.PI) / 180), b[0] - a[0]) * 180) / Math.PI;
    L.marker(mid, {
      interactive: false,
      icon: L.divIcon({ className: "", iconSize: [14, 14], iconAnchor: [7, 7],
        html: `<div style="transform:rotate(${ang}deg);color:${color};font-size:14px;line-height:14px;text-align:center">▲</div>` }),
    }).addTo(this.layer);
  }
  badge(p, o) {
    const m = L.marker(p, {
      zIndexOffset: o.z || 500,
      icon: L.divIcon({ className: "", iconSize: [o.size || 24, o.size || 24], iconAnchor: [(o.size || 24) / 2, (o.size || 24) / 2], html: badgeHtml(o) }),
    });
    if (o.tip) m.bindTooltip(o.tip);
    if (o.onClick) m.on("click", o.onClick);
    m.addTo(this.layer);
  }
}

class GoogleAdapter {
  constructor(el, center) {
    this.kind = "google";
    this.map = new google.maps.Map(el, {
      center: { lat: center[0], lng: center[1] }, zoom: 12, mapTypeControl: true, streetViewControl: false, fullscreenControl: true,
      clickableIcons: false,
    });
    this.items = [];
    this.info = new google.maps.InfoWindow();
  }
  clear() { for (const i of this.items) i.setMap(null); this.items = []; }
  setView(c, z) { this.map.setCenter({ lat: c[0], lng: c[1] }); this.map.setZoom(z); }
  resize() { /* google maps resizes itself */ }
  fit(pts) {
    if (!pts.length) return;
    const b = new google.maps.LatLngBounds();
    for (const p of pts) b.extend({ lat: p[0], lng: p[1] });
    this.map.fitBounds(b, 40);
  }
  _wire(m, o) {
    if (o.tip) m.addListener("mouseover", () => { this.info.setContent(`<div style="font:12px system-ui">${o.tip}</div>`); this.info.open({ map: this.map, anchor: m }); });
    if (o.tip) m.addListener("mouseout", () => this.info.close());
    if (o.onClick) m.addListener("click", o.onClick);
    this.items.push(m);
  }
  dot(p, o) {
    const m = new google.maps.Marker({
      position: { lat: p[0], lng: p[1] }, map: this.map, zIndex: 10,
      icon: { path: google.maps.SymbolPath.CIRCLE, scale: o.r || 4, fillColor: o.color, fillOpacity: o.opacity ?? 0.85, strokeColor: o.stroke || o.color, strokeWeight: o.weight ?? 1 },
    });
    this._wire(m, o);
  }
  line(path, o) {
    const icons = [];
    if (o.arrows) icons.push({ icon: { path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW, scale: 2.6, strokeColor: o.color, fillColor: o.color, fillOpacity: 1 }, offset: "50%", repeat: "120px" });
    if (o.dashed) icons.push({ icon: { path: "M 0,-1 0,1", strokeOpacity: 1, scale: 3 }, offset: "0", repeat: "14px" });
    const pl = new google.maps.Polyline({
      path: path.map((p) => ({ lat: p[0], lng: p[1] })), map: this.map, strokeColor: o.color,
      strokeOpacity: o.dashed ? 0 : (o.opacity ?? 0.9), strokeWeight: o.weight || 3, icons, zIndex: 20,
    });
    this.items.push(pl);
  }
  badge(p, o) {
    const size = o.size || 24;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}"><circle cx="${size / 2}" cy="${size / 2}" r="${size / 2 - 2}" fill="${o.fill || "#fff"}" stroke="${o.color}" stroke-width="3"/>` +
      `<text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" font-family="system-ui,Arial" font-weight="700" font-size="${o.text.length > 2 ? 9 : 11}" fill="${o.textColor || "#1d2733"}">${esc(o.text)}</text></svg>`;
    const m = new google.maps.Marker({
      position: { lat: p[0], lng: p[1] }, map: this.map, zIndex: o.z || 500,
      icon: { url: "data:image/svg+xml;charset=UTF-8," + encodeURIComponent(svg), anchor: new google.maps.Point(size / 2, size / 2) },
    });
    this._wire(m, o);
  }
}

function badgeHtml(o) {
  const s = o.size || 24;
  return `<div style="width:${s}px;height:${s}px;border-radius:50%;background:${o.fill || "#fff"};border:3px solid ${o.color};box-sizing:border-box;display:grid;place-items:center;font:700 ${o.text.length > 2 ? 9 : 11}px system-ui;color:${o.textColor || "#1d2733"};box-shadow:0 1px 3px rgba(0,0,0,.35)">${esc(o.text)}</div>`;
}

function arrowSegments(path) {
  // One arrow per sufficiently long segment.
  const out = [];
  for (let i = 0; i + 1 < path.length; i++) {
    const a = path[i];
    const b = path[i + 1];
    if (Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) > 0.0025) out.push([a, b]);
  }
  return out;
}

/* ------------------------------------------------------------------------ */

let adapter = null;
let mapEl = null;
let handlers = {};
const geometryCache = new Map();

// Google reports the reason for a rejected key only on the console
// ("Google Maps JavaScript API error: RefererNotAllowedMapError ..."). Capture it.
const GM_HELP = {
  RefererNotAllowedMapError: "add this site's URL (https://<your-app>.up.railway.app/*) under the key's Website restrictions",
  ApiNotActivatedMapError: "enable \"Maps JavaScript API\" in Google Cloud for this project",
  ApiTargetBlockedMapError: "the key's API restrictions must include \"Maps JavaScript API\"",
  InvalidKeyMapError: "the key is wrong – check GOOGLE_MAPS_BROWSER_KEY in Railway",
  MissingKeyMapError: "GOOGLE_MAPS_BROWSER_KEY is empty",
  ExpiredKeyMapError: "the key has expired – create a new one",
  BillingNotEnabledMapError: "enable billing on the Google Cloud project",
  DeletedApiProjectMapError: "the key's Google Cloud project was deleted",
};
export const gmError = { code: null, text: null };
(() => {
  const orig = console.error.bind(console);
  console.error = (...args) => {
    const msg = args.map(String).join(" ");
    const m = msg.match(/([A-Za-z]+MapError)/);
    if (m && /Google Maps/i.test(msg)) {
      gmError.code = m[1];
      gmError.text = msg.split("\n")[0];
    }
    orig(...args);
  };
})();
export function gmErrorMessage() {
  const code = gmError.code;
  if (!code) return "Google Maps rejected the browser key (no error code reported)";
  return `Google Maps: ${code} – ${GM_HELP[code] || "see developers.google.com/maps/documentation/javascript/error-messages"}`;
}

function loadGoogle(key) {
  return new Promise((resolve, reject) => {
    if (window.google?.maps) return resolve();
    window.__notanaGmReady = resolve;
    window.gm_authFailure = () => setTimeout(() => reject(new Error(gmErrorMessage())), 50);
    const s = document.createElement("script");
    s.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&v=weekly&callback=__notanaGmReady`;
    s.async = true;
    s.onerror = () => reject(new Error("Could not load Google Maps"));
    document.head.appendChild(s);
    setTimeout(() => reject(new Error("Google Maps load timeout")), 15000);
  });
}

/** Create the map. Returns the provider actually used ("google" | "leaflet"). */
export async function initMap(el, cfg, h) {
  mapEl = el;
  handlers = h;
  const center = [59.325, 18.03];
  if (cfg?.provider === "google" && cfg.browser_key) {
    try {
      await loadGoogle(cfg.browser_key);
      adapter = new GoogleAdapter(el, center);
      window.gm_authFailure = () => {
        // Key rejected after load (wrong referrer / API not enabled): switch back to
        // the OpenStreetMap map. The console message with the code arrives right after.
        setTimeout(() => {
          const fresh = document.createElement("div");
          fresh.id = el.id;
          fresh.style.cssText = el.style.cssText;
          el.replaceWith(fresh);
          mapEl = fresh;
          adapter = new LeafletAdapter(fresh, center);
          handlers.onProviderFallback?.(`${gmErrorMessage()}. Showing OpenStreetMap instead.`, gmError.code);
          handlers.rerender?.();
        }, 50);
      };
      return "google";
    } catch (e) {
      handlers.onProviderFallback?.(`${e.message} – showing OpenStreetMap instead`);
      el.innerHTML = "";
    }
  }
  adapter = new LeafletAdapter(el, center);
  return "leaflet";
}

export function invalidate() { adapter?.resize(); }

function recipientStatus(state) {
  const bad = {};
  if (state.plan) for (const vid of Object.keys(state.plan.unplanned)) {
    const v = state.visitsById[vid];
    if (v) bad[v.recipient_id] = (bad[v.recipient_id] || 0) + 1;
  }
  return bad;
}

function drawRecipients(state, muted, highlight) {
  const bad = recipientStatus(state);
  for (const r of state.scenario.recipients) {
    if (highlight?.has(r.id)) continue;
    const isBad = bad[r.id];
    adapter.dot([r.lat, r.lon], {
      r: isBad ? 6 : muted ? 3 : 4,
      color: isBad ? "#c0392b" : muted ? "#9aa5b1" : "#1f8a4c",
      opacity: muted && !isBad ? 0.55 : 0.85,
      tip: `${esc(r.id)} · ${esc(r.name)}<br>${esc(r.address)}${isBad ? `<br><b>${isBad} unplanned visit(s)</b>` : ""}`,
      onClick: () => handlers.onRecipient(r.id),
    });
  }
}

function drawOffices(state) {
  for (const b of state.scenario.bases) {
    const l = state.scenario.locations[b];
    adapter.badge([l.lat, l.lon], { text: "⌂", color: "#1d2733", fill: "#1d2733", textColor: "#fff", size: 20, z: 300, tip: esc(l.label) });
  }
}

/** Ordered items of one employee's day (shared by map and itinerary). */
export function routeItems(state, eid) {
  const r = state.plan?.routes[eid];
  if (!r) return [];
  const locs = state.scenario.locations;
  const stops = [...r.stops].sort((a, b) => a.start - b.start);
  const items = [];
  const startLoc = r.start_location_id;
  if (r.route_start !== null && r.route_start !== undefined) items.push({ kind: "start", time: r.route_start, loc: startLoc, label: locs[startLoc]?.label || startLoc });
  let n = 0;
  for (const s of stops) {
    if (s.kind === "visit") n += 1;
    items.push({ ...s, n: s.kind === "visit" ? n : null, loc: s.location_id });
  }
  if (r.route_end !== null && r.route_end !== undefined) items.push({ kind: "end", time: r.route_end, loc: r.end_location_id, travel: r.travel_to_end, label: locs[r.end_location_id]?.label });
  return items;
}

async function roadGeometry(state, eid) {
  const key = `${state.plan.id}:${eid}`;
  if (!geometryCache.has(key)) geometryCache.set(key, api("GET", `/api/plans/${state.plan.id}/routes/${eid}/geometry`).catch(() => null));
  return geometryCache.get(key);
}

export async function renderMap(state, opts) {
  if (!adapter || !state.scenario) return;
  const sc = state.scenario;
  if (state._mapScenario !== sc.id) {
    adapter.setView(sc.center, 12);
    state._mapScenario = sc.id;
  }
  adapter.clear();
  const plan = state.plan;
  const mode = plan ? opts.mode : "all";
  const changed = opts.changed || new Set();

  if (mode === "employee" && opts.employee && plan?.routes[opts.employee]) {
    const eid = opts.employee;
    const color = "#1f6fd1";
    const items = routeItems(state, eid);
    const visitRecipients = new Set(items.filter((i) => i.kind === "visit").map((i) => state.visitsById[i.visit_id]?.recipient_id));
    drawRecipients(state, true, visitRecipients);
    drawOffices(state);
    const pts = items.filter((i) => i.kind !== "unavailable").map((i) => [sc.locations[i.loc].lat, sc.locations[i.loc].lon]);
    let geo = null;
    if (opts.followRoads) {
      const token = (renderMap._token = (renderMap._token || 0) + 1);
      geo = await roadGeometry(state, eid);
      if (token !== renderMap._token) return; // a newer render started
    }
    if (geo && geo.source === "google-routes") {
      for (const leg of geo.legs) adapter.line(leg.path, { color, weight: 5, opacity: 0.85, arrows: true });
    } else {
      for (let i = 0; i + 1 < pts.length; i++) adapter.line([pts[i], pts[i + 1]], { color, weight: 4, opacity: 0.85, arrows: true });
    }
    opts.onGeometry?.(geo ? geo.source : "straight");
    for (const it of items) {
      const l = sc.locations[it.loc];
      const p = [l.lat, l.lon];
      if (it.kind === "visit") {
        const v = state.visitsById[it.visit_id] || {};
        adapter.badge(p, {
          text: String(it.n), color: changed.has(it.visit_id) ? "#e07a1f" : v.staff === 2 ? "#7b5bc9" : color,
          fill: it.visit_id === opts.selectedVisit ? "#ffe9a8" : "#fff", size: 26, z: 900,
          tip: `<b>${it.n}. ${hhmm(it.start)}–${hhmm(it.end)}</b> ${esc(it.visit_id)}<br>${esc(v.recipient_id || "")} · ${it.end - it.start} min${v.staff === 2 ? " · double-staffed" : ""}<br>travel in: ${it.travel_from_prev} min`,
          onClick: () => handlers.onVisit(it.visit_id),
        });
      }
    }
    const emp = state.empsById[eid];
    if (emp?.home_lat != null) {
      // Home and commute to the team office (own time, not part of the route).
      const office = sc.locations[emp.start_location_id];
      if (office) adapter.line([[emp.home_lat, emp.home_lon], [office.lat, office.lon]], { color: "#7a8594", weight: 2, opacity: 0.8, dashed: true });
      adapter.badge([emp.home_lat, emp.home_lon], { text: "⌂", color: "#1d2733", fill: "#ffffff", size: 22, z: 430,
        tip: `<b>Hem</b> ${esc(emp.home_address)}<br>${emp.commute_minutes ?? "?"} min till kontoret ${emp.commute_mode === "car" ? "med bil" : "med kollektivtrafik"}` });
    }
    adapter.badge(pts[0], { text: "S", color: "#1d2733", fill: "#2a9d5b", textColor: "#fff", size: 22, z: 450, tip: "Start of the day" });
    adapter.badge(pts[pts.length - 1], { text: "E", color: "#1d2733", fill: "#d1495b", textColor: "#fff", size: 18, z: 440, tip: "End of the day" });
    if (opts.fit !== false) adapter.fit(pts);
    return;
  }

  if (mode === "team" && plan) {
    const emps = sc.employees.filter((e) => e.team === opts.team && plan.routes[e.id]?.stops.some((s) => s.kind === "visit"));
    drawRecipients(state, true);
    drawOffices(state);
    const all = [];
    emps.forEach((e, i) => {
      const color = colorFor(i);
      const items = routeItems(state, e.id).filter((x) => x.kind !== "unavailable");
      const pts = items.map((x) => [sc.locations[x.loc].lat, sc.locations[x.loc].lon]);
      all.push(...pts);
      adapter.line(pts, { color, weight: 3, opacity: 0.8, arrows: true });
      for (const it of items) {
        if (it.kind !== "visit") continue;
        const l = sc.locations[it.loc];
        adapter.dot([l.lat, l.lon], { r: 6, color, stroke: "#fff", weight: 2, opacity: 1,
          tip: `${esc(e.id)} · ${it.n}. ${hhmm(it.start)} ${esc(it.visit_id)}`, onClick: () => handlers.onEmployee(e.id) });
      }
    });
    opts.onTeam?.(emps.map((e, i) => ({ id: e.id, name: e.name, color: colorFor(i) })));
    if (opts.fit !== false) adapter.fit(all);
    return;
  }

  // Overview: every recipient by status, optionally all routes faintly.
  drawRecipients(state, false);
  drawOffices(state);
  if (plan && opts.showAll) {
    Object.values(plan.routes).forEach((r, i) => {
      const items = routeItems(state, r.employee_id).filter((x) => x.kind !== "unavailable");
      const pts = items.map((x) => [sc.locations[x.loc].lat, sc.locations[x.loc].lon]);
      if (pts.length > 2) adapter.line(pts, { color: colorFor(i), weight: 1.5, opacity: 0.35 });
    });
  }
}

/** Itinerary panel for one employee. */
export function renderItinerary(el, state, eid, geoSource, onVisit) {
  const e = state.empsById[eid];
  const items = routeItems(state, eid);
  if (!e || !items.length) {
    el.innerHTML = '<div class="hint">This employee has no route in the current plan.</div>';
    return;
  }
  const visits = items.filter((i) => i.kind === "visit");
  const care = visits.reduce((a, v) => a + v.end - v.start, 0);
  const travel = items.reduce((a, i) => a + (i.travel_from_prev || 0), 0) + (items.find((i) => i.kind === "end")?.travel || 0);
  const row = (html, cls = "", vid = "") => `<li class="it ${cls}" ${vid ? `data-visit="${vid}"` : ""}>${html}</li>`;
  const travelRow = (m) => (m ? row(`<span class="it-travel">↓ ${m} min travel</span>`, "travel") : "");
  const out = [];
  for (const it of items) {
    if (it.kind === "start") out.push(row(`<b>${hhmm(it.time)}</b> Start · ${esc(it.label)}`, "endpoint"));
    else if (it.kind === "end") { out.push(travelRow(it.travel)); out.push(row(`<b>${hhmm(it.time)}</b> Back · ${esc(it.label || "")}`, "endpoint")); }
    else if (it.kind === "break") { out.push(travelRow(it.travel_from_prev)); out.push(row(`<b>${hhmm(it.start)}–${hhmm(it.end)}</b> ☕ Meal break at the office`, "brk")); }
    else if (it.kind === "unavailable") out.push(row(`<b>${hhmm(it.start)}–${hhmm(it.end)}</b> ⏸ Off (split shift / unavailable)`, "brk"));
    else {
      const v = state.visitsById[it.visit_id] || {};
      const r = state.scenario.recipients.find((x) => x.id === v.recipient_id);
      out.push(travelRow(it.travel_from_prev));
      out.push(row(
        `<span class="it-n">${it.n}</span><div><b>${hhmm(it.start)}–${hhmm(it.end)}</b> ${esc(it.visit_id)} ${v.staff === 2 ? "👥" : ""} ${it.locked ? "🔒" : ""}<br>` +
        `<span class="hint">${esc(r?.name || v.recipient_id || "")} · ${esc(r?.address || "")}<br>${v.interventions || "?"} interventions · ${it.end - it.start} min · window ${hhmm(v.earliest)}–${hhmm(v.latest)}${v.priority >= 5 ? " · <b>prio 5</b>" : ""}</span></div>`,
        "visit", it.visit_id));
    }
  }
  el.innerHTML = `
    <div class="it-head"><b>${esc(e.id)}</b> ${esc(e.name)}<br><span class="hint">Team ${esc(e.team)} · shift ${hhmm(e.shift_start)}–${hhmm(e.shift_end)}</span>
    ${e.home_address ? `<div class="hint it-home">🏠 Bor: ${esc(e.home_address)}${e.commute_minutes != null ? `<br>Till kontoret: <b>${e.commute_minutes} min</b> ${e.commute_mode === "car" ? "med bil" : "med kollektivtrafik"} · ${e.commute_km} km · ${e.commute_source === "google" ? "Google Routes" : "uppskattning"} (egen tid, ingår inte i rutten)` : ""}</div>` : ""}
    <div class="it-sum"><span><b>${visits.length}</b> visits</span><span><b>${Math.round(care / 6) / 10}</b> h care</span><span><b>${travel}</b> min travel</span></div>
    <div class="hint">${geoSource === "google-routes" ? "Lines follow roads (Google Routes)." : "Lines are straight between stops; travel times come from the travel matrix."} Numbers = visit order.</div></div>
    <ol class="itinerary">${out.join("")}</ol>`;
  el.querySelectorAll("[data-visit]").forEach((li) => li.addEventListener("click", () => onVisit(li.dataset.visit)));
}

export function renderTeamLegend(el, list, onEmployee) {
  el.innerHTML = `<div class="it-head"><b>${list.length} routes in this team</b><div class="hint">Each colour is one employee; arrows show driving direction. Click a name for the full itinerary.</div></div>` +
    `<ul class="legend-list">${list.map((x) => `<li data-emp="${x.id}"><i style="background:${x.color}"></i>${esc(x.id)} <span class="hint">${esc(x.name)}</span></li>`).join("")}</ul>`;
  el.querySelectorAll("[data-emp]").forEach((li) => li.addEventListener("click", () => onEmployee(li.dataset.emp)));
}
