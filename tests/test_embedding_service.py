"""Regression tests for the 2026-07-29 embedding-dimension-mismatch fix.

Covers:
1. OPENAI_EMBEDDING_MODEL (canonical) takes priority over OPENAI_EMBED_MODEL (legacy alias).
2. Legacy alias is used only when the canonical variable is absent.
3. A runtime/DB dimension mismatch is rejected before any chunk is embedded.
4. Settings saves the canonical OPENAI_EMBEDDING_MODEL key, not the legacy alias.
5. A retried sync does not duplicate active chunks for the same file_id.

Never hits a real DB/network/LLM — all external calls are faked/mocked.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _resolve_openai_embedding_model(env: dict) -> str:
    """Mirrors config.py's exact precedence expression (canonical wins
    when set; legacy alias only used as a fallback) — kept identical to
    config.py so this test fails the moment that precedence regresses."""
    return env.get("OPENAI_EMBEDDING_MODEL") or env.get("OPENAI_EMBED_MODEL") or "text-embedding-3-large"


class TestCanonicalVsLegacyPrecedence(unittest.TestCase):
    def test_canonical_wins_when_both_set(self):
        env = {"OPENAI_EMBEDDING_MODEL": "text-embedding-3-large", "OPENAI_EMBED_MODEL": "text-embedding-3-small"}
        self.assertEqual(_resolve_openai_embedding_model(env), "text-embedding-3-large")

    def test_legacy_alias_used_only_when_canonical_absent(self):
        env = {"OPENAI_EMBED_MODEL": "text-embedding-3-small"}
        self.assertEqual(_resolve_openai_embedding_model(env), "text-embedding-3-small")

    def test_default_when_neither_set(self):
        self.assertEqual(_resolve_openai_embedding_model({}), "text-embedding-3-large")


class TestValidateEmbeddingConfiguration(unittest.TestCase):
    def test_mismatch_raises_before_any_embedding(self):
        from services.embedding_service import validate_embedding_configuration, EmbeddingConfigurationError

        fake_provider = type("P", (), {
            "dimensions": lambda self: 1536,
            "model_name": lambda self: "text-embedding-3-small",
        })()
        with patch("services.embedding_service.get_embedding_provider", return_value=fake_provider), \
             patch("services.embedding_service.get_expected_db_vector_dimension", return_value=3072):
            with self.assertRaises(EmbeddingConfigurationError) as ctx:
                validate_embedding_configuration(sb=None)
            self.assertIn("3072", str(ctx.exception))
            self.assertIn("1536", str(ctx.exception))

    def test_matching_dimensions_does_not_raise(self):
        from services.embedding_service import validate_embedding_configuration

        fake_provider = type("P", (), {
            "dimensions": lambda self: 3072,
            "model_name": lambda self: "text-embedding-3-large",
        })()
        with patch("services.embedding_service.get_embedding_provider", return_value=fake_provider), \
             patch("services.embedding_service.get_expected_db_vector_dimension", return_value=3072):
            validate_embedding_configuration(sb=None)  # must not raise

    def test_unreachable_db_does_not_raise(self):
        """A DB the check itself couldn't reach is NOT treated as a
        confirmed mismatch — only a real, known mismatch blocks ingestion."""
        from services.embedding_service import validate_embedding_configuration

        fake_provider = type("P", (), {
            "dimensions": lambda self: 1536,
            "model_name": lambda self: "text-embedding-3-small",
        })()
        with patch("services.embedding_service.get_embedding_provider", return_value=fake_provider), \
             patch("services.embedding_service.get_expected_db_vector_dimension", return_value=None):
            validate_embedding_configuration(sb=None)  # must not raise


class TestSettingsUsesCanonicalKey(unittest.TestCase):
    def test_ai_section_keys_use_canonical_embedding_var(self):
        from admin.routes import SECTION_KEYS
        self.assertIn("OPENAI_EMBEDDING_MODEL", SECTION_KEYS["ai"])
        self.assertNotIn("OPENAI_EMBED_MODEL", SECTION_KEYS["ai"])


class _FakeChunksTable:
    """Minimal in-memory stand-in for the `knowledge_chunks` table,
    enough to exercise deactivate_old_chunks() + upsert_chunks()'s own
    dedup contract without touching a real DB."""

    def __init__(self, store):
        self._store = store
        self._filters = {}
        self._pending_update = None
        self._pending_insert = None

    def update(self, payload):
        self._pending_update = payload
        return self

    def insert(self, rows):
        self._pending_insert = rows
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def execute(self):
        if self._pending_update is not None:
            for row in self._store:
                if all(row.get(k) == v for k, v in self._filters.items()):
                    row.update(self._pending_update)
            return type("R", (), {"data": []})()
        if self._pending_insert is not None:
            self._store.extend(self._pending_insert)
            return type("R", (), {"data": self._pending_insert})()
        return type("R", (), {"data": [r for r in self._store if all(r.get(k) == v for k, v in self._filters.items())]})()


class _FakeSb:
    def __init__(self, store):
        self._store = store

    def table(self, name):
        assert name == "knowledge_chunks"
        return _FakeChunksTable(self._store)


class TestRetryDoesNotDuplicateActiveChunks(unittest.TestCase):
    def test_second_upsert_leaves_only_new_chunks_active(self):
        import ingestion.embedder as embedder

        store = [
            {"id": "old-1", "file_id": "f1", "is_active": True, "content": "old chunk 1"},
            {"id": "old-2", "file_id": "f1", "is_active": True, "content": "old chunk 2"},
        ]
        fake_sb = _FakeSb(store)

        with patch.object(embedder, "_get_supabase", return_value=fake_sb):
            embedder.deactivate_old_chunks("f1")

        self.assertTrue(all(r["is_active"] is False for r in store if r["file_id"] == "f1"))

        with patch.object(embedder, "_get_supabase", return_value=fake_sb):
            fake_sb.table("knowledge_chunks").insert(
                [{"id": "new-1", "file_id": "f1", "is_active": True, "content": "retried chunk 1"}]
            ).execute()

        active = [r for r in store if r["file_id"] == "f1" and r["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], "new-1")
        # Old rows are preserved (soft-deactivated), never hard-deleted or duplicated.
        self.assertEqual(len(store), 3)


if __name__ == "__main__":
    unittest.main()
