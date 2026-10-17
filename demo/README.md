# Demo output

Each folder holds one demo run per example PRD, made with `uv run agent-factory demo`. For each PRD there is a `<prd>.blueprint.json` (the analysis, blueprint, validation report and approval), a `<prd>.run.json` (status, results, full trace, compiled graph) and a `<prd>.md` (exported output). `summary.json` holds the timings and usage.

## `ollama/`: real local model (`qwen3.8` via Ollama, LLM analyzer), 2026-10-17

| PRD | Patterns (from LLM analysis) | Graph nodes | Planning | Execution | Model calls | Tokens | Disposition |
|---|---|---|---|---|---|---|---|
| competitor_comparison | parallel + serial + maker–checker | 8 | 28.9 s | 185.4 s | 5 | 18,273 | met |
| document_transformation | serial + maker–checker | 9 | 32.8 s | 401.8 s | 11 | 39,761 | met (FAQ revised twice) |
| financial_analysis | parallel + serial + maker–checker, `data.describe_numeric` tool | 7 | 37.5 s | 141.5 s | 3 | 12,940 | met |
| vendor_due_diligence | hub + 3 subagents → serial | 6 | 26.9 s | 159.8 s | 6 | 15,355 | unreviewed (no review requested) |

Cost: $0, since everything ran locally. Planning latency is the time to analyze the PRD plus generate and validate the blueprint.

`financial_analysis.rejected.blueprint.json` comes from the first attempt. qwen3.8 dropped every `depends_on` and `user_inputs` field, so the validator rejected the blueprint before execution (`tool_input_unmapped`) and the run never started. That led to two fixes, recorded as ADR-01 in `docs/DECISIONS.md`: the analysis schema sent to the model now marks every field required, and the Work Director gained a bounded repair loop. After the fix, the same PRD produced a valid blueprint on the first analysis. The memo's figures match the tool's computed statistics and the source CSV.

## `mock/`: offline deterministic model (template analyzer)

Reproducible, needs no network or credentials, and finishes in under a second. Its outputs are placeholders. It shows the template-derived graphs: competitor comparison as a parallel fan-out and join, document transformation as a serial chain, financial analysis as a hybrid with tool and maker–checker, and vendor due diligence as hub + subagent.
