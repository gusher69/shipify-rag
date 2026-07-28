from typing import Dict, Optional

from config import OPENAI_CHAT_MODEL
from services.llm_service import get_llm_service
from services.prompt_builder import build_prompt, get_active_prompt_for_channel
from services.policy_engine import channel_rule_notes, business_rule_notes, knowledge_rule_notes
from services.policy_studio_service import get_default_policy_set

LINE_OA_CHANNEL = "LINE OA"

# Segment-based tone hints stay here (LINE-specific personalization logic,
# not part of the reusable system prompt) and are passed into
# PromptBuilderService as a response rule so they show up in the
# Playground's Prompt tab like everything else — never concatenated
# ad hoc into the system prompt again.
_SEGMENT_NOTES = {
    "cold": "ลูกค้าใหม่ แนะนำบริการด้วย",
    "warm": "ลูกค้าเคยใช้บริการแล้ว ทักทายเป็นกันเอง",
    "hot":  "ลูกค้า VIP ประจำ ตอบแบบรู้จักกันดี",
}


def generate_reply(
    question: str,
    rag_context: str,
    erp_data: Optional[Dict],
    user_profile: Optional[Dict],
) -> Dict:
    """Real LINE OA customer traffic. The system prompt is NEVER
    hardcoded here — it always comes from Prompt Studio via
    get_active_prompt_for_channel("LINE OA"), falling back to Global
    Default if no LINE OA prompt is assigned (see
    services/prompt_builder.py)."""
    segment = user_profile.get("segment", "cold") if user_profile else "cold"
    segment_note = _SEGMENT_NOTES.get(segment, "")

    context_parts = []
    if rag_context:
        context_parts.append(f"ข้อมูลจาก Knowledge Base:\n{rag_context}")
    if erp_data and "error" not in erp_data:
        context_parts.append(f"ข้อมูลจาก ERP:\n{erp_data}")
    context = "\n\n".join(context_parts) if context_parts else ""

    # Default AI Policy set (services/policy_studio_service.py / AI
    # Policies UI) — same business/knowledge/channel rule notes the
    # Playground applies, so real LINE OA traffic honors an admin's
    # policy settings, not just a demo in the Playground.
    policy_config = (get_default_policy_set().get("config") or {})
    policy_notes = (
        business_rule_notes(policy_config.get("business_rules", {}))
        + knowledge_rule_notes(policy_config.get("knowledge_rules", {}))
        + channel_rule_notes(policy_config.get("channel_rules", {}))
    )
    if segment_note:
        policy_notes.append(f"Customer segment note: {segment_note}")

    template = get_active_prompt_for_channel(LINE_OA_CHANNEL)
    built = build_prompt(
        question, context, template=template,
        policy_notes=policy_notes or None,
    )

    try:
        llm = get_llm_service()
        response = llm.generate(built.messages, model=OPENAI_CHAT_MODEL, temperature=0.3, max_tokens=300)
        reply = response.text.strip()

        has_context = bool(rag_context or erp_data)
        confidence = 0.85 if has_context else 0.45

        return {"reply": reply, "confidence": confidence}
    except Exception:
        return {"reply": "ขออภัยค่ะ ระบบขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้งนะคะ", "confidence": 0.0}
