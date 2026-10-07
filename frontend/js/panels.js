import { esc, fmtNum, hhmm, pct } from "./util.js";

const KPIS = [
  ["interventions_planned_share", "Interventions planned", "pct", -1],
  ["interventions_unplanned", "Unplanned interventions", 0, 1],
  ["planned_visits", "Planned visits", 0, -1],
  ["unplanned_visits", "Unplanned visits", 0, 1],
  ["unplanned_high_priority", "Unplanned prio 5", 0, 1],
  ["hard_violations", "Hard violations", 0, 1],
  ["travel_minutes", "Travel (min)", 0, 1],
  ["avg_travel_per_visit", "Travel / visit", 1, 1],
  ["p90_leg_minutes", "90 % of trips ≤ (min)", 0, 1],
  ["longest_leg_minutes", "Longest trip (min)", 0, 1],
  ["preferred_time_deviation_avg", "Pref. deviation avg (min)", 1, 1],
  ["continuity_known_share", "Continuity (known staff)", "pct", -1],
  ["gender_wish_met_share", "Gender wish met", "pct", -1],
  ["language_wish_met_share", "Language wish met", "pct", -1],
  ["overtime_minutes", "Overtime (min)", 0, 1],
  ["workload_std_minutes", "Workload std (min)", 0, 1],
  ["utilization", "Utilisation", "pct", 0],
  ["solver_runtime_s", "Solver runtime (s)", 1, 0],
];

function kv(v, d) {
  return d === "pct" ? pct(v) : fmtNum(v, d);
}

export function renderKpis(el, plan, parent) {
  if (!plan) return;
  const s = plan.score;
  const ps = parent?.score;
  el.innerHTML = KPIS.map(([k, label, d, dir]) => {
    const v = s[k];
    let delta = "";
    if (ps && ps[k] !== undefined && ps[k] !== null && v !== null && dir !== 0) {
      const dv = v - ps[k];
      if (Math.abs(dv) > 1e-9) {
        const worse = dir * dv > 0;
        delta = `<div class="d ${worse ? "up" : "down"}">${dv > 0 ? "+" : ""}${d === "pct" ? Math.round(dv * 100) + " pp" : fmtNum(dv, d)}</div>`;
      }
    }
    const cls = k === "hard_violations" ? (v ? "bad" : "ok") : k === "unplanned_high_priority" && v ? "bad" : "";
    const sub = k === "interventions_planned_share" && s.interventions_total
      ? `<div class="l">${fmtNum(s.interventions_planned)} of ${fmtNum(s.interventions_total)}</div>` : "";
    return `<div class="kpi ${cls}"><div class="v">${kv(v, d)}</div><div class="l">${label}</div>${sub}${delta}</div>`;
  }).join("");
  const stab = s.stability || {};
  if (stab.compared_to) {
    el.innerHTML += `<div class="kpi"><div class="v">${fmtNum(stab.visits_changed)}</div><div class="l">Visits changed vs previous</div><div class="d">${stab.employee_changes} reassigned · ${stab.start_time_changes} retimed · ${stab.routes_changed} routes</div></div>`;
  }
}

const LEVEL = { within_rules: "within rules", minor: "minor deviation", major: "major deviation", not_permitted: "not permitted", resource: "extra resource" };

function suggestionBlock(opts) {
  if (!opts) return "";
  if (!opts.length) return '<div class="hint">No option found.</div>';
  return `<div class="sugg">${opts.slice(0, 5).map((o) => `<div class="opt">
      <span class="lvl ${o.level}">${LEVEL[o.level] || o.level}</span>
      <span class="pts" title="severity points (0 = within all rules)">${o.severity} pts</span>
      <b>${esc(o.title)}</b>
      ${o.kind === "replan" ? `<span class="v">${o.changes} visit(s) change in ${o.routes.length} routes${o.also_planned?.length ? ` · also plans ${esc(o.also_planned.join(", "))}` : ""}</span>` : ""}
      ${o.violations.length ? `<span class="v">${o.violations.map((x) => esc(x.text)).join(" · ")}</span>` : ""}
      ${o.pushed_visits?.length ? `<span class="v">moves ${esc(o.pushed_visits.join(", "))}</span>` : ""}
      <button data-apply="${o.id}" data-kind="${o.kind}">${o.kind === "pool" ? "Call in pool staff" : o.severity ? "Apply as approved exception" : "Apply"}</button>
    </div>`).join("")}</div>`;
}

export function renderConflicts(el, state, onVisit, actions = {}) {
  const plan = state.plan;
  if (!plan) return (el.innerHTML = '<div class="empty">No plan yet.</div>');
  const rows = Object.values(plan.unplanned).map((u) => ({ u, v: state.visitsById[u.visit_id] })).filter((x) => x.v);
  rows.sort((a, b) => b.v.priority - a.v.priority || a.v.earliest - b.v.earliest);
  const s = plan.score;
  const ivLine = `<p><b>${fmtNum(s.interventions_planned)} of ${fmtNum(s.interventions_total)} interventions planned (${pct(s.interventions_planned_share)})</b> · ${fmtNum(s.interventions_unplanned)} interventions in ${rows.length} unplanned visits</p>`;
  const exc = plan.validation?.approved_exceptions?.length
    ? `<p class="hint">${plan.validation.approved_exceptions.length} rule deviation(s) in this plan are approved exceptions: ${plan.exceptions.map((x) => esc(x.visit_id)).join(", ")}.</p>` : "";
  if (!rows.length) return (el.innerHTML = `${ivLine}${exc}<div class="empty">Every visit is planned. 🎉</div>`);
  const sug = state.suggestions && state.suggestions.plan_id === plan.id ? state.suggestions : null;
  const fix = state.autofix;
  const byCode = {};
  for (const { u } of rows) {
    const c = u.reasons[0]?.code || "UNKNOWN";
    byCode[c] = (byCode[c] || 0) + 1;
  }
  const ss = sug?.summary;
  el.innerHTML = `
    <h3>${rows.length} visits cannot currently be planned</h3>
    ${ivLine}${exc}
    <div class="actions">
      <button class="primary" id="btn-suggest">${sug ? "Recompute solutions" : "Find solutions for every unplanned visit"}</button>
      <button id="btn-autofix" title="Deep re-optimisation of all open visits, then a local re-plan of the nearest routes per visit. Only valid plans that lose no planned visit are kept.">Auto-fix within rules</button>
    </div>
    ${fix ? `<p class="hint">Auto-fix: ${fix.net_planned_gain} more visit(s) planned within the rules (${fix.unplanned_before} → ${fix.unplanned_after} unplanned; ${pct(fix.interventions_planned_share_before)} → ${pct(fix.interventions_planned_share_after)} of interventions), ${fix.seconds} s.</p>` : ""}
    ${ss ? `<p>Best option per visit: <span class="lvl within_rules">within rules</span> ${ss.within_rules} · <span class="lvl minor">minor</span> ${ss.minor} · <span class="lvl resource">extra resource</span> ${ss.resource} · <span class="lvl major">major</span> ${ss.major} · <span class="lvl not_permitted">not permitted</span> ${ss.not_permitted}. Options are sorted by severity, least first; applying one with deviations records an approved exception that the validator keeps reporting.</p>` : ""}
    <p>${Object.entries(byCode).map(([c, n]) => `<span class="code">${esc(c)}</span> ${n}`).join(" &nbsp; ")}</p>
    <p class="hint">Reasons are computed from the final plan by deterministic gap analysis (other visits fixed). The engine never forces an invalid assignment or shortens care to make a visit fit.</p>
    <div class="scroll"><table>
      <thead><tr><th>Visit</th><th>Recipient</th><th>Window</th><th class="num">Duration</th><th class="num">Prio</th><th>Staff</th><th>Requires</th><th>Why</th></tr></thead>
      <tbody>${rows.map(({ u, v }) => `
        <tr class="click" data-visit="${v.id}">
          <td><b>${esc(v.id)}</b></td><td>${esc(v.recipient_id)}</td>
          <td>${hhmm(v.earliest)}–${hhmm(v.latest)} <span class="pill">${v.timing}</span></td>
          <td class="num">${v.duration}</td><td class="num">${v.priority}</td><td>${v.staff === 2 ? "2 (sync)" : "1"}</td>
          <td>${[...v.delegations, ...v.skills].map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</td>
          <td>${u.reasons.map((r) => `<div><span class="code ${r.code.startsWith("NO_") ? "bad" : ""}">${esc(r.code)}</span> ${esc(r.message)}</div>`).join("")}
            ${sug ? suggestionBlock(sug.visits[v.id]) : ""}</td>
        </tr>`).join("")}</tbody></table></div>`;
  el.querySelectorAll("tr[data-visit]").forEach((tr) => tr.addEventListener("click", (ev) => {
    if (ev.target.closest("button")) return;
    onVisit(tr.dataset.visit);
  }));
  el.querySelector("#btn-suggest")?.addEventListener("click", () => actions.suggest?.());
  el.querySelector("#btn-autofix")?.addEventListener("click", () => actions.autofix?.());
  el.querySelectorAll("button[data-apply]").forEach((b) => b.addEventListener("click", () => {
    const opt = Object.values(sug.visits).flat().find((o) => o.id === b.dataset.apply);
    if (opt) actions.apply?.(opt);
  }));
}

export function renderScore(el, plan) {
  if (!plan) return (el.innerHTML = '<div class="empty">No plan yet.</div>');
  const s = plan.score;
  const v = plan.validation;
  const w = s.weighted || {};
  const st = plan.solver_stats || {};
  const errs = v.errors || [];
  el.innerHTML = `
    <div class="grid2">
      <div>
        <h3>Independent validation: <span class="badge ${v.valid ? "ok" : "bad"}">${v.valid ? "VALID" : "INVALID"}</span></h3>
        <p class="hint">The validator is a separate module that re-checks every hard rule from the raw scenario, travel matrix and plan. It does not trust the optimizer.</p>
        <table><tbody>${Object.entries(v.checks || {}).map(([k, n]) => `<tr><td>${esc(k)}</td><td class="num">${fmtNum(n)} checks</td></tr>`).join("")}</tbody></table>
        ${errs.length ? `<h4>Errors (${v.error_count})</h4><div class="scroll"><table><tbody>${errs.map((e) => `<tr><td><span class="code bad">${esc(e.code)}</span></td><td>${esc(e.message)}</td></tr>`).join("")}</tbody></table></div>` : "<p>No hard-constraint violations.</p>"}
        ${(v.warnings || []).length ? `<h4>Warnings (${v.warning_count})</h4><table><tbody>${v.warnings.map((e) => `<tr><td><span class="code">${esc(e.code)}</span></td><td>${esc(e.message)}</td></tr>`).join("")}</tbody></table>` : ""}
      </div>
      <div>
        <h3>Weighted objective breakdown</h3>
        <p class="hint">Same weights as the optimizer, recomputed from the plan. Lower is better. Hard constraints are never part of this score.</p>
        <table><thead><tr><th>Term</th><th class="num">Points</th></tr></thead><tbody>
          ${Object.entries(w).filter(([k]) => k !== "total").map(([k, n]) => `<tr><td>${esc(k.replaceAll("_", " "))}</td><td class="num">${fmtNum(n)}</td></tr>`).join("")}
          <tr><th>Total</th><th class="num">${fmtNum(w.total)}</th></tr></tbody></table>
        <h4>Raw KPIs</h4>
        <table><tbody>${Object.entries(s).filter(([k, x]) => typeof x !== "object" || x === null).map(([k, x]) => `<tr><td>${esc(k.replaceAll("_", " "))}</td><td class="num">${esc(typeof x === "number" ? fmtNum(x, 3) : x)}</td></tr>`).join("")}</tbody></table>
      </div>
    </div>
    <h3>Solver pipeline</h3>
    <div class="pre">${esc(JSON.stringify({ construction: st.construction, routing: st.routing, repair_insertion: st.repair_insertion, enhanced_repair: st.enhanced_repair, timetabling: st.timetabling, gap_fill: st.gap_fill, total_s: st.seconds_total, replan: st.replan }, null, 2))}</div>`;
}

export function renderReplan(el, state, onVisit) {
  const ev = state.lastIncident;
  const hist = (state.scenario?.history || []).filter((h) => h.type === "incident");
  if (!ev && !hist.length) return (el.innerHTML = '<div class="empty">Apply an incident (left panel) to see a before/after comparison.</div>');
  const cur = ev || hist[hist.length - 1];
  const rows = (ev?.diff?.rows || []).slice().sort((a, b) => (a.change > b.change ? 1 : -1));
  const c = cur.diff_counts || {};
  el.innerHTML = `
    <h3>Re-planning report</h3>
    <div class="pre">${esc(cur.summary)}</div>
    ${cur.meta?.advisor ? `<p><b>Advisor:</b> ${esc(cur.meta.advisor.source)} — ${esc(cur.meta.advisor.rationale)} ${cur.meta.advisor.usage?.cost_usd ? `(API cost $${cur.meta.advisor.usage.cost_usd})` : ""}</p>` : ""}
    ${cur.meta?.ranking ? `<p><b>Classifier ranking (phase: score):</b> ${cur.meta.ranking.map(([k, v]) => `${k}: ${fmtNum(v, 1)}`).join(" · ")}</p>` : ""}
    <div class="grid2">
      <div><h4>Repair phases</h4><table><thead><tr><th>Phase</th><th class="num">Employees</th><th class="num">Free / pinned visits</th><th class="num">s</th><th>Result</th></tr></thead><tbody>
        ${(cur.phases || []).map((p) => `<tr><td>${p.phase}. ${esc(p.name)}</td><td class="num">${p.employees}</td><td class="num">${p.free_visits} / ${p.pinned_visits}</td><td class="num">${p.seconds}</td><td>${p.accepted ? "✅ accepted" : esc(p.reason)}${p.valid ? "" : " · INVALID"}</td></tr>`).join("")}
      </tbody></table><p>Strategy used: <b>${esc(cur.strategy_used)}</b></p></div>
      <div><h4>What changed</h4><table><tbody>
        ${Object.entries(c).map(([k, n]) => `<tr><td>${esc(k.replaceAll("_", " "))}</td><td class="num">${fmtNum(n)}</td></tr>`).join("")}</tbody></table></div>
    </div>
    ${rows.length ? `<h4>Changed visits (${rows.length})</h4><div class="scroll"><table><thead><tr><th>Visit</th><th>Change</th><th>Before</th><th>After</th><th class="num">Δ min</th><th>Window</th><th>Note</th></tr></thead><tbody>
      ${rows.map((r) => `<tr class="click" data-visit="${r.visit_id}"><td>${esc(r.visit_id)}${r.double_staffed ? " 👥" : ""}</td><td><span class="code">${esc(r.change)}</span></td>
        <td>${esc(r.before_employees.join("+"))} ${hhmm(r.before_start)}</td><td>${esc(r.after_employees.join("+"))} ${hhmm(r.after_start)}</td>
        <td class="num">${r.shift_minutes ?? ""}</td><td>${r.window ? `${hhmm(r.window[0])}–${hhmm(r.window[1])}` : ""}</td><td>${esc(r.unplanned_reason || "")}</td></tr>`).join("")}
    </tbody></table></div>` : ""}`;
  el.querySelectorAll("tr[data-visit]").forEach((tr) => tr.addEventListener("click", () => onVisit(tr.dataset.visit)));
}

export function renderVisit(el, d, state, actions) {
  const v = d.visit;
  const a = d.assignment;
  const rp = d.replanning;
  el.innerHTML = `
    <h3>${esc(v.id)} <span class="badge ${v.status === "planned" ? "ok" : "bad"}">${esc(v.status)}</span> ${v.locked ? '<span class="badge warn">locked</span>' : ""}</h3>
    <dl class="kv">
      <dt>Recipient</dt><dd>${esc(d.recipient.id)} · ${esc(d.recipient.name)} (synthetic)<br>${esc(d.recipient.address)}</dd>
      <dt>Total duration</dt><dd>${v.total_duration} min (full duration reserved)</dd>
      <dt>Start window</dt><dd>${esc(v.window_text)} · <b>${esc(v.timing)}</b> timing</dd>
      <dt>Preferred start</dt><dd>${hhmm(v.preferred_start)}</dd>
      <dt>Priority</dt><dd>${v.priority}</dd>
      <dt>Staff</dt><dd>${v.required_employee_count}${v.required_employee_count === 2 ? " (synchronised start)" : ""}</dd>
      <dt>Skills (all staff)</dt><dd>${v.required_skills.map((x) => `<span class="pill">${esc(x)}</span>`).join("") || "–"}</dd>
      <dt>Delegations (lead)</dt><dd>${v.required_delegations.map((x) => `<span class="pill">${esc(x)}</span>`).join("") || "–"}</dd>
      <dt>Wishes</dt><dd>${wishText(d.recipient) || "–"}</dd>
      <dt>Continuity</dt><dd>knows: ${esc(d.recipient.known_employees.join(", "))}${d.recipient.preferred_employees.length ? `<br>preferred: ${esc(d.recipient.preferred_employees.join(", "))}` : ""}${d.recipient.avoid_employees.length ? `<br>must avoid: ${esc(d.recipient.avoid_employees.join(", "))}` : ""}</dd>
    </dl>
    ${a ? `<h4>Assignment · ${esc(a.start_text)} (${a.preferred_deviation >= 0 ? "+" : ""}${a.preferred_deviation} min vs preferred) · continuity ${a.continuity_score}</h4>
      ${a.staff.map((s) => `<div class="card" style="margin:6px 0;padding:8px">
        <b>${esc(s.employee_id)}</b> ${esc(s.name)} · <span class="pill">${s.role}</span> ${hhmm(s.start)}–${hhmm(s.end)}
        <div class="hint">from ${esc(s.previous_stop)} (${s.travel_from_previous} min) → to ${esc(s.next_stop)} (${s.travel_to_next} min) · ${s.qualified_alternatives_on_shift} other qualified employees on shift</div>
        <ul>${s.why.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>`).join("")}` : ""}
    ${d.unplanned_reasons ? `<h4>Why unplanned</h4>${d.unplanned_reasons.map((r) => `<p><span class="code">${esc(r.code)}</span> ${esc(r.message)}</p>`).join("")}` : ""}
    ${rp ? `<h4>Re-planning</h4><p>${rp.moved ? `Moved: ${esc(rp.before_employees.join("+") || "unplanned")} ${hhmm(rp.before_start)} → ${esc(rp.after_employees.join("+") || "unplanned")} ${hhmm(rp.after_start)}` : "Not changed by the last re-plan."}</p>` : ""}
    <h4>Interventions (${d.interventions.length})</h4>
    <table><thead><tr><th>Intervention</th><th class="num">Min</th><th>Timing</th><th>Window</th><th>Req.</th></tr></thead><tbody>
      ${d.interventions.map((i) => `<tr><td>${esc(i.label)}${i.double_staffing ? " 👥" : ""}</td><td class="num">${i.duration}</td><td>${esc(i.timing)}</td><td>${hhmm(i.window[0])}–${hhmm(i.window[1])}</td><td>${[...i.delegations, ...i.skills].map((x) => `<span class="pill">${esc(x)}</span>`).join("")}</td></tr>`).join("")}
    </tbody></table>
    <p style="margin-top:12px">${v.status === "planned" ? `<button id="btn-lock">${v.locked ? "Unlock visit" : "Lock visit (keep employee & time in re-planning)"}</button>` : ""}
    ${v.status === "planned" && d.interventions.some((i) => ["medication", "insulin", "eye_drops"].includes(i.type)) ? ` <button id="btn-medmove">Move medication…</button>` : ""}</p>`;
  el.querySelector("#btn-lock")?.addEventListener("click", () => actions.lock(v.id, !v.locked));
  el.querySelector("#btn-medmove")?.addEventListener("click", () => actions.medMove(v.id));
}

export function renderHistory(el, state, onSelect) {
  const plans = state.scenario?.plans || [];
  if (!plans.length) return (el.innerHTML = "");
  el.innerHTML = "<h4>Plan versions</h4>" + plans.map((p) => `
    <div class="item ${p.id === state.scenario.current_plan_id ? "current" : ""}" data-plan="${p.id}">
      <b>${esc(p.id)}</b> ${p.clock !== null && p.clock !== undefined ? `@${hhmm(p.clock)}` : ""} <span class="badge ${p.valid ? "ok" : "bad"}">${p.valid ? "VALID" : "INVALID"}</span><br>
      <span class="hint">${esc(p.strategy)} · ${p.planned} planned · ${p.unplanned} unplanned</span></div>`).join("");
  el.querySelectorAll("[data-plan]").forEach((d) => d.addEventListener("click", () => onSelect(d.dataset.plan)));
}

const EXP_METRICS = [
  ["unplanned_visits", "Unplanned visits", 0],
  ["lost_vs_base", "Visits lost vs base plan", 0],
  ["hard_violations", "Hard violations", 0],
  ["travel_minutes", "Travel minutes", 0],
  ["preferred_time_deviation_avg", "Time-window deviation avg (min)", 1],
  ["continuity_known_share", "Continuity (known staff)", 3],
  ["changed_assignments_total", "Changed assignments", 0],
  ["changed_start_times_total", "Changed start times", 0],
  ["solver_runtime_s", "Re-planning runtime (s)", 1],
  ["api_cost_usd", "API cost (USD)", 4],
];

export function renderExperiment(el, r) {
  const ran = r.results.filter((x) => !x.skipped);
  const bars = EXP_METRICS.map(([k, label, d]) => {
    const max = Math.max(...ran.map((x) => Math.abs(x.metrics[k] || 0)), 1e-9);
    return `<div class="bar-chart"><h4>${label}</h4>${ran.map((x) => `
      <div class="bar-row"><span>${esc(x.label)}</span><div class="bar-track"><div class="bar-fill s-${x.strategy}" style="width:${(100 * Math.abs(x.metrics[k] || 0)) / max}%"></div></div><span class="num">${fmtNum(x.metrics[k], d)}</span></div>`).join("")}</div>`;
  }).join("");
  el.innerHTML = `
    <h3>Experiment · scenario ${esc(r.scenario.id)} (seed ${r.scenario.seed}, ${r.scenario.visits} visits, ${r.scenario.employees} employees) · ${r.seconds}s</h3>
    <p>Baseline day plan: ${r.baseline_plan.planned} planned, ${r.baseline_plan.unplanned} unplanned, ${r.baseline_plan.valid ? "VALID" : "INVALID"}. Incidents: ${r.incidents.map((i) => `${esc(i.kind)}@${hhmm(i.clock)}`).join(", ")}.</p>
    <div class="pre">${r.conclusion.map(esc).join("\n")}</div>
    ${r.results.filter((x) => x.skipped).map((x) => `<p class="hint">${esc(x.label)}: ${esc(x.note)}</p>`).join("")}
    <div class="bars" style="margin-top:12px">${bars}</div>
    <h4>Per-incident detail</h4>
    <table><thead><tr><th>Strategy</th><th>Incident</th><th>Phase used</th><th class="num">s</th><th class="num">Changed</th><th class="num">Newly unplanned</th><th>Valid</th><th>Advisor</th></tr></thead><tbody>
    ${ran.map((x) => x.steps.map((s) => `<tr><td>${esc(x.label)}</td><td>${esc(s.incident.kind)} @${hhmm(s.incident.clock)}</td><td>${esc(s.phase_used)}</td><td class="num">${s.seconds}</td><td class="num">${s.visits_changed}</td><td class="num">${s.newly_unplanned}</td><td>${s.valid ? "✅" : "❌"}</td><td>${esc(s.advisor?.source || "")}</td></tr>`).join("")).join("")}
    </tbody></table>`;
}


const LANG = { sv: "Swedish", fi: "Finnish", ar: "Arabic", fa: "Persian", so: "Somali", bcs: "Bosnian/Croatian/Serbian", es: "Spanish", pl: "Polish", en: "English" };

function wishText(r) {
  const out = [];
  if (r.gender_preference) {
    out.push(`${r.gender_strict ? "<b>requires</b>" : "wishes"} ${r.gender_preference === "F" ? "female" : "male"} staff ${r.gender_scope === "all" ? "for all visits" : "for intimate care"}`);
  }
  if (r.languages?.length) out.push(`${r.language_required ? "<b>needs</b>" : "prefers"} ${r.languages.map((l) => esc(LANG[l] || l)).join("/")}-speaking staff${r.language_required ? " (lead)" : ""}`);
  if (r.pets?.length) out.push(`${r.pets.map(esc).join(" + ")} in the home (no allergic staff)`);
  if (r.smokes) out.push("smoking home (no staff with smoke-free requirement)");
  return out.join("<br>");
}

export function renderWeek(el, w, onDay) {
  if (!w) {
    el.innerHTML = '<div class="empty">Generate a week scenario (Horizon: a week) to plan Monday to Sunday.</div>';
    return;
  }
  const t = w.totals;
  const v = w.validation;
  const rules = w.rules;
  const byCode = {};
  for (const e of v.errors) (byCode[e.code] ||= []).push(e);
  el.innerHTML = `
    <div class="kpis" style="margin-bottom:12px">
      <div class="kpi"><div class="v">${fmtNum(t.interventions)}</div><div class="l">Interventions this week</div></div>
      <div class="kpi"><div class="v">${fmtNum(t.visits)}</div><div class="l">Visits</div></div>
      <div class="kpi"><div class="v">${t.planned ?? "–"}</div><div class="l">Planned (${t.days_planned}/7 days)</div></div>
      <div class="kpi"><div class="v">${t.unplanned ?? "–"}</div><div class="l">Unplanned</div></div>
      <div class="kpi"><div class="v">${t.headcount}</div><div class="l">Employees (head count)</div></div>
      <div class="kpi ${t.days_planned === 7 ? (v.valid ? "ok" : "bad") : ""}"><div class="v">${t.days_planned === 7 ? (v.valid ? "✓" : v.errors.length) : "–"}</div><div class="l">Week rules (${v.checks} checks)</div></div>
    </div>
    <p class="hint">Rules checked across days from the plans themselves: ${rules.min_daily_rest_h} h dygnsvila between work days (ATL 13 §), ${rules.min_weekly_rest_h} h veckovila per 7 days (ATL 14 §), weekly hours ≤ contract + ${rules.max_weekly_overtime_h} h overtime. Within each day the day validator checks max ${rules.max_continuous_work_h} h work without a rest (ATL 15 §) and max ${rules.max_daily_work_h} h work.</p>
    ${v.errors.length ? `<h4>Week rule violations</h4>${Object.entries(byCode).map(([c, es]) => `<p><span class="code">${esc(c)}</span> ${es.length} × · ${esc(es[0].message)}</p>`).join("")}` : ""}
    <h4>Days</h4>
    <table><thead><tr><th>Day</th><th class="num">Interventions</th><th class="num">Visits</th><th class="num">On duty</th><th class="num">Planned</th><th class="num">Unplanned</th><th class="num">Travel (min)</th><th class="num">Known staff</th><th>Validator</th></tr></thead><tbody>
      ${w.days.map((d) => `<tr class="clickable" data-sid="${esc(d.scenario_id)}"><td><a href="#" data-sid="${esc(d.scenario_id)}">${esc(d.weekday)}</a></td><td class="num">${fmtNum(d.interventions)}</td><td class="num">${d.visits}</td><td class="num">${d.employees_on_duty}</td><td class="num">${d.planned ?? "–"}</td><td class="num">${d.unplanned ?? "–"}</td><td class="num">${d.travel_minutes != null ? fmtNum(d.travel_minutes) : "–"}</td><td class="num">${d.continuity_known_share != null ? pct(d.continuity_known_share) : "–"}</td><td>${d.valid == null ? "not planned" : d.valid ? '<span class="badge ok">VALID</span>' : '<span class="badge bad">INVALID</span>'}</td></tr>`).join("")}
    </tbody></table>
    <h4>Hours per employee</h4>
    <div style="overflow-x:auto"><table class="hours"><thead><tr><th>Employee</th><th>Team</th><th>Shift</th>${w.days.map((d) => `<th class="num">${esc(d.weekday)}</th>`).join("")}<th class="num">Week</th><th class="num">Contract</th></tr></thead><tbody>
      ${w.employees.map((e) => `<tr><td>${esc(e.employee_id)} · ${esc(e.name)}</td><td>${esc(e.team)}</td><td>${esc(e.shift)}</td>${e.hours.map((h, i) => `<td class="num ${h === null && e.days_off.includes(w.days[i].weekday) ? "off" : ""}">${h === null ? (e.days_off.includes(w.days[i].weekday) ? "off" : "–") : h.toFixed(1)}</td>`).join("")}<td class="num ${e.worked_hours > e.contract_hours + rules.max_weekly_overtime_h ? "over" : ""}">${e.worked_hours.toFixed(1)}</td><td class="num">${e.contract_hours.toFixed(1)}</td></tr>`).join("")}
    </tbody></table></div>`;
  el.querySelectorAll("a[data-sid]").forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); onDay(a.dataset.sid); }));
}


const ICON = { alarm: "🚨", child_sick: "🧒", employee_delayed: "⏱", traffic: "🚗", employee_sick: "🤒", visit_cancelled: "✖", medication_moved: "💊" };

export function renderLiveFeed(el, feed, onPlan) {
  if (!feed?.length) {
    el.innerHTML = '<div class="empty">Waiting for the first event…</div>';
    return;
  }
  el.innerHTML = [...feed].reverse().map((e) => e.skipped ? `<div class="ev"><span class="t">${hhmm(e.clock)}</span> ${esc(e.label)}: ${esc(e.title)}</div>` : `
    <div class="ev ${e.kind}">
      <div class="top"><span class="t">${esc(e.clock_text)}</span><span>${ICON[e.kind] || ""} <b>${esc(e.label)}</b></span>
        <span class="badge ${e.valid ? "ok" : "bad"}">${esc(e.status)}</span>
        ${e.kind === "alarm" && e.response_minutes != null ? `<span class="badge ${e.response_minutes <= 15 ? "ok" : "warn"}">${esc(e.dispatched)} on site in ${e.response_minutes} min</span>` : ""}
        <button data-plan="${e.plan_id}">Show this plan</button></div>
      <div>${esc(e.title)}</div>
      <div class="meta">${e.visits_changed} visit(s) changed · ${e.routes_unchanged ?? "–"} routes untouched · ${e.unplanned_total} unplanned in total · ${pct(e.interventions_planned_share)} of interventions planned · re-planned in ${e.seconds} s (${esc(e.strategy_used)})</div>
      ${e.newly_unplanned.length ? `<div class="meta">Newly unplanned: ${e.newly_unplanned.map(esc).join(", ")}</div>` : ""}
      ${e.suggestions.map((h) => `<div class="meta">→ best option for ${esc(h.visit_id)}: <span class="lvl ${h.best.level}">${LEVEL[h.best.level] || h.best.level}</span> ${esc(h.best.title)}${h.best.violations.length ? ` (${h.best.violations.map((x) => esc(x.text)).join("; ")})` : ""}</div>`).join("")}
      <details><summary>Re-planning report</summary><pre>${esc(e.summary)}</pre></details>
    </div>`).join("");
  el.querySelectorAll("button[data-plan]").forEach((b) => b.addEventListener("click", () => onPlan(b.dataset.plan)));
}


const ACTION_TEXT = { local: "local repair, escalate if needed (default)", local_enhanced: "local + ejection-chain repair",
  expanded: "start with the expanded neighbourhood", broad: "start with a broad re-optimisation" };

export function renderML(el, d) {
  if (!d) return (el.innerHTML = '<div class="empty">Loading…</div>');
  const m = d.model || {};
  const ev = m.evaluation;
  const bar = (v, max) => `<span style="display:inline-block;height:8px;width:${Math.round(160 * v / (max || 1))}px;background:var(--brand);border-radius:4px"></span>`;
  const maxImp = Math.max(...(m.feature_importance || [{ importance: 1 }]).map((x) => x.importance));
  el.innerHTML = `
    <h3>Learned re-planning strategy selector</h3>
    <p class="hint">What is learned: which re-planning strategy works best for an incident. Each training case is a simulated incident (alarm, sick child, delay, traffic, sickness, cancellation, moved medication) on a planned day; <b>all four strategies are run on the same case</b> and measured (lost promised visits, extra unplanned, visits changed, runtime, validity). One gradient-boosted model per strategy predicts that score from the incident's features; strategy <b>E</b> uses the prediction. Strategy <b>C</b> (advisor) also receives the most similar cases (case-based advice; Claude gets them in its facts when an API key is set). The model only chooses search parameters: plans are still built by the optimizer and checked by the independent validator.</p>
    <div class="kpis" style="margin:10px 0">
      <div class="kpi ${m.trained ? "ok" : ""}"><div class="v">${m.trained ? "trained" : "not trained"}</div><div class="l">${m.available === false ? "scikit-learn missing" : esc(m.trained_at || m.note || "")}</div></div>
      <div class="kpi"><div class="v">${fmtNum(d.cases_total)}</div><div class="l">training cases (${d.cases_from_app} from this app)</div></div>
      ${ev && ev.mean_regret ? `
      <div class="kpi"><div class="v">${ev.mean_regret.learned}</div><div class="l">mean regret, learned (cross-validated)</div></div>
      <div class="kpi"><div class="v">${ev.mean_regret.default}</div><div class="l">mean regret, always default</div></div>
      <div class="kpi"><div class="v">${pct(ev.picked_best_share.learned)}</div><div class="l">cases where learned pick = best (default ${pct(ev.picked_best_share.default)})</div></div>
      <div class="kpi"><div class="v">${ev.lost_visits_total.learned} / ${ev.lost_visits_total.default}</div><div class="l">promised visits lost: learned / default (oracle ${ev.lost_visits_total.oracle})</div></div>` : ""}
    </div>
    ${ev && ev.mean_score ? `<p>${ev.folds}-fold cross-validation on ${ev.cases} cases (the model never sees the case it is scored on). Mean score (lower = better): learned <b>${ev.mean_score.learned}</b>, always default <b>${ev.mean_score.default}</b>, best possible <b>${ev.mean_score.oracle}</b>. Learned beat the default on ${ev.better_than_default} cases and did worse on ${ev.worse_than_default}. ${ev.mean_regret.learned < ev.mean_regret.default ? "The learned selector is measurably better than the default on these cases." : "The learned selector is <b>not</b> better than the default on these cases; more or more varied cases are needed."}</p>` : `<p class="hint">${esc(ev?.note || "Evaluation runs after training.")}</p>`}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px">
      <div><h4>Strategies</h4><table><thead><tr><th>Strategy</th><th class="num">best in</th><th class="num">chosen (CV)</th></tr></thead><tbody>
        ${(m.actions || Object.keys(ACTION_TEXT)).map((a) => `<tr><td><b>${esc(a)}</b> · ${esc(ACTION_TEXT[a] || "")}</td><td class="num">${ev?.best_action_counts?.[a] ?? "–"}</td><td class="num">${ev?.chosen_action_counts?.[a] ?? "–"}</td></tr>`).join("")}
      </tbody></table></div>
      <div><h4>What the model looks at</h4><table><tbody>
        ${(m.feature_importance || []).map((f) => `<tr><td>${esc(f.feature)}</td><td>${bar(f.importance, maxImp)}</td><td class="num">${f.importance.toFixed(3)}</td></tr>`).join("") || '<tr><td class="hint">train the model first</td></tr>'}
      </tbody></table></div>
    </div>
    <h4>Cases by incident</h4><p>${Object.entries(d.cases_by_kind).map(([k, n]) => `<span class="pill">${esc(k)} ${n}</span>`).join(" ")}</p>
    <h4>Latest cases</h4>
    <div class="scroll"><table><thead><tr><th>Source</th><th>Incident</th><th>Best</th>${Object.keys(ACTION_TEXT).map((a) => `<th class="num">${a}</th>`).join("")}</tr></thead><tbody>
      ${d.recent.map((c) => `<tr><td>${esc(c.source)}</td><td>${esc(c.incident.kind)} ${hhmm(c.incident.clock)}</td><td><b>${esc(c.best)}</b></td>${Object.keys(ACTION_TEXT).map((a) => `<td class="num">${c.scores[a] ?? "–"}</td>`).join("")}</tr>`).join("")}
    </tbody></table></div>`;
}


function commuteText(c) {
  if (!c) return "";
  const src = c.source === "google" ? "Google Routes" : c.source === "estimate" ? "uppskattning" : "Google + uppskattning";
  return ` · Resa hemifrån till kontoret: median ${c.median_minutes} min, 90 % ≤ ${c.p90_minutes} min${c.over_45 ? `, ${c.over_45} över 45 min` : ""} (${src})`;
}

function greeting() {
  const h = new Date().getHours();
  return h < 10 ? "God morgon." : h < 18 ? "God dag." : "God kväll.";
}

export function renderInterventionHead(el, d, hasPlan) {
  const s = d.summary;
  const pctTxt = (a, b) => (b ? `${Math.round(1000 * a / b) / 10} %` : "–");
  if (!hasPlan) {
    el.innerHTML = `<div class="iv-hero"><div class="eyebrow">Simulerad dag · ej optimerad</div><h2>${greeting()}</h2>
      <div class="sub">${fmtNum(s.interventions_total)} insatser i ${esc(s.area)} väntar på att planeras. Optimera dagen för att tilldela dem till personalen.</div></div>
      <div class="iv-cards">
        <div class="iv-card"><div class="k">Insatser</div><div class="n">${fmtNum(s.interventions_total)}</div><div class="s">enskilda insatser i dag</div></div>
        <div class="iv-card"><div class="k">Besök</div><div class="n">${fmtNum(s.visits_total)}</div><div class="s">insatser grupperade per brukare</div></div>
        <div class="iv-card"><div class="k">Brukare</div><div class="n">${fmtNum(s.recipients)}</div><div class="s">med beviljade insatser</div></div>
        <div class="iv-card"><div class="k">Medarbetare</div><div class="n">${fmtNum(s.employees)}</div><div class="s">i tjänst${d.commute ? ` · resa till jobbet ${d.commute.median_minutes} min (median)` : ""}</div></div>
      </div>
      <div class="iv-count"><h3>${fmtNum(s.interventions_total)} enskilda insatser</h3><span class="r">Optimera dagen för att tilldela dem</span></div>`;
    return;
  }
  const cls = s.valid ? (s.approved_exceptions ? "warn" : "") : "bad";
  el.innerHTML = `
    <div class="iv-hero"><div class="eyebrow">Dagens omsorgsplan · ${esc(s.area)} · ${esc(s.weekday)} · seed ${s.seed}
      <span class="iv-status ${cls}" title="Oberoende validator">${s.valid ? (s.approved_exceptions ? "Giltig med godkända avsteg" : "Giltig plan") : "Ogiltig plan"}</span></div>
      <h2>${greeting()}</h2>
      <div class="sub">Samlad bild av dagens insatser och besök. Planen räknas fram av optimeringen och kontrolleras av en oberoende validator${s.clock ? `, klockan är ${esc(s.clock)}` : ""}.</div></div>
    <div class="iv-cards">
      <div class="iv-card"><div class="k">Planerade insatser</div><div class="n">${fmtNum(s.interventions_assigned)}</div>
        <div class="s">av ${fmtNum(s.interventions_total)} (${pctTxt(s.interventions_assigned, s.interventions_total)}) · ${s.visits_planned}/${s.visits_total} besök${s.clock ? `<br><b>${fmtNum(s.interventions_done)} utförda</b>${s.interventions_ongoing ? ` · ${fmtNum(s.interventions_ongoing)} pågår` : ""}` : ""}</div></div>
      <div class="iv-card"><div class="k">Oplanerade besök</div><div class="n ${s.visits_unplanned ? "bad" : ""}">${s.visits_unplanned}</div>
        <div class="s">${s.unplanned_high_priority} med prio 5 · prioritetsvikt ${s.unplanned_priority_weight}</div></div>
      <div class="iv-card"><div class="k">Godkänt schema</div><div class="n">${s.staff_compliant}/${s.staff_working}</div>
        <div class="s">Arbetstidslagen, kompetens och önskemål</div></div>
      <div class="iv-card"><div class="k">Kontinuitet</div><div class="n">${s.continuity != null ? Math.round(100 * s.continuity) + " %" : "–"}</div>
        <div class="s">känd personal · ${s.runtime_s ?? "–"} s · ${fmtNum(s.iterations ?? 0)} iterationer</div></div>
    </div>
    <div id="iv-suggest"></div>
    <div class="iv-note">Kontrollerat: ${s.rules_checked.map(esc).join(" · ")} · Inte verifierat mot kollektivavtal${commuteText(d.commute)}</div>
    <div class="iv-count"><h3>${fmtNum(s.interventions_total)} enskilda insatser</h3>
      <span class="r">${fmtNum(s.interventions_assigned)} tilldelade · ${fmtNum(s.interventions_unassigned)} utan tilldelning · ${s.visits_total} besök · ${s.recipients} brukare · ${s.employees} medarbetare</span></div>`;
}

const PERSON_ICO = '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 1h10v14H3zm5 3a2 2 0 1 0 0 4 2 2 0 0 0 0-4zm-3.2 8h6.4c0-1.8-1.4-3-3.2-3s-3.2 1.2-3.2 3z"/></svg>';

/** "Notana föreslår lösningar": the unplanned visits that need a decision, most important first. */
export function renderSuggestBanner(el, items, total, onVisit, onAll) {
  if (!el) return;
  if (!total) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="suggest">
    <div class="suggest-head"><b>Notana föreslår lösningar</b><span>${total} besök kunde inte planeras. Öppna ett besök för en rekommenderad lösning som du kan acceptera.</span><span class="bubble">${total}</span></div>
    <div class="suggest-grid">${items.map((x) => `<button class="suggest-card" data-visit="${esc(x.visit_id)}">
      <span class="who">${PERSON_ICO}${esc(x.name)}<span class="plus">+</span></span>
      <span class="what">${esc(x.visit_id)} · ${esc(x.window)}${x.priority >= 5 ? " · prio 5" : ""} · ${esc(x.reason)}</span></button>`).join("")}</div>
    ${total > items.length ? `<p class="hint" style="margin-top:8px"><button id="btn-suggest-all">Visa alla ${total} oplanerade</button></p>` : ""}
  </div>`;
  el.querySelectorAll("[data-visit]").forEach((b) => b.addEventListener("click", () => onVisit(b.dataset.visit)));
  el.querySelector("#btn-suggest-all")?.addEventListener("click", onAll);
}

export function renderInterventionRows(el, rows, append, onVisit) {
  const body = rows.map((r) => `<tr class="click" data-visit="${esc(r.visit_id)}">
      <td class="mono">${esc(r.id)}<br><span class="hint">${esc(r.visit_id)}</span></td>
      <td>${esc(r.recipient)}<br><span class="hint">${esc(r.recipient_id)} · ${esc(r.zone)}</span></td>
      <td>${esc(r.need)}${r.double ? ' <span class="pill">2 personal</span>' : ""}</td>
      <td class="mono">${esc(r.window)}<br><span class="hint">${r.duration} min · ${esc(r.timing)}</span></td>
      <td>${r.requirements.length ? r.requirements.map((x) => `<span class="pill">${esc(x)}</span>`).join("") : "Grundkompetens"}</td>
      <td>${r.status === "assigned"
        ? `<b>${r.staff.map((x) => esc(x.name)).join(" + ")}</b><br><span class="hint">${r.staff.map((x) => esc(x.id)).join(" + ")} · start ${esc(r.start)}${r.progress === "done" ? " · utförd" : r.progress === "ongoing" ? " · pågår" : ""}</span>`
        : r.status === "cancelled" ? '<span class="hint">Inställt</span>'
        : `<span class="unassigned">Utan tilldelning</span>${r.reason ? `<div class="why"><span class="code">${esc(r.reason.code)}</span> ${esc(r.reason.message)}</div>` : ""}`}</td>
    </tr>`).join("");
  if (append) {
    el.querySelector("tbody").insertAdjacentHTML("beforeend", body);
  } else {
    el.innerHTML = `<table><thead><tr><th>Insats / besök</th><th>Brukare</th><th>Omsorgsbehov</th><th>Tid / längd</th><th>Krav</th><th>Tilldelning</th></tr></thead><tbody>${body}</tbody></table>`;
  }
  el.querySelectorAll("tr[data-visit]").forEach((tr) => { tr.onclick = () => onVisit(tr.dataset.visit); });
}
