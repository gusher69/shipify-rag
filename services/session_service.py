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
                       confidence: Optional[str] = None, limit: int = 200) -> List[Dict]:
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
