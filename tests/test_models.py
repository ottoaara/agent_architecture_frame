from types import SimpleNamespace

import pytest

from agent_factory.contracts import CHECKER_OUTPUT_SCHEMA, FieldSpec, Policies, build_model
from agent_factory.models import (
    AnthropicModelClient,
    BudgetExceeded,
    MeteredModel,
    MockModelClient,
    ModelRefusal,
)

Out = build_model("Out", [FieldSpec(name="title"), FieldSpec(name="n", type="integer"),
                          FieldSpec(name="tags", type="string_list")])
Verdict = build_model("Verdict", CHECKER_OUTPUT_SCHEMA)


def test_mock_fills_schema_deterministically():
    m = MockModelClient()
    a = m.generate(system="s", prompt="p", schema=Out, purpose="agent:x").output
    b = m.generate(system="s", prompt="p", schema=Out, purpose="agent:x").output
    assert a == b and a.n == 1 and len(a.tags) == 1


def test_mock_checker_policy():
    m = MockModelClient(checker_approves_after=2)
    verdicts = [m.generate(system="", prompt="", schema=Verdict, purpose="check:r").output.approved
                for _ in range(3)]
    assert verdicts == [False, False, True]
    never = MockModelClient(checker_approves_after=-1)
    assert not never.generate(system="", prompt="", schema=Verdict,
                              purpose="check:r").output.approved


def test_metered_model_enforces_call_budget():
    meter = MeteredModel(MockModelClient(), Policies(max_model_calls=2))
    meter.generate(system="", prompt="x", schema=Out, purpose="a")
    meter.generate(system="", prompt="x", schema=Out, purpose="b")
    with pytest.raises(BudgetExceeded):
        meter.generate(system="", prompt="x", schema=Out, purpose="c")
    assert meter.usage.model_calls == 2 and meter.usage.total_tokens > 0


class FakeSDK:
    def __init__(self, response):
        self.kwargs = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))
        self.response = response

    def _parse(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _response(stop_reason="end_turn", parsed=None):
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def test_anthropic_client_uses_structured_outputs_and_fallbacks():
    parsed = Out(title="t", n=2, tags=[])
    sdk = FakeSDK(_response(parsed=parsed))
    gen = AnthropicModelClient(client=sdk).generate(system="sys", prompt="hi", schema=Out,
                                                    purpose="agent:a")
    assert gen.output == parsed and (gen.input_tokens, gen.output_tokens) == (10, 5)
    kw = sdk.kwargs
    assert kw["model"] == "claude-opus-5-5" and kw["output_format"] is Out
    assert kw["output_config"] == {"effort": "medium"} and kw["fallbacks"] == "default"


def test_anthropic_client_surfaces_refusal():
    with pytest.raises(ModelRefusal):
        AnthropicModelClient(client=FakeSDK(_response("refusal"))).generate(
            system="", prompt="", schema=Out, purpose="agent:a")
