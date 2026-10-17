"""Real-model end-to-end run (PRD §15). Opt in with ``uv run pytest -m live``.

Uses the provider in the environment (.env): ``ollama`` (local) or ``anthropic``.
"""

import dataclasses

import pytest

from agent_factory.cli import EXAMPLES, load_inputs
from agent_factory.config import Settings
from agent_factory.service import FactoryService

pytestmark = pytest.mark.live


def test_free_form_analysis_and_parallel_run_with_real_model(tmp_path):
    settings = Settings.from_env()
    if settings.provider == "mock":
        pytest.skip("set AGENT_FACTORY_PROVIDER=ollama or anthropic")
    svc = FactoryService(dataclasses.replace(settings, data_dir=tmp_path))

    sub = svc.submit_prd((EXAMPLES / "prds" / "competitor_comparison.md").read_text(),
                         analyzer="llm")
    assert sub["status"] == "proposed", sub
    bp = svc.get_blueprint(sub["blueprint_id"])["blueprint"]
    assert "parallel" in {p["pattern"] for p in bp["execution"]["patterns"]}

    svc.decide(sub["blueprint_id"], None, approve=True, approver="live_test")
    run = svc.start_run(sub["blueprint_id"], load_inputs("competitor_comparison"),
                        background=False)
    assert run["status"] == "succeeded", (run.get("result") or {}).get("error")
    assert run["disposition"] in ("met", "not_met", "unreviewed")
    assert run["result"]["final_output"]
    executed = {e["node"] for e in run["events"] if e["type"] == "node_started"}
    assert executed <= set(run["graph"]["nodes"])  # trace matches the compiled graph
    assert run["result"]["usage"]["model_calls"] > 0
