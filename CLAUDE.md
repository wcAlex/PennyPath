# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current Phase: 1B — Dashboard

We are in Phase 1, sub-phase 1B. See `design/phases.md` for the full phase breakdown.

**Phase 1A is complete.** The data foundation (`src/storage.py`, `src/statement_ingester.py`, `src/plaid_client.py`) ingests Plaid + PDF/CSV statements into a unified `transactions` table with magnitude-only amounts and a closed-enum `section_type`. The `v_transactions_signed` view derives per-account balance flow. See `design/storage.md` for the contract. Build on top of this; do not re-litigate the storage schema unless a 1B requirement makes it necessary.

**In scope (Phase 1B):**
- **Four standard charts** rendered from the existing `transactions` data:
  - Spending — category donut + breakdown
  - Income — donut + 12-month history
  - Transactions — filterable list (date / category / amount / account)
  - Cash Flow — 12-month income vs. spending
- **Internal transfers excluded** from spending and income totals. The pairing logic is the query-layer concern deferred from 1A — implement it here.
- **LLM annotation layer** — each chart carries a short, warm annotation and, where useful, a gentle suggested action. Annotations are generated when new data syncs and cached; a manual "refresh insights" action is available.
- **Pinned-chart rendering** — the dashboard renders and persists custom charts pinned from chat. (Chart *generation* from chat is 1C; 1B just needs the rendering / persistence slot.)
- **A web surface** to host the dashboard.

**Out of scope:** drill-down chat and custom chart generation (Phase 1C); native mobile (deferred); auth, multi-user, cloud Postgres, AWS (Phase 1D Tryout); coaching (Phase 3).

When helping with code, stay within Phase 1B constraints. Do not build chat drill-down, custom chart generation from chat, native mobile, or coaching unless explicitly asked. The web surface is the delivery target — `src/web_chat.py` is the existing seed to extend or replace.

---

## Project

**PennyPath** is a personal finance companion mobile app — not a budgeting tool. The product philosophy is non-judgmental, low-pressure habit coaching. It helps users build healthy financial discipline progressively and adaptively, like a caring buddy rather than an enforcer.

Two-tier product: a **base plan** (personal finance visibility — dashboard, Q&A chat, monthly analysis) and an optional **paid coaching plan** ($9.99/month, 6-month structured program with progressive habit phases).

## Architecture

The system has three main layers:

### 1. Mobile App (Frontend)
User-facing mobile app for:
- Account registration and onboarding (profile, goal, account linking)
- Viewing the personal finance dashboard (spending overview, category trends, goal progress)
- Interactive Q&A chat with the companion
- Monthly analysis report
- Coaching plan enrollment and progress tracking

### 2. Backend API
Handles all business logic:
- User and account management
- Plaid integration (bank/card linking, transaction sync) and PDF/CSV statement ingestion
- AI/LLM orchestration — generating check-in messages, monthly analyses, and coaching plans
- Finance analysis and progress tracking

### 3. Companion Agent
The AI companion runs on a schedule and reacts to user messages:
- Pulls user transaction data and financial state
- Generates contextual, personalized messages using an LLM
- Delivers bi-weekly coaching check-ins and weekly progress summaries
- Handles user replies in the in-app chat interface

## Key Integrations

- **Plaid** — bank account and credit card linking, read-only transaction retrieval
- **LLM (DeepSeek / Claude)** — powers the companion's tone, spending analysis, and coaching plans

## Data Model (planned)

User data (profiles, linked accounts, preferences, goals) and synthesized finance snapshots are stored in a database. Raw bank transaction data is fetched from Plaid on demand or synced periodically; sensitive data should be minimized and encrypted at rest.

## Product Tone

When building AI prompts, message templates, or any user-facing copy, the companion's voice must be:
- Warm and non-judgmental ("you spent a bit more this week — that's okay, here's what I noticed")
- Progressive (small wins, incremental goals, never shame)
- Conversational (short messages, not reports)
- Adaptive (responds to the user's actual patterns, not generic advice)
