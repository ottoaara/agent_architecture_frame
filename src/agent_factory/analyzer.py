"""Requirements analysis: PRD text → RequirementsAnalysis (FR-01, FR-02).

``LLMAnalyzer`` handles free-form PRDs through a model with structured output.
``TemplateAnalyzer`` deterministically parses PRDs written in the structured
template (see ``examples/prds``) so the factory runs fully offline.
"""

from __future__ import annotations

import re
from typing import Protocol

from .capabilities import SkillCatalog, ToolRegistry
from .contracts import AnalyzedTask, FieldSpec, RequirementsAnalysis, RunInput
from .models import ModelClient, to_prompt_json


class Analyzer(Protocol):
    def analyze(self, prd_text: str) -> RequirementsAnalysis: ...


def snake(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"t_{s}" if s else "task"
    return s[:48]


ANALYZER_SYSTEM = """You are the Work Director of an agent factory. You read a product \
requirements document (PRD) and decompose it into the smallest set of well-scoped tasks that \
together achieve the objective. You do not execute anything.

Decomposition rules:
- Each task produces a concrete output; list its output fields (snake_case names; types \
string, number, integer, boolean or string_list).
- depends_on lists only tasks whose OUTPUT the task genuinely needs. Independent work must have \
no dependency so it can run in parallel.
- user_inputs lists the run inputs (declared in run_inputs) the task reads directly.
- Set needs_review=true only where quality or factual verification with revision is called for; \
give concrete review_criteria.
- Use delegate_to only when the task requires several specialised sub-investigations.
- Set tool only to an id from the tool catalog, and only for deterministic processing it \
performs exactly. Never invent tools.
- Put mandatory information the PRD does not supply in missing_information. If the PRD has no \
discernible objective, leave objective empty.
"""


class LLMAnalyzer:
    def __init__(self, model: ModelClient, tools: ToolRegistry, skills: SkillCatalog) -> None:
        self.model, self.tools, self.skills = model, tools, skills
        self.last_usage: tuple[int, int] = (0, 0)

    def analyze(self, prd_text: str) -> RequirementsAnalysis:
        return self._run(prd_text, "Produce the requirements analysis.")

    def repair(self, prd_text: str, previous: RequirementsAnalysis,
               problems: list[str]) -> RequirementsAnalysis:
        """Re-analyze after the blueprint built from ``previous`` failed validation."""
        return self._run(prd_text, (
            f"A previous analysis (JSON):\n{to_prompt_json(previous.model_dump())}\n\n"
            "produced a blueprint that failed structural validation:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\n\nProduce a corrected requirements analysis. Check every task's depends_on "
            "and user_inputs against the PRD, and give each tool task the run inputs or "
            "upstream outputs its tool requires."))

    def _run(self, prd_text: str, instruction: str) -> RequirementsAnalysis:
        prompt = (
            f"Tool catalog (JSON):\n{to_prompt_json(self.tools.catalog())}\n\n"
            f"PRD:\n<prd>\n{prd_text}\n</prd>\n\n{instruction}"
        )
        gen = self.model.generate(system=ANALYZER_SYSTEM, prompt=prompt,
                                  schema=RequirementsAnalysis, purpose="director:analyze")
        self.last_usage = (gen.input_tokens, gen.output_tokens)
        return RequirementsAnalysis.model_validate(gen.output.model_dump())


# --------------------------------------------------------------------------- template parser

_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.M)
_TASK = re.compile(r"^###\s+([^:\n]+?)(?::\s*(.+))?\s*$", re.M)
_KV = re.compile(r"^(depends on|inputs|outputs|review|delegate|tool)\s*:\s*(.*)$", re.I)
_FIELD = re.compile(r"^([A-Za-z][\w ]*?)\s*(?:\((\w+)\))?$")
_INPUT = re.compile(r"^[-*]\s*([A-Za-z][\w ]*?)\s*(?:\((required|optional)\))?\s*(?::\s*(.*))?$",
                    re.I)


def _split_sections(text: str) -> tuple[str, dict[str, str]]:
    parts = _SECTION.split(text)
    sections = {parts[i].strip().lower(): parts[i + 1].strip() for i in range(1, len(parts), 2)}
    return parts[0], sections


def _bullets(block: str) -> list[str]:
    return [ln.strip()[1:].strip() for ln in block.splitlines() if ln.strip()[:1] in "-*"]


def _list(value: str, sep: str = ",") -> list[str]:
    return [v.strip() for v in value.split(sep) if v.strip() and v.strip().lower() != "none"]


class TemplateAnalyzer:
    """Parses the structured PRD template. Free-form PRDs need ``LLMAnalyzer``."""

    def analyze(self, prd_text: str) -> RequirementsAnalysis:
        _, sec = _split_sections(prd_text)
        objective = " ".join(sec.get("objective", "").split())
        missing = [] if objective else ["objective: the PRD does not state an objective"]

        run_inputs = []
        for line in sec.get("inputs", "").splitlines():
            m = _INPUT.match(line.strip())
            if m:
                run_inputs.append(RunInput(
                    name=snake(m.group(1)), description=(m.group(3) or "").strip(),
                    required=(m.group(2) or "required").lower() == "required"))

        tasks = [self._task(header, title, body) for header, title, body in
                 self._task_blocks(sec.get("tasks", ""))]
        if not tasks:
            missing.append("tasks: the PRD lists no tasks under '## Tasks'")
        return RequirementsAnalysis(
            objective=objective,
            tasks=tasks,
            run_inputs=run_inputs,
            constraints=_bullets(sec.get("constraints", "")),
            success_criteria=_bullets(sec.get("success criteria", "")),
            missing_information=missing,
        )

    @staticmethod
    def _task_blocks(block: str):
        matches = list(_TASK.finditer(block))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(block)
            yield m.group(1), m.group(2) or m.group(1), block[m.end():end]

    @staticmethod
    def _task(header: str, title: str, body: str) -> AnalyzedTask:
        fields: dict[str, str] = {}
        prose = []
        for raw in body.strip().splitlines():
            line = raw.strip()
            m = _KV.match(line)
            if m:
                fields[m.group(1).lower()] = m.group(2).strip()
            elif line:
                prose.append(line)
        outputs = []
        for item in _list(fields.get("outputs", "")):
            fm = _FIELD.match(item)
            if fm:
                outputs.append(FieldSpec(name=snake(fm.group(1)), type=fm.group(2) or "string"))
        review = fields.get("review", "")
        return AnalyzedTask(
            id=snake(header),
            title=title.strip(),
            description=" ".join(prose) or title.strip(),
            depends_on=[snake(d) for d in _list(fields.get("depends on", ""))],
            user_inputs=[snake(i) for i in _list(fields.get("inputs", ""))],
            output_fields=outputs,
            needs_review=bool(review) and review.lower() not in ("no", "none"),
            review_criteria=_list(review, ";") if review.lower() not in ("yes", "no") else [],
            delegate_to=_list(fields.get("delegate", "")),
            tool=fields.get("tool") or None,
        )


def looks_like_template(prd_text: str) -> bool:
    _, sec = _split_sections(prd_text)
    return "tasks" in sec and bool(_TASK.search(sec["tasks"]))
