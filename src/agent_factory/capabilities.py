"""Trusted tool registry and skill catalog (FR-07, PRD §9).

Only tools registered here can ever run. A blueprint must list a tool in
``capabilities.tools`` to use it, and tools with side effects additionally
require an explicit human approval for the run.
"""

from __future__ import annotations

import csv
import io
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from .contracts import FieldSpec, build_model

SideEffect = Literal["none", "read", "write", "external"]


class CapabilityError(Exception):
    """Base class for tool/skill failures that must surface rather than be substituted."""


class ToolNotAvailable(CapabilityError):
    pass


class ToolBlocked(CapabilityError):
    """Raised when a tool is not permitted or not approved for this run."""


class ScopeViolation(CapabilityError):
    pass


@dataclass
class ToolContext:
    run_id: str
    workspace: Path
    permitted_tools: set[str]
    approved_tools: set[str]
    audit: Callable[[str, dict[str, Any]], None] = lambda _type, _data: None

    def scoped_path(self, relative: str) -> Path:
        root = self.workspace.resolve()
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            raise ScopeViolation(f"path {relative!r} escapes the tool workspace")
        return candidate


ToolFn = Callable[[ToolContext, BaseModel], dict[str, Any]]


@dataclass
class ToolSpec:
    id: str
    description: str
    side_effect: SideEffect
    input_schema: list[FieldSpec]
    output_schema: list[FieldSpec]
    fn: ToolFn
    requires_approval: bool = False
    _input_model: type[BaseModel] | None = field(default=None, repr=False)

    @property
    def input_model(self) -> type[BaseModel]:
        if self._input_model is None:
            self._input_model = build_model(f"{self.id.replace('.', '_')}_in", self.input_schema)
        return self._input_model

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "side_effect": self.side_effect,
            "requires_approval": self.requires_approval,
            "inputs": [f.name for f in self.input_schema],
            "outputs": [f.name for f in self.output_schema],
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.id in self._tools:
            raise ValueError(f"tool {spec.id!r} already registered")
        if spec.side_effect in ("write", "external"):
            spec.requires_approval = True
        self._tools[spec.id] = spec
        return spec

    def get(self, tool_id: str) -> ToolSpec:
        try:
            return self._tools[tool_id]
        except KeyError:
            raise ToolNotAvailable(f"tool {tool_id!r} is not registered") from None

    def __contains__(self, tool_id: str) -> bool:
        return tool_id in self._tools

    def ids(self) -> list[str]:
        return sorted(self._tools)

    def catalog(self) -> list[dict[str, Any]]:
        return [self._tools[i].describe() for i in self.ids()]

    def invoke(self, tool_id: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        spec = self.get(tool_id)
        if tool_id not in ctx.permitted_tools:
            ctx.audit("tool_blocked", {"tool": tool_id, "reason": "not permitted by blueprint"})
            raise ToolBlocked(f"tool {tool_id!r} is not permitted by the blueprint")
        if spec.requires_approval and tool_id not in ctx.approved_tools:
            ctx.audit("tool_blocked", {"tool": tool_id, "reason": "side effect not approved"})
            raise ToolBlocked(f"tool {tool_id!r} has side effects and was not approved")
        try:
            parsed = spec.input_model.model_validate(args)
        except ValidationError as e:
            raise CapabilityError(f"invalid input for tool {tool_id!r}: {e}") from e
        ctx.audit("tool_call", {"tool": tool_id, "args": sorted(args)})
        return spec.fn(ctx, parsed)


# --------------------------------------------------------------------------- built-in tools


def _text_stats(_ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    text: str = a.text
    return {"words": len(text.split()), "characters": len(text), "lines": text.count("\n") + 1}


def _parse_csv(_ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(a.csv_text.strip()))
    rows = list(reader)
    if not reader.fieldnames:
        raise CapabilityError("CSV has no header row")
    return {"columns": list(reader.fieldnames), "row_count": len(rows), "rows": {"items": rows}}


def _describe_numeric(_ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(a.csv_text.strip()))
    rows = list(reader)
    stats: dict[str, Any] = {}
    for col in reader.fieldnames or []:
        values = []
        for r in rows:
            try:
                values.append(float(r[col]))
            except (TypeError, ValueError):
                break
        else:
            if values:
                stats[col] = {
                    "min": min(values),
                    "max": max(values),
                    "mean": round(statistics.fmean(values), 4),
                    "sum": round(sum(values), 4),
                }
    return {"row_count": len(rows), "numeric_columns": sorted(stats), "stats": stats}


def _read_text(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    path = ctx.scoped_path(a.path)
    if not path.is_file():
        raise CapabilityError(f"file {a.path!r} not found in workspace")
    return {"content": path.read_text(encoding="utf-8")}


def _write_text(ctx: ToolContext, a: BaseModel) -> dict[str, Any]:
    path = ctx.scoped_path(a.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(a.content, encoding="utf-8")
    return {"path": str(path.relative_to(ctx.workspace.resolve())), "bytes": len(a.content)}


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    s = FieldSpec
    reg.register(ToolSpec(
        "text.stats", "Count words, characters and lines in text.", "none",
        [s(name="text")],
        [s(name="words", type="integer"), s(name="characters", type="integer"),
         s(name="lines", type="integer")],
        _text_stats,
    ))
    reg.register(ToolSpec(
        "data.parse_csv", "Parse CSV text with a header row into columns and rows.", "none",
        [s(name="csv_text")],
        [s(name="columns", type="string_list"), s(name="row_count", type="integer"),
         s(name="rows", type="object")],
        _parse_csv,
    ))
    reg.register(ToolSpec(
        "data.describe_numeric", "Compute min/max/mean/sum for numeric CSV columns.", "none",
        [s(name="csv_text")],
        [s(name="row_count", type="integer"), s(name="numeric_columns", type="string_list"),
         s(name="stats", type="object")],
        _describe_numeric,
    ))
    reg.register(ToolSpec(
        "files.read_text", "Read a UTF-8 text file from the run workspace.", "read",
        [s(name="path")], [s(name="content")], _read_text,
    ))
    reg.register(ToolSpec(
        "files.write_text", "Write a UTF-8 text file into the run workspace.", "write",
        [s(name="path"), s(name="content")],
        [s(name="path"), s(name="bytes", type="integer")], _write_text,
    ))
    return reg


# --------------------------------------------------------------------------- skills


@dataclass(frozen=True)
class SkillSpec:
    id: str
    title: str
    instructions: str


_SKILLS = [
    SkillSpec("researcher", "Researcher",
              "Investigate the assigned topic using only the provided context and general "
              "knowledge. State assumptions explicitly; never invent figures or sources."),
    SkillSpec("analyst", "Analyst",
              "Analyse the provided data. Quote the numbers you rely on and show how each "
              "conclusion follows from them."),
    SkillSpec("data_engineer", "Data engineer",
              "Validate and transform the provided data exactly as instructed. Report any rows "
              "or fields that could not be processed instead of guessing values."),
    SkillSpec("writer", "Writer",
              "Write clear, structured prose for a business reader. Use only facts present in "
              "the provided inputs."),
    SkillSpec("synthesizer", "Synthesizer",
              "Combine several upstream results into one consistent view. Attribute each point "
              "to the input it came from and flag contradictions."),
    SkillSpec("reviewer", "Reviewer",
              "Assess the artifact strictly against the rubric. Approve only if every criterion "
              "is met; otherwise list unmet criteria and actionable feedback."),
    SkillSpec("coordinator", "Coordinator",
              "Break the assignment into scoped sub-tasks for the available specialists and "
              "consolidate their findings."),
]


class SkillCatalog:
    def __init__(self, skills: list[SkillSpec] | None = None) -> None:
        self._skills = {s.id: s for s in (skills if skills is not None else _SKILLS)}

    def get(self, skill_id: str) -> SkillSpec:
        try:
            return self._skills[skill_id]
        except KeyError:
            raise CapabilityError(f"skill {skill_id!r} is not in the catalog") from None

    def __contains__(self, skill_id: str) -> bool:
        return skill_id in self._skills

    def ids(self) -> list[str]:
        return sorted(self._skills)
