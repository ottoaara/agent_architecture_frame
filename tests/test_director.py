from pathlib import Path

import pytest

from agent_factory.analyzer import TemplateAnalyzer, looks_like_template
from agent_factory.capabilities import SkillCatalog, default_registry
from agent_factory.contracts import AnalyzedTask, ModelConfig, RequirementsAnalysis, RunInput
from agent_factory.director import MAX_REPAIRS, WorkDirector, generate_blueprint, normalize

PRDS = Path(__file__).parent.parent / "examples" / "prds"
TOOLS, SKILLS = default_registry(), SkillCatalog()


def direct(text):
    return WorkDirector(TemplateAnalyzer(), TOOLS, SKILLS, ModelConfig()).direct(text)


def prd(name):
    return (PRDS / f"{name}.md").read_text()


@pytest.mark.parametrize("name", ["competitor_comparison", "financial_analysis",
                                  "document_transformation", "vendor_due_diligence"])
def test_golden_prds_produce_valid_blueprints(name):
    res = direct(prd(name))
    assert res.ok, (res.issues, res.report.summary())
    assert looks_like_template(prd(name))


def test_competitor_comparison_is_parallel_fan_out_and_join():
    bp = direct(prd("competitor_comparison")).blueprint
    assert bp.execution.pattern_names == ["parallel", "serial"]
    report = bp.task("comparison_report")
    assert set(report.dependencies) == {"research_acme", "research_brightpay", "research_cobalt"}
    assert bp.quality.final_task == "comparison_report"


def test_financial_analysis_is_hybrid_with_tool_and_review():
    bp = direct(prd("financial_analysis")).blueprint
    assert bp.execution.pattern_names == ["maker_checker", "parallel", "serial"]
    metrics = bp.task("compute_metrics")
    assert metrics.kind == "tool" and metrics.required_inputs[0].source == "run:financials_csv"
    assert [lp.maker for lp in bp.execution.loops] == ["draft_memo"]
    assert len(bp.execution.loops[0].rubric) == 3
    assert bp.capabilities.tools == ["data.describe_numeric"]
    assert bp.quality.final_task == "draft_memo"
    assert all(t.dependencies or t.id in ("compute_metrics", "sector_context") for t in bp.tasks)


def test_document_transformation_is_serial():
    bp = direct(prd("document_transformation")).blueprint
    assert bp.execution.pattern_names == ["serial"]
    assert [t.id for t in bp.tasks] == ["validate_document", "extract_rules", "write_faq"]
    faq = bp.task("write_faq")
    assert [r.source for r in faq.optional_inputs] == ["run:audience"]


def test_vendor_due_diligence_uses_hub():
    bp = direct(prd("vendor_due_diligence")).blueprint
    hub = bp.task("risk_investigation")
    assert hub.kind == "hub" and [s.id for s in hub.subagents] == [
        "security", "financial_stability", "regulatory_compliance"]
    assert "hub_subagent" in bp.execution.pattern_names


def test_distinct_prds_yield_distinct_graphs():
    shapes = set()
    for name in ["competitor_comparison", "financial_analysis", "document_transformation"]:
        bp = direct(prd(name)).blueprint
        shapes.add((tuple(bp.execution.pattern_names),
                    tuple(sorted((e.source, e.target) for e in bp.execution.edges))))
    assert len(shapes) == 3


def test_missing_objective_is_surfaced():
    res = direct("# Untitled\n\n## Tasks\n### a: Do a thing\nOutputs: x\n")
    assert res.blueprint is None
    assert [i.code for i in res.issues] == ["missing_objective"]


def test_empty_prd():
    assert [i.code for i in direct("   ").issues] == ["empty_prd"]


def test_unknown_tool_is_escalated_not_substituted():
    text = prd("document_transformation").replace(
        "Outputs: is_valid (boolean), sections (string_list), problems (string_list)",
        "Tool: python.exec")
    res = direct(text)
    assert res.blueprint is not None and not res.ok
    assert "unknown_tool" in res.report.codes()


def test_multiple_sinks_get_a_final_join():
    analysis = RequirementsAnalysis(objective="o", tasks=[
        AnalyzedTask(id="a", title="A", description="a"),
        AnalyzedTask(id="b", title="B", description="b")])
    bp = generate_blueprint(normalize(analysis)[0], "h", TOOLS, ModelConfig())
    assert bp.quality.final_task == "final_output"
    assert set(bp.task("final_output").dependencies) == {"a", "b"}


def test_normalize_dedupes_ids_and_drops_unknown_deps():
    analysis = RequirementsAnalysis(objective="o", tasks=[
        AnalyzedTask(id="Step One", title="x", description="x"),
        AnalyzedTask(id="Step One", title="y", description="y", depends_on=["ghost"])])
    out, issues = normalize(analysis)
    assert [t.id for t in out.tasks] == ["step_one", "step_one_2"]
    assert issues[0].code == "dropped_dependency"


def test_same_analysis_same_blueprint_hash():
    a = direct(prd("financial_analysis")).blueprint
    b = direct(prd("financial_analysis")).blueprint
    assert a.content_hash() == b.content_hash()


class RepairingAnalyzer:
    """Returns an unwired tool task first; repairs succeed after ``fix_after`` attempts."""

    def __init__(self, fix_after):
        self.fix_after, self.repairs = fix_after, []

    def _analysis(self, wired):
        return RequirementsAnalysis(
            objective="Describe the numbers",
            run_inputs=[RunInput(name="financials_csv")],
            tasks=[AnalyzedTask(id="stats", title="Stats", description="describe",
                                tool="data.describe_numeric",
                                user_inputs=["financials_csv"] if wired else [])])

    def analyze(self, prd_text):
        return self._analysis(wired=False)

    def repair(self, prd_text, previous, problems):
        self.repairs.append(problems)
        return self._analysis(wired=len(self.repairs) >= self.fix_after)


def test_invalid_model_analysis_is_repaired_with_validator_feedback():
    analyzer = RepairingAnalyzer(fix_after=1)
    res = WorkDirector(analyzer, TOOLS, SKILLS, ModelConfig()).direct("any prd")
    assert res.ok, res.report.summary()
    assert len(analyzer.repairs) == 1
    assert any("tool_input_unmapped" in p for p in analyzer.repairs[0])
    assert [w.code for w in res.warnings] == ["analysis_repaired"]


def test_repair_loop_is_bounded():
    analyzer = RepairingAnalyzer(fix_after=99)
    res = WorkDirector(analyzer, TOOLS, SKILLS, ModelConfig()).direct("any prd")
    assert not res.ok and res.report.errors[0].code == "tool_input_unmapped"
    assert len(analyzer.repairs) == MAX_REPAIRS
