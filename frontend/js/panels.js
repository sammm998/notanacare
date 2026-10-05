import { esc, fmtNum, hhmm, pct } from "./util.js";

const KPIS = [
  ["planned_visits", "Planned visits", 0, -1],
  ["unplanned_visits", "Unplanned visits", 0, 1],
  ["unplanned_high_priority", "Unplanned prio 5", 0, 1],
  ["hard_violations", "Hard violations", 0, 1],
  ["travel_minutes", "Travel (min)", 0, 1],
  ["avg_travel_per_visit", "Travel / visit", 1, 1],
  ["preferred_time_deviation_avg", "Pref. deviation avg (min)", 1, 1],
  ["continuity_known_share", "Continuity (known staff)", "pct", -1],
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
    return `<div class="kpi ${cls}"><div class="v">${kv(v, d)}</div><div class="l">${label}</div>${delta}</div>`;
  }).join("");
  const stab = s.stability || {};
  if (stab.compared_to) {
    el.innerHTML += `<div class="kpi"><div class="v">${fmtNum(stab.visits_changed)}</div><div class="l">Visits changed vs previous</div><div class="d">${stab.employee_changes} reassigned · ${stab.start_time_changes} retimed · ${stab.routes_changed} routes</div></div>`;
  }
}

export function renderConflicts(el, state, onVisit) {
  const plan = state.plan;
  if (!plan) return (el.innerHTML = '<div class="empty">No plan yet.</div>');
  const rows = Object.values(plan.unplanned).map((u) => ({ u, v: state.visitsById[u.visit_id] })).filter((x) => x.v);
  rows.sort((a, b) => b.v.priority - a.v.priority || a.v.earliest - b.v.earliest);
  if (!rows.length) return (el.innerHTML = '<div class="empty">Every visit is planned. 🎉</div>');
  const byCode = {};
  for (const { u } of rows) {
    const c = u.reasons[0]?.code || "UNKNOWN";
    byCode[c] = (byCode[c] || 0) + 1;
  }
  el.innerHTML = `
    <h3>${rows.length} visits cannot currently be planned</h3>
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
          <td>${u.reasons.map((r) => `<div><span class="code ${r.code.startsWith("NO_") ? "bad" : ""}">${esc(r.code)}</span> ${esc(r.message)}</div>`).join("")}</td>
        </tr>`).join("")}</tbody></table></div>`;
  el.querySelectorAll("tr[data-visit]").forEach((tr) => tr.addEventListener("click", () => onVisit(tr.dataset.visit)));
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
