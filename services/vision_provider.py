"""Vision analysis provider abstraction — used ONLY for pages that
TextQualityEvaluator/page_classifier flagged as needing visual analysis
(corrupted/sparse text, image-only pages, tables/infographics). Never
called on every page by default (cost control) — see
services/pdf_page_pipeline.py for the gating logic.

Reuses services/llm_service.get_llm_service() (this project's ONLY
sanctioned place to call an LLM) with a multimodal (image + text)
message — no separate OpenAI client is created here.
"""
import base64
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw_text: str) -> dict:
    """Vision models often wrap JSON in markdown code fences (```json ... ```)
    or add stray text around it. Try progressively looser extraction before
    giving up, so the common fenced-JSON case doesn't fall through to the
    raw-text Failsafe path."""
    text = raw_text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except Exception:
            pass
    obj_match = _JSON_OBJECT_RE.search(text)
    if obj_match:
        return json.loads(obj_match.group(0))
    raise ValueError("No JSON object found in Vision response")


@dataclass
class VisionResult:
    title: Optional[str]
    headings: List[str]
    visible_text: str
    tables: List[Dict]          # [{"title": ..., "headers": [...], "rows": [[...]]}]
    captions: List[str]
    image_description: Optional[str]
    relationships: List[str]
    markdown: str                # final structured Markdown built from the above
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Optional[Dict] = field(default=None, repr=False)


class VisionProvider(ABC):
    @abstractmethod
    def analyze_page_image(self, image_bytes: bytes, *, context_hint: str = "") -> VisionResult:
        ...


class NullVisionProvider(VisionProvider):
    """Disabled — used when the analysis profile is 'disabled' or 'basic',
    or when the admin hasn't enabled Vision. Never makes an API call."""
    def analyze_page_image(self, image_bytes: bytes, *, context_hint: str = "") -> VisionResult:
        return VisionResult(title=None, headings=[], visible_text="", tables=[], captions=[],
                             image_description=None, relationships=[], markdown="",
                             model="none", raw=None)


_VISION_SYSTEM_PROMPT = (
    "You are a document-structure extraction assistant. You will be shown one page image from a "
    "brochure, insurance document, or similar business document. Extract ONLY what is visibly present "
    "-- never invent facts, numbers, or labels that are not in the image. "
    "Respond with ONLY a JSON object with these exact keys: title (string or null), "
    "headings (array of strings), visible_text (string -- all readable text on the page, in reading "
    "order), tables (array of objects, each with title (string or null), headers (array of strings), "
    "rows (array of arrays of strings) -- preserve every table exactly as rows/columns, never flatten "
    "a table into prose), captions (array of strings -- captions for any figures/photos), "
    "image_description (string or null -- a short factual description ONLY if the page is dominated "
    "by a photo/illustration with no meaningful text; null otherwise), relationships (array of short "
    "strings describing important visible relationships, e.g. 'Plan 4 -> Room per day -> 7,000 baht'). "
    "Preserve units, currency symbols, and footnote markers exactly as shown. Do not translate text."
)


class OpenAIVisionProvider(VisionProvider):
    """Real implementation — uses gpt-4o's vision input via
    services.llm_service.get_llm_service(). This is the ONLY component in
    this pipeline that spends money per call; every caller must gate it
    behind an explicit analysis-profile check (see pdf_page_pipeline.py)."""

    def __init__(self, model: Optional[str] = None):
        from config import OPENAI_CHAT_MODEL
        self._model = model or OPENAI_CHAT_MODEL

    def analyze_page_image(self, image_bytes: bytes, *, context_hint: str = "") -> VisionResult:
        from services.llm_service import get_llm_service
        b64 = base64.b64encode(image_bytes).decode("ascii")
        user_content = [
            {"type": "text", "text": context_hint or "Extract this page's structure."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        messages = [
            {"role": "system", "content": _VISION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        llm = get_llm_service()
        resp = llm.generate(messages, model=self._model, temperature=0.0, max_tokens=1500)

        try:
            parsed = _extract_json(resp.text)
        except Exception:
            # Model didn't return clean JSON — degrade to a plain-text
            # result rather than failing the whole page (Failsafe).
            return VisionResult(title=None, headings=[], visible_text=resp.text, tables=[],
                                 captions=[], image_description=None, relationships=[],
                                 markdown=resp.text, model=self._model,
                                 input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, raw=resp.raw)

        from services.table_markdown import build_table_markdown, validate_table_shape
        md_parts = []
        if parsed.get("title"):
            md_parts.append(f"# {parsed['title']}")
        for h in parsed.get("headings") or []:
            md_parts.append(f"## {h}")
        if parsed.get("visible_text"):
            md_parts.append(parsed["visible_text"])
        for t in parsed.get("tables") or []:
            t_headers = t.get("headers") or []
            t_rows = t.get("rows") or []
            numeric_cols = [i for i, h in enumerate(t_headers) if i > 0]
            md_parts.append(build_table_markdown(t_headers, t_rows, title=t.get("title"),
                                                  numeric_columns=numeric_cols))
            issues = validate_table_shape(t_headers, t_rows)
            for issue in issues:
                # Flagged, not silently dropped — a malformed table (e.g. a
                # missing row-label column) still ships with its numeric
                # data, but with a visible warning attached so it can be
                # caught in review/import-preview instead of misleading a
                # downstream RAG answer into attaching a value to the wrong
                # benefit label.
                md_parts.append(f"> ⚠ Table structure warning: {issue}")
        for c in parsed.get("captions") or []:
            md_parts.append(f"_{c}_")
        if parsed.get("image_description"):
            md_parts.append(f"> {parsed['image_description']}")

        return VisionResult(
            title=parsed.get("title"), headings=parsed.get("headings") or [],
            visible_text=parsed.get("visible_text") or "", tables=parsed.get("tables") or [],
            captions=parsed.get("captions") or [], image_description=parsed.get("image_description"),
            relationships=parsed.get("relationships") or [],
            markdown="\n\n".join(p for p in md_parts if p and p.strip()),
            model=self._model, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, raw=resp.raw,
        )


def get_vision_provider(enabled: bool) -> VisionProvider:
    return OpenAIVisionProvider() if enabled else NullVisionProvider()
