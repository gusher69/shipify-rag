"""Conversation Analytics — Platform Service (Part 9/10).

NO DATABASE MIGRATION THIS SPRINT — analytics is in-memory only, but
built behind a storage ABSTRACTION (`ConversationAnalyticsStore`) so a
future `PostgresConversationAnalyticsStore` can be swapped in later
without changing the Conversation Strategy Engine, the ERP Test
Harness, or any route/UI code. No consumer may reach into a bare dict
directly — everything goes through `get_analytics_store()`.

Record shape (Part 9):
    {
        "conversation_id": str, "action_id": Optional[str],
        "started_at": iso str, "completed_at": Optional[iso str],
        "total_turns": int,
        "questions_asked": [field/group names ...],
        "questions_skipped": [field/group names ...],
        "auto_filled_fields": [field names ...],
        "detected_fields": [field names ...],
        "confidence_scores": {field_name: float},
        "confirmation_result": Optional[bool],
        "execution_result": Optional[str],   # e.g. "completed"/"cancelled"/"error"
    }

No secret values are ever recorded here (standing constraint) — callers
must only pass field NAMES/booleans/scores, never raw field values.
"""
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationAnalyticsStore(ABC):
    @abstractmethod
    def record_turn(self, record: Dict) -> None: ...

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Optional[Dict]: ...

    @abstractmethod
    def list_conversations(self, action_id: Optional[str] = None, limit: int = 100) -> List[Dict]: ...

    @abstractmethod
    def compute_metrics(self, action_id: Optional[str] = None) -> Dict: ...


class InMemoryConversationAnalyticsStore(ConversationAnalyticsStore):
    """Part 9's in-memory implementation. One row per conversation_id,
    updated in place on each turn (`record_turn` merges the incoming
    per-turn delta into the running conversation record rather than
    appending a new row every call)."""

    def __init__(self):
        self._conversations: Dict[str, Dict] = {}

    def record_turn(self, record: Dict) -> None:
        conversation_id = record.get("conversation_id")
        if not conversation_id:
            return
        existing = self._conversations.get(conversation_id)
        if existing is None:
            existing = {
                "conversation_id": conversation_id,
                "action_id": record.get("action_id"),
                "started_at": record.get("started_at") or _now_iso(),
                "completed_at": None,
                "total_turns": 0,
                "questions_asked": [],
                "questions_skipped": [],
                "auto_filled_fields": [],
                "detected_fields": [],
                "confidence_scores": {},
                "confirmation_result": None,
                "execution_result": None,
            }
            self._conversations[conversation_id] = existing

        existing["total_turns"] += 1
        if record.get("action_id"):
            existing["action_id"] = record["action_id"]
        for key in ("questions_asked", "questions_skipped", "auto_filled_fields", "detected_fields"):
            for item in record.get(key) or []:
                if item not in existing[key]:
                    existing[key].append(item)
        existing["confidence_scores"].update(record.get("confidence_scores") or {})
        if record.get("confirmation_result") is not None:
            existing["confirmation_result"] = record["confirmation_result"]
        if record.get("execution_result") is not None:
            existing["execution_result"] = record["execution_result"]
            existing["completed_at"] = _now_iso()

    def get_conversation(self, conversation_id: str) -> Optional[Dict]:
        row = self._conversations.get(conversation_id)
        return dict(row) if row else None

    def list_conversations(self, action_id: Optional[str] = None, limit: int = 100) -> List[Dict]:
        rows = list(self._conversations.values())
        if action_id:
            rows = [r for r in rows if r.get("action_id") == action_id]
        rows.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return [dict(r) for r in rows[:limit]]

    # ── Part 10 — Conversation Metrics ──────────────────────────────────
    def compute_metrics(self, action_id: Optional[str] = None) -> Dict:
        rows = self.list_conversations(action_id=action_id, limit=10_000)
        n = len(rows)
        if n == 0:
            return {
                "conversation_count": 0, "average_questions": 0.0, "average_turns": 0.0,
                "auto_fill_rate": 0.0, "detection_accuracy": None, "skip_rate": 0.0,
                "confirmation_rate": 0.0, "cancellation_rate": 0.0,
                "most_asked_questions": [], "most_auto_filled_fields": [],
                "top_cancelled_actions": [], "top_missing_fields": [],
                "note": "no conversations recorded yet",
            }

        total_asked = sum(len(r["questions_asked"]) for r in rows)
        total_skipped = sum(len(r["questions_skipped"]) for r in rows)
        total_auto_filled = sum(len(r["auto_filled_fields"]) for r in rows)
        total_detected = sum(len(r["detected_fields"]) for r in rows)
        total_turns = sum(r["total_turns"] for r in rows)
        confirmed = sum(1 for r in rows if r.get("confirmation_result") is True)
        confirmation_eligible = sum(1 for r in rows if r.get("confirmation_result") is not None)
        cancelled = sum(1 for r in rows if r.get("execution_result") == "cancelled")

        asked_counts: Dict[str, int] = {}
        for r in rows:
            for q in r["questions_asked"]:
                asked_counts[q] = asked_counts.get(q, 0) + 1
        auto_filled_counts: Dict[str, int] = {}
        for r in rows:
            for f in r["auto_filled_fields"]:
                auto_filled_counts[f] = auto_filled_counts.get(f, 0) + 1
        cancelled_actions: Dict[str, int] = {}
        for r in rows:
            if r.get("execution_result") == "cancelled" and r.get("action_id"):
                cancelled_actions[r["action_id"]] = cancelled_actions.get(r["action_id"], 0) + 1

        def _top(d: Dict[str, int], k: int = 10):
            return [{"name": name, "count": count} for name, count in sorted(d.items(), key=lambda t: t[1], reverse=True)[:k]]

        # Detection Accuracy — honestly: we have no ground-truth
        # correction-tracking mechanism from the runtime this sprint (no
        # "the customer corrected an auto-filled value" signal exists
        # yet), so this is left as None with an explicit note rather than
        # fabricated. What IS honestly computable is the auto-fill rate
        # (detected-and-applied vs. detected total).
        detection_accuracy = None

        return {
            "conversation_count": n,
            "average_questions": round(total_asked / n, 2),
            "average_turns": round(total_turns / n, 2),
            "auto_fill_rate": round(total_auto_filled / total_detected, 4) if total_detected else 0.0,
            "detection_accuracy": detection_accuracy,
            "detection_accuracy_note": ("No correction-tracking signal exists in the runtime yet "
                                        "(the customer contradicting/correcting an auto-filled value "
                                        "is not currently recorded) — reporting None rather than "
                                        "fabricating a number."),
            "skip_rate": round(total_skipped / (total_asked + total_skipped), 4) if (total_asked + total_skipped) else 0.0,
            "confirmation_rate": round(confirmed / confirmation_eligible, 4) if confirmation_eligible else 0.0,
            "cancellation_rate": round(cancelled / n, 4),
            "most_asked_questions": _top(asked_counts),
            "most_auto_filled_fields": _top(auto_filled_counts),
            "top_cancelled_actions": _top(cancelled_actions),
            "top_missing_fields": _top(asked_counts),  # questions_asked IS "what was still missing"
        }


_default_store: Optional[ConversationAnalyticsStore] = None


def get_analytics_store() -> ConversationAnalyticsStore:
    """The ONE factory every consumer (routes, dashboard, runtime) must
    go through — never a bare module-level dict reached into directly."""
    global _default_store
    if _default_store is None:
        _default_store = InMemoryConversationAnalyticsStore()
    return _default_store


def reset_analytics_store_for_tests() -> None:
    """Test-only helper — resets the module-level default store so tests
    don't leak state into one another."""
    global _default_store
    _default_store = InMemoryConversationAnalyticsStore()
