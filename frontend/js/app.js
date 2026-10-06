import { api, runJob } from "./api.js";
import { changedVisits, renderGantt } from "./gantt.js";
import { initMap, invalidate, renderItinerary, renderMap, renderTeamLegend } from "./map.js";
import { renderConflicts, renderExperiment, renderHistory, renderKpis, renderLiveFeed, renderML, renderReplan, renderScore, renderVisit, renderWeek } from "./panels.js";
import { $, $$, esc, hhmm, parseHHMM, toast } from "./util.js";

const state = {
  meta: null, health: null, scenario: null, plan: null, parent: null, lastIncident: null,
  visitsById: {}, empsById: {}, tab: "map", selectedEmp: "", selectedVisit: null, mapMode: "employee",
  week: null, // week summary (week scenarios only)
  suggestions: null, autofix: null, // unplanned-visit solutions for the current plan
  live: { job: null, feed: [] },
};

// ------------------------------------------------------------------ boot
async function boot() {
  [state.meta, state.health] = await Promise.all([api("GET", "/api/meta"), api("GET", "/api/health")]);
  const area = $("#sel-area");
  area.innerHTML = Object.entries(state.meta.areas).map(([k, a]) => `<option value="${k}">${esc(a.name)}</option>`).join("");
  for (const sel of [$("#sel-strategy"), $("#sel-strategy2")]) {
    sel.innerHTML = Object.entries(state.meta.strategies).map(([k, d]) => `<option value="${k}">${esc(d)}</option>`).join("");
  }
  $("#weights").innerHTML = Object.entries(state.meta.weights).map(([k, v]) =>
    `<label>${esc(k.replaceAll("_", " "))}<input name="w_${k}" type="number" step="any" value="${v}" /></label>`).join("");
  const h = state.health;
  setBadge("#b-version", `version ${h.version || "?"}`, "muted");
  setBadge("#b-llm", h.llm_configured ? `advisor: Claude (${h.llm_model})` : "advisor: deterministic (no API key)", h.llm_configured ? "ok" : "muted");
  setBadge("#b-travel", `travel: ${h.travel_provider}${h.google_maps_configured ? "" : " (no Google key)"}`, "muted");
  $$("#f-scenario input[type=range]").forEach((r) => {
    const out = r.nextElementSibling;
    const upd = () => (out.value = r.value);
    r.addEventListener("input", upd);
    upd();
  });
  wire();
  renderIncidentFields();
  if (!window.L) await new Promise((r) => window.addEventListener("load", r, { once: true }));
  const provider = await initMap($("#map"), state.meta.map, {
    onRecipient, onVisit: openVisit, onEmployee: selectEmployee, rerender: render,
    onProviderFallback: (msg, code) => {
      toast(msg, 15000);
      setBadge("#map-provider", `map: OpenStreetMap${code ? ` (Google: ${code})` : ""}`, "warn");
      $("#map-provider").title = msg;
    },
  });
  setBadge("#map-provider", provider === "google" ? "map: Google Maps" : "map: OpenStreetMap", provider === "google" ? "ok" : "muted");
  if (!state.meta.map?.road_geometry) $("#lbl-roads").classList.add("hidden");
}

function setBadge(sel, text, cls) {
  const b = $(sel);
  b.textContent = text;
  b.className = `badge ${cls}`;
}

// ------------------------------------------------------------------ data
function indexScenario(sc) {
  state.scenario = sc;
  state.visitsById = Object.fromEntries(sc.visits.map((v) => [v.id, v]));
  state.empsById = Object.fromEntries(sc.employees.map((e) => [e.id, e]));
  setBadge("#b-scenario", `${sc.week ? `${sc.weekday} · ` : ""}${sc.area} · ${sc.counts.interventions} interventions → ${sc.counts.visits} visits · ${sc.counts.recipients} recipients · ${sc.counts.employees} employees`, "muted");
  setBadge("#b-travel", `travel: ${sc.travel.source}${sc.travel.traffic.global_multiplier !== 1 || Object.keys(sc.travel.traffic.zone_multipliers).length ? " + traffic" : ""}`, sc.travel.source.startsWith("google") ? "ok" : "muted");
  $("#b-travel").title = (sc.travel.notes || []).join("\n") || "Active travel-time source";
  $("#sel-emp").innerHTML = sc.employees.map((e) => `<option value="${e.id}">${esc(e.id)} · ${esc(e.name)} · ${esc(e.team)}</option>`).join("");
  $("#sel-map-team").innerHTML = sc.zones.map((z) => `<option>${esc(z)}</option>`).join("");
  $("#sel-team").innerHTML = '<option value="">all teams</option>' + sc.zones.map((z) => `<option>${esc(z)}</option>`).join("");
  $("#f-plan button").disabled = false;
  renderWeekBar();
}

// ------------------------------------------------------------------ unplanned solutions
async function showPlan(pid) {
  try {
    await api("POST", `/api/plans/${pid}/activate`);
    await refreshScenario();
    await loadPlan(pid);
    render();
  } catch (err) {
    toast(err.message);
  }
}

const conflictActions = {
  async suggest() {
    try {
      state.suggestions = await runJob(api("POST", `/api/plans/${state.plan.id}/suggestions`), "Finding solutions");
      render();
    } catch (err) {
      toast(err.message, 8000);
    }
  },
  async autofix() {
    try {
      const r = await runJob(api("POST", `/api/plans/${state.plan.id}/autofix`), "Auto-fix within rules");
      state.autofix = r;
      await refreshScenario();
      await loadPlan(r.plan_id);
      render();
      toast(`${r.net_planned_gain} more visit(s) planned within the rules · ${r.unplanned_after} still unplanned`, 8000);
    } catch (err) {
      toast(err.message, 8000);
    }
  },
  async apply(opt) {
    try {
      if (opt.kind === "pool") {
        const clock = Math.max(opt.clock, (state.plan.clock ?? -1) + 1);
        const res = await runJob(api("POST", `/api/scenarios/${state.scenario.id}/incidents`, {
          kind: "extra_staff", clock, params: { count: 1, team: opt.team, lead_minutes: 30, delegations: opt.delegations, skills: opt.skills }, strategy: "baseline",
        }), "Calling in pool staff");
        state.lastIncident = res;
        await refreshScenario();
        await loadPlan(res.after_plan_id);
        render();
        toast(`Pool staff added in ${opt.team}: ${res.diff_counts.visits_changed} visits changed`);
        return;
      }
      const r = await api("POST", `/api/plans/${state.plan.id}/suggestions/${opt.id}/apply`);
      await refreshScenario();
      await loadPlan(r.plan_id);
      render();
      toast(r.status === "VALID" ? "Applied within the rules: plan VALID" : r.valid ? "Applied as an approved exception (still reported by the validator)" : `Applied, but the plan is ${r.status}`, 8000);
    } catch (err) {
      toast(err.message, 8000);
    }
  },
};

// ------------------------------------------------------------------ live day
async function startLive() {
  const f = $("#f-live");
  const body = { start: parseHHMM(f.start.value), end: parseHHMM(f.end.value), events: Number(f.events.value),
    pace_s: Number(f.pace_s.value), seed: Number(f.seed.value) };
  let job;
  try {
    job = await api("POST", `/api/scenarios/${state.scenario.id}/live`, body);
  } catch (err) {
    return toast(err.message, 8000);
  }
  state.live = { job: job.job_id, feed: [] };
  $("#btn-live").disabled = true;
  $("#btn-live-stop").disabled = false;
  let shown = null;
  try {
    for (;;) {
      await new Promise((r) => setTimeout(r, 800));
      const j = await api("GET", `/api/jobs/${job.job_id}`);
      if (j.clock != null) $("#live-clock").textContent = hhmm(j.clock);
      state.live.feed = j.feed || [];
      $("#t-live").textContent = state.live.feed.length ? String(state.live.feed.length) : "";
      const last = state.live.feed[state.live.feed.length - 1];
      $("#live-stats").textContent = last ? `${state.live.feed.length} events · ${last.unplanned_total} unplanned · ${Math.round(100 * last.interventions_planned_share * 10) / 10} % of interventions planned · last plan ${last.status}` : "running…";
      if (j.latest_plan_id && j.latest_plan_id !== shown) {
        shown = j.latest_plan_id;
        await refreshScenario();
        await loadPlan(shown);
        render();
      } else if (state.tab === "live") {
        renderLiveFeed($("#live-feed"), state.live.feed, showPlan);
      }
      if (j.status === "done" || j.status === "error") {
        if (j.status === "error") toast(j.error, 10000);
        else toast(`Live day finished: ${j.result.events} events, ${j.result.unplanned} unplanned, plan ${j.result.valid ? "VALID" : "INVALID"}`, 8000);
        break;
      }
    }
  } finally {
    state.live.job = null;
    $("#btn-live").disabled = false;
    $("#btn-live-stop").disabled = true;
    render();
  }
}

// ------------------------------------------------------------------ week
function renderWeekBar() {
  const wk = state.scenario?.week;
  $("#week-bar").classList.toggle("hidden", !wk);
  $("#tab-btn-week").classList.toggle("hidden", !wk);
  if (!wk) {
    state.week = null;
    if (state.tab === "week") switchTab("map");
    return;
  }
  $("#btn-plan-week").disabled = false;
  const valid = Object.fromEntries((state.week?.days || []).map((d) => [d.scenario_id, d.valid]));
  $("#week-days").innerHTML = wk.days.map((d) => {
    const v = valid[d.scenario_id];
    const dot = d.current_plan_id ? `<i class="dot ${v === false ? "bad" : "ok"}"></i>` : '<i class="dot"></i>';
    return `<button data-sid="${esc(d.scenario_id)}" class="${d.scenario_id === state.scenario.id ? "active" : ""}" title="${d.current_plan_id ? "planned" : "not planned yet"}">${esc(d.weekday)}${dot}</button>`;
  }).join("");
  $$("#week-days button").forEach((b) => b.addEventListener("click", () => selectDay(b.dataset.sid)));
}

async function loadWeek() {
  const wk = state.scenario?.week;
  state.week = wk ? await api("GET", `/api/weeks/${wk.id}`) : null;
}

async function selectDay(sid) {
  try {
    indexScenario(await api("GET", `/api/scenarios/${sid}`));
    state.lastIncident = null;
    if (state.scenario.current_plan_id) {
      await loadPlan(state.scenario.current_plan_id);
    } else {
      state.plan = state.parent = null;
      setBadge("#b-valid", "not validated", "muted");
      $("#f-incident button").disabled = true;
      $("#kpis").innerHTML = `<div class="empty">${esc(state.scenario.weekday)}: ${state.scenario.counts.visits} visits from ${state.scenario.counts.interventions} interventions, ${state.scenario.counts.employees} employees on duty. Optimise this day or the whole week.</div>`;
    }
    render();
  } catch (err) {
    toast(err.message);
  }
}

function planBody() {
  const f = $("#f-plan");
  const weights = {};
  $$("#weights input").forEach((i) => (weights[i.name.slice(2)] = Number(i.value)));
  return { strategy: f.strategy.value, time_limit_s: Number(f.time_limit_s.value), weights, max_overtime_min: Number(f.max_overtime_min.value) };
}

async function planWeek() {
  const wk = state.scenario?.week;
  if (!wk) return;
  const btn = $("#btn-plan-week");
  btn.disabled = true;
  try {
    const res = await runJob(api("POST", `/api/weeks/${wk.id}/plan`, planBody()), "Optimising week");
    await loadWeek();
    await selectDay(state.scenario.id);
    switchTab("week");
    toast(`Week optimised: ${state.week.totals.planned} of ${state.week.totals.visits} visits planned · days ${res.valid ? "VALID" : "with violations"} · week rules ${state.week.validation.valid ? "OK" : "VIOLATED"}`, 8000);
  } catch (err) {
    toast(err.message, 8000);
  } finally {
    btn.disabled = false;
  }
}

async function loadPlan(pid) {
  state.plan = await api("GET", `/api/plans/${pid}`);
  state.parent = state.plan.parent_plan_id ? await api("GET", `/api/plans/${state.plan.parent_plan_id}`) : null;
  const v = state.plan.validation;
  const nexc = v.approved_exceptions?.length || 0;
  setBadge("#b-valid", v.valid ? (nexc ? `VALID · ${nexc} approved exception(s)` : "VALID ✓ (independent validator)") : `INVALID · ${v.error_count} violations`,
    v.valid ? (nexc ? "warn" : "ok") : "bad");
  $("#f-incident button").disabled = false;
  $("#btn-live").disabled = !!state.live.job;
  if (state.suggestions && state.suggestions.plan_id !== state.plan.id) state.suggestions = null;
  if (state.plan.clock !== null && state.plan.clock !== undefined) {
    const cur = parseHHMM($("#f-incident [name=clock]").value);
    if (cur < state.plan.clock) $("#f-incident [name=clock]").value = hhmm(state.plan.clock + 5);
  }
  renderIncidentFields();
}

async function refreshScenario() {
  indexScenario(await api("GET", `/api/scenarios/${state.scenario.id}`));
}

// ------------------------------------------------------------------ render
function render() {
  renderKpis($("#kpis"), state.plan, state.parent);
  const n = state.plan ? Object.keys(state.plan.unplanned).length : 0;
  $("#t-conf").textContent = n ? String(n) : "";
  renderHistory($("#plan-history"), state, async (pid) => {
    await api("POST", `/api/plans/${pid}/activate`);
    await refreshScenario();
    await loadPlan(pid);
    state.lastIncident = null;
    render();
  });
  const t = state.tab;
  if (t === "map") renderMapTab();
  if (t === "timeline") renderGantt($("#gantt"), state, {
    team: $("#sel-team").value, sort: $("#sel-sort").value, onlyChanged: $("#chk-changed").checked, selectedVisit: state.selectedVisit,
  }, openVisit, selectEmployee);
  if (t === "conflicts") renderConflicts($("#conflicts"), state, openVisit, conflictActions);
  if (t === "live") renderLiveFeed($("#live-feed"), state.live.feed, showPlan);
  if (t === "ml") {
    renderML($("#ml"), state.ml);
    $("#btn-ml-cases").disabled = !state.plan;
    api("GET", "/api/ml").then((d) => { state.ml = d; if (state.tab === "ml") renderML($("#ml"), d); }).catch((e) => toast(e.message));
  }
  if (t === "replan") renderReplan($("#replan"), state, openVisit);
  if (t === "score") renderScore($("#score"), state.plan);
  if (t === "week") {
    renderWeek($("#week"), state.week, selectDay);
    if (state.scenario?.week) loadWeek().then(() => { if (state.tab === "week") renderWeek($("#week"), state.week, selectDay); renderWeekBar(); }).catch((e) => toast(e.message));
  }
}

function employeesWithRoutes() {
  const plan = state.plan;
  if (!plan) return [];
  return state.scenario.employees.filter((e) => plan.routes[e.id]?.stops.some((s) => s.kind === "visit")).map((e) => e.id);
}

function ensureSelectedEmployee() {
  const ids = employeesWithRoutes();
  if (ids.length && !ids.includes(state.selectedEmp)) {
    // Start with the busiest employee: the clearest first example of a day.
    const n = (e) => state.plan.routes[e].stops.filter((s) => s.kind === "visit").length;
    state.selectedEmp = ids.reduce((a, b) => (n(b) > n(a) ? b : a), ids[0]);
  }
  if (state.selectedEmp) $("#sel-emp").value = state.selectedEmp;
}

function setMapMode(mode, persist = true) {
  if (persist) state.mapMode = mode;
  $$("#map-mode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $$("#tab-map [data-for]").forEach((el) => el.classList.toggle("hidden", el.dataset.for !== mode || (el.id === "lbl-roads" && !state.meta.map?.road_geometry)));
}

function renderMapTab() {
  setMapMode(state.plan ? state.mapMode : "all", false); // without a plan only the overview exists
  const side = $("#map-side");
  if (state.plan && state.mapMode === "employee") ensureSelectedEmployee();
  if (!state.plan) side.innerHTML = '<div class="hint">Recipients are shown. Optimise the day to see each employee\'s route.</div>';
  else if (state.mapMode === "all") {
    const n = Object.keys(state.plan.unplanned).length;
    side.innerHTML = `<div class="it-head"><b>Overview</b><div class="hint">Every care recipient: green = all visits planned, red = at least one visit unplanned (${n} visits). Black houses are team offices. Switch to <b>One employee's day</b> to follow a route stop by stop.</div></div>`;
  }
  renderMap(state, {
    mode: state.plan ? state.mapMode : "all",
    employee: state.selectedEmp,
    team: $("#sel-map-team").value,
    followRoads: $("#chk-roads").checked && !!state.meta.map?.road_geometry,
    showAll: $("#chk-routes").checked,
    changed: changedVisits(state.plan, state.parent),
    selectedVisit: state.selectedVisit,
    onGeometry: (src) => renderItinerary(side, state, state.selectedEmp, src, openVisit),
    onTeam: (list) => renderTeamLegend(side, list, selectEmployee),
  });
}

function selectEmployee(eid) {
  state.selectedEmp = eid;
  $("#sel-emp").value = eid;
  setMapMode("employee");
  switchTab("map");
}

function stepEmployee(delta) {
  const ids = employeesWithRoutes();
  if (!ids.length) return;
  const i = Math.max(0, ids.indexOf(state.selectedEmp));
  selectEmployee(ids[(i + delta + ids.length) % ids.length]);
}

function onRecipient(rid) {
  const vids = state.scenario.visits.filter((v) => v.recipient_id === rid).map((v) => v.id);
  if (vids.length) openVisit(vids.find((v) => state.plan?.unplanned[v]) || vids[0]);
}

async function openVisit(vid) {
  if (!state.plan) return;
  state.selectedVisit = vid;
  try {
    const d = await api("GET", `/api/plans/${state.plan.id}/visits/${vid}`);
    renderVisit($("#drawer-body"), d, state, {
      lock: async (id, locked) => {
        await api("POST", `/api/scenarios/${state.scenario.id}/visits/${id}/lock`, { locked });
        await refreshScenario();
        toast(locked ? `${id} locked: re-planning will keep its employee and time` : `${id} unlocked`);
        openVisit(id);
      },
      medMove: (id) => {
        $("#sel-incident").value = "medication_moved";
        renderIncidentFields(id);
        $("#drawer").classList.add("hidden");
        toast("Set the new time in the incident panel and apply.");
      },
    });
    $("#drawer").classList.remove("hidden");
  } catch (e) {
    toast(e.message);
  }
}

function switchTab(tab) {
  state.tab = tab;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab").forEach((s) => s.classList.toggle("hidden", s.id !== `tab-${tab}`));
  if (tab === "map") invalidate();
  render();
}

// ------------------------------------------------------------- incidents
function employeeOptions(onlyWithFuture) {
  const clock = parseHHMM($("#f-incident [name=clock]").value);
  const plan = state.plan;
  return (state.scenario?.employees || [])
    .filter((e) => !onlyWithFuture || (plan?.routes[e.id]?.stops || []).some((s) => s.kind === "visit" && s.start > clock))
    .map((e) => `<option value="${e.id}">${esc(e.id)} · ${esc(e.team)} · ${hhmm(e.shift_start)}–${hhmm(e.shift_end)}</option>`).join("");
}

function renderIncidentFields(preVisit) {
  const kind = $("#sel-incident").value;
  const box = $("#incident-fields");
  const zones = state.scenario?.zones || [];
  const clock = parseHHMM($("#f-incident [name=clock]").value);
  let html = "";
  if (kind === "employee_sick") {
    html = `<label>Employees <select name="employee_ids" multiple size="5">${employeeOptions(true)}</select></label>
      <label>…or random <input name="random" type="number" min="0" max="20" value="0" /></label>
      <label>Random seed <input name="seed" type="number" value="7" /></label>`;
  } else if (kind === "employee_delayed") {
    html = `<label>Employee <select name="employee_id">${employeeOptions(true)}</select></label>
      <label>Delay (min) <input name="minutes" type="number" value="25" min="5" max="240" /></label>`;
  } else if (kind === "medication_moved") {
    const vs = (state.scenario?.visits || []).filter((v) => v.has_medication && state.plan?.assignments[v.id] && state.plan.assignments[v.id].start > clock);
    html = `<label>Visit <select name="visit_id">${vs.map((v) => `<option value="${v.id}" ${v.id === preVisit ? "selected" : ""}>${esc(v.id)} · ${esc(v.recipient_id)} · planned ${hhmm(state.plan.assignments[v.id].start)}</option>`).join("")}</select></label>
      <label>New time <input name="new_time" type="time" value="${hhmm(Math.max(clock + 40, 9 * 60))}" /></label>
      <label>Window ± (min) <input name="window_minutes" type="number" value="15" min="0" max="120" /></label>`;
  } else if (kind === "traffic") {
    html = `<label>Scope <select name="scope"><option value="zone">zone</option><option value="corridor">corridor (zone ↔ zone)</option><option value="global">global</option></select></label>
      <label>Zone <select name="zone">${zones.map((z) => `<option>${esc(z)}</option>`).join("")}</select></label>
      <label>Corridor to <select name="zone2">${zones.map((z) => `<option>${esc(z)}</option>`).join("")}</select></label>
      <label>Multiplier <input name="multiplier" type="number" step="0.1" value="1.6" min="1" max="5" /></label>`;
  } else if (kind === "extra_staff") {
    html = `<label>Count <input name="count" type="number" value="2" min="1" max="20" /></label>
      <label>Team <select name="team">${zones.map((z) => `<option>${esc(z)}</option>`).join("")}</select></label>
      <label>Ready after (min) <input name="lead_minutes" type="number" value="30" min="0" max="240" /></label>`;
  } else if (kind === "child_sick") {
    html = `<label>Employee <select name="employee_id">${employeeOptions(true)}</select></label>
      <label>Away <select name="return_minutes"><option value="">rest of the day</option><option value="120">2 h, then back</option><option value="180">3 h, then back</option></select></label>`;
  } else if (kind === "alarm") {
    const rs = state.scenario?.recipients || [];
    html = `<label>Recipient <select name="recipient_id"><option value="">random (no visit in progress)</option>${rs.map((r) => `<option value="${r.id}">${esc(r.id)} · ${esc(r.zone)}</option>`).join("")}</select></label>
      <label>Duration (min) <input name="duration" type="number" value="20" min="5" max="90" /></label>
      <label>Must be on site within (min) <input name="max_response_minutes" type="number" value="45" min="5" max="180" /></label>`;
  } else if (kind === "visit_cancelled") {
    const vs = (state.scenario?.visits || []).filter((v) => state.plan?.assignments[v.id] && state.plan.assignments[v.id].start > clock);
    html = `<label>Visit <select name="visit_id">${vs.map((v) => `<option value="${v.id}">${esc(v.id)} · ${esc(v.recipient_id)} · ${hhmm(state.plan.assignments[v.id].start)}</option>`).join("")}</select></label>`;
  }
  box.innerHTML = html;
}

function incidentParams(form) {
  const kind = form.kind.value;
  const f = (n) => form.querySelector(`[name=${n}]`);
  if (kind === "employee_sick") {
    const rnd = Number(f("random").value);
    if (rnd > 0) return { random: rnd, seed: Number(f("seed").value) };
    const ids = [...f("employee_ids").selectedOptions].map((o) => o.value);
    if (!ids.length) throw new Error("Select at least one employee or a random count.");
    return { employee_ids: ids };
  }
  if (kind === "employee_delayed") return { employee_id: f("employee_id").value, minutes: Number(f("minutes").value) };
  if (kind === "medication_moved") {
    if (!f("visit_id").value) throw new Error("No future medication visit available.");
    return { visit_id: f("visit_id").value, new_time: parseHHMM(f("new_time").value), window_minutes: Number(f("window_minutes").value) };
  }
  if (kind === "traffic") {
    const scope = f("scope").value;
    const m = Number(f("multiplier").value);
    if (scope === "global") return { global_multiplier: m };
    if (scope === "corridor") return { corridor: [f("zone").value, f("zone2").value], multiplier: m };
    return { zone: f("zone").value, multiplier: m };
  }
  if (kind === "extra_staff") return { count: Number(f("count").value), team: f("team").value, lead_minutes: Number(f("lead_minutes").value) };
  if (kind === "visit_cancelled") return { visit_id: f("visit_id").value };
  if (kind === "child_sick") {
    const back = f("return_minutes").value;
    return { employee_id: f("employee_id").value, ...(back ? { return_minutes: Number(back) } : {}) };
  }
  if (kind === "alarm") {
    let rid = f("recipient_id").value;
    if (!rid) {
      const clock = parseHHMM(form.clock.value);
      const busy = new Set(Object.values(state.plan.assignments).filter((a) => a.start - 40 < clock && clock < a.end + 90).map((a) => state.visitsById[a.visit_id]?.recipient_id));
      const free = (state.scenario.recipients || []).map((r) => r.id).filter((r) => !busy.has(r));
      rid = free[Math.floor(Math.random() * free.length)];
    }
    return { recipient_id: rid, duration: Number(f("duration").value), max_response_minutes: Number(f("max_response_minutes").value) };
  }
  return {};
}

// ------------------------------------------------------------------ wiring
function wire() {
  $$("#tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  $("#drawer-close").addEventListener("click", () => $("#drawer").classList.add("hidden"));
  $("#sel-emp").addEventListener("change", (e) => { state.selectedEmp = e.target.value; render(); });
  $("#chk-routes").addEventListener("change", render);
  $("#chk-roads").addEventListener("change", render);
  $("#sel-map-team").addEventListener("change", render);
  $("#btn-prev-emp").addEventListener("click", () => stepEmployee(-1));
  $("#btn-next-emp").addEventListener("click", () => stepEmployee(1));
  $$("#map-mode button").forEach((b) => b.addEventListener("click", () => { setMapMode(b.dataset.mode); render(); }));
  for (const id of ["#sel-team", "#sel-sort", "#chk-changed"]) $(id).addEventListener("change", render);
  $("#sel-incident").addEventListener("change", () => renderIncidentFields());
  $("#f-incident [name=clock]").addEventListener("change", () => renderIncidentFields());

  $("#f-scenario").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = {};
    for (const [k, v] of fd.entries()) body[k] = ["area", "travel_provider"].includes(k) ? v : Number(v);
    body.breaks_enabled = e.target.breaks_enabled.checked;
    body.preferences_enabled = e.target.preferences_enabled.checked;
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      indexScenario(await runJob(api("POST", "/api/scenarios?background=true", body), "Generating scenario"));
      state.plan = state.parent = state.lastIncident = null;
      setBadge("#b-valid", "not validated", "muted");
      $("#f-incident button").disabled = true;
      state.week = null;
      $("#kpis").innerHTML = `<div class="empty">${state.scenario.week ? "Week ready (Mon–Sun). Monday: " : "Scenario ready: "} ${state.scenario.counts.visits} visits built from ${state.scenario.counts.interventions} interventions (${state.scenario.counts.double_staffed} double-staffed, ${state.scenario.counts.hard_windows} hard windows, ${state.scenario.counts.with_requirements} with skill/delegation requirements; ${Math.round(state.scenario.counts.care_minutes / 60)} care hours). ${state.scenario.week ? "Optimise one day, or the whole week with the button above." : "Now optimise the day."}</div>`;
      render();
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  $("#btn-plan-week").addEventListener("click", planWeek);
  $("#btn-ml-train").addEventListener("click", async () => {
    try {
      state.ml = await runJob(api("POST", "/api/ml/train"), "Training");
      renderML($("#ml"), state.ml);
    } catch (err) {
      toast(err.message, 8000);
    }
  });
  $("#btn-ml-cases").addEventListener("click", async () => {
    const n = Number($("#ml-n").value) || 4;
    try {
      state.ml = await runJob(api("POST", `/api/scenarios/${state.scenario.id}/ml/cases?n=${n}&seed=${Date.now() % 100000}`), "Generating training cases");
      renderML($("#ml"), state.ml);
      toast(`${state.ml.added} new cases from this scenario; model retrained on ${state.ml.cases_total} cases`, 8000);
    } catch (err) {
      toast(err.message, 8000);
    }
  });
  $("#f-live").addEventListener("submit", (e) => { e.preventDefault(); startLive(); });
  $("#btn-live-stop").addEventListener("click", () => state.live.job && api("POST", `/api/jobs/${state.live.job}/cancel`).catch(() => {}));
  $("#f-plan").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const btn = f.querySelector("button");
    btn.disabled = true;
    try {
      const res = await runJob(api("POST", `/api/scenarios/${state.scenario.id}/plan`, planBody()), "Optimising");
      await refreshScenario();
      state.autofix = null;
      await loadPlan(res.plan_id);
      state.lastIncident = null;
      render();
      toast(res.valid ? "Plan optimised and independently validated: VALID" : "Plan produced but validation FAILED – see Score & validation");
    } catch (err) {
      toast(err.message, 8000);
    } finally {
      btn.disabled = false;
    }
  });

  $("#f-incident").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    let params;
    try {
      params = incidentParams(f);
    } catch (err) {
      return toast(err.message);
    }
    const btn = f.querySelector("button");
    btn.disabled = true;
    try {
      const res = await runJob(api("POST", `/api/scenarios/${state.scenario.id}/incidents`, {
        kind: f.kind.value, clock: parseHHMM(f.clock.value), params, strategy: f.strategy.value,
      }), "Re-planning");
      state.lastIncident = res;
      await refreshScenario();
      await loadPlan(res.after_plan_id);
      switchTab("replan");
      toast(`${res.diff_counts.visits_changed} visits changed · ${res.strategy_used}`);
    } catch (err) {
      toast(err.message, 8000);
    } finally {
      btn.disabled = false;
    }
  });

  $("#f-exp").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const strategies = $$("input[name=s]:checked", f).map((i) => i.value);
    const btn = f.querySelector("button");
    btn.disabled = true;
    $("#experiment").innerHTML = '<div class="empty">Running… every strategy re-plans the same incident sequence; this takes a few minutes.</div>';
    try {
      const r = await runJob(api("POST", "/api/experiments", {
        scenario: { seed: Number(f.seed.value), target_interventions: Number(f.target_interventions.value), employee_count: Number(f.employee_count.value) },
        strategies, base_time_limit_s: Number(f.base_time_limit_s.value),
      }), "Experiment");
      renderExperiment($("#experiment"), r);
    } catch (err) {
      $("#experiment").innerHTML = `<div class="empty">Experiment failed: ${esc(err.message)}</div>`;
    } finally {
      btn.disabled = false;
    }
  });
}

boot().catch((e) => toast(`Failed to start: ${e.message}`, 10000));
