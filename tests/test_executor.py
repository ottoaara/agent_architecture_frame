import pytest

from agent_factory.capabilities import SkillCatalog, default_registry
from agent_factory.contracts import (
    CHECKER_OUTPUT_SCHEMA,
    FieldSpec,
    InputRef,
    PatternUse,
    RunInput,
    SubagentSpec,
)
from agent_factory.executor import execute
from agent_factory.models import MockModelClient
from agent_factory.trace import Tracer

from helpers import blueprint, loop, task

TOOLS, SKILLS = default_registry(), SkillCatalog()


def run(bp, tmp_path, inputs=None, model=None, approved=None):
    tracer = Tracer("run_t")
    res = execute(bp, run_id="run_t", inputs=inputs or {}, model=model or MockModelClient(),
                  tools=TOOLS, skills=SKILLS, workspace=tmp_path, tracer=tracer,
                  approved_tools=approved)
    return res, tracer


def checker(tid, maker):
    t = task(tid, [maker], kind="checker")
    t.output_schema = list(CHECKER_OUTPUT_SCHEMA)
    return t


def started(tracer):
    return tracer.executed_nodes()


def graph_nodes(tracer):
    return set(tracer.of_type("run_started")[0].data["graph"]["nodes"])


def test_serial_runs_in_dependency_order(tmp_path):
    bp = blueprint([task("a"), task("b", ["a"]), task("c", ["b"])])
    res, tr = run(bp, tmp_path)
    assert res.status == "succeeded" and res.disposition == "unreviewed"
    assert started(tr) == ["a", "b", "c"]
    assert set(res.final_output) == {"text"}


def test_parallel_fan_out_and_deterministic_join(tmp_path):
    bp = blueprint([task("x"), task("y"), task("z"), task("join", ["x", "y", "z"])],
                   patterns=[PatternUse(pattern="parallel", tasks=["x", "y", "z"])])
    res, tr = run(bp, tmp_path)
    assert res.status == "succeeded"
    order = started(tr)
    assert set(order[:3]) == {"x", "y", "z"} and order[3] == "join"
    assert set(res.results) == {"x", "y", "z", "join"}  # distinct keys per branch


def test_hub_delegates_to_each_subagent_and_consolidates(tmp_path):
    hub = task("investigate", kind="hub", subagents=[
        SubagentSpec(id="legal", role="Legal", responsibility="legal angle"),
        SubagentSpec(id="market", role="Market", responsibility="market angle"),
    ])
    bp = blueprint([hub, task("report", ["investigate"])],
                   patterns=[PatternUse(pattern="hub_subagent", tasks=["investigate"])])
    res, tr = run(bp, tmp_path)
    assert res.status == "succeeded", res.error
    order = started(tr)
    assert order[0] == "investigate__plan"
    assert order.count("investigate__worker") == 2
    assert order[-2:] == ["investigate", "report"]
    subs = {e.data["subagent"] for e in tr.of_type("delegation_result")}
    assert subs == {"legal", "market"}


def _mc(max_revisions=2):
    return blueprint([task("draft"), checker("review", "draft"), task("publish", ["draft"])],
                     loops=[loop("draft", "review", max_revisions)],
                     patterns=[PatternUse(pattern="maker_checker", tasks=["draft", "review"])],
                     final="publish")


def test_maker_checker_revises_until_approved(tmp_path):
    res, tr = run(_mc(), tmp_path, model=MockModelClient(checker_approves_after=1))
    assert res.status == "succeeded" and res.disposition == "met"
    assert started(tr) == ["draft", "review", "draft", "review", "draft_review__exit", "publish"]
    assert res.quality["loops"][0]["attempts"] == 2
    attempts = [(e.node, e.data["attempt"]) for e in tr.of_type("node_started")
                if e.node in ("draft", "review")]
    assert attempts == [("draft", 1), ("review", 1), ("draft", 2), ("review", 2)]
    assert [e.data["attempt"] for e in tr.of_type("review_verdict")] == [1, 2]


def test_checker_never_approves_terminates_at_limit(tmp_path):
    res, tr = run(_mc(max_revisions=2), tmp_path, model=MockModelClient(checker_approves_after=-1))
    assert res.status == "succeeded" and res.disposition == "not_met"
    assert started(tr).count("review") == 3  # initial + 2 revisions
    assert tr.of_type("loop_limit_reached")
    assert res.quality["loops"][0]["unmet_criteria"]
    assert "did not meet" in res.quality["note"]


def test_hybrid_parallel_then_maker_checker(tmp_path):
    bp = blueprint(
        [task("a"), task("b"), task("draft", ["a", "b"]), checker("review", "draft"),
         task("final", ["draft"])],
        loops=[loop("draft", "review", 1)],
        patterns=[PatternUse(pattern="parallel", tasks=["a", "b"]),
                  PatternUse(pattern="maker_checker", tasks=["draft", "review"])],
    )
    res, tr = run(bp, tmp_path, model=MockModelClient(checker_approves_after=1))
    assert res.status == "succeeded" and res.disposition == "met"
    assert bp.execution.is_hybrid
    assert set(started(tr)) <= graph_nodes(tr)


def test_missing_mandatory_input_stops_before_any_node(tmp_path):
    bp = blueprint([task("a", inputs=[InputRef(name="doc", source="run:document")]),
                    task("b", ["a"])], inputs=[RunInput(name="document")])
    res, tr = run(bp, tmp_path, inputs={})
    assert res.status == "failed" and "missing_run_input" in res.error
    assert started(tr) == []


def test_unapproved_side_effect_blocks_and_audits(tmp_path):
    save = task("save", ["a"], kind="tool", tool="files.write_text", out=("path",),
                inputs=[InputRef(name="content", source="task:a.text")],
                tool_args={"path": "out/report.md"})
    bp = blueprint([task("a"), save, task("after", ["save"])], tools=["files.write_text"])
    bp.capabilities.allowed_operations = ["write"]
    res, tr = run(bp, tmp_path)
    assert res.status == "blocked" and res.disposition == "blocked"
    assert tr.of_type("audit_tool_blocked")
    assert "after" not in started(tr)
    assert not (tmp_path / "out/report.md").exists()

    res, _ = run(bp, tmp_path, approved={"files.write_text"})
    assert res.status == "succeeded"
    assert (tmp_path / "out/report.md").exists()


def test_model_call_budget_enforced(tmp_path):
    bp = blueprint([task("a"), task("b", ["a"]), task("c", ["b"])])
    bp.policies.max_model_calls = 2
    res, tr = run(bp, tmp_path)
    assert res.status == "failed" and "budget" in res.error
    assert tr.of_type("node_failed")[0].node == "c"


def test_tool_task_in_serial_pipeline(tmp_path):
    stats = task("stats", kind="tool", tool="data.describe_numeric", out=("row_count",),
                 inputs=[InputRef(name="csv_text", source="run:data")])
    stats.output_schema = [FieldSpec(name="row_count", type="integer"),
                           FieldSpec(name="numeric_columns", type="string_list")]
    bp = blueprint([stats, task("summary", ["stats"])], inputs=["data"],
                   tools=["data.describe_numeric"])
    res, _ = run(bp, tmp_path, inputs={"data": "a,b\n1,2\n3,4\n"})
    assert res.status == "succeeded", res.error
    assert res.results["stats"] == {"row_count": 2, "numeric_columns": ["a", "b"]}


@pytest.mark.parametrize("n", [1, 2, 3])
def test_usage_is_recorded(tmp_path, n):
    tasks = [task("t0")] + [task(f"t{i}", [f"t{i-1}"]) for i in range(1, n)]
    res, _ = run(blueprint(tasks), tmp_path)
    assert res.usage.model_calls == n
