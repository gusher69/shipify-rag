"""Runs every tests/fixtures/real_line/*.json case through the
tools/replay_real_turn.py harness so `unittest discover` keeps the
captured real-production regressions green.

Each fixture reproduces a real DecisionEngine-visible state; the harness
runs the REAL decide() (ERP HTTP + RAG pipeline mocked) and asserts the
fixture's `expected` block.
"""
import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

from replay_real_turn import replay  # noqa: E402

_FIXTURE_DIR = _ROOT / "tests" / "fixtures" / "real_line"


class RealLineReplayFixtures(unittest.TestCase):
    pass


def _make(fixture_path: Path):
    fx0 = json.loads(fixture_path.read_text(encoding="utf-8"))

    def _test(self):
        fx = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertTrue(replay(fx, verbose=False), f"{fixture_path.name} did not match its expected block")

    # A fixture that documents a still-open bug (its `expected` block is
    # the TARGET behavior, not today's) is registered as an expected
    # failure so the branch suite stays green while the regression is
    # captured. Drop the `exposes_open_bug` key once the fix lands.
    if fx0.get("exposes_open_bug"):
        return unittest.expectedFailure(_test)
    return _test


for _p in sorted(_FIXTURE_DIR.glob("*.json")):
    setattr(RealLineReplayFixtures, f"test_{_p.stem}", _make(_p))


if __name__ == "__main__":
    unittest.main()
