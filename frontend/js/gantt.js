import { esc, hhmm } from "./util.js";

const T0 = 6 * 60;
const T1 = 23 * 60;
const PX = 1.15; // px per minute
const LABEL = 170;

const x = (m) => LABEL + (m - T0) * PX;

export function changedVisits(plan, parent) {
  const out = new Set();
  if (!plan || !parent) return out;
  for (const [vid, a] of Object.entries(plan.assignments)) {
    const b = parent.assignments[vid];
    if (!b || b.start !== a.start || [...b.employee_ids].sort().join() !== [...a.employee_ids].sort().join()) out.add(vid);
  }
  return out;
}

export function renderGantt(container, state, opts, onVisit, onEmployee) {
  const plan = state.plan;
  const sc = state.scenario;
  if (!plan || !sc) {
    container.innerHTML = '<div class="empty">No plan yet.</div>';
    return;
  }
  const changed = changedVisits(plan, state.parent);
  const changedRoutes = new Set();
  for (const vid of changed) for (const e of plan.assignments[vid].employee_ids) changedRoutes.add(e);
  if (state.parent) for (const [vid, a] of Object.entries(state.parent.assignments)) {
    if (!plan.assignments[vid]) for (const e of a.employee_ids) changedRoutes.add(e);
  }
  let emps = sc.employees.filter((e) => !opts.team || e.team === opts.team);
  if (opts.onlyChanged) emps = emps.filter((e) => changedRoutes.has(e.id));
  const load = (e) => (plan.routes[e.id]?.stops || []).filter((s) => s.kind === "visit").reduce((a, s) => a + s.end - s.start, 0);
  if (opts.sort === "team") emps.sort((a, b) => a.team.localeCompare(b.team) || a.shift_start - b.shift_start);
  else if (opts.sort === "load") emps.sort((a, b) => load(b) - load(a));
  else emps.sort((a, b) => a.shift_start - b.shift_start || a.id.localeCompare(b.id));

  const width = x(T1) + 10;
  const parts = [`<div class="g-inner" style="width:${width}px">`, '<div class="g-axis">'];
  for (let t = T0; t <= T1; t += 60) parts.push(`<div class="g-tick" style="left:${x(t)}px">${hhmm(t)}</div>`);
  parts.push("</div>");
  const visits = state.visitsById;
  for (const e of emps) {
    const r = plan.routes[e.id];
    const sick = e.status !== "working";
    parts.push(`<div class="g-row" data-emp="${e.id}">`);
    parts.push(`<span class="g-label" data-emp="${e.id}" title="${esc(e.name)} · ${esc(e.team)} · ${esc([...e.delegations, ...e.skills].join(", "))}">${esc(e.id)} <span style="color:var(--muted)">${esc(e.team.slice(0, 9))}</span>${sick ? " 🤒" : ""} · ${Math.round(load(e) / 6) / 10}h</span>`);
    parts.push(`<div class="g-shift" style="left:${x(e.shift_start)}px;width:${(e.shift_end - e.shift_start) * PX}px"></div>`);
    for (const iv of e.unavailable || []) {
      if (iv.reason === "split-shift gap") continue;
      parts.push(`<div class="g-unavail" title="${esc(iv.reason)} ${hhmm(iv.start)}–${hhmm(iv.end)}" style="left:${x(iv.start)}px;width:${Math.max(2, (iv.end - iv.start) * PX)}px"></div>`);
    }
    if (r) {
      for (const s of r.stops) {
        if (s.travel_from_prev > 0) {
          const ts = s.start - s.travel_from_prev;
          parts.push(`<div class="g-travel" style="left:${x(ts)}px;width:${s.travel_from_prev * PX}px"></div>`);
        }
        const w = Math.max(3, (s.end - s.start) * PX);
        if (s.kind !== "visit") {
          const cls = s.kind === "unavailable" ? "una" : "brk";
          parts.push(`<div class="g-stop ${cls}" title="${s.kind} ${hhmm(s.start)}–${hhmm(s.end)}" style="left:${x(s.start)}px;width:${w}px">${s.kind === "break" ? "☕" : ""}</div>`);
          continue;
        }
        const v = visits[s.visit_id] || {};
        const cls = [v.priority >= 5 ? "p5" : v.priority === 4 ? "p4" : "p3"];
        if (v.staff === 2) cls.push("dbl");
        if (changed.has(s.visit_id)) cls.push("chg");
        if (s.locked) cls.push("lck");
        if (opts.selectedVisit === s.visit_id) cls.push("sel");
        const tip = `${s.visit_id} ${hhmm(s.start)}–${hhmm(s.end)} · ${v.recipient_id || ""} · window ${hhmm(v.earliest)}–${hhmm(v.latest)}${v.staff === 2 ? " · double-staffed" : ""}${s.locked ? " · started/locked" : ""}`;
        parts.push(`<div class="g-stop ${cls.join(" ")}" data-visit="${s.visit_id}" title="${esc(tip)}" style="left:${x(s.start)}px;width:${w}px">${esc(s.visit_id.replace("V-", ""))}</div>`);
      }
    }
    parts.push("</div>");
  }
  if (plan.clock !== null && plan.clock !== undefined) {
    parts.push(`<div class="g-clock" style="left:${x(plan.clock)}px" title="simulation clock ${hhmm(plan.clock)}"></div>`);
  }
  parts.push("</div>");
  container.innerHTML = parts.join("");
  container.onclick = (ev) => {
    const v = ev.target.closest("[data-visit]");
    if (v) return onVisit(v.dataset.visit);
    const l = ev.target.closest(".g-label");
    if (l) onEmployee(l.dataset.emp);
  };
}
