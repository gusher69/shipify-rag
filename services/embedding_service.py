"""EmbeddingService — the ONE place embeddings are generated, for both
ingestion (documents) and retrieval (queries). Before this module,
rag/searcher.py and ingestion/embedder.py each called
sentence-transformers directly — duplicated logic that made it
impossible to swap providers without editing two files, and impossible to
guarantee query/document embeddings always used the same model.

Provider-neutral interface (`EmbeddingProvider`) with two implementations:
- LocalSentenceTransformerEmbeddingProvider (default, free, already proven
  in production — see config.EMBEDDING_MODEL)
- OpenAIEmbeddingProvider (candidate — see rag/evaluation.py for the
  comparison report required before this is ever made the ACTIVE
  production provider; see migrations/018_embedding_versions.sql for how
  its output is stored without touching the existing local embeddings)

Switching config.EMBEDDING_PROVIDER changes what get_embedding_provider()
returns, but does NOT retroactively re-embed anything already stored —
see ingestion/rebuild_embeddings.py for that explicit, separate step.
"""
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

from config import (
    EMBEDDING_PROVIDER, EMBEDDING_MODEL, EMBEDDING_DIM, EMBEDDING_BATCH_SIZE,
    OPENAI_API_KEY, OPENAI_EMBEDDING_MODEL, OPENAI_EMBEDDING_DIMENSIONS,
)


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """Batched — always prefer this over calling embed_query in a loop
        for ingestion, so providers can actually batch the API/model call."""
        raise NotImplementedError

    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        raise NotImplementedError

    @abstractmethod
    def model_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def dimensions(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def version(self) -> str:
        """A stable identifier for THIS exact (provider, model, dimensions)
        combination — matches embedding_versions.version in
        migrations/018_embedding_versions.sql."""
        raise NotImplementedError

    @property
    def provider_name(self) -> str:
        raise NotImplementedError


def _validate_texts(texts: List[str]) -> List[str]:
    cleaned = [(t or "").strip() for t in texts]
    if any(not t for t in cleaned):
        raise ValueError("embed_documents/embed_query received an empty or whitespace-only text — "
                          "callers must filter these out before requesting an embedding.")
    return cleaned


class LocalSentenceTransformerEmbeddingProvider(EmbeddingProvider):
    provider_name = "local"

    def __init__(self, model_name: str = EMBEDDING_MODEL, dim: int = EMBEDDING_DIM):
        self._model_name = model_name
        self._dim = dim
        self._model = None

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        texts = _validate_texts(texts)
        vectors = self._get_model().encode(texts, batch_size=EMBEDDING_BATCH_SIZE)
        return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> List[float]:
        _validate_texts([text])
        return self._get_model().encode(text).tolist()

    def model_name(self) -> str:
        return self._model_name

    def dimensions(self) -> int:
        return self._dim

    def version(self) -> str:
        return "local-mpnet-v1"


@dataclass
class _OpenAIEmbedResult:
    vectors: List[List[float]]
    input_tokens: int


class OpenAIEmbeddingProvider(EmbeddingProvider):
    provider_name = "openai"

    # Native output dimension per model, used only when
    # config.OPENAI_EMBEDDING_DIMENSIONS isn't explicitly set (i.e. no
    # truncation requested).
    _NATIVE_DIMENSIONS = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(self, model_name: str = OPENAI_EMBEDDING_MODEL,
                 dimensions: Optional[int] = OPENAI_EMBEDDING_DIMENSIONS,
                 max_retries: int = 4, timeout: float = 30.0):
        if not OPENAI_API_KEY:
            raise RuntimeError("OpenAIEmbeddingProvider requires OPENAI_API_KEY to be configured.")
        self._model_name = model_name
        self._dimensions = dimensions or self._NATIVE_DIMENSIONS.get(model_name, 1536)
        self._max_retries = max_retries
        self._timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            # Never log the key itself — only that a client was created.
            self._client = OpenAI(api_key=OPENAI_API_KEY, timeout=self._timeout)
        return self._client

    def _embed_batch_with_retry(self, batch: List[str]) -> _OpenAIEmbedResult:
        kwargs = {"model": self._model_name, "input": batch}
        if OPENAI_EMBEDDING_DIMENSIONS:
            kwargs["dimensions"] = OPENAI_EMBEDDING_DIMENSIONS

        last_exc = None
        for attempt in range(self._max_retries):
            try:
                resp = self._get_client().embeddings.create(**kwargs)
                vectors = [d.embedding for d in resp.data]
                for v in vectors:
                    if len(v) != self._dimensions:
                        raise ValueError(
                            f"OpenAI returned a {len(v)}-dim vector but this provider is "
                            f"configured for {self._dimensions} dims (model={self._model_name}, "
                            f"requested dimensions param={OPENAI_EMBEDDING_DIMENSIONS}). Refusing "
                            f"to store a mismatched vector."
                        )
                tokens = getattr(getattr(resp, "usage", None), "total_tokens", 0) or 0
                print(f"[embedding_service] OpenAI embed batch: model={self._model_name} "
                      f"n={len(batch)} tokens={tokens}")
                return _OpenAIEmbedResult(vectors=vectors, input_tokens=tokens)
            except Exception as e:
                last_exc = e
                is_rate_limit = "rate" in str(e).lower() or "429" in str(e)
                wait = min(2 ** attempt, 20) * (2 if is_rate_limit else 1)
                print(f"[embedding_service] OpenAI embed attempt {attempt+1}/{self._max_retries} "
                      f"failed ({type(e).__name__}); retrying in {wait}s")
                if attempt < self._max_retries - 1:
                    time.sleep(wait)
        raise RuntimeError(f"OpenAI embedding failed after {self._max_retries} attempts: {last_exc}")

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        texts = _validate_texts(texts)
        all_vectors: List[List[float]] = []
        for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[i:i + EMBEDDING_BATCH_SIZE]
            result = self._embed_batch_with_retry(batch)
            all_vectors.extend(result.vectors)
        return all_vectors

    def embed_query(self, text: str) -> List[float]:
        texts = _validate_texts([text])
        result = self._embed_batch_with_retry(texts)
        return result.vectors[0]

    def model_name(self) -> str:
        return self._model_name

    def dimensions(self) -> int:
        return self._dimensions

    def version(self) -> str:
        dim_suffix = f"-{self._dimensions}d" if OPENAI_EMBEDDING_DIMENSIONS else ""
        short = {"text-embedding-3-small": "te3s", "text-embedding-3-large": "te3l"}.get(
            self._model_name, re.sub(r"[^a-z0-9]+", "", self._model_name.lower()))
        return f"openai-{short}{dim_suffix}-v1"


_provider_singleton: Optional[EmbeddingProvider] = None


def get_embedding_provider() -> EmbeddingProvider:
    """The single factory both ingestion and retrieval must call — this
    is what guarantees query and document embeddings always come from the
    same provider/model/dimensions/version."""
    global _provider_singleton
    if _provider_singleton is None:
        if EMBEDDING_PROVIDER == "openai":
            _provider_singleton = OpenAIEmbeddingProvider()
        else:
            _provider_singleton = LocalSentenceTransformerEmbeddingProvider()
    return _provider_singleton


class EmbeddingConfigurationError(RuntimeError):
    """Raised by validate_embedding_configuration() when the runtime
    embedding provider's dimension doesn't match knowledge_chunks'
    actual vector column width — never caught silently, always meant to
    fail an ingestion/sync attempt fast, before any chunk is embedded."""
    pass


def get_expected_db_vector_dimension(sb) -> Optional[int]:
    """Reads knowledge_chunks.embedding's ACTUAL configured width straight
    from Postgres (pg_attribute.atttypmod — for pgvector's `vector` type
    this holds the dimension directly, no -4 offset like varchar) rather
    than assuming a hardcoded constant, so this stays correct across any
    future migration that changes the column width again. Returns None
    (never raises) if the check itself can't run — a missing/unreachable
    DB is a separate failure mode from a real dimension mismatch, and
    callers should not treat "couldn't check" as "definitely mismatched"."""
    try:
        import psycopg2
        from config import SUPABASE_DB_URL
        if not SUPABASE_DB_URL:
            return None
        conn = psycopg2.connect(SUPABASE_DB_URL, connect_timeout=5)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'knowledge_chunks'::regclass "
                "AND attname = 'embedding' AND NOT attisdropped"
            )
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        print(f"[embedding_service] could not read knowledge_chunks.embedding's expected "
              f"dimension from the database: {e}")
        return None


def validate_embedding_configuration(sb=None) -> None:
    """Part of the 2026-07-29 embedding-dimension-mismatch fix (Fix 3).
    Called once before an ingestion/sync run starts embedding ANY chunk
    — never partway through, so a job never appears stuck at partial
    progress because chunk N failed after chunks 1..N-1 already
    succeeded. Compares the ACTIVE runtime provider's dimensions()
    against the database's actual knowledge_chunks.embedding column
    width and raises EmbeddingConfigurationError with a clear, actionable
    message on any mismatch. A DB-unreachable/unknown result is NOT
    treated as a mismatch (see get_expected_db_vector_dimension) — only a
    real, confirmed mismatch blocks ingestion."""
    provider = get_embedding_provider()
    runtime_dim = provider.dimensions()
    expected_dim = get_expected_db_vector_dimension(sb)
    if expected_dim is not None and runtime_dim != expected_dim:
        raise EmbeddingConfigurationError(
            f"Embedding configuration mismatch: runtime model {provider.model_name()} "
            f"produces {runtime_dim} dimensions, but knowledge_chunks expects {expected_dim} "
            f"dimensions. Fix OPENAI_EMBEDDING_MODEL in Settings/.env before retrying — no "
            f"chunks were embedded for this attempt."
        )


def report_and_validate_embedding_configuration(sb=None) -> Optional[dict]:
    """Startup-time embedding configuration check (2026-08-01 configuration-
    hardening pass). Logs the three numbers an operator needs to see once,
    at boot — active embedding model, active dimension, expected database
    dimension — then delegates the actual pass/fail decision to
    validate_embedding_configuration() (composed, not duplicated: there is
    still exactly one place that decides "mismatch or not").

    Only a CONFIRMED mismatch (both sides known, and different) raises —
    same principle validate_embedding_configuration()/
    get_expected_db_vector_dimension() already use for "DB unreachable is
    not a mismatch". A provider that can't even be constructed (e.g.
    OPENAI_API_KEY unset) is logged and treated as "couldn't check", not
    as a fatal startup error — an unrelated config gap should produce a
    clear log line, not block the whole app from starting the same way a
    genuine vector-dimension mismatch must.
    """
    try:
        provider = get_embedding_provider()
    except Exception as e:
        print(f"[embedding_service] Startup embedding check skipped — could not construct the "
              f"embedding provider ({type(e).__name__}: {e}). Fix OPENAI_API_KEY / "
              f"EMBEDDING_PROVIDER before relying on ingestion or search.")
        return None

    runtime_dim = provider.dimensions()
    expected_dim = get_expected_db_vector_dimension(sb)
    print(f"[embedding_service] Startup embedding check — active model: {provider.model_name()} "
          f"({provider.provider_name}) | active dimension: {runtime_dim} | "
          f"expected DB dimension: "
          f"{expected_dim if expected_dim is not None else 'unknown (DB unreachable at startup)'}")

    validate_embedding_configuration(sb)  # raises EmbeddingConfigurationError on a CONFIRMED mismatch only
    return {"provider": provider.provider_name, "model": provider.model_name(),
            "runtime_dimension": runtime_dim, "expected_db_dimension": expected_dim}


def build_embedding_text(*, file_name: str, document_title: Optional[str] = None,
                          heading_path: Optional[List[str]] = None,
                          section_title: Optional[str] = None,
                          chunk_type: Optional[str] = None,
                          content: str) -> str:
    """The contextual text actually sent to the embedding model — always
    kept SEPARATE from the chunk's display text (see
    ingestion/ingest.py::chunk_text, which stores this under
    metadata['embedding_text']; the UI must only ever render
    metadata/content as-is, never embedding_text, as the "source text").
    Deliberately excludes noisy identifiers (chunk IDs, file IDs, UUIDs).
    """
    lines = [f"Document: {file_name}"]
    if document_title and document_title != file_name:
        lines.append(f"Title: {document_title}")
    section = " > ".join(heading_path) if heading_path else (section_title or "")
    if section:
        lines.append(f"Section: {section}")
    if chunk_type:
        lines.append(f"Type: {chunk_type}")
    lines.append("Content:")
    lines.append(content)
    return "\n".join(lines)
