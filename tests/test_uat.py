"""PRD §12 UAT scenarios, driven end-to-end through the service from PRD text."""

from pathlib import Path

import pytest

from agent_factory.config import Settings
from agent_factory.models import MockModelClient, ModelError
from agent_factory.service import FactoryError, FactoryService

ROOT = Path(__file__).parent.parent
PRDS = ROOT / "examples" / "prds"
CSV = (ROOT / "examples" / "data" / "synthetic_financials.csv").read_text()
POLICY = (ROOT / "examples" / "data" / "remote_work_policy.txt").read_text()

SAVE_PRD = """# Save a summary
## Objective
Summarise a note and save the summary to the run workspace.
## Inputs
- note (required): text to summarise
- path (required): relative output path
## Tasks
### write_summary: Write summary
Inputs: note
Outputs: content
Summarise the note in three sentences.
### save_summary: Save summary
Depends on: write_summary
Inputs: path
Tool: files.write_text
Write the summary to the given path.
"""


def service(tmp_path, model=None):
    return FactoryService(Settings(provider="mock", data_dir=tmp_path),
                          model_factory=lambda cfg: model or MockModelClient())


def approved(svc, text, tools=()):
    sub = svc.submit_prd(text)
    assert sub["status"] == "proposed", sub
    svc.decide(sub["blueprint_id"], None, approve=True, approved_tools=list(tools))
    return sub


def run(svc, sub, inputs):
    return svc.start_run(sub["blueprint_id"], inputs, run_id=sub["run_id"], background=False)


def nodes(run_record, type_="node_started"):
    return [e["node"] for e in run_record["events"] if e["type"] == type_]


def test_parallel_competitive_analysis(tmp_path):
    svc = service(tmp_path)
    sub = approved(svc, (PRDS / "competitor_comparison.md").read_text())
    r = run(svc, sub, {"company_profile": "200 staff, UK"})
    assert r["status"] == "succeeded"
    order = nodes(r)
    assert set(order[:3]) == {"research_acme", "research_brightpay", "research_cobalt"}
    assert order[3] == "comparison_report"
    assert set(r["result"]["final_output"]) == {"comparison_table", "recommendation", "rationale"}


def test_serial_transformation_stops_when_upstream_fails(tmp_path):
    def broken(_prompt, _schema):
        raise ModelError("simulated outage")

    svc = service(tmp_path, MockModelClient(responders={"agent:validate_document": broken}))
    sub = approved(svc, (PRDS / "document_transformation.md").read_text())
    r = run(svc, sub, {"policy_text": POLICY})
    assert r["status"] == "failed" and "simulated outage" in r["result"]["error"]
    assert nodes(r) == ["validate_document"]  # extract_rules/write_faq never ran
    assert any(e["type"] == "node_retry" for e in r["events"])


def test_serial_transformation_happy_path_order(tmp_path):
    svc = service(tmp_path)
    sub = approved(svc, (PRDS / "document_transformation.md").read_text())
    r = run(svc, sub, {"policy_text": POLICY})
    assert nodes(r) == ["validate_document", "extract_rules", "write_faq"]


def test_hybrid_analysis_with_maker_checker(tmp_path):
    svc = service(tmp_path, MockModelClient(checker_approves_after=1))
    sub = approved(svc, (PRDS / "financial_analysis.md").read_text())
    r = run(svc, sub, {"financials_csv": CSV})
    assert r["status"] == "succeeded" and r["disposition"] == "met"
    order = nodes(r)
    assert set(order[:2]) == {"compute_metrics", "sector_context"}
    assert order.count("draft_memo") == 2 and order.count("draft_memo_review") == 2
    assert r["result"]["results"]["compute_metrics"]["row_count"] == 6
    loop = r["result"]["quality"]["loops"][0]
    assert loop["approved"] and loop["attempts"] == 2


def test_missing_mandatory_context(tmp_path):
    svc = service(tmp_path)
    sub = approved(svc, (PRDS / "financial_analysis.md").read_text())
    with pytest.raises(FactoryError) as e:
        run(svc, sub, {"business_context": "only optional input"})
    assert e.value.details[0]["code"] == "missing_run_input"
    assert svc.get_run(sub["run_id"])["status"] == "pending"
    assert svc.get_run(sub["run_id"])["events"] == []


def test_code_execution_request_is_rejected(tmp_path):
    svc = service(tmp_path)
    text = SAVE_PRD.replace("Tool: files.write_text", "Tool: python.exec")
    sub = svc.submit_prd(text)
    assert sub["status"] == "invalid"
    assert "unknown_tool" in {e["code"] for e in sub["errors"]}
    audit = svc.store.audit_log(sub["blueprint_id"])
    assert audit[-1]["action"] == "blueprint_generated"
    assert audit[-1]["data"]["status"] == "invalid"


def test_unapproved_side_effect_tool_blocked_and_audited(tmp_path):
    svc = service(tmp_path)
    sub = approved(svc, SAVE_PRD)  # approved, but files.write_text not approved
    r = run(svc, sub, {"note": "hello", "path": "out/summary.md"})
    assert r["status"] == "blocked"
    assert "save_summary" in nodes(r, "node_failed")
    assert any(a["action"] == "tool_blocked" for a in svc.store.audit_log(sub["run_id"]))
    assert not (tmp_path / "runs" / sub["run_id"] / "out" / "summary.md").exists()

    sub2 = approved(svc, SAVE_PRD, tools=["files.write_text"])
    r2 = run(svc, sub2, {"note": "hello", "path": "out/summary.md"})
    assert r2["status"] == "succeeded", r2["result"]
    assert (tmp_path / "runs" / sub2["run_id"] / "out" / "summary.md").exists()


def test_checker_never_approves(tmp_path):
    svc = service(tmp_path, MockModelClient(checker_approves_after=-1))
    sub = approved(svc, (PRDS / "financial_analysis.md").read_text())
    r = run(svc, sub, {"financials_csv": CSV})
    assert r["status"] == "succeeded" and r["disposition"] == "not_met"
    assert nodes(r).count("draft_memo_review") == 3  # 1 + max_revisions(2)
    assert r["result"]["final_output"] is not None  # best available artifact
    assert "did not meet the quality threshold" in svc.export_markdown(sub["run_id"])


def test_app_restart_keeps_history(tmp_path):
    svc = service(tmp_path)
    sub = approved(svc, (PRDS / "vendor_due_diligence.md").read_text())
    run(svc, sub, {"vendor_dossier": "fictional"})
    restarted = service(tmp_path)
    assert restarted.get_run(sub["run_id"])["status"] == "succeeded"
    assert restarted.get_blueprint(sub["blueprint_id"])["blueprint"]["objective"]
    assert [r["id"] for r in restarted.list_runs()] == [sub["run_id"]]
