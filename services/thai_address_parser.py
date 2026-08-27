"""Thai Address Parser — Platform-First, metadata-driven layer
(2026-08-20, Shipping Address Change Request sprint).

Two independent, deterministic (no LLM) capabilities, following the exact
convention services/semantic_parameter_inference.py already established:
every signal is read from a parameter's own `field_metadata`, so an action
with no `address_component` tags configured is completely unaffected.

1. Compound request-block parsing — one free-text message ("ผู้รับ หญิง
   0616807329 ที่อยู่ 8/7 ม.8 ต.ตาขัน อ.บ้านค่าย จ.ระยอง 21120", or just the
   bare address block on its own) is split into named components using the
   labels/markers customers actually type: "ผู้รับ"/"ชื่อผู้รับ" for the
   receiver's name, a bare mobile-shaped digit run for the receiver's
   phone, "ที่อยู่"/"ที่อยู่จัดส่ง" for the address line, and the standard
   Thai administrative-division markers in any common form (abbreviated
   "ต./อ./จ." or full "ตำบล/อำเภอ/จังหวัด", Bangkok's
   "แขวง/เขต/กรุงเทพมหานคร"/"กทม."). A trailing 5-digit run is always the
   postal code. Each piece is extracted and REMOVED from the working text
   before the next piece is searched for, specifically so an earlier,
   unrelated mention of the word "ที่อยู่" inside a request phrase (e.g.
   "อยากเปลี่ยนที่อยู่จัดส่ง...") is never mistaken for the actual address
   label -- only the LAST such mention before the first geographic marker
   is treated as the label.

2. Field-level correction detection — "จังหวัดผิด เป็นชลบุรี" names one
   already-known field and its new value, for overwriting a single
   already-collected slot without discarding the others (never possible
   through the normal "only fill what's missing" binding loop).

Never fabricates a component that isn't present in the text.
"""
import re
from typing import Dict, Optional, Tuple

# Geographic component name -> ordered list of marker phrases a customer
# might use. Longer/more specific phrases first so e.g. "กรุงเทพมหานคร" is
# tried before any accidental partial match.
# "กรุงเทพ"/"กรุงเทพฯ" (Bangkok Stuck-Loop fix, P1 audit finding) -- the
# short colloquial form is how the overwhelming majority of real Bangkok
# customers actually write it; only the full "กรุงเทพมหานคร" and the
# abbreviation "กทม." were previously recognized, so a customer answering
# a direct "which province?" question with plain "กรุงเทพ" was silently
# ignored and asked the identical question again forever. Longer/more
# specific aliases still come first (กรุงเทพมหานคร, กรุงเทพฯ) so they are
# never partially shadowed by the shorter "กรุงเทพ".
_BANGKOK_PROVINCE_ALIASES = ("กรุงเทพมหานคร", "กรุงเทพฯ", "กทม.", "กรุงเทพ")
_GEO_MARKERS = (
    ("subdistrict", ("ตำบล", "แขวง", "ต.")),
    ("district", ("อำเภอ", "เขต", "อ.")),
    ("province", _BANGKOK_PROVINCE_ALIASES + ("จังหวัด", "จ.")),
)
_GEO_MARKER_RE = re.compile(
    "|".join(f"(?P<{name}>{'|'.join(re.escape(m) for m in markers)})" for name, markers in _GEO_MARKERS)
)
# Address Change Full UAT fix (2026-08-24) — the trailing 5-digit run
# must be its OWN token, never the tail end of a longer ASCII
# alphanumeric identifier this same message might be answering a
# DIFFERENT parameter with (confirmed live: this pre-pass runs on EVERY
# message while the action has any address_component parameter, so a
# bare ShipmentCode reply like "SP100820260716001" — itself ending in
# "16001" — was silently fabricating a PostalCode of "16001" that was
# never in the message at all). A real postal code is always preceded
# by whitespace, punctuation, a Thai character, or nothing (start of
# string); only an immediately-preceding ASCII letter/digit — i.e. the
# digits are embedded inside a longer code — is rejected. Thai-script
# adjacency (the compact "...จ.ระยอง21120" style, no space) is
# deliberately unaffected by this guard.
_POSTAL_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(\d{5})\s*$")
_PHONE_RE = re.compile(r"(?<!\d)0\d{8,9}(?!\d)")
# A "เบอร์"/"เบอร์โทร" label immediately before the phone digits (natural
# casual phrasing, "...เบอร์ 0812345678...", never guaranteed a colon) is
# stripped along with the number itself — same convention as the postal
# code's own label cleanup below — so it never leaks into a neighbouring
# field (confirmed live, Address Change Full UAT 2026-08-24: leftover
# "เบอร์" text was bleeding into the receiver name / address value).
_PHONE_LABEL_RE = re.compile(r"เบอร์(?:โทร)?\s*[:：]?\s*$")
# Connector words a customer may place directly after the receiver-name
# label with no space/colon at all -- "ชื่อผู้รับใหม่คือ สมชาย ใจดี",
# "ชื่อผู้รับคือ สมชาย ใจดี" (Receiver-Name Field-Stealing fix, P1 audit
# finding) -- reuses the same "เป็น/คือ" value-introducing vocabulary
# _CORRECTION_VALUE_RE below already recognizes, so the label match never
# leaves this connector phrase stuck to the front of the captured name.
# Longer/more specific alternatives first, same convention as every other
# marker list in this file.
_LABEL_CONNECTOR_RE = r"(?:ใหม่คือ|เปลี่ยนเป็น|แก้เป็น|คือ|เป็น)?"
_RECEIVER_NAME_RE = re.compile(
    # Longest/most specific marker first: casual "ผู้รับชื่อสมชาย" (no
    # space) reverses the usual "ชื่อผู้รับ" word order — both are
    # genuinely used by real customers (Address Change Full UAT,
    # 2026-08-24) and must resolve to the same field.
    r"(?:ผู้รับชื่อ|ชื่อผู้รับ|ผู้รับ)\s*[:\-]?\s*" + _LABEL_CONNECTOR_RE + r"\s*(.+?)"
    r"(?=\n|ที่อยู่จัดส่ง|ที่อยู่|อยู่|เบอร์|ตำบล|แขวง|ต\.|อำเภอ|เขต|อ\.|จังหวัด|กรุงเทพมหานคร|กรุงเทพฯ|กทม\.|กรุงเทพ|จ\.|$)"
)
# Bare "อยู่" (no "ที่" prefix) is also a genuine, commonly-used address
# label in casual phrasing ("...อยู่ 99/12 หมู่ 4 ตำบล...") — always
# tried AFTER the longer "ที่อยู่"/"ที่อยู่จัดส่ง" alternatives (which
# already contain "อยู่" as a substring) so a labeled address is never
# double-matched. The existing "must contain a digit or a geo marker to
# count as a real address value" plausibility check below protects
# against a stray, unrelated "อยู่" (e.g. "...ข้อมูลอยู่เลยครับ") ever
# fabricating a false address.
# "ที่อยู่บิลขนส่ง"/"ที่อยู่รับของ"/"ที่อยู่รับสินค้า" (Address Change
# Full UAT fix, 2026-08-24) — these mirror requestshippingaddresschange's
# OWN real configured trigger phrase vocabulary ("ต้องการเปลี่ยนที่อยู่
# บิลขนส่ง", "เปลี่ยนที่อยู่รับของ", "เปลี่ยนที่อยู่รับสินค้า"). Without
# them recognized as ONE compound label, only the shorter "ที่อยู่" prefix
# matched, leaving the descriptor word itself ("บิลขนส่ง"/"รับของ"/
# "รับสินค้า") sitting in front of whatever followed — confirmed live:
# "ต้องการเปลี่ยนที่อยู่บิลขนส่ง SP100820260716001" produced Address=
# "บิลขนส่ง SP100820260716001" (the bare-identifier guard below only
# catches a trailing code with NOTHING else in front of it).
_ADDRESS_LABEL_RE = re.compile(
    r"ที่อยู่บิลขนส่ง|ที่อยู่รับของ|ที่อยู่รับสินค้า|ที่อยู่จัดส่ง|ที่อยู่|อยู่")
# A bare short-letters-then-digits token — the SAME generic shape every
# platform identifier (CustCode, OrderCode, ShipmentCode, ...) shares —
# is never a real street address by itself, no matter what label
# preceded it. See the plausibility check this guards, below.
_BARE_IDENTIFIER_RE = re.compile(r"^[A-Za-z]{1,4}\d+$")
# The SAME identifier shape as _BARE_IDENTIFIER_RE above, but matched as a
# standalone TOKEN anywhere in the raw message rather than requiring the
# whole captured value to be nothing else (Compact One-Shot Address fix,
# P1 audit finding). Found and removed from this parser's own local
# working copy BEFORE address/name extraction even runs -- exactly the
# same "find this token, remove it, then look for the next piece"
# convention _POSTAL_CODE_RE and _PHONE_RE already follow below -- so a
# customer supplying CustCode/ShipmentCode/Tracking in the SAME message
# as the address, with no label of their own, is never glued into the
# free-form Address value. Never touches the ORIGINAL `message` the
# generic per-parameter binding loop runs against afterwards, so these
# identifiers are still bound normally by their own validation_pattern --
# this only stops them being MISTAKEN for address text.
_STANDALONE_IDENTIFIER_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d{4,}(?![A-Za-z0-9])")
# A short run of Thai-only text (no digit, no ASCII letter of its own)
# immediately followed by an ASCII letter — the start of a code — is a
# left-over descriptor word from the trigger phrase's OWN label (e.g.
# "ของบิล" in "ที่อยู่ของบิล SP100820260716001", "บิลขนส่ง" in
# "ที่อยู่บิลขนส่ง SP1008..."), never part of the real address, no
# matter which specific descriptor word an admin's or customer's exact
# phrasing happens to use. Generalizes the compound-label list below
# (kept for the phrases already explicitly documented there) instead of
# needing every future descriptor word enumerated one at a time.
_LEADING_THAI_DESCRIPTOR_RE = re.compile(r"^[ก-๙\s]{1,20}(?=[A-Za-z])")
# A customer using a label+colon layout ("ตำบล: ตาขัน", "จังหวัด :ระยอง",
# "จังหวัด : ระยอง", full-width "：" included) leaves the colon and any
# surrounding spaces sitting right after a matched marker/label — never
# part of the real value. Stripped once, immediately after the marker,
# everywhere a marker's own value is sliced out of the raw text (geo
# markers below, and the "ที่อยู่" label). _RECEIVER_NAME_RE has its own
# equivalent `[:\-]?\s*` built directly into the pattern; this shared
# helper exists for the two call sites that slice value text out
# separately instead of matching it inline.
_COLON_AFTER_MARKER_RE = re.compile(r"^\s*[:：]?\s*")
# The postal code's own digits are found and removed via _POSTAL_CODE_RE
# (a trailing 5-digit run) BEFORE the geo-marker loop runs, but a
# colon-labeled message's "รหัสไปรษณีย์:" label text is left behind —
# confirmed live (2026-08-24, Production Safety Check): with nothing
# recognized to bound it, this label text got silently swallowed into
# Province's own greedy value (the LAST geo marker always captures to
# end-of-text), producing "ระยอง\nรหัสไปรษณีย์:" instead of "ระยอง". This
# label is never itself a captured field (postal_code is already handled
# above) -- only ever stripped so it can't pollute whatever geo value
# happens to precede it.
_POSTAL_LABEL_RE = re.compile(r"รหัสไปรษณีย์\s*[:：]?\s*$")
# Trailing Thai sentence-final politeness particles a customer commonly
# appends after providing information -- "...10540 ครับ", "...10540
# นะคะ" (Postal-Code Trailing-Politeness fix, P1 audit finding). Same
# vocabulary services/pending_confirmation_service.py's own
# _TRAILING_PARTICLES_RE already recognizes for confirmation replies,
# reused here (not a fresh one-off list) and stripped ONLY from the very
# end of the whole message -- never mid-text -- so it can never be
# mistaken for part of whatever value immediately precedes it (confirmed
# live: "...รหัสไปรษณีย์: 10540\nครับ" swallowed the postal code AND the
# particle both into Province instead of the two ever being told apart).
_TRAILING_PARTICLES_RE = re.compile(r"\s*(?:(?:นะคะ|นะครับ|ค่ะ|ครับ|น่ะ|จ้า|จ้ะ|นะ)\s*)+$")


def parse_thai_address(text: str) -> Dict[str, str]:
    """Splits one free-text message into
    {"receiver_name": ..., "receiver_phone": ..., "address": ...,
    "subdistrict": ..., "district": ..., "province": ..., "postal_code": ...}
    -- only the keys actually found in `text` are present; nothing is
    invented. Returns {} for text with no recognizable marker/signal at
    all."""
    text = (text or "").strip()
    if not text:
        return {}
    text = _TRAILING_PARTICLES_RE.sub("", text)
    text = _STANDALONE_IDENTIFIER_RE.sub(" ", text)
    result: Dict[str, str] = {}

    postal_match = _POSTAL_CODE_RE.search(text)
    if postal_match:
        result["postal_code"] = postal_match.group(1)
        text = text[:postal_match.start()].rstrip()
        label_match = _POSTAL_LABEL_RE.search(text)
        if label_match:
            text = text[:label_match.start()].rstrip()

    phone_match = _PHONE_RE.search(text)
    if phone_match:
        result["receiver_phone"] = phone_match.group(0)
        prefix = text[:phone_match.start()]
        label_match = _PHONE_LABEL_RE.search(prefix)
        cut_start = label_match.start() if label_match else phone_match.start()
        text = text[:cut_start] + " " + text[phone_match.end():]

    name_match = _RECEIVER_NAME_RE.search(text)
    if name_match:
        value = name_match.group(1).strip()
        if value:
            result["receiver_name"] = value
        text = text[:name_match.start()] + " " + text[name_match.end():]

    geo_matches = list(_GEO_MARKER_RE.finditer(text))
    # Confirmed live defect (2026-08-20, Production UAT): a message with
    # NEITHER a geo marker NOR an explicit "ที่อยู่" label (e.g. a bare
    # "SP1008" or "ต้องการเปลี่ยนที่อยู่บิลขนส่ง" replayed against this
    # action's OWN first-turn history slot, per _replay_business_action_
    # collection's "always process turn 0" rule -- see decision_engine.py)
    # must NEVER be claimed as the address merely because nothing else
    # matched. That used to run via an unconditional "leading text = the
    # whole message" fallback -- this pre-pass runs BEFORE the main
    # per-parameter binding loop and uses setdefault(), so it grabbed
    # "SP1008" as Address before CustCode's own, far more specific
    # validation_pattern ever got a chance to bind it correctly. Only an
    # EXPLICIT signal -- a "ที่อยู่"/"ที่อยู่จัดส่ง" label, or at least one
    # geo marker following it -- earns the "address" attribution now; a
    # genuinely bare, unmarked address line (no label, no geo marker at
    # all) is left for the existing, already-proven-safe generic
    # free-text fallback (_extract_candidates_for_binding) to bind, the
    # SAME mechanism SendLineNotiCS's own free-text Message parameter
    # already relies on.
    leading = text[:geo_matches[0].start()] if geo_matches else text
    label_matches = list(_ADDRESS_LABEL_RE.finditer(leading))
    if label_matches:
        raw_address = leading[label_matches[-1].end():]
        address_value = _COLON_AFTER_MARKER_RE.sub("", raw_address, count=1).strip()
    elif geo_matches:
        address_value = leading.strip()
    else:
        address_value = ""
    # A bare "ที่อยู่" label match with NOTHING after it that looks like a
    # real address (no geo marker followed, no digit at all) is very
    # likely the verb "เปลี่ยนที่อยู่"/"แก้ที่อยู่" itself, not a label —
    # confirmed live: "ต้องการเปลี่ยนที่อยู่บิลขนส่ง" (no geo marker at all)
    # matched "ที่อยู่" as a label and left "บิลขนส่ง" as a false address.
    # Every real Thai address in this feature's own requirement always
    # includes at least a house/building number; requiring one digit is a
    # safe, minimal plausibility check that rejects this false positive
    # without rejecting any address that actually has a house number.
    if address_value and not geo_matches and not any(ch.isdigit() for ch in address_value):
        address_value = ""
    # Address Change Full UAT fix (2026-08-24) — confirmed live: "ต้องการ
    # เปลี่ยนที่อยู่จัดส่ง SP1008" (the trigger phrase, with the customer's
    # own CustCode tacked on in the SAME message) has its own trigger
    # phrase literally CONTAIN the "ที่อยู่จัดส่ง" label, so the trailing
    # "SP1008" passed the digit check above and was claimed as the street
    # address, silently discarding the real identifier. A real Thai
    # address always has more shape than one bare alphanumeric code (a
    # house-number/slash, a Mu/Soi/Thanon word, multiple words); a
    # value that is ITSELF nothing but one short letters+digits token
    # (the same generic identifier shape CustCode/OrderCode/ShipmentCode
    # etc. all share) is never a real address on its own, regardless of
    # whether a geo marker happened to follow.
    if address_value:
        stripped = _LEADING_THAI_DESCRIPTOR_RE.sub("", address_value)
        if _BARE_IDENTIFIER_RE.fullmatch(stripped or address_value):
            address_value = ""
    if address_value:
        result["address"] = address_value

    for i, m in enumerate(geo_matches):
        component = m.lastgroup
        value_start = m.end()
        value_end = geo_matches[i + 1].start() if i + 1 < len(geo_matches) else len(text)
        raw_value = text[value_start:value_end]
        value = _COLON_AFTER_MARKER_RE.sub("", raw_value, count=1).strip()
        if not value and m.group(0) in _BANGKOK_PROVINCE_ALIASES:
            # Unlike "จ./จังหวัด", every Bangkok alias IS the province
            # value itself (never a prefix before a separate name) --
            # canonicalized to the one full official form regardless of
            # which alias the customer actually typed.
            value = "กรุงเทพมหานคร"
        if value:
            result[component] = value

    return result


# ── Field-level correction detection ────────────────────────────────────
# Concept name -> the SAME marker phrases used above, so "province" is
# recognized whether the customer writes "จังหวัด" or "จ." when correcting
# it -- plus the concepts that have no marker in the main parser above.
_CORRECTION_CONCEPT_TERMS = {
    "subdistrict": ("ตำบล", "แขวง"),
    "district": ("อำเภอ", "เขต"),
    "province": ("จังหวัด",) + _BANGKOK_PROVINCE_ALIASES,
    "postal_code": ("รหัสไปรษณีย์",),
    "address": ("ที่อยู่",),
    "receiver_name": ("ชื่อผู้รับ",),
    "receiver_phone": ("เบอร์โทรผู้รับ", "เบอร์โทร"),
}
# "ไม่ใช่" (Customer Journey UAT, 2026-08-27) — a mid-workflow correction
# phrased as "<field>ไม่ใช่<old value> เป็น<new value>" (e.g. "ชื่อผู้รับไม่ใช่
# สมชาย เป็นสมศักดิ์ครับ") was falling through to the generic compound-
# address parser instead of this correction path, since none of the
# original cue phrases cover "ไม่ใช่" -- the customer's OWN corrected value
# was then silently never applied to any collected slot. Every concept term
# this cue can pair with (see _CORRECTION_CONCEPT_TERMS above) already
# requires BOTH a named field AND a "เป็น"/"คือ" replacement value before
# detect_field_correction returns anything, so this addition does not
# widen false-positive risk beyond the existing cue phrases' own scope.
_CORRECTION_CUE_RE = re.compile(r"ผิด|เปลี่ยนเป็น|แก้เป็น|ที่จริงคือ|ที่ถูกคือ|ไม่ใช่")
_CORRECTION_VALUE_RE = re.compile(r"(?:เป็น|คือ)\s*(.+)$")


def detect_field_correction(message: str) -> Optional[Tuple[str, str]]:
    """Recognizes "<field name>ผิด เป็น<new value>" (and close variants) --
    returns (component_name, new_value) for the ONE field named, or None
    if the message doesn't clearly name both a known field and a
    correction cue. Never guesses a value: if no "เป็น"/"คือ" separator is
    found, returns None rather than fabricating what the new value might
    be."""
    message = (message or "").strip()
    message = _TRAILING_PARTICLES_RE.sub("", message)
    if not message or not _CORRECTION_CUE_RE.search(message):
        return None

    matched_component = None
    matched_at = -1
    for component, terms in _CORRECTION_CONCEPT_TERMS.items():
        for term in terms:
            idx = message.find(term)
            if idx != -1 and (matched_component is None or idx < matched_at):
                matched_component, matched_at = component, idx
    if matched_component is None:
        return None

    value_match = _CORRECTION_VALUE_RE.search(message)
    if not value_match:
        return None
    new_value = value_match.group(1).strip()
    if not new_value:
        return None
    return matched_component, new_value
