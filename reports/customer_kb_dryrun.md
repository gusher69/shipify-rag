# KB Ingestion DRY RUN — Phase 6 closure gate C

**READ-ONLY. Nothing was written to the Knowledge Base.**

- Prepared entries: **28**
- Backed by an authoritative customer source: **28 / 28**
- Invented business truth: **0** (every row's content is copied from the cited source row)
- NEW: 3 · UPDATE: 17 · REVIEW (wording differs): 8 · BLOCKED (probe failed): 0
- Unresolved conflicts: **8**

| FEEDBACK_ID | Topic | Authoritative source | Existing KB match | Duplicate/Conflict | Action | Test IDs |
|---|---|---|---|---|---|---|
| AI-API-S1-1.0 | ขอที่อยู่โกดังหน่อย | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 5fdffb90-b922-4aef-847c-74e73fa941ec(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-10.0 | บิลขนส่งสามารถชำระได้เลยไหม | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | f82fa904-27fd-4ee9-a8b2-4b3661a2a003(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-13.0 | ระยะเวลาการส่งจากร้านจีน -โกดังจีน | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | bd2a7f98-4cbc-4afb-b514-9d16fc23eca5(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-14.0 | ชำระบัตรเครดิตได้ไหม | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | a2618c6d-643a-4337-b525-59efdb98db05(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-15.0 | ขอเบอร์ติดต่อ | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 21a4c966-7cb8-41ef-bebe-e85ee698b0fc(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-2.0 | ขอที่อยู่โกดังจีน,ต้องการส่งของผ่านชิปปิ้ง ส่งไปที่อ | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | c726a00b-e630-4032-8597-6a977a3eba73(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-20.0 | ขนส่งเอกชนมีอะไรบ้าง | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 02bb0ab6-061c-4bc9-84c9-90bc275d1eb9(active=True) | NEEDS_REVIEW | REVIEW (topic present, wording differs) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-22.0 | เรทเท่าไหร่คะ ,เรทนำเข้าเท่าไหร่ ,เรทฝากสั่งเท่าไหร่ | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 304e5a3c-9234-451c-9235-49a47d97ce67(active=True) | NEEDS_REVIEW | REVIEW (topic present, wording differs) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-23.0 | คูปองใช้ไม่หมด คืนได้ไหมคะ | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 3764f0c6-aa2a-4694-babf-8877229068fa(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-24.0 | มีบริการอะไรบ้าง | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 6a2b7964-b605-4ff3-b8c4-c37a14b0417b(active=True); | NEEDS_REVIEW | REVIEW (topic present, wording differs) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-25.0 | ฝากสั่งกับฝากนำเข้าต่างกันอย่างไง | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 6759fd84-8bd2-4b39-81bc-0563d29f2b5e(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-26.0 | ชำระบิลขนส่งยังไง,ชำระค่านำเข้ายังไง | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 06636112-16bc-4a8a-b051-15f238df8fd4(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-27.0 | สนใจฝากสั่งสินค้าค่าใช้จ่ายคิดยังไง | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 39dec856-4eae-44d7-9b63-ce20496d9f17(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-28.0 | มีบริการตีลังไม้ไหม,ตีลังได้ไหม | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | d164742f-dcc1-4e28-8b0f-ec9442425eb0(active=True); | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-4.0 | ค่าขนส่งคิดยังไง คำนวนค่าส่งให้หน่อย | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 60dbeffe-2f1d-499d-ae1e-7da1100528ed(active=True) | NEEDS_REVIEW | REVIEW (topic present, wording differs) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-5.0 | ออกใบกำกับได้ไหม | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | ff288877-868c-431b-a694-c7a4afa9034b(active=True) | NEEDS_REVIEW | REVIEW (topic present, wording differs) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-6.0 | CBM คิวคืออะไร | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | aef75cdc-6909-4c0d-a8a3-8a01cc3d5507(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-7.0 | มีขั้นต่ำในการสั่งไหม | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | fbf503ab-a2c8-42cc-9900-26eb52d96632(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-8.0 | สินค้าที่ห้ามนำเข้ามีอะไรบ้าง | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 552cd392-b8af-46be-87ad-507ef65f0ec6(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| AI-API-S1-9.0 | วิธีการชำระบิลสั่งซื้อ,ชำระค่าสินค้ายังไง,จ่ายบิลสั่ | AI_API_Requirement_For_Client.xlsx, sheet "1.ถ | 506370f4-5f12-4631-8a6d-26095241e77b(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| TRAIN-01 | ครีมอาบน้ำนำเข้าได้ไหม | ปัญหาที่เจอในการตอบ (1).xlsx row 2 | ae466369-e043-4ba6-844c-9a8192556dd5(active=True) | NEEDS_REVIEW | REVIEW (topic present, wording differs) | tests/test_phase6_kb_dryrun.py |
| TRAIN-02 | ทางรถกับทางเรือระยะเวลากี่วัน | ปัญหาที่เจอในการตอบ (1).xlsx row 3 + Ai.xlsx s | — | NO | NEW | tests/test_phase6_kb_dryrun.py |
| TRAIN-03 | แล้วเรทนำเข้าเท่าไหร่คะ | ปัญหาที่เจอในการตอบ (1).xlsx row 4 | — | NO | NEW | tests/test_phase6_kb_dryrun.py |
| TRAIN-04 | ฝากสั่งน้ำหอมได้ไหมคะ | ปัญหาที่เจอในการตอบ (1).xlsx row 5 | — | NO | NEW | tests/test_phase6_kb_dryrun.py |
| TRAIN-06 | โกดังอ่อนนุชเปิดทุกวันหรอคะ | ปัญหาที่เจอในการตอบ (1).xlsx row 7 | 08c654b4-b4af-414e-8e83-d2e4cb7f5b9b(active=True) | NEEDS_REVIEW | REVIEW (topic present, wording differs) | tests/test_phase6_kb_dryrun.py |
| TRAIN-07 | มีขนส่งทางเครื่องบินไหม | ปัญหาที่เจอในการตอบ (1).xlsx row 8 | 4d7c910b-df04-43cd-a289-12efc5198dbc(active=True) | NEEDS_REVIEW | REVIEW (topic present, wording differs) | tests/test_phase6_kb_dryrun.py |
| TRAIN-10 | โหลดใบกำกับยังไง | ปัญหาที่เจอในการตอบ (1).xlsx row 11 + AI_API…x | 389645f9-88b3-4fec-9d69-357268f9d2a2(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |
| TRAIN-11 | ตีลังไม้ได้ไหม | ปัญหาที่เจอในการตอบ (1).xlsx row 12 + Ai.xlsx  | c24215bc-d4de-4b68-b4b5-d8eb14e3cce6(active=True) | NO | UPDATE (existing chunk covers this topic) | tests/test_phase6_kb_dryrun.py |

## Idempotent ingestion plan

1. Key every entry by its `FEEDBACK_ID` in chunk metadata (`metadata.feedback_id`), so re-running the ingest updates the same row instead of appending a duplicate.
2. For `NEW`: insert one chunk carrying the verbatim source text.
3. For `UPDATE`: update the matched chunk's content in place, keeping its id.
4. For `REVIEW`: do **not** ingest automatically — a human compares the existing wording with the source row and picks one.
5. Re-run this dry run after any ingest; a clean second run (all `UPDATE`, no `NEW`) proves idempotency.

## Rollback / export procedure

1. **Before** ingesting, export the affected rows: `select id, content, is_active, metadata from knowledge_chunks where metadata->>'feedback_id' in (...)` — save as `reports/kb_pre_ingest_backup.json`.
2. Rollback = restore those rows from the backup by id; entries that were `NEW` are removed by id.
3. Because every write is keyed by `feedback_id`, rollback never touches a chunk this plan did not create or update.
