"""Model provider layer: one structured-output interface over Claude, Ollama and a mock.

Every call goes through ``MeteredModel`` so call counts and tokens are recorded
and the blueprint's budgets are enforced (PRD §7, §10).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from .contracts import ModelConfig, Policies, Usage


class ModelError(Exception):
    pass


class ModelRefusal(ModelError):
    pass


class BudgetExceeded(ModelError):
    pass


@dataclass
class Generation:
    output: BaseModel
    input_tokens: int
    output_tokens: int
    model: str


class ModelClient(Protocol):
    name: str

    def generate(self, *, system: str, prompt: str, schema: type[BaseModel],
                 purpose: str) -> Generation: ...


# --------------------------------------------------------------------------- Claude


class AnthropicModelClient:
    """Claude via the official SDK with structured outputs (``messages.parse``)."""

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium",
                 max_tokens: int = 16000, client: Any = None) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model, self.effort, self.max_tokens = model, effort, max_tokens
        self.name = f"anthropic:{model}"

    def generate(self, *, system: str, prompt: str, schema: type[BaseModel],
                 purpose: str) -> Generation:
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": self.effort},
                # Server-side fallback re-runs a declined request on a suitable model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self._anthropic.RateLimitError as e:
            raise ModelError(f"rate limited ({purpose}): {e.message}") from e
        except self._anthropic.APIStatusError as e:
            raise ModelError(f"API error {e.status_code} ({purpose}): {e.message}") from e
        except self._anthropic.APIConnectionError as e:
            raise ModelError(f"cannot reach the Claude API ({purpose})") from e
        if response.stop_reason == "refusal":
            raise ModelRefusal(f"model declined the request ({purpose})")
        if response.stop_reason == "max_tokens":
            raise ModelError(f"output truncated at max_tokens ({purpose})")
        if response.parsed_output is None:
            raise ModelError(f"no structured output returned ({purpose})")
        return Generation(response.parsed_output, response.usage.input_tokens,
                          response.usage.output_tokens, response.model)


# --------------------------------------------------------------------------- Ollama (LangChain)


class OllamaModelClient:
    def __init__(self, model: str, base_url: str) -> None:
        from langchain_ollama import ChatOllama

        self.chat = ChatOllama(model=model, base_url=base_url, temperature=0)
        self.model = model
        self.name = f"ollama:{model}"

    def generate(self, *, system: str, prompt: str, schema: type[BaseModel],
                 purpose: str) -> Generation:
        runnable = self.chat.with_structured_output(schema, method="json_schema",
                                                    include_raw=True)
        try:
            out = runnable.invoke([("system", system), ("human", prompt)])
        except Exception as e:  # connection errors surface as many types
            raise ModelError(f"Ollama call failed ({purpose}): {e}") from e
        if out.get("parsing_error") or out.get("parsed") is None:
            raise ModelError(f"Ollama returned unparseable output ({purpose})")
        usage = getattr(out["raw"], "usage_metadata", None) or {}
        return Generation(out["parsed"], usage.get("input_tokens", 0),
                          usage.get("output_tokens", 0), self.model)


# --------------------------------------------------------------------------- mock


Responder = Callable[[str, type[BaseModel]], dict[str, Any] | BaseModel]


class MockModelClient:
    """Deterministic offline model. Fills any schema with plausible placeholder values.

    ``checker_approves_after``: number of checker calls that reject before the first
    approval (None = always approve, -1 = never approve).
    ``responders``: per-purpose overrides, keyed by purpose prefix.
    """

    name = "mock"

    def __init__(self, checker_approves_after: int | None = None,
                 responders: dict[str, Responder] | None = None) -> None:
        self.checker_approves_after = checker_approves_after
        self.responders = responders or {}
        self.calls: list[str] = []
        self._checker_calls: dict[str, int] = {}
        self._lock = threading.Lock()

    def generate(self, *, system: str, prompt: str, schema: type[BaseModel],
                 purpose: str) -> Generation:
        with self._lock:
            self.calls.append(purpose)
        for prefix, responder in self.responders.items():
            if purpose.startswith(prefix):
                data = responder(prompt, schema)
                break
        else:
            data = self._fill(schema.model_json_schema(), purpose, prompt)
            if purpose.startswith("check:"):
                data.update(self._verdict(purpose))
        output = data if isinstance(data, BaseModel) else schema.model_validate(data)
        text = output.model_dump_json()
        return Generation(output, max(1, len(system + prompt) // 4), max(1, len(text) // 4),
                          "mock")

    def _verdict(self, purpose: str) -> dict[str, Any]:
        with self._lock:
            n = self._checker_calls.get(purpose, 0)
            self._checker_calls[purpose] = n + 1
        k = self.checker_approves_after
        approved = k is None or (k >= 0 and n >= k)
        return {
            "approved": approved,
            "score": 0.9 if approved else 0.4,
            "unmet_criteria": [] if approved else ["mock: criterion not yet satisfied"],
            "feedback": "Looks good." if approved else f"Revision {n + 1}: add more detail.",
        }

    def _fill(self, schema: dict[str, Any], purpose: str, prompt: str) -> dict[str, Any]:
        defs = schema.get("$defs", {})
        digest = hashlib.sha1(prompt.encode()).hexdigest()[:6]

        def value(s: dict[str, Any], name: str) -> Any:
            if "$ref" in s:
                return value(defs[s["$ref"].split("/")[-1]], name)
            if "anyOf" in s:
                non_null = [x for x in s["anyOf"] if x.get("type") != "null"]
                return value(non_null[0], name) if non_null else None
            if "enum" in s:
                return s["enum"][0]
            if "const" in s:
                return s["const"]
            t = s.get("type")
            if t == "object":
                props = s.get("properties", {})
                return {k: value(v, k) for k, v in props.items()}
            if t == "array":
                items = s.get("items", {})
                resolved = defs[items["$ref"].split("/")[-1]] if "$ref" in items else items
                enum_props = {k: v["enum"] for k, v in resolved.get("properties", {}).items()
                              if "enum" in v}
                if enum_props:  # one item per allowed value, e.g. one task per subagent
                    key, options = next(iter(enum_props.items()))
                    return [{**value(resolved, name), key: opt} for opt in options]
                return [value(items, name)]
            if t == "boolean":
                return True
            if t == "integer":
                return 1
            if t == "number":
                return 0.8
            return f"[mock {purpose} #{digest}] {name}"

        return value(schema, "output")


# --------------------------------------------------------------------------- metering


class MeteredModel:
    """Wraps a client; counts usage and enforces the run's budgets."""

    def __init__(self, client: ModelClient, policies: Policies,
                 on_call: Callable[[str, Generation], None] | None = None) -> None:
        self.client, self.policies = client, policies
        self.usage = Usage()
        self.on_call = on_call
        self._lock = threading.Lock()

    def generate(self, *, system: str, prompt: str, schema: type[BaseModel],
                 purpose: str) -> BaseModel:
        with self._lock:
            if self.usage.model_calls >= self.policies.max_model_calls:
                raise BudgetExceeded(
                    f"model call budget of {self.policies.max_model_calls} exhausted")
            if self.usage.total_tokens >= self.policies.max_total_tokens:
                raise BudgetExceeded(
                    f"token budget of {self.policies.max_total_tokens} exhausted")
            self.usage.model_calls += 1
        gen = self.client.generate(system=system, prompt=prompt, schema=schema, purpose=purpose)
        with self._lock:
            self.usage.input_tokens += gen.input_tokens
            self.usage.output_tokens += gen.output_tokens
        if self.on_call:
            self.on_call(purpose, gen)
        return gen.output


def make_model_client(config: ModelConfig, ollama_base_url: str = "http://127.0.0.1:11434",
                      ollama_model: str | None = None) -> ModelClient:
    if config.provider == "anthropic":
        return AnthropicModelClient(config.model, config.effort, config.max_tokens)
    if config.provider == "ollama":
        return OllamaModelClient(ollama_model or config.model, ollama_base_url)
    if config.provider == "mock":
        return MockModelClient()
    raise ModelError(f"unknown provider {config.provider!r}")


def to_prompt_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)
