"""FastAPI service for the planning simulator (+ static frontend)."""

from __future__ import annotations

import logging
import os
import random
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog as C
from .advisor import make_advisor
from .config import ObjectiveWeights, SolverSettings
from .diagnostics import diagnose_unplanned
from .domain import Plan, ScenarioConfig, fmt_time, to_jsonable
from .experiments import ExperimentSpec, run_experiment
from .explain import explain_plan_text, explain_visit, plan_diff
from .generator import generate_scenario, generate_week
from .geo import AREAS
from .incidents import INCIDENT_KINDS, Incident, apply_incident
from .planner import STRATEGIES, World, plan_day
from .replanning import clone_world, replan_with_strategy
from .store import Database, Session, SessionStore
from .travel import make_provider
from .validator import validate_plan
from . import intervention_view as IV
from .live import run_live
from .ml.cases import generate_cases
from .ml.model import selector
from .suggestions import apply_option, autofix, public, suggest_for_plan
from .week import plan_week, week_summary

log = logging.getLogger("notana")
ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
DB = Database(os.environ.get("NOTANA_DB", str(ROOT / "data" / "notana.sqlite3")))
STORE = SessionStore(DB)
POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("NOTANA_WORKERS", "2")))
JOBS: dict[str, dict[str, Any]] = {}
WORLD_SNAPSHOTS: dict[str, World] = {}  # plan id -> world state the plan belongs to
WEEKS: dict[str, list[str]] = {}  # week id -> day scenario ids (Mon..Sun)
ML_STORE = Path(os.environ.get("NOTANA_ML_CASES", str(ROOT / "data" / "ml_cases.jsonl")))
SUGGESTIONS: dict[str, dict[str, list[dict]]] = {}  # plan id -> visit id -> options (with private payload)

app = FastAPI(title="Notana Care Planning Simulator", version="1.0.0")
# Train the strategy selector from the case data (bundled + generated in the app)
# in the background, so start-up is not delayed.
import threading as _threading  # noqa: E402

_threading.Thread(target=lambda: selector(ML_STORE).train(evaluate_cv=True), daemon=True).start()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# Which code is running: Railway sets RAILWAY_GIT_COMMIT_SHA for every deployment.
STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
VERSION = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or "dev")[:7]
FEATURES = ["week", "wishes", "atl", "lns", "suggestions", "live", "alarm", "vab", "ml", "documentation"]


@app.middleware("http")
async def no_stale_frontend(request, call_next):  # noqa: ANN001, ANN201
    """The page, scripts and styles are always revalidated, so a new deployment is
    visible on the next load instead of after the browser's heuristic cache expires."""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/documentation") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# --------------------------------------------------------------------------- jobs
def submit(kind: str, fn, *args) -> dict:
    jid = "J-" + uuid.uuid4().hex[:10]
    job = {"id": jid, "kind": kind, "status": "queued", "progress": [], "result": None, "error": None,
           "created": time.time(), "started": None, "finished": None}
    JOBS[jid] = job

    def run() -> None:
        job["status"] = "running"
        job["started"] = time.time()
        try:
            job["result"] = fn(job, *args)
            job["status"] = "done"
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s failed", jid)
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["traceback"] = traceback.format_exc()[-4000:]
        job["finished"] = time.time()

    POOL.submit(run)
    return {"job_id": jid, "status": "queued"}


@app.get("/api/jobs/{jid}")
def get_job(jid: str) -> dict:
    job = JOBS.get(jid)
    if job is None:
        raise HTTPException(404, "unknown job")
    out = {k: v for k, v in job.items() if k != "traceback"}
    if job["started"]:
        out["elapsed_s"] = round((job["finished"] or time.time()) - job["started"], 1)
    return out


# ------------------------------------------------------------------- payloads
def scenario_payload(s: Session) -> dict:
    sc = s.world.scenario
    tm = s.world.travel()
    visits = []
    for v in sc.visits.values():
        types = [sc.interventions[i].type for i in v.intervention_ids]
        visits.append({
            "id": v.id, "recipient_id": v.recipient_id, "location_id": v.location_id,
            "earliest": v.earliest_start, "latest": v.latest_start, "preferred": v.preferred_start,
            "duration": v.total_duration_minutes, "staff": v.required_employee_count, "priority": v.priority,
            "timing": v.timing.value, "skills": v.required_skills, "delegations": v.required_delegations,
            "slot": v.slot, "interventions": len(v.intervention_ids), "types": types, "locked": v.locked,
            "intimate": v.intimate_care,
            "status": v.status.value,
            "has_medication": any(t in ("medication", "insulin", "eye_drops") for t in types),
        })
    R, E = sc.recipients.values(), sc.employees.values()
    return {
        "id": sc.id,
        "day": sc.day,
        "weekday": sc.weekday,
        "week": week_ref(s.week_id) if s.week_id else None,
        "rules": to_jsonable(sc.rules),
        "wishes": {
            "gender_preference": sum(1 for r in R if r.gender_preference),
            "gender_strict": sum(1 for r in R if r.gender_strict),
            "language": sum(1 for r in R if r.languages),
            "language_required": sum(1 for r in R if r.language_required),
            "pets": sum(1 for r in R if r.pets),
            "smokes": sum(1 for r in R if r.smokes),
            "male_staff": sum(1 for e in E if e.gender == "M"),
            "bike_staff": sum(1 for e in E if e.travel_mode == "bike"),
            "pet_allergic_staff": sum(1 for e in E if e.pet_allergies),
            "smoke_free_staff": sum(1 for e in E if e.avoid_smoking),
            "intimate_visits": sum(1 for v in sc.visits.values() if v.intimate_care),
        },
        "config": to_jsonable(sc.config),
        "area": sc.area_name,
        "center": AREAS[sc.config.area].center,
        "zones": [c.name for c in AREAS[sc.config.area].clusters],
        "counts": {"interventions": len(sc.interventions), "visits": len(sc.visits), "recipients": len(sc.recipients),
                   "employees": len(sc.employees),
                   "double_staffed": sum(1 for v in sc.visits.values() if v.required_employee_count == 2),
                   "hard_windows": sum(1 for v in sc.visits.values() if v.timing.value == "hard"),
                   "with_requirements": sum(1 for v in sc.visits.values() if v.required_skills or v.required_delegations),
                   "care_minutes": sum(v.total_duration_minutes * v.required_employee_count for v in sc.visits.values())},
        "locations": {k: {"lat": l.lat, "lon": l.lon, "zone": l.zone, "label": l.label} for k, l in sc.locations.items()},
        "bases": sc.bases,
        "recipients": [to_jsonable(r) for r in sc.recipients.values()],
        "employees": [to_jsonable(e) for e in sc.employees.values()],
        "visits": visits,
        "travel": {"source": tm.source, "notes": tm.base.notes, "traffic": s.world.traffic.to_dict(),
                   "multiplier": sc.config.travel_time_multiplier},
        "weights": s.world.weights.to_dict(),
        "settings": s.world.settings.to_dict(),
        "current_plan_id": s.current_plan_id,
        "plans": [{"id": p.id, "strategy": p.strategy, "created_at": p.created_at, "clock": p.clock,
                   "parent": p.parent_plan_id, "valid": p.validation.get("valid"),
                   "planned": p.score.get("planned_visits"), "unplanned": p.score.get("unplanned_visits")}
                  for p in s.plans.values()],
        "history": s.history,
    }


def week_ref(wid: str) -> dict:
    days = []
    for sid in WEEKS.get(wid, []):
        s = STORE.sessions.get(sid)
        if s is None:
            continue
        days.append({"day": s.world.scenario.day, "weekday": s.world.scenario.weekday, "scenario_id": sid,
                     "current_plan_id": s.current_plan_id})
    return {"id": wid, "days": days}


def plan_payload(p: Plan) -> dict:
    d = to_jsonable(p)
    d["summary_text"] = explain_plan_text(p)
    return d


# -------------------------------------------------------------------- models
class ScenarioIn(BaseModel):
    seed: int = 42
    target_interventions: int = Field(5000, ge=100, le=12000)
    employee_count: int = Field(100, ge=2, le=300)
    area: str = "stockholm"
    staffing_pressure: float = Field(0.15, ge=0, le=1)
    double_staffing_pct: float = Field(0.12, ge=0, le=0.6)
    strict_window_pct: float = Field(0.30, ge=0, le=1)
    skill_requirement_pct: float = Field(0.55, ge=0, le=1)
    travel_time_multiplier: float = Field(1.0, ge=0.3, le=4)
    double_staffing_sync_tolerance: int = Field(0, ge=0, le=15)
    breaks_enabled: bool = True
    preferences_enabled: bool = True
    days: int = Field(1, ge=1, le=7)  # 1 = one day, >1 = a full week (Mon..Sun)
    travel_provider: str = "auto"


class PlanIn(BaseModel):
    strategy: str = "baseline"
    time_limit_s: float = Field(25, ge=2, le=300)
    weights: dict[str, Any] | None = None
    max_overtime_min: int | None = Field(None, ge=0, le=120)


class IncidentIn(BaseModel):
    kind: str
    clock: int = Field(..., ge=0, le=24 * 60)
    params: dict[str, Any] = {}
    strategy: str = "baseline"


class ExperimentIn(BaseModel):
    scenario: ScenarioIn = ScenarioIn()
    incidents: list[IncidentIn] = []
    strategies: list[str] = ["baseline", "enhanced", "advisor", "classifier", "learned"]
    base_time_limit_s: float = Field(25, ge=2, le=120)
    skip_llm_without_key: bool = False


class LiveIn(BaseModel):
    start: int = Field(7 * 60 + 30, ge=0, le=24 * 60)
    end: int = Field(20 * 60, ge=0, le=24 * 60)
    events: int = Field(8, ge=1, le=40)
    seed: int = 7
    pace_s: float = Field(1.5, ge=0, le=10)


class LockIn(BaseModel):
    locked: bool = True


# ------------------------------------------------------------------ endpoints
@app.get("/api/health")
def health() -> dict:
    prov = make_provider()
    return {
        "ok": True,
        "travel_provider": prov.name,
        "google_maps_configured": bool(os.environ.get("GOOGLE_MAPS_API_KEY")),
        "llm_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "llm_model": os.environ.get("NOTANA_CLAUDE_MODEL", "claude-opus-5-5"),
        "classifier": "mock-linear (no JEV SDK available)",
        "sessions": len(STORE.sessions),
        "version": VERSION,
        "started_at": STARTED_AT,
        "features": FEATURES,
    }


@app.get("/api/meta")
def meta() -> dict:
    return {
        "areas": {k: {"name": a.name, "center": a.center, "zones": [c.name for c in a.clusters]} for k, a in AREAS.items()},
        "defaults": to_jsonable(ScenarioConfig()),
        "weights": ObjectiveWeights().to_dict(),
        "settings": SolverSettings().to_dict(),
        "strategies": STRATEGIES,
        "incident_kinds": INCIDENT_KINDS,
        "skills": list(C.SKILLS),
        "delegations": list(C.DELEGATIONS),
        "intervention_types": {t.key: t.label for t in C.INTERVENTION_TYPES},
        # The browser key is public by nature: restrict it to Maps JavaScript API and
        # to this site's URL (HTTP referrer) in Google Cloud. Never the server key.
        "map": {
            "provider": "google" if os.environ.get("GOOGLE_MAPS_BROWSER_KEY") else "leaflet",
            "browser_key": os.environ.get("GOOGLE_MAPS_BROWSER_KEY") or None,
            "road_geometry": bool(os.environ.get("GOOGLE_MAPS_API_KEY")),
        },
    }


@app.post("/api/scenarios")
def create_scenario(body: ScenarioIn, background: bool = False) -> dict:
    """Generate a day (or week). With ``background=true`` it runs as a job: building
    the travel matrix with Google Routes can take up to its time budget (60 s)."""
    cfg = ScenarioConfig(**{k: v for k, v in body.model_dump().items() if k != "travel_provider"})
    if cfg.area not in AREAS:
        raise HTTPException(400, f"unknown area {cfg.area}")
    if background:
        def work(job: dict) -> dict:
            return _create(cfg, body.travel_provider, job["progress"].append)

        return submit("scenario", work)
    return _create(cfg, body.travel_provider)


def _provider(name: str, progress=None):  # noqa: ANN001, ANN202
    prov = make_provider(name)
    if progress is not None and hasattr(prov, "progress"):
        prov.progress = progress
        progress("building the travel-time matrix with Google Routes (closest pairs first, max "
                 f"{int(getattr(prov, 'budget_s', 60))} s)")
    return prov


def _create(cfg: ScenarioConfig, travel_provider: str, progress=None) -> dict:  # noqa: ANN001
    if cfg.days > 1:
        return create_week(cfg, travel_provider, progress)
    if progress:
        progress("generating interventions, visits and staff")
    sc = generate_scenario(cfg, scenario_id="S-" + uuid.uuid4().hex[:8])
    world = World.create(sc, provider=_provider(travel_provider, progress))
    s = Session(sc.id, world)
    STORE.add(s)
    DB.save_scenario(sc.id, to_jsonable(cfg), scenario_payload(s)["counts"])
    return scenario_payload(s)


def create_week(cfg: ScenarioConfig, travel_provider: str, progress=None) -> dict:  # noqa: ANN001
    wid = "W-" + uuid.uuid4().hex[:8]
    if progress:
        progress("generating Monday to Sunday")
    scenarios = generate_week(cfg, scenario_id=wid)
    first = World.create(scenarios[0], provider=_provider(travel_provider, progress))
    WEEKS[wid] = [sc.id for sc in scenarios]
    for sc in scenarios:
        # Same addresses every day: one travel matrix for the week.
        w = first if sc is scenarios[0] else World(
            sc, first.base_matrix, weights=ObjectiveWeights.from_dict(first.weights.to_dict()),
            settings=SolverSettings(**first.settings.to_dict()),
        )
        s = Session(sc.id, w, week_id=wid)
        STORE.add(s)
        DB.save_scenario(sc.id, to_jsonable(sc.config), {"week": wid, "day": sc.day, "visits": len(sc.visits)})
    return scenario_payload(STORE.get(scenarios[0].id))


def _week(wid: str) -> list[Session]:
    if wid not in WEEKS:
        raise HTTPException(404, "unknown week (sessions are in memory; regenerate from the seed)")
    sessions = [STORE.sessions.get(sid) for sid in WEEKS[wid]]
    if any(s is None for s in sessions):
        raise HTTPException(404, "week evicted from memory; regenerate from the seed")
    return sessions  # type: ignore[return-value]


@app.get("/api/weeks/{wid}")
def get_week(wid: str) -> dict:
    sessions = _week(wid)
    out = week_summary([s.world.scenario for s in sessions],
                       [s.plans.get(s.current_plan_id) if s.current_plan_id else None for s in sessions])
    out["id"] = wid
    return out


@app.post("/api/weeks/{wid}/plan")
def optimize_week(wid: str, body: PlanIn) -> dict:
    sessions = _week(wid)
    if body.strategy not in STRATEGIES:
        raise HTTPException(400, f"unknown strategy; choose from {list(STRATEGIES)}")
    by_world = {id(s.world): s for s in sessions}

    def plan_and_record(world: World, strategy: str, limit: float | None) -> Plan:
        s = by_world[id(world)]
        with s.lock:
            if body.weights:
                world.weights = ObjectiveWeights.from_dict({**world.weights.to_dict(), **body.weights})
            if body.max_overtime_min is not None:
                world.settings.max_overtime_min = body.max_overtime_min
            plan = plan_day(world, strategy, limit)
            s.plans[plan.id] = plan
            s.current_plan_id = plan.id
            WORLD_SNAPSHOTS[plan.id] = clone_world(world)
            DB.save_plan(plan)
            ev = {"type": "plan", "plan_id": plan.id, "strategy": strategy, "summary": explain_plan_text(plan)}
            s.history.append(ev)
            DB.save_event(s.id, "plan", ev)
            return plan

    def work(job: dict) -> dict:
        plans = plan_week([s.world for s in sessions], body.strategy, body.time_limit_s,
                          progress=job["progress"].append, plan_fn=plan_and_record)
        return {"week_id": wid, "plan_ids": [p.id for p in plans],
                "valid": all(p.validation.get("valid") for p in plans)}

    return submit("plan-week", work)


@app.get("/api/scenarios/{sid}")
def get_scenario(sid: str) -> dict:
    return scenario_payload(_session(sid))


def _session(sid: str) -> Session:
    try:
        return STORE.get(sid)
    except KeyError:
        raise HTTPException(404, "unknown scenario: the server restarted or was redeployed, which clears in-memory sessions; regenerate from the seed") from None


def _plan(pid: str) -> tuple[Session, Plan]:
    try:
        return STORE.find_plan(pid)
    except KeyError:
        raise HTTPException(404, "unknown plan") from None


@app.post("/api/scenarios/{sid}/plan")
def optimize(sid: str, body: PlanIn) -> dict:
    s = _session(sid)
    if body.strategy not in STRATEGIES:
        raise HTTPException(400, f"unknown strategy; choose from {list(STRATEGIES)}")

    def work(job: dict) -> dict:
        with s.lock:
            if body.weights:
                s.world.weights = ObjectiveWeights.from_dict({**s.world.weights.to_dict(), **body.weights})
            if body.max_overtime_min is not None:
                s.world.settings.max_overtime_min = body.max_overtime_min
            job["progress"].append(f"optimising {len(s.world.scenario.visits)} visits with {len(s.world.scenario.employees)} employees")
            plan = plan_day(s.world, body.strategy, body.time_limit_s)
            s.plans[plan.id] = plan
            s.current_plan_id = plan.id
            WORLD_SNAPSHOTS[plan.id] = clone_world(s.world)
            DB.save_plan(plan)
            ev = {"type": "plan", "plan_id": plan.id, "strategy": body.strategy,
                  "summary": explain_plan_text(plan)}
            s.history.append(ev)
            DB.save_event(sid, "plan", ev)
            return {"plan_id": plan.id, "valid": plan.validation.get("valid")}

    return submit("plan", work)


@app.get("/api/plans/{pid}")
def get_plan(pid: str) -> dict:
    return plan_payload(_plan(pid)[1])


@app.post("/api/plans/{pid}/activate")
def activate_plan(pid: str) -> dict:
    """Return to an earlier plan (and the world state it belongs to)."""
    s, p = _plan(pid)
    with s.lock:
        if pid in WORLD_SNAPSHOTS:
            s.world = clone_world(WORLD_SNAPSHOTS[pid])
        s.current_plan_id = pid
    return {"current_plan_id": pid}


@app.post("/api/plans/{pid}/validate")
def revalidate(pid: str) -> dict:
    s, p = _plan(pid)
    w = WORLD_SNAPSHOTS.get(pid, s.world)
    parent = s.plans.get(p.parent_plan_id) if p.parent_plan_id else None
    rep = validate_plan(w.scenario, w.travel(), p, sync_tolerance=w.settings.sync_tolerance_min,
                        max_overtime=w.settings.max_overtime_min, reference=parent, clock=p.clock)
    return rep.to_dict()


@app.get("/api/plans/{pid}/visits/{vid}")
def visit_detail(pid: str, vid: str) -> dict:
    s, p = _plan(pid)
    w = WORLD_SNAPSHOTS.get(pid, s.world)
    if vid not in w.scenario.visits:
        raise HTTPException(404, "unknown visit")
    parent = s.plans.get(p.parent_plan_id) if p.parent_plan_id else None
    return explain_visit(w, p, vid, parent)


@app.get("/api/plans/{pid}/routes/{eid}/geometry")
def route_geometry(pid: str, eid: str) -> dict:
    """Road paths of one employee's route for display (straight lines without a key)."""
    from .travel.road_geometry import road_legs

    s, p = _plan(pid)
    w = WORLD_SNAPSHOTS.get(pid, s.world)
    r = p.routes.get(eid)
    if r is None:
        raise HTTPException(404, "unknown employee")
    locs = w.scenario.locations
    emp = w.scenario.employees[eid]
    seq = [r.start_location_id or emp.start_location_id]
    for st in sorted(r.stops, key=lambda x: x.start):
        if st.kind == "unavailable":
            continue  # unpaid gap: no driving into it
        seq.append(st.location_id)
    seq.append(r.end_location_id or emp.end_location_id)
    pts = [(locs[x].lat, locs[x].lon) for x in seq]
    return {"employee_id": eid, "points": pts, **road_legs(pts)}


@app.get("/api/plans/{pid}/diff/{other}")
def diff(pid: str, other: str) -> dict:
    s, a = _plan(pid)
    _, b = _plan(other)
    return plan_diff(s.world.scenario, a, b, b.clock)


@app.post("/api/plans/{pid}/advisor")
def advisor_explain(pid: str) -> dict:
    s, p = _plan(pid)
    adv = make_advisor()
    conflicts = [{"visit_id": v, "code": u.reasons[0].code if u.reasons else "UNKNOWN",
                  "message": u.reasons[0].message if u.reasons else ""} for v, u in p.unplanned.items()]
    facts = {"text": explain_plan_text(p), "score": {k: v for k, v in p.score.items() if k != "weighted"}}
    return {"advisor": adv.name, "plan": adv.explain_plan(facts), "conflicts": adv.analyze_conflict(conflicts),
            "disclaimer": "Narrative only. Feasibility is decided by the optimizer and the independent validator."}


@app.post("/api/scenarios/{sid}/visits/{vid}/lock")
def lock_visit(sid: str, vid: str, body: LockIn) -> dict:
    s = _session(sid)
    v = s.world.scenario.visits.get(vid)
    if v is None:
        raise HTTPException(404, "unknown visit")
    if body.locked and (s.current is None or vid not in s.current.assignments):
        raise HTTPException(400, "only planned visits can be locked")
    v.locked = body.locked
    return {"visit_id": vid, "locked": v.locked}


def _random_employees(s: Session, n: int, clock: int, seed: int) -> list[str]:
    plan = s.current
    cands = sorted(
        e for e, r in plan.routes.items()
        if any(st.kind == "visit" and st.start > clock for st in r.stops)
    ) if plan else []
    rng = random.Random(seed)
    return rng.sample(cands, min(n, len(cands)))


@app.post("/api/scenarios/{sid}/incidents")
def incident(sid: str, body: IncidentIn) -> dict:
    s = _session(sid)
    if s.current is None:
        raise HTTPException(400, "optimise a plan first")
    if body.kind not in INCIDENT_KINDS:
        raise HTTPException(400, f"unknown incident; choose from {INCIDENT_KINDS}")
    if body.strategy not in STRATEGIES:
        raise HTTPException(400, f"unknown strategy; choose from {list(STRATEGIES)}")
    params = dict(body.params)
    if body.kind == "employee_sick" and params.get("random"):
        params["employee_ids"] = _random_employees(s, int(params["random"]), body.clock, int(params.get("seed", 7)))
        if not params["employee_ids"]:
            raise HTTPException(400, "no employee with remaining visits at that time")

    def work(job: dict) -> dict:
        with s.lock:
            before = s.current
            assert before is not None
            if before.clock is not None and body.clock < before.clock:
                raise ValueError(f"clock {fmt_time(body.clock)} is before the current plan's clock {fmt_time(before.clock)}")
            inc = Incident(body.kind, body.clock, params)
            effect = apply_incident(s.world, before, inc)
            job["progress"].append(effect.description)
            res, meta = replan_with_strategy(s.world, before, effect, body.strategy)
            after = res.plan
            s.plans[after.id] = after
            s.current_plan_id = after.id
            WORLD_SNAPSHOTS[after.id] = clone_world(s.world)
            DB.save_plan(after)
            ev = {
                "type": "incident",
                "incident": inc.to_dict(),
                "effect": effect.to_dict(),
                "before_plan_id": before.id,
                "after_plan_id": after.id,
                "strategy": body.strategy,
                "strategy_used": res.strategy_used,
                "summary": res.summary,
                "summary_facts": res.summary_facts,
                "diff_counts": res.diff["counts"],
                "phases": after.solver_stats.get("replan", {}).get("phases"),
                "meta": meta,
            }
            s.history.append(ev)
            DB.save_event(sid, "incident", ev)
            return {**ev, "diff": res.diff}

    return submit("incident", work)


def _record(s: Session, plan: Plan, ev: dict) -> None:
    s.plans[plan.id] = plan
    s.current_plan_id = plan.id
    WORLD_SNAPSHOTS[plan.id] = clone_world(s.world)
    DB.save_plan(plan)
    s.history.append(ev)
    DB.save_event(s.id, ev["type"], ev)


def _current_plan(pid: str) -> tuple[Session, Plan]:
    s, p = _plan(pid)
    if s.current_plan_id != pid:
        raise HTTPException(409, "suggestions work on the current plan; activate this plan first")
    return s, p


def _suggestions_payload(pid: str) -> dict:
    sug = SUGGESTIONS.get(pid, {})
    total = len(sug)
    best = {vid: (opts[0]["level"] if opts else None) for vid, opts in sug.items()}
    return {
        "plan_id": pid,
        "visits": {vid: [public(o) for o in opts] for vid, opts in sug.items()},
        "summary": {
            "unplanned": total,
            "within_rules": sum(1 for b in best.values() if b == "within_rules"),
            "minor": sum(1 for b in best.values() if b == "minor"),
            "major": sum(1 for b in best.values() if b == "major"),
            "resource": sum(1 for b in best.values() if b == "resource"),
            "not_permitted": sum(1 for b in best.values() if b == "not_permitted"),
        },
    }


@app.post("/api/plans/{pid}/suggestions")
def compute_suggestions(pid: str) -> dict:
    s, p = _current_plan(pid)

    def work(job: dict) -> dict:
        with s.lock:
            SUGGESTIONS[pid] = suggest_for_plan(s.world, p, progress=job["progress"].append)
        return _suggestions_payload(pid)

    return submit("suggestions", work)


@app.get("/api/plans/{pid}/suggestions")
def get_suggestions(pid: str) -> dict:
    if pid not in SUGGESTIONS:
        raise HTTPException(404, "no suggestions computed for this plan")
    return _suggestions_payload(pid)


@app.post("/api/plans/{pid}/visits/{vid}/suggestions")
def visit_suggestions(pid: str, vid: str) -> dict:
    """Options for one unplanned visit (recommended first), computed on demand."""
    s, p = _current_plan(pid)
    if vid not in p.unplanned:
        raise HTTPException(400, f"{vid} is not unplanned in this plan")
    v = s.world.scenario.visits.get(vid)
    if v is not None and p.clock is not None and v.latest_start < p.clock:
        return {"plan_id": pid, "visit_id": vid, "options": [],
                "window_closed": fmt_time(v.latest_start), "clock": fmt_time(p.clock),
                "reason": f"the time window closed at {fmt_time(v.latest_start)}, before the current clock {fmt_time(p.clock)}"}
    with s.lock:
        opts = suggest_for_plan(s.world, p, [vid], replan_time_s=2.5).get(vid, [])
    SUGGESTIONS.setdefault(pid, {})[vid] = opts
    return {"plan_id": pid, "visit_id": vid, "options": [public(o) for o in opts]}


@app.post("/api/plans/{pid}/suggestions/{oid}/apply")
def apply_suggestion(pid: str, oid: str) -> dict:
    s, p = _current_plan(pid)
    opt = next((o for opts in SUGGESTIONS.get(pid, {}).values() for o in opts if o["id"] == oid), None)
    if opt is None:
        raise HTTPException(404, "unknown suggestion (recompute suggestions for the current plan)")
    if opt["kind"] == "pool":
        raise HTTPException(400, "apply pool options as an 'extra staff' incident")
    with s.lock:
        new = apply_option(s.world, p, opt)
        ev = {"type": "suggestion", "plan_id": new.id, "before_plan_id": p.id, "visit_id": opt["visit_id"],
              "option": public(opt), "summary": f"{opt['visit_id']}: {opt['title']}"
              + ("" if opt["severity"] == 0 else f" — approved exception ({opt['severity']} points)")}
        _record(s, new, ev)
    return {"plan_id": new.id, "valid": new.validation.get("valid"), "status": new.validation.get("status")}


@app.post("/api/plans/{pid}/autofix")
def autofix_plan(pid: str, deep_s: float = 45.0, budget_s: float = 120.0) -> dict:
    s, p = _current_plan(pid)

    def work(job: dict) -> dict:
        with s.lock:
            new, summary = autofix(s.world, p, progress=job["progress"].append, deep_s=deep_s, budget_s=budget_s)
            if new is not p:
                ev = {"type": "autofix", "plan_id": new.id, "before_plan_id": p.id, "summary_facts": summary,
                      "summary": f"Auto-fix within rules: {summary['net_planned_gain']} more visit(s) planned, "
                                 f"{summary['unplanned_after']} still unplanned."}
                _record(s, new, ev)
            return {"plan_id": new.id, "changed": new is not p, **summary}

    return submit("autofix", work)


@app.post("/api/scenarios/{sid}/live")
def start_live(sid: str, body: LiveIn) -> dict:
    """Run the day live: random (seeded) events, each re-planned at once."""
    s = _session(sid)
    if s.current is None:
        raise HTTPException(400, "optimise a plan first")
    if body.end <= body.start:
        raise HTTPException(400, "end must be after start")

    def work(job: dict) -> dict:
        job["feed"] = []
        job["clock"] = body.start
        job["cancel"] = False

        def on_event(entry: dict, plan: Plan) -> None:
            ev = {"type": "live", "plan_id": plan.id, "before_plan_id": entry["before_plan_id"],
                  "incident": entry["incident"], "summary": entry["summary"], "label": entry["label"]}
            _record(s, plan, ev)
            job["feed"].append(entry)
            job["latest_plan_id"] = plan.id
            job["progress"].append(f"{entry['clock_text']} {entry['label']}")

        with s.lock:
            start = s.current
            cur, feed = run_live(
                s.world, start, body.start, body.end, body.events, body.seed, body.pace_s,
                on_event=on_event, should_stop=lambda: job.get("cancel", False),
                on_clock=lambda c: job.__setitem__("clock", c),
            )
        return {"plan_id": cur.id, "events": len(feed), "feed": feed,
                "unplanned": len(cur.unplanned), "valid": cur.validation.get("valid"),
                "interventions_planned_share": cur.score.get("interventions_planned_share")}

    return submit("live", work)


def _ml_payload() -> dict:
    sel = selector(ML_STORE)
    cases = sel.cases()
    kinds: dict[str, int] = {}
    for c in cases:
        kinds[c["incident"]["kind"]] = kinds.get(c["incident"]["kind"], 0) + 1
    return {
        "model": sel.info,
        "cases_total": len(cases),
        "cases_by_kind": kinds,
        "cases_from_app": sum(1 for c in cases if c["source"].startswith("app:")),
        "recent": [{"source": c["source"], "incident": c["incident"], "best": c["best"],
                    "scores": {a: o["score"] for a, o in c["outcomes"].items()}} for c in cases[-12:]][::-1],
        "store": str(ML_STORE),
    }


@app.get("/api/ml")
def ml_status() -> dict:
    return _ml_payload()


@app.post("/api/ml/train")
def ml_train() -> dict:
    def work(job: dict) -> dict:
        job["progress"].append("training and cross-validating")
        selector(ML_STORE).train(evaluate_cv=True)
        return _ml_payload()

    return submit("ml-train", work)


@app.post("/api/scenarios/{sid}/ml/cases")
def ml_generate(sid: str, n: int = 4, seed: int = 1) -> dict:
    """Generate n new cases on this scenario's current plan (every action is run
    on a clone of the world, so the session itself is not changed), then retrain."""
    s = _session(sid)
    if s.current is None:
        raise HTTPException(400, "optimise a plan first")
    n = max(1, min(int(n), 20))

    def work(job: dict) -> dict:
        with s.lock:
            world, plan = clone_world(s.world), s.current
        cases = generate_cases(world, plan, n, seed, f"app:{sid}", progress=job["progress"].append)
        sel = selector(ML_STORE)
        sel.add_cases(cases)
        job["progress"].append("retraining and cross-validating")
        sel.train(evaluate_cv=True)
        return {"added": len(cases), **_ml_payload()}

    return submit("ml-cases", work)


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str) -> dict:
    job = JOBS.get(jid)
    if job is None:
        raise HTTPException(404, "unknown job")
    job["cancel"] = True
    return {"job_id": jid, "cancel": True}


_IV_CACHE: dict[tuple[str, str | None], list[dict]] = {}


@app.get("/api/scenarios/{sid}/interventions")
def scenario_interventions(sid: str, plan_id: str | None = None, q: str = "", filter: str = "all",
                           offset: int = 0, limit: int = 100) -> dict:
    """Every single intervention with its visit, recipient, need, time, requirements and
    assignment (or why it is unassigned). Filtered and paged on the server."""
    s = _session(sid)
    pid = plan_id or s.current_plan_id
    plan = s.plans.get(pid) if pid else None
    world = WORLD_SNAPSHOTS.get(pid, s.world) if pid else s.world
    key = (sid, pid)
    if key not in _IV_CACHE:
        if len(_IV_CACHE) > 40:
            _IV_CACHE.clear()
        _IV_CACHE[key] = IV.rows(world, plan)
    rows_ = _IV_CACHE[key]
    if filter not in IV.FILTERS:
        raise HTTPException(400, f"unknown filter; choose from {list(IV.FILTERS)}")
    return {"summary": IV.summary(world, plan, rows_),
            **IV.query(rows_, q, filter, max(0, offset), max(1, min(limit, 500)))}


@app.get("/api/scenarios/{sid}/history")
def history(sid: str) -> list[dict]:
    return _session(sid).history


@app.get("/api/scenarios/{sid}/export")
def export(sid: str) -> JSONResponse:
    s = _session(sid)
    return JSONResponse({"scenario": to_jsonable(s.world.scenario), "plans": [to_jsonable(p) for p in s.plans.values()],
                         "history": s.history})


@app.post("/api/experiments")
def experiment(body: ExperimentIn) -> dict:
    cfg = ScenarioConfig(**{k: v for k, v in body.scenario.model_dump().items() if k != "travel_provider"})
    bad = [x for x in body.strategies if x not in STRATEGIES]
    if bad:
        raise HTTPException(400, f"unknown strategies {bad}")
    spec = ExperimentSpec(cfg, [Incident(i.kind, i.clock, i.params) for i in body.incidents], body.strategies,
                          body.base_time_limit_s, body.skip_llm_without_key)

    def work(job: dict) -> dict:
        return run_experiment(spec, lambda m: job["progress"].append(m))

    return submit("experiment", work)


@app.post("/api/plans/{pid}/diagnose")
def rediagnose(pid: str) -> dict:
    s, p = _plan(pid)
    w = WORLD_SNAPSHOTS.get(pid, s.world)
    return {k: to_jsonable(v) for k, v in diagnose_unplanned(w.scenario, w.travel(), p).items()}


# ------------------------------------------------------------------ frontend
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(FRONTEND / "index.html")

    @app.get("/documentation")
    def documentation() -> FileResponse:
        """System documentation: architecture, engine, rules, AI and ML, operations."""
        return FileResponse(FRONTEND / "docs.html")

