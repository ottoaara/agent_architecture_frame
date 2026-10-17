"""Work Director: analysis → pattern selection → blueprint → validation (FR-02..FR-05).

The model (or template parser) decides *what* work exists. Everything after
that is deterministic: the same analysis always yields the same blueprint.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from .analyzer import Analyzer, snake
from .capabilities import SkillCatalog, ToolRegistry
from .contracts import (
    CHECKER_OUTPUT_SCHEMA,
    AnalyzedTask,
    Blueprint,
    Capabilities,
    Edge,
    ExecutionPlan,
    FieldSpec,
    InputRef,
    LoopSpec,
    ModelConfig,
    PatternUse,
    Policies,
    Quality,
    RequirementsAnalysis,
    RunInput,
    SubagentSpec,
    TaskSpec,
    sha256_text,
)
from .validator import Issue, ValidationReport, estimated_steps, validate_blueprint

DEFAULT_OUTPUT = [FieldSpec(name="summary", description="The task's result")]
DEFAULT_RUBRIC = ["Output fully addresses the task", "Claims are supported by the inputs"]
FINAL_TASK_ID = "final_output"


class DirectorResult(BaseModel):
    prd_hash: str
    analysis: RequirementsAnalysis | None = None
    blueprint: Blueprint | None = None
    report: ValidationReport = Field(default_factory=ValidationReport)
    issues: list[Issue] = Field(default_factory=list)  # blocking: no blueprint generated
    warnings: list[Issue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.blueprint is not None and self.report.valid and not self.issues


# --------------------------------------------------------------------------- normalization


def normalize(analysis: RequirementsAnalysis) -> tuple[RequirementsAnalysis, list[Issue]]:
    """Make ids safe and unique, drop unknown dependencies. Never invents work."""
    issues: list[Issue] = []
    seen: dict[str, int] = {}
    rename: dict[str, str] = {}
    tasks: list[AnalyzedTask] = []
    for t in analysis.tasks:
        base = snake(t.id or t.title)
        if base == FINAL_TASK_ID:
            base = f"{base}_task"
        new = base if base not in seen else f"{base}_{seen[base] + 1}"
        seen[base] = seen.get(base, 0) + 1
        rename.setdefault(t.id, new)
        tasks.append(t.model_copy(update={"id": new}))
    known = {t.id for t in tasks}
    inputs = {snake(i.name): i.model_copy(update={"name": snake(i.name)})
              for i in analysis.run_inputs}
    out = []
    for t in tasks:
        deps = []
        for d in t.depends_on:
            nd = rename.get(d, snake(d))
            if nd == t.id or nd not in known:
                issues.append(Issue(code="dropped_dependency", task=t.id,
                                    message=f"dependency {d!r} of {t.id!r} does not exist"))
            elif nd not in deps:
                deps.append(nd)
        user_inputs = [snake(i) for i in t.user_inputs]
        for ui in user_inputs:
            if ui not in inputs:
                inputs[ui] = RunInput(name=ui, description=f"needed by task {t.id}")
        fields = [f.model_copy(update={"name": snake(f.name)}) for f in t.output_fields]
        out.append(t.model_copy(update={"depends_on": deps, "user_inputs": user_inputs,
                                        "output_fields": fields}))
    normalized = analysis.model_copy(update={"tasks": out, "run_inputs": list(inputs.values())})
    return normalized, issues


# --------------------------------------------------------------------------- pattern selection


def _levels(tasks: list[AnalyzedTask]) -> dict[str, int]:
    by_id = {t.id: t for t in tasks}
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def level(i: str) -> int:
        if i in memo:
            return memo[i]
        if i in visiting:  # cycle: the validator will reject it
            return 0
        visiting.add(i)
        memo[i] = 1 + max((level(d) for d in by_id[i].depends_on), default=-1)
        visiting.discard(i)
        return memo[i]

    return {t.id: level(t.id) for t in tasks}


def select_patterns(analysis: RequirementsAnalysis) -> list[PatternUse]:
    tasks = analysis.tasks
    levels = _levels(tasks)
    patterns: list[PatternUse] = []
    by_level: dict[int, list[str]] = defaultdict(list)
    for t in tasks:
        by_level[levels[t.id]].append(t.id)
    for lvl in sorted(by_level):
        group = by_level[lvl]
        if len(group) > 1:
            joins = sorted({t.id for t in tasks if len(set(t.depends_on) & set(group)) > 1})
            patterns.append(PatternUse(
                pattern="parallel", tasks=group + joins,
                rationale=f"{len(group)} tasks have no dependency on each other; fan out"
                          + (f" and join at {', '.join(joins)}" if joins else "")))
    chained = [t.id for t in tasks if t.depends_on]
    if chained:
        patterns.append(PatternUse(
            pattern="serial", tasks=chained,
            rationale="these tasks consume predecessor outputs; run in dependency order"))
    for t in tasks:
        if t.delegate_to:
            patterns.append(PatternUse(
                pattern="hub_subagent", tasks=[t.id],
                rationale=f"needs specialised delegation: {', '.join(t.delegate_to)}"))
        if t.needs_review:
            patterns.append(PatternUse(
                pattern="maker_checker", tasks=[t.id, f"{t.id}_review"],
                rationale="quality/factual verification with bounded revision"))
    return patterns


# --------------------------------------------------------------------------- blueprint generation


def _skill_for(t: AnalyzedTask) -> str:
    text = f"{t.title} {t.description}".lower()
    if t.delegate_to:
        return "coordinator"
    if len(t.depends_on) > 1:
        return "synthesizer"
    for words, skill in [
        (("validat", "transform", "clean", "normaliz", "extract", "parse"), "data_engineer"),
        (("research", "investigat", "profile"), "researcher"),
        (("write", "report", "draft", "memo", "summar"), "writer"),
    ]:
        if any(w in text for w in words):
            return skill
    return "analyst"


def _agent_fields(fields: list[FieldSpec]) -> list[FieldSpec]:
    fixed = [f.model_copy(update={"type": "string"}) if f.type == "object" else f for f in fields]
    return fixed or list(DEFAULT_OUTPUT)


def generate_blueprint(
    analysis: RequirementsAnalysis,
    prd_hash: str,
    tools: ToolRegistry,
    model: ModelConfig,
    version: int = 1,
    blueprint_id: str | None = None,
) -> Blueprint:
    run_inputs = {i.name: i for i in analysis.run_inputs}
    produced: dict[str, list[FieldSpec]] = {}
    specs: list[TaskSpec] = []
    loops: list[LoopSpec] = []
    used_tools: set[str] = set()

    for t in analysis.tasks:
        required = [InputRef(name=d, source=f"task:{d}") for d in t.depends_on]
        optional: list[InputRef] = []
        for ui in t.user_inputs:
            ref = InputRef(name=ui, source=f"run:{ui}")
            (required if run_inputs[ui].required else optional).append(ref)

        if t.tool:
            used_tools.add(t.tool)
            spec = _tool_task(t, tools, run_inputs, produced)
        else:
            spec = TaskSpec(
                id=t.id, role=t.title, responsibility=t.description,
                kind="hub" if t.delegate_to else "agent",
                required_inputs=required, optional_inputs=optional,
                output_schema=_agent_fields(t.output_fields), dependencies=list(t.depends_on),
                skill=_skill_for(t),
                subagents=[SubagentSpec(
                    id=snake(name), role=f"{name} specialist",
                    responsibility=f"Investigate the '{name}' aspect of: {t.description}",
                ) for name in dict.fromkeys(t.delegate_to)],
                max_delegations=max(1, len(set(t.delegate_to))),
            )
        specs.append(spec)
        produced[t.id] = spec.output_schema

        if t.needs_review:
            cid = f"{t.id}_review"
            specs.append(TaskSpec(
                id=cid, role=f"Reviewer for {t.title}",
                responsibility=f"Review the output of '{t.title}' against the rubric.",
                kind="checker", skill="reviewer", dependencies=[t.id],
                required_inputs=[InputRef(name="artifact", source=f"task:{t.id}")],
                output_schema=list(CHECKER_OUTPUT_SCHEMA),
            ))
            loops.append(LoopSpec(
                id=f"{t.id}_loop", maker=t.id, checker=cid, max_revisions=2,
                rubric=t.review_criteria or analysis.success_criteria or DEFAULT_RUBRIC))

    checkers = {lp.checker for lp in loops}
    depended = {d for s in specs if s.id not in checkers for d in s.dependencies}
    sinks = [s.id for s in specs if s.id not in depended and s.id not in checkers]
    if len(sinks) == 1:
        final = sinks[0]
    else:
        final = FINAL_TASK_ID
        specs.append(TaskSpec(
            id=final, role="Final synthesizer",
            responsibility=f"Combine all results into the final deliverable for: "
                           f"{analysis.objective}",
            dependencies=sinks, required_inputs=[InputRef(name=s, source=f"task:{s}")
                                                 for s in sinks],
            output_schema=[FieldSpec(name="deliverable", description="Final deliverable"),
                           FieldSpec(name="key_points", type="string_list")],
            skill="synthesizer",
        ))

    patterns = select_patterns(analysis)
    if final == FINAL_TASK_ID:
        patterns.append(PatternUse(pattern="parallel" if len(sinks) > 1 else "serial",
                                   tasks=[*sinks, final],
                                   rationale="independent results joined deterministically"))
    edges = [Edge(source=d, target=s.id) for s in specs for d in s.dependencies]
    ops = sorted({tools.get(t).side_effect for t in used_tools if t in tools} - {"none"})
    bp_kwargs: dict[str, Any] = {} if blueprint_id is None else {"blueprint_id": blueprint_id}
    bp = Blueprint(
        **bp_kwargs,
        version=version,
        objective=analysis.objective or "(missing objective)",
        source_prd_hash=prd_hash,
        inputs=list(run_inputs.values()),
        tasks=specs,
        execution=ExecutionPlan(patterns=patterns, edges=edges, loops=loops),
        capabilities=Capabilities(tools=sorted(used_tools),
                                  skills=sorted({s.skill for s in specs if s.skill}),
                                  model=model, allowed_operations=ops),
        quality=Quality(criteria=analysis.success_criteria, final_task=final),
    )
    steps = estimated_steps(bp)
    calls = sum(3 if s.kind == "hub" else 1 for s in specs) + sum(
        len(bp.task(lp.maker).subagents) for lp in loops)
    calls += sum(len(s.subagents) for s in specs)
    calls += sum(2 * lp.max_revisions * (2 + len(bp.task(lp.maker).subagents)) for lp in loops)
    bp.policies = Policies(max_steps=steps + 10, max_model_calls=calls + 5)
    return bp


def _tool_task(t: AnalyzedTask, tools: ToolRegistry, run_inputs: dict[str, RunInput],
               produced: dict[str, list[FieldSpec]]) -> TaskSpec:
    """Map a tool's declared inputs onto available run inputs / upstream fields."""
    if t.tool not in tools:  # keep it: validation will escalate the unknown tool
        return TaskSpec(id=t.id, role=t.title, responsibility=t.description, kind="tool",
                        tool=t.tool, dependencies=list(t.depends_on),
                        output_schema=_agent_fields(t.output_fields))
    spec = tools.get(t.tool)
    refs: list[InputRef] = []
    for f in spec.input_schema:
        if not f.required:
            continue
        source = None
        if f.name in t.user_inputs:
            source = f"run:{f.name}"
        else:
            for d in t.depends_on:
                if any(o.name == f.name for o in produced.get(d, [])):
                    source = f"task:{d}.{f.name}"
                    break
            if source is None and len(t.user_inputs) == 1 and not t.depends_on:
                source = f"run:{t.user_inputs[0]}"
            if source is None and len(t.depends_on) == 1 and not t.user_inputs:
                strings = [o for o in produced.get(t.depends_on[0], []) if o.type == "string"]
                if len(strings) == 1:
                    source = f"task:{t.depends_on[0]}.{strings[0].name}"
        if source:
            refs.append(InputRef(name=f.name, source=source))
    return TaskSpec(id=t.id, role=t.title, responsibility=t.description, kind="tool",
                    tool=t.tool, dependencies=list(t.depends_on), required_inputs=refs,
                    output_schema=[f.model_copy() for f in spec.output_schema])


# --------------------------------------------------------------------------- director graph


BLOCKING = {"missing_objective", "no_tasks"}
MAX_REPAIRS = 2  # bounded re-analysis when a model-built blueprint fails validation


class DirectorState(TypedDict, total=False):
    prd_text: str
    analysis: RequirementsAnalysis
    issues: list[Issue]
    blueprint: Blueprint
    report: ValidationReport
    repairs: int


class WorkDirector:
    def __init__(self, analyzer: Analyzer, tools: ToolRegistry, skills: SkillCatalog,
                 model_config: ModelConfig) -> None:
        self.analyzer, self.tools, self.skills, self.model_config = (
            analyzer, tools, skills, model_config)
        g = StateGraph(DirectorState)
        g.add_node("analyze", self._analyze)
        g.add_node("generate_blueprint", self._generate)
        g.add_node("validate", self._validate)
        g.add_edge(START, "analyze")
        g.add_conditional_edges("analyze", self._can_generate, ["generate_blueprint", END])
        g.add_edge("generate_blueprint", "validate")
        g.add_conditional_edges("validate", self._needs_repair, ["analyze", END])
        self.graph = g.compile()

    def direct(self, prd_text: str, version: int = 1,
               blueprint_id: str | None = None) -> DirectorResult:
        self._version, self._blueprint_id = version, blueprint_id
        prd_hash = sha256_text(prd_text)
        if not prd_text.strip():
            return DirectorResult(prd_hash=prd_hash, issues=[
                Issue(code="empty_prd", message="the PRD is empty")])
        state = self.graph.invoke({"prd_text": prd_text, "issues": [], "repairs": 0})
        return DirectorResult(prd_hash=prd_hash, analysis=state.get("analysis"),
                              blueprint=state.get("blueprint"),
                              report=state.get("report") or ValidationReport(),
                              issues=[i for i in state["issues"] if i.code in BLOCKING],
                              warnings=[i for i in state["issues"] if i.code not in BLOCKING])

    def _analyze(self, state: DirectorState) -> dict[str, Any]:
        report, repairs = state.get("report"), state.get("repairs", 0)
        if report is not None and not report.valid:  # looped back from validate: repair
            repairs += 1
            raw = self.analyzer.repair(state["prd_text"], state["analysis"],
                                       [f"[{e.code}] {e.message}" for e in report.errors])
        else:
            raw = self.analyzer.analyze(state["prd_text"])
        analysis, issues = normalize(raw)
        if repairs:
            issues.append(Issue(code="analysis_repaired", message=(
                f"re-analysis {repairs} after validation errors: {report.summary()}")))
        if not analysis.objective.strip():
            issues.append(Issue(code="missing_objective",
                                message="the PRD does not state an objective"))
        if not analysis.tasks:
            issues.append(Issue(code="no_tasks", message="no tasks could be identified"))
        for m in analysis.missing_information:
            if not m.lower().startswith(("objective", "tasks")):
                issues.append(Issue(code="missing_information", message=m))
        return {"analysis": analysis, "issues": issues, "repairs": repairs}

    @staticmethod
    def _can_generate(state: DirectorState) -> str:
        return END if any(i.code in BLOCKING for i in state["issues"]) else "generate_blueprint"

    def _generate(self, state: DirectorState) -> dict[str, Any]:
        bp = generate_blueprint(state["analysis"], sha256_text(state["prd_text"]), self.tools,
                                self.model_config, self._version, self._blueprint_id)
        return {"blueprint": bp}

    def _validate(self, state: DirectorState) -> dict[str, Any]:
        return {"report": validate_blueprint(state["blueprint"], self.tools, self.skills)}

    def _needs_repair(self, state: DirectorState) -> str:
        """Model analyses get bounded retries with validator feedback; templates do not."""
        if (state["report"].valid or not hasattr(self.analyzer, "repair")
                or state["repairs"] >= MAX_REPAIRS):
            return END
        return "analyze"
