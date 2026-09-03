# Customer UAT Master dataset

`customer_uat_master.jsonl` — one JSON object per line, each a logical customer-UAT
case distilled from `docs/customer_uat_sources/` (the authoritative customer
feedback set). Built by `scratchpad/build_uat.py` from the parsed sources.

Fields per case:
- `case_id`            stable id (CUS-G## sheet1, CUS-S## sheet2/CSW, CUS-F## real 2/9/2025 failures, CUS-SC# screenshots, CUS-P## PDF-only, CUS-RL-* real-LINE replay fixtures)
- `source`             {file, location}
- `user_message` / `user_message_variants`   the customer wording + paraphrase variants (test data, NOT production rules)
- `meaning`            one-line human intent
- `expected_route`     RAG | ERP | CLARIFY | HUMAN_CS | WORKFLOW | NA
- `expected_public_private`   PUBLIC | PRIVATE | NA
- `expected_behavior`  what should happen (semantic, not a fixed string)
- `customer_provided_expected_answer`   the CS-approved reply when the source gives one, else null
- `ai_actually_answered` / `recorded_verdict` / `customer_note`   present for the real-failure rows
- `expected_answer_constraints`   [] (fill with semantic constraints, not exact strings)
- `status`             SOURCE_CONFIRMED | NEEDS_INTERPRETATION
- `requires_real_line` true — final verification is a REAL LINE OA turn
- `replay_fixture` / `linked_cases`   cross-links

No secrets: values matching a long-token pattern are replaced with `[MASKED-SECRET]`
on ingest. This dataset is DEV/EVALUATION only — never loaded into the LINE runtime
or the RAG knowledge base.
