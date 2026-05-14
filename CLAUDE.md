# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current Phase: 1 — Local Agent

We are in Phase 1. See `design/phases.md` for the full phase breakdown.

**In scope:** single-user local agent, Plaid (one account), Python CLI, local JSON memory, Claude API, console output.  
**Out of scope:** web UI, database, auth, multi-user, AWS, background scheduling.

When helping with code, stay within Phase 1 constraints. Do not add multi-user support, database access, or cloud infrastructure unless explicitly asked.

---

## Project

**PennyPath** is a personal finance companion — not a budgeting tool. The product philosophy is non-judgmental, low-pressure habit coaching. It helps users build healthy financial discipline progressively and adaptively, like a caring buddy rather than an enforcer.

Key differentiator: a conversational AI companion delivered via Signal (or another end-to-end encrypted messaging channel) that checks in with users daily in a human, coaching-style tone — not alerts, not dashboards, but real conversation.

## Architecture

The system has three main layers:

### 1. Web App (Frontend)
User-facing website for:
- Account registration and onboarding
- Linking bank accounts and credit cards via Plaid
- Viewing finance reports and spending summaries
- Managing notification preferences (Signal number, chat channel)

### 2. Backend API
Handles all business logic:
- User and account management
- Plaid integration (bank/card linking, transaction sync)
- AI/LLM orchestration — generating daily check-in messages, spending insights, and coaching plans
- Scheduling and dispatching messages to Signal / encrypted chat
- Finance analysis and progress tracking

### 3. Messaging Layer
The daily companion runs as an async agent:
- Pulls user transaction data and financial state
- Constructs a contextual, personalized message using an LLM
- Sends via Signal API or equivalent encrypted channel
- Handles user replies and maintains conversation continuity

## Key Integrations

- **Plaid** — bank account and credit card linking, transaction retrieval
- **Signal / encrypted messaging** — primary channel for the AI companion chatbot
- **LLM (Claude)** — powers the companion's tone, coaching language, and adaptive plans

## Data Model (planned)

User data (profiles, linked accounts, preferences, goals) and synthesized finance snapshots are stored in a database. Raw bank transaction data is fetched from Plaid on demand or synced periodically; sensitive data should be minimized and encrypted at rest.

## Product Tone

When building AI prompts, message templates, or any user-facing copy, the companion's voice must be:
- Warm and non-judgmental ("you spent a bit more this week — that's okay, here's what I noticed")
- Progressive (small wins, incremental goals, never shame)
- Conversational (short messages, not reports)
- Adaptive (responds to the user's actual patterns, not generic advice)
