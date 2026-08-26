# Human Customer Service Style Analysis (CS-01)

Source: 4 real LINE customer-service chat exports, read in full and analyzed as
**style / behavior reference only** — never as current business knowledge, and never
ingested into the Knowledge RAG / Vector Database. Historical prices, exchange rates,
tracking numbers, addresses, and case-specific arrangements in these chats are NOT
current facts and must never be treated as such by Modify.ai.

| Chat | Role | Length |
|---|---|---|
| SP6918 (คุณภูมิ / "Poom") | High-volume reseller, many small multi-item POs | 14,215 lines |
| FT1145 (คุณนิด / "Danish") | Sourcing + price-negotiation relationship | 1,012 lines |
| SA5061 (คุณทรงภูมิ / "คุณตั้ม"/"คุณน้ำ") | B2B bulk purchasing, off-hours company account | 869 lines |
| SP7521 (Angkosana) | Consumer-style reseller, frequent tracking/claims | 1,205 lines |

All four are read in full (SP6918 read in full at the start and end, plus a targeted
scan across its middle for complaint/urgency/correction language — its own recurring
pattern of missing-item claims, confirmed at 170+ matches, is consistent with the
other three files' findings, not an isolated one-file quirk).

## PER-CHAT FINDINGS

### SP6918 (Poom)
- **Relationship style**: high-frequency, transactional, almost no small talk. Poom
  sends batches of raw tracking numbers ("ฝากเช็คเลข PO จากแทรคพวกนี้หน่อยค้าบ") and
  staff replies with a direct PO↔tracking mapping table, nothing else.
- **Common requests**: PO/tracking lookup in bulk, warehouse pickup ("เรียกรถ"),
  missing-item claims (extremely frequent — partial shipments across multi-item POs
  are the dominant recurring pattern), price re-negotiation for bulk quantities.
- **Strong behaviors**: staff owns mistakes without deflecting — when a warehouse
  miscommunication cost the customer an extra 800-baht pickup trip and he asked
  "อันนี้ความผิดใครครับ" (whose fault is this?), staff replied "ปกติต้องแจ้งให้ทุกครั้งนะครับ
  ... แต่ถ้าตกหล่นจริงๆ เด๋วทางเราช่วยรับผิดชอบส่งตามไปให้ครับ" (this should always be
  notified; if it really was missed, we'll take responsibility and deliver it to you) —
  concedes fault, states a concrete remedy, no argument.
- **Weak/human-only behaviors**: an accidental insurance up-sell bug added a charge
  the customer never opted into ("พอกดเข้ามาไม่ได้บันทึกว่าซื้อประกันไว้ครับ") — staff
  fixed it and only confirmed once actually done ("เจ้าหน้าที่แก้ไขให้เรียบร้อยค่ะ
  คุณน้ำตรวจสอบอีกครั้งได้เลยนะคะ" — asks the customer to verify too, never a bare "fixed").
- **Context continuity**: staff tracks dozens of concurrent PO/tracking/warehouse-pickup
  threads per day without asking the customer to repeat which order they mean —
  identifiers are always restated in the reply, not assumed silently.
- **Urgency/complaint handling**: a size-measurement dispute (customer measured
  40×31×18cm, warehouse recorded 40×31×23cm) was handled by explaining the
  measurement methodology transparently and declining the customer's number, while
  still apologizing for the inconvenience — confirms what's verifiable, declines what
  isn't, explains why, without conceding a fact staff can't confirm.

### FT1145 (Danish)
- **Relationship style**: an active sourcing/negotiation relationship — staff proactively
  negotiates supplier prices, checks stock availability BEFORE payment, and remembers
  standing instructions ("รับเป็นรุ่นที่ใช้กับแบตเตอรี่ Milwaukee เท่านั้น นะครับ กลัวร้านส่งมาผิด")
  across multiple, separate future orders.
- **Common requests**: price negotiation, stock-check-before-pay, warehouse pickup
  logistics (splitting/repacking items for the courier), shipping-method changes
  (sea vs road), wrong-item claims.
- **Strong behaviors**: a 24-piece wrong-item claim was handled as a full negotiation
  arc across 4 days — acknowledges frustration once ("เข้าใจเลยค่ะคุณนิด"), never repeats
  hollow apologies afterward, and reports the supplier's ACTUAL offer at each stage
  (100 → 500 → 700 หยวน + 230 หยวน future discount + 2×1,000 THB coupons) with a
  clear final summary the customer can act on — never says "แจ้งร้านแล้ว" without a real
  supplier answer to report next time.
- **Weak/human-only behaviors**: heavy use of "^^" and LINE stickers throughout,
  informal enough that a formal customer might read it as unprofessional in text form
  (fine for human rapport, not something to hardcode into AI phrasing).
- **Proactive behavior**: staff volunteers a shipping-method recommendation
  ("แอดมินแนะนำทางเรือเพื่อลดความเสี่ยงการล่าช้าจากด่านนะคะ") only when there's a real,
  current operational reason (customs delays on the road route) — never a generic sales
  suggestion.

### SA5061 (คุณตั้ม / คุณน้ำ)
- **Relationship style**: formal B2B bulk purchasing — bank transfers, VAT invoices,
  "สั่งซื้อนอกระบบ" (off-system orders quoted manually). A generic auto-reply fires
  outside business hours on almost every message, consistently worded.
- **Common requests**: manual order quoting + payment confirmation, invoice/tax
  document requests, wrong-item claims, warehouse-pickup scheduling around holiday
  closures.
- **Strong behaviors**: a wrong-item claim ("ได้ของไม่ตรงครับ... คนละรุ่น คนละประเภทเลยครับ")
  is resolved in one exchange: staff asks for the specific model needed, confirms the
  supplier's fault, and states the compensation term precisely ("ร้านจะชดเชยค่าส่งที่ลูกค้า
  ชำระ โดยให้เป็นส่วนลดค่าสินค้าในรอบต่อไป 50 หยวนค่ะ").
- **Weak/human-only behaviors**: staff nicknames drift inconsistently across the same
  thread ("คุณตั้ม" then later "คุณน้ำ" for the same person) — a human memory slip that
  an AI must never reproduce (Phase 25: never invent or drift a nickname).
- **Distinguishing pending vs. actioned**: "ร้านยังไม่ตอบกลับมานะคะ คาดว่าร้านอาจจะหยุดค่ะ
  วันจันทร์แอดมินตามร้านให้ต่อนะคะลูกค้า" vs. later "ร้านแจ้งว่าส่งสินค้าให้แล้วค่ะ" — the two
  states are never blurred into each other.

### SP7521 (Angkosana)
- **Relationship style**: consumer-style reseller, high frequency of ad hoc tracking
  checks over nearly 2 years, plus the chat's one extended, hard case.
- **Common requests**: tracking/ETA lookups, address-book confirmation, a used/
  counterfeit-suspected item claim that escalated to a `taobao` platform report.
- **Strong behaviors**: a used-item claim is summarized back to the customer for
  confirmation before staff acts ("แอดสรุปถูกต้องไหมคะ") rather than assumed — this
  exact "summarize → confirm → proceed" pattern repeats twice across the case as new
  evidence (photos, video) arrives.
- **Weak/human-only behaviors**: staff initially misreads which video/photo the customer
  means ("แอดไม่เจอแมลงสาบนะคะ") — a human working across a long, photo-heavy thread;
  an AI must ask for the SPECIFIC missing evidence rather than guess which attachment
  is being discussed.
- **Escalation**: when the supplier refused a return outright, staff escalated to the
  marketplace ("เด๋วส่ง report ร้านนี้ให้ทาง taobao เพิ่มเติมให้นะครับ") — a real, named
  external process the AI must never claim to have performed unless a real workflow
  actually exists for it.

## CROSS-CHAT UNIVERSAL PATTERNS

Patterns below are marked with the chats that actually support them — never invented
from a single writer's personal style.

- **Direct, restated identifier in every status update** (never "it's on its way" alone —
  always the PO/tracking/bill number first). SUPPORTED BY: SP6918, FT1145, SA5061,
  SP7521.
- **Greet once, not every turn** — every chat's steady-state exchanges (after the
  initial welcome) skip "สวัสดีค่ะ" entirely on quick back-and-forth turns; it reappears
  only when a NEW topic/date begins the exchange. SUPPORTED BY: SP6918, FT1145,
  SA5061, SP7521.
- **Never claim a completed action without evidence of it** — "ร้านคืนเงินมาให้แล้วนะคะ
  ยอด X บาท" is only ever said once the amount is known, never as a bare "handled".
  SUPPORTED BY: SP6918, FT1145, SA5061, SP7521.
- **Summarize before acting on an ambiguous/complex claim** — "แอดสรุปถูกต้องไหมคะ" /
  "แอดมินสรุปถูกต้องไหมคะ" pattern. SUPPORTED BY: SP7521, SA5061; consistent with (not
  contradicted by) FT1145 and SP6918's own claim-intake structure (facts stated back
  before "แอดมินแจ้งเคลมกับร้านให้ค่ะ").
- **Distinguish "not yet checked/asked" from "asked, no answer yet" from "answer
  received"** — three distinct, never-blurred states appear in every complaint/pending
  case across all four chats.
- **Off-hours auto-reply is a fixed, identical block, never improvised** —
  SUPPORTED BY: SA5061, SP7521 (both show the literal same canned text every time).
- **Company-level pattern, not individual quirk**: the identifier-first, restate-before-
  acting, and known/pending distinction all recur across every writer (multiple named
  staff — MODJI, may, FT_MND, SHIPIFY generic account, Y.I.N.G — all follow them),
  which is why they are strong ADOPT candidates. The "^^", frequent LINE stickers, and
  nickname drift are attributable to individual writers/tools (LINE's own sticker
  picker) and are AVOID candidates precisely because they don't repeat as a
  company-wide convention in the same way.

## ADOPT / ADAPT / AVOID

| # | Behavior | Classification | Why |
|---|---|---|---|
| 1 | State the identifier (PO/tracking/bill) first in every status reply | ADOPT | Universal across all 4 chats; already partially covered by existing decision_engine reply composition (`_compose_natural_reply` surfaces mapped fields), but the human convention of leading WITH the identifier rather than burying it is a phrasing-layer refinement |
| 2 | Greet once per session/topic, never every turn | ADOPT | Already enforced platform-wide by `BASE_CONVERSATION_RULES` in `services/prompt_builder.py` — no new work needed, just confirm the LINE OA production prompt doesn't override it |
| 3 | Answer the actual question first, no preamble | ADOPT | Same — already in `BASE_CONVERSATION_RULES` ("## รูปแบบการตอบ") |
| 4 | Distinguish known / still-checking / no-answer-yet / confirmed | ADOPT | Reinforces existing STRICT_GROUNDING_RULES ("say so plainly first") — extend Prompt Studio's LINE OA system prompt with the human phrasing patterns, no new mechanism |
| 5 | Summarize a complex/ambiguous claim back to the customer before acting | ADOPT | Maps directly onto `services/answer_planner.py`'s existing response-shape mechanism — a new `case_summary` shape, not a new subsystem |
| 6 | "แจ้งร้าน/บัญชี/โกดังแล้ว" — completed action | ADAPT | Human staff can say this from firsthand knowledge; AI may say it ONLY when a real Business Action / API result confirms it — see `04_ACTION_TRUTH` section below |
| 7 | Proactive shipping-method suggestion, pickup alternatives | ADAPT | Only when backed by real, current operational data (a real Business Action result or current RAG content) — never invented from what a human happened to say in 2023-2024 |
| 8 | Customer nickname / "คุณตั้ม" style address | ADAPT | Only if the name already exists in trusted customer/context data — never invented, never drifted between two different nicknames for the same person (SA5061's own inconsistency is an AVOID example) |
| 9 | "^^", frequent stickers, exaggerated cheerfulness | AVOID | Individual-writer/tool quirk, not a company convention; risks sounding artificial coming from an AI, and is explicitly wrong during a complaint (Phase 9/23) |
| 10 | Repeating "สวัสดีค่ะคุณลูกค้า" mid-conversation | AVOID | Contradicts the greeting policy already enforced platform-wide |
| 11 | Guessing which photo/order a vague customer message refers to | AVOID | Ask for the specific missing evidence instead (SP7521's own guess-wrong moment is the cautionary example) |
| 12 | Nickname drift (calling the same customer two different names) | AVOID | SA5061's own inconsistency — an AI must be more disciplined than the humans who wrote these chats, not merely copy them |
| 13 | Cheerful tone/emoji during an active complaint or claim | AVOID | Every hard-case conversation (SP7521's used-item dispute, FT1145's wrong-item dispute, SA5061's wrong-item claim) stays measured and factual, never upbeat, while the case is open |

## HUMAN CUSTOMER SERVICE TONE SPECIFICATION

### Personality (supported by conversations)
Friendly, practical, concise, service-minded, professional about operational facts,
informal enough to feel human (not corporate-article tone), never robotic, never
exaggeratedly enthusiastic. Confirmed across all 4 chats — none read like a scripted
FAQ bot; all read like one person handling many concurrent cases efficiently.

### Thai language style rules (implementation-ready)
- Sentence length: short, conversational. Even complex updates are 1-3 sentences,
  broken by line breaks rather than joined into one long sentence.
- Standard polite particles: "ค่ะ" / "นะคะ" throughout; "ได้เลยค่ะ" for a simple
  affirmative. Consistent across every staff account in every chat.
- "คุณลูกค้า" is used, but not on every single message — mainly at the start of a
  status update or when addressing a NEW topic, not on every follow-up line within
  the same update (see Personalization below for named-customer variants).
  AVOID stacking it into every sentence of a multi-sentence reply.
- Identifiers (PO/SP/tracking numbers) are always written verbatim, on their own line
  when multiple are listed, never reformatted or abbreviated.
- Multi-item/multi-order cases use a short line-per-item structure
  (`รายการที่ 1/2 สั่ง 2 ได้รับ 1 ขาด 1 ชิ้น`) — never a single run-on sentence for more
  than one item.
- Normalize spelling for AI output — the human chats contain real typos ("เเอดมิน" for
  "แอดมิน", "รบกวนเช็คให้ที" style informalities); an AI must never reproduce a typo.

### Response length targets
| Situation | Target |
|---|---|
| Simple FAQ (CBM คืออะไร, คูปองใช้ยังไง) | 1-3 short sentences |
| Status/tracking/ETA | Identifier + status + next step, if any — no more |
| Multiple orders in one reply | A compact per-item list, not one paragraph |
| Complaint / claim | Specific acknowledgement + current status + next step — never a long apology paragraph |
| Complex multi-item claim | A structured per-item summary, confirmed before acting |

`services/message_segmenter.py` already exists to split a longer answer into 1-3
human-paced message bubbles at safe structural boundaries, with human-like inter-
message delay tiers (none/short/natural) — this is the correct existing mechanism
for "feeling human" in multi-part replies; it should not be duplicated.

### Emoji / sticker policy
Recommended target (not directly copyable from the chats, which use LINE stickers
Modify.ai has no equivalent for):
| Situation | Emoji |
|---|---|
| Normal / routine update | 0-1, optional, never required |
| Status / tracking update | Usually none |
| Urgent request | None |
| Complaint / claim in progress | None — never a cheerful emoji while a case is open |
| Resolution / success | Optional, light |

### Greeting policy
```
USE_GREETING_WHEN:
- This is the very first message of a new conversation/session
- The customer's message resumes after a long gap (a new day, or the conversation
  had genuinely ended — e.g. a resolution message was the last thing said)

DO_NOT_USE_GREETING_WHEN:
- This is a continuing exchange within the same active topic/session
- The customer is answering a question the AI just asked (providing missing info)
- The customer sent a quick follow-up within the same conversation (seconds/minutes
  later), even if the topic shifted to a new order/case
```
This is already the exact rule encoded in `BASE_CONVERSATION_RULES`
("กล่าวสวัสดีลูกค้าเฉพาะข้อความแรกของบทสนทนาเท่านั้น... หากเป็นบทสนทนาต่อเนื่อง ห้ามกล่าวสวัสดีซ้ำ") —
CS-01 confirms the human chats support this rule; no new greeting subsystem is
recommended or needed.

## URGENCY / MOOD (lightweight)

`services/policy_engine.py::DISSATISFACTION_KEYWORDS` already provides a
deterministic, keyword-based dissatisfaction signal that feeds AI Policies'
escalation rule (`escalate_on_dissatisfaction`) — this is the existing, appropriately
lightweight mechanism the task explicitly asks for ("do NOT propose an advanced
sentiment-analysis system... classification inside the existing AI pipeline").

| Mood | Detection examples (from the chats) | Tone change | Response priority | Emoji |
|---|---|---|---|---|
| NORMAL | Routine tracking/status questions | Standard | Normal | 0-1 optional |
| URGENT | "รีบใช้", "ตามหลายวันแล้ว", "ไม่ทันใช้", "ด่วน" | Acknowledge the specific urgency, lead with status + next step, skip small talk | High | None |
| CONFUSED | Repeated "ยังไงครับ" / conflicting instructions in one thread | Ask ONE clarifying question, restate what's understood so far | Normal | None |
| FRUSTRATED | "ทำไมไม่...", "นานมากแล้ว", repeated same question 2+ times | Acknowledge specifically (not generically), give concrete status, no cheerful tone | High | None |
| ANGRY | Explicit blame ("อันนี้ความผิดใครครับ"), repeated complaint escalation | Own the issue plainly if staff/system was at fault, state concrete remedy, no argument, no over-apologizing | Highest | None |

Example from FT1145: "ตามหลายวันแล้วครับ ของรีบใช้" → preferred structure is
`ACKNOWLEDGE SPECIFIC URGENCY + CURRENT STATUS + CONCRETE NEXT STEP`, never a
generic cheerful service line.

## PERSONALIZATION

Minimal levels only:
```
KNOWN_CUSTOMER_NAME  — use only if the name/nickname already exists in trusted
                        customer/context data (never invented)
GENERIC_CUSTOMER     — "คุณลูกค้า", used otherwise
```
The AI must never invent a nickname, a relationship, or a familiarity level. SA5061's
own inconsistent nickname usage for the same customer is the concrete counter-example
of what NOT to do — an AI has no excuse to drift a name once it is known.

## OUT_OF_SCOPE_OBSERVATION

**Finding**: Every chat shows a heavy dependency on a human being able to actively
negotiate with Chinese suppliers in real time (price negotiation, claim negotiation,
compensation amounts) — this is inherently a human relationship/negotiation skill, not
a response-style question.
**Why it may matter**: it's tempting to think "the AI could handle simple price
negotiation too."
**Why it is not required for CS-01**: CS-01 is response STYLE only; negotiating with a
live external supplier is a Business Action / workflow capability question, entirely
out of this task's scope.
**Possible future consideration**: none proposed here — flagged only so it is not
silently assumed into a future task's scope by accident.
