# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current Phase: A — Local Statement Companion

We are mid-Phase A. See `design/phases.md` for the full phase breakdown.

**Built so far:** Statement ingestion (PDF / CSV) (`src/statement_ingester.py`), unified `transactions` storage (`src/storage.py`) with the `section_type` enum and `v_transactions_signed` view, the four standard dashboard charts, Q&A chat backed by typed query tools (`src/chat_agent.py`, `src/chat_tools.py`), and a local web surface (`src/web_chat.py`, `src/templates/dashboard.html`). See `design/storage.md` for the storage contract — build on it; do not re-litigate the schema unless a Phase A requirement forces it.

**Rough edges to smooth before Phase B (Cloud Deploy):**
- **Chat iteration** — answer quality, tool selection, and conversational flow are not yet at the level we'd want a stranger to see.
- **Goal management** — profile and goals are first-class product concepts (they shape companion tone and dashboard emphasis), but UI and persistence for setting / editing them are incomplete. Goals are **multi-timeframe and category-scoped** (e.g. "~$40k/year on ski and travel" alongside monthly intentions); the companion co-creates them by proposing numbers grounded in the user's actual history. Progress tiles per intention live on the dashboard.
- **Pin-to-dashboard** — chart generation in chat works; persisting pinned custom charts back onto the dashboard is incomplete.
- LLM annotation polish, internal-transfer pairing edge cases, and refresh-insights UX are also Phase A finish work.

**Out of scope right now:** Plaid, payment/bill reminders, purchase warnings, recommendation engine, native mobile, auth, multi-user, cloud. These belong to later phases (D, D, D, E, C, B, B, B respectively).

When helping with code, stay within Phase A constraints. The local web surface is the delivery target for now — `src/web_chat.py` is the existing seed to extend or replace.

---

## Project

**PennyPath** is a personal finance companion — not a budgeting tool. The product philosophy is non-judgmental, low-pressure habit formation. It helps users build healthy financial discipline progressively and adaptively, like a caring buddy rather than an enforcer.

One product, not a tiered program: ingest the user's data, surface insight on an annotated dashboard, let the user shape that dashboard through chat (pinning generated charts), and use chat to discuss what to do next. Later paid extensions (bill reminders, purchase warnings, spending-reduction recommendations) extend the same companion once we have real-time bank-linking access — see `design/phases.md` Phases D and E.

## Architecture

The system has three main layers:

### 1. Web Frontend
User-facing web UI for:
- Onboarding (profile, goal, first statement upload)
- Viewing the personal finance dashboard (spending overview, category trends, goal context)
- Interactive Q&A chat with the companion
- Monthly analysis report
- Native mobile is deferred to Phase C and is demand-gated.

### 2. Backend API
Handles all business logic:
- User and account management (profile + goal)
- PDF / CSV statement ingestion (Plaid arrives in Phase D)
- AI/LLM orchestration — annotations, chat answers, monthly analyses
- Finance analysis and progress tracking against the user's stated goal

### 3. Companion Agent
The AI companion reacts to user messages and (later) runs on a schedule:
- Pulls user transaction data and financial state
- Generates contextual, personalized responses using an LLM
- Handles user replies in the in-app chat interface
- Phase D adds proactive check-ins (bill reminders, purchase warnings) once real-time bank data is available

## Key Integrations

- **LLM (DeepSeek / Claude)** — powers the companion's tone, spending analysis, and annotations
- **Plaid** — bank account and credit card linking, read-only transaction retrieval (Phase D, not yet integrated)

## Data Model (planned)

User data (profiles, linked accounts, preferences, goals) and synthesized finance snapshots are stored in a database. Raw bank transaction data is fetched from Plaid on demand or synced periodically; sensitive data should be minimized and encrypted at rest.

## Product Tone

When building AI prompts, message templates, or any user-facing copy, the companion's voice must be:
- Warm and non-judgmental ("you spent a bit more this week — that's okay, here's what I noticed")
- Progressive (small wins, incremental goals, never shame)
- Conversational (short messages, not reports)
- Adaptive (responds to the user's actual patterns, not generic advice)
