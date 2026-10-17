"""SQLite persistence for PRDs, blueprints, approvals, runs, trace events and audit (FR-09)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .contracts import TraceEvent, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS prds (
    id TEXT PRIMARY KEY, title TEXT, text TEXT NOT NULL, hash TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS blueprints (
    id TEXT NOT NULL, version INTEGER NOT NULL, prd_id TEXT NOT NULL, hash TEXT,
    status TEXT NOT NULL, blueprint TEXT, report TEXT, analysis TEXT, issues TEXT,
    created_at TEXT, PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, blueprint_id TEXT, version INTEGER, decision TEXT,
    approver TEXT, approved_tools TEXT, comment TEXT, blueprint_hash TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, blueprint_id TEXT, version INTEGER, status TEXT, disposition TEXT,
    inputs TEXT, result TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT, seq INTEGER, ts TEXT, type TEXT, node TEXT, data TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT, action TEXT, subject TEXT,
    data TEXT
);
"""


def _j(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _uj(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self.lock:
            self.db.execute(sql, params)
            self.db.commit()

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # ---- audit
    def audit(self, action: str, subject: str, data: dict[str, Any] | None = None,
              actor: str = "system") -> None:
        self._exec("INSERT INTO audit (ts, actor, action, subject, data) VALUES (?,?,?,?,?)",
                   (utcnow().isoformat(), actor, action, subject, _j(data or {})))

    def audit_log(self, subject: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM audit" + (" WHERE subject = ?" if subject else "") + " ORDER BY id"
        rows = self._all(sql, (subject,) if subject else ())
        return [{**r, "data": _uj(r["data"])} for r in rows]

    # ---- prds
    def add_prd(self, prd_id: str, title: str, text: str, hash_: str) -> None:
        self._exec("INSERT INTO prds VALUES (?,?,?,?,?)",
                   (prd_id, title, text, hash_, utcnow().isoformat()))

    def get_prd(self, prd_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM prds WHERE id = ?", (prd_id,))

    # ---- blueprints
    def latest_blueprint_for_hash(self, prd_hash: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT b.id, b.version FROM blueprints b JOIN prds p ON p.id = b.prd_id "
            "WHERE p.hash = ? ORDER BY b.created_at DESC, b.version DESC LIMIT 1", (prd_hash,))

    def add_blueprint(self, *, blueprint_id: str, version: int, prd_id: str, hash_: str | None,
                      status: str, blueprint: Any, report: Any, analysis: Any,
                      issues: Any) -> None:
        self._exec("INSERT INTO blueprints VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (blueprint_id, version, prd_id, hash_, status, _j(blueprint), _j(report),
                    _j(analysis), _j(issues), utcnow().isoformat()))

    def set_blueprint_status(self, blueprint_id: str, version: int, status: str) -> None:
        self._exec("UPDATE blueprints SET status = ? WHERE id = ? AND version = ?",
                   (status, blueprint_id, version))

    def get_blueprint(self, blueprint_id: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self._one("SELECT * FROM blueprints WHERE id = ? ORDER BY version DESC LIMIT 1",
                            (blueprint_id,))
        else:
            row = self._one("SELECT * FROM blueprints WHERE id = ? AND version = ?",
                            (blueprint_id, version))
        return self._decode_bp(row) if row else None

    def list_blueprints(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT b.id, b.version, b.status, b.hash, b.created_at, b.prd_id, "
                         "p.title FROM blueprints b JOIN prds p ON p.id = b.prd_id "
                         "ORDER BY b.created_at DESC")
        return rows

    @staticmethod
    def _decode_bp(row: dict[str, Any]) -> dict[str, Any]:
        return {**row, **{k: _uj(row[k]) for k in ("blueprint", "report", "analysis", "issues")}}

    # ---- approvals
    def add_approval(self, *, blueprint_id: str, version: int, decision: str, approver: str,
                     approved_tools: list[str], comment: str, blueprint_hash: str) -> None:
        self._exec("INSERT INTO approvals (blueprint_id, version, decision, approver, "
                   "approved_tools, comment, blueprint_hash, created_at) VALUES (?,?,?,?,?,?,?,?)",
                   (blueprint_id, version, decision, approver, _j(approved_tools), comment,
                    blueprint_hash, utcnow().isoformat()))

    def latest_approval(self, blueprint_id: str, version: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM approvals WHERE blueprint_id = ? AND version = ? "
                        "ORDER BY id DESC LIMIT 1", (blueprint_id, version))
        return {**row, "approved_tools": _uj(row["approved_tools"])} if row else None

    # ---- runs & events
    def add_run(self, run_id: str, blueprint_id: str, version: int, status: str,
                inputs: dict[str, Any]) -> None:
        now = utcnow().isoformat()
        self._exec("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                   (run_id, blueprint_id, version, status, None, _j(inputs), None, now, now))

    def update_run(self, run_id: str, *, status: str, disposition: str | None = None,
                   result: Any = None, inputs: dict[str, Any] | None = None) -> None:
        sets = "status = ?, disposition = ?, result = ?, updated_at = ?"
        params: list[Any] = [status, disposition, _j(result), utcnow().isoformat()]
        if inputs is not None:
            sets += ", inputs = ?"
            params.append(_j(inputs))
        self._exec(f"UPDATE runs SET {sets} WHERE id = ?", (*params, run_id))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return {**row, "inputs": _uj(row["inputs"]), "result": _uj(row["result"])} if row else None

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._all("SELECT id, blueprint_id, version, status, disposition, created_at, "
                         "updated_at FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))

    def runs_with_status(self, status: str) -> list[str]:
        return [r["id"] for r in self._all("SELECT id FROM runs WHERE status = ?", (status,))]

    def add_event(self, e: TraceEvent) -> None:
        self._exec("INSERT INTO events VALUES (?,?,?,?,?,?)",
                   (e.run_id, e.seq, e.ts.isoformat(), e.type, e.node, _j(e.data)))

    def events(self, run_id: str, after: int = -1) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
                         (run_id, after))
        return [{**r, "data": _uj(r["data"])} for r in rows]
