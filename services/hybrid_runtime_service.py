"""Hybrid Runtime Service — the shared, production-safe synthesis layer
for combining an ERP result and a RAG result into one explicit, labeled
response.

Extracted from services/hybrid_playground_router.py (2026-08-02
Production Integration Sprint, Phase 1 Step B) so services/
decision_engine.py (production) and the AI Playground's
hybrid-playground/ask route can both consume the SAME implementation —
the Decision Engine must never import a Playground-only module.
services/hybrid_playground_router.py re-imports and re-exports
`synthesize_hybrid_answer` under its own name for backward compatibility
(existing imports/tests keep working unchanged); it is otherwise now a
thin Playground-only adapter (the naive Auto-mode keyword heuristic and
the legacy naive concatenation), never production code.

Moved verbatim — no behavior change.
"""
from typing import Dict, List, Optional


def synthesize_hybrid_answer(*, erp_answer: Optional[str] = None, erp_error: Optional[str] = None,
                              rag_answer: Optional[str] = None, rag_error: Optional[str] = None,
                              rag_citations: Optional[List[str]] = None) -> Dict:
    """Minimal, explicit synthesis (2026-08-02 Hybrid Question
    Segmentation sprint) — NOT an LLM-based rewrite, NOT intelligent
    reasoning across the two sources. Combines the ERP result and the
    RAG result into two clearly labeled, structured sections:

    - ERP section: customer-specific data, or an honest unavailability
      note if the ERP call errored — NEVER a RAG citation attached to it.
    - RAG section: policy/knowledge-base content, with its own citations
      listed underneath it only.

    A path that simply didn't run (None, no error) contributes no
    section at all — never a fabricated placeholder pretending it ran."""
    erp_section = f"ไม่สามารถดึงข้อมูลลูกค้าได้ในขณะนี้: {erp_error}" if erp_error else (erp_answer or None)
    rag_section = f"ไม่สามารถค้นหาข้อมูลจากฐานความรู้ได้ในขณะนี้: {rag_error}" if rag_error else (rag_answer or None)

    parts = []
    if erp_section:
        parts.append(f"📦 ข้อมูลเฉพาะลูกค้า (ERP):\n{erp_section}")
    if rag_section:
        citation_block = ("\n\nแหล่งที่มา:\n" + "\n".join(f"- {c}" for c in rag_citations)) if rag_citations else ""
        parts.append(f"📘 ข้อมูลนโยบาย/ความรู้ทั่วไป (Knowledge Base):\n{rag_section}{citation_block}")

    strategy = ("minimal explicit synthesis — structured, labeled sections per source; "
                "no LLM rewrite, no cross-domain blending, citations attached only to the RAG section")
    if not parts:
        return {"merged_answer": "ไม่พบคำตอบทั้งจาก ERP และฐานความรู้ค่ะ",
                "erp_section": None, "rag_section": None, "strategy": strategy}
    return {"merged_answer": "\n\n".join(parts), "erp_section": erp_section, "rag_section": rag_section,
            "strategy": strategy}
