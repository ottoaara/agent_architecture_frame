"""Curated negative-test suite (PRD §10: 100% of invalid blueprints rejected)."""

import pytest

from agent_factory.capabilities import SkillCatalog, default_registry
from agent_factory.contracts import (
    CHECKER_OUTPUT_SCHEMA,
    Edge,
    FieldSpec,
    InputRef,
    PatternUse,
    RunInput,
    SubagentSpec,
)
from agent_factory.validator import validate_blueprint

from helpers import blueprint, loop, task

TOOLS, SKILLS = default_registry(), SkillCatalog()


def check(bp, run_inputs=None):
    return validate_blueprint(bp, TOOLS, SKILLS, run_inputs)


def checker(tid, maker):
    t = task(tid, [maker], kind="checker")
    t.output_schema = list(CHECKER_OUTPUT_SCHEMA)
    return t


def mc_patterns(*ids):
    return [PatternUse(pattern="maker_checker", tasks=list(ids))]


# ---------------------------------------------------------------- valid blueprints

def test_valid_serial():
    assert check(blueprint([task("a"), task("b", ["a"]), task("c", ["b"])])).valid


def test_valid_parallel_join():
    bp = blueprint([task("x"), task("y"), task("z"), task("join", ["x", "y", "z"])],
                   patterns=[PatternUse(pattern="parallel", tasks=["x", "y", "z"])])
    assert check(bp).valid, check(bp).summary()


def test_valid_maker_checker():
    bp = blueprint([task("draft"), checker("review", "draft")],
                   loops=[loop("draft", "review")], patterns=mc_patterns("draft", "review"),
                   final="draft")
    rep = check(bp)
    assert rep.valid, rep.summary()


def test_declared_loop_back_edge_is_allowed():
    bp = blueprint([task("draft"), checker("review", "draft")],
                   loops=[loop("draft", "review")], patterns=mc_patterns("draft", "review"),
                   final="draft",
                   edges=[Edge(source="draft", target="review"),
                          Edge(source="review", target="draft")])
    assert check(bp).valid, check(bp).summary()


def test_side_effect_tool_listed_as_requiring_approval():
    t = task("save", kind="tool", tool="files.write_text", out=("path",),
             tool_args={"path": "report.md", "content": "x"})
    bp = blueprint([t], tools=["files.write_text"])
    bp.capabilities.allowed_operations = ["write"]
    rep = check(bp)
    assert rep.valid, rep.summary()
    assert rep.approvals_required == ["files.write_text"]


# ---------------------------------------------------------------- negative suite

def _cyclic():
    return blueprint([task("a", ["c"]), task("b", ["a"]), task("c", ["b"])])


def _undeclared_back_edge():
    return blueprint([task("a"), task("b", ["a"])],
                     edges=[Edge(source="a", target="b"), Edge(source="b", target="a")])


def _unresolved_dep():
    return blueprint([task("a"), task("b", ["ghost"], inputs=[])])


def _unknown_tool():
    return blueprint([task("run", kind="tool", tool="python.exec", out=("result",))],
                     tools=["python.exec"])


def _tool_not_permitted():
    return blueprint([task("s", kind="tool", tool="text.stats", out=("words",),
                           inputs=[InputRef(name="text", source="run:doc")])],
                     inputs=["doc"])


def _write_without_operation():
    t = task("save", kind="tool", tool="files.write_text", out=("path",),
             tool_args={"path": "r.md", "content": "x"})
    return blueprint([t], tools=["files.write_text"])


def _agent_calls_tool():
    return blueprint([task("a", tool="text.stats")], tools=["text.stats"])


def _input_not_upstream():
    return blueprint([task("a"), task("b", inputs=[InputRef(name="a", source="task:a")])])


def _missing_field():
    return blueprint([task("a"), task("b", ["a"],
                                      inputs=[InputRef(name="x", source="task:a.nope")])])


def _required_from_optional_field():
    a = task("a")
    a.output_schema.append(FieldSpec(name="maybe", required=False))
    return blueprint([a, task("b", ["a"], inputs=[InputRef(name="m", source="task:a.maybe")])])


def _undeclared_run_input():
    return blueprint([task("a", inputs=[InputRef(name="d", source="run:document")])])


def _required_from_optional_run_input():
    return blueprint([task("a", inputs=[InputRef(name="d", source="run:document")])],
                     inputs=[RunInput(name="document", required=False)])


def _checker_without_loop():
    return blueprint([task("draft"), checker("review", "draft")], final="draft")


def _loop_with_wrong_checker_kind():
    return blueprint([task("draft"), task("review", ["draft"])],
                     loops=[loop("draft", "review")], patterns=mc_patterns("draft", "review"),
                     final="draft")


def _checker_missing_approved():
    c = task("review", ["draft"], kind="checker", out=("feedback",))
    return blueprint([task("draft"), c], loops=[loop("draft", "review")],
                     patterns=mc_patterns("draft", "review"), final="draft")


def _downstream_reads_checker():
    return blueprint([task("draft"), checker("review", "draft"), task("pub", ["review"])],
                     loops=[loop("draft", "review")], patterns=mc_patterns("draft", "review"))


def _hub_without_subagents():
    return blueprint([task("hub", kind="hub")],
                     patterns=[PatternUse(pattern="hub_subagent", tasks=["hub"])])


def _subagents_on_agent():
    return blueprint([task("a", subagents=[SubagentSpec(id="s", role="r", responsibility="x")])])


def _unknown_skill():
    bp = blueprint([task("a", skill="hacker")])
    bp.capabilities.skills = ["hacker"]
    return bp


def _skill_not_permitted():
    return blueprint([task("a", skill="writer")])


def _duplicate_ids():
    return blueprint([task("a"), task("a")])


def _final_task_missing():
    return blueprint([task("a")], final="zzz")


def _loop_pattern_undeclared():
    return blueprint([task("draft"), checker("review", "draft")],
                     loops=[loop("draft", "review")], final="draft")


def _step_budget_too_small():
    bp = blueprint([task("a"), task("b", ["a"]), task("c", ["b"])])
    bp.policies.max_steps = 3
    return bp


def _tool_output_type_mismatch():
    return blueprint([task("s", kind="tool", tool="text.stats", out=("words",),
                           tool_args={"text": "hello"}, inputs=[])], tools=["text.stats"])


def _tool_input_unmapped():
    return blueprint([task("s", kind="tool", tool="text.stats", out=("words",), inputs=[])],
                     tools=["text.stats"])


NEGATIVE = {
    "cycle": _cyclic,
    "edge_without_dependency": _undeclared_back_edge,
    "unresolved_dependency": _unresolved_dep,
    "unknown_tool": _unknown_tool,
    "tool_not_permitted": _tool_not_permitted,
    "operation_not_allowed": _write_without_operation,
    "agent_tool_use": _agent_calls_tool,
    "input_not_upstream": _input_not_upstream,
    "unresolved_input": _missing_field,
    "required_from_optional": _required_from_optional_field,
    "undeclared_run_input": _undeclared_run_input,
    "required_from_optional ": _required_from_optional_run_input,
    "orphan_checker": _checker_without_loop,
    "loop_checker_kind": _loop_with_wrong_checker_kind,
    "checker_schema": _checker_missing_approved,
    "loop_wiring": _downstream_reads_checker,
    "hub_without_subagents": _hub_without_subagents,
    "subagents_on_non_hub": _subagents_on_agent,
    "unknown_skill": _unknown_skill,
    "skill_not_permitted": _skill_not_permitted,
    "duplicate_task": _duplicate_ids,
    "final_task_unknown": _final_task_missing,
    "pattern_mismatch": _loop_pattern_undeclared,
    "step_budget": _step_budget_too_small,
    "tool_input_unmapped": _tool_input_unmapped,
    "tool_output_mismatch": _tool_output_type_mismatch,
}


@pytest.mark.parametrize("code", list(NEGATIVE))
def test_invalid_blueprint_rejected(code):
    rep = check(NEGATIVE[code]())
    assert not rep.valid
    assert code.strip() in rep.codes(), rep.summary()


def test_missing_mandatory_run_input_is_actionable():
    bp = blueprint([task("a", inputs=[InputRef(name="d", source="run:document")])],
                   inputs=[RunInput(name="document", description="the source text")])
    rep = check(bp, run_inputs={"document": "  "})
    assert rep.codes() == {"missing_run_input"}
    assert "document" in rep.errors[0].message and "source text" in rep.errors[0].message
    assert check(bp, run_inputs={"document": "hello"}).valid


def test_model_outputs_cannot_use_untyped_objects():
    t = task("a")
    t.output_schema = [FieldSpec(name="blob", type="object")]
    assert "object_field_on_model_output" in check(blueprint([t])).codes()
