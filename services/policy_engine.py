"""PolicyEngine — evaluates a resolved AI Policy set (services/
policy_studio_service.py) against a question/answer before/after the LLM
call. Each policy SECTION (Business/Knowledge/Escalation/Attachment/
Channel Rules) is a real function that reads the policy set's config and
returns a verdict — never a hardcoded "5 policies active" display.

Escalation keyword-matching mirrors the exact handoff logic already live
in line_bot/webhook.py (see get_escalation_settings() below, which both
that file and this one now read from), so the Playground reflects real
production behavior, not a fabricated demo.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from services.policy_studio_service import get_default_policy_set, DEFAULT_CONFIG

# Same keyword list already used for LINE OA smart-handoff — kept here as
# the single source of truth so Playground and production never silently
# drift apart. A policy set's escalation_rules do NOT let an admin edit
# this specific keyword list yet (no technical rule builder per spec);
# they only control whether/how escalation triggers.
DISSATISFACTION_KEYWORDS = ["ต่อรอง", "ราคาพิเศษ", "ร้องเรียน", "ไม่พอใจ", "เอาเรื่อง"]


@dataclass
class PolicyVerdict:
    name: str
    status: str          # "applied" | "triggered" | "skipped"
    detail: str


@dataclass
class PolicyResult:
    verdicts: List[PolicyVerdict]
    escalate: bool
    notes: List[str]        # human-readable notes fed into the prompt (see PromptBuilderService)
    policy_set_name: str = "Standard Policy"
    escalation_message: Optional[str] = None

    @property
    def active_count(self) -> int:
        return sum(1 for v in self.verdicts if v.status != "skipped")


def business_rule_notes(rules: Dict) -> List[str]:
    notes = []
    if rules.get("no_guess_prices"):
        notes.append("Never guess or estimate a price — state only prices found in the Context.")
    if rules.get("no_guess_delivery_status"):
        notes.append("Never guess a delivery/shipping status — state only what the Context confirms.")
    if rules.get("no_answer_unavailable_info"):
        notes.append("If the requested information is not available, say so plainly instead of guessing.")
    if rules.get("require_approved_knowledge"):
        notes.append("Only answer using approved knowledge sources — do not use outside/general knowledge.")
    return notes


def knowledge_rule_notes(rules: Dict) -> List[str]:
    notes = []
    if rules.get("use_rag_first"):
        notes.append("Prefer the retrieved Knowledge Base context over any other source.")
    if rules.get("cite_source"):
        notes.append("Cite the source document when possible.")
    if rules.get("say_if_no_info"):
        notes.append("If no reliable information exists in the Context, clearly say so.")
    if rules.get("prefer_latest_document"):
        notes.append("When sources conflict, prefer the most recently approved document.")
    return notes


def channel_rule_notes(rules: Dict) -> List[str]:
    length_map = {"short": "Keep replies short.", "standard": "Use a standard reply length.",
                  "detailed": "Provide detailed, thorough replies."}
    emoji_map = {"never": "Never use emoji.", "sometimes": "Use emoji sparingly, only where natural.",
                 "allowed": "Emoji are welcome where appropriate."}
    formality_map = {"formal": "Use a formal tone.", "friendly": "Use a warm, friendly tone.",
                      "neutral": "Use a neutral, plain tone."}
    notes = []
    if rules.get("line_response_length") in length_map:
        notes.append(length_map[rules["line_response_length"]])
    if rules.get("emoji_usage") in emoji_map:
        notes.append(emoji_map[rules["emoji_usage"]])
    if rules.get("formality") in formality_map:
        notes.append(formality_map[rules["formality"]])
    return notes


def get_escalation_settings(policy_set: Optional[Dict] = None) -> Dict:
    """Single source of truth for escalation config — used by both this
    module's evaluate() (AI Playground) and line_bot/webhook.py (real LINE
    OA traffic), so the two can never silently diverge."""
    policy_set = policy_set or get_default_policy_set()
    rules = (policy_set.get("config") or {}).get("escalation_rules", DEFAULT_CONFIG["escalation_rules"])
    return {
        "enabled": rules.get("enabled", True),
        "confidence_threshold": rules.get("confidence_threshold", 0.5),
        "message": rules.get("message") or DEFAULT_CONFIG["escalation_rules"]["message"],
        "escalate_on_no_answer": rules.get("escalate_on_no_answer", True),
        "escalate_on_dissatisfaction": rules.get("escalate_on_dissatisfaction", True),
    }


def get_messaging_settings(policy_set: Optional[Dict] = None) -> Dict:
    """Single source of truth for services/message_segmenter.py's
    business-user controls (Reply Mode / Maximum Messages / Message
    Delay) — same fallback convention as get_escalation_settings() above,
    so an old policy set missing "messaging_rules" entirely still
    resolves to sensible defaults instead of crashing."""
    policy_set = policy_set or get_default_policy_set()
    rules = (policy_set.get("config") or {}).get("messaging_rules", DEFAULT_CONFIG["messaging_rules"])
    return {
        "reply_mode": rules.get("reply_mode") or DEFAULT_CONFIG["messaging_rules"]["reply_mode"],
        "max_messages": rules.get("max_messages") or DEFAULT_CONFIG["messaging_rules"]["max_messages"],
        "message_delay": rules.get("message_delay") or DEFAULT_CONFIG["messaging_rules"]["message_delay"],
    }


def evaluate(question: str, confidence_score: Optional[float] = None,
             policy_set: Optional[Dict] = None) -> PolicyResult:
    policy_set = policy_set or get_default_policy_set()
    config = policy_set.get("config") or DEFAULT_CONFIG
    verdicts: List[PolicyVerdict] = []
    notes: List[str] = []
    escalate = False

    esc = get_escalation_settings(policy_set)

    # ── Escalation Rules ──
    if not esc["enabled"]:
        verdicts.append(PolicyVerdict("Escalation Rules", "skipped", "Escalation is turned off for this policy set"))
    else:
        dissatisfaction_hit = next((kw for kw in DISSATISFACTION_KEYWORDS if kw in question), None)
        if dissatisfaction_hit and esc["escalate_on_dissatisfaction"]:
            verdicts.append(PolicyVerdict("Escalation Rules", "triggered",
                                          f"Customer appears dissatisfied (matched {dissatisfaction_hit!r})"))
            escalate = True
        elif (confidence_score is not None and confidence_score < esc["confidence_threshold"]
              and esc["escalate_on_no_answer"]):
            verdicts.append(PolicyVerdict("Escalation Rules", "triggered",
                                          f"Confidence {confidence_score:.2f} is below the "
                                          f"{esc['confidence_threshold']:.2f} threshold — no reliable answer found"))
            escalate = True
        else:
            verdicts.append(PolicyVerdict("Escalation Rules", "applied", "No escalation condition matched"))

    if escalate:
        notes.append(f"This question triggered escalation — hand off to a human. Use this message: {esc['message']!r}")

    # ── Business Rules ──
    business_notes = business_rule_notes(config.get("business_rules", {}))
    verdicts.append(PolicyVerdict(
        "Business Rules", "applied",
        "; ".join(business_notes) if business_notes else "No business constraints configured"))
    notes.extend(business_notes)

    # ── Knowledge Rules ──
    knowledge_notes = knowledge_rule_notes(config.get("knowledge_rules", {}))
    verdicts.append(PolicyVerdict(
        "Knowledge Rules", "applied",
        "; ".join(knowledge_notes) if knowledge_notes else "No knowledge-scope restrictions configured"))
    notes.extend(knowledge_notes)

    # ── Attachment Rules — informational verdict; actual enforcement
    #    happens where attachments are assembled (line_bot/webhook.py). ──
    att = config.get("attachment_rules", {})
    att_desc = []
    if att.get("text_first"):
        att_desc.append("text sent before attachments")
    if att.get("send_image_if_available"):
        att_desc.append("images sent when available")
    if att.get("send_file_link_if_available"):
        att_desc.append("file links sent when available")
    if att.get("skip_broken_attachments"):
        att_desc.append("missing/broken attachments skipped")
    verdicts.append(PolicyVerdict("Attachment Rules", "applied",
                                  "; ".join(att_desc) if att_desc else "No attachment restrictions configured"))

    # ── Channel Rules ──
    channel_notes = channel_rule_notes(config.get("channel_rules", {}))
    verdicts.append(PolicyVerdict(
        "Channel Rules", "applied",
        "; ".join(channel_notes) if channel_notes else "No channel-specific overrides configured"))
    notes.extend(channel_notes)

    return PolicyResult(verdicts=verdicts, escalate=escalate, notes=notes,
                         policy_set_name=policy_set.get("name") or "Standard Policy",
                         escalation_message=esc["message"] if escalate else None)
