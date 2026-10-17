# Dynamic Agent Factory — local POC

Give it a natural-language PRD and it turns it into a **validated, versioned blueprint**. After human approval, it compiles that blueprint into a **LangGraph** graph at runtime, executes it, and persists the results with a full trace. No business-specific agents are written by hand: different PRDs produce different graphs from the blueprint alone.

Spec: [`agent_factory.prd`](agent_factory.prd) · Plan: [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) · Decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md)

```
PRD ─▶ Work Director (analyze) ─▶ pattern selection ─▶ blueprint ─▶ validator
                                                                      │
        results + trace ◀─ LangGraph executor ◀─ compiler ◀─ human approval
```

## Setup (macOS)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # installs the pinned versions from uv.lock
cp .env.example .env          # then choose a provider (see below)
```

| `AGENT_FACTORY_PROVIDER` | What it uses | Notes |
|---|---|---|
| `mock` | deterministic offline model | No network. PRDs must use the structured template (see `examples/prds`). |
| `ollama` | local model (`OLLAMA_MODEL`, default `qwen3.8`) | Run `ollama serve` and `ollama pull <model>` first. Nothing leaves the machine. |
| `anthropic` | Claude (`AGENT_FACTORY_MODEL`, default `claude-opus-5-5`) | Needs `ANTHROPIC_API_KEY`. Sends PRD and input text to the API, so use synthetic data only. |

Secrets live only in `.env`, which git ignores.

## Run

```bash
# API on http://127.0.0.1:8000 (loopback only)
uv run agent-factory-api

# UI on http://localhost:8501 (second terminal; talks to the API)
uv run streamlit run src/agent_factory/ui.py --server.address 127.0.0.1

# Headless demo: submit, approve, run and export every example PRD
uv run agent-factory demo --out demo/<name>               # all PRDs
uv run agent-factory demo --only financial_analysis       # one PRD
AGENT_FACTORY_PROVIDER=ollama uv run agent-factory demo --out demo/ollama
```

The `demo` command acts as the POC owner and approves each proposed blueprint. It approves side-effect tools only when you pass `--approve-tools`.

### API

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | readiness, provider, model |
| GET | `/catalog` | registered tools and skills |
| POST | `/prds` | submit PRD text (`{"text", "title?", "analyzer?"}`) → run id, blueprint id, status |
| POST | `/prds/upload` | upload a `.md` / `.txt` PRD |
| GET | `/blueprints`, `/blueprints/{id}` | list or fetch a validated blueprint, its report and its approval |
| POST | `/blueprints/{id}/approve` | `{"approve": true/false, "approved_tools": [...], "comment"}` |
| POST | `/runs` | start an approved blueprint (`{"blueprint_id", "inputs", "run_id?"}`) |
| GET | `/runs`, `/runs/{id}` | status, disposition, results, trace events, compiled graph |
| GET | `/runs/{id}/export` | Markdown export of the output |
| GET | `/audit` | audit log (approvals, tool calls and blocks, run start and finish) |

## Tests

```bash
uv run pytest                          # full offline suite (mock model)
uv run pytest tests/test_uat.py        # PRD §12 UAT scenarios
uv run pytest tests/test_executor.py::test_checker_never_approves_terminates_at_limit
uv run pytest -m live                  # real-provider tests (need credentials)
uv run ruff check .
```

## How a PRD becomes a graph

1. **Analyze.** The LLM (or, offline, the template parser) extracts the objective, tasks, dependencies, run inputs, constraints, success criteria and missing information. A missing objective blocks blueprint generation.
2. **Select patterns** (PRD §6). Deterministic rules choose them: same-level independent tasks run **parallel**, dependents run **serial**, tasks with `delegate_to` become **hub + subagent**, and tasks with `needs_review` become a **maker–checker** loop. Patterns combine freely into hybrids.
3. **Generate the blueprint** (PRD §8.1). It contains tasks with required and optional `InputRef`s and output schemas, edges, bounded loops, the concurrency cap, tools and skills, model config, budgets, the rubric and the final task.
4. **Validate** (FR-05). The validator rejects unknown or duplicate ids, edges that disagree with dependencies, cycles outside declared loops, unresolvable inputs, unregistered or undeclared tools, and missing mandatory run inputs.
5. **Approve** (FR-08). Approval is bound to the blueprint's content hash. `write` and `external` tools also need explicit per-tool approval.
6. **Compile and run.** The compiler builds a LangGraph `StateGraph`. Each node validates its output against its schema. Every event goes to SQLite, so the trace matches the compiled graph.

## Writing a PRD

The LLM analyzer accepts free-form Markdown or text. The offline template format looks like this:

```markdown
# Title
## Objective
One paragraph.
## Inputs
- name (required|optional): description
## Tasks
### task_id: Title
Depends on: other_task, ...
Inputs: run_input, ...
Outputs: field (string|number|integer|boolean|string_list), ...
Tool: data.describe_numeric
Review: criterion one; criterion two
Delegate: specialist a, specialist b
What the task must do.
## Success criteria
- ...
## Constraints
- ...
```

## Example PRDs (golden cases)

| PRD | Shape |
|---|---|
| `competitor_comparison` | 3 research branches run in parallel, then join into a comparison report |
| `financial_analysis` | deterministic tool (`data.describe_numeric`) runs in parallel with a context task, then a memo with maker–checker review |
| `document_transformation` | serial: validate, then extract rules, then write FAQ |
| `vendor_due_diligence` | hub + 3 delegated specialists, then a recommendation |

Demo output for each run is in `demo/` (`*.blueprint.json`, `*.run.json`, `*.md`, `summary.json`).

## Data and storage

`var/factory.db` holds PRDs, blueprint versions, approvals, runs, trace events and the audit log. `var/checkpoints.db` holds LangGraph checkpoints, and `var/runs/<run_id>/` is each run's tool workspace. All of these survive restarts. If the process dies mid-run, the run is marked `failed (interrupted)`.

## Known limitations (Phase 1)

- **The checker is the model grading itself.** A `met` disposition means the rubric checker approved the output, not that the output is correct. The UI shows both the verdict and the unmet criteria.
- **Analysis quality depends on the model.** Smaller local models can over- or under-decompose a PRD. Structural validation and human approval are the safeguards.
- Interrupted runs are not resumed from checkpoints. They are marked failed.
- The mock provider returns placeholder text and exists for plumbing and tests only.
- The tool registry is small (text stats, CSV parse and describe, scoped file read and write). New tools are added in code, not generated.
- Cost is reported as model calls and tokens, not currency.
- Out of scope, per PRD §3.2 and FR-12: PDF input, Chroma/RAG, Deep Agents delegation, skill and tool generation, auth, and multi-user use.
