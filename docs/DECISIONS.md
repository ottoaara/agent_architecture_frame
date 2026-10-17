# Architecture Decision Notes — Dynamic Agent Factory (Phase 1)

Short records of the decisions that shape the POC. Each one names the PRD clause it serves.

## ADR-01 — The model decides *what*, code decides *how*

**Context.** PRD §13 calls out the risk that model-generated decomposition can miss dependencies. FR-06 requires distinct graphs with no case-specific agent code.

**Decision.** The model (or the offline template parser) produces only a `RequirementsAnalysis`: tasks, dependencies, run inputs, review needs and delegation needs. Deterministic code (`director.py`) handles pattern selection, blueprint generation and graph compilation (`compiler.py`).

**Consequences.** The same analysis always gives the same blueprint and graph, so graphs are testable and reproducible. A weak analysis gives a weak but still *valid* graph, never an unsafe one. Pattern choice follows the rules in PRD §6:

| Signal in the analysis | Pattern |
|---|---|
| ≥2 tasks at the same dependency level | parallel (fan-out, join barrier) |
| task consumes a predecessor's output | serial |
| task lists `delegate_to` specialists | hub + subagent |
| task has `needs_review` | maker–checker loop |

**Bounded repair.** If a blueprint built from a *model* analysis fails validation, the Work Director graph loops `validate → analyze`. It sends the validator errors back to the model, at most `MAX_REPAIRS = 2` times, and records each repair as an `analysis_repaired` warning. If the blueprint is still invalid after that, it is stored as `invalid` and cannot be approved. The model-facing analysis schema marks every field as required, so small local models cannot silently drop `depends_on` or `user_inputs`. (Both changes came from what we saw in live `qwen3.8` runs.)

## ADR-02 — Schemas are data; no generated code

**Context.** PRD §3.2 and §9: no execution of model-generated Python in Phase 1.

**Decision.** Output schemas are lists of `FieldSpec` and become Pydantic models through `create_model`. Tools come only from the trusted `ToolRegistry`. The factory never evaluates model text as code.

## ADR-03 — Explicit context contracts

**Context.** PRD §8.3 says mandatory context must never be fabricated.

**Decision.** Every task input is an `InputRef` (`run:<name>`, `task:<id>`, or `task:<id>.<field>`), and is either required or optional. The validator proves that every required reference resolves before execution. At runtime, a missing required value raises `MissingContext`, the run fails safely, and no downstream node runs. Optional values may be `null`.

## ADR-04 — Fan-out safety

**Decision.** Each task writes only `results[<task_id>]`, and a dict-merge reducer combines writes from parallel branches. Joins use LangGraph multi-source edges, so a join waits for all predecessors. Hub delegations are keyed by `a<attempt>_<index>_<subagent>`, so each revision's delegations stay separate.

## ADR-05 — Loops must be declared

**Decision.** A `LoopSpec` (maker, checker, max_revisions, rubric) is the only way a cycle can enter a graph. The validator rejects any other cycle. The checker routes back to the maker or to a synthetic `<loop>__exit` node, and downstream edges leave from that exit node, so join barriers stay correct. Once `max_revisions` is reached the loop always stops. The run then returns the best artifact with disposition `not_met` and lists the unmet criteria.

## ADR-06 — Approval is tied to the blueprint hash

**Context.** FR-08 and PRD §9.

**Decision.** Approval records the blueprint's content hash, which leaves out id, version and timestamp. A run is refused if the blueprint changed after approval. Tools with a `write` or `external` side-effect class need a second, explicit approval for each tool. A tool the blueprint does not list is blocked outright. A blocked call stops the run with status `blocked` and writes an audit event. The factory never substitutes another tool.

## ADR-07 — Model layer

**Context.** PRD §7 lists LangChain with Claude, plus optional Ollama.

**Decision.** All model calls go through one `ModelClient` protocol that returns validated structured output:
- **Claude** is called through the official `anthropic` SDK (`messages.parse` with structured outputs). This is a deliberate deviation from "LangChain for Claude": the native SDK gives schema-guaranteed output, effort control and refusal handling without an extra adapter layer.
- **Ollama** goes through `langchain-ollama` (`with_structured_output(method="json_schema")`). This was verified with `qwen3.8`.
- A **deterministic mock** runs the full pipeline offline and in CI.

`MeteredModel` counts calls and tokens and enforces the blueprint's call and token budgets.

## ADR-08 — Trace lives outside graph state

**Decision.** A `Tracer` streams node events (started, succeeded, failed, model call, tool call, review verdict, loop exit) to SQLite as they happen, so a failed run still has a complete trace. Every run event records the compiled graph's nodes and edges, which lets the UI show that the trace matches the proposed graph.

## ADR-09 — Persistence

**Decision.** SQLite (`var/factory.db`) holds PRDs, versioned blueprints, approvals, runs, events and the audit log. LangGraph checkpoints go to `var/checkpoints.db`. On restart, all history can still be queried. A run that was `running` when the process died is marked `failed` ("interrupted"). Resuming from a checkpoint is out of Phase 1 scope.

## ADR-10 — Two analyzers

**Decision.** `LLMAnalyzer` handles free-form PRDs. `TemplateAnalyzer` parses PRDs written in the structured template (`examples/prds`) with no model call, which keeps tests and the mock demo fully offline. In `auto` mode, a real provider uses the LLM and the mock provider uses the template.

## ADR-11 — Deferred to Phase 2

The following are deferred: Deep Agents delegation (hub + subagent is native LangGraph `Send` for now), Chroma/RAG, skill and tool generation, PDF input and resuming from checkpoints. These match the PRD's "Later" items (FR-12, §11).

## ADR-12 — Runtime

Python 3.12 (the PRD minimum is 3.11), with `uv` for dependency management and `uv.lock` pinning every version. Key pins verified at build time: `langgraph 1.2.16`, `langchain-core 1.6.12`, `langchain-ollama 1.1.0`, `anthropic 1.15.0`, `pydantic 2.14.1`, `fastapi 0.143.3`, `streamlit 1.66.0`.
