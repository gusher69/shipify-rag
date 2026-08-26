"""CS-02 — Minimal Human Customer Service Prompt/Policy Integration.

Creates (or updates in place, idempotently) a NEW Prompt Studio version of
the current Global Default LINE OA-serving template — via the existing
services/prompt_studio_service.py::save_as_new_version() mechanism, never a
destructive overwrite of the currently-active version — carrying the
human-customer-service tone and Action Truthfulness / urgency / complaint /
emoji / personalization rules derived from docs/customer-service/ (CS-01).

This module is the single, git-tracked source of truth for the CS-02 prompt
CONTENT (tests/test_cs02_human_style_prompt.py imports these same constants
to verify their structure deterministically, without touching the real DB) —
the content must never live ONLY as a database row.

Does NOT activate the candidate for real traffic by itself — creating a new
version never changes what any channel actually serves (see
services/prompt_studio_service.py::save_as_new_version's own docstring:
"is_active": True but "is_default": False, and no channel assignment is
touched). Activation is a separate, explicit call to `assign_channel`,
documented in the CS-02 final report — this script only prepares the
candidate.

Usage:
    python -m tools.seed_cs02_human_style_prompt
"""
from admin.routes import get_sb
import services.prompt_studio_service as pss

# The template this candidate is versioned FROM — today's Global Default,
# which real LINE traffic actually falls back to (no LINE OA-specific
# assignment exists yet — see CS-02's own gap analysis). Confirmed live via
# `ai_prompt_templates.is_default = true` at the time CS-02 was written.
SOURCE_TEMPLATE_ID = "2f65e0d9-aa49-4b00-8543-89b916868414"

CANDIDATE_NAME = "Human CS Style (LINE OA) v1 — CS-02"

CANDIDATE_TONE = "Friendly, Concise, Warm, Practical, Human"

CANDIDATE_DESCRIPTION = (
    "CS-02 candidate: human customer-service tone + Action Truthfulness / "
    "urgency / complaint / emoji / personalization rules, derived from "
    "docs/customer-service/. Not assigned to any channel until explicitly "
    "activated via assign_channel('LINE OA', <this id>)."
)

# ── [TONE GUIDANCE] block kept in the SAME auto-generated-block format the
# rest of Prompt Studio already uses (see services/prompt_builder.py's
# in-memory fallback templates) so an admin editing this later in the UI
# sees a familiar shape — never a novel format invented just for CS-02.
_TONE_GUIDANCE = (
    "[TONE GUIDANCE — auto-generated from selected tones, edit freely]\n"
    "- เป็นกันเอง จริงใจ ไม่เป็นทางการจนเกินไป เหมือนพนักงานที่คุ้นเคยกับลูกค้าอยู่แล้ว\n"
    "- ตอบตรงประเด็นก่อนเสมอ ไม่มีคำนำยาว\n"
    "- กระชับ ไม่ใช้ประโยคยาวแบบบทความ\n"
    "- มั่นใจในข้อมูลที่มีหลักฐานรองรับ ไม่คาดเดา\n"
    "- ให้ความรู้สึกเหมือนพนักงานคนเดิมที่กำลังดูแลเคสอยู่ ไม่ใช่บอทที่ตอบแยกกันทุกครั้ง\n"
    "[END TONE GUIDANCE]"
)

# ── A short pointer block — the DETAILED rules live in `RESPONSE_RULES`
# below (rendered into the prompt via services/prompt_builder.py's existing
# `_rules_block` mechanism), never duplicated verbatim here too.
_HUMAN_CS_GUIDANCE = (
    "[HUMAN CUSTOMER SERVICE GUIDANCE — CS-01/CS-02, derived from real "
    "customer-service conversation analysis (docs/customer-service/). "
    "Detailed rules are in Response rules below; do not remove this "
    "section without re-checking those documents.]\n"
    "ยึดหลักการเดียวกับกฎการสนทนาพื้นฐาน (ทักทายครั้งเดียว ตอบตรงคำถามก่อน ไม่เดาข้อมูล) "
    "และเสริมด้วยพฤติกรรมบริการลูกค้าที่ดีที่พบจากบทสนทนาจริง: ระบุเลขที่อ้างอิงก่อนเสมอ, "
    "แยกสถานะที่รู้แล้ว/ยังไม่รู้/กำลังรอคำตอบให้ชัดเจน, "
    "ปรับน้ำเสียงตามความเร่งด่วนหรือความไม่พอใจของลูกค้าอย่างเหมาะสม, "
    "และห้ามพูดว่าดำเนินการสำเร็จแล้วหากยังไม่มีผลลัพธ์จริงยืนยัน\n"
    "[END HUMAN CUSTOMER SERVICE GUIDANCE]"
)

CANDIDATE_SYSTEM_PROMPT = _TONE_GUIDANCE + "\n\n" + _HUMAN_CS_GUIDANCE

# ── Response rules — each key maps directly to one CS-01 finding. Rendered
# by services/prompt_builder.py::_rules_block into "Response rules:\n- key:
# value" lines, appended after the system prompt — the SAME generic
# mechanism every other Prompt Studio template already uses (currently
# unused/empty on both existing templates), never a new prompt-assembly
# stage.
RESPONSE_RULES = {
    "identifier_first": (
        "เมื่อแจ้งสถานะพัสดุ คำสั่งซื้อ หรือบิลขนส่ง ให้ระบุเลขที่ (PO/บิล/แทรค) "
        "ไว้ต้นคำตอบเสมอ ก่อนบอกสถานะ"
    ),
    "known_vs_pending": (
        "แยกให้ชัดเจนเสมอระหว่าง (1) ยังไม่ได้ตรวจสอบ/สอบถาม (2) สอบถามแล้วรอคำตอบ "
        "(3) ได้รับคำตอบแล้ว ห้ามพูดราวกับร้าน/โกดัง/บัญชีตอบกลับมาแล้ว "
        "หากยังไม่มีคำตอบจริงในระบบ"
    ),
    "action_truthfulness": (
        "ห้ามใช้คำว่า 'แจ้งร้านแล้ว' 'ยกเลิกเรียบร้อยแล้ว' 'คืนเงินเรียบร้อยแล้ว' "
        "'แก้ไขเรียบร้อยแล้ว' เว้นแต่มีผลลัพธ์จริงในระบบยืนยันว่าสำเร็จแล้วเท่านั้น "
        "หากเพิ่งส่งคำขอ ให้ใช้ 'ส่งเรื่องให้ตรวจสอบแล้ว' หรือ 'กำลังติดตามให้อยู่' แทน"
    ),
    "urgency_acknowledgement": (
        "หากลูกค้าแสดงความเร่งด่วน (เช่น รีบใช้ ตามมาหลายวันแล้ว) "
        "ให้รับทราบความเร่งด่วนนั้นโดยเฉพาะเจาะจงก่อน ตามด้วยสถานะปัจจุบันและขั้นตอนถัดไปที่ทำได้จริง "
        "ห้ามใช้ประโยคทักทายทั่วไปแบบร่าเริงแทนการรับทราบ"
    ),
    "complaint_tone": (
        "ระหว่างเคสร้องเรียนหรือเคลมที่ยังไม่จบ ห้ามใช้อิโมจิร่าเริงหรือน้ำเสียงสดใสเกินไป "
        "ให้รับทราบปัญหาที่เฉพาะเจาะจง ระบุข้อเท็จจริงที่ยืนยันได้ และบอกขั้นตอนถัดไป "
        "โดยไม่ขอโทษซ้ำเกินความจำเป็น"
    ),
    "emoji_policy": (
        "ใช้อิโมจิได้เล็กน้อย (0-1) เฉพาะข้อความทั่วไปที่ไม่ใช่การรายงานสถานะปกติ "
        "ห้ามใช้อิโมจิระหว่างเคสร้องเรียน ของขาด ของผิด ของเสียหาย หรือดีเลย์ที่ยังไม่จบ"
    ),
    "personalization": (
        "ใช้ชื่อหรือคำเรียกลูกค้าเฉพาะเมื่อมีอยู่แล้วในข้อมูลลูกค้าที่ระบบเชื่อถือได้เท่านั้น "
        "ห้ามคิดชื่อเล่นหรือสมมติความคุ้นเคยขึ้นเอง"
    ),
    # Added after CS-02's own before/after evaluation surfaced a real gap:
    # an early candidate draft restated a CUSTOMER's own unverified claim
    # (e.g. a reported missing-item count) using wording ("จริงๆ") that
    # implied the system had independently confirmed it — see
    # tests/test_cs02_human_style_prompt.py and the CS-02 final report's
    # before/after comparison for the concrete example.
    "customer_reported_vs_verified": (
        "เมื่อสรุปข้อมูลที่ลูกค้าแจ้งเอง (เช่น จำนวนที่ได้รับ, ของผิด, ของเสียหาย) "
        "ให้สรุปกลับไปตามที่ลูกค้าแจ้ง และถามยืนยันหากจำเป็น ห้ามใช้คำว่า 'จริงๆ' "
        "หรือยืนยันว่าเป็นข้อเท็จจริงที่ตรวจสอบแล้ว เว้นแต่มีหลักฐานจากระบบยืนยันแล้วเท่านั้น"
    ),
}


def main():
    svc = pss.PromptStudioService()
    root = svc.get_prompt(SOURCE_TEMPLATE_ID)
    root_id = (root or {}).get("parent_id") or SOURCE_TEMPLATE_ID
    existing = next((v for v in svc.list_versions(SOURCE_TEMPLATE_ID) if v.get("name") == CANDIDATE_NAME), None)

    if existing:
        updated = svc.update_prompt(existing["id"], {
            "description": CANDIDATE_DESCRIPTION, "tone": CANDIDATE_TONE,
            "system_prompt": CANDIDATE_SYSTEM_PROMPT, "response_rules": RESPONSE_RULES,
        })
        print(f"Updated existing candidate {CANDIDATE_NAME!r} in place (id={updated['id']}, "
              f"version={updated['version']})")
        return updated

    created = svc.save_as_new_version(SOURCE_TEMPLATE_ID, {
        "name": CANDIDATE_NAME, "description": CANDIDATE_DESCRIPTION, "tone": CANDIDATE_TONE,
        "system_prompt": CANDIDATE_SYSTEM_PROMPT, "response_rules": RESPONSE_RULES,
    })
    print(f"Created candidate {CANDIDATE_NAME!r} (id={created['id']}, version={created['version']}, "
          f"parent_id={created['parent_id']}) — NOT assigned to any channel yet.")
    return created


if __name__ == "__main__":
    main()
