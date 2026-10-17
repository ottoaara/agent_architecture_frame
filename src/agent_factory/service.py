"""Factory service: the single entry point used by the API, UI and CLI.

submit PRD → Work Director → stored blueprint (proposed | invalid)
approve/reject (human gate, FR-08) → start run (validated inputs) → trace + result persisted
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from typing import Any, Literal

from langgraph.checkpoint.sqlite import SqliteSaver

from .analyzer import LLMAnalyzer, TemplateAnalyzer, looks_like_template
from .capabilities import SkillCatalog, ToolRegistry, default_registry
from .config import Settings
from .contracts import Blueprint, ModelConfig, new_id, sha256_text
from .director import WorkDirector
from .executor import execute
from .models import ModelClient, make_model_client
from .store import Store
from .trace import Tracer
from .validator import check_run_inputs

AnalyzerMode = Literal["auto", "template", "llm"]


class FactoryError(Exception):
    def __init__(self, message: str, status: int = 400, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.details = status, details


class FactoryService:
    def __init__(self, settings: Settings | None = None, *,
                 model_factory: Callable[[ModelConfig], ModelClient] | None = None,
                 tools: ToolRegistry | None = None, skills: SkillCatalog | None = None) -> None:
        self.settings = settings or Settings.from_env()
        self.tools = tools or default_registry()
        self.skills = skills or SkillCatalog()
        self.store = Store(self.settings.db_path)
        self._model_factory = model_factory or (lambda cfg: make_model_client(
            cfg, self.settings.ollama_base_url, self.settings.ollama_model))
        conn = sqlite3.connect(self.settings.checkpoint_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(conn)
        self._threads: dict[str, threading.Thread] = {}
        self._recover_interrupted_runs()

    # ------------------------------------------------------------------ config

    def model_config(self) -> ModelConfig:
        s = self.settings
        model = s.ollama_model if s.provider == "ollama" else s.model
        return ModelConfig(provider=s.provider, model=model, effort=s.effort)

    def _recover_interrupted_runs(self) -> None:
        for run_id in self.store.runs_with_status("running"):
            run = self.store.get_run(run_id)
            self.store.update_run(run_id, status="failed", disposition="failed",
                                  result={**(run.get("result") or {}),
                                          "error": "interrupted: the factory restarted mid-run"})
            self.store.audit("run_interrupted", run_id)

    # ------------------------------------------------------------------ PRDs & blueprints

    def submit_prd(self, text: str, title: str | None = None,
                   analyzer: AnalyzerMode = "auto", actor: str = "user") -> dict[str, Any]:
        if not text or not text.strip():
            raise FactoryError("PRD text is empty", 422)
        cfg = self.model_config()
        chosen = self._choose_analyzer(text, analyzer, cfg)
        prd_hash = sha256_text(text)
        prd_id = new_id("prd")
        title = title or _title_of(text)
        self.store.add_prd(prd_id, title, text, prd_hash)
        self.store.audit("prd_submitted", prd_id, {"title": title, "hash": prd_hash,
                                                   "analyzer": type(chosen).__name__}, actor)

        previous = self.store.latest_blueprint_for_hash(prd_hash)
        version = previous["version"] + 1 if previous else 1
        bp_id = previous["id"] if previous else new_id("bp")
        director = WorkDirector(chosen, self.tools, self.skills, cfg)
        try:
            result = director.direct(text, version=version, blueprint_id=bp_id)
        except Exception as e:
            self.store.audit("analysis_failed", prd_id, {"error": str(e)}, actor)
            raise FactoryError(f"requirements analysis failed: {e}", 502) from e

        status = "proposed" if result.ok else "invalid"
        bp = result.blueprint
        self.store.add_blueprint(
            blueprint_id=bp_id, version=version, prd_id=prd_id,
            hash_=bp.content_hash() if bp else None, status=status,
            blueprint=bp.model_dump(mode="json") if bp else None,
            report=result.report.model_dump(), analysis=result.analysis.model_dump()
            if result.analysis else None,
            issues=[i.model_dump() for i in result.issues + result.warnings])
        self.store.audit("blueprint_generated", bp_id, {"version": version, "status": status,
                                                        "hash": bp.content_hash() if bp else None})
        run_id = new_id("run")
        self.store.add_run(run_id, bp_id, version, "pending", {})
        return {
            "prd_id": prd_id, "run_id": run_id, "blueprint_id": bp_id, "version": version,
            "status": status, "issues": [i.model_dump() for i in result.issues],
            "warnings": [i.model_dump() for i in result.warnings],
            "errors": [e.model_dump() for e in result.report.errors],
        }

    def _choose_analyzer(self, text: str, mode: AnalyzerMode, cfg: ModelConfig):
        if mode == "llm" or (mode == "auto" and cfg.provider != "mock"):
            if cfg.provider == "mock":
                raise FactoryError("LLM analysis needs a real provider "
                                   "(AGENT_FACTORY_PROVIDER=anthropic or ollama)", 422)
            return LLMAnalyzer(self._model_factory(cfg), self.tools, self.skills)
        if not looks_like_template(text):
            raise FactoryError(
                "template analysis needs a PRD in the structured template with a '## Tasks' "
                "section; use a real model provider for free-form PRDs", 422)
        return TemplateAnalyzer()

    def get_blueprint(self, blueprint_id: str, version: int | None = None) -> dict[str, Any]:
        row = self.store.get_blueprint(blueprint_id, version)
        if not row:
            raise FactoryError(f"blueprint {blueprint_id!r} not found", 404)
        approval = self.store.latest_approval(blueprint_id, row["version"])
        return {**row, "approval": approval}

    def list_blueprints(self) -> list[dict[str, Any]]:
        return self.store.list_blueprints()

    def decide(self, blueprint_id: str, version: int | None, approve: bool,
               approver: str = "user", approved_tools: list[str] | None = None,
               comment: str = "") -> dict[str, Any]:
        row = self.get_blueprint(blueprint_id, version)
        if row["status"] == "invalid":
            raise FactoryError("an invalid blueprint cannot be approved", 409,
                               row["report"]["errors"] if row["report"] else None)
        bp = Blueprint.model_validate(row["blueprint"])
        tools = sorted(set(approved_tools or []))
        unknown = [t for t in tools if t not in bp.capabilities.tools]
        if unknown:
            raise FactoryError(f"cannot approve tools the blueprint does not use: {unknown}", 422)
        decision = "approved" if approve else "rejected"
        self.store.add_approval(blueprint_id=blueprint_id, version=row["version"],
                                decision=decision, approver=approver, approved_tools=tools,
                                comment=comment, blueprint_hash=row["hash"])
        self.store.set_blueprint_status(blueprint_id, row["version"], decision)
        self.store.audit(f"blueprint_{decision}", blueprint_id,
                         {"version": row["version"], "hash": row["hash"],
                          "approved_tools": tools, "comment": comment}, approver)
        return self.get_blueprint(blueprint_id, row["version"])

    # ------------------------------------------------------------------ runs

    def start_run(self, blueprint_id: str, inputs: dict[str, Any], *, version: int | None = None,
                  run_id: str | None = None, background: bool = True,
                  actor: str = "user") -> dict[str, Any]:
        row = self.get_blueprint(blueprint_id, version)
        bp = Blueprint.model_validate(row["blueprint"]) if row["blueprint"] else None
        if bp is None or row["status"] == "invalid":
            raise FactoryError("blueprint is invalid and cannot run", 409)
        if bp.policies.require_human_approval and row["status"] != "approved":
            raise FactoryError(f"blueprint {blueprint_id} v{row['version']} is "
                               f"{row['status']}; human approval is required before running", 409)
        if row["approval"] and row["approval"]["blueprint_hash"] != bp.content_hash():
            raise FactoryError("blueprint changed after approval; re-approval required", 409)
        problems = check_run_inputs(bp, inputs)
        if problems:
            raise FactoryError("run inputs are incomplete", 422,
                               [p.model_dump() for p in problems])

        if run_id:
            existing = self.store.get_run(run_id)
            if not existing or existing["status"] != "pending":
                raise FactoryError(f"run {run_id!r} is not a pending run", 409)
            if existing["blueprint_id"] != blueprint_id:
                raise FactoryError(f"run {run_id!r} belongs to another blueprint", 409)
            self.store.update_run(run_id, status="running", inputs=inputs)
        else:
            run_id = new_id("run")
            self.store.add_run(run_id, blueprint_id, row["version"], "running", inputs)
        approved_tools = set((row["approval"] or {}).get("approved_tools") or [])
        self.store.audit("run_started", run_id, {"blueprint_id": blueprint_id,
                                                 "version": row["version"],
                                                 "blueprint_hash": bp.content_hash(),
                                                 "approved_tools": sorted(approved_tools)}, actor)

        def work() -> None:
            tracer = Tracer(run_id, sink=self.store.add_event)
            try:
                result = execute(bp, run_id=run_id, inputs=inputs,
                                 model=self._model_factory(bp.capabilities.model),
                                 tools=self.tools, skills=self.skills,
                                 workspace=self.settings.workspace_for(run_id), tracer=tracer,
                                 approved_tools=approved_tools, checkpointer=self.checkpointer)
                payload = result.model_dump(mode="json")
                self.store.update_run(run_id, status=result.status,
                                      disposition=result.disposition, result=payload)
            except Exception as e:  # last-resort guard: never leave a run "running"
                tracer.emit("run_finished", status="failed", error=f"internal error: {e}")
                payload = {"error": f"internal error: {e}"}
                self.store.update_run(run_id, status="failed", disposition="failed",
                                      result=payload)
            for ev in tracer.events:
                if ev.type.startswith("audit_"):
                    self.store.audit(ev.type.removeprefix("audit_"), run_id, ev.data)
            self.store.audit("run_finished", run_id, {"status": payload.get("status", "failed"),
                                                      "disposition": payload.get("disposition"),
                                                      "usage": payload.get("usage")})

        if background:
            thread = threading.Thread(target=work, name=run_id, daemon=True)
            self._threads[run_id] = thread
            thread.start()
        else:
            work()
        return self.get_run(run_id)

    def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        thread = self._threads.get(run_id)
        if thread:
            thread.join(timeout)
        return self.get_run(run_id)

    def get_run(self, run_id: str, events_after: int = -1) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise FactoryError(f"run {run_id!r} not found", 404)
        events = self.store.events(run_id, events_after)
        graph = next((e["data"].get("graph") for e in self.store.events(run_id)
                      if e["type"] == "run_started"), None)
        return {**run, "events": events, "graph": graph}

    def list_runs(self) -> list[dict[str, Any]]:
        return self.store.list_runs()

    def export_markdown(self, run_id: str) -> str:
        run = self.get_run(run_id)
        result = run.get("result") or {}
        lines = [f"# Run {run_id}", "",
                 f"- Blueprint: {run['blueprint_id']} v{run['version']}",
                 f"- Status: {run['status']} · disposition: {run['disposition']}", ""]
        if result.get("error"):
            lines += [f"**Error:** {result['error']}", ""]
        for loop in (result.get("quality") or {}).get("loops", []):
            verdict = "approved" if loop["approved"] else "NOT approved"
            lines.append(f"- Review `{loop['loop']}`: {verdict} after {loop['attempts']} "
                         f"check(s); unmet: {', '.join(loop['unmet_criteria']) or 'none'}")
        if note := (result.get("quality") or {}).get("note"):
            lines += ["", f"> {note}"]
        lines += ["", "## Final output", ""]
        for key, value in (result.get("final_output") or {}).items():
            lines.append(f"### {key}")
            lines.append("\n".join(f"- {v}" for v in value) if isinstance(value, list)
                         else str(value))
            lines.append("")
        return "\n".join(lines)


def _title_of(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip().lstrip("#").strip()[:120]
    return "Untitled PRD"
