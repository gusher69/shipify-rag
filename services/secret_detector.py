"""Local, deterministic secret detection — runs BEFORE anything is sent
to an LLM. Never relies on the model to identify a secret; only asks
the model to help name/classify what this module already found.

Detects secret-shaped name/value pairs in: cURL headers (-H), cURL form
fields (-d/--data/-F), JSON request bodies, URL-encoded bodies, query
strings, HTTP Basic Auth, and free-text documentation examples
("SecretCode: abc123", "SecretCode=abc123").
"""
import base64
import json
import re
from typing import Dict, List, Tuple
from urllib.parse import parse_qsl, urlparse

# Semantic, case-insensitive substring matching over a NORMALIZED name
# (separators stripped) rather than a fixed exact-name list — so
# "SecretCode", "secret_code", "CLIENT_SECRET", "ApiToken", "accessToken",
# "auth_token", and "passcode" are ALL recognized, not just an exact
# match against one literal spelling. Deliberately broad: a false
# positive here just means one extra parameter routed to the Credential
# Store review step (never silently lost); a false negative would leak
# a real secret, which is the worse failure mode.
_SECRET_KEYWORDS = (
    "secretcode", "secret", "apikey", "xapikey", "token", "accesstoken", "authtoken",
    "authorization", "bearer", "password", "passcode", "clientsecret", "privatekey",
    "credential", "signature",
)


def is_secret_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", (name or "").strip().lower())
    if not normalized:
        return False
    return any(keyword in normalized for keyword in _SECRET_KEYWORDS)


class DetectedSecret:
    def __init__(self, name: str, value: str, location: str):
        self.name = name
        self.value = value
        self.location = location  # "header" | "body_form" | "body_json" | "query_string" | "basic_auth" | "text"

    def to_dict(self) -> Dict:
        return {"name": self.name, "value": self.value, "location": self.location}


def _detect_in_headers(text: str) -> List[DetectedSecret]:
    found = []
    for m in re.finditer(r"(?:-H|--header)\s+[\"']?([\w-]+)\s*:\s*([^\"'\n]+)", text):
        name, value = m.group(1).strip(), m.group(2).strip()
        if name.lower() == "authorization":
            bearer_m = re.match(r"Bearer\s+(.+)", value, re.IGNORECASE)
            if bearer_m:
                found.append(DetectedSecret("Authorization", bearer_m.group(1).strip(), "header"))
                continue
            basic_m = re.match(r"Basic\s+(.+)", value, re.IGNORECASE)
            if basic_m:
                found.extend(_decode_basic_auth(basic_m.group(1).strip()))
                continue
            found.append(DetectedSecret("Authorization", value, "header"))
        elif is_secret_name(name):
            found.append(DetectedSecret(name, value, "header"))
    for m in re.finditer(r"^([\w-]+)\s*:\s*(.+)$", text, re.MULTILINE):
        name, value = m.group(1).strip(), m.group(2).strip()
        if name.lower() == "authorization":
            bearer_m = re.match(r"Bearer\s+(.+)", value, re.IGNORECASE)
            if bearer_m:
                value = bearer_m.group(1).strip()
        if is_secret_name(name) and not any(f.value == value for f in found):
            found.append(DetectedSecret(name, value, "header"))
    return found


def _decode_basic_auth(b64_value: str) -> List[DetectedSecret]:
    try:
        decoded = base64.b64decode(b64_value).decode("utf-8", errors="ignore")
        if ":" in decoded:
            user, pwd = decoded.split(":", 1)
            return [DetectedSecret("Basic Auth Password", pwd, "basic_auth")]
    except Exception:
        pass
    return [DetectedSecret("Authorization", b64_value, "basic_auth")]


def _detect_in_form_fields(text: str) -> List[DetectedSecret]:
    found = []
    for m in re.finditer(r"(?:-d|--data(?:-raw|-urlencode)?|-F|--form)\s+[\"']?([^\"'\n]+)", text):
        payload = m.group(1)
        for pair in re.split(r"[&\n]", payload):
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            name, value = name.strip(), value.strip()
            if is_secret_name(name) and value:
                found.append(DetectedSecret(name, value, "body_form"))
    # A bare "key=value&key2=value2" line (no cURL flag at all — e.g.
    # pasted directly from documentation) — split on "&" first so a
    # greedy value never swallows a trailing "&other_field=..." pair.
    for line in text.splitlines():
        if "&" in line or ("=" in line and ":" not in line):
            for pair in line.split("&"):
                if "=" not in pair:
                    continue
                name, _, value = pair.partition("=")
                name, value = name.strip(), value.strip()
                if is_secret_name(name) and value and not any(f.value == value for f in found):
                    found.append(DetectedSecret(name, value, "body_form"))
    for m in re.finditer(r"^\s*([\w-]+)\s*:\s*([^\s,;]+)\s*$", text, re.MULTILINE):
        name, value = m.group(1).strip(), m.group(2).strip()
        if is_secret_name(name) and not any(f.value == value for f in found):
            found.append(DetectedSecret(name, value, "text"))
    return found


def _detect_in_json_bodies(text: str) -> List[DetectedSecret]:
    found = []
    for m in re.finditer(r"\{[^{}]*\}", text, re.DOTALL):
        try:
            data = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            if is_secret_name(str(k)) and isinstance(v, (str, int, float)) and str(v):
                found.append(DetectedSecret(str(k), str(v), "body_json"))
    return found


def _detect_in_query_string(text: str) -> List[DetectedSecret]:
    found = []
    for m in re.finditer(r"https?://\S+", text):
        parsed = urlparse(m.group(0))
        if not parsed.query:
            continue
        for name, value in parse_qsl(parsed.query):
            if is_secret_name(name) and value:
                found.append(DetectedSecret(name, value, "query_string"))
    return found


def detect_secrets(raw_text: str) -> List[DetectedSecret]:
    """Runs every detector and returns a de-duplicated list (same name+
    value pair kept once, first location wins)."""
    if not raw_text:
        return []
    all_found = (
        _detect_in_headers(raw_text) + _detect_in_query_string(raw_text) +
        _detect_in_json_bodies(raw_text) + _detect_in_form_fields(raw_text)
    )
    seen = set()
    deduped = []
    for s in all_found:
        key = (s.name.lower(), s.value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)
    return deduped


def redact_detected_secrets(raw_text: str, detected: List[DetectedSecret]) -> Tuple[str, List[Dict]]:
    """Replaces every detected secret VALUE with a numbered placeholder,
    preserving parameter name/location/auth-type context so the LLM can
    still reason about structure. Returns (redacted_text, redaction_log)
    — the log records name/location only, never the value."""
    redacted = raw_text
    log = []
    for i, s in enumerate(detected, start=1):
        placeholder = f"[REDACTED_SECRET_{i}]"
        if s.value and s.value in redacted:
            redacted = redacted.replace(s.value, placeholder)
        log.append({"name": s.name, "location": s.location, "placeholder": placeholder})
    return redacted, log
