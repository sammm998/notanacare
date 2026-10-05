import { esc, hhmm } from "./util.js";

let map, layerRecipients, layerRoutes, layerBases;

export function hue(id) {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

export function initMap(onRecipientClick) {
  if (map || !window.L) return;
  map = L.map("map", { preferCanvas: true }).setView([59.325, 18.03], 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap contributors · synthetic people &amp; addresses",
  }).addTo(map);
  layerRoutes = L.layerGroup().addTo(map);
  layerRecipients = L.layerGroup().addTo(map);
  layerBases = L.layerGroup().addTo(map);
  map._onRecipient = onRecipientClick;
}

export function invalidate() {
  if (map) setTimeout(() => map.invalidateSize(), 50);
}

export function renderMap(state, selectedEmp, showRoutes) {
  if (!map) return;
  const sc = state.scenario;
  if (!sc) return;
  if (state._mapScenario !== sc.id) {
    map.setView(sc.center, 12);
    state._mapScenario = sc.id;
  }
  layerRecipients.clearLayers();
  layerRoutes.clearLayers();
  layerBases.clearLayers();
  const plan = state.plan;
  const unplannedByRecipient = {};
  if (plan) for (const vid of Object.keys(plan.unplanned)) {
    const v = state.visitsById[vid];
    if (v) unplannedByRecipient[v.recipient_id] = (unplannedByRecipient[v.recipient_id] || 0) + 1;
  }
  for (const r of sc.recipients) {
    const bad = unplannedByRecipient[r.id];
    const m = L.circleMarker([r.lat, r.lon], {
      radius: bad ? 6 : 4, weight: 1, color: bad ? "#c0392b" : "#1f8a4c",
      fillColor: bad ? "#c0392b" : "#1f8a4c", fillOpacity: 0.75,
    });
    m.bindTooltip(`${esc(r.id)} · ${esc(r.name)}<br>${esc(r.address)}${bad ? `<br><b>${bad} unplanned visit(s)</b>` : ""}`);
    m.on("click", () => map._onRecipient && map._onRecipient(r.id));
    m.addTo(layerRecipients);
  }
  for (const b of sc.bases) {
    const l = sc.locations[b];
    L.marker([l.lat, l.lon], {
      icon: L.divIcon({ className: "", html: '<div style="width:12px;height:12px;background:#1d2733;border:2px solid #fff"></div>' }),
    }).bindTooltip(esc(l.label)).addTo(layerBases);
  }
  if (!plan || !showRoutes) return;
  const loc = (id) => sc.locations[id];
  const routes = Object.values(plan.routes).filter((r) => r.stops.length);
  for (const r of routes) {
    if (selectedEmp && r.employee_id !== selectedEmp) continue;
    const pts = [];
    const start = loc(r.start_location_id);
    if (start) pts.push([start.lat, start.lon]);
    for (const s of [...r.stops].sort((a, b) => a.start - b.start)) {
      const l = loc(s.location_id);
      if (l) pts.push([l.lat, l.lon]);
    }
    const end = loc(r.end_location_id);
    if (end) pts.push([end.lat, end.lon]);
    const color = `hsl(${hue(r.employee_id)} 65% 42%)`;
    L.polyline(pts, { color, weight: selectedEmp ? 4 : 1.5, opacity: selectedEmp ? 0.9 : 0.35 }).addTo(layerRoutes);
    if (selectedEmp) {
      let n = 0;
      for (const s of [...r.stops].sort((a, b) => a.start - b.start)) {
        if (s.kind !== "visit") continue;
        n += 1;
        const l = loc(s.location_id);
        L.marker([l.lat, l.lon], {
          icon: L.divIcon({ className: "", html: `<div class="num-marker">${n}</div>`, iconSize: [20, 20] }),
        })
          .bindTooltip(`${n}. ${esc(s.visit_id)} ${hhmm(s.start)}–${hhmm(s.end)} (travel ${s.travel_from_prev} min)`)
          .on("click", () => map._onVisit && map._onVisit(s.visit_id))
          .addTo(layerRoutes);
      }
      map.fitBounds(L.latLngBounds(pts).pad(0.15));
    }
  }
}

export function onVisitClick(fn) {
  if (map) map._onVisit = fn;
}
