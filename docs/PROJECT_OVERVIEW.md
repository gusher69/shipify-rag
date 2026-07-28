# Project Overview

## Scope & Objective

Shipify AI Platform automates first-line customer service on **LINE Official Account** for Shipify (China import/logistics), and is architected to be reused for future customers in other industries via configuration rather than custom code (see the Platform-First Architecture principle in `CLAUDE.md`).

The system answers two broad categories of customer question:

1. **Static / policy questions** ("What's your return policy?", "How do I track my shipment number format?") — answered from a **Knowledge Base** (RAG over ingested PDFs/Word/Excel/Markdown documents).
2. **Live / record-specific questions** ("Where is my order #12345?", "What's my wallet balance?", "Do I have any coupons?") — answered by calling the customer's own **ERP system** through a configured **Business Action**, never from cached/stale data.

When the system isn't confident enough to answer safely, or the customer is negotiating/complaining, the conversation is escalated to a human agent via **Smart Handoff** (LINE Notify), with full context attached (intent, sentiment, retrieved knowledge, ERP data, chat history).

## Primary User Flow (customer-facing)

```
1. Customer sends a message in LINE OA
2. Intent + sentiment + language are detected
3. Decision Engine decides: Knowledge Base, ERP Business Action, or both
4. Answer is generated (RAG-grounded and/or ERP-data-grounded), tone-adjusted
5. If confidence is high enough → auto-reply
   If confidence is low, or a handoff trigger is detected → Smart Handoff to CS team
6. Customer's profile (segment, chat style, last active) is updated
```

## Primary User Flow (admin-facing)

The **Admin Web UI** is where the actual product work happens day-to-day:

- **Knowledge Base** — upload documents manually or let the nightly Google Drive sync pull them in; preview how a chunk will be retrieved; manage File Library and Sync Activity.
- **ERP Integration (Business Actions)** — the most actively developed area. An admin pastes a cURL command / Postman collection / API documentation into the **AI-Guided ERP Setup** wizard; AI analyzes it and proposes a full configuration (parameters, which fields are secrets vs. customer search fields, HTTP details, AI behaviour text, routing preference). The admin reviews and edits everything on a **Review & Edit** page — nothing saves without explicit confirmation — then saves as a Business Action. The Review page also surfaces *why* AI made each decision, a semantic classification of every field (canonical business names, detected entities/intents), and Knowledge Base recommendations derived from what was detected.
- **AI Playground** — test how the AI responds to sample conversations without touching LINE.
- **Prompt Studio** / **AI Policies** — tune system prompts and behavioural policies.
- **AI Evaluation / Production Validation** — benchmark and validate AI quality before/after changes.

## Key Feature Areas (for orientation — see `docs/CURRENT_STATUS.md` for maturity of each)

| Area | What it does |
|---|---|
| RAG Knowledge Base | Ingest → chunk → embed → retrieve → cite |
| Business Action Center | Config-driven ERP/API integration registry |
| AI Auto Setup / Smart Capability Setup | AI-assisted Business Action configuration from a pasted API description |
| Decision Engine | Routes a message to the right data source |
| Credential Store | Encrypted secret storage for Business Action credentials |
| LINE OA Webhook | The actual customer-facing channel |
| User Profiles | Customer segmentation for tone/handling |
| Admin Web UI | Everything above, managed by a human |

## Out of Scope (for this codebase, currently)

- Writing back to the customer's ERP (the ERP bridge is explicitly **read-only**).
- Any channel other than LINE (no WhatsApp/Facebook/web-chat integration exists yet).
- A no-code visual workflow builder (mentioned as a P3 Future Enhancement in `CLAUDE.md`, not built).
