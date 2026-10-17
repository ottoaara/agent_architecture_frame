"""Small builders for hand-written blueprints used across tests."""

from agent_factory.contracts import (
    Blueprint,
    Edge,
    ExecutionPlan,
    FieldSpec,
    InputRef,
    LoopSpec,
    PatternUse,
    Quality,
    RunInput,
    TaskSpec,
)


def task(tid, deps=(), *, inputs=None, kind="agent", out=("text",), **kw):
    if inputs is None:
        inputs = [InputRef(name=d, source=f"task:{d}") for d in deps]
    required = inputs
    return TaskSpec(
        id=tid,
        role=f"{tid} role",
        responsibility=f"do {tid}",
        kind=kind,
        dependencies=list(deps),
        required_inputs=required,
        output_schema=[FieldSpec(name=o) for o in out],
        **kw,
    )


def blueprint(tasks, *, loops=(), inputs=(), patterns=None, final=None, edges=None, tools=(), **kw):
    if edges is None:
        edges = [Edge(source=d, target=t.id) for t in tasks for d in t.dependencies]
    patterns = patterns or [PatternUse(pattern="serial", tasks=[t.id for t in tasks])]
    bp = Blueprint(
        objective="test objective",
        source_prd_hash="0" * 64,
        inputs=[RunInput(name=i) if isinstance(i, str) else i for i in inputs],
        tasks=list(tasks),
        execution=ExecutionPlan(patterns=patterns, edges=edges, loops=list(loops)),
        quality=Quality(criteria=["useful"], final_task=final or tasks[-1].id),
        **kw,
    )
    bp.capabilities.tools = list(tools)
    return bp


def loop(maker, checker, max_revisions=2):
    return LoopSpec(id=f"{maker}_review", maker=maker, checker=checker,
                    max_revisions=max_revisions, rubric=["is correct"])
