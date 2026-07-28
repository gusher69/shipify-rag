"""LLMService — the ONLY place in this codebase that should call an LLM
provider's chat-completion API directly.

Today there is exactly one provider (OpenAI, via the existing
OPENAI_API_KEY/OPENAI_CHAT_MODEL config already used by line_bot/tone.py).
Adding a new provider later (Claude, Gemini, OpenRouter, Ollama, Azure
OpenAI) means writing one new LLMProvider subclass and adding one branch
to get_llm_service() — callers (AI Playground, LINE webhook, anything
else) never change, since they only ever see LLMResponse.

Do NOT import `openai` anywhere else in the codebase — go through
get_llm_service().generate() instead.
"""
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    raw: Optional[Dict] = field(default=None, repr=False)  # provider's raw response, for Raw JSON tab / debugging


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, messages: List[Dict], *, model: str, temperature: float = 0.3,
                 max_tokens: int = 500) -> LLMResponse:
        raise NotImplementedError


# Rough per-1K-token USD pricing for cost estimation in the Playground —
# NOT billing-accurate, just enough to show an order-of-magnitude estimate.
# Update as needed; unknown models fall back to a conservative default.
_PRICING_PER_1K = {
    "gpt-4o":            {"input": 0.0025, "output": 0.010},
    "gpt-4o-mini":       {"input": 0.00015, "output": 0.0006},
    "gpt-4-turbo":       {"input": 0.010, "output": 0.030},
    "gpt-3.5-turbo":     {"input": 0.0005, "output": 0.0015},
}
_DEFAULT_PRICING = {"input": 0.005, "output": 0.015}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _PRICING_PER_1K.get(model, _DEFAULT_PRICING)
    return (input_tokens / 1000) * rates["input"] + (output_tokens / 1000) * rates["output"]


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)

    def generate(self, messages: List[Dict], *, model: str, temperature: float = 0.3,
                 max_tokens: int = 500) -> LLMResponse:
        t0 = time.time()
        resp = self._client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
        )
        latency_ms = (time.time() - t0) * 1000
        usage = resp.usage
        return LLMResponse(
            text=resp.choices[0].message.content or "",
            model=model, provider="openai",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency_ms,
            raw=resp.model_dump() if hasattr(resp, "model_dump") else None,
        )


_instance: Optional[LLMProvider] = None


def get_llm_service() -> LLMProvider:
    """Returns the currently configured LLMProvider. Today this is always
    OpenAI (per this project's current requirements — no provider
    selection UI yet), but callers must never assume that."""
    global _instance
    if _instance is None:
        from config import OPENAI_API_KEY
        _instance = OpenAIProvider(OPENAI_API_KEY)
    return _instance


def reset_llm_service():
    global _instance
    _instance = None
