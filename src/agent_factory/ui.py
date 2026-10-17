"""Streamlit UI on localhost:8501 (FR-11). Talks to the FastAPI backend only.

Run: uv run streamlit run src/agent_factory/ui.py
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import streamlit as st

API = os.environ.get("AGENT_FACTORY_API_URL", "http://127.0.0.1:8000")
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
STATUS_COLORS = {"succeeded": "#2e7d32", "failed": "#c62828", "running": "#f9a825",
                 "pending": "#9e9e9e"}

st.set_page_config(page_title="Agent Factory", layout="wide")


def api(method: str, path: str, **kw):
    try:
        r = httpx.request(method, f"{API}{path}", timeout=60, **kw)
    except httpx.HTTPError as e:
        st.error(f"Cannot reach the API at {API}: {e}")
        st.stop()
    if r.status_code >= 400:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") \
            else {"detail": r.text}
        st.error(body.get("detail"))
        if body.get("details"):
            st.json(body["details"])
        return None
    return r


def blueprint_dot(bp: dict) -> str:
    lines = ["digraph G {", "rankdir=LR;", 'node [shape=box style="rounded,filled" '
             'fillcolor="#eef3fb" fontname="Helvetica"];']
    shapes = {"tool": "component", "checker": "diamond", "hub": "box3d"}
    for t in bp["tasks"]:
        lines.append(f'"{t["id"]}" [label="{t["id"]}\\n({t["kind"]})" '
                     f'shape={shapes.get(t["kind"], "box")}];')
    for e in bp["execution"]["edges"]:
        lines.append(f'"{e["source"]}" -> "{e["target"]}";')
    for lp in bp["execution"]["loops"]:
        lines.append(f'"{lp["checker"]}" -> "{lp["maker"]}" [style=dashed color="#c62828" '
                     f'label="revise ≤{lp["max_revisions"]}"];')
    lines.append("}")
    return "\n".join(lines)


def run_dot(graph: dict, events: list[dict]) -> str:
    status: dict[str, str] = {}
    for e in events:
        if e["node"] and e["type"] in ("node_started", "node_succeeded", "node_failed"):
            status[e["node"]] = {"node_started": "running", "node_succeeded": "succeeded",
                                 "node_failed": "failed"}[e["type"]]
        if e["type"] == "loop_exit":
            status[e["node"]] = "succeeded"
    lines = ["digraph G {", "rankdir=LR;", 'node [shape=box style="rounded,filled" '
             'fontname="Helvetica" fontcolor=white];']
    for n in graph["nodes"]:
        if n in ("__start__", "__end__"):
            lines.append(f'"{n}" [shape=circle label="" width=0.25 fillcolor="#424242"];')
        else:
            color = STATUS_COLORS.get(status.get(n, "pending"))
            lines.append(f'"{n}" [fillcolor="{color}"];')
    for src, dst, conditional in graph["edges"]:
        style = ' [style=dashed]' if conditional else ""
        lines.append(f'"{src}" -> "{dst}"{style};')
    lines.append("}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- layout

health = api("GET", "/health").json()
st.title("Dynamic Agent Factory")
st.caption(f"API {API} · provider **{health['provider']}** · model `{health['model']}`")

tab_submit, tab_bp, tab_run, tab_history = st.tabs(
    ["1 · Submit PRD", "2 · Review & approve", "3 · Run & trace", "History"])

with tab_submit:
    examples = sorted((EXAMPLES / "prds").glob("*.md")) if (EXAMPLES / "prds").exists() else []
    choice = st.selectbox("Start from an example", ["(none)"] + [p.stem for p in examples])
    uploaded = st.file_uploader("…or upload a PRD (.md / .txt)", type=["md", "txt"])
    default = ""
    if uploaded is not None:
        default = uploaded.getvalue().decode("utf-8")
    elif choice != "(none)":
        default = (EXAMPLES / "prds" / f"{choice}.md").read_text()
    text = st.text_area("PRD", value=default, height=360)
    analyzer = st.radio("Analyzer", ["auto", "template", "llm"], horizontal=True,
                        help="auto: template parser in mock mode, the model otherwise")
    if st.button("Submit PRD", type="primary", disabled=not text.strip()):
        r = api("POST", "/prds", json={"text": text, "analyzer": analyzer})
        if r:
            sub = r.json()
            st.session_state.update(blueprint_id=sub["blueprint_id"], run_id=sub["run_id"])
            st.success(f"Run ID **{sub['run_id']}** · blueprint `{sub['blueprint_id']}` "
                       f"v{sub['version']} · status **{sub['status']}**")
            for issue in sub["issues"] + sub["errors"]:
                st.error(f"[{issue['code']}] {issue['message']}")
            for w in sub["warnings"]:
                st.warning(f"[{w['code']}] {w['message']}")
            if sub["status"] == "proposed":
                st.info("Open **2 · Review & approve** to inspect the proposed architecture.")

with tab_bp:
    bps = api("GET", "/blueprints").json()
    if not bps:
        st.info("No blueprints yet.")
    else:
        labels = {f"{b['title']} — {b['id']} v{b['version']} ({b['status']})": b for b in bps}
        current = st.session_state.get("blueprint_id")
        idx = next((i for i, b in enumerate(bps) if b["id"] == current), 0)
        picked = labels[st.selectbox("Blueprint", list(labels), index=idx)]
        row = api("GET", f"/blueprints/{picked['id']}",
                  params={"version": picked["version"]}).json()
        bp = row["blueprint"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Status", row["status"])
        c2.metric("Version", row["version"])
        c3.metric("Patterns", ", ".join(sorted({p["pattern"] for p in bp["execution"]["patterns"]}))
                  if bp else "—")
        report = row["report"] or {}
        for e in report.get("errors", []):
            st.error(f"[{e['code']}] {e['message']}")
        for issue in row.get("issues") or []:
            st.warning(f"[{issue['code']}] {issue['message']}")
        if bp:
            st.subheader("Proposed execution graph")
            st.graphviz_chart(blueprint_dot(bp))
            st.subheader("Pattern rationale")
            for p in bp["execution"]["patterns"]:
                st.markdown(f"- **{p['pattern']}** — {', '.join(p['tasks'])}: {p['rationale']}")
            st.subheader("Tasks")
            st.dataframe([{
                "id": t["id"], "kind": t["kind"], "role": t["role"], "skill": t["skill"],
                "tool": t["tool"], "depends on": ", ".join(t["dependencies"]),
                "required inputs": ", ".join(r["source"] for r in t["required_inputs"]),
                "outputs": ", ".join(f["name"] for f in t["output_schema"]),
            } for t in bp["tasks"]], width="stretch")
            with st.expander("Run inputs, policies and quality"):
                st.json({"inputs": bp["inputs"], "policies": bp["policies"],
                         "quality": bp["quality"], "capabilities": bp["capabilities"]})
            with st.expander("Raw blueprint JSON"):
                st.json(bp)
            st.subheader("Decision")
            if row.get("approval"):
                a = row["approval"]
                st.caption(f"Last decision: **{a['decision']}** by {a['approver']} at "
                           f"{a['created_at']} (hash {a['blueprint_hash'][:12]}…)")
            needs = report.get("approvals_required", [])
            tools = st.multiselect("Approve side-effect tools", needs, default=[],
                                   help="Tools with side effects stay blocked unless approved")
            comment = st.text_input("Comment")
            a1, a2 = st.columns(2)
            disabled = row["status"] == "invalid"
            if a1.button("Approve", type="primary", disabled=disabled):
                if api("POST", f"/blueprints/{row['id']}/approve",
                       json={"approve": True, "version": row["version"],
                             "approved_tools": tools, "comment": comment}):
                    st.session_state["blueprint_id"] = row["id"]
                    st.rerun()
            if a2.button("Reject", disabled=disabled):
                if api("POST", f"/blueprints/{row['id']}/approve",
                       json={"approve": False, "version": row["version"], "comment": comment}):
                    st.rerun()

with tab_run:
    bp_id = st.session_state.get("blueprint_id")
    if not bp_id:
        st.info("Submit and approve a blueprint first.")
    else:
        row = api("GET", f"/blueprints/{bp_id}").json()
        bp = row["blueprint"]
        st.markdown(f"Blueprint `{bp_id}` v{row['version']} · **{row['status']}**")
        if bp and row["status"] == "approved":
            with st.form("inputs"):
                values = {}
                for i in bp["inputs"]:
                    label = f"{i['name']}{' *' if i['required'] else ''}"
                    values[i["name"]] = st.text_area(label, help=i["description"], height=120)
                    f = st.file_uploader(f"…or load {i['name']} from a file",
                                         type=["txt", "md", "csv"], key=f"f_{i['name']}")
                    if f is not None:
                        values[i["name"]] = f.getvalue().decode("utf-8")
                go = st.form_submit_button("Start run", type="primary")
            if go:
                inputs = {k: v for k, v in values.items() if v and v.strip()}
                pending = st.session_state.get("run_id")
                body = {"blueprint_id": bp_id, "version": row["version"], "inputs": inputs}
                existing = api("GET", f"/runs/{pending}").json() if pending else None
                if existing and existing["status"] == "pending" and \
                        existing["blueprint_id"] == bp_id:
                    body["run_id"] = pending
                r = api("POST", "/runs", json=body)
                if r:
                    st.session_state["active_run"] = r.json()["id"]

        run_id = st.session_state.get("active_run")
        if run_id:
            run = api("GET", f"/runs/{run_id}").json()
            st.subheader(f"Run {run_id}")
            c1, c2, c3 = st.columns(3)
            c1.metric("Status", run["status"])
            c2.metric("Disposition", run["disposition"] or "—")
            usage = (run.get("result") or {}).get("usage") or {}
            c3.metric("Model calls / tokens",
                      f"{usage.get('model_calls', 0)} / "
                      f"{usage.get('input_tokens', 0) + usage.get('output_tokens', 0)}")
            if run.get("graph"):
                st.graphviz_chart(run_dot(run["graph"], run["events"]))
            result = run.get("result") or {}
            if result.get("error"):
                st.error(result["error"])
            for loop in (result.get("quality") or {}).get("loops", []):
                msg = (f"Review **{loop['loop']}**: "
                       f"{'approved' if loop['approved'] else 'NOT approved'} after "
                       f"{loop['attempts']} check(s) (limit {loop['max_revisions'] + 1})")
                (st.success if loop["approved"] else st.warning)(msg)
                if loop["unmet_criteria"]:
                    st.markdown("Unmet: " + "; ".join(loop["unmet_criteria"]))
            if note := (result.get("quality") or {}).get("note"):
                st.warning(note)
            st.caption("A checker approval is a model judgement, not a guarantee of correctness.")
            if result.get("final_output"):
                st.subheader("Final output")
                st.json(result["final_output"])
                md = httpx.get(f"{API}/runs/{run_id}/export", params={"format": "md"}).text
                d1, d2 = st.columns(2)
                d1.download_button("Export Markdown", md, file_name=f"{run_id}.md")
                d2.download_button("Export JSON", json.dumps(run, indent=2, default=str),
                                   file_name=f"{run_id}.json")
            st.subheader("Trace")
            st.dataframe([{"seq": e["seq"], "time": e["ts"][11:23], "type": e["type"],
                           "node": e["node"],
                           "detail": json.dumps(e["data"], default=str)[:300]}
                          for e in run["events"]], width="stretch", hide_index=True)
            with st.expander("All task results"):
                st.json(result.get("results") or {})
            if run["status"] in ("pending", "running"):
                time.sleep(1.0)
                st.rerun()

with tab_history:
    runs = api("GET", "/runs").json()
    st.dataframe(runs, width="stretch", hide_index=True)
    open_id = st.selectbox("Open run", [""] + [r["id"] for r in runs if r["status"] != "pending"])
    if open_id and st.button("Open in Run & trace"):
        r = next(x for x in runs if x["id"] == open_id)
        st.session_state.update(active_run=open_id, blueprint_id=r["blueprint_id"])
        st.rerun()
