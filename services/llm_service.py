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


# Bounded transport timeout for the chat client (latency P0, 2026-08-31).
# Production evidence: a healthy gpt-4o-mini call is ~1-3s, but the client
# was built with NO timeout -> an effective 600s read, so an
# intermittently half-open keep-alive connection to api.openai.com stalled
# one real LINE turn ~83s before recovering. read=20s caps a dead call;
# connect/pool are short because a reachable endpoint accepts immediately.
_CHAT_TIMEOUT_KW = dict(connect=5.0, read=20.0, write=10.0, pool=5.0)


def _build_openai_client(api_key: str):
    import httpx
    from openai import OpenAI
    # max_retries=0: the SDK's own retry would reuse the same (possibly
    # dead) connection pool; we do exactly ONE application-level retry
    # with a FRESH client instead (see generate()), so a stalled turn is
    # 1 original + 1 retry, never more.
    return OpenAI(api_key=api_key, timeout=httpx.Timeout(**_CHAT_TIMEOUT_KW), max_retries=0)


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = _build_openai_client(api_key)

    def generate(self, messages: List[Dict], *, model: str, temperature: float = 0.3,
                 max_tokens: int = 500) -> LLMResponse:
        from openai import APITimeoutError, APIConnectionError
        t0 = time.time()
        try:
            resp = self._client.chat.completions.create(
                model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
            )
        except (APITimeoutError, APIConnectionError) as first:
            # Stale/half-open connection to api.openai.com — discard this
            # client (fresh pool), retry ONCE. If the retry also fails,
            # reset the module singleton so the NEXT turn starts clean and
            # re-raise: the existing caller (RAG orchestrator / Decision
            # Engine) already degrades safely and never shows a raw
            # network error to the LINE customer.
            print(f"[llm_service] OpenAI chat transport error ({first!r}) — fresh client, one retry")
            try:
                self._client = _build_openai_client(self._api_key)
            except Exception:
                reset_llm_service()
                raise first
            try:
                resp = self._client.chat.completions.create(
                    model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
                )
            except (APITimeoutError, APIConnectionError) as second:
                reset_llm_service()
                raise second
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
