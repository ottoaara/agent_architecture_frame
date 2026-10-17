"""Execute a validated blueprint and produce a RunResult with a full trace (FR-09, FR-10)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from langgraph.errors import GraphRecursionError

from .capabilities import CapabilityError, SkillCatalog, ToolBlocked, ToolContext, ToolRegistry
from .compiler import ExecutionContext, GraphCompiler, MissingContext, RunTimeout, graph_structure
from .contracts import Blueprint, RunResult, utcnow
from .models import BudgetExceeded, MeteredModel, ModelClient, ModelError
from .trace import Tracer
from .validator import validate_blueprint


class BlueprintInvalid(Exception):
    def __init__(self, report) -> None:
        super().__init__(report.summary())
        self.report = report


def execute(
    bp: Blueprint,
    *,
    run_id: str,
    inputs: dict[str, Any],
    model: ModelClient,
    tools: ToolRegistry,
    skills: SkillCatalog,
    workspace: Path,
    tracer: Tracer | None = None,
    approved_tools: set[str] | None = None,
    checkpointer: Any = None,
) -> RunResult:
    tracer = tracer or Tracer(run_id)
    result = RunResult(run_id=run_id, blueprint_id=bp.blueprint_id,
                       blueprint_version=bp.version, blueprint_hash=bp.content_hash(),
                       status="running", started_at=utcnow())

    report = validate_blueprint(bp, tools, skills, run_inputs=inputs)
    if not report.valid:
        tracer.emit("run_rejected", errors=[e.model_dump() for e in report.errors])
        result.status, result.disposition = "failed", "failed"
        result.error = f"blueprint validation failed: {report.summary()}"
        result.finished_at = utcnow()
        return result

    workspace.mkdir(parents=True, exist_ok=True)
    meter = MeteredModel(model, bp.policies, on_call=lambda purpose, gen: tracer.emit(
        "model_call", purpose=purpose, model=gen.model, input_tokens=gen.input_tokens,
        output_tokens=gen.output_tokens))
    tool_ctx = ToolContext(run_id=run_id, workspace=workspace,
                           permitted_tools=set(bp.capabilities.tools),
                           approved_tools=set(approved_tools or ()),
                           audit=lambda type_, data: tracer.emit(f"audit_{type_}", **data))
    ctx = ExecutionContext(blueprint=bp, model=meter, tools=tools, skills=skills, tracer=tracer,
                           tool_ctx=tool_ctx,
                           deadline=time.monotonic() + bp.policies.timeout_seconds)
    compiled = GraphCompiler(ctx).compile(checkpointer=checkpointer)
    tracer.emit("run_started", blueprint_id=bp.blueprint_id, version=bp.version,
                blueprint_hash=result.blueprint_hash, patterns=bp.execution.pattern_names,
                graph=graph_structure(compiled), model=model.name)

    config = {
        "configurable": {"thread_id": run_id},
        "recursion_limit": bp.policies.max_steps,
        "max_concurrency": bp.execution.concurrency_cap,
    }
    state: dict[str, Any] = {}
    try:
        state = compiled.invoke({"inputs": inputs, "results": {}, "reviews": {}}, config)
    except ToolBlocked as e:
        _stop(result, "blocked", "blocked", f"blocked: {e}")
    except MissingContext as e:
        _stop(result, "failed", "failed", f"missing mandatory context: {e}")
    except BudgetExceeded as e:
        _stop(result, "failed", "failed", f"budget exceeded: {e}")
    except RunTimeout as e:
        _stop(result, "failed", "failed", f"timeout: {e}")
    except GraphRecursionError:
        _stop(result, "failed", "failed", f"step limit of {bp.policies.max_steps} reached")
    except (ModelError, CapabilityError) as e:
        _stop(result, "failed", "failed", f"{type(e).__name__}: {e}")
    else:
        _finish(bp, result, state)

    if result.status != "succeeded" and checkpointer is not None:
        # Recover partial results from the last checkpoint for the trace view.
        snapshot = compiled.get_state(config)
        state = dict(snapshot.values or {})
        result.results = state.get("results", {})
    result.usage = meter.usage
    result.finished_at = utcnow()
    tracer.emit("run_finished", status=result.status, disposition=result.disposition,
                error=result.error, usage=meter.usage.model_dump())
    return result


def _stop(result: RunResult, status, disposition, error: str) -> None:
    result.status, result.disposition, result.error = status, disposition, error


def _finish(bp: Blueprint, result: RunResult, state: dict[str, Any]) -> None:
    result.results = state.get("results", {})
    result.final_output = result.results.get(bp.quality.final_task)
    reviews = state.get("reviews", {})
    loops = []
    for lp in bp.execution.loops:
        review = reviews.get(lp.id, {})
        last = (review.get("history") or [{}])[-1]
        loops.append({
            "loop": lp.id,
            "maker": lp.maker,
            "approved": bool(review.get("approved")),
            "attempts": review.get("attempts", 0),
            "max_revisions": lp.max_revisions,
            "unmet_criteria": last.get("unmet_criteria", []),
            "score": last.get("score"),
        })
    result.quality = {"criteria": bp.quality.criteria, "loops": loops}
    result.status = "succeeded"
    if not loops:
        result.disposition = "unreviewed"
    elif all(lp["approved"] for lp in loops):
        result.disposition = "met"
    else:
        result.disposition = "not_met"
        result.quality["note"] = ("Best available artifact returned; it did not meet the "
                                  "quality threshold within the revision limit.")
        if bp.quality.on_unmet == "fail":
            result.status = "failed"
            result.error = "quality threshold not met"
