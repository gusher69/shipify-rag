"""CS-03 — Production Human CS Behavior Validation & Final Tuning.

Creates (or updates in place, idempotently) a NEW Prompt Studio version,
versioned FROM the CS-02 candidate (tools/seed_cs02_human_style_prompt.py,
id ba11ab4f-6616-42cc-9666-297baeb22d04) via the existing
services/prompt_studio_service.py::save_as_new_version() mechanism — never
a destructive overwrite of CS-02, which remains fully intact for rollback.

CS-03's 53-scenario evaluation (single-turn + multi-turn, run through the
real active-channel prompt-resolution path) found the CS-02 wording already
correct for the large majority of behavior (directness, greeting/reference
continuity, correction, change-of-mind, known/pending/completed status
distinctions, multi-order/multi-intent structuring, refund-state wording,
cancellation/shipping-change truthfulness). Four reproducible gaps were
found and are fixed here with MINIMAL wording changes only — three existing
RESPONSE_RULES keys are reinforced, one new key is added. No business fact,
tone block, or unrelated rule is touched (FACTS CHANGED: NO).

1. "unknown_information_wording" (NEW key) — CS-03 Category T found the
   model reliably (4/4 reproductions) answered "no information" questions
   with wording that names "ฐานความรู้" (knowledge base) — an
   internal-system-sounding term the CS-03 spec explicitly flags as an
   undesired/robotic pattern, in favor of a natural
   "ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ" style. CS-02's RESPONSE_RULES had no
   rule governing this specific phrasing choice at all.

2. "urgency_acknowledgement" (extended) — CS-03 Category H found the model
   reliably (3/3 reproductions) skipped urgency acknowledgement entirely
   for an INDIRECT urgency signal ("ลูกค้าผมตามของอยู่ครับ" — pressure from
   the customer's own downstream customer) that doesn't match the rule's
   original two literal example phrases ("รีบใช้", "ตามมาหลายวันแล้ว").
   Broadened the example set to explicitly cover indirect/contextual
   urgency, not just the two literal phrases.

3. "complaint_tone" (extended) — CS-03 Category I found the model
   inconsistently (2/4 reproductions) skipped acknowledging an inconvenience
   complaint ("เสียเวลาไปรับของหลายรอบมากครับ") that isn't phrased as a
   formal ร้องเรียน/เคลม, jumping straight to a status update with generic
   proactive filler instead. Broadened to explicitly cover inconvenience-style
   complaints (เสียเวลา, ต้องไปหลายรอบ, รอนาน), not just formal complaints/claims.

4. "customer_reported_vs_verified" (extended) — CS-03 Category L found the
   model occasionally (1/4 reproductions) reused the exact "จริงๆ" wording
   CS-02 had already fixed for a single-item case, this time for a
   MULTIPLE-missing-items case the original CS-02 test never covered.
   Section 32 of the CS-03 spec lists "unverified customer complaint stated
   as verified fact" as a hard-fail condition, so this reproducibility rate
   (not 0%) is treated as confirmed and worth reinforcing even though it is
   not deterministic. Generalized beyond the single literal word "จริงๆ" to
   synonyms, and made explicit that the rule applies identically whether one
   item or many are involved.

Does NOT activate the candidate for real traffic by itself — see CS-03's
own final report for the explicit assign_channel(...) call and evaluation
gate this version must pass before activation.

Usage:
    python -m tools.seed_cs03_human_style_prompt_v2
"""
import services.prompt_studio_service as pss
from tools.seed_cs02_human_style_prompt import (
    CANDIDATE_SYSTEM_PROMPT, CANDIDATE_TONE, RESPONSE_RULES as CS02_RESPONSE_RULES,
)

# The CS-02 candidate this version is versioned FROM — the currently ACTIVE
# LINE OA template (confirmed live via ai_prompt_assignments during CS-03's
# own baseline check). CS-02 itself remains fully intact and rollback-able.
CS02_TEMPLATE_ID = "ba11ab4f-6616-42cc-9666-297baeb22d04"

CANDIDATE_NAME = "Human CS Style (LINE OA) v2 — CS-03"

CANDIDATE_DESCRIPTION = (
    "CS-03 candidate: minimal wording reinforcement on top of CS-02 — natural "
    "'no information' phrasing (no 'ฐานความรู้'), broader urgency/inconvenience "
    "acknowledgement coverage, stronger customer-reported-vs-verified wording "
    "for multi-item cases. No business fact changed. Not assigned to any "
    "channel until explicitly activated via assign_channel('LINE OA', <this id>)."
)

# CANDIDATE_SYSTEM_PROMPT is imported unchanged from CS-02 above — CS-03
# made no tone-block changes.

# Start from CS-02's rules, unchanged keys stay byte-identical; only the 3
# listed keys are reworded and 1 new key is added.
RESPONSE_RULES = dict(CS02_RESPONSE_RULES)

RESPONSE_RULES["urgency_acknowledgement"] = (
    "หากลูกค้าแสดงความเร่งด่วนหรือความกดดัน ไม่ว่าจะพูดตรงๆ (เช่น รีบใช้ ตามมาหลายวันแล้ว) "
    "หรือพูดถึงผลกระทบทางอ้อม เช่น 'ลูกค้าผมตามของอยู่ครับ' หรือ 'ลูกค้าของตัวเองกำลังตามเรื่องอยู่' "
    "(หมายถึงลูกค้าของผู้ถามเองกำลังกดดันผู้ถามอยู่ ไม่ใช่เรื่องที่ไม่เกี่ยวข้อง) "
    "ให้ขึ้นต้นคำตอบด้วยการรับทราบความเร่งด่วนนั้นโดยเฉพาะเจาะจงก่อนเสมอ (เช่น 'เข้าใจว่าคุณมีลูกค้าที่รอติดตามอยู่ครับ') "
    "ตามด้วยสถานะปัจจุบันและขั้นตอนถัดไปที่ทำได้จริง "
    "ห้ามข้ามการรับทราบไปตอบแค่สถานะเฉยๆ โดยไม่พูดถึงความเร่งด่วนที่ลูกค้าแจ้งเลย "
    "และห้ามใช้ประโยคทักทายทั่วไปแบบร่าเริงแทนการรับทราบ"
)

RESPONSE_RULES["complaint_tone"] = (
    "ระหว่างเคสร้องเรียน เคลม หรือลูกค้าบ่นถึงความไม่สะดวก (เช่น เสียเวลา ต้องไปหลายรอบ รอนาน) "
    "ห้ามใช้อิโมจิร่าเริงหรือน้ำเสียงสดใสเกินไป ให้รับทราบปัญหาที่เฉพาะเจาะจงก่อนเสมอ "
    "(ห้ามตอบแค่สถานะเฉยๆ โดยไม่พูดถึงปัญหาที่ลูกค้าแจ้ง) ระบุข้อเท็จจริงที่ยืนยันได้ และบอกขั้นตอนถัดไป "
    "โดยไม่ขอโทษซ้ำเกินความจำเป็น"
)

RESPONSE_RULES["customer_reported_vs_verified"] = (
    "เมื่อสรุปข้อมูลที่ลูกค้าแจ้งเอง (เช่น จำนวนที่ได้รับ, ของผิด, ของเสียหาย, ของขาดหลายรายการ) "
    "ให้สรุปกลับไปตามที่ลูกค้าแจ้งเท่านั้น และถามยืนยันหากจำเป็น ห้ามใช้คำว่า 'จริงๆ' หรือคำยืนยันอื่นที่ความหมายเดียวกัน "
    "(เช่น 'แน่นอน' 'ยืนยันว่า') เพื่อบอกว่าเป็นข้อเท็จจริงที่ตรวจสอบแล้ว เว้นแต่มีหลักฐานจากระบบยืนยันแล้วเท่านั้น "
    "กฎนี้ใช้เหมือนกันไม่ว่าจะเป็นรายการเดียวหรือหลายรายการ"
)

RESPONSE_RULES["unknown_information_wording"] = (
    "เมื่อไม่มีข้อมูลสำหรับคำถามของลูกค้า ให้ตอบด้วยประโยคธรรมชาติ เช่น 'ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ' "
    "ห้ามพูดคำว่า 'ฐานความรู้' หรือคำศัพท์ระบบภายในอื่นๆ กับลูกค้าโดยเด็ดขาด"
)


def main():
    svc = pss.PromptStudioService()
    existing = next((v for v in svc.list_versions(CS02_TEMPLATE_ID) if v.get("name") == CANDIDATE_NAME), None)

    if existing:
        updated = svc.update_prompt(existing["id"], {
            "description": CANDIDATE_DESCRIPTION, "tone": CANDIDATE_TONE,
            "system_prompt": CANDIDATE_SYSTEM_PROMPT, "response_rules": RESPONSE_RULES,
        })
        print(f"Updated existing candidate {CANDIDATE_NAME!r} in place (id={updated['id']}, "
              f"version={updated['version']})")
        return updated

    created = svc.save_as_new_version(CS02_TEMPLATE_ID, {
        "name": CANDIDATE_NAME, "description": CANDIDATE_DESCRIPTION, "tone": CANDIDATE_TONE,
        "system_prompt": CANDIDATE_SYSTEM_PROMPT, "response_rules": RESPONSE_RULES,
    })
    print(f"Created candidate {CANDIDATE_NAME!r} (id={created['id']}, version={created['version']}, "
          f"parent_id={created['parent_id']}) — NOT assigned to any channel yet.")
    return created


if __name__ == "__main__":
    main()
