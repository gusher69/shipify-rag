# LangGraph + Langfuse — MVP architecture and evidence

Baseline: `10fb30b` (the Phase 6 post-deploy hardening commit). Production
is unchanged and still runs `fde43f1`.

## What was added

| Layer | Where | What it does |
|---|---|---|
| Agent state | `services/agent/state.py` | one typed `AgentState` per turn, plus the comparable `AgentDecision` |
| Graph | `services/agent/graph.py` | a bounded, acyclic LangGraph — no cycles, no self-chosen loops |
| Nodes | `services/agent/nodes/` | understand → remember → plan → tool → validate → ground → respond → safety → persist |
| Adapter | `services/agent/adapters/existing_engine.py` | the ONLY boundary to the platform; declares the tool contracts |
| Runner | `services/agent/runner.py` | traced run, shadow run, and the current-vs-graph comparison |
| Tracing | `services/observability/langfuse_client.py` | degrade-safe, privacy-masking Langfuse client |
| Channel | `line_bot/webhook.py` | runs the graph beside the live turn, reusing its result |

## The minimal graph, and how the 13 nodes map onto it

The requested MVP shape is on the left; the implemented nodes on the
right. Nothing extra was invented — the implemented nodes are the same
pipeline with the plan/validate/safety boundaries named separately,
because those three are what the response layer is forbidden to decide
for itself.

| Requested | Implemented |
|---|---|
| `load_context` | `normalize_input` |
| `resolve_current_turn` | `resolve_current_turn` |
| `merge_state` | `merge_conversation_state`, `resolve_precedence` |
| `plan_action` | `resolve_auth_requirement`, `plan_next_action`, `select_tool` |
| `existing_engine/tool_adapter` | `execute_tool`, `validate_tool_result` |
| `response` | `ground_response`, `plan_response`, `safety_check` |
| `trace` | the Langfuse span wrapping the whole run (`runner.run_agent`) |
| `persist` | `persist_state` |

## What it deliberately does NOT do

* No second decision engine. No routing table, policy verdict, auth rule
  or Thai pattern is re-implemented in a node. This is enforced
  mechanically: `tests/test_agent_graph.py` reads every node's source and
  fails if one imports a platform service directly or defines a Thai
  regex of its own.
* No new database, no LangGraph checkpointer backend. Durable state is
  handed back to the existing session store.
* No tool rewrite. The Decision Engine still executes; the graph plans.
* No cutover. `LANGGRAPH_MODE` defaults to `shadow`.

## Authority

| Mode | Who answers the customer |
|---|---|
| `off` | current engine; the graph never runs |
| `shadow` *(default)* | current engine; the graph runs beside it for comparison only |
| `owner_test` | the graph, for configured OWNER_TEST senders only; every REAL_LINE customer keeps the current engine |
| `production` | the graph, for everyone — a deliberate owner decision, never a default |

In `owner_test` and `production` the graph may only take a turn when its
own safety gate raised nothing; a flagged turn always falls back to the
current engine's answer.

## Shadow mode never doubles work

The webhook passes the engine result it already computed into the graph,
and `execute_tool` reuses it (`execute_tool(reused)` in the node path).
An observation-only run therefore performs no second ERP call, no second
retrieval and no second Human-CS notification.

## Langfuse

Traced per turn: resolved intent, conversation act, active journey,
entity NAMES, known-slot names, requested slot, planned action, selected
tool and tool class, response route, grounding status, safety flags, node
path, errors, latency, graph version, mode, and whether the graph was
authoritative.

Never traced: the customer's message text, LINE user id, customer code,
phone, email, address, tracking or order identifiers, or any private ERP
payload. Masking happens in-process before the SDK sees anything, keyed
both on value patterns and on field names, so a payload with no
recognisable pattern is still redacted.

Failure behaviour: unset, unreachable, slow, misconfigured, or raising
from inside the SDK — every path returns a no-op handle and the
conversation is unaffected. `flush()` is never called on the request
path.

## Intelligence changes that came out of the MVP work

Each was a root cause found while making the graph's state honest, and
each is fixed in the shared mechanism rather than per phrase:

1. **The frame died whenever no product was known yet.** A quantity-only
   opener produced an ack, and the next turn had no frame at all — so
   "รองเท้าครับ" attached to nothing and the bot answered
   "ตอนนี้ยังไม่มีข้อมูลยืนยันเรื่องนี้ค่ะ". Reproduced against the current
   production engine before the fix. The frame now survives on any known
   slot.
2. **The product ask and its own detector had drifted.** The renderer
   sends "รบกวนแจ้งชื่อหรือประเภทสินค้าที่สนใจนำเข้าด้วยนะคะ"; the detector
   allowed ten characters between "รบกวน" and "ประเภทสินค้า" and that
   sentence has eleven. The ask is now a named constant and the detector
   is anchored on structure, with a parity test.
3. **The frame read its own question back as a product.** The ask ends in
   the same "สนใจนำเข้า…นะคะ" shape as the acknowledgement, so the frame
   remembered `product="ด้วย"`. A captured span that is only particles is
   no longer a product name.
4. **`_BARE_METHOD_ANSWER_RE` was defined twice**, the later and narrower
   definition shadowing the earlier — it knew "ทางเรือ" but not "ส่งเรือ",
   so answering the bot's own transport question dead-ended. One
   definition now, built from the shared method vocabulary.
5. **A bare method answer had no SET path**, only a SWAP path, so a
   first-time answer resolved to nothing.
6. **Colloquial permission particles** ("ได้หรอ", "ได้ปะ") were missing
   from the one place a permission question is recognised.
7. **Known slots were not merged into the service-intent reply**, so a
   bare product answer re-asked a quantity given a turn earlier.

## Evidence

* `tests/test_agent_mvp_gate.py` — the four priorities, end to end, with
  real execution and real reply text carried between turns (16 tests).
* `tests/test_agent_graph.py` — structure, architecture invariants,
  authority gates, owner cases A–J (27 tests).
* `tests/test_agent_langfuse_safety.py` — degrade-safety and privacy
  masking (10 tests).
* `reports/agent_generalization_lab.json` — supplementary, see the note
  below.

### Note on the generalization lab

The lab was run once before the scope freeze: 2075 turns. Its
single-turn understanding numbers are sound — product 100%, quantity
100%, unit 100%, method 98.3%, product/question contamination 0,
known-slot re-ask 0, stale takeover 0, auth violation 0, private leak 0,
false completion 0.

Its **context-continuity figure (81.8%) is not a valid measurement** and
should be ignored. The lab's understanding tier stubs execution for
speed, which replaces each assistant turn with a fixed `[STUB]` string —
and on this platform the assistant's own wording *is* the conversation
state. Stubbing the reply therefore erases the state the next turn is
supposed to remember. Continuity is proven instead in
`tests/test_agent_mvp_gate.py`, unstubbed, where it passes.

Fixing the lab's stub would mean extending it, which the scope freeze
excludes; it is recorded here as a known limitation of that harness
rather than silently left to look like a product defect.
