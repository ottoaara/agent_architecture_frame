# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

Phase 1 is implemented in `src/agent_factory/`. The specification is `agent_factory.prd` (Dynamic Agent Factory, local macOS POC, v1.0), which is the source of truth for scope and acceptance criteria. Requirement IDs (FR-01..FR-12) and UAT scenarios (§12) define "done". The plan is in `docs/IMPLEMENTATION_PLAN.md` and the decisions are in `docs/DECISIONS.md`.

## Commands

- Install: `uv sync` (Python 3.12, versions pinned in `uv.lock`)
- API: `uv run agent-factory-api` (127.0.0.1:8000)
- UI: `uv run streamlit run src/agent_factory/ui.py --server.address 127.0.0.1` (needs the API running)
- Demo, all example PRDs: `uv run agent-factory demo --out demo/<name>`; add `--only <prd>` for one PRD
- Tests: `uv run pytest` (offline, mock model); single test: `uv run pytest tests/test_executor.py::test_name`
- Real-model test: `uv run pytest -m live` (uses `AGENT_FACTORY_PROVIDER` from `.env`)
- Lint: `uv run ruff check .`

Providers: `mock` (offline), `ollama` (local `qwen3.8` at 127.0.0.1:11434), `anthropic` (needs `ANTHROPIC_API_KEY`).

## What is being built

A standalone local Python 3.11+ application that turns a natural-language prompt or text/Markdown PRD into a validated, versioned **blueprint**, compiles that blueprint into a LangGraph graph at runtime, executes it after human approval, and persists results plus a full trace. The delivered app must run independently of Claude Code — Claude Code is only the dev tool.

Pipeline: submit PRD → Work Director analysis (objective, tasks, dependencies, inputs/outputs, constraints) → pattern selection → blueprint generation → blueprint validation → graph shown for approval → compile & run → maker–checker revision where configured → results/trace → persistence.

## Architecture

- **Factory engine (LangGraph):** Work Director, requirements analyzer, pattern selector, blueprint generator, blueprint validator, dynamic graph compiler/executor.
- **Contracts:** Pydantic models for the analysis, the blueprint (§8.1 lists required fields: ids/version/source PRD hash, tasks with required vs optional inputs and output schemas, execution plan with edges/bounded loops/concurrency cap, capabilities, policies, quality rubric), and every agent's input/output.
- **Patterns (§6):** parallel (fan-out, deterministic join), serial (schema-validated handoffs), hub + subagent (supervisor delegation into shared state), maker–checker (bounded revisions). Hybrids must compose these.
- **Tool registry / skill catalog:** nodes may only use explicitly registered, permitted tools.
- **Model layer:** LangChain with Anthropic Claude; optional Ollama. Deep Agents only where delegated planning is warranted.
- **Storage:** local files + SQLite for runs, blueprints, checkpoints, approvals; optional Chroma later.
- **API:** FastAPI on `127.0.0.1:8000` — `POST /prds`, `GET /blueprints/{id}`, `POST /blueprints/{id}/approve`, `POST /runs`, `GET /runs/{id}`, `GET /health`.
- **UI:** Streamlit on `localhost:8501` for submission, graph visualization, approval, progress, output export.

## Build order (PRD §15)

Implement core contracts, Work Director, blueprint validator, graph compiler, and test harness **before** the API or UI. Use mocks for unit tests plus at least one real model-backed end-to-end run. Deliverables include README commands, pinned requirements/lock file, `.env.example`, tests, example PRDs (golden cases: competitor comparison, synthetic financial analysis, document transformation), architecture decision notes, and demo output artifacts. Verify LangChain/LangGraph/Deep Agents versions at build time and pin them.

## Invariants to preserve

- **No case-specific agent code.** Distinct PRDs must yield distinct graphs purely from blueprints (FR-06).
- **Validate before execute.** Reject cycles outside declared bounded loops, unresolved dependencies, missing mandatory data, and unapproved tools (FR-05).
- **Never fabricate missing mandatory context** — raise a user-input interrupt or fail safely; optional fields may be null.
- **Parallel branches write to distinct state keys**; aggregators merge deterministically.
- **Maker–checker loops always terminate** at the configured max; if the threshold is unmet, label the best artifact as such.
- **No execution of model-generated Python** in Phase 1; no new tool generation in Phase 1.
- **Don't silently substitute tools** — escalate missing permissions/capabilities.
- Human approval is required for every blueprint; restricted side effects need additional approval.
- Audit/record approvals, blueprint hash/version, tool invocations, model usage, evaluation outcomes, errors.
- Bind to loopback only; secrets via environment variables (never committed). Synthetic/public data only.

Out of scope for Phase 1: PDF input, Chroma/RAG, Deep Agents delegation, skill generation, multi-tenant auth, distributed deployment (see PRD §3.2, §11).
