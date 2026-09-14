# -*- coding: utf-8 -*-
"""Test-package bootstrap — PHASE 6 K (deterministic test environment).

Python imports this module before ANY `tests.test_*` module, so this is
the single reproducible place to pin the test tier. It replaces both the
per-file ``os.environ.setdefault("OPENAI_API_KEY", ...)`` convention
(present in only 21 of ~194 test files) and the ad-hoc
``env OPENAI_API_KEY=sk-invalid-… python -m unittest …`` prefix that had
to be typed by hand before every run.

TWO EXPLICIT TIERS
------------------
OFFLINE_REGRESSION (the default, what CI / the Regression Gate runs):
    every outbound OpenAI call degrades deterministically (401 ->
    the callers' own documented UNKNOWN / no-embedding fallbacks). No
    paid API call can happen by accident, regardless of what the local
    .env holds — the previous behaviour, where config.py's load_dotenv()
    picked up a REAL key and a test file without its own guard silently
    billed live traffic, is exactly the non-determinism this removes.

LIVE_TIER_EVALUATION (explicit opt-in):
    SHIPIFY_LIVE_TIER=1 python -m unittest tests.<module>
    Leaves whatever key the environment/.env provides untouched, so the
    gated LLM family resolver and real embeddings are exercised. Only
    for deliberate live-tier evaluation runs; never for regression.

`is_live_tier()` lets an individual test skip itself in the wrong tier.
"""
import os

LIVE_TIER_ENV_VAR = "SHIPIFY_LIVE_TIER"
_OFFLINE_SENTINEL_KEY = "sk-invalid-offline-regression-tier"


def is_live_tier() -> bool:
    """True only when the operator explicitly asked for the live tier."""
    return os.environ.get(LIVE_TIER_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def _pin_offline_tier() -> None:
    # Direct assignment, not setdefault: a real key inherited from the
    # shell or loaded out of .env by config.py is precisely what made
    # offline runs non-deterministic, so the offline tier overrides it.
    os.environ["OPENAI_API_KEY"] = _OFFLINE_SENTINEL_KEY
    # Same reasoning for the alternate providers the LLM/embedding
    # services can be pointed at — the offline tier must not be able to
    # reach ANY paid backend by accident.
    for alt in ("AZURE_OPENAI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        if os.environ.get(alt):
            os.environ[alt] = _OFFLINE_SENTINEL_KEY


if not is_live_tier():
    _pin_offline_tier()
