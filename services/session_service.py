"""SessionService — the single place that turns AI Playground turns into
persisted, reproducible Sessions (see migrations/016_ai_sessions.sql).

    AI Playground -> SessionService -> Conversation -> Prompt -> RAG ->
    Pipeline -> LLM

Every playground_orchestrator.run_playground_turn() call is wrapped by
admin/routes.py's /admin/playground/ask, which calls
SessionService.record_turn() right after — that's the one place a Session
is created/updated. Nothing here calls the LLM/RAG itself; this module
only persists what playground_orchestrator already computed, so it stays
reusable for any future caller (LINE OA simulation, batch eval, etc.)
without duplicating pipeline logic.

Every write degrades gracefully: if the migration hasn't been run yet, or
a query fails, callers get an empty/None result instead of a crash — the
same fallback pattern used everywhere else in this app.
"""
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import SUPABASE_URL, SUPABASE_KEY

_supabase = None


def _get_sb():
    global _supabase
    if _supabase is None:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _search_text(question: str, answer: str, chunks: List[Dict], prompt, policy) -> str:
    parts = [question or "", answer or ""]
    for c in chunks or []:
        parts.append(c.get("file_name") or c.get("source") or "")
        for att in (c.get("attachments") or []):
            parts.append(att.get("filename") or att.get("name") or "")
    try:
        parts.append(prompt.template.name)
        parts.append(prompt.template.id)
    except Exception:
        pass
    try:
        for v in policy.verdicts:
            parts.append(v.name)
    except Exception:
        pass
    return " ".join(p for p in parts if p).lower()[:8000]


def extract_conversation_fields(decide_result: Dict) -> Dict:
    """Normalizes a services.decision_engine.py::DecisionEngine.decide()
    result (called with context={"developer_mode": True}) into the flat
    shape record_conversation_turn() persists. Purely a read/mapping layer
    over fields decide() already computed for every routing type (RAG /
    API-WEBHOOK / HYBRID / WORKFLOW / HUMAN_HANDOFF / SAFE_FALLBACK) —
    never re-derives routing, prompt, policy, or confidence itself, and
    never masks anything additionally: erp_request/erp_response are
    already the SAME masked/sanitized values action_executor.py produced
    (mask_execution_secrets/sanitize_response_body), just read through."""
    dev = decide_result.get("developer") or {}
    routing_type = (decide_result.get("routing") or {}).get("type")
    intent = dev.get("intent") or {}
    handoff_payload = decide_result.get("handoff_payload") or {}
    reply = decide_result.get("reply") or {}

    chunks: List[Dict] = []
    prompt: Dict = {}
    policy: Dict = {}
    erp_request: Dict = {}
    erp_response: Dict = {}
    confidence = dev.get("confidence")
    model = None

    exec_result = dev.get("execution_result") or {}
    exec_inner = exec_result.get("result") or {}

    if routing_type == "RAG":
        chunks = exec_inner.get("chunks") or []
        prompt = {"template_id": exec_inner.get("prompt_template_id"),
                  "template_name": exec_inner.get("prompt_template_name"),
                  "template_version": exec_inner.get("prompt_template_version")}
        policy = {"policy_set_name": exec_inner.get("policy_set_name"),
                  "escalate": exec_inner.get("policy_escalate")}
        model = (exec_result.get("metadata") or {}).get("model")
    elif routing_type in ("API", "WEBHOOK", "TOOL"):
        erp_request = exec_inner.get("request") or {}
        erp_response = {"status_code": exec_inner.get("status_code"),
                         "response": exec_inner.get("response"), "error": exec_inner.get("error")}
    elif routing_type == "HYBRID":
        erp_exec = dev.get("erp_execution_result") or {}
        erp_inner = erp_exec.get("result") or {}
        erp_request = erp_inner.get("request") or {}
        erp_response = {"status_code": erp_inner.get("status_code"),
                         "response": erp_inner.get("response"), "error": erp_exec.get("error")}
        rag_exec = dev.get("rag_execution_result") or {}
        rag_inner = rag_exec.get("result") or {}
        chunks = rag_inner.get("chunks") or []
        prompt = {"template_id": rag_inner.get("prompt_template_id"),
                  "template_name": rag_inner.get("prompt_template_name"),
                  "template_version": rag_inner.get("prompt_template_version")}
        policy = {"policy_set_name": rag_inner.get("policy_set_name"), "merge_strategy": dev.get("merge_strategy")}
        if confidence is None:
            confidence = rag_inner.get("confidence")
        model = (rag_exec.get("metadata") or {}).get("model")

    escalated = routing_type == "HUMAN_HANDOFF"
    collection_status = dev.get("information_collection_status") or {}
    # A turn that's still mid-collection (asking the customer for one
    # more parameter) never reaches _execute_selected_action, so the
    # top-level dev["selected_business_action"] is never set for it --
    # collection_status carries the SAME value in that case (Customer
    # Intelligence V1, 2026-08-15; needed so identifier persistence below
    # sees CustCode/OrderCode/etc. even on an incomplete turn).
    selected_business_action = dev.get("selected_business_action") or collection_status.get("selected_business_action")

    return {
        "broad_intent": intent.get("broad_intent"), "actionable_intent": intent.get("actionable_intent"),
        "routing_type": routing_type, "selected_business_action": selected_business_action,
        "escalated": escalated, "escalation_reason": handoff_payload.get("reason") if escalated else None,
        "chunks": chunks, "prompt": prompt, "policy": policy,
        "erp_request": erp_request, "erp_response": erp_response,
        "attachment_metadata": {"images": reply.get("images") or [], "files": reply.get("files") or []},
        "confidence": confidence, "model": model,
        "input_tokens": exec_inner.get("input_tokens"), "output_tokens": exec_inner.get("output_tokens"),
        "latency_ms": dev.get("latency_ms"),
        # Customer Intelligence V1 (2026-08-15) -- the SAME customer-
        # message-sourced parameter values decide() already collected
        # this turn (never a credential/secret: collection_status only
        # ever holds customer_message-sourced parameters, per
        # services/decision_engine.py's own _handle_dynamic_collection).
        # profiles/manager.py::update_profile_from_turn reads this to
        # remember CustCode/OrderCode/ShipmentCode/Tracking across turns.
        "collected_parameters": collection_status.get("collected_parameters") or {},
        "metadata": {"classification": (dev.get("classification") or {}).get("classification"),
                     "workflow": dev.get("workflow"), "alert": decide_result.get("alert"),
                     "error": decide_result.get("error")},
    }


class SessionService:
    """Reusable entry point — see module docstring for the pipeline this
    sits at the end of."""

    # ── Session CRUD ──────────────────────────────────────────────
    def create_session(self, name: Optional[str] = None) -> Optional[Dict]:
        try:
            row = {"name": name or "New Session", "status": "completed"}
            res = _get_sb().table("ai_sessions").insert(row).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[SessionService] create_session failed: {e}")
            return None

    def get_session(self, session_id: str) -> Optional[Dict]:
        sb = _get_sb()
        try:
            sres = sb.table("ai_sessions").select("*").eq("id", session_id).is_("deleted_at", "null").execute()
            if not sres.data:
                return None
            session = sres.data[0]
        except Exception as e:
            print(f"[SessionService] get_session failed: {e}")
            return None

        try:
            mres = sb.table("ai_session_messages").select("*").eq("session_id", session_id) \
                .order("turn_index").execute()
            messages = mres.data or []
        except Exception as e:
            print(f"[SessionService] messages fetch failed: {e}")
            messages = []

        message_ids = [m["id"] for m in messages]
        events_by_msg: Dict[str, List[Dict]] = {}
        traces_by_msg: Dict[str, Dict] = {}
        if message_ids:
            try:
                eres = sb.table("ai_session_events").select("*").in_("message_id", message_ids) \
                    .order("stage_index").execute()
                for e in (eres.data or []):
                    events_by_msg.setdefault(e["message_id"], []).append(e)
            except Exception as e:
                print(f"[SessionService] events fetch failed: {e}")
            try:
                tres = sb.table("ai_session_traces").select("*").in_("message_id", message_ids).execute()
                for t in (tres.data or []):
                    traces_by_msg[t["message_id"]] = t
            except Exception as e:
                print(f"[SessionService] traces fetch failed: {e}")

        for m in messages:
            m["events"] = events_by_msg.get(m["id"], [])
            m["trace"] = traces_by_msg.get(m["id"])

        session["messages"] = messages
        return session

    def list_sessions(self, search: Optional[str] = None, date_filter: Optional[str] = None,
                       model: Optional[str] = None, status: Optional[str] = None,
                       confidence: Optional[str] = None, limit: int = 200,
                       channel: Optional[str] = None) -> List[Dict]:
        sb = _get_sb()
        try:
            q = sb.table("ai_sessions").select("*").is_("deleted_at", "null")
            if search:
                like = f"%{search.lower()}%"
                q = q.ilike("search_text", like)
            if model:
                q = q.eq("model", model)
            if status:
                q = q.eq("status", status)
            if channel:
                q = q.eq("channel", channel)
            q = q.order("updated_at", desc=True).limit(limit)
            res = q.execute()
            sessions = res.data or []
        except Exception as e:
            print(f"[SessionService] list_sessions failed: {e}")
            return []

        if date_filter in ("today", "yesterday", "7d", "30d"):
            sessions = [s for s in sessions if _matches_date_filter(s.get("updated_at"), date_filter)]

        if confidence in ("high", "medium", "low"):
            band = {"high": lambda c: c is not None and c >= 0.85,
                    "medium": lambda c: c is not None and 0.6 <= c < 0.85,
                    "low": lambda c: c is not None and c < 0.6}[confidence]
            sessions = [s for s in sessions if band(s.get("last_confidence"))]

        return sessions

    def rename_session(self, session_id: str, name: str) -> bool:
        try:
            _get_sb().table("ai_sessions").update(
                {"name": name, "updated_at": _now_iso()}
            ).eq("id", session_id).execute()
            return True
        except Exception as e:
            print(f"[SessionService] rename_session failed: {e}")
            return False

    def delete_session(self, session_id: str) -> bool:
        try:
            _get_sb().table("ai_sessions").update(
                {"deleted_at": _now_iso()}
            ).eq("id", session_id).execute()
            return True
        except Exception as e:
            print(f"[SessionService] delete_session failed: {e}")
            return False

    def clear_conversation(self, session_id: str) -> bool:
        try:
            _get_sb().table("ai_session_messages").delete().eq("session_id", session_id).execute()
            _get_sb().table("ai_sessions").update({
                "message_count": 0, "total_input_tokens": 0, "total_output_tokens": 0,
                "total_cost_usd": 0, "avg_latency_ms": None, "last_confidence": None,
                "last_question": None, "last_answer": None, "search_text": "",
                "updated_at": _now_iso(),
            }).eq("id", session_id).execute()
            return True
        except Exception as e:
            print(f"[SessionService] clear_conversation failed: {e}")
            return False

    def duplicate_session(self, session_id: str) -> Optional[Dict]:
        source = self.get_session(session_id)
        if not source:
            return None
        new_session = self.create_session(name=f"{source['name']} (copy)")
        if not new_session:
            return None
        sb = _get_sb()
        for m in source["messages"]:
            try:
                new_msg = sb.table("ai_session_messages").insert({
                    "session_id": new_session["id"], "turn_index": m["turn_index"],
                    "role": m["role"], "content": m["content"], "status": m["status"],
                    "latency_ms": m.get("latency_ms"), "input_tokens": m.get("input_tokens"),
                    "output_tokens": m.get("output_tokens"), "estimated_cost_usd": m.get("estimated_cost_usd"),
                    "confidence": m.get("confidence"), "confidence_label": m.get("confidence_label"),
                }).execute().data[0]
                for ev in m.get("events") or []:
                    sb.table("ai_session_events").insert({
                        "session_id": new_session["id"], "message_id": new_msg["id"],
                        "stage_index": ev["stage_index"], "stage_name": ev["stage_name"],
                        "status": ev["status"], "duration_ms": ev["duration_ms"], "detail": ev.get("detail"),
                    }).execute()
                if m.get("trace"):
                    t = m["trace"]
                    sb.table("ai_session_traces").insert({
                        "session_id": new_session["id"], "message_id": new_msg["id"],
                        "chunks": t.get("chunks") or [], "prompt": t.get("prompt") or {},
                        "policy": t.get("policy") or {}, "services_used": t.get("services_used") or [],
                        "metadata": t.get("metadata") or {}, "raw_response": t.get("raw_response") or {},
                    }).execute()
            except Exception as e:
                print(f"[SessionService] duplicate_session message copy failed: {e}")
        try:
            sb.table("ai_sessions").update({
                "message_count": source.get("message_count") or 0,
                "total_input_tokens": source.get("total_input_tokens") or 0,
                "total_output_tokens": source.get("total_output_tokens") or 0,
                "total_cost_usd": source.get("total_cost_usd") or 0,
                "avg_latency_ms": source.get("avg_latency_ms"),
                "last_confidence": source.get("last_confidence"),
                "last_question": source.get("last_question"), "last_answer": source.get("last_answer"),
                "model": source.get("model"), "embedding_model": source.get("embedding_model"),
                "prompt_template_id": source.get("prompt_template_id"), "prompt_version": source.get("prompt_version"),
                "search_text": source.get("search_text"),
            }).eq("id", new_session["id"]).execute()
        except Exception as e:
            print(f"[SessionService] duplicate_session summary update failed: {e}")
        return self.get_session(new_session["id"])

    # ── Recording a turn (the auto-create/update hook) ──────────────
    def record_turn(self, session_id: Optional[str], question: str, result, status: str = "completed") -> Optional[Dict]:
        """`result` is a services.playground_orchestrator.PlaygroundResult.
        Creates a session if session_id is falsy. Returns the updated
        session summary dict (never raises — a persistence failure must
        never block the Playground from showing the answer it already
        computed)."""
        try:
            sb = _get_sb()
            session = None
            if session_id:
                sres = sb.table("ai_sessions").select("*").eq("id", session_id).execute()
                session = (sres.data or [None])[0]
            if not session:
                session = self.create_session(name=question[:60] or "New Session")
            if not session:
                return None
            session_id = session["id"]

            next_turn = (session.get("message_count") or 0)
            user_msg = sb.table("ai_session_messages").insert({
                "session_id": session_id, "turn_index": next_turn, "role": "user",
                "content": question, "status": "completed",
            }).execute().data[0]

            assistant_msg = sb.table("ai_session_messages").insert({
                "session_id": session_id, "turn_index": next_turn + 1, "role": "assistant",
                "content": result.answer, "status": status,
                "latency_ms": result.latency_ms, "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens, "estimated_cost_usd": result.estimated_cost_usd,
                "confidence": result.confidence, "confidence_label": result.confidence_label,
            }).execute().data[0]

            for i, stage in enumerate(result.stages):
                sb.table("ai_session_events").insert({
                    "session_id": session_id, "message_id": assistant_msg["id"], "stage_index": i,
                    "stage_name": stage.name, "status": stage.status,
                    "duration_ms": stage.duration_ms, "detail": stage.detail,
                }).execute()

            prompt_dict = {
                "template_id": result.prompt.template.id, "template_name": result.prompt.template.name,
                "template_version": result.prompt.template.version,
                "system_prompt": result.prompt.template.system_prompt,
                "final_prompt": result.prompt.final_prompt_text,
                "context": result.context,
            }
            policy_dict = {
                "escalate": result.policy.escalate, "active_count": result.policy.active_count,
                "verdicts": [asdict(v) for v in result.policy.verdicts], "notes": result.policy.notes,
            }
            metadata_dict = {
                "model": result.model, "embedding_model": result.embedding_model,
                "temperature": result.temperature, "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens, "latency_ms": round(result.latency_ms, 1),
                "estimated_cost_usd": round(result.estimated_cost_usd, 6),
                "confidence": round(result.confidence, 4), "confidence_label": result.confidence_label,
                "retrieved_chunks": len(result.chunks), "executed_at": _now_iso(),
            }
            sb.table("ai_session_traces").insert({
                "session_id": session_id, "message_id": assistant_msg["id"],
                "chunks": result.chunks, "prompt": prompt_dict, "policy": policy_dict,
                "services_used": result.services_used, "metadata": metadata_dict, "raw_response": {},
            }).execute()

            new_count = next_turn + 2
            prev_total_in = session.get("total_input_tokens") or 0
            prev_total_out = session.get("total_output_tokens") or 0
            prev_cost = float(session.get("total_cost_usd") or 0)
            prev_avg_latency = session.get("avg_latency_ms")
            assistant_turns_before = next_turn // 2
            new_avg_latency = (
                result.latency_ms if prev_avg_latency is None else
                ((float(prev_avg_latency) * assistant_turns_before) + result.latency_ms) / (assistant_turns_before + 1)
            )

            sb.table("ai_sessions").update({
                "status": status, "message_count": new_count,
                "model": result.model, "embedding_model": result.embedding_model,
                "prompt_template_id": result.prompt.template.id, "prompt_version": result.prompt.template.version,
                "total_input_tokens": prev_total_in + result.input_tokens,
                "total_output_tokens": prev_total_out + result.output_tokens,
                "total_cost_usd": prev_cost + result.estimated_cost_usd,
                "avg_latency_ms": new_avg_latency,
                "last_confidence": result.confidence, "last_question": question, "last_answer": result.answer,
                "search_text": _search_text(question, result.answer, result.chunks, result.prompt, result.policy),
                "updated_at": _now_iso(), "last_message_at": _now_iso(),
            }).eq("id", session_id).execute()

            return self.get_session(session_id)
        except Exception as e:
            print(f"[SessionService] record_turn failed: {e}")
            return None

    # ── Analytics (Phase 3.7, 2026-08-05) ──────────────────────────
    def get_conversation_stats(self, channel: Optional[str] = None, limit: int = 2000) -> Dict:
        """Aggregate stats for the Conversation Analytics dashboard —
        Routing/Intent/Confidence distributions, escalation rate, prompt/
        policy usage. Computed in Python over the most recent `limit`
        assistant messages (not a DB-side GROUP BY) — simplest thing that
        works at this data volume; revisit with real SQL aggregation if a
        deployment's ai_session_messages table gets large enough for this
        to matter."""
        sb = _get_sb()
        try:
            q = sb.table("ai_session_messages").select(
                "routing_type,broad_intent,actionable_intent,confidence,escalated,escalation_reason,session_id"
            ).eq("role", "assistant").order("created_at", desc=True).limit(limit)
            messages = q.execute().data or []
        except Exception as e:
            print(f"[SessionService] get_conversation_stats messages query failed: {e}")
            messages = []

        if channel:
            try:
                session_ids = {s["id"] for s in sb.table("ai_sessions").select("id").eq("channel", channel).execute().data or []}
                messages = [m for m in messages if m.get("session_id") in session_ids]
            except Exception as e:
                print(f"[SessionService] get_conversation_stats channel filter failed: {e}")

        routing_counts: Dict[str, int] = {}
        intent_counts: Dict[str, int] = {}
        confidence_buckets = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
        escalation_count = 0
        for m in messages:
            rt = m.get("routing_type") or "unknown"
            routing_counts[rt] = routing_counts.get(rt, 0) + 1
            intent = m.get("broad_intent") or "unknown"
            intent_counts[intent] = intent_counts.get(intent, 0) + 1
            conf = m.get("confidence")
            if conf is None:
                confidence_buckets["unknown"] += 1
            elif conf >= 0.85:
                confidence_buckets["high"] += 1
            elif conf >= 0.6:
                confidence_buckets["medium"] += 1
            else:
                confidence_buckets["low"] += 1
            if m.get("escalated"):
                escalation_count += 1

        try:
            tier_rows = sb.table("user_profiles").select("conversation_tier").execute().data or []
            tier_counts: Dict[str, int] = {}
            for r in tier_rows:
                t = r.get("conversation_tier") or "cold"
                tier_counts[t] = tier_counts.get(t, 0) + 1
        except Exception as e:
            print(f"[SessionService] get_conversation_stats tier query failed: {e}")
            tier_counts = {}

        return {
            "total_turns": len(messages),
            "routing_distribution": routing_counts,
            "intent_distribution": dict(sorted(intent_counts.items(), key=lambda kv: -kv[1])[:15]),
            "confidence_distribution": confidence_buckets,
            "escalation_count": escalation_count,
            "escalation_rate": round(escalation_count / len(messages), 4) if messages else 0.0,
            "tier_distribution": tier_counts,
        }

    # ── Conversation History (Phase 3.1, 2026-08-05) ────────────────
    # Reuses the SAME ai_sessions/ai_session_messages/ai_session_events/
    # ai_session_traces tables record_turn() already writes to for the AI
    # Playground — a LINE conversation IS an ai_sessions row (channel=
    # 'line', line_user_id set), and each customer message IS a Turn,
    # exactly like the Conversation -> Turn -> Turn model already in
    # place. No new tables, no parallel logging system.

    def get_or_create_active_conversation(self, line_user_id: str, *, inactivity_hours: float = 24.0,
                                           channel: str = "line", name: Optional[str] = None) -> Optional[Dict]:
        """Returns this user's currently-active conversation (the most
        recent non-deleted ai_sessions row for this line_user_id+channel,
        if its last activity was within `inactivity_hours`), or starts a
        new one. A "conversation" is a burst of activity, same convention
        any real chat platform uses — after the gap, the next message
        starts a new Conversation ID/Session ID (both map to the same
        ai_sessions.id; see ARCHITECTURE.md for why one row serves both
        concepts here).

        `channel` defaults to "line" (every existing caller — line_bot/
        webhook.py — is unaffected); the AI Playground (Real User Journey
        UAT, 2026-08-15) passes channel="playground" so its own
        synthetic-user sessions are scoped separately and never collide
        with a real LINE user's conversation history/profile. `name`
        lets a caller give the session a recognizable title (e.g. a UAT
        Journey label) instead of the generic default — only applied
        when a NEW session is created, never overwriting an existing
        session's name on every turn."""
        sb = _get_sb()
        try:
            res = sb.table("ai_sessions").select("*").eq("line_user_id", line_user_id) \
                .eq("channel", channel).is_("deleted_at", "null") \
                .order("updated_at", desc=True).limit(1).execute()
            existing = (res.data or [None])[0]
        except Exception as e:
            print(f"[SessionService] get_or_create_active_conversation lookup failed: {e}")
            existing = None

        if existing:
            last_activity = existing.get("last_message_at") or existing.get("updated_at")
            try:
                last_dt = datetime.fromisoformat((last_activity or "").replace("Z", "+00:00"))
                age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
            except Exception:
                age_hours = None
            if age_hours is not None and age_hours < inactivity_hours:
                return existing

        try:
            row = {"name": name or f"{channel.upper()}: {line_user_id[:12]}", "status": "completed",
                   "channel": channel, "line_user_id": line_user_id}
            res = sb.table("ai_sessions").insert(row).execute()
            return (res.data or [None])[0]
        except Exception as e:
            print(f"[SessionService] get_or_create_active_conversation create failed: {e}")
            return None

    # ── Context Continuity (Customer Intelligence V1, 2026-08-15) ────
    # A bounded recent-message window for THIS conversation only, shaped
    # exactly like the history list services/decision_engine.py::decide()
    # already accepts elsewhere (e.g. line_bot/webhook.py's pending-
    # confirmation replay) -- never a second history format. Scoped to a
    # single ai_sessions.id, which is itself already scoped to a single
    # line_user_id (get_or_create_active_conversation), so one user's
    # history can never leak into another's: there is no code path here
    # that reads across conversation_id/line_user_id boundaries.
    def get_recent_history(self, conversation_id: str, *, max_turns: int = 3) -> List[Dict]:
        """Returns up to `max_turns` user/assistant EXCHANGES (2 messages
        each) as oldest-first [{"role", "content"}, ...] -- deliberately
        bounded (never the full conversation) so the Decision Engine/LLM
        never receives unlimited history."""
        try:
            res = _get_sb().table("ai_session_messages").select("role,content,turn_index") \
                .eq("session_id", conversation_id).order("turn_index", desc=True) \
                .limit(max_turns * 2).execute()
            rows = list(reversed(res.data or []))
            return [{"role": r["role"], "content": r.get("content") or ""} for r in rows]
        except Exception as e:
            print(f"[SessionService] get_recent_history failed (treating as no history): {e}")
            return []

    # ── Human Handoff state (2026-08-13) ─────────────────────────────
    # Reuses the SAME ai_sessions row as the conversation object (see
    # get_or_create_active_conversation above) rather than a new table —
    # migrations/037_handoff_state.sql adds handoff_status/handoff_reason/
    # handoff_notified_at columns. NONE (default) -> PENDING (a trigger
    # fired this turn, notification in flight) -> NOTIFIED (CS already
    # notified for this active conversation — the duplicate-protection
    # check every caller must consult before sending another real
    # notification) -> RESOLVED (reserved for a future admin/CS action).

    def get_handoff_status(self, conversation_id: str) -> str:
        try:
            res = _get_sb().table("ai_sessions").select("handoff_status") \
                .eq("id", conversation_id).single().execute()
            return (res.data or {}).get("handoff_status") or "NONE"
        except Exception as e:
            print(f"[SessionService] get_handoff_status failed (treating as NONE): {e}")
            return "NONE"

    def set_handoff_status(self, conversation_id: str, status: str, *, reason: Optional[str] = None) -> None:
        if status not in ("NONE", "PENDING", "NOTIFIED", "RESOLVED"):
            raise ValueError(f"invalid handoff status: {status}")
        update = {"handoff_status": status}
        if reason is not None:
            update["handoff_reason"] = reason
        if status == "NOTIFIED":
            update["handoff_notified_at"] = _now_iso()
        try:
            _get_sb().table("ai_sessions").update(update).eq("id", conversation_id).execute()
        except Exception as e:
            print(f"[SessionService] set_handoff_status failed (non-fatal): {e}")

    def record_conversation_turn(self, session_id: Optional[str], question: str, decide_result: Dict,
                                  *, line_user_id: Optional[str] = None, conversation_tier: Optional[str] = None) -> Optional[Dict]:
        """The Decision Engine / LINE OA equivalent of record_turn() above.
        `decide_result` is the raw dict services.decision_engine.py::
        DecisionEngine.decide() returns (developer_mode=True in its
        context so the full trace is present) — never a second
        implementation of routing/prompt/policy logic, only a persistence
        mapping over what decide() already computed. Never stores a
        secret: erp_request/erp_response are read from developer_trace
        exactly as decide() already masked/sanitized them (same
        sanitize_for_preview/mask_execution_secrets helpers action_executor.py
        itself uses) — this function does not additionally mask anything
        because there is nothing left to mask by the time it gets here."""
        try:
            sb = _get_sb()
            session = None
            if session_id:
                sres = sb.table("ai_sessions").select("*").eq("id", session_id).execute()
                session = (sres.data or [None])[0]
            if not session:
                session = self.get_or_create_active_conversation(line_user_id) if line_user_id else self.create_session(name=question[:60])
            if not session:
                return None
            session_id = session["id"]

            fields = extract_conversation_fields(decide_result)
            reply_text = (decide_result.get("reply") or {}).get("text") or ""

            next_turn = (session.get("message_count") or 0)
            sb.table("ai_session_messages").insert({
                "session_id": session_id, "turn_index": next_turn, "role": "user",
                "content": question, "status": "completed",
                "broad_intent": fields["broad_intent"], "actionable_intent": fields["actionable_intent"],
            }).execute()

            assistant_msg = sb.table("ai_session_messages").insert({
                "session_id": session_id, "turn_index": next_turn + 1, "role": "assistant",
                "content": reply_text, "status": "completed",
                "latency_ms": fields["latency_ms"], "input_tokens": fields["input_tokens"],
                "output_tokens": fields["output_tokens"], "confidence": fields["confidence"],
                "routing_type": fields["routing_type"], "selected_business_action": fields["selected_business_action"],
                "escalated": fields["escalated"], "escalation_reason": fields["escalation_reason"],
            }).execute().data[0]

            sb.table("ai_session_traces").insert({
                "session_id": session_id, "message_id": assistant_msg["id"],
                "chunks": fields["chunks"], "prompt": fields["prompt"], "policy": fields["policy"],
                "metadata": fields["metadata"], "erp_request": fields["erp_request"],
                "erp_response": fields["erp_response"], "attachment_metadata": fields["attachment_metadata"],
            }).execute()

            new_count = next_turn + 2
            prev_avg_latency = session.get("avg_latency_ms")
            assistant_turns_before = next_turn // 2
            new_avg_latency = (
                fields["latency_ms"] if prev_avg_latency is None or fields["latency_ms"] is None else
                ((float(prev_avg_latency) * assistant_turns_before) + fields["latency_ms"]) / (assistant_turns_before + 1)
            )
            sb.table("ai_sessions").update({
                "message_count": new_count, "last_confidence": fields["confidence"],
                "last_question": question, "last_answer": reply_text,
                "avg_latency_ms": new_avg_latency, "conversation_tier": conversation_tier,
                "search_text": _search_text(question, reply_text, fields["chunks"], None, None),
                "updated_at": _now_iso(), "last_message_at": _now_iso(),
            }).eq("id", session_id).execute()

            return self.get_session(session_id)
        except Exception as e:
            print(f"[SessionService] record_conversation_turn failed: {e}")
            return None

    # ── Compare ──────────────────────────────────────────────────
    def compare_sessions(self, session_id_a: str, session_id_b: str) -> Optional[Dict]:
        a, b = self.get_session(session_id_a), self.get_session(session_id_b)
        if not a or not b:
            return None

        def _last_assistant(session):
            for m in reversed(session["messages"]):
                if m["role"] == "assistant":
                    return m
            return None

        ma, mb = _last_assistant(a), _last_assistant(b)
        ta = ma.get("trace") if ma else None
        tb = mb.get("trace") if mb else None

        return {
            "a": {"id": a["id"], "name": a["name"], "question": a.get("last_question"), "answer": a.get("last_answer")},
            "b": {"id": b["id"], "name": b["name"], "question": b.get("last_question"), "answer": b.get("last_answer")},
            "prompt_diff": {
                "a": (ta or {}).get("prompt", {}).get("final_prompt", ""),
                "b": (tb or {}).get("prompt", {}).get("final_prompt", ""),
            },
            "answer_diff": {"a": a.get("last_answer") or "", "b": b.get("last_answer") or ""},
            "chunks_diff": {
                "a": [c.get("source") or c.get("file_name") for c in (ta or {}).get("chunks", [])],
                "b": [c.get("source") or c.get("file_name") for c in (tb or {}).get("chunks", [])],
            },
            "policy_diff": {
                "a": [v.get("name") for v in (ta or {}).get("policy", {}).get("verdicts", []) if v.get("status") == "triggered"],
                "b": [v.get("name") for v in (tb or {}).get("policy", {}).get("verdicts", []) if v.get("status") == "triggered"],
            },
            "latency_diff": {"a": ma.get("latency_ms") if ma else None, "b": mb.get("latency_ms") if mb else None},
            "tokens_diff": {
                "a": (ma.get("input_tokens") or 0) + (ma.get("output_tokens") or 0) if ma else None,
                "b": (mb.get("input_tokens") or 0) + (mb.get("output_tokens") or 0) if mb else None,
            },
        }

    # ── Export ──────────────────────────────────────────────────
    def export_markdown(self, session_id: str) -> Optional[str]:
        session = self.get_session(session_id)
        if not session:
            return None
        lines = [f"# {session['name']}", "", f"Model: {session.get('model') or 'Not available'}  ",
                 f"Created: {session.get('created_at')}", ""]
        for m in session["messages"]:
            lines.append(f"## {m['role'].title()} (turn {m['turn_index']})")
            lines.append(m["content"])
            if m["role"] == "assistant":
                if m.get("confidence_label"):
                    lines.append(f"\n_Confidence: {m['confidence_label']} ({m.get('confidence')})_")
                trace = m.get("trace") or {}
                chunks = trace.get("chunks") or []
                if chunks:
                    lines.append("\n**Retrieved Chunks:**")
                    for c in chunks:
                        lines.append(f"- {c.get('source') or c.get('file_name') or 'unknown'} (score={c.get('score')})")
                prompt = trace.get("prompt") or {}
                if prompt.get("final_prompt"):
                    lines.append(f"\n**Prompt (v{prompt.get('template_version')}):**\n```\n{prompt['final_prompt']}\n```")
            lines.append("")
        return "\n".join(lines)

    def export_json(self, session_id: str) -> Optional[Dict]:
        return self.get_session(session_id)

    def export_all_json(self, *, search: Optional[str] = None, date_filter: Optional[str] = None,
                         model: Optional[str] = None, status: Optional[str] = None,
                         confidence: Optional[str] = None, channel: Optional[str] = None,
                         limit: int = 200) -> Dict:
        """Bundles every session matching the given filters (the SAME
        filters the Conversation History table applies — "Export All"
        downloads exactly what's currently listed, not literally every row
        in the table regardless of filter) into one JSON document, each
        with its full message/event/trace detail via get_session()."""
        summaries = self.list_sessions(search=search, date_filter=date_filter, model=model,
                                        status=status, confidence=confidence, channel=channel, limit=limit)
        sessions = [self.get_session(s["id"]) for s in summaries]
        return {
            "exported_at": _now_iso(),
            "session_count": len(sessions),
            "sessions": [s for s in sessions if s],
        }


def _matches_date_filter(ts: Optional[str], date_filter: str) -> bool:
    if not ts:
        return False
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    now = datetime.now(timezone.utc)
    days = (now.date() - d.date()).days
    if date_filter == "today":
        return days == 0
    if date_filter == "yesterday":
        return days == 1
    if date_filter == "7d":
        return 0 <= days <= 7
    if date_filter == "30d":
        return 0 <= days <= 30
    return True


_session_service_singleton: Optional[SessionService] = None


def get_session_service() -> SessionService:
    global _session_service_singleton
    if _session_service_singleton is None:
        _session_service_singleton = SessionService()
    return _session_service_singleton
