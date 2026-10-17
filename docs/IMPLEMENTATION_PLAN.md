# Implementation Plan — Dynamic Agent Factory (Phase 1)

Source: `agent_factory.prd` v1.0. Build order follows PRD §15: contracts → Work Director → validator → compiler → test harness, then persistence, API and UI.

Each increment ends with a passing `uv run pytest` and is independently reviewable.

| # | Increment | PRD refs | Exit test |
|---|-----------|----------|-----------|
| 0 | Scaffold: uv project (Python 3.12), pinned deps, ruff, pytest, `.env.example`, `.gitignore` | §13 | `uv run pytest` runs |
| 1 | Contracts: `RequirementsAnalysis`, `Blueprint` (§8.1 fields), `FieldSpec` output schemas → runtime Pydantic models, input refs | FR-02, FR-04, §8 | schema round-trip, hash stability |
| 2 | Tool registry + skill catalog: trusted, allowlisted tools with side-effect class + approval flag, scoped filesystem | FR-07, §9 | unknown tool rejected, scope escape rejected |
| 3 | Blueprint validator: ids, edges vs dependencies, cycles only inside declared loops, input resolution, mandatory run inputs, tool permissions, policy bounds | FR-05 | curated negative suite: 100% rejected |
| 4 | Model layer: `ModelClient` protocol; Anthropic (official SDK, structured outputs), Ollama (LangChain), deterministic `MockModelClient`; usage + budget accounting | §7, §10 | mock structured output validates against schema |
| 5 | Graph compiler + executor: blueprint → LangGraph `StateGraph`; parallel fan-out/join, serial handoffs, hub+subagent via `Send`, bounded maker–checker; trace events; budgets; safe failure | FR-06, FR-10, §6, §8.3 | 4 patterns + hybrid execute; checker-never-approves terminates; missing context fails safely |
| 6 | Work Director: LangGraph flow analyze → select patterns → generate blueprint → validate; LLM analyzer + offline template analyzer | FR-01..FR-04 | 3 golden PRDs → 3 different valid graphs |
| 7 | Persistence + service: SQLite for PRDs, blueprints, approvals, runs, events, audit; LangGraph checkpoints | FR-08, FR-09 | restart restores run history |
| 8 | FastAPI (`127.0.0.1:8000`) | FR-11, §8.2 | TestClient end-to-end: submit → approve → run → get |
| 9 | Streamlit (`localhost:8501`): submit, graph view, approve, run, trace, export | FR-11 | manual smoke + API contract |
| 10 | Example PRDs, UAT test suite (§12), README, ADRs, demo artifacts, one live model run | §12, DoD | UAT tests pass; live run recorded |

## Key design decisions

1. **LLM decides *what*, code decides *how*.** The model produces only the `RequirementsAnalysis` (tasks, dependencies, review/delegation needs). Pattern selection, blueprint generation and graph compilation are deterministic code. This keeps graphs reproducible, testable and governed.
2. **No generated code.** Output schemas are declared as `FieldSpec` lists and turned into Pydantic models with `create_model`; tools come only from the trusted registry.
3. **Explicit context contracts.** Every task input is an `InputRef` to a run input (`run:<name>`) or an upstream task output (`task:<id>` / `task:<id>.<field>`). The validator proves every required ref is resolvable before execution.
4. **Fan-out safety.** Each task writes only `results[task_id]`; a merge reducer combines branch writes. Joins use LangGraph multi-source edges so they wait for all predecessors.
5. **Loops are declared.** A `LoopSpec` (maker, checker, max_revisions, rubric) is the only way to introduce a cycle. Downstream edges leave the loop through a synthetic exit node so join barriers stay correct.
6. **Hub + subagent.** A hub task compiles to `plan → delegate (Send fan-out to declared subagents, capped) → consolidate`.
7. **Trace outside graph state.** A `Tracer` streams node events to the store as they happen, so failures still leave a complete trace.
8. **Model provider.** Claude is called through the official `anthropic` SDK (`messages.parse` structured outputs). Ollama is via `langchain-ollama`. A deterministic mock provider makes the whole pipeline runnable offline and in CI.

## Status (2026-10-17): Phase 1 complete

Increments 0–10 are done. `uv run pytest` passes 105 offline tests, and `ruff` is clean. Real-model end-to-end runs used local `qwen3.8` (see `demo/ollama/`).

| Requirement | Evidence |
|---|---|
| FR-01 PRD input, run ID, missing objective | `POST /prds`, `/prds/upload` (.md/.txt); `test_service::test_submit_returns_unique_run_id_and_proposed_blueprint`; `test_director` missing-objective tests |
| FR-02 structured analysis | `RequirementsAnalysis` (Pydantic); `LLMAnalyzer` + `TemplateAnalyzer`; bounded repair loop (`test_director::test_repair_loop_is_bounded`) |
| FR-03 four patterns + hybrids | `director.select_patterns`; `test_director` golden PRDs; `test_executor` per-pattern + hybrid |
| FR-04 versioned blueprint | `Blueprint` §8.1 fields; version bumps per PRD hash; `test_contracts` |
| FR-05 validation before execution | `validator.py`; curated negative suite `test_validator::test_invalid_blueprint_rejected` (100% rejected) |
| FR-06 dynamic compiler | `compiler.py`, no case-specific code; 4 PRDs give 4 different graphs (`demo/`) |
| FR-07 tool registry / skills | `capabilities.py`; `test_capabilities` (unregistered, unpermitted, scope escape) |
| FR-08 approval gate | hash-bound approval + per-tool side-effect approval; `test_service::test_run_requires_approval`, `test_uat::test_unapproved_side_effect_tool_blocked_and_audited` |
| FR-09 trace + persistence | SQLite store + LangGraph checkpoints; `test_uat::test_app_restart_keeps_history` |
| FR-10 maker–checker | `LoopSpec` rubric + max revisions; `test_uat::test_checker_never_approves` |
| FR-11 Streamlit + FastAPI | `api.py` (127.0.0.1:8000), `ui.py` (8501); `test_api`; manual smoke of submit, approve, run and export |
| §12 UAT | `tests/test_uat.py` covers all seven scenarios |
| §15 real model run | `demo/ollama/` (4/4 PRDs succeeded); `uv run pytest -m live` |

Not done in Phase 1 (by design, see `docs/DECISIONS.md` ADR-11): Deep Agents, Chroma, PDF input, checkpoint resume, and a Claude-backed demo run (the provider is implemented, but it sends data to a hosted API and so needs explicit authorization).
