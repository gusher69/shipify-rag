# Shipify AI CS Agent — Full Project Context
> อัพเดตครั้งล่าสุด: รวม Web UI + Google Drive sync เข้า scope
> ห้ามเผยแพร่ — เอกสารลับ

---

## 01 ภาพรวมโปรเจกต์

Shipify คือแพลตฟอร์มนำเข้าและรับสั่งซื้อสินค้าจากจีน ดำเนินการมากว่า 6-7 ปี
เป้าหมายคือนำ AI มาตอบลูกค้าแทนทีม CS แบบ 24/7 รองรับขยายตลาดต่างประเทศโดยไม่ต้องเพิ่มบุคลากร

### ผู้เกี่ยวข้อง
- Shipify — client จริง (ไม่ได้คุยตรง)
- Mod (Sasiwan Jandaeng) — PM/middleman จาก modty.ai | modtyno@gmail.com
- Dev (เรา) — รับงานจาก Mod ทำ technical ทั้งหมด

```
Shipify (client) → Mod (PM) → Dev (เรา)
```

---

## 02 System Architecture

### Flow หลัก
```
ลูกค้าพิมพ์ใน LINE OA
        ↓
LINE Webhook (FastAPI)
        ↓
AI Middleware — Intent classifier + Language detect + User Profile loader
      ↙              ↓              ↘
RAG Engine     User Profile      ERP Bridge
(Qdrant)       (Cold/Warm/Hot)   (PHP/MySQL read-only)
      ↘              ↓              ↙
        Tone Engine — สร้าง draft + Confidence Score
              ↙                    ↘
       score สูง              score ต่ำ / ต่อรอง / ข้อพิพาท
           ↓                           ↓
     Auto reply                  LINE Notify แจ้ง CS
     (LINE OA)                   (Smart Handoff)
          ↓
    Update User Profile
```

### การแบ่งประเภทข้อมูล (สำคัญมาก)

| ข้อมูล | เก็บที่ไหน | อัพเดทเมื่อไหร่ | ใครแก้ |
|---|---|---|---|
| ราคาสินค้า / ค่าขนส่ง | ERP Bridge (real-time) | ทุกครั้งที่ถาม | Shipify IT |
| สต็อก / สถานะออเดอร์ | ERP Bridge (real-time) | ทุกครั้งที่ถาม | Shipify IT |
| FAQ / นโยบาย / คู่มือ | Qdrant (จาก .md หรือ PDF) | เมื่อ Mod/CS แก้ผ่าน Web UI | Mod / CS team |
| PDF catalog สินค้า | Qdrant (จาก PDF) | เมื่อ upload ไฟล์ใหม่ | Mod / CS team |
| User profile | PostgreSQL | หลังทุก conversation | อัตโนมัติ |

**กฎสำคัญ: ราคาและสต็อก ไม่เก็บใน Qdrant เด็ดขาด**
เพราะเปลี่ยนบ่อย ถ้าเก็บใน Qdrant ข้อมูลจะเก่าและตอบผิด

---

## 03 Knowledge Base Management (Module เพิ่มใหม่)

### ปัญหาที่ต้องแก้
Shipify ต้องการแก้ไข FAQ นโยบาย และ upload PDF สินค้าใหม่ได้เอง
โดยไม่ต้องรู้ code และข้อมูลต้องอัพเดทใน Qdrant อัตโนมัติหลังแก้

### สิ่งที่ต้องสร้างเพิ่ม

#### A) Admin Web UI (Knowledge Manager)
Web interface สำหรับให้ Shipify/Mod จัดการ knowledge base เองได้

หน้าที่มี:
- หน้า Documents — ดูรายการ .md files ทั้งหมด แก้ไข เพิ่ม ลบ
- หน้า Upload — drag & drop PDF/Word/MD files
- หน้า Re-embed — กดปุ่ม sync ให้ Qdrant อัพเดท
- หน้า Preview — ทดสอบถามคำถาม แล้วดูว่า AI จะตอบอะไร

Stack: FastAPI + Jinja2 templates (หรือ simple HTML) + login ด้วย password

#### B) Google Drive Sync (สำหรับ PDF เยอะๆ)
Shipify upload PDF ใหม่เข้า Google Drive folder ที่กำหนด
Python cron job รันทุกคืน ดึงไฟล์ใหม่มา embed เข้า Qdrant อัตโนมัติ

Flow:
```
Shipify upload PDF เข้า Google Drive folder
        ↓
Python cron job (รันทุกคืน 02:00)
        ↓
ตรวจหาไฟล์ใหม่/แก้ไข (เทียบ modified date)
        ↓
อ่าน PDF/Word/MD → chunk → embed → upsert Qdrant
        ↓
ส่ง LINE Notify แจ้ง Mod ว่า sync เสร็จแล้ว
```

#### C) รองรับไฟล์หลายประเภท
Python pipeline อ่านได้ทุกประเภท ข้อมูลครบ 100% ไม่มีขาด:

| ประเภทไฟล์ | Library | ใช้กับ |
|---|---|---|
| .pdf | pdfplumber / pymupdf | catalog สินค้า, เอกสาร |
| .docx | python-docx | คู่มือ, นโยบาย |
| .md | built-in open() | FAQ ที่เขียนใน Obsidian/Web UI |
| .json/.csv | pandas | LINE chat export |

---

## 04 Tech Stack

| Layer | Technology | หมายเหตุ |
|---|---|---|
| Channel | LINE Messaging API v3 | ภาษาไทยเป็นหลัก |
| AI Model | GPT-4o หรือ Claude 3.5 Sonnet | Mod จะ provide API key |
| Embedding | OpenAI text-embedding-3-small | 1536 dims |
| Vector DB | Qdrant (Docker self-hosted) | localhost:6333 |
| Knowledge Base | .md + PDF + Word | จัดการผ่าน Web UI หรือ Google Drive |
| Backend | Python 3.13 + FastAPI | |
| User Profile DB | PostgreSQL | Cold/Warm/Hot segment |
| ERP Bridge | Python wrapper → PHP/MySQL | read-only เท่านั้น |
| Knowledge Manager | FastAPI + HTML UI | Web UI แก้ไข knowledge เอง |
| File sync | Google Drive API + cron job | sync PDF อัตโนมัติทุกคืน |
| Hosting | Railway.app หรือ DigitalOcean | ตกลงกับ Mod ก่อน deploy |

---

## 05 Folder Structure

```
shipify-rag/
├── CONTEXT.md
├── config.py
├── requirements.txt
├── .env
├── .env.example
├── README.md
│
├── ingestion/
│   ├── __init__.py
│   ├── ingest.py          ← อ่านทุกไฟล์ใน knowledge/
│   ├── embedder.py        ← embed + upsert Qdrant
│   ├── pdf_reader.py      ← อ่าน PDF ด้วย pdfplumber
│   ├── word_reader.py     ← อ่าน .docx ด้วย python-docx
│   └── gdrive_sync.py     ← Google Drive sync (ใหม่)
│
├── rag/
│   ├── __init__.py
│   └── searcher.py        ← search Qdrant คืน top 3 chunks
│
├── erp/
│   ├── __init__.py
│   └── bridge.py          ← MySQL read-only wrapper
│
├── profiles/
│   ├── __init__.py
│   └── manager.py         ← CRUD user profile + segment logic
│
├── line_bot/
│   ├── __init__.py
│   ├── webhook.py         ← FastAPI LINE handler
│   ├── intent.py          ← classifier 4 หมวด
│   └── tone.py            ← prompt system + few-shot
│
├── admin/                 ← Web UI (ใหม่)
│   ├── __init__.py
│   ├── routes.py          ← FastAPI routes สำหรับ admin
│   ├── auth.py            ← login/logout
│   └── templates/
│       ├── documents.html ← จัดการ .md files
│       ├── upload.html    ← upload PDF/Word
│       └── preview.html   ← ทดสอบถามคำถาม
│
├── knowledge/             ← ไฟล์ทั้งหมดเก็บที่นี่
│   ├── faq/
│   ├── policies/
│   ├── products/          ← PDF catalog สินค้า
│   └── guides/
│
└── tests/
    ├── __init__.py
    └── uat_questions.csv  ← 100 คำถาม UAT
```

---

## 06 Config (.env)

```env
# OpenAI
OPENAI_API_KEY=sk-xxx

# Qdrant
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=shipify_knowledge

# LINE OA
LINE_CHANNEL_SECRET=xxx
LINE_CHANNEL_TOKEN=xxx
LINE_NOTIFY_TOKEN=xxx

# PostgreSQL (User Profile)
DATABASE_URL=postgresql://user:password@localhost:5432/shipify_profiles

# Google Drive
GOOGLE_DRIVE_FOLDER_ID=xxx
GOOGLE_SERVICE_ACCOUNT_JSON=credentials.json

# App
CONFIDENCE_THRESHOLD=0.75
AUTO_MODE=false

# Admin UI
ADMIN_USERNAME=admin
ADMIN_PASSWORD=xxx
```

---

## 07 RAG Pipeline

### Ingestion flow
```
knowledge/ folder (PDF + Word + .md)
        ↓
ingest.py อ่านทุกไฟล์
        ↓
chunk 300-500 tokens, overlap 50
        ↓
metadata: intent, source, filename, modified_date
        ↓
embed ด้วย OpenAI text-embedding-3-small
        ↓
upsert เข้า Qdrant collection
```

### Intent categories (4 หมวด)
- สต็อก — สินค้า ความพร้อม (route ไป ERP ถ้าถามราคา/จำนวน)
- ออเดอร์ — สถานะ tracking (route ไป ERP เสมอ)
- นโยบาย — การคืน การเคลม เงื่อนไข (RAG)
- ทั่วไป — ข้อมูลบริษัท วิธีสั่งซื้อ FAQ (RAG)

### Re-embed trigger
- กดปุ่มใน Admin Web UI
- Google Drive cron job (ทุกคืน 02:00)
- Manual รัน script

---

## 08 ERP Bridge

- ต่อ PHP/MySQL ของ Shipify แบบ read-only เท่านั้น
- ห้ามแก้ schema หรือ code ERP เดิมเด็ดขาด
- Query เฉพาะ: สต็อกสินค้า, สถานะออเดอร์, ราคา, ค่าขนส่ง
- latency < 500ms
- มี error handling + timeout + fallback message เสมอ
- Mod จะส่ง SSH credentials ใน Phase 2

---

## 09 User Profile System

### Schema (PostgreSQL)
```sql
user_profiles (
  line_user_id        TEXT PRIMARY KEY,
  display_name        TEXT,
  segment             TEXT,     -- 'cold' | 'warm' | 'hot'
  order_count         INT,
  total_spend         DECIMAL,
  preferred_price_range TEXT,
  chat_style          TEXT,     -- 'short' | 'detailed'
  last_active         TIMESTAMP,
  notes               TEXT      -- AI summary ของลูกค้าคนนี้
)
```

### Segment logic (ต้องคลียร์กับ Mod)
| Segment | เงื่อนไขเบื้องต้น | AI ตอบแบบ |
|---|---|---|
| Cold | ลูกค้าใหม่ ยังไม่เคยสั่ง | แนะนำ welcome |
| Warm | เคยสั่ง 1-2 ครั้ง | รู้จัก ติดตาม |
| Hot | สั่งบ่อย / มูลค่าสูง | VIP รู้ preference |

---

## 10 LINE Webhook

- FastAPI รับ POST จาก LINE
- Verify signature ด้วย LINE_CHANNEL_SECRET
- Session management — จำ context ข้ามข้อความ
- Rate limiting + failover
- Smart handoff ผ่าน LINE Notify (ไม่มี co-pilot UI)

---

## 11 Admin Web UI (Knowledge Manager)

### หน้าที่มี
1. Login — username/password
2. Documents — list .md files, แก้ไขใน browser, เพิ่ม/ลบ
3. Upload — drag & drop PDF/Word/MD → บันทึกใน knowledge/
4. Sync — กดปุ่ม re-embed ทั้งหมด หรือเฉพาะไฟล์ที่แก้
5. Preview — พิมพ์คำถาม ดูว่า RAG จะดึง chunks อะไรมา + AI จะตอบว่าอะไร

### Stack
- FastAPI + Jinja2 templates
- Login ง่ายๆ ด้วย session
- ไม่ต้อง React หรือ framework หนัก

---

## 12 Google Drive Sync

### Setup
1. สร้าง Google Service Account
2. แชร์ folder ให้ Service Account
3. ใส่ GOOGLE_DRIVE_FOLDER_ID และ credentials.json ใน .env

### Cron job (ingestion/gdrive_sync.py)
- รันทุกคืน 02:00 (หรือ manual trigger จาก Admin UI)
- ตรวจหาไฟล์ใหม่/แก้ไข เทียบ modified_time
- download → อ่าน → chunk → embed → upsert Qdrant
- ส่ง LINE Notify แจ้ง Mod เมื่อ sync เสร็จ

---

## 13 Code Standards

- Python 3.13+
- ทุก external API call ต้องมี try/catch + timeout + fallback
- ห้าม silent fail
- ห้าม hardcode API keys ทุกอย่างอยู่ใน .env
- Branch: main = production, dev = working, feature/* = features
- ออกแบบ language-agnostic รองรับ Filipino/Malay ทีหลัง
- Source code ทั้งหมดเป็นของ Mod/client

---

## 14 Payment Terms

| งวด | เงื่อนไข | จำนวน |
|---|---|---|
| งวด 1 | จ่ายก่อนเริ่มงาน (kickoff) | ฿5,000 |
| งวด 2 | Phase 2 ครบ + Mod demo staging ผ่าน | ฿10,000 |
| งวด 3 | Phase 3 ครบ + go-live + Mod sign off | ฿9,000 |
| รวม | | ฿24,000 |

หมายเหตุ: Web UI + Google Drive sync เป็น scope เพิ่มเติม
ต้อง quote กับ Mod ก่อนว่าจะรวมในงวดไหน หรือ quote แยก

---

## 15 Timeline 5 สัปดาห์

| Phase | สัปดาห์ | งาน |
|---|---|---|
| Phase 1 | W1–2 | ingestion pipeline (PDF+Word+MD), Qdrant setup, ERP spec |
| Phase 2 | W2–4 | AI core, LINE webhook, ERP bridge, Admin UI, Google Drive sync, staging |
| UAT | W4 | CS + admin ทดสอบ 8 ด้าน 100 คำถาม |
| Phase 3 | W5 | Go-live, bug fix, docs, handover |

---

## 16 UAT Testing 8 ด้าน

ทดสอบด้วย 100 คำถาม ผ่าน admin review portal เป้าหมาย accuracy >85%

| # | ด้าน | เกณฑ์ |
|---|---|---|
| 1 | ความถูกต้อง | ตรงกับ ERP/Knowledge base |
| 2 | Tone & ภาษาไทย | ตรงแบรนด์ Shipify |
| 3 | Intent routing | ERP vs RAG ถูก >75% |
| 4 | Edge cases | ภาษาวิบัติ/นอกเรื่อง |
| 5 | Smart handoff | ไม่มี miss |
| 6 | Response time | <2 วินาที 80% |
| 7 | User profile | segment ปรับ tone ถูก |
| 8 | Fallback | บอกไม่รู้ดีกว่าตอบผิด |

---

## 17 สิ่งที่ Mod จะส่งให้ (ยังไม่ได้รับ)

| รายการ | Phase |
|---|---|
| Export ประวัติแชต LINE OA 1-2 ปี (JSON/CSV) | Phase 1 |
| เอกสาร FAQ / นโยบาย / คู่มือ (PDF/Word) | Phase 1 |
| Sample Q&A 30-50 ชุด | Phase 1 |
| LINE OA credentials | Phase 2 |
| ERP test environment access (SSH/API) | Phase 2 |
| AI API Keys (OpenAI/Anthropic) | Phase 2 |
| Tone guidelines | Phase 2 |
| Google Drive folder สำหรับ sync | Phase 2 |

---

## 18 สิ่งที่ยังต้องคลียร์กับ Mod

- [ ] Web UI + Google Drive sync รวมใน ฿24,000 หรือ quote เพิ่ม?
- [ ] เกณฑ์ Cold/Warm/Hot (สั่งกี่ครั้งถึงเป็น Hot?)
- [ ] ERP schema คร่าวๆ ตารางสต็อก/ออเดอร์/ราคา
- [ ] LINE User ID เชื่อมกับ ERP ได้เลยไหม
- [ ] Hosting ใช้ Railway หรือ DigitalOcean? billing ใครจ่าย?
- [ ] AI Model สุดท้ายใช้ GPT-4o หรือ Claude 3.5?
- [ ] Google Drive folder ID สำหรับ PDF sync

---

## 19 Step ที่ต้องทำต่อ (เรียงตาม priority)

### Phase 1B — ทำตอนนี้
- [ ] ingestion/pdf_reader.py — อ่าน PDF ด้วย pdfplumber
- [ ] ingestion/word_reader.py — อ่าน .docx ด้วย python-docx
- [ ] ingestion/ingest.py — รวมทุกประเภทไฟล์ chunk + metadata
- [ ] ingestion/embedder.py — embed + upsert Qdrant
- [ ] rag/searcher.py — search คืน top 3 chunks + score
- [ ] ทดสอบ retrieval accuracy >60%

### Phase 1C
- [ ] erp/bridge.py — MySQL read-only wrapper
- [ ] profiles/manager.py — CRUD + segment logic

### Phase 2
- [ ] line_bot/webhook.py — FastAPI LINE handler
- [ ] line_bot/intent.py — classifier 4 หมวด
- [ ] line_bot/tone.py — prompt system + few-shot Thai
- [ ] Smart handoff + LINE Notify
- [ ] admin/routes.py — Web UI knowledge manager
- [ ] ingestion/gdrive_sync.py — Google Drive sync
- [ ] Deploy staging Railway

### Phase 3
- [ ] Full auto mode (config flag)
- [ ] UAT 100 คำถาม + admin review portal
- [ ] Production deploy + monitoring
- [ ] Docs + admin guide + handover
