# -*- coding: utf-8 -*-
"""SYSTEM-WIDE CONVERSATION INTELLIGENCE — P2.1 shadow report.

ONE deterministic, read-only query over the persisted
`ai_session_traces.metadata->'conversation_intelligence'` telemetry
(services/conversation_intelligence_telemetry.py). Never mutates
anything; safe to run at any time, as often as needed.

Usage:
    python -m tools.p2_shadow_report [--since 2026-09-10]

Reports, for `sample_source = REAL_LINE` only (the cutover-gate
denominator — task §8 must not be inflated by other traffic):

  LIVE ELIGIBLE TURNS, MATCH / STRUCTURED_IMPROVEMENT / LEGACY_CORRECT /
  STRUCTURED_WRONG / AMBIGUOUS, FRAME PARITY %, UNIT PRESERVATION FAIL,
  STALE JOURNEY TAKEOVER, KNOWN SLOT RE-ASK, LLM AUTHORITY VIOLATION.

OWNER_TEST (a configured owner/tester LINE sender — task P2.1A,
config.OWNER_TEST_LINE_USER_IDS), ADMIN_AUTO, and TEST/OTHER turns are
all reported separately and never mixed into the REAL_LINE denominator.
"""
from __future__ import annotations

import argparse
import json

import config
import psycopg2

_PARITY_CLASSES = ("MATCH", "STRUCTURED_IMPROVEMENT", "LEGACY_CORRECT",
                   "STRUCTURED_WRONG", "AMBIGUOUS")


def _fetch_rows(cur, since: str | None):
    q = """
        SELECT metadata->'conversation_intelligence' AS ci
        FROM ai_session_traces
        WHERE metadata->'conversation_intelligence' IS NOT NULL
    """
    params = []
    if since:
        q += " AND created_at >= %s"
        params.append(since)
    cur.execute(q, params)
    return [r[0] for r in cur.fetchall() if r[0]]


def _summarize(rows: list[dict]) -> dict:
    by_source: dict = {}
    for ci in rows:
        src = ci.get("sample_source") or "OTHER"
        by_source.setdefault(src, []).append(ci)

    def _agg(group: list[dict]) -> dict:
        n = len(group)
        classes = {c: sum(1 for g in group if g.get("parity_class") == c) for c in _PARITY_CLASSES}
        agree = classes["MATCH"] + classes["STRUCTURED_IMPROVEMENT"]
        return {
            "eligible_turns": n,
            **classes,
            "frame_parity_pct": round(100.0 * agree / n, 2) if n else None,
            "unit_preservation_fail": sum(1 for g in group if g.get("unit_preserved") is False),
            "stale_journey_takeover": sum(1 for g in group if g.get("stale_journey_takeover")),
            "known_slot_reask": sum(1 for g in group if g.get("known_slot_reask")),
            "llm_authority_violation": sum(1 for g in group if g.get("llm_authority_violation")),
        }

    real_line = _agg(by_source.get("REAL_LINE", []))
    # P2.1A — a configured owner/tester LINE sender (config.
    # OWNER_TEST_LINE_USER_IDS) is REPORTED separately and MUST NEVER be
    # folded into the REAL_LINE denominator (task §5/§6).
    owner_test = _agg(by_source.get("OWNER_TEST", []))
    admin_auto = _agg(by_source.get("ADMIN_AUTO", []))
    other = _agg(by_source.get("TEST", []) + by_source.get("OTHER", []))

    gate = (real_line["eligible_turns"] >= 200
           and (real_line["frame_parity_pct"] or 0) >= 99.0
           and real_line["STRUCTURED_WRONG"] == 0
           and real_line["llm_authority_violation"] == 0
           and real_line["unit_preservation_fail"] == 0
           and real_line["stale_journey_takeover"] == 0
           and real_line["known_slot_reask"] == 0)

    return {"REAL_LINE": real_line, "OWNER_TEST": owner_test, "ADMIN_AUTO": admin_auto,
            "TEST_OR_OTHER": other, "ready_for_read_cutover": gate}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO timestamp lower bound (optional)")
    args = ap.parse_args()

    conn = psycopg2.connect(config.SUPABASE_DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    try:
        rows = _fetch_rows(cur, args.since)
    finally:
        cur.close()
        conn.close()

    summary = _summarize(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()
    print(f"LIVE ELIGIBLE TURNS (REAL_LINE): {summary['REAL_LINE']['eligible_turns']}")
    print(f"OWNER_TEST TURNS: {summary['OWNER_TEST']['eligible_turns']}")
    print(f"ADMIN_AUTO TURNS: {summary['ADMIN_AUTO']['eligible_turns']}")
    print(f"TEST/OTHER TURNS: {summary['TEST_OR_OTHER']['eligible_turns']}")
    print(f"FRAME PARITY: {summary['REAL_LINE']['frame_parity_pct']}%")
    print(f"READY FOR STRUCTURED READ CUTOVER: {'YES' if summary['ready_for_read_cutover'] else 'NO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
