"""Command line: run the golden demo PRDs end-to-end and write demo artifacts.

    uv run agent-factory demo [--only NAME] [--out demo] [--approve-tools]

Provider/model come from the environment (.env). The CLI acts as the POC owner:
it approves each proposed blueprint, but side-effect tools are approved only
with --approve-tools.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .config import Settings
from .service import FactoryError, FactoryService

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"


def load_inputs(name: str) -> dict[str, str]:
    raw = json.loads((EXAMPLES / "inputs.json").read_text()).get(name, {})
    return {k: (ROOT / v[1:]).read_text() if v.startswith("@") else v for k, v in raw.items()}


def demo(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    svc = FactoryService(settings)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    names = [args.only] if args.only else sorted(p.stem for p in (EXAMPLES / "prds").glob("*.md"))
    rows = []
    for name in names:
        print(f"== {name} ({settings.provider}:{svc.model_config().model})")
        t0 = time.monotonic()
        try:
            sub = svc.submit_prd((EXAMPLES / "prds" / f"{name}.md").read_text(),
                                 analyzer=args.analyzer)
        except FactoryError as e:
            print(f"   submission failed: {e}")
            rows.append({"prd": name, "status": "submission_failed", "error": str(e)})
            continue
        planning = time.monotonic() - t0
        bp = svc.get_blueprint(sub["blueprint_id"], sub["version"])
        (out / f"{name}.blueprint.json").write_text(json.dumps(bp, indent=2, default=str))
        if sub["status"] != "proposed":
            print(f"   blueprint {sub['status']}: {sub['errors'] or sub['issues']}")
            rows.append({"prd": name, "status": sub["status"], "planning_s": round(planning, 2)})
            continue
        needs = bp["report"]["approvals_required"]
        svc.decide(sub["blueprint_id"], sub["version"], approve=True, approver="cli_demo",
                   approved_tools=needs if args.approve_tools else [], comment="demo run")
        t1 = time.monotonic()
        try:
            run = svc.start_run(sub["blueprint_id"], load_inputs(name), version=sub["version"],
                                run_id=sub["run_id"], background=False)
        except FactoryError as e:
            print(f"   run rejected: {e} {e.details or ''}")
            rows.append({"prd": name, "status": "run_rejected", "error": str(e)})
            continue
        execution = time.monotonic() - t1
        (out / f"{name}.run.json").write_text(json.dumps(run, indent=2, default=str))
        (out / f"{name}.md").write_text(svc.export_markdown(run["id"]))
        usage = (run.get("result") or {}).get("usage") or {}
        rows.append({
            "prd": name, "run_id": run["id"], "status": run["status"],
            "disposition": run["disposition"],
            "patterns": sorted({p["pattern"] for p in bp["blueprint"]["execution"]["patterns"]}),
            "graph_nodes": len((run.get("graph") or {}).get("nodes", [])),
            "planning_s": round(planning, 2), "execution_s": round(execution, 2),
            "model_calls": usage.get("model_calls"),
            "tokens": (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
            "error": (run.get("result") or {}).get("error"),
        })
        print(f"   {run['status']} / {run['disposition']} in {execution:.1f}s, "
              f"{usage.get('model_calls')} calls")
    (out / "summary.json").write_text(json.dumps(rows, indent=2))
    print(f"\nArtifacts written to {out}/")
    return 0 if all(r.get("status") == "succeeded" for r in rows) else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-factory")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo", help="run the example PRDs end-to-end")
    d.add_argument("--only", help="run a single example PRD by name")
    d.add_argument("--out", default="demo", help="artifact directory (default: demo)")
    d.add_argument("--analyzer", default="auto", choices=["auto", "template", "llm"])
    d.add_argument("--approve-tools", action="store_true",
                   help="also approve side-effect tools the blueprints request")
    args = parser.parse_args()
    raise SystemExit(demo(args))


if __name__ == "__main__":
    main()
