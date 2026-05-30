# PennyPath — Development Phases

Each phase delivers a complete product experience for one feature area, then hardens it before
moving on. Within each phase (or sub-phase), work progresses through the steps below so each
loop stays short and testable.

## Steps within every phase

| Step | What happens | Infrastructure |
|---|---|---|
| **1. Local** | Build the feature as a CLI / local script; iterate fast on quality | Local files / SQLite, no cloud |
| **2. Code analysis** | Review error handling, edge cases, prompt quality, test coverage | Static review + unit tests |
| **3. Self-experiment** | Run it with your own real data for 1–2 weeks; iterate until it feels genuinely useful | Local + real data |
| **4. Tryout** | Deploy minimally; invite 3–10 people; gather real feedback | Light cloud or Docker |
| **5. Production ready** | Security hardening, monitoring, full cloud | AWS full stack |

---

## Phase 1 — Personal Finance Visibility

**Goal:** Deliver the complete base-plan experience — a storage-and-search foundation, an
annotated personal dashboard, and Q&A chat — end to end. A user can link accounts (Plaid or
manual upload), see where their money goes on an annotated dashboard, and ask the companion
questions in plain language.

This is the whole product's foundation. Phase 1 is built in three sequential workstreams —
**1A Data Foundation → 1B Dashboard → 1C Companion Chat** — because the dashboard needs the
data layer, and chat needs both. Each workstream runs through the early maturity steps
(Local → Code analysis → Self-experiment); once all three cohere, a shared Tryout (1D) deploys
them. Don't move on until the base-plan experience genuinely feels useful to real users.

### Delivery surfaces

PennyPath ships on a **web app** and an **MCP connector** (the companion reachable inside
Claude / ChatGPT) first. **Native mobile is deferred** to a later phase — the product's value
is the agent (storage, search, memory, companion logic), and the web + MCP surfaces prove that
without the cost and lead time of an app. The storage and agent layers stay surface-agnostic,
so native mobile can be added later without reworking them.

---

### Phase 1A — Data Foundation

**Goal:** A storage, search, and context layer the rest of the product builds on.

**In scope:**
- **Financial Record Store** — relational schema for institutions, accounts, and transactions,
  with a `flow_type` field (spending / income / transfer / fee / refund). Plaid and statement
  ingestion both write normalized records into the store.
- **Internal vs. external money movement** — detect and pair the two legs of an internal
  transfer (e.g. a credit-card payment) so they are excluded from spending/income aggregates.
- **Semantic search** — embed transaction text; hybrid search combining vector similarity with
  structured filters (date / category / amount / account).
- **User Context / Memory Store** — tiered memory: a structured profile (working memory) plus
  episodic memory, with an LLM-driven consolidation job that compacts, synthesizes, and forgets
  so the agent always has the latest ~3–6 months plus a compact longer-history background.
- **MCP Data-Access Layer** — typed query tools, hybrid semantic search, a guarded `run_sql`
  escape hatch, `get_user_context`, `search_memory`. Agent logic talks only to the MCP.
- CLI commands to ingest data and exercise every MCP tool.

**Local stage:** extend the existing SQLite store (`src/storage.py`); use `sqlite-vec` or an
embedding table for vectors. Formalize the existing `src/wiki_updater.py` into the consolidation
job. Cloud Postgres + `pgvector` comes at Tryout (1D).

**Out of scope:** dashboard charts, chat polish, auth, web UI, cloud.

**Success bar:** From the CLI you can ingest Plaid + statement data, run a semantic search
("coffee-related spending") and a typed query, and confirm the memory store compacts old
context while keeping recent detail.

---

### Phase 1B — Dashboard

**Goal:** The annotated personal dashboard — standard charts plus LLM insight.

**In scope:**
- **Standard charts** — Spending (category donut + breakdown), Income (donut + 12-month
  history), Transactions (filterable list), Cash Flow (12-month income vs. spending). Internal
  transfers excluded from spending and income totals.
- **LLM annotation layer** — each chart carries a short, warm annotation and, where useful, a
  gentle suggested action. Annotations are generated when new data syncs and cached; a manual
  "refresh insights" action is available.
- **Pinned-chart rendering** — the dashboard renders and persists custom charts pinned from
  chat (chart generation itself is 1C).
- A web surface to render the dashboard.

**Out of scope:** native mobile, coaching, drill-down chat (1C).

**Success bar:** The dashboard renders the four standard charts from real data, each carries a
warm and accurate annotation, and totals correctly exclude internal transfers.

---

### Phase 1C — Companion Chat

**Goal:** Q&A chat with drill-down and custom chart generation.

**In scope:**
- Q&A over the user's real data, answered through the MCP tools.
- **Drill-down** from any dashboard chart ("why was March higher?", "show me just weekends").
- **Custom chart generation** — the companion produces a new chart in response to a question.
- **Pin from chat** — a generated chart can be pinned to the dashboard (1B renders it).
- Conversation memory wired into the User Context / Memory Store.

**Out of scope:** native mobile, coaching, push notifications.

**Success bar:** `chat` answers finance questions accurately and warmly, can generate a
relevant custom chart, and a pinned chart appears on the dashboard on the next visit.

---

### Step 1D — Tryout

**Goal:** Let 3–5 real people try the base-plan experience on the web + MCP surfaces.

**Adds:**
- PostgreSQL + `pgvector` (local Docker for dev; RDS for the tryout deploy), replacing local SQLite.
- FastAPI backend (`api/` alongside `src/`) serving the dashboard and chat.
- JWT auth (email + password registration).
- Plaid Link web flow (a simple HTML page is fine); PDF/CSV upload via a basic web form.
- The MCP server published so testers can also reach the companion inside Claude.
- Minimal deployment: Lambda + API Gateway + RDS.
- Invite link to share with testers.

**Folder structure adds:**
```
api/
  main.py          # FastAPI app + Mangum adapter
  auth.py          # JWT registration + login
  plaid.py         # token exchange + webhook ingestion
  chat.py          # conversation endpoints
  dashboard.py     # chart data + annotations
mcp/
  server.py        # MCP data-access server (typed tools, semantic search, run_sql)
db/
  models.py        # SQLAlchemy models
  migrations/      # Alembic migrations
web/
  onboarding.html  # minimal Plaid Link + upload form
src/               # unchanged — reused by the API and the MCP server
```

**Out of scope:** native mobile, coaching, payments, full security hardening.

**Success bar:** A friend links their account, sees an annotated dashboard, has a real
conversation with the companion, and says "I'd actually use this."

---

## Phase 2 — Privacy & Trust

**Goal:** Make the product safe and trustworthy at scale. This phase runs in parallel with
Phase 1 Step 1D (Tryout) — start it once the feature is working and you're preparing for
real user traffic.

**In scope:**

*Data security:*
- KMS encryption for Plaid access tokens (encrypt before DB write; decrypt only in Lambda memory)
- Secrets Manager for all API keys (Plaid, LLM, Stripe) — remove from `.env` in production
- Lambda inside VPC; RDS with no public endpoint
- Statement files in private S3 bucket; presigned URLs with 15-minute expiry
- MCP data access through a read-only, user-scoped DB role; `run_sql` is SELECT-only and timed out

*Auth hardening:*
- JWT rotation and revocation on logout
- Refresh token invalidation on suspicious activity
- Rate limiting on auth endpoints

*Webhooks:*
- Plaid webhook `Plaid-Verification` JWT header verified on every request
- Stripe webhook `Stripe-Signature` HMAC verified before processing

*User rights:*
- Account deletion flow: wipe all user data across all tables within 30 seconds of request
- Episodic memory consolidated and purged past the retention window
- Data export (user can download their finance records and history)

*Compliance:*
- Privacy policy and terms of service (before public launch)
- GDPR / CCPA: data minimization audit, right to deletion, no cross-user data leakage

*Observability:*
- Audit log: account linking, data access, message dispatch → CloudWatch (90-day retention)
- Error alerting for Lambda failures, DB connection issues, LLM errors
- Uptime monitoring

**Success bar:** An independent security review finds no critical or high issues.
Users can delete their account and all data in a few clicks.

---

## Phase 3 — Coaching Plan

**Goal:** Add the optional paid coaching feature. Users who subscribe get a structured 6-month
plan with bi-weekly check-ins, weekly progress summaries, and progressive phase targets.

### Step 3.1 — Local

**In scope:**
- Coaching plan state machine: Foundation (7 days) → Building (21 days) → Momentum (3 months)
- Coaching-aware SpendingContext: adds `coaching_phase` and `phase_target` fields
- Updated companion prompt for coaching context (phase-specific tone and framing)
- Phase transition detection + milestone message generation
- CLI commands:
  - `python -m src.cli coaching-checkin` — generate a bi-weekly check-in
  - `python -m src.cli weekly-summary` — generate a weekly progress summary
  - `python -m src.cli advance-phase` — manually advance to the next phase (for testing)
- Local coaching state file (`data/coaching.json`)

**Out of scope:** payments, mobile UI, scheduling, DB

**Success bar:** `python -m src.cli coaching-checkin` produces a check-in that meaningfully
references the current phase target and feels distinctly different from a base-plan check-in.
The Foundation → Building transition message acknowledges the milestone without sounding hollow.

---

### Step 3.2 — Code Analysis

- Phase transition boundary conditions (what if check-in happens exactly on day 7? Day 8?)
- Does `phase_target` in SpendingContext actually influence LLM output, or is it ignored?
- Coaching vs. base-plan tone: are they distinguishable? Is coaching more directive than it should be?
- Weekly summary: does it feel useful or like a report card?

---

### Step 3.3 — Self-Experiment

Run the full Foundation phase (7 days) manually via CLI:
- Trigger check-ins daily
- Advance phase at day 7; observe the transition message
- Note where coaching messages feel preachy, repetitive, or generic
- Iterate on prompt and SpendingContext until coaching feels meaningfully different from the base plan

---

### Step 3.4 — Tryout

**Adds:**
- `coaching_plans` and `subscriptions` DB tables
- Stripe integration (test mode — real card flow, no live charge)
- API endpoints: `/coaching/subscribe`, `/coaching/status`, `/coaching/advance-phase`
- Coaching UI: coaching plan view (current phase, target, progress, next check-in date)
- EventBridge rules per coaching user (bi-weekly + weekly triggers)
- Phase transition notifications
- 3–5 beta users who agree to try the paid flow

**Out of scope:** live Stripe billing, App Store in-app purchase

**Success bar:** A beta user goes through the full Foundation phase (7 days), advances to Building,
and says the check-ins felt meaningfully different and useful.

---

### Step 3.5 — Production Ready

**Adds:**
- Stripe production keys + live billing ($9.99/month)
- Cancellation and downgrade flow (cancel → lose coaching features, keep base plan)
- Graduation tracking: record when users complete all 3 phases
- Coaching analytics (internal): graduation rate, engagement per phase, churn by phase
- App Store in-app purchase (optional — can also keep direct Stripe)

---

## Phase 4 — Native Mobile

**Goal:** Add a native iOS / Android app once the web + MCP experience is proven. The web app
remains; mobile is an additional surface, valuable mainly for proactive push notifications and
a polished on-the-go experience. This phase is **demand-gated** — build it when traction and
user demand justify it, which may be before or after Phase 3.

**Adds:**
- React Native project (Expo or bare workflow) in `mobile/`
- All screens from `design/product.md`: onboarding wizard, annotated home dashboard, companion
  chat, monthly analysis, settings
- Plaid Link React Native SDK; JWT stored in the device secure keychain
- API integration against the existing backend — the surface-agnostic design means no API rework
- Push notifications for check-ins and monthly analysis (SNS → APNs / FCM)
- App Store (iOS) + Google Play (Android) submission

**Folder structure adds:**
```
mobile/
  app/
    (tabs)/
      index.tsx       # home dashboard
      chat.tsx        # companion chat
      analysis.tsx    # monthly analysis
      settings.tsx    # account + preferences
    onboarding/
      register.tsx
      profile.tsx
      goal.tsx
      link-accounts.tsx
  components/
  lib/
    api.ts            # typed API client wrapping fetch
    auth.ts           # JWT keychain helpers
  app.json
  package.json
```

**Success bar:** A new user can complete onboarding on a real device, see their dashboard, and
have a conversation with the companion — all without touching a terminal. The app is live in
both stores.

---

## Working with Claude across phases

### How Claude knows which phase and step you're in

Update `CLAUDE.md` at the start of each phase or sub-phase:

```
## Current Phase: 1A — Data Foundation
In scope: local CLI, real Plaid + statement data, building the storage / search / memory layer
and the MCP data-access server.
Out of scope: dashboard, chat polish, auth, web UI, cloud, coaching.
```

Open each session with a one-line scope reminder:
> "Phase 1A. I want to improve the consolidation job — old context isn't compacting well."

### Advancing a step

1. Update `## Current Phase` in `CLAUDE.md`
2. Open the next session: *"Moving to Phase 1B (Dashboard). Here's what that adds..."*

### Preventing scope creep

> "Keep it Phase 1A — storage and MCP only, no dashboard or chat work yet."

### Readiness check before advancing

> "Before we move from 1A to 1B, review src/ and tell me what's solid in the data layer
>  and what still needs work."
