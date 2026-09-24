import json
from types import SimpleNamespace

from vtrade.config import LLMConfig
from vtrade.llm import ClaudeAnalyst


class FakeMessages:
    def __init__(self, payload, stop_reason="end_turn"):
        self.payload, self.stop_reason = payload, stop_reason
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        text = SimpleNamespace(type="text", text=json.dumps(self.payload))
        return SimpleNamespace(stop_reason=self.stop_reason, content=[text], stop_details=None)


def _client(messages):
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))


def test_approve_above_min_confidence_uses_fallbacks_and_schema():
    messages = FakeMessages({"decision": "approve", "confidence": 0.8, "reasoning": "fine"})
    review = ClaudeAnalyst(LLMConfig(enabled=True), _client(messages)).review({"x": 1})
    assert review.approved and review.confidence == 0.8 and not review.error
    kw = messages.kwargs
    assert kw["model"] == "claude-opus-5"
    assert kw["fallbacks"] == "default"
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["thinking"] == {"type": "adaptive"}


def test_low_confidence_approval_is_vetoed():
    messages = FakeMessages({"decision": "approve", "confidence": 0.3, "reasoning": "meh"})
    review = ClaudeAnalyst(LLMConfig(min_confidence=0.5), _client(messages)).review({})
    assert not review.approved


def test_refusal_follows_on_error_policy():
    messages = FakeMessages({}, stop_reason="refusal")
    assert not ClaudeAnalyst(LLMConfig(on_error="veto"), _client(messages)).review({}).approved
    review = ClaudeAnalyst(LLMConfig(on_error="allow"), _client(messages)).review({})
    assert review.approved and review.error


def test_malformed_output_is_an_error():
    messages = FakeMessages({"decision": "approve"})  # missing fields
    review = ClaudeAnalyst(LLMConfig(), _client(messages)).review({})
    assert review.error and not review.approved
