from pathlib import Path

import pytest

from agent_factory.config import Settings
from agent_factory.models import MockModelClient
from agent_factory.service import FactoryError, FactoryService

PRDS = Path(__file__).parent.parent / "examples" / "prds"


def make(tmp_path, approves_after=None):
    return FactoryService(Settings(provider="mock", data_dir=tmp_path),
                          model_factory=lambda cfg: MockModelClient(approves_after))


def submit(svc, name):
    return svc.submit_prd((PRDS / f"{name}.md").read_text())


def test_submit_returns_unique_run_id_and_proposed_blueprint(tmp_path):
    svc = make(tmp_path)
    a, b = submit(svc, "competitor_comparison"), submit(svc, "competitor_comparison")
    assert a["run_id"] != b["run_id"]
    assert a["status"] == "proposed"
    assert (b["blueprint_id"], b["version"]) == (a["blueprint_id"], 2)  # versioned by PRD


def test_run_requires_approval(tmp_path):
    svc = make(tmp_path)
    sub = submit(svc, "competitor_comparison")
    with pytest.raises(FactoryError, match="approval"):
        svc.start_run(sub["blueprint_id"], {"company_profile": "x"}, background=False)
    svc.decide(sub["blueprint_id"], None, approve=False, comment="not now")
    with pytest.raises(FactoryError, match="rejected"):
        svc.start_run(sub["blueprint_id"], {"company_profile": "x"}, background=False)


def test_full_lifecycle_and_restart_persistence(tmp_path):
    svc = make(tmp_path, approves_after=1)
    sub = submit(svc, "financial_analysis")
    svc.decide(sub["blueprint_id"], None, approve=True, approver="poc_owner")
    run = svc.start_run(sub["blueprint_id"],
                        {"financials_csv": "region,revenue\nn,1\ns,2\n"},
                        run_id=sub["run_id"])
    run = svc.wait(run["id"], timeout=30)
    assert run["status"] == "succeeded" and run["disposition"] == "met"
    assert run["result"]["blueprint_hash"]
    started = [e["node"] for e in run["events"] if e["type"] == "node_started"]
    assert set(started) <= set(run["graph"]["nodes"])  # graph view matches trace

    reopened = make(tmp_path)  # simulated restart
    again = reopened.get_run(run["id"])
    assert again["status"] == "succeeded" and len(again["events"]) == len(run["events"])
    assert reopened.get_blueprint(sub["blueprint_id"])["status"] == "approved"
    actions = [a["action"] for a in reopened.store.audit_log()]
    for expected in ["prd_submitted", "blueprint_generated", "blueprint_approved",
                     "run_started", "run_finished"]:
        assert expected in actions
    assert "## Final output" in reopened.export_markdown(run["id"])


def test_missing_input_is_actionable_and_nothing_runs(tmp_path):
    svc = make(tmp_path)
    sub = submit(svc, "document_transformation")
    svc.decide(sub["blueprint_id"], None, approve=True)
    with pytest.raises(FactoryError) as e:
        svc.start_run(sub["blueprint_id"], {"audience": "staff"}, background=False)
    assert e.value.status == 422
    assert e.value.details[0]["code"] == "missing_run_input"
    assert "policy_text" in e.value.details[0]["message"]


def test_interrupted_runs_are_marked_failed_on_restart(tmp_path):
    svc = make(tmp_path)
    svc.store.add_run("run_x", "bp_x", 1, "running", {})
    assert make(tmp_path).get_run("run_x")["status"] == "failed"


def test_invalid_blueprint_cannot_be_approved(tmp_path):
    svc = make(tmp_path)
    text = (PRDS / "document_transformation.md").read_text().replace(
        "Outputs: is_valid (boolean), sections (string_list), problems (string_list)",
        "Tool: python.exec")
    sub = svc.submit_prd(text)
    assert sub["status"] == "invalid" and sub["errors"]
    with pytest.raises(FactoryError, match="invalid"):
        svc.decide(sub["blueprint_id"], None, approve=True)


def test_free_form_prd_rejected_in_mock_mode(tmp_path):
    with pytest.raises(FactoryError, match="template"):
        make(tmp_path).submit_prd("We want a nice report about our sales.")
