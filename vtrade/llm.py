"""Optional Claude reviewer: approves or vetoes each proposed entry before it is sent.

The ML model decides *when* a setup looks good; Claude acts as a second opinion that can veto
entries into conditions the model handles poorly (volatility spikes, overextended moves, poor
reward/risk). It never opens trades on its own and is not used in backtests, because the model
may already know how historical prices played out.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from vtrade.config import LLMConfig

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the risk reviewer for an automated spot crypto trading bot.

A gradient-boosted model has proposed a long entry. You receive the model's probability, the
proposed stop-loss, take-profit and position size, current indicator values and recent candles.
You have no news feed, so judge only from the data given.

Approve when the setup is consistent with the signal. Veto when the data shows risk the model may
underweight, for example: a volatility spike far above normal, price stretched well above its
moving averages after a vertical move, entering straight into a sharp sell-off, or a stop so tight
relative to recent ranges that it is likely to be hit by noise.

Set confidence between 0 and 1 for how sure you are of your decision. Keep reasoning to two or
three sentences that name the specific numbers you relied on."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "veto"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["decision", "confidence", "reasoning"],
    "additionalProperties": False,
}

# Models documented to accept server-side `fallbacks: "default"`.
_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}


@dataclass(frozen=True)
class Review:
    approved: bool
    confidence: float
    reasoning: str
    error: bool = False


class ClaudeAnalyst:
    def __init__(self, cfg: LLMConfig, client: Any = None):
        self.cfg = cfg
        if client is None:
            import anthropic

            client = anthropic.Anthropic(timeout=180.0, max_retries=2)
        self.client = client

    def _request(self, context: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": 16000,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": "Review this proposed entry:\n\n" + json.dumps(context, indent=2, default=str),
                }
            ],
            "output_config": {"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
        }
        if not self.cfg.model.startswith("claude-haiku"):  # Haiku 4.5 has no adaptive thinking / effort
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"]["effort"] = self.cfg.effort
        if self.cfg.model in _FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            response = self.client.beta.messages.create(**kwargs)
        else:
            response = self.client.messages.create(**kwargs)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise RuntimeError(f"Claude declined the review: {getattr(details, 'category', None)}")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude review hit max_tokens before finishing")
        text = next(block.text for block in response.content if block.type == "text")
        return json.loads(text)

    def review(self, context: dict[str, Any]) -> Review:
        import anthropic

        try:
            data = self._request(context)
            confidence = min(max(float(data["confidence"]), 0.0), 1.0)
            approved = data["decision"] == "approve" and confidence >= self.cfg.min_confidence
            reasoning = str(data["reasoning"]).strip()
            if data["decision"] == "approve" and not approved:
                reasoning = f"[approval below min_confidence {self.cfg.min_confidence}] {reasoning}"
            return Review(approved, confidence, reasoning)
        except anthropic.AuthenticationError:
            reason = "Claude authentication failed - set ANTHROPIC_API_KEY in .env"
        except anthropic.RateLimitError:
            reason = "Claude rate limit hit"
        except anthropic.APIStatusError as exc:
            reason = f"Claude API error {exc.status_code}: {exc.message}"
        except anthropic.APIConnectionError:
            reason = "Could not reach the Claude API"
        except (RuntimeError, StopIteration, KeyError, ValueError, TypeError) as exc:
            reason = f"Unusable Claude review: {exc}"
        log.warning("%s (on_error=%s)", reason, self.cfg.on_error)
        return Review(self.cfg.on_error == "allow", 0.0, reason, error=True)
