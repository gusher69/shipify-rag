# -*- coding: utf-8 -*-
"""PHASE-6C — production-equivalent conversation harness.

Exercises the REAL path a LINE turn takes:

    incoming message
    -> line_bot/webhook.py pre-decide logic
       (pending-confirmation lookup, confirm/cancel classification,
        expired handling, handoff_status passthrough)
    -> DecisionEngine.decide()  (real intent / conversation-act /
       RAG / ERP / calculator / workflow routing, real prompt
       composition incl. real Base Conversation Rules + Tone/System
       Prompt)
    -> final customer reply
    -> post-decide persisted conversation state
       (mid-collection / confirmation pending rows)

`WebhookConversation` is ONE conversation: history + an in-memory
pending-confirmation store persist across .send() calls, exactly like the
production webhook's per-user state.

Two modes:
  deterministic  — real DecisionEngine + real semantic layer + real
                   classifiers + real prompt build; the RAG synthesis
                   LLM and the ERP HTTP call are mocked so
                   routing / intent / conversation-act / state /
                   contradiction / stale-state assertions are stable and
                   fast. This is the acceptance path for everything that
                   does not need generated prose.
  live           — real LLM + real OpenAI embeddings + real RAG
                   retrieval, for grounding / hallucination /
                   general-assistance validation. ERP stays mocked
                   (safe): READ returns an empty payload, WRITE never
                   executes.

No real LINE messages are ever sent.
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

from services.decision_engine import DecisionEngine
from services.pending_confirmation_service import classify_confirmation_reply

_EXPIRED_TEXT = "คำขอก่อนหน้าหมดเวลายืนยันแล้วค่ะ รบกวนแจ้งคำขอใหม่อีกครั้งนะคะ"
_CANCELLED_TEXT = "รับทราบค่ะ ยกเลิกการดำเนินการแล้ว"


def _fake_rag(answer: str, confidence: float):
    from tests.test_decision_engine import _fake_playground_result
    return _fake_playground_result(answer=answer, confidence=confidence)


class WebhookConversation:
    def __init__(self, *, engine: DecisionEngine, mode: str = "deterministic",
                 handoff_status: str = "NONE", tenant_id: str = "default",
                 channel: str = "line", user_id: str = "Uphase6c",
                 cust_code: Optional[str] = None, conversation_tier: Optional[str] = None,
                 rag_answer: str = "[RAG]", rag_conf: float = 0.2):
        self.engine = engine
        self.mode = mode
        self.handoff_status = handoff_status
        self.tenant_id, self.channel, self.user_id = tenant_id, channel, user_id
        self.cust_code = cust_code
        self.conversation_tier = conversation_tier
        self.rag_answer, self.rag_conf = rag_answer, rag_conf
        self.history: List[Dict] = []
        self._pending: List[Dict] = []
        self.turns: List[Dict] = []          # recorded {user, result}

    # ── pending store (PendingConfirmationService semantics) ──────────
    def _active(self):
        now = datetime.now(timezone.utc)
        for r in reversed(self._pending):
            if r["status"] != "pending":
                continue
            if r["expires_at"] and now > r["expires_at"]:
                r["status"] = "expired"
                return None
            return r
        return None

    def _most_recent(self):
        return self._pending[-1] if self._pending else None

    def _create_pending(self, *, action_id, action_name, parameters, question_text,
                        confirmation_required=True, ttl=300):
        for r in self._pending:
            if r["status"] == "pending":
                r["status"] = "cancelled"
        now = datetime.now(timezone.utc)
        self._pending.append({
            "id": f"p{len(self._pending)}", "status": "pending",
            "pending_action_id": action_id, "pending_action_name": action_name,
            "pending_parameters": parameters or {}, "question_text": question_text,
            "confirmation_required": confirmation_required,
            "created_at": now, "expires_at": now + timedelta(seconds=ttl)})

    def seed_expired_confirmation(self, *, action_name="SendLineNotiCS", minutes_ago=420):
        self._create_pending(action_id="seed", action_name=action_name,
                             parameters={}, question_text="ยืนยันไหมคะ")
        self._pending[-1]["status"] = "expired"
        self._pending[-1]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)

    def seed_active_confirmation(self, *, action_id="seedA", action_name="SendLineNotiCS",
                                params=None, question="ยืนยันการดำเนินการ 'SendLineNotiCS' ไหมคะ"):
        self._create_pending(action_id=action_id, action_name=action_name,
                             parameters=params or {}, question_text=question)

    def seed_history(self, turns: List[Dict]):
        self.history = list(turns)

    # ── one inbound turn ─────────────────────────────────────────────
    def _patches(self):
        b = MagicMock()
        binding = ({"cust_code": self.cust_code, "status": "verified", "channel": self.channel,
                    "external_user_id": self.user_id, "tenant_id": self.tenant_id}
                   if self.cust_code else None)
        b.get_verified_binding.return_value = binding
        b.get_verified_binding_for_custcode.return_value = binding
        payload = {"data": {}}
        req = MagicMock(return_value=MagicMock(status_code=200, json=lambda: payload,
                                               text=json.dumps(payload)))
        ctx = [
            patch("services.customer_binding_service.get_customer_binding_service", return_value=b),
            patch("services.credential_store.CredentialStore.resolve",
                  return_value={"ok": True, "value": "S", "error": None}),
            patch("services.action_executor.requests.request", req),
        ]
        if self.mode == "deterministic":
            ctx.append(patch("services.link_conversion_flow.requests.get",
                             return_value=MagicMock(status_code=200, headers={}, close=lambda: None)))
            ctx.append(patch("services.playground_orchestrator.run_playground_turn",
                             return_value=_fake_rag(self.rag_answer, self.rag_conf)))
        return ctx

    def send(self, message: str) -> Dict:
        rh = list(self.history)
        decide_context = {
            "channel": self.channel, "developer_mode": True,
            "customer_context": ({"cust_code": self.cust_code} if self.cust_code else {}) |
                                ({"conversation_tier": self.conversation_tier} if self.conversation_tier else {}),
            "tenant_id": self.tenant_id, "external_user_id": self.user_id,
            "handoff_status": self.handoff_status,
        }
        result = None
        with contextlib.ExitStack() as stack:
            for p in self._patches():
                stack.enter_context(p)

            pending = self._active()
            if pending:
                kind = classify_confirmation_reply(message)
                if kind == "confirm":
                    result = self.engine.decide(message, history=rh, context={
                        **decide_context, "confirmed": True,
                        "confirmed_action_id": pending["pending_action_id"],
                        "confirmed_parameters": pending.get("pending_parameters") or {}})
                    pending["status"] = "executed"
                elif kind == "cancel":
                    pending["status"] = "cancelled"
                    result = {"reply": {"text": _CANCELLED_TEXT}, "routing": {"type": "GENERAL"}, "developer": {}}
                else:
                    result = self.engine.decide(message, history=rh, context={
                        **decide_context,
                        "pending_action_id": pending["pending_action_id"],
                        "pending_parameters": pending.get("pending_parameters") or {}})
            else:
                kind = classify_confirmation_reply(message)
                if kind in ("confirm", "cancel"):
                    mr = self._most_recent()
                    if mr and mr.get("status") == "expired":
                        import datetime as _dt
                        _exp = mr.get("expires_at")
                        _fresh = bool(_exp and (_dt.datetime.now(_dt.timezone.utc) - _exp)
                                      < _dt.timedelta(minutes=10))
                        _accept_help = False
                        try:
                            from services.service_intent_flow import is_help_affirmation as _iha
                            _accept_help = _iha(message, rh)
                        except Exception:
                            pass
                        if _fresh and not _accept_help:
                            result = {"reply": {"text": _EXPIRED_TEXT},
                                      "routing": {"type": "WORKFLOW"}, "developer": {}}
                if result is None:
                    result = self.engine.decide(message, history=rh, context=decide_context)

        dev = result.get("developer") or {}
        reply_text = (result.get("reply") or {}).get("text") or ""
        routing = (result.get("routing") or {}).get("type")

        # post-decide pending persistence (affects the NEXT turn)
        ics = dev.get("information_collection_status") or {}
        gate = dev.get("confirmation_gate") or {}
        if gate.get("required") and not gate.get("confirmed") and gate.get("action_id"):
            self._create_pending(action_id=gate["action_id"], action_name=gate.get("action_key"),
                                 parameters=ics.get("collected_parameters") or {},
                                 question_text=reply_text, confirmation_required=True)
        elif routing == "WORKFLOW" and ics.get("selected_action_id") and not ics.get("is_complete"):
            self._create_pending(action_id=ics["selected_action_id"],
                                 action_name=ics.get("selected_business_action"),
                                 parameters=ics.get("collected_parameters") or {},
                                 question_text=reply_text, confirmation_required=False)
        elif routing in ("API", "WEBHOOK", "TOOL", "NOTIFICATION", "HUMAN_HANDOFF"):
            a = self._active()
            if a and a.get("pending_action_name") == dev.get("selected_business_action"):
                a["status"] = "cancelled"

        self.history.append({"role": "user", "content": message})
        self.history.append({"role": "assistant", "content": reply_text})

        out = {
            "message": message,
            "routing": routing,
            "selection_source": dev.get("selection_source"),
            "service_intent_family": dev.get("service_intent_family"),
            "intent_family": (dev.get("semantic_interpretation") or {}).get("intent_family"),
            "conversation_act": (dev.get("semantic_interpretation") or {}).get("conversation_act"),
            "actionable_intent": (dev.get("intent") or {}).get("actionable_intent"),
            "turn_intent": dev.get("turn_intent"),
            "general_chat_used": (dev.get("execution_result") or {}).get("general_chat_used")
                                 if isinstance(dev.get("execution_result"), dict) else dev.get("general_chat_used"),
            "handoff_reason": (result.get("handoff_payload") or {}).get("reason"),
            "collected_parameters": ics.get("collected_parameters") or {},
            "pending_flow_broken": dev.get("pending_flow_broken_by_current_intent"),
            "reply": reply_text,
            "developer": dev,
        }
        self.turns.append({"user": message, "result": out})
        return out
