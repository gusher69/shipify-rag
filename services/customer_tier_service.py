"""Customer Tier Service — Phase 3.3 (2026-08-05, Conversation Intelligence
sprint). Deterministic, non-AI classification of a LINE customer's
CONVERSATION behavior into exactly 4 tiers: cold / warm / hot / negative.

This is a SEPARATE axis from profiles/manager.py::calc_segment() (the
existing purchase/order-based cold/warm/hot field) — see
migrations/035_phase3_conversation_intelligence.sql for why the two are
kept independent (segment answers "has this person bought before?";
conversation_tier answers "how is this conversation relationship going?").
Drives Prompt Studio's Customer Tier Prompt selection
(services/prompt_builder.py::get_active_prompt_for_tier).

No AI model computes this score — every input is a plain counter already
accumulated by profiles/manager.py::update_profile_from_turn(), combined
with a fixed, documented formula (per CLAUDE.md: deterministic
post-processing must stay separably labeled from AI-generated output).
Re-evaluated after every conversation turn, so a tier can move in either
direction as behavior changes — it never needs a human to manually
reclassify a customer.
"""
from typing import Dict

TIERS = ("cold", "warm", "hot", "negative")

# Tunable, documented weights — not derived from any model, safe to
# adjust here if real production data shows the bands are miscalibrated.
_FREQUENCY_WEIGHT = 2.0       # per conversation (capped at 10)
_ERP_USAGE_WEIGHT = 1.0       # per ERP-backed request (capped at 10) — proxy for purchase intent
_TOPIC_BREADTH_WEIGHT = 1.0   # per distinct topic asked about (capped at 10)
_POSITIVE_WEIGHT = 3.0        # per turn classified positive (see profiles/manager.py)
_NEGATIVE_WEIGHT = 5.0        # per turn classified negative
_COMPLAINT_WEIGHT = 10.0      # per detected complaint/legal-threat keyword hit

_HOT_THRESHOLD = 15.0
_WARM_THRESHOLD = 5.0


def compute_tier(profile: Dict) -> Dict:
    """Returns {"tier": str, "score": float} from a user_profiles row.
    Never raises — a malformed/partial profile just yields the lowest
    tier ("cold") with score 0.

    A customer with any complaint (and not clearly outweighed by positive
    turns), or more negative than positive turns overall, is always
    "negative" regardless of how frequently they engage — per spec,
    "Complaint should simply increase the Negative score," never let
    frequency alone promote a complaining customer into "hot"."""
    conversation_count = profile.get("conversation_count") or 0
    erp_requests_count = profile.get("erp_requests_count") or 0
    topic_count = len(profile.get("interested_topics") or [])
    positive_count = profile.get("positive_count") or 0
    negative_count = profile.get("negative_count") or 0
    complaint_count = profile.get("complaint_count") or 0

    score = (
        _FREQUENCY_WEIGHT * min(conversation_count, 10)
        + _ERP_USAGE_WEIGHT * min(erp_requests_count, 10)
        + _TOPIC_BREADTH_WEIGHT * min(topic_count, 10)
        + _POSITIVE_WEIGHT * positive_count
        - _NEGATIVE_WEIGHT * negative_count
        - _COMPLAINT_WEIGHT * complaint_count
    )

    if (complaint_count > 0 and negative_count >= positive_count) or \
       (negative_count > positive_count and negative_count > 0):
        tier = "negative"
    elif score >= _HOT_THRESHOLD:
        tier = "hot"
    elif score >= _WARM_THRESHOLD:
        tier = "warm"
    else:
        tier = "cold"

    return {"tier": tier, "score": round(score, 2)}


def update_tier_for_profile(line_user_id: str) -> Dict:
    """Re-computes and persists the tier for a LINE user, reading the
    freshest profile row (so it reflects stats update_profile_from_turn()
    just wrote in the same request). Never raises."""
    from profiles.manager import get_profile, supabase, TABLE
    profile = get_profile(line_user_id) or {}
    result = compute_tier(profile)
    try:
        supabase.table(TABLE).update({
            "conversation_tier": result["tier"], "tier_score": result["score"],
        }).eq("line_user_id", line_user_id).execute()
    except Exception as e:
        print(f"[customer_tier_service] update_tier_for_profile failed: {e}")
    return result
