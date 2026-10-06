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
from .generator import generate_scenario
from .geo import AREAS
from .incidents import INCIDENT_KINDS, Incident, apply_incident
from .planner import STRATEGIES, World, plan_day
from .replanning import clone_world, replan_with_strategy
from .store import Database, Session, SessionStore
from .travel import make_provider
from .validator import validate_plan

log = logging.getLogger("notana")
ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
DB = Database(os.environ.get("NOTANA_DB", str(ROOT / "data" / "notana.sqlite3")))
STORE = SessionStore(DB)
POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("NOTANA_WORKERS", "2")))
JOBS: dict[str, dict[str, Any]] = {}
WORLD_SNAPSHOTS: dict[str, World] = {}  # plan id -> world state the plan belongs to

app = FastAPI(title="Notana Care Planning Simulator", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
            "status": v.status.value,
            "has_medication": any(t in ("medication", "insulin", "eye_drops") for t in types),
        })
    return {
        "id": sc.id,
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
    staffing_pressure: float = Field(0.3, ge=0, le=1)
    double_staffing_pct: float = Field(0.12, ge=0, le=0.6)
    strict_window_pct: float = Field(0.30, ge=0, le=1)
    skill_requirement_pct: float = Field(0.55, ge=0, le=1)
    travel_time_multiplier: float = Field(1.0, ge=0.3, le=4)
    double_staffing_sync_tolerance: int = Field(0, ge=0, le=15)
    breaks_enabled: bool = True
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
    strategies: list[str] = ["baseline", "enhanced", "advisor", "classifier"]
    base_time_limit_s: float = Field(25, ge=2, le=120)
    skip_llm_without_key: bool = False


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
def create_scenario(body: ScenarioIn) -> dict:
    cfg = ScenarioConfig(**{k: v for k, v in body.model_dump().items() if k != "travel_provider"})
    if cfg.area not in AREAS:
        raise HTTPException(400, f"unknown area {cfg.area}")
    sc = generate_scenario(cfg, scenario_id="S-" + uuid.uuid4().hex[:8])
    world = World.create(sc, provider=make_provider(body.travel_provider))
    s = Session(sc.id, world)
    STORE.add(s)
    DB.save_scenario(sc.id, to_jsonable(cfg), scenario_payload(s)["counts"])
    return scenario_payload(s)


@app.get("/api/scenarios/{sid}")
def get_scenario(sid: str) -> dict:
    return scenario_payload(_session(sid))


def _session(sid: str) -> Session:
    try:
        return STORE.get(sid)
    except KeyError:
        raise HTTPException(404, "unknown scenario (sessions are in memory; regenerate from the seed)") from None


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

