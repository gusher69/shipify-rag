"""Zero-Quota Interactive Chat static guard (LINE Messaging Cost Audit,
2026-08-25).

No centralized outbound-LINE-message service exists in this codebase (a
larger abstraction was deliberately NOT introduced for this — the audit
found nothing that needed converting: line_bot/webhook.py is the ONLY
module that ever calls a linebot.v3.messaging.MessagingApi method, and
the only method it ever calls, at both call sites, is reply_message).

This is the smallest safe protection instead: a repo-wide static scan
that fails the moment ANY .py file calls .push_message(/.multicast(/
.broadcast(/.narrowcast( on a LINE Messaging API client, UNLESS that
file is in the explicit allowlist below. Today the allowlist is empty —
there is nothing to send a counted LINE message anywhere in this
codebase. A future developer who accidentally changes an interactive
Reply into a Push (or adds a new proactive feature without updating this
allowlist deliberately) gets a failing test, not silent paid-message
usage."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files allowed to call a counted LINE send method — empty today. Adding
# a genuine, deliberate proactive-notification feature (Class D in the
# audit: a real business-initiated push with no incoming replyToken)
# means adding its file path here explicitly, not silently.
ALLOWLISTED_COUNTED_SEND_FILES: set = set()

_COUNTED_METHOD_RE = re.compile(r"\.(push_message|multicast|broadcast|narrowcast)\s*\(")

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def _iter_repo_py_files():
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


class TestNoCountedLineSendOutsideAllowlist(unittest.TestCase):
    def test_no_push_multicast_broadcast_narrowcast_call_anywhere(self):
        offenders = []
        for path in _iter_repo_py_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWLISTED_COUNTED_SEND_FILES:
                continue
            # Never scan this guard's own file — it necessarily mentions
            # the method names in comments/docstrings/regex, which would
            # otherwise trip on itself.
            if rel == Path(__file__).relative_to(REPO_ROOT).as_posix():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in _COUNTED_METHOD_RE.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{rel}:{line_no} -> {match.group(0)}")

        self.assertEqual(
            offenders, [],
            "Found a counted LINE send method (push_message/multicast/broadcast/"
            "narrowcast) outside the explicit allowlist. If this is a genuine, "
            "deliberate proactive notification feature, add its file to "
            "ALLOWLISTED_COUNTED_SEND_FILES in this test with a comment "
            "explaining the business requirement. If it's an interactive chat "
            "path, it must use reply_message instead.\n" + "\n".join(offenders))

    def test_reply_message_still_used_somewhere(self):
        """Sanity check that the scan itself works — line_bot/webhook.py's
        two reply_message call sites must still be found, proving the
        regex/file-walk isn't silently matching nothing."""
        found = False
        for path in _iter_repo_py_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel != "line_bot/webhook.py":
                continue
            text = path.read_text(encoding="utf-8")
            found = ".reply_message(" in text
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
