"""Dynamic LangGraph compiler (FR-06, PRD §6, §8.3).

A validated Blueprint becomes a ``StateGraph`` with no case-specific code:

* agent / tool / checker tasks → one node each, named by task id
* hub tasks → ``<id>__plan`` → ``Send`` fan-out to ``<id>__worker`` → ``<id>`` (consolidate)
* maker–checker loops → checker routes to the maker (revise) or ``<loop>__exit``
* a task with several dependencies waits on all of them (join barrier)

Each task writes only ``results[<task id>]``, so parallel branches never conflict.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field, create_model

from .capabilities import SkillCatalog, ToolContext, ToolRegistry
from .contracts import Blueprint, InputRef, LoopSpec, TaskSpec, build_model
from .models import MeteredModel, ModelError, ModelRefusal, to_prompt_json
from .trace import Tracer


class MissingContext(Exception):
    """A mandatory input is absent at runtime; the run stops instead of fabricating it."""


class RunTimeout(Exception):
    pass


def _merge(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


def _merge_nested(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = {**out.get(k, {}), **v}
    return out


class RunState(TypedDict, total=False):
    inputs: dict[str, Any]
    results: Annotated[dict[str, Any], _merge]
    reviews: Annotated[dict[str, Any], _merge]
    plans: Annotated[dict[str, Any], _merge]
    delegations: Annotated[dict[str, Any], _merge_nested]


@dataclass
class ExecutionContext:
    blueprint: Blueprint
    model: MeteredModel
    tools: ToolRegistry
    skills: SkillCatalog
    tracer: Tracer
    tool_ctx: ToolContext
    deadline: float = field(default_factory=lambda: float("inf"))


SYSTEM_RULES = (
    "You are one node in a governed multi-agent workflow.\n"
    "Rules:\n"
    "- Use only the inputs provided and well-established general knowledge.\n"
    "- Never fabricate data, figures, quotes or sources. If an input is insufficient, say so "
    "explicitly in your output.\n"
    "- Respond only with the requested structured output."
)


def _system(role: str, skill_text: str | None) -> str:
    parts = [f"Role: {role}"]
    if skill_text:
        parts.append(f"Skill guidance: {skill_text}")
    parts.append(SYSTEM_RULES)
    return "\n\n".join(parts)


def _schema_text(model: type[BaseModel]) -> str:
    lines = []
    for name, f in model.model_fields.items():
        req = "required" if f.is_required() else "optional"
        lines.append(f"- {name} ({req}): {f.description or ''}".rstrip())
    return "\n".join(lines)


# --------------------------------------------------------------------------- compiler


class GraphCompiler:
    def __init__(self, ctx: ExecutionContext) -> None:
        self.ctx = ctx
        self.bp = ctx.blueprint
        self.loops_by_maker: dict[str, LoopSpec] = {lp.maker: lp for lp in self.bp.execution.loops}
        self.loops_by_checker: dict[str, LoopSpec] = {
            lp.checker: lp for lp in self.bp.execution.loops
        }

    # naming
    def entry(self, t: TaskSpec) -> str:
        return f"{t.id}__plan" if t.kind == "hub" else t.id

    def exit(self, t: TaskSpec, for_target: TaskSpec | None = None) -> str:
        lp = self.loops_by_maker.get(t.id)
        if lp and not (for_target and for_target.id == lp.checker):
            return f"{lp.id}__exit"
        return t.id

    def compile(self, checkpointer: Any = None):
        g = StateGraph(RunState)
        tasks = {t.id: t for t in self.bp.tasks}
        for t in self.bp.tasks:
            if t.kind == "hub":
                g.add_node(f"{t.id}__plan", self._wrap(t, f"{t.id}__plan", self._hub_plan))
                g.add_node(f"{t.id}__worker", self._wrap(t, f"{t.id}__worker", self._hub_worker))
                g.add_node(t.id, self._wrap(t, t.id, self._hub_consolidate))
                g.add_conditional_edges(f"{t.id}__plan", self._dispatch(t),
                                        [f"{t.id}__worker", t.id])
                g.add_edge(f"{t.id}__worker", t.id)
            elif t.kind == "tool":
                g.add_node(t.id, self._wrap(t, t.id, self._tool))
            elif t.kind == "checker":
                g.add_node(t.id, self._wrap(t, t.id, self._checker))
            else:
                g.add_node(t.id, self._wrap(t, t.id, self._agent))

        for lp in self.bp.execution.loops:
            exit_node = f"{lp.id}__exit"
            g.add_node(exit_node, self._loop_exit(lp))
            g.add_conditional_edges(lp.checker, self._route(lp),
                                    {"revise": self.entry(tasks[lp.maker]), "done": exit_node})

        has_outgoing: set[str] = set()
        for t in self.bp.tasks:
            if not t.dependencies:
                g.add_edge(START, self.entry(t))
                continue
            sources = [self.exit(tasks[d], for_target=t) for d in t.dependencies]
            has_outgoing.update(sources)
            g.add_edge(sources[0] if len(sources) == 1 else sources, self.entry(t))

        terminal = [t.id for t in self.bp.tasks if t.kind != "checker"]
        terminal += [f"{lp.id}__exit" for lp in self.bp.execution.loops]
        for node in terminal:
            if node in self.loops_by_maker:  # makers exit through their loop
                continue
            if node not in has_outgoing:
                g.add_edge(node, END)
        return g.compile(checkpointer=checkpointer)

    # ------------------------------------------------------------------ node plumbing

    def _wrap(self, t: TaskSpec, node: str, fn):
        ctx = self.ctx

        def run(state: RunState) -> dict[str, Any]:
            if time.monotonic() > ctx.deadline:
                raise RunTimeout(f"run exceeded {ctx.blueprint.policies.timeout_seconds}s")
            attempt = self._attempt(t, state)
            ctx.tracer.emit("node_started", node, task=t.id, kind=t.kind, attempt=attempt)
            started = time.monotonic()
            retries = ctx.blueprint.policies.max_retries
            for i in range(retries + 1):
                try:
                    update = fn(t, state)
                    break
                except ModelRefusal as e:
                    self._fail(node, t, e)
                    raise
                except ModelError as e:
                    if i == retries:
                        self._fail(node, t, e)
                        raise
                    ctx.tracer.emit("node_retry", node, task=t.id, error=str(e), retry=i + 1)
                except Exception as e:
                    self._fail(node, t, e)
                    raise
            ctx.tracer.emit("node_succeeded", node, task=t.id, attempt=attempt,
                            seconds=round(time.monotonic() - started, 3),
                            output=self._output_of(t, node, update))
            return update

        run.__name__ = node
        return run

    def _fail(self, node: str, t: TaskSpec, e: Exception) -> None:
        self.ctx.tracer.emit("node_failed", node, task=t.id, error=str(e),
                             error_type=type(e).__name__)

    @staticmethod
    def _output_of(t: TaskSpec, node: str, update: dict[str, Any]) -> Any:
        if node == t.id:
            return update.get("results", {}).get(t.id)
        return update.get("plans") or update.get("delegations")

    def _attempt(self, t: TaskSpec, state: RunState) -> int:
        lp = self.loops_by_maker.get(t.id) or self.loops_by_checker.get(t.id)
        if not lp:
            return 1
        # attempts counts completed checks, so the maker's draft and the check of it share n+1
        return state.get("reviews", {}).get(lp.id, {}).get("attempts", 0) + 1

    def _resolve(self, t: TaskSpec, state: RunState) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for ref, required in [(r, True) for r in t.required_inputs] + [
            (r, False) for r in t.optional_inputs
        ]:
            value = _lookup(ref, state)
            if value is None and required:
                raise MissingContext(
                    f"task {t.id!r} requires {ref.source!r} ({ref.name}) but it is missing")
            values[ref.name] = value
        lp = self.loops_by_maker.get(t.id)
        if lp:
            history = state.get("reviews", {}).get(lp.id, {}).get("history", [])
            if history:
                values["previous_draft"] = state.get("results", {}).get(t.id)
                values["review_feedback"] = history[-1]
        return values

    def _generate(self, t: TaskSpec, schema: type[BaseModel], instruction: str,
                  inputs: dict[str, Any], purpose: str, role: str | None = None) -> dict:
        skill = self.ctx.skills.get(t.skill).instructions if t.skill else None
        prompt = (
            f"Overall objective: {self.bp.objective}\n\n"
            f"Your task: {instruction}\n\n"
            f"Inputs (JSON):\n{to_prompt_json(inputs)}\n\n"
            f"Produce these output fields:\n{_schema_text(schema)}"
        )
        out = self.ctx.model.generate(system=_system(role or t.role, skill), prompt=prompt,
                                      schema=schema, purpose=purpose)
        return out.model_dump()

    # ------------------------------------------------------------------ node kinds

    def _agent(self, t: TaskSpec, state: RunState) -> dict[str, Any]:
        inputs = self._resolve(t, state)
        instruction = t.responsibility
        if "review_feedback" in inputs:
            instruction += ("\nThis is a revision: address every point in review_feedback "
                            "and improve previous_draft.")
        schema = build_model(f"{t.id}_output", t.output_schema)
        out = self._generate(t, schema, instruction, inputs, purpose=f"agent:{t.id}")
        return {"results": {t.id: out}}

    def _tool(self, t: TaskSpec, state: RunState) -> dict[str, Any]:
        args = {**t.tool_args, **self._resolve(t, state)}
        self.ctx.tracer.emit("tool_call", t.id, tool=t.tool, args=sorted(args))
        raw = self.ctx.tools.invoke(t.tool, args, self.ctx.tool_ctx)
        schema = build_model(f"{t.id}_output", t.output_schema)
        out = schema.model_validate({f.name: raw.get(f.name) for f in t.output_schema})
        return {"results": {t.id: out.model_dump()}}

    def _checker(self, t: TaskSpec, state: RunState) -> dict[str, Any]:
        lp = self.loops_by_checker[t.id]
        inputs = self._resolve(t, state)
        inputs["rubric"] = lp.rubric
        schema = build_model(f"{t.id}_output", t.output_schema)
        verdict = self._generate(
            t, schema,
            f"{t.responsibility}\nEvaluate the artifact against every rubric item. "
            "Set approved=true only if all items are met.",
            inputs, purpose=f"check:{t.id}",
        )
        review = dict(state.get("reviews", {}).get(lp.id, {"attempts": 0, "history": []}))
        review["attempts"] = review.get("attempts", 0) + 1
        review["history"] = [*review.get("history", []), verdict]
        review["approved"] = bool(verdict.get("approved"))
        self.ctx.tracer.emit("review_verdict", t.id, loop=lp.id, attempt=review["attempts"],
                             approved=review["approved"],
                             unmet_criteria=verdict.get("unmet_criteria", []))
        return {"results": {t.id: verdict}, "reviews": {lp.id: review}}

    def _route(self, lp: LoopSpec):
        tracer = self.ctx.tracer

        def route(state: RunState) -> Literal["revise", "done"]:
            review = state.get("reviews", {}).get(lp.id, {})
            if review.get("approved"):
                return "done"
            if review.get("attempts", 0) > lp.max_revisions:
                tracer.emit("loop_limit_reached", lp.checker, loop=lp.id,
                            attempts=review.get("attempts"), max_revisions=lp.max_revisions)
                return "done"
            tracer.emit("revision_requested", lp.checker, loop=lp.id,
                        attempt=review.get("attempts"))
            return "revise"

        return route

    def _loop_exit(self, lp: LoopSpec):
        tracer = self.ctx.tracer

        def loop_exit(state: RunState) -> dict[str, Any]:
            review = state.get("reviews", {}).get(lp.id, {})
            tracer.emit("node_started", f"{lp.id}__exit", task=lp.maker, kind="loop_exit")
            tracer.emit("loop_exit", f"{lp.id}__exit", loop=lp.id,
                        approved=bool(review.get("approved")), attempts=review.get("attempts"))
            return {}

        loop_exit.__name__ = f"{lp.id}__exit"
        return loop_exit

    # ------------------------------------------------------------------ hub + subagent

    def _plan_schema(self, t: TaskSpec) -> type[BaseModel]:
        ids = tuple(s.id for s in t.subagents)
        item = create_model(
            f"{t.id}_delegation",
            subagent=(Literal[ids], Field(description="which specialist to use")),
            instruction=(str, Field(description="scoped sub-task for that specialist")),
        )
        return create_model(f"{t.id}_plan",
                            delegations=(list[item], Field(description="delegated sub-tasks")))

    def _hub_plan(self, t: TaskSpec, state: RunState) -> dict[str, Any]:
        inputs = self._resolve(t, state)
        roster = [{"id": s.id, "role": s.role, "responsibility": s.responsibility}
                  for s in t.subagents]
        out = self._generate(
            t, self._plan_schema(t),
            f"{t.responsibility}\nDelegate scoped sub-tasks to these specialists "
            f"(at most {t.max_delegations}): {to_prompt_json(roster)}",
            inputs, purpose=f"plan:{t.id}", role=f"{t.role} (coordinator)",
        )
        items = out["delegations"]
        if len(items) > t.max_delegations:
            self.ctx.tracer.emit("delegation_capped", f"{t.id}__plan", requested=len(items),
                                 cap=t.max_delegations)
            items = items[: t.max_delegations]
        attempt = self._attempt(t, state)
        plan = {"attempt": attempt, "inputs": inputs,
                "items": [{**it, "key": f"a{attempt}_{i}_{it['subagent']}"}
                          for i, it in enumerate(items)]}
        return {"plans": {t.id: plan}}

    def _dispatch(self, t: TaskSpec):
        def dispatch(state: RunState):
            plan = state.get("plans", {}).get(t.id, {})
            sends = [Send(f"{t.id}__worker", {"hub": t.id, "item": it, "inputs": plan["inputs"]})
                     for it in plan.get("items", [])]
            return sends or t.id

        return dispatch

    def _hub_worker(self, t: TaskSpec, payload: dict[str, Any]) -> dict[str, Any]:
        item = payload["item"]
        sub = next(s for s in t.subagents if s.id == item["subagent"])
        schema = build_model(f"{t.id}_{sub.id}_output", sub.output_schema)
        out = self._generate(t, schema, f"{sub.responsibility}\nAssignment: {item['instruction']}",
                             payload["inputs"], purpose=f"subagent:{t.id}.{sub.id}", role=sub.role)
        self.ctx.tracer.emit("delegation_result", f"{t.id}__worker", subagent=sub.id,
                             key=item["key"], output=out)
        return {"delegations": {t.id: {item["key"]: {"subagent": sub.id, **out}}}}

    def _hub_consolidate(self, t: TaskSpec, state: RunState) -> dict[str, Any]:
        inputs = self._resolve(t, state)
        plan = state.get("plans", {}).get(t.id, {})
        prefix = f"a{plan.get('attempt', 1)}_"
        inputs["delegated_findings"] = [
            v for k, v in sorted(state.get("delegations", {}).get(t.id, {}).items())
            if k.startswith(prefix)
        ]
        schema = build_model(f"{t.id}_output", t.output_schema)
        out = self._generate(t, schema,
                             f"{t.responsibility}\nConsolidate the delegated findings.",
                             inputs, purpose=f"agent:{t.id}")
        return {"results": {t.id: out}}


def _lookup(ref: InputRef, state: RunState) -> Any:
    if ref.kind == "run":
        return state.get("inputs", {}).get(ref.target)
    produced = state.get("results", {}).get(ref.target)
    if produced is None or ref.field is None:
        return produced
    return produced.get(ref.field)


def graph_structure(compiled) -> dict[str, list]:
    """Nodes/edges of the compiled LangGraph, for display and trace cross-checks."""
    g = compiled.get_graph()
    return {
        "nodes": sorted(g.nodes),
        "edges": sorted([e.source, e.target, bool(e.conditional)] for e in g.edges),
    }
