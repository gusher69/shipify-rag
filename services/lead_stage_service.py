"""Lead Stage Service — P4 Cold / Warm / Hot lead analysis (2026-09-01).

An ANALYTICAL side-channel only. It never touches the reply path, never
calls an LLM or the network, and nothing in the Decision Engine, the
prompt builder or the RAG pipeline reads its output — the AI answer for a
given message is byte-identical with or without this module. (Deliberately
SEPARATE from user_profiles.conversation_tier, which DOES drive Prompt
Studio tier-prompt selection — see services/customer_tier_service.py. P4
must not change conversation behavior, so it gets its own columns.)

Model — deterministic signal accumulation, never one keyword:
  * Each real LINE turn contributes a set of SIGNALS, derived from the
    structured turn result the pipeline already produced (actionable
    intent + resolved entities via rag.query_*; routing type, selected
    Business Action and collected identifiers from the Decision Engine
    trace) plus a few explicit multi-word operational phrases.
  * Signals carry weights; the per-turn delta is capped and only ever
    added, so a follow-up like "ขอบคุณครับ" or an FAQ question cannot
    pull an already-earned stage down.
  * lead_score is durable 0..100. On a new turn after long inactivity it
    decays once (lazy — no scheduler). Stage bands: <30 COLD, 30..69
    WARM, >=70 HOT.
  * A turn whose own signals are decisive upgrades immediately: any
    "evaluating" signal floors the turn at WARM; any "operational" signal
    (real order/shipment Business Action, goods-at-warehouse, explicit
    "open a bill" / "ready to order", pay-this-item) floors it at HOT.
  * A bare private-identity lookup (GetDataCustomer — "what's my
    registered phone?") is explicitly NOT operational: it adds only a
    small verified-customer signal and never floors to HOT.
"""
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

LEAD_STAGES = ("COLD", "WARM", "HOT")

_COLD_MAX = 29
_WARM_MAX = 69
_PER_TURN_DELTA_CAP = 45
_SCORE_CAP = 100
_DECAY_AFTER_DAYS = 14.0
_DECAY_FACTOR = 0.7
_MAX_REASONS = 6

# ── Signal weights (tunable, not model-derived) ───────────────────────
_WEIGHTS: Dict[str, int] = {
    "general_inquiry": 5,
    "import_interest": 10,
    "product_identified": 20,
    "eligibility_check": 15,
    "rate_interest": 15,
    "duration_interest": 15,
    "process_interest": 10,
    "transport_interest": 10,
    "transport_comparison": 10,
    "warehouse_interest": 10,
    "quantity_known": 10,
    "verified_customer_lookup": 5,     # private identity/info lookup — NOT operational
    # operational (each also floors the turn at HOT)
    "active_order_intent": 30,
    "active_shipment": 30,
    "goods_at_warehouse": 30,
    "payment_for_item": 25,
}

_EVALUATING_SIGNALS = frozenset({
    "product_identified", "eligibility_check", "rate_interest", "duration_interest",
    "process_interest", "transport_comparison", "warehouse_interest", "quantity_known",
})
_OPERATIONAL_SIGNALS = frozenset({
    "active_order_intent", "active_shipment", "goods_at_warehouse", "payment_for_item",
})

# Machine key -> Thai reason text shown in Admin.
SIGNAL_LABELS_TH: Dict[str, str] = {
    "general_inquiry": "สอบถามข้อมูลทั่วไป",
    "import_interest": "สนใจนำเข้าสินค้าจากจีน",
    "product_identified": "ระบุสินค้าแล้ว",
    "eligibility_check": "ถามว่าสินค้านำเข้าได้ไหม",
    "rate_interest": "ถามค่าขนส่ง / ค่าบริการ",
    "duration_interest": "ถามระยะเวลาขนส่ง",
    "process_interest": "ถามขั้นตอนการนำเข้า",
    "transport_interest": "ถามเรื่องการขนส่ง (รถ/เรือ)",
    "transport_comparison": "เปรียบเทียบรถกับเรือ",
    "warehouse_interest": "ถามข้อมูลโกดังจีน",
    "quantity_known": "ระบุปริมาณ / ขนาดสินค้า",
    "verified_customer_lookup": "ลูกค้ายืนยันตัวตน ขอดูข้อมูลส่วนตัว",
    "active_order_intent": "ต้องการเปิดบิล / พร้อมสั่ง",
    "active_shipment": "มีรายการขนส่ง / ออเดอร์ที่ต้องดำเนินการ",
    "goods_at_warehouse": "สินค้าอยู่ที่โกดังจีนแล้ว",
    "payment_for_item": "ต้องการชำระเงินสำหรับรายการ",
}

# ── Explicit multi-word operational phrases (never a lone keyword) ─────
_ACTIVE_ORDER_RE = re.compile(
    r"เปิดบิล|ออกบิล|เปิดออเดอร์|เปิดคำสั่งซื้อ|พร้อมสั่ง|พร้อมเปิดบิล|สั่งเลย|"
    r"ยืนยันการสั่ง|ทำรายการสั่งซื้อ|ต้องการสั่งซื้อ|จะสั่งซื้อ")
_GOODS_AT_WAREHOUSE_RE = re.compile(
    r"(ของ|สินค้า|พัสดุ).{0,6}(อยู่|ถึง|เข้า|มาถึง).{0,4}โกดังจีน|"
    r"โกดังจีน.{0,6}(แล้ว|เรียบร้อย)|ของอยู่จีนแล้ว|สินค้าอยู่จีนแล้ว")
_PAYMENT_FOR_ITEM_RE = re.compile(
    r"(จ่าย|ชำระ|เติมเงิน).{0,10}(บิลนี้|รายการนี้|ออเดอร์นี้|wallet|วอลเล็ท|วอลเลท)|"
    r"wallet.{0,10}(จ่าย|ชำระ)|ใช้คูปอง.{0,10}(บิล|รายการ|ออเดอร์)|"
    r"เอาคูปอง.{0,10}(บิล|รายการ|ออเดอร์)")
_PROCESS_RE = re.compile(
    r"ขั้นตอน|วิธีการนำเข้า|นำเข้า.{0,10}ยังไง|นำเข้า.{0,10}อย่างไร|"
    r"ต้องทำอะไรบ้าง|เริ่มยังไง|บริการแพ็ก|ตีลัง|รีแพ็ค|แพ็คของ")
_QTY_RE = re.compile(
    r"\d+\s*(กิโล|กก\.?|กิโลกรัม|ตัน|ชิ้น|กล่อง|ลัง|คิว|คิวบิก|cbm|ลูกบาศก์)",
    re.IGNORECASE)
_TRANSPORT_WORD_RE = re.compile(r"ทางรถ|ทางเรือ|รถ|เรือ|ขนส่ง")
_TRANSPORT_COMPARISON_RE = re.compile(
    r"(รถ.{0,8}(กับ|หรือ|vs).{0,8}เรือ)|(เรือ.{0,8}(กับ|หรือ|vs).{0,8}รถ)|"
    r"อันไหน(ถูก|เร็ว|ดี|คุ้ม)กว่า|แบบไหน(ถูก|เร็ว|ดี|คุ้ม)กว่า", re.IGNORECASE)
_GREETING_RE = re.compile(r"^\s*(สวัสดี|หวัดดี|ดีครับ|ดีค่ะ|hello|hi|hey)\b", re.IGNORECASE)

# Business Action keys that are pure private-record LOOKUPS (not operational).
_LOOKUP_ONLY_ACTIONS = frozenset({
    "getdatacustomer", "customer_data_lookup", "get_customer_data",
    "customer_lookup", "customer_lookup_ux", "customer_lookup_test2",
    "live_test_customer_lookup_v2", "live_test_customer_lookup_v2_21974",
    "live_test_customer_lookup_v2_renamed", "geturlproductdetail",
    "product_lookup", "product_lookup_fixture", "fixture_product_lookup",
})
# Business Action keys that mean the customer has real order/shipment records
# in motion -> operational.
_ORDER_SHIPMENT_ACTIONS = frozenset({
    "search_data_order", "searchdataorder", "searchdataorderlist", "search_po",
    "order_lookup", "fixture_order_lookup", "fixture_cancel_order",
    "searchdatashipment", "searchdatashipmentlist", "searchdatatracking",
    "tracking_lookup", "requestshippingaddresschange",
})

# Real LINE userId shape (P3.1 provenance). P4 runs for real LINE users only.
_REAL_LINE_ID_RE = re.compile(r"^U[0-9a-f]{32}$")

# Broad "wants to import" cue — deliberately wider than
# rag.query_resolution._IMPORT_INTEREST_RE (which is scoped to explicit
# "อยากนำเข้า/สนใจนำเข้า" phrasings): here any mention of importing counts
# as early discovery interest.
_IMPORT_INTEREST_RE = re.compile(
    r"นำเข้า|นำสินค้าเข้า|สั่งของจากจีน|สั่งจากจีน|ชิปปิ้ง|ชิปของ|import|shipping", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _band(score: int) -> str:
    if score <= _COLD_MAX:
        return "COLD"
    if score <= _WARM_MAX:
        return "WARM"
    return "HOT"


def _rank(stage: str) -> int:
    return {"COLD": 0, "WARM": 1, "HOT": 2}.get(stage, 0)


# ── Per-turn signal extraction (pure) ────────────────────────────────

def evaluate_turn_signals(*, question: str, actionable_intent: Optional[str] = None,
                          entities: Optional[Dict] = None, routing_type: Optional[str] = None,
                          selected_business_action: Optional[str] = None,
                          collected_parameters: Optional[Dict] = None,
                          is_verified: bool = False, product_identified: bool = False,
                          facets: Optional[List[str]] = None,
                          transport_modes: Optional[List[str]] = None,
                          comparison: Optional[str] = None) -> List[str]:
    """The signal keys this single turn demonstrates. Pure; no I/O.
    `product_identified` / `facets` / `transport_modes` / `comparison` come
    from the pipeline's own resolver+decomposition (RequestSpec)."""
    q = question or ""
    ents = entities or {}
    ai = actionable_intent or ""
    ba = (selected_business_action or "").lower()
    collected = collected_parameters or {}
    facets = facets or []
    transport_modes = transport_modes or []
    erp_turn = (routing_type in ("API", "WEBHOOK", "TOOL", "HYBRID")) or bool(ba)
    signals: List[str] = []

    def add(sig):
        if sig not in signals:
            signals.append(sig)

    # ── operational (each floors the turn at HOT) ──
    if _ACTIVE_ORDER_RE.search(q):
        add("active_order_intent")
    if _GOODS_AT_WAREHOUSE_RE.search(q):
        add("goods_at_warehouse")
    if _PAYMENT_FOR_ITEM_RE.search(q):
        add("payment_for_item")
    if erp_turn and ba in _ORDER_SHIPMENT_ACTIONS:
        add("active_shipment")
    # A real ERP identifier the customer supplied AND the turn actually ran an
    # order/shipment action with it -> they have a live record to operate on.
    if erp_turn and ba in _ORDER_SHIPMENT_ACTIONS and any(
            k in collected for k in ("OrderCode", "ShipmentCode", "Tracking", "PONumber")):
        add("active_shipment")

    # ── private identity/info lookup — deliberately NOT operational ──
    if is_verified and erp_turn and ba in _LOOKUP_ONLY_ACTIONS:
        add("verified_customer_lookup")
    if is_verified and ai == "tracking_status" and "active_shipment" not in signals:
        add("verified_customer_lookup")

    # ── evaluating (each floors the turn at WARM) ──
    topic = ents.get("topic")
    if product_identified or (topic and topic not in ("บริษัท", "โกดัง", "Tracking", "ใบกำกับ")):
        add("product_identified")
    if ai == "prohibited_goods" and "product_identified" not in signals:
        add("eligibility_check")
    if ai in ("shipping_rate", "shipping_calculation") or "rate" in facets:
        add("rate_interest")
    if ai == "shipping_duration" or "duration" in facets:
        add("duration_interest")
    if ai in ("warehouse_location", "warehouse_map", "warehouse_contact"):
        add("warehouse_interest")
    if _PROCESS_RE.search(q):
        add("process_interest")
    if _QTY_RE.search(q):
        add("quantity_known")
    if comparison or len(transport_modes) >= 2 or _TRANSPORT_COMPARISON_RE.search(q):
        add("transport_comparison")
    if transport_modes or ents.get("transport") or _TRANSPORT_WORD_RE.search(q):
        add("transport_interest")

    # ── discovery ──
    if _IMPORT_INTEREST_RE.search(q):
        add("import_interest")
    if (_GREETING_RE.search(q) or ai in ("company_overview", "company_summary",
                                          "service_information", "summary", "unknown")):
        add("general_inquiry")

    return signals


def _turn_floor(signals: List[str]) -> Optional[str]:
    if any(s in _OPERATIONAL_SIGNALS for s in signals):
        return "HOT"
    if any(s in _EVALUATING_SIGNALS for s in signals):
        return "WARM"
    return None


def recompute_lead_stage(*, prev_score: int, prev_updated_at: Optional[str],
                         turn_signals: List[str], had_operational: bool = False,
                         now: Optional[datetime] = None) -> Dict:
    """Deterministic score/stage update for one turn. Returns
    {"stage", "score", "turn_signals"}. `had_operational` = the customer
    has EVER shown an operational signal (this turn or earlier); without
    one, evaluation questions alone plateau at strong WARM — HOT always
    requires operational purchase/import intent."""
    now = now or _now()
    score = max(0, int(prev_score or 0))

    # Lazy decay after long inactivity — never a scheduler.
    last = _parse_ts(prev_updated_at)
    if last is not None:
        idle_days = (now - last).total_seconds() / 86400.0
        if idle_days > _DECAY_AFTER_DAYS:
            score = int(round(score * _DECAY_FACTOR))

    delta = min(_PER_TURN_DELTA_CAP, sum(_WEIGHTS.get(s, 0) for s in turn_signals))
    score = min(_SCORE_CAP, score + delta)

    operational_now = any(s in _OPERATIONAL_SIGNALS for s in turn_signals)
    if not (had_operational or operational_now):
        score = min(score, _WARM_MAX)          # WARM ceiling until an operational signal

    stage = _band(score)
    floor = _turn_floor(turn_signals)
    if floor and _rank(floor) > _rank(stage):
        stage = floor
        score = max(score, _COLD_MAX + 1 if floor == "WARM" else _WARM_MAX + 1)

    return {"stage": stage, "score": score, "turn_signals": turn_signals}


def _merge_reasons(prev: Optional[List[str]], new: List[str]) -> List[str]:
    """Most-recent signals first, de-duplicated, bounded."""
    out: List[str] = []
    for s in list(new) + list(prev or []):
        if s in _WEIGHTS and s not in out:
            out.append(s)
    return out[:_MAX_REASONS]


def is_real_line_user_id(line_user_id: Optional[str]) -> bool:
    return bool(_REAL_LINE_ID_RE.match(line_user_id or ""))


def _resolved_turn_view(question: str, history) -> Dict:
    """Reuse the SAME deterministic resolver + intent/entity/decomposition
    extractors the RAG pipeline runs, so a bare follow-up ("น้ำหอมครับ"
    after an import question) is understood the same way retrieval saw it.
    No LLM, no network. Returns {entities, actionable_intent, product,
    facets, transport_modes, comparison}."""
    out = {"entities": {}, "actionable_intent": None, "product": False,
           "facets": [], "transport_modes": [], "comparison": None}
    try:
        from rag.query_resolution import (resolve_conversation, extract_entities,
                                          decompose_request, reconstruct_product_list_continuation)
        from rag.query_understanding import classify_actionable_intent
        resolved = (resolve_conversation(question or "", history) or {}).get(
            "resolved_question") or question or ""
        ents = extract_entities(resolved) or {}
        out["entities"] = ents
        out["actionable_intent"] = classify_actionable_intent(resolved, ents)["actionable_intent"]
        spec = decompose_request(resolved, history=history, raw_question=question or "")
        out["facets"] = list(spec.effective_facets() or [])
        out["transport_modes"] = list(spec.transport_modes or [])
        out["comparison"] = spec.comparison
        out["product"] = bool(spec.entities) or bool(
            reconstruct_product_list_continuation(question or "", history))
    except Exception:
        pass
    return out


def update_lead_stage_from_turn(line_user_id: str, *, decide_result: Optional[Dict] = None,
                                conversation_fields: Optional[Dict] = None,
                                question: str = "", is_verified: bool = False,
                                history: Optional[list] = None,
                                now: Optional[datetime] = None) -> Optional[Dict]:
    """Evaluate this real LINE turn's lead signals and persist
    lead_stage / lead_score / lead_reasons / lead_stage_updated_at on
    user_profiles. Real LINE users only. Never raises — a failure here
    must never affect the turn (this runs in the detached post-reply
    thread). Returns the persisted dict, or None if skipped."""
    try:
        if not is_real_line_user_id(line_user_id):
            return None

        cf = conversation_fields or {}
        rv = _resolved_turn_view(question or "", history)
        actionable_intent = rv["actionable_intent"] or cf.get("actionable_intent")

        turn_signals = evaluate_turn_signals(
            question=question,
            actionable_intent=actionable_intent,
            entities=rv["entities"],
            routing_type=cf.get("routing_type"),
            selected_business_action=cf.get("selected_business_action"),
            collected_parameters=cf.get("collected_parameters"),
            is_verified=is_verified,
            product_identified=rv["product"],
            facets=rv["facets"],
            transport_modes=rv["transport_modes"],
            comparison=rv["comparison"],
        )

        from profiles.manager import get_profile, supabase, TABLE, _profile_cache_clear
        profile = get_profile(line_user_id) or {}
        prev_reasons = profile.get("lead_reasons") or []
        if isinstance(prev_reasons, str):
            import json as _json
            try:
                prev_reasons = _json.loads(prev_reasons)
            except Exception:
                prev_reasons = []
        outcome = recompute_lead_stage(
            prev_score=profile.get("lead_score") or 0,
            prev_updated_at=profile.get("lead_stage_updated_at"),
            turn_signals=turn_signals,
            had_operational=any(r in _OPERATIONAL_SIGNALS for r in prev_reasons),
            now=now,
        )
        reasons = _merge_reasons(profile.get("lead_reasons"), turn_signals)
        row = {
            "lead_stage": outcome["stage"],
            "lead_score": int(outcome["score"]),
            "lead_reasons": reasons,
            "lead_stage_updated_at": (now or _now()).isoformat(),
        }
        try:
            _profile_cache_clear(line_user_id)
            supabase.table(TABLE).update(row).eq("line_user_id", line_user_id).execute()
        except Exception as e:  # pragma: no cover - persistence best-effort
            print(f"[lead_stage_service] persist failed: {e}")
        return row
    except Exception as e:  # pragma: no cover - never affect the turn
        print(f"[lead_stage_service] update_lead_stage_from_turn failed (non-fatal): {e}")
        return None


def stage_reason_labels(reasons: Optional[List[str]]) -> List[str]:
    """Machine reason keys -> Thai display text (Admin)."""
    return [SIGNAL_LABELS_TH.get(r, r) for r in (reasons or []) if r]
