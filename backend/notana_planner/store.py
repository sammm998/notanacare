"""SQLite persistence (audit trail) + in-memory live sessions.

Live state (world + plan objects) is held in memory. Every scenario config,
plan and incident is also written to SQLite as JSON, so the full history can
be exported or audited. Because generation is deterministic, a session can be
reconstructed from its config (seed) plus the stored incident sequence.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import Plan, to_jsonable
from .planner import World

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (id TEXT PRIMARY KEY, created REAL, config TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, scenario_id TEXT, created REAL, strategy TEXT,
    parent_id TEXT, valid INTEGER, score TEXT, body TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT, created REAL,
    kind TEXT, body TEXT);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple) -> None:
        with self._lock:
            self._conn.execute(sql, args)
            self._conn.commit()

    def save_scenario(self, sid: str, config: dict, summary: dict) -> None:
        self._exec("INSERT OR REPLACE INTO scenarios VALUES (?,?,?,?)", (sid, time.time(), json.dumps(config), json.dumps(summary)))

    def save_plan(self, plan: Plan) -> None:
        self._exec(
            "INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?,?,?,?)",
            (
                plan.id,
                plan.scenario_id,
                time.time(),
                plan.strategy,
                plan.parent_plan_id,
                int(bool(plan.validation.get("valid"))),
                json.dumps(plan.score, default=str),
                json.dumps(to_jsonable(plan), default=str),
            ),
        )

    def save_event(self, sid: str, kind: str, body: dict) -> None:
        self._exec("INSERT INTO events (scenario_id, created, kind, body) VALUES (?,?,?,?)", (sid, time.time(), kind, json.dumps(body, default=str)))

    def events(self, sid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT created, kind, body FROM events WHERE scenario_id=? ORDER BY id", (sid,)).fetchall()
        return [{"created": r[0], "kind": r[1], **json.loads(r[2])} for r in rows]


@dataclass
class Session:
    id: str
    world: World
    plans: dict[str, Plan] = field(default_factory=dict)
    current_plan_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def current(self) -> Plan | None:
        return self.plans.get(self.current_plan_id) if self.current_plan_id else None


class SessionStore:
    def __init__(self, db: Database, max_sessions: int = 8) -> None:
        self.db = db
        self.sessions: dict[str, Session] = {}
        self.max_sessions = max_sessions
        self._lock = threading.Lock()

    def add(self, session: Session) -> None:
        with self._lock:
            self.sessions[session.id] = session
            while len(self.sessions) > self.max_sessions:
                self.sessions.pop(next(iter(self.sessions)))

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            raise KeyError(sid)
        return s

    def find_plan(self, pid: str) -> tuple[Session, Plan]:
        for s in self.sessions.values():
            if pid in s.plans:
                return s, s.plans[pid]
        raise KeyError(pid)
