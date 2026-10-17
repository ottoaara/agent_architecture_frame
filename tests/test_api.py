import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_factory.api import create_app
from agent_factory.config import Settings
from agent_factory.models import MockModelClient
from agent_factory.service import FactoryService

PRDS = Path(__file__).parent.parent / "examples" / "prds"


@pytest.fixture
def client(tmp_path):
    svc = FactoryService(Settings(provider="mock", data_dir=tmp_path),
                         model_factory=lambda cfg: MockModelClient(checker_approves_after=1))
    return TestClient(create_app(svc))


def wait_done(client, run_id):
    for _ in range(200):
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] not in ("pending", "running"):
            return run
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and "data.describe_numeric" in body["tools"]


def test_submit_inspect_approve_run_export(client):
    sub = client.post("/prds", json={"text": (PRDS / "vendor_due_diligence.md").read_text()})
    assert sub.status_code == 201
    sub = sub.json()
    bp = client.get(f"/blueprints/{sub['blueprint_id']}").json()
    assert bp["status"] == "proposed" and bp["report"]["errors"] == []

    early = client.post("/runs", json={"blueprint_id": sub["blueprint_id"],
                                       "inputs": {"vendor_dossier": "x"}})
    assert early.status_code == 409

    ok = client.post(f"/blueprints/{sub['blueprint_id']}/approve", json={"approve": True})
    assert ok.json()["status"] == "approved"

    bad = client.post("/runs", json={"blueprint_id": sub["blueprint_id"], "inputs": {}})
    assert bad.status_code == 422 and bad.json()["details"][0]["code"] == "missing_run_input"

    started = client.post("/runs", json={"blueprint_id": sub["blueprint_id"],
                                         "run_id": sub["run_id"],
                                         "inputs": {"vendor_dossier": "A fictional vendor."}})
    assert started.status_code == 202
    run = wait_done(client, sub["run_id"])
    assert run["status"] == "succeeded", run
    assert any(e["type"] == "delegation_result" for e in run["events"])

    md = client.get(f"/runs/{sub['run_id']}/export?format=md")
    assert md.status_code == 200 and md.text.startswith(f"# Run {sub['run_id']}")
    assert any(a["action"] == "blueprint_approved" for a in client.get("/audit").json())


def test_upload_prd_file(client):
    files = {"file": ("prd.md", (PRDS / "document_transformation.md").read_bytes(),
                      "text/markdown")}
    assert client.post("/prds/upload", files=files).status_code == 201
    pdf = {"file": ("prd.pdf", b"%PDF", "application/pdf")}
    assert client.post("/prds/upload", files=pdf).status_code == 415


def test_unknown_ids_are_404(client):
    assert client.get("/blueprints/bp_nope").status_code == 404
    assert client.get("/runs/run_nope").status_code == 404
