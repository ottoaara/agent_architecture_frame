"""Versioned, machine-readable contracts shared by every factory stage (PRD §8)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

BLUEPRINT_SCHEMA_VERSION = "1.0"
IDENT = r"^[a-z][a-z0-9_]{0,63}$"


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- output schemas

FieldType = Literal["string", "number", "integer", "boolean", "string_list", "object"]

_PY_TYPES: dict[str, Any] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "string_list": list[str],
    "object": dict[str, Any],
}


class FieldSpec(Strict):
    """One field of a declared output schema. Schemas are data, never generated code."""

    name: str = Field(pattern=IDENT)
    type: FieldType = "string"
    description: str = ""
    required: bool = True


def build_model(model_name: str, fields: list[FieldSpec]) -> type[BaseModel]:
    """Turn declared FieldSpecs into a Pydantic model used to validate a handoff."""
    definitions: dict[str, Any] = {}
    for f in fields:
        py_type = _PY_TYPES[f.type]
        if f.required:
            definitions[f.name] = (py_type, Field(description=f.description))
        else:
            definitions[f.name] = (py_type | None, Field(default=None, description=f.description))
    return create_model(model_name, __config__=ConfigDict(extra="forbid"), **definitions)


# --------------------------------------------------------------------------- context contracts


class InputRef(Strict):
    """A named input for a task, resolved from a run input or an upstream task output.

    source syntax: ``run:<input>`` | ``task:<task_id>`` | ``task:<task_id>.<field>``
    """

    name: str = Field(pattern=IDENT)
    source: str = Field(pattern=r"^(run:[a-z][a-z0-9_]*|task:[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?)$")
    description: str = ""

    @property
    def kind(self) -> Literal["run", "task"]:
        return "run" if self.source.startswith("run:") else "task"

    @property
    def target(self) -> str:
        return self.source.split(":", 1)[1].split(".", 1)[0]

    @property
    def field(self) -> str | None:
        rest = self.source.split(":", 1)[1]
        return rest.split(".", 1)[1] if "." in rest else None


class RunInput(Strict):
    name: str = Field(pattern=IDENT)
    description: str = ""
    required: bool = True


# --------------------------------------------------------------------------- blueprint

TaskKind = Literal["agent", "tool", "checker", "hub"]
PatternName = Literal["parallel", "serial", "hub_subagent", "maker_checker"]


class SubagentSpec(Strict):
    id: str = Field(pattern=IDENT)
    role: str
    responsibility: str
    output_schema: list[FieldSpec] = Field(
        default_factory=lambda: [FieldSpec(name="findings", description="Delegated findings")]
    )


class TaskSpec(Strict):
    id: str = Field(pattern=IDENT)
    role: str
    responsibility: str
    kind: TaskKind = "agent"
    required_inputs: list[InputRef] = Field(default_factory=list)
    optional_inputs: list[InputRef] = Field(default_factory=list)
    output_schema: list[FieldSpec] = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)
    skill: str | None = None
    tool: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    subagents: list[SubagentSpec] = Field(default_factory=list)
    max_delegations: int = Field(default=4, ge=1, le=16)


class Edge(Strict):
    source: str
    target: str


CHECKER_OUTPUT_SCHEMA = [
    FieldSpec(name="approved", type="boolean", description="True only if every criterion is met"),
    FieldSpec(name="score", type="number", description="0.0-1.0 overall rubric score"),
    FieldSpec(name="unmet_criteria", type="string_list", description="Rubric items not met"),
    FieldSpec(name="feedback", description="Concrete revision instructions for the maker"),
]


class LoopSpec(Strict):
    """The only sanctioned cycle: maker -> checker -> (revise maker | exit)."""

    id: str = Field(pattern=IDENT)
    maker: str
    checker: str
    max_revisions: int = Field(default=2, ge=0, le=10)
    rubric: list[str] = Field(min_length=1)


class PatternUse(Strict):
    pattern: PatternName
    tasks: list[str]
    rationale: str = ""


class ExecutionPlan(Strict):
    patterns: list[PatternUse]
    edges: list[Edge]
    loops: list[LoopSpec] = Field(default_factory=list)
    concurrency_cap: int = Field(default=4, ge=1, le=32)

    @property
    def pattern_names(self) -> list[str]:
        return sorted({p.pattern for p in self.patterns})

    @property
    def is_hybrid(self) -> bool:
        return len(self.pattern_names) > 1


class ModelConfig(Strict):
    provider: Literal["anthropic", "ollama", "mock"] = "mock"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens: int = Field(default=16000, ge=256)


class Capabilities(Strict):
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    model: ModelConfig = Field(default_factory=ModelConfig)
    allowed_operations: list[Literal["read", "write", "external"]] = Field(default_factory=list)


class Policies(Strict):
    require_human_approval: bool = True
    max_steps: int = Field(default=60, ge=1, le=500)
    max_retries: int = Field(default=1, ge=0, le=5)
    timeout_seconds: int = Field(default=900, ge=1)
    max_model_calls: int = Field(default=60, ge=1)
    max_total_tokens: int = Field(default=400_000, ge=1)
    on_failure: Literal["stop"] = "stop"


class Quality(Strict):
    criteria: list[str] = Field(default_factory=list)
    final_task: str
    on_unmet: Literal["label_and_return", "fail"] = "label_and_return"


class Blueprint(Strict):
    schema_version: str = BLUEPRINT_SCHEMA_VERSION
    blueprint_id: str = Field(default_factory=lambda: new_id("bp"))
    version: int = Field(default=1, ge=1)
    objective: str = Field(min_length=1)
    source_prd_hash: str
    created_at: datetime = Field(default_factory=utcnow)
    inputs: list[RunInput] = Field(default_factory=list)
    tasks: list[TaskSpec] = Field(min_length=1)
    execution: ExecutionPlan
    capabilities: Capabilities = Field(default_factory=Capabilities)
    policies: Policies = Field(default_factory=Policies)
    quality: Quality

    def task(self, task_id: str) -> TaskSpec:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def content_hash(self) -> str:
        """Hash of the executable content (excludes id/version/timestamp) for audit."""
        payload = self.model_dump(
            mode="json", exclude={"blueprint_id", "version", "created_at"}
        )
        return sha256_text(json.dumps(payload, sort_keys=True))


# --------------------------------------------------------------------------- Work Director analysis


def _all_required(schema: dict[str, Any]) -> None:
    # Python defaults stay, but the schema sent to the model lists every field as required,
    # so models (small local ones especially) cannot silently drop dependencies or inputs.
    schema["required"] = list(schema.get("properties", {}))


class AnalyzedTask(BaseModel):
    """Model-produced task description. Loosely typed on purpose; normalized by the director."""

    model_config = ConfigDict(json_schema_extra=_all_required)

    id: str = Field(description="short snake_case identifier, unique within the analysis")
    title: str
    description: str = Field(description="what this task must produce and how")
    depends_on: list[str] = Field(
        default_factory=list, description="ids of tasks whose output this task needs"
    )
    user_inputs: list[str] = Field(
        default_factory=list, description="names of run inputs (from run_inputs) this task reads"
    )
    output_fields: list[FieldSpec] = Field(
        default_factory=list, description="fields this task must output"
    )
    needs_review: bool = Field(
        default=False, description="true if quality/factual verification with revision is needed"
    )
    review_criteria: list[str] = Field(default_factory=list)
    delegate_to: list[str] = Field(
        default_factory=list,
        description="specialist sub-investigations to delegate (hub + subagent), if any",
    )
    tool: str | None = Field(default=None, description="registered tool id, only if listed")


class RequirementsAnalysis(BaseModel):
    model_config = ConfigDict(json_schema_extra=_all_required)

    objective: str = Field(description="single-sentence objective; empty if none can be found")
    tasks: list[AnalyzedTask]
    run_inputs: list[RunInput] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(
        default_factory=list, description="mandatory information the PRD does not provide"
    )


# --------------------------------------------------------------------------- runs & trace

RunStatus = Literal["pending", "running", "succeeded", "failed", "blocked"]
# met / not_met come from a checker verdict; unreviewed = completed with no checker in the graph.
Disposition = Literal["met", "not_met", "unreviewed", "failed", "blocked"]


class TraceEvent(BaseModel):
    run_id: str
    seq: int
    ts: datetime = Field(default_factory=utcnow)
    type: str  # run_started | node_started | node_succeeded | node_failed | tool_call | ...
    node: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class RunResult(BaseModel):
    run_id: str
    blueprint_id: str
    blueprint_version: int
    blueprint_hash: str
    status: RunStatus
    disposition: Disposition | None = None
    final_output: dict[str, Any] | None = None
    results: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    usage: Usage = Field(default_factory=Usage)
    started_at: datetime | None = None
    finished_at: datetime | None = None
