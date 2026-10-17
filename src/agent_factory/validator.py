"""Blueprint validation before execution (FR-05).

Every check is structural and deterministic: a blueprint that passes can be
compiled, and every required input is provably produced before it is read.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from .capabilities import SkillCatalog, ToolRegistry
from .contracts import Blueprint, InputRef, TaskSpec


class Issue(BaseModel):
    code: str
    message: str
    task: str | None = None


class ValidationReport(BaseModel):
    errors: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    approvals_required: list[str] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {e.code for e in self.errors}

    def summary(self) -> str:
        return "; ".join(f"[{e.code}] {e.message}" for e in self.errors) or "valid"


def estimated_steps(bp: Blueprint) -> int:
    """Upper bound on LangGraph supersteps the compiled graph can take."""
    steps = len(bp.tasks) + 2
    steps += sum(2 for t in bp.tasks if t.kind == "hub")
    steps += sum(2 * (lp.max_revisions + 1) + 1 for lp in bp.execution.loops)
    return steps


def validate_blueprint(
    bp: Blueprint,
    tools: ToolRegistry,
    skills: SkillCatalog,
    run_inputs: dict[str, Any] | None = None,
) -> ValidationReport:
    r = ValidationReport()

    def err(code: str, msg: str, task: str | None = None) -> None:
        r.errors.append(Issue(code=code, message=msg, task=task))

    def warn(code: str, msg: str, task: str | None = None) -> None:
        r.warnings.append(Issue(code=code, message=msg, task=task))

    # ---- identity
    ids = [t.id for t in bp.tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    for d in dupes:
        err("duplicate_task", f"task id {d!r} is defined more than once", d)
    tasks: dict[str, TaskSpec] = {t.id: t for t in bp.tasks}
    for t in bp.tasks:
        if "__" in t.id:
            err("reserved_id", f"task id {t.id!r} must not contain '__'", t.id)

    # ---- dependencies
    for t in bp.tasks:
        for d in t.dependencies:
            if d == t.id:
                err("self_dependency", f"task {t.id!r} depends on itself", t.id)
            elif d not in tasks:
                err("unresolved_dependency", f"task {t.id!r} depends on unknown task {d!r}", t.id)

    # ---- loops (the only sanctioned cycles)
    back_edges: set[tuple[str, str]] = set()
    makers_seen: set[str] = set()
    for lp in bp.execution.loops:
        maker, checker = tasks.get(lp.maker), tasks.get(lp.checker)
        if maker is None or checker is None:
            err("loop_unknown_task", f"loop {lp.id!r} references unknown maker/checker")
            continue
        if checker.kind != "checker":
            err("loop_checker_kind", f"loop {lp.id!r}: {checker.id!r} must have kind 'checker'",
                checker.id)
        if maker.kind not in ("agent", "hub"):
            err("loop_maker_kind", f"loop {lp.id!r}: maker must be an agent or hub", maker.id)
        if checker.dependencies != [maker.id]:
            err("loop_wiring", f"loop {lp.id!r}: checker must depend on the maker only",
                checker.id)
        if maker.id in makers_seen:
            err("loop_overlap", f"maker {maker.id!r} is in more than one loop", maker.id)
        makers_seen.add(maker.id)
        out = {f.name: f for f in checker.output_schema}
        if out.get("approved") is None or out["approved"].type != "boolean":
            err("checker_schema", f"checker {checker.id!r} must output boolean 'approved'",
                checker.id)
        if "feedback" not in out:
            err("checker_schema", f"checker {checker.id!r} must output 'feedback'", checker.id)
        back_edges.add((checker.id, maker.id))
        others = [t.id for t in bp.tasks if checker.id in t.dependencies]
        if others:
            err("loop_wiring",
                f"tasks {others} depend on checker {checker.id!r}; depend on the maker instead",
                checker.id)
    looped_checkers = {lp.checker for lp in bp.execution.loops}
    for t in bp.tasks:
        if t.kind == "checker" and t.id not in looped_checkers:
            err("orphan_checker", f"checker {t.id!r} is not part of a declared loop", t.id)

    # ---- edges must mirror dependencies (plus declared loop back-edges)
    dep_edges = {(d, t.id) for t in bp.tasks for d in t.dependencies if d in tasks}
    declared = set()
    for e in bp.execution.edges:
        if e.source not in tasks or e.target not in tasks:
            err("edge_unknown_task", f"edge {e.source}->{e.target} references an unknown task")
            continue
        declared.add((e.source, e.target))
    undeclared_cycle_edges = (declared - dep_edges) - back_edges
    for s, t in sorted(undeclared_cycle_edges):
        err("edge_without_dependency", f"edge {s}->{t} has no matching dependency", t)
    for s, t in sorted(dep_edges - declared):
        err("dependency_without_edge", f"dependency {s}->{t} has no matching edge", t)

    # ---- acyclicity (outside declared loops)
    cycle = _find_cycle(tasks, (declared - back_edges) | dep_edges)
    if cycle:
        err("cycle", f"undeclared cycle: {' -> '.join(cycle)}")

    ancestors = _ancestors(tasks) if not cycle else {t: set() for t in tasks}

    # ---- context contracts
    run_decl = {i.name: i for i in bp.inputs}
    for t in bp.tasks:
        names = [ref.name for ref in t.required_inputs + t.optional_inputs]
        for n in sorted({n for n in names if names.count(n) > 1}):
            err("duplicate_input", f"task {t.id!r} declares input {n!r} twice", t.id)
        for ref, required in [(x, True) for x in t.required_inputs] + [
            (x, False) for x in t.optional_inputs
        ]:
            _check_ref(bp, t, ref, required, tasks, ancestors, run_decl, err)

    # ---- kinds, tools, skills
    permitted = set(bp.capabilities.tools)
    for tool_id in sorted(permitted):
        if tool_id not in tools:
            err("unknown_tool", f"tool {tool_id!r} is not in the trusted registry")
            continue
        spec = tools.get(tool_id)
        allowed = bp.capabilities.allowed_operations
        if spec.side_effect != "none" and spec.side_effect not in allowed:
            err("operation_not_allowed",
                f"tool {tool_id!r} needs '{spec.side_effect}' which is not in allowed_operations")
        if spec.requires_approval:
            r.approvals_required.append(tool_id)
    for t in bp.tasks:
        if t.kind == "tool":
            if not t.tool:
                err("tool_missing", f"tool task {t.id!r} names no tool", t.id)
            elif t.tool not in tools:
                err("unknown_tool", f"task {t.id!r} uses unregistered tool {t.tool!r}", t.id)
            elif t.tool not in permitted:
                err("tool_not_permitted", f"task {t.id!r} uses {t.tool!r} not in capabilities",
                    t.id)
            else:
                spec = tools.get(t.tool)
                provided = {ref.name for ref in t.required_inputs} | set(t.tool_args)
                missing = [f.name for f in spec.input_schema
                           if f.required and f.name not in provided]
                if missing:
                    err("tool_input_unmapped", f"task {t.id!r} does not supply {missing}", t.id)
                tool_out = {f.name: f.type for f in spec.output_schema}
                bad = [f.name for f in t.output_schema if tool_out.get(f.name) != f.type]
                if bad:
                    err("tool_output_mismatch", f"task {t.id!r} expects {bad} which {t.tool!r} "
                        "does not produce with that type", t.id)
        elif t.tool:
            err("agent_tool_use", f"task {t.id!r} ({t.kind}) may not call tools directly; "
                "use a tool task", t.id)
        if t.kind != "tool":
            schemas = [t.output_schema] + [s.output_schema for s in t.subagents]
            if any(f.type == "object" for fields in schemas for f in fields):
                err("object_field_on_model_output",
                    f"task {t.id!r}: model outputs must use typed fields, not 'object'", t.id)
        if t.kind == "hub":
            if not t.subagents:
                err("hub_without_subagents", f"hub task {t.id!r} declares no subagents", t.id)
            sub_ids = [s.id for s in t.subagents]
            if len(sub_ids) != len(set(sub_ids)):
                err("duplicate_subagent", f"hub task {t.id!r} repeats a subagent id", t.id)
        elif t.subagents:
            err("subagents_on_non_hub", f"task {t.id!r} declares subagents but is not a hub", t.id)
        if t.skill is not None:
            if t.skill not in skills:
                err("unknown_skill", f"task {t.id!r} uses unknown skill {t.skill!r}", t.id)
            elif t.skill not in bp.capabilities.skills:
                err("skill_not_permitted", f"skill {t.skill!r} not listed in capabilities", t.id)

    # ---- patterns & quality
    for p in bp.execution.patterns:
        unknown = [x for x in p.tasks if x not in tasks]
        if unknown:
            err("pattern_unknown_task", f"pattern {p.pattern} references {unknown}")
    names = set(bp.execution.pattern_names)
    if bool(bp.execution.loops) != ("maker_checker" in names):
        err("pattern_mismatch", "maker_checker pattern must be declared iff loops exist")
    if any(t.kind == "hub" for t in bp.tasks) != ("hub_subagent" in names):
        err("pattern_mismatch", "hub_subagent pattern must be declared iff hub tasks exist")
    if bp.quality.final_task not in tasks:
        err("final_task_unknown", f"final task {bp.quality.final_task!r} does not exist")
    elif tasks[bp.quality.final_task].kind == "checker":
        err("final_task_checker", "final task must be the artifact producer, not the checker")
    if not bp.quality.criteria:
        warn("no_quality_criteria", "no measurable quality criteria declared")

    # ---- policies
    needed = estimated_steps(bp)
    if needed > bp.policies.max_steps:
        err("step_budget", f"graph may need {needed} steps but max_steps is "
            f"{bp.policies.max_steps}")

    # ---- mandatory run data (when inputs are supplied for a run)
    if run_inputs is not None:
        r.errors.extend(check_run_inputs(bp, run_inputs))
    return r


def check_run_inputs(bp: Blueprint, run_inputs: dict[str, Any]) -> list[Issue]:
    issues = []
    for i in bp.inputs:
        value = run_inputs.get(i.name)
        if i.required and (value is None or (isinstance(value, str) and not value.strip())):
            issues.append(Issue(code="missing_run_input",
                                message=f"required input {i.name!r} is missing: {i.description}"))
    undeclared = sorted(set(run_inputs) - {i.name for i in bp.inputs})
    for name in undeclared:
        issues.append(Issue(code="undeclared_run_input", message=f"input {name!r} is not declared"))
    return issues


def _check_ref(bp, t, ref: InputRef, required, tasks, ancestors, run_decl, err) -> None:
    if ref.kind == "run":
        decl = run_decl.get(ref.target)
        if decl is None:
            err("undeclared_run_input", f"task {t.id!r} reads undeclared input {ref.target!r}",
                t.id)
        elif required and not decl.required:
            err("required_from_optional",
                f"task {t.id!r} requires {ref.source!r} but that run input is optional", t.id)
        return
    src = tasks.get(ref.target)
    if src is None:
        err("unresolved_input", f"task {t.id!r} reads from unknown task {ref.target!r}", t.id)
        return
    if ref.target not in ancestors.get(t.id, set()):
        err("input_not_upstream",
            f"task {t.id!r} reads {ref.source!r} but {ref.target!r} is not upstream", t.id)
    if ref.field is not None:
        fields = {f.name: f for f in src.output_schema}
        if ref.field not in fields:
            err("unresolved_input", f"task {t.id!r} reads missing field {ref.source!r}", t.id)
        elif required and not fields[ref.field].required:
            err("required_from_optional",
                f"task {t.id!r} requires {ref.source!r} but that field is optional", t.id)


def _find_cycle(tasks: dict[str, Any], edges: set[tuple[str, str]]) -> list[str] | None:
    adj: dict[str, list[str]] = defaultdict(list)
    for s, t in sorted(edges):
        adj[s].append(t)
    color: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = 1
        stack.append(n)
        for m in adj[n]:
            if color.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if color.get(m) is None and (found := visit(m)):
                return found
        stack.pop()
        color[n] = 2
        return None

    for n in sorted(tasks):
        if color.get(n) is None and (found := visit(n)):
            return found
    return None


def _ancestors(tasks: dict[str, TaskSpec]) -> dict[str, set[str]]:
    memo: dict[str, set[str]] = {}

    def anc(n: str) -> set[str]:
        if n not in memo:
            memo[n] = set()
            for d in tasks[n].dependencies:
                if d in tasks:
                    memo[n] |= {d} | anc(d)
        return memo[n]

    return {n: anc(n) for n in tasks}
