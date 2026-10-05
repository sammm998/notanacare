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
2. **Optimise** (≈25 s). The header shows **VALID ✓ (independent validator)** or **INVALID** with the violations.
3. Explore the **Map**, **Timeline** (Gantt), **Unplanned / conflicts** (each with a reason) and **Score & validation**.
4. Set the **clock** (e.g. 10:14), apply an **incident** and read the **Re-planning report** (before/after, phases used, every changed visit).
5. **Experiments** tab: run strategies A–D on the same scenario and incidents.

Headless: `python demo.py --sick 2 --clock 10:14` · Tests: `python -m pytest` (≈75 s).

Optional environment (see `.env.example`, never commit keys):

| Variable | Effect |
|---|---|
| `GOOGLE_MAPS_API_KEY` | Google **Routes API** `computeRouteMatrix` travel times (traffic-aware, cached to disk). Without it: synthetic travel. |
| `ANTHROPIC_API_KEY` | Claude planning advisor for strategy C (`claude-opus-5-5`, override with `NOTANA_CLAUDE_MODEL`). Without it: deterministic advisor. |

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
  validator.py       INDEPENDENT validator (imports nothing from solver/)
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
2. **OR-Tools routing** (guided local search, warm-started from 1). Employees are vehicles with
   their own start/end, every role of every visit is a node, and pairs are pinned.
3. **Repair insertion** of leftovers with exact feasibility checks.
4. **Enhanced repair** (strategy B+): ejection chains. Remove a blocking visit, insert the
   unplanned one and re-insert the blocker elsewhere. A lower-priority visit is displaced only for
   a strictly higher-priority one, and that is recorded.
5. **CP-SAT timetabling**: given the sequences, exact start times for all visits and breaks with
   global double-staffing synchronisation, recipient non-overlap and soft time preferences
   (solved to optimality, typically 0.1 s).
6. **Gap fill** at fixed times, then **independent validation**, **diagnostics** and **scoring**.

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
| Locked, started and completed visits never move | frozen prefix + pinned nodes | `LOCKED_VISIT_MOVED`, `LOCKED_VISIT_REMOVED` |
| Recipient never double-booked | non-overlap constraints | `RECIPIENT_DOUBLE_BOOKED` |
| Completeness: each visit is planned **or** reported unplanned | — | `VISIT_MISSING`, `PLANNED_AND_UNPLANNED` |

Hard constraints are model constraints, never large penalties. The only large penalties are on
*dropping* a visit, which yields an explicit unplanned visit with a reason, never an invalid assignment.

### Soft objectives (configurable in the UI, `config.ObjectiveWeights`)

Priority-weighted unplanned visits (dominant) · travel time · travel distance · deviation from
preferred time · continuity (known / preferred staff) · workload balance (care minutes above fair
share) · overtime · idle span · **re-planning stability**: employee change, change of communicated
start time, touching an unaffected route, dropping an already-communicated visit. The **Score**
tab recomputes every term from the plan, so you can see *why* one plan beats another.

### Real-time re-planning (`replanning.py`)

* Everything started by the clock (visits, breaks, gaps) is frozen and copied verbatim.
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
fallback). The solver never calls an API: it reads a precomputed location matrix, and `TrafficConditions`
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

---

## Calibration notes (honest)

* The requested shape (≈5,000 interventions of 4–10 min, 6–12 per visit, ≤4 visits/day, ~100
  employees) implies ≈ 5 h of care per employee per day, so it is **inherently tight**. Defaults use
  realistic Swedish home-care shift templates (early, day, late, *delad tur* split shifts,
  part-time) with meal breaks between demand peaks.
* With the defaults (seed 42, pressure 0.3), the day plan leaves about **3 % of visits unplanned**
  (≈ 18–21 of 587), each with a structured reason. Raise *staffing pressure* or remove employees to
  see real capacity problems, or add *extra pool staff* to see them resolved. The engine never shortens
  care and never hides a shortage.
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
`GET /api/scenarios/{id}/export` · OpenAPI docs at `/docs`.
