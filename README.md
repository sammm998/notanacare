# Notana Care · Care-planning & re-planning simulator

A working prototype that takes thousands of **already-structured care interventions**, turns them
into **visits**, assigns them to **employees**, sequences them **geographically**, respects **time
windows and care constraints** (skills, delegations, double staffing, breaks, shifts) and
**re-plans in real time** when reality changes (sickness, delays, traffic, moved medication).

```
STRUCTURED INTERVENTIONS → VISITS → PLANNING ENGINE → EMPLOYEE ROUTES → REAL-TIME RE-PLANNING
```

Nothing is faked: the optimizer (Google OR-Tools routing + CP-SAT) actually solves the generated
problem, and **every plan is re-checked by an independent validator** before it is shown as VALID.
All people and addresses are synthetic.

> Staff scheduling (who works which shift) is an **input**. This app solves **care planning**:
> which employee performs which visit, in which order, at what time.

---

## Quick start

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn notana_planner.api:app --port 8000    # open http://localhost:8000
```

1. **Generate scenario** (default: Stockholm, seed 42, ~5,000 interventions → ~590 visits, ~158 recipients, 100 employees).
   Choose *Horizon: a week* to get Monday–Sunday (≈35,000 interventions, 140 employees with rosters).
2. **Optimise** (≈25 s). The header shows **VALID ✓ (independent validator)** or **INVALID** with the violations.
3. The **Insatser** tab (first) shows the day in interventions: planned of ~5,000, unplanned visits,
   staff with a compliant schedule, continuity, and a searchable table of every single intervention
   with recipient, need, time, requirements and assignment (or why not).
   The header badge reads e.g. *Insatser: 4 812 av 5 004 planerade · 31 oplanerade · 1 903 utförda kl. 12:10*.
   Explore the **Map**, **Timeline** (Gantt), **Unplanned / conflicts** (each with a reason) and **Score & validation**.
   Opening an unplanned visit shows a **recommended solution** with **Acceptera** (plus the other options).
   Live incidents run only when you start them (*Start live day* in the Live tab); tick *Run 3 real-time
   incidents automatically* (off by default) to start three events after the first optimisation.
4. Set the **clock** (e.g. 10:14), apply an **incident** and read the **Re-planning report** (before/after, phases used, every changed visit).
5. **Experiments** tab: run strategies A–D on the same scenario and incidents.
6. Week scenarios: pick a day in the **Week** bar, or **Optimise whole week**; the **Week** tab shows
   every day, hours per employee and the cross-day rules (dygnsvila, veckovila, weekly hours).

Headless: `python demo.py --sick 2 --clock 10:14` · Tests: `python -m pytest` (64 test functions, ≈20 min).

Optional environment (see `.env.example`, never commit keys):

| Variable | Effect |
|---|---|
| `GOOGLE_MAPS_API_KEY` | Google **Routes API** `computeRouteMatrix` travel times (traffic-aware, cached to disk). Without it: synthetic travel. |
| `GOOGLE_MAPS_BROWSER_KEY` | Google Maps as the map background. A **separate** key, restricted to *Maps JavaScript API* and to your site URL (HTTP referrer), because it is visible in the browser. Without it: OpenStreetMap. |
| `ANTHROPIC_API_KEY` | Claude planning advisor for strategy C (`claude-opus-5-5`, override with `NOTANA_CLAUDE_MODEL`). Without it: deterministic advisor. |

### Deploy

The repo ships a `Dockerfile` (single process on purpose: live sessions live in memory), plus
`render.yaml` and `fly.toml`.

* **Render**: Dashboard → *New* → *Blueprint* → select this repo. Optionally set `GOOGLE_MAPS_API_KEY` / `ANTHROPIC_API_KEY`.
* **Fly.io** (Stockholm region): `fly launch --copy-config --no-deploy && fly deploy`.
* **Anywhere with Docker**: `docker build -t notana-planner . && docker run -p 8000:8000 notana-planner`.

Give it real CPU (≥ 2 vCPU, 2–4 GB RAM). A full day takes about 25 s to optimise; on free tiers it works but is slow.
There is no login, so put it behind your own access control before sharing beyond a demo audience.

---

## Architecture

```
backend/notana_planner/
  domain.py          typed model: CareRecipient, Intervention, Visit, Employee, Plan, Route, ...
  catalog.py         intervention types, skills, delegations
  generator.py       deterministic seeded scenario generator (interventions, recipients, staff, shifts)
  visit_builder.py   interventions → visits (sum of durations, window intersection, double staffing)
  travel/            TravelTimeProvider: synthetic (Haversine) + Google Routes; TravelMatrix + traffic
  solver/
    problem.py       PlanningProblem / Task / VehicleSpec (global plan and every repair neighbourhood)
    construction.py  exact insertion heuristic, synchronised *pair* insertion for double staffing
    routing_solver.py OR-Tools routing model (all hard constraints as model constraints)
    timetabling.py   CP-SAT: exact start times for the chosen sequences (cross-route sync)
    lns.py           enhanced repair (ejection chains) — strategy B
    engine.py        pipeline orchestration
  working_time.py    Arbetstidslagen -> per-employee planning bounds (break windows, latest end)
  week.py            week planning (Mon..Sun), continuity carry-over, cross-day validator
  validator.py       INDEPENDENT validator (imports nothing from solver/ or working_time.py)
  scoring.py         objective score breakdown recomputed from the plan
  diagnostics.py     why is a visit unplanned? (structured reasons from gap analysis)
  incidents.py       sick / delay / traffic / medication moved / extra staff / cancellation
  replanning.py      phased local repair with stability penalties
  explain.py         plan diff, fact-based re-planning summary, per-visit explanation
  advisor/           PlanningAdvisor: deterministic + optional Claude adapter
  classifier/        DecisionClassifier interface + mock (no JEV SDK available)
  experiments.py     same scenario, several strategies, measured comparison
  store.py, api.py   SQLite audit trail, FastAPI + background jobs
frontend/            no-build ES modules: map (vendored Leaflet), Gantt, panels
```

### Solve pipeline (`solver/engine.py`)

1. **Construction** – exact single-route time-window insertion (forward/backward passes).
   Double-staffed visits are inserted as **pairs**: two different qualified employees whose
   feasible start intervals intersect, fixed at the same start in both routes.
2. **Ruin-and-recreate LNS** (`solver/ruin_recreate.py`, 75 % of the time budget): remove a
   cluster of visits close in space *and time*, a string of one route, the visits reached by the
   longest trips, or an out-and-back excursion (A → far B → back near A, B removed together with the
   visits around it at the same time so it can move to someone working there), re-insert
   them (plus unplanned visits nearby) with the same exact checks, keep the result if the cost is
   lower (threshold acceptance). Cost = unplanned penalties + travel + long-trip penalty
   (150/min beyond 15 min) + continuity, wishes and area costs. The insertion check is linear
   (earliest/latest starts of a route computed once and cached, every position checked in O(1);
   identical to full recomputation, checked on 1.8 M calls), which doubles the LNS iterations.
   *Why:* measured on the full day, OR-Tools' local search evaluated ~15 moves/s and found **no**
   improving solution in 25 s (≈10,000 cross-route constraints). With the LNS the default day goes
   from 22 to 5–8 unplanned visits and 90 % of trips are ≤ 15–17 min.
3. **OR-Tools routing** (guided local search, warm-started from 2, rest of the budget). Employees are
   vehicles with their own start/end and travel matrix (car / bike), every role of every visit is a node,
   and pairs are pinned.
4. **Repair insertion** of leftovers with exact feasibility checks.
5. **Enhanced repair** (strategy B+): ejection chains. Remove a blocking visit, insert the
   unplanned one and re-insert the blocker elsewhere. A lower-priority visit is displaced only for
   a strictly higher-priority one, and that is recorded.
6. **CP-SAT timetabling**: given the sequences, exact start times for all visits and breaks with
   global double-staffing synchronisation, recipient non-overlap and soft time preferences
   (solved to optimality, typically 0.1 s).
7. **Gap fill** at fixed times, then **independent validation**, **diagnostics** and **scoring**.

### Hard constraints and where they are enforced

| Constraint | Optimizer | Validator |
|---|---|---|
| Visit inside the assigned employee's shift (+ approved overtime) | vehicle start/end cumul ranges | `OUTSIDE_SHIFT`, `BEFORE_SHIFT` |
| No overlap per employee | routing path + timetabling chain | `OVERLAP`, `ROUTE_NOT_CHRONOLOGICAL` |
| `end(A) + travel(A,B) ≤ start(B)` | transit = duration + travel | `TRAVEL_INFEASIBLE`, `RETURN_INFEASIBLE` |
| Full visit duration reserved | node service time = Σ intervention durations | `DURATION_NOT_RESERVED` |
| Skills (all staff) / delegations (lead) / avoided employees | allowed vehicles per role | `MISSING_SKILL`, `MISSING_DELEGATION`, `AVOIDED_EMPLOYEE` |
| Hard time windows | cumul range of each visit node | `TIME_WINDOW_VIOLATED` |
| Double staffing: exactly 2 distinct, synchronised (± tolerance), both full duration | pair construction + CP-SAT `|s_a − s_b| ≤ tol` | `WRONG_STAFF_COUNT`, `SAME_EMPLOYEE_TWICE`, `DOUBLE_STAFFING_NOT_SYNCHRONISED` |
| Mandatory meal breaks / unavailable gaps (split shifts, sickness) | break nodes at the team office | `BREAK_MISSING`, `DURING_UNAVAILABILITY` |
| Strict gender requirement (intimate care or all visits), pet allergy, smoke-free workplace (all staff); required language (lead) | allowed vehicles per role | `GENDER_REQUIREMENT_VIOLATED`, `PET_ALLERGY`, `SMOKING_EXPOSURE`, `LANGUAGE_REQUIRED` |
| Arbetstidslagen: max 5 h work without a rest (15 §), max 10 h work per day, 11 h dygnsvila (13 §) | break windows clamped, latest route end capped (`working_time.py`) | `ATL_CONTINUOUS_WORK`, `MAX_DAILY_WORK`, `DAILY_REST_VIOLATED` |
| Week: 11 h rest between work days, 36 h veckovila per 7 days (14 §), weekly hours ≤ contract + overtime | roster (2 consecutive days off) + tonight's end capped by tomorrow's start | `week.validate_week`: `DAILY_REST_VIOLATED`, `WEEKLY_REST_VIOLATED`, `WEEKLY_HOURS_EXCEEDED` |
| Locked, started and completed visits never move | frozen prefix + pinned nodes | `LOCKED_VISIT_MOVED`, `LOCKED_VISIT_REMOVED` |
| Recipient never double-booked | non-overlap constraints | `RECIPIENT_DOUBLE_BOOKED` |
| Completeness: each visit is planned **or** reported unplanned | — | `VISIT_MISSING`, `PLANNED_AND_UNPLANNED` |

Hard constraints are model constraints, never large penalties. The only large penalties are on
*dropping* a visit, which yields an explicit unplanned visit with a reason, never an invalid assignment.

### Soft objectives (configurable in the UI, `config.ObjectiveWeights`)

Priority-weighted unplanned visits (dominant) · travel time · travel distance · deviation from
preferred time · continuity (known / preferred staff) · workload balance (care minutes above fair
share) · overtime · idle span · **wishes**: non-strict gender wish, preferred language, employee's
preferred area · **re-planning stability**: employee change, change of communicated
start time, touching an unaffected route, dropping an already-communicated visit. The **Score**
tab recomputes every term from the plan, so you can see *why* one plan beats another.

### Unplanned visits: solutions, ranked (`suggestions.py`)

The Unplanned tab shows interventions planned / unplanned (count and share of the ~5,000) and, on
request, options for **every** unplanned visit, sorted by severity (least first):

* **Within rules** (0 points): a real re-optimisation of the 5–6 nearest qualified routes together;
  kept only if the visit is placed, no other planned visit is lost and the validator says VALID.
* **Deviations**: the visit inserted into one route (two for double staffing) at every position, later
  stops pushed; every consequence is priced: start outside the window (1/min, 8/min if time-critical),
  pushed visits, overtime (2/min), meal break moved (2/min), more than 5 h without a rest (4/min),
  work in an unpaid gap, desynchronised double staffing (300), wishes (10–400), pet allergy (800),
  missing skill (600), missing delegation (2000, *not permitted*), single staffing of a double visit (1500).
* **Extra resource**: pool staff with the visit's qualifications for the zone and window.

Applying a deviation creates a plan with an **approved exception**: the validator still reports every
violation, listed as approved (`VALID_WITH_APPROVED_EXCEPTIONS`); without the approval the same plan is
INVALID. **Auto-fix within rules** first re-optimises all open visits (warm start, 45 s) and then
re-plans the nearest routes per remaining visit; it reports the net number of visits gained.

### Live day (`live.py`) and new incidents

*Live* runs the clock through the day; seeded random events arrive and are re-planned at once
(local repair, 3–5 s): **safety alarm** (trygghetslarm: a new urgent visit; the employee who can be
on site first is dispatched, accounting for where they are and the visit they are finishing; their
other visits are re-planned; if their day cannot absorb it the next fastest goes), **child sick**
(VAB: the employee finishes the visit in progress and leaves, for the rest of the day or 2–3 h),
delays, traffic, sickness, cancellations and moved medication. The feed shows each event, who was
dispatched and how fast, what changed, what became unplanned and its best option. Every plan is validated.

### Machine learning: a strategy selector trained on simulated cases (`ml/`)

What is learned: **which re-planning strategy works best for an incident**. A training case is a
simulated incident (alarm, VAB, delay, traffic, sickness, cancellation, moved medication) at a random
time on a planned day of a random scenario. **All four strategies run on the same case** (counterfactual,
world cloned per action): `local` (default escalation), `local_enhanced`, `expanded`, `broad`. Each
outcome is scored: `1000·lost high-priority + 100·lost + 20·extra unplanned + visits changed + 0.5·s`
(invalid plan = 1e6). Features are known before re-planning (incident kind, clock, released visits and
their priority / double staffing / hard windows / care minutes, affected employees, remaining care vs
remaining staff time, unplanned before, traffic). One gradient-boosted regressor (scikit-learn) per
strategy predicts the score; **strategy E** takes the lowest prediction. The model is retrained from the
case data at start-up and on request (no pickled model to drift out of sync with the library).

Evaluation is 5-fold cross-validation (scored only on unseen cases) against "always default" and the
oracle; the *Machine learning* tab shows it, including when the model is not better. New cases can be
generated in the app on the current scenario (session untouched) and the model retrained. Strategy C's
advisor gets the most similar cases with their measured outcomes (Claude in its facts; the
deterministic fallback votes by mean score, case-based reasoning). Simulated cases are bundled in
`ml/data/replan_cases.jsonl` (generated with `python train_ml.py --seeds 1-28 --per-seed 6`).

### Documentation page

`/documentation` (link in the header) explains architecture, every module, the engine, rules, wishes,
Arbetstidslagen, week planning, suggestions, the live day, AI and ML, travel, API, operations and the
measured numbers.

### Wishes and person attributes

Generated with their own random stream (switch off with *Wishes & person attributes*):

| Who | Attribute | Share (default) | Rule |
|---|---|---|---|
| Recipient | wishes female / male staff, for intimate care (hygiene, shower, toileting, dressing …) or all visits | 22 %, of which 40 % strict | strict: hard (all staff); else weighted wish |
| Recipient | mother tongue other than Swedish (Finnish, Arabic, Persian, Somali, BCS, Spanish, Polish) | ≈ 10 % | required (often with dementia): lead must speak it; else wish |
| Recipient | dog / cat in the home · smokes | 25 % · 8 % | hard for allergic / smoke-free staff |
| Employee | gender (80 % women) · languages · pet allergy · smoke-free requirement · bike instead of car · preferred area | 8 % allergic, 12 % smoke-free, 12 % bike | bike: own travel matrix (×1.35) in optimizer and validator |

The generator keeps hard wishes coherent with the roster, as a unit manager would: if a language is
required, at least two employees who are on shift and hold the visit's delegations speak it; a strict
gender wish that the roster cannot honour for some visit becomes a weighted wish. Diagnostics name
wishes when they are the reason a visit is unplanned (`WISHES_EXCLUDE_ALL_STAFF`).

### Week planning (`week.py`)

`generate_week` builds seven day scenarios for the same recipients and staff. `employee_count` is the
staff on duty per day; the head count is ×7/5. Everyone keeps one shift type and has two consecutive
days off, spread evenly per shift type and team. Daily needs repeat with day-to-day variation;
shower (2/week), cleaning (1), laundry (1), shopping (2) and walks (3) follow weekly frequencies.
Days are optimised Monday→Sunday; an employee who served a recipient becomes "known" for the following
days (continuity grows through the week). Tonight's latest end is capped by tomorrow's shift start
minus 11 h. `validate_week` re-checks the cross-day rules from the plans themselves.

### Real-time re-planning (`replanning.py`)

* Everything started by the clock (visits, breaks, gaps) is frozen and copied verbatim.
* Previously unplanned visits are part of every phase: an incident can free capacity too (the partner
  of a released double-staffed visit), and the report says how many of them could now be planned.
  They are never counted as "lost" and only land on employees inside the neighbourhood.
* **Phase 1, local repair**: affected employees plus the 6 helpers with the most real openings for
  the released visits (exact gap check), over a 3-hour horizon. The helpers' later visits are pinned
  and all other routes are untouched.
* **Phase 2, expanded**: 24 helpers, rest of the day.
* **Phase 3, broad**: all employees, all remaining visits, warm-started from the current plan.
* A phase is accepted when no communicated or released visit is lost and the plan validates.
  Otherwise the next phase runs, and the best phase wins (fewest lost high-priority visits →
  fewest lost visits → fewest unplanned → lowest cost). The report states which phase was used.

The summary is generated from structured facts, for example:

```
Employee E-014 reported sick at 10:14.
8 future visits were directly affected.
8 were reassigned without violating hard constraints.
Double-staffed visit V-0071 moved by 14 minutes (10:52 -> 10:38), both employees synchronised.
V-0228 could not be scheduled: 2 qualified employee(s) have enough free time in 14:40-16:36, but not enough to also travel there and on to their next stop.
Strategy: expanded neighbourhood over 24 employees (77 routes untouched); tried phase 1 (3 visit(s) left unplanned), phase 2 (...)
Independent validation: VALID (0 hard violations, 0 warnings).
```

### Travel time

`TravelTimeProvider` → `SyntheticTravelTimeProvider` (Haversine × 1.35 routing factor at 24 km/h +
3 min parking/door overhead) or `GoogleRoutesTravelTimeProvider` (Routes API `computeRouteMatrix`,
25×25 blocks, `TRAFFIC_AWARE`, one matrix per departure hour, disk cache, per-element synthetic
fallback). The Routes API allows ~3,000 elements/minute and a 165-address day is ~27,000, so a matrix
has a **time budget** (`NOTANA_GOOGLE_BUDGET_S`, default 60 s): blocks are grouped by zone and requested
closest pairs first (3 in parallel), the far pairs keep the synthetic estimate, and the travel badge says
how many trips came from Google. Scenario generation runs as a background job with progress. The solver never calls an API: it reads a precomputed location matrix, and `TrafficConditions`
(global / zone / corridor multipliers) are layered on top for incidents. The header shows the active source.

### Optional AI layers, and what they must not do

* `PlanningAdvisor` (`advisor/`): `suggest_repair_strategy`, `analyze_conflict`, `explain_plan`,
  `explain_replan`. The Claude adapter receives structured facts only and returns schema-validated
  parameters, which are **clamped to safe bounds** and executed by the deterministic engine. Errors
  and refusals fall back to the deterministic advisor, and token cost is tracked per call.
  **It never decides validity.**
* `DecisionClassifier` (`classifier/`): ranks *already validated* alternatives and classifies incidents.
  No verified JEV SDK exists in this environment, so only the interface and a transparent mock are provided.

### Experiment mode

Strategies **A** baseline · **B** + enhanced repair · **C** + planning advisor · **D** + classifier
ranking, run on the **same** seed, visits, employees, travel matrix, baseline plan and incident
sequence. The report gives measured metrics only: unplanned, lost, hard violations, travel,
time-window deviation, continuity, changed assignments and start times, runtime and API cost.
It says *"no measurable difference"* when that is the case, labels C as the deterministic fallback when no
API key is set, and notes that a single seed is indicative rather than statistically established.

### Measured result (default scenario, seed 42, no API key)

Baseline day plan: **584 / 587 visits planned, VALID** (wishes, Arbetstidslagen and LNS on). Incident
chain: 2 employees sick at 10:14 → Södermalm traffic ×1.6 at 10:34 → a 25-min delay at 10:44 → a
medication moved at 10:54.

| Strategy | Unplanned at end | Lost vs base plan | Hard violations | Travel (min) | Changed assignments | Re-plan runtime |
|---|---|---|---|---|---|---|
| A. Baseline optimizer | 13 | 11 | 0 | 6,581 | 64 | 87 s |
| B. + enhanced repair | 13 | 10 | 0 | 6,549 | 63 | 83 s |
| C. + advisor (deterministic, case-based) | 13 | 10 | 0 | 6,479 | 61 | 112 s |
| D. + mock classifier ranking | 12 | 9 | 0 | 6,703 | 81 | 107 s |
| E. + learned selector (ML) | 13 | 10 | 0 | 6,503 | 65 | 107 s |

Reading it honestly: on this one chain D ends with one unplanned visit fewer, at the cost of the most
changed assignments and the most travel; the others are within a few percent of each other. One seed shows nothing statistically; the 96-case nested cross-validation
(see Machine learning) is the measure that counts, and there E does not beat the default yet. No LLM was
involved (no API key).

---

## Calibration notes (honest)

* The requested shape (≈5,000 interventions of 4–10 min, 6–12 per visit, ≤4 visits/day, ~100
  employees) implies ≈ 5 h of care per employee per day, so it is **inherently tight**. Defaults use
  Swedish home-care shift templates that comply with Arbetstidslagen (early, late, *delad tur*
  07:30–12:00 + 16:30–20:30, part-time) with meal breaks between demand peaks.
* Staffing as a unit manager would lay it out: team size follows each zone's care minutes, every team
  gets the same shift mix, and delegations/skills are spread evenly over each team's shifts and
  weighted by the zone's need. Compared with random allocation this halves visits outside the own
  zone, cuts out-and-back detours by ~40 % and travel per visit by ~15 % (four seeds).
* With the defaults (pressure 0.15, wishes and Arbetstidslagen on) one day plans **97.8–99.2 % of
  the ~5,000 interventions** (seed 42: 4,960 of 5,002, 5 unplanned visits; seed 43: 97.8 %, 12;
  seed 44: 98.0 %, 11), every plan VALID; median trip 6–7 min, 90 % of trips ≤ 12–13 min, travel per
  visit 10.2–11.7 min (was 11.8–13.0 before demand-based staffing). Before the LNS the same day left 22
  visits unplanned (seed 42). A live day (8 events) keeps every plan VALID; alarms have staff on site in
  5–11 min. A full default week (35,253 interventions, 4,167 visits, 140 employees) plans 34,024
  interventions (96.5 %) in about 3 minutes, all seven days VALID and the week rules met. An
  understaffed day (1,516 interventions, 24 employees) plans 80.6 %; every unplanned visit gets ranked
  options (best: minor deviation 10, pool staff 19, major 6) and auto-fix within the rules plans one
  more visit (80.7 %).
* Machine learning, measured honestly: on 96 simulated cases (nested 5-fold CV) the learned selector
  scores 375 vs 365 for the default and 354 for the oracle, i.e. it does **not** beat the default yet
  (the default is within 3 % of the oracle). More and more varied cases are needed; the app can
  generate them. Raise *staffing pressure* or remove employees to see real
  capacity problems, or add *extra pool staff* to see them resolved. The engine never shortens care
  and never hides a shortage.
* Unplanned diagnostics are evaluated **without moving other visits**. *"No opening"* means none
  exists in the current plan, not a proof that no plan could include the visit.

## Known limitations / engineering notes

* Breaks are taken at the team office (common practice). OR-Tools' native break intervals were
  dropped because they rejected valid warm starts on large models.
* In this OR-Tools build, `SetAllowedVehiclesForIndex` has a broken Python binding (vehicle-var domains
  are used instead), and unary-vector vehicle-dependent transits made valid re-planning models
  infeasible (binary callbacks are used). Both are covered by tests.
* Travel times are static per incident (no time-dependent traffic inside a solve). An employee who
  is mid-travel at the clock is re-planned from their last stop.
* Live sessions are in memory. SQLite stores the audit trail (configs, plans, incidents), and a session
  can be reconstructed from its seed plus its incident sequence.

## API (selection)

`POST /api/scenarios` · `POST /api/scenarios/{id}/plan` → job · `GET /api/jobs/{id}` ·
`GET /api/plans/{id}` · `GET /api/plans/{id}/visits/{vid}` (explanation) ·
`POST /api/scenarios/{id}/incidents` → job · `POST /api/plans/{id}/validate` ·
`POST /api/scenarios/{id}/visits/{vid}/lock` · `POST /api/experiments` → job ·
`GET /api/scenarios/{id}/export` · `GET /api/weeks/{id}` (week summary + week validation) ·
`POST /api/weeks/{id}/plan` → job · `POST /api/plans/{id}/suggestions` → job ·
`POST /api/plans/{id}/visits/{visit}/suggestions` · `POST /api/plans/{id}/suggestions/{option}/apply` · `POST /api/plans/{id}/autofix` → job ·
`GET /api/ml` · `POST /api/ml/train` → job · `POST /api/scenarios/{id}/ml/cases?n=4` → job ·
`POST /api/scenarios/{id}/live` → job (feed in the job) · `POST /api/jobs/{id}/cancel` · OpenAPI docs at `/docs`.
