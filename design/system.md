# PennyPath — System Design

## System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│  Clients                                                         │
│  Web app   ·   Claude / ChatGPT (MCP connector)   ·   Mobile*    │
│  (* native mobile is a later phase — see design/phases.md)       │
└───┬──────────────────────────────────────────────────────────────┘
    │ HTTPS
    ▼
┌──────────────────────────────────────────────────────────────────┐
│  Backend API  (FastAPI / Lambda + API Gateway HTTP API)          │
│  - Auth, user mgmt, onboarding                                   │
│  - Plaid token exchange + webhook ingestion                      │
│  - Statement upload (presigned S3 URL)                           │
│  - Dashboard, conversation, coaching, Stripe endpoints           │
└──────┬───────────────────┬──────────────────┬────────────────────┘
       ▼                   ▼                  ▼
┌──────────────┐    ┌──────────────┐   ┌────────────────────┐
│  Companion   │◄───│  SQS Queue   │   │   S3 Bucket        │
│  Agent       │    │ (async jobs) │   │  (statement        │
│ (Python      │    └──────────────┘   │   uploads)         │
│  Lambda)     │                       └─────────┬──────────┘
│ scheduled +  │                                 ▼
│ reactive Q&A │                       ┌────────────────────┐
└──────┬───────┘                       │  Statement Parser  │
       │ reads                         │  (Python Lambda)   │
       │ via MCP                       └─────────┬──────────┘
       ▼                                         │ writes records
┌─────────────────────────────────────────┐      │
│  MCP Data-Access Layer  (read-only)     │      │
│  typed tools · hybrid semantic search · │      │
│  guarded run_sql · get_user_context ·   │      │
│  search_memory                          │      │
└────────┬────────────────────┬───────────┘      │
         ▼                    ▼                  ▼
┌──────────────────┐  ┌────────────────────────────────────┐
│  User Context /  │  │  Financial Record Store            │
│  Memory Store    │  │  Postgres + pgvector               │
│  profile +       │  │  institutions · accounts ·         │
│  episodic memory │  │  transactions (+ vector embedding) │
└──────────────────┘  └────────────────────────────────────┘

┌────────────────────────────────────────────────┐
│  External APIs                                 │
│  Plaid   ·   DeepSeek / Claude   ·   Stripe    │
└────────────────────────────────────────────────┘
```

---

## Components

### Clients

PennyPath's logic lives in the backend and the MCP layer; the client is a surface, not the product. Three surfaces, delivered in order (see `design/phases.md`):

- **Web app** — the primary Phase 1 surface. Hosts onboarding, the Plaid Link flow, the annotated dashboard, and companion chat. Auth is a short-lived JWT.
- **MCP connector** — the same MCP server that backs the agent internally exposes a curated tool subset to users inside Claude / ChatGPT, so the companion is reachable without a dedicated app.
- **Native mobile (React Native)** — deferred to a later phase. When built, it adds APNs / FCM push (via **AWS SNS Mobile Push**) for proactive check-ins and uses the **Plaid Link React Native SDK**.

Across every surface, Plaid linking exchanges only the short-lived `public_token` via the backend — the client never sees the Plaid `access_token`.

---

### Backend API

**Python FastAPI** deployed as **AWS Lambda** via **Mangum** (ASGI adapter) behind **API Gateway (HTTP API)**.

Responsibilities:
- User registration, login, JWT issuance and refresh
- Onboarding: finance profile and goal persistence
- Plaid `public_token` → `access_token` exchange; store encrypted token in DB
- Receive and verify Plaid webhooks (`TRANSACTIONS` events) → enqueue sync job to SQS
- Generate presigned S3 URLs for statement file uploads
- Receive and verify Stripe webhooks → update subscription status in DB
- Serve dashboard data, conversation history, and monthly analyses
- Coaching plan creation, phase advancement, and status queries
- Receive inbound chat messages → enqueue to SQS

The API is stateless; all state lives in PostgreSQL or the job queue.

---

### Storage Layer

Two complementary stores, both reached through the MCP Data-Access Layer (below). Unlike the earlier design, raw finance records **are** persisted — the dashboard, chat drill-down, and semantic search all require queryable history.

#### Layer 1 — Financial Record Store

The system of record for all finance data. **PostgreSQL on RDS with the `pgvector` extension.**

| Table | Purpose |
|---|---|
| `users` | Credentials, finance profile, notification preferences |
| `institutions` | Banks / card issuers — name, Plaid institution id |
| `accounts` | Per user, per institution — name, mask (last 4), type (checking / credit / savings), source (plaid / statement) |
| `transactions` | Every finance record — see schema below |
| `linked_accounts` | Plaid item + encrypted `access_token` per user |
| `uploaded_statements` | Metadata for user-uploaded PDF/CSV files (S3 key, parse status) |
| `coaching_plans` | Active plan per user: current phase, phase start date, plan start date |
| `subscriptions` | Stripe subscription state: customer ID, subscription ID, status, period end |
| `push_tokens` | Device push tokens per user (platform: ios / android) |

**`transactions` schema** — the heart of the store:

| Field | Notes |
|---|---|
| `id`, `user_id`, `account_id` | Identity and ownership |
| `date`, `posted_date` | Transaction and posting dates |
| `amount`, `direction` | Magnitude + `debit` / `credit` |
| `flow_type` | `spending` · `income` · `transfer` · `fee` · `refund` |
| `category`, `merchant`, `description`, `raw_description` | Classification, display name, original statement text |
| `counterparty_account_id`, `transfer_group_id` | Set for internal transfers (see below) |
| `source`, `source_file` | `plaid` / `statement_csv` / `statement_pdf` |
| `embedding` | `pgvector` vector of `merchant + description + category` |
| `ingested_at` | Audit |

**Internal vs. external money movement.** When a user moves their own money — e.g. a credit-card payment showing as `MOBILE BANKING PAYMENT TO CRD 8373` (−$217.93) on checking and `PAYMENT FROM CHK 0790` (+$217.93) on the card — that is one move, neither spending nor income. The two legs are paired by `transfer_group_id` (and `counterparty_account_id` when both accounts are linked), marked `flow_type = transfer`, and **excluded from all spending and income aggregates** so dashboards never double-count. Real merchant purchases and salary are `flow_type = spending` / `income`.

**Semantic search.** Each transaction's `embedding` lets the agent search spending by meaning — "coffee-related spending" matches `BLUE BOTTLE` even when the word "coffee" never appears. See *Search ranking* below.

#### Layer 2 — User Context / Memory Store

The agent's evolving understanding of the user. It must self-learn and stay current as the user's life changes — so it compacts, synthesizes, and forgets. Two tiers:

- **Profile (working memory)** — structured, always-current, small enough to load on every call: name, finance profile, current finance stage, goals, preferences.
- **Episodic memory** — chat turns and observations the agent has made. Recent entries (~3–6 months) are kept verbatim; older entries are compacted into summaries. Each entry carries a `salience` score, a `status` (`active` / `compacted` / `stale` / `archived`), and an `embedding` for semantic recall.

| Table | Purpose |
|---|---|
| `user_profile` | Working memory — finance stage, goals, preferences (structured + JSON) |
| `memory_episodes` | Episodic memory — chat turns, observations, summaries; salience, status, `embedding` |
| `monthly_analyses` | LLM-generated monthly narrative per user |
| `pinned_charts` | User-pinned custom charts generated from chat (chart spec + underlying query) |

**Consolidation.** A periodic, LLM-driven job — the production form of today's `src/wiki_updater.py` — compacts old episodes into summaries, folds durable facts into the profile, marks low-value entries `stale`, and archives the rest. "Forgetting" is a function of age and salience. The net effect: the LLM always receives **latest detail (~3–6 months) plus a compact longer-history background**, never the full raw history.

#### Storage security & retention

Plaid `access_token` values are encrypted before INSERT with a **KMS data key**, decrypted only in Lambda memory, and never logged. Stored finance records are encrypted at rest, with field-level minimization (no more than analysis requires). Episodic history past the retention window is consolidated and then purged; users can trigger immediate full deletion from settings.

**Deletion on account closure.** When a user closes their account, all of their personal data — finance records, accounts, linked Plaid items, profile, episodic memory, and conversation history — is permanently deleted across every table and both stores. This is a product promise, not a best-effort cleanup; the account-deletion flow is specified in `design/phases.md` (Phase 2).

---

### MCP Data-Access Layer

All agent and API data access goes through an **MCP server** over both stores — the agent never queries the database directly. This keeps data access uniform, auditable, and safe, and it doubles as the **external Claude connector** surface (the same server exposes a curated subset of tools to users inside Claude).

**Hybrid query model:**

| Tool | Purpose |
|---|---|
| `query_transactions(filters)` | Typed, parameterized query — date / category / amount / account / flow_type |
| `get_spending_breakdown` · `get_cash_flow` · `get_income_summary` | Pre-shaped aggregates for the standard dashboard charts |
| `search_transactions_semantic(text, filters)` | Hybrid search — vector similarity + structured filters |
| `run_sql(query)` | Guarded text-to-SQL escape hatch for complex drill-downs |
| `get_user_context()` | Profile / working memory |
| `search_memory(text)` | Semantic recall over episodic memory |

Typed tools cover the common cases; `run_sql` is the escape hatch for ad-hoc questions the typed tools cannot express.

**Safety.** The MCP connects through a **read-only database role**. Every query is scoped to the calling `user_id` — via row-level security or an always-injected `WHERE user_id = ?` predicate — so no tool can reach another user's data. `run_sql` accepts `SELECT` only, is validated before execution, and runs under a statement timeout.

#### Search ranking — do we need an evaluator?

**No dedicated evaluator or LLM reranker is planned.** Rationale:
- Typed and SQL queries are deterministic — `ORDER BY` is the ranking; an evaluator would add latency and cost for no gain.
- Semantic search already returns a ranking (pgvector cosine score). At single-user / few-thousand-record scale, cosine score + a similarity threshold + a sensible `LIMIT` is sufficient.
- An LLM-as-evaluator scoring each result is too slow and costly for transaction search.
- The real precision lever is **hybrid search** — structured filters narrow the candidate set before vector similarity ranks it. That is built into `search_transactions_semantic`.

If the Phase 1A self-experiment shows the agent retrieving irrelevant transactions, add a lightweight **cross-encoder reranker** — not a full LLM judge. Retrieval quality is something to *measure* during self-experiment, not pre-optimize.

---

### Companion Agent

A Python Lambda with multiple trigger paths:

**Scheduled — coaching check-in** (bi-weekly, coaching users only):
- **EventBridge Scheduler** fires per-user on the user's check-in schedule
- Reads recent transactions via MCP, generates a coaching-aware check-in message
- Stores the turn in `memory_episodes`, sends push notification

**Scheduled — weekly summary** (coaching users only):
- **EventBridge Scheduler** fires weekly per coaching user
- Generates a short progress summary against the current phase target
- Stores and pushes to device

**Scheduled — monthly analysis** (all users):
- **EventBridge Scheduler** fires monthly per user
- Fetches 30 days of transaction data, runs full analysis
- Generates narrative (anomalies, optimization opportunities, goal progress)
- Stores in `monthly_analyses`, sends push notification

**Reactive — inbound Q&A** (all users):
- User sends a chat message on any surface (web, MCP connector)
- API enqueues to **SQS** `{user_id, message_text, timestamp}`
- Companion Lambda consumes the message, queries data via MCP, generates a response
- Stores both turns in `memory_episodes`, delivers response via API or push notification

**Execution steps (all paths):**

```
1. Load user context (profile, goals, finance stage) via MCP get_user_context
2. Query the relevant window of transactions via MCP (typed tools / semantic search)
3. Pre-process → SpendingContext (Python, no LLM)
4. Recall relevant prior context via MCP search_memory + recent episodic turns
5. Build prompt: system prompt + SpendingContext + coaching context + memory
6. Call LLM API → assistant message text
7. Safety check: scan for flagged phrases → re-prompt once if triggered
8. Persist the outbound turn to episodic memory (memory_episodes)
9. Send push notification (APNs / FCM via SNS) where the surface supports it
```

Transaction sync (decrypting the Plaid `access_token`, fetching from Plaid, merging
statement data, writing records into the store) runs in the ingestion path — not here.
The agent always reads finance data from the store via MCP, never from Plaid directly.

---

### Statement Parser

A Python Lambda triggered by **S3 object creation events**.

When a user uploads a bank statement PDF or CSV via the web app:
1. Client requests a presigned S3 upload URL from the API
2. Client uploads the file directly to S3 (never passes through the API)
3. S3 event triggers the parser Lambda
4. Lambda parses the file → records using the `transactions` schema
5. Deduplicates against existing records (by date + amount + description hash)
6. Writes the records into the Financial Record Store, embeds them for semantic search, and updates `uploaded_statements.parse_status`

PDF parsing uses text extraction; CSV parsing uses column mapping with flexible header detection. Both normalize into the same `transactions` schema so the agent sees a unified data source regardless of origin (Plaid or uploaded statement).

---

### Coaching Plan Engine

Coaching is managed as state in the `coaching_plans` table, advanced by the Companion Agent and API.

**Phase transitions:**
- Foundation (7 days) → Building (21 days) → Momentum (3 months)
- Phase advancement is triggered by the Companion Agent at the end of each phase window
- The companion generates a phase-transition message acknowledging the milestone before starting the next phase

**Subscription gate:**
- Coaching features are gated behind an active Stripe subscription
- The API checks `subscriptions.status = 'active'` before serving coaching endpoints
- Stripe webhooks keep subscription state current (payment failure → grace period → downgrade)

---

## Token Efficiency

LLM cost is controlled by a strict separation: **code does analysis, LLM does language**.

### Pre-processing (Python, zero LLM cost)

Before any LLM call, transactions read from the Financial Record Store (via MCP) are aggregated in Python into a compact `SpendingContext`:

```json
{
  "period": "2024-04-15 → 2024-04-22",
  "highlights": [
    {"type": "category_delta", "category": "dining", "amount": 95, "baseline_4wk": 60, "delta_pct": 58},
    {"type": "streak", "label": "cooked at home", "days": 5},
    {"type": "new_recurring", "merchant": "Adobe", "amount": 12},
    {"type": "large_charge", "merchant": "Amazon", "amount": 140}
  ],
  "total_spend": 380,
  "baseline_total": 310,
  "coaching_phase": "building",
  "phase_target": "reduce dining spend by 20%"
}
```

This struct is < 250 tokens. The LLM never sees a raw transaction list.

### LLM usage

**Provider:** abstracted behind an `LLMClient` interface; selected by `LLM_PROVIDER` env var. Default is **DeepSeek** (low cost). Swappable to Claude Sonnet / Haiku with no code changes.

| Task | Default model | Approx. input tokens | Approx. output tokens |
|---|---|---|---|
| Inbound Q&A response | DeepSeek Chat | ~300 | ~100 |
| Bi-weekly coaching check-in | DeepSeek Chat | ~500 | ~120 |
| Weekly progress summary | DeepSeek Chat | ~400 | ~80 |
| Monthly analysis narrative | DeepSeek Chat (or Sonnet) | ~800 | ~300 |
| Dashboard annotations & actions | DeepSeek Chat | ~600 | ~250 |

**Dashboard annotations are cached.** They are generated once when new data syncs (a Plaid transaction sync or a statement upload), then served from cache on every dashboard view — not regenerated per view. A manual "refresh insights" action re-runs them on demand. See `design/product.md`.

**Prompt caching:** the system prompt (persona, tone rules, few-shot examples) is identical across all users — use provider caching where available.

**Target per coaching check-in:** < 600 input tokens + < 150 output tokens.

---

## Security & Trust

| Concern | Approach |
|---|---|
| Plaid access tokens | Encrypted with AWS KMS before DB write; decrypted only in Lambda memory |
| Stored finance records | Encrypted at rest in RDS; field-level minimization; retention windows; user-deletable |
| MCP data access | Read-only DB role; every query scoped to the caller's `user_id`; `run_sql` is SELECT-only with a statement timeout |
| Statement file uploads | Presigned S3 URLs with 15-minute expiry; bucket is private, no public access |
| Conversation & episodic memory | Encrypted at rest in RDS; consolidated then purged past the retention window; user-deletable |
| API keys (Plaid, LLM, Stripe) | Stored in AWS Secrets Manager; injected at Lambda cold start |
| User auth | Short-lived JWT (15 min) + secure keychain refresh token (30 days); invalidated on logout |
| Plaid webhooks | `Plaid-Verification` JWT header verified on every request |
| Stripe webhooks | Stripe-Signature HMAC header verified before processing |
| Network | Lambda inside VPC; RDS has no public endpoint; all traffic HTTPS |
| Push tokens | Stored per-device; rotated on re-login; deleted on logout |
| Audit log | Account linking, data access, subscription events → CloudWatch (90-day retention) |

---

## Companion Agent — Gentleness by Design

Tone safety is enforced at multiple layers:

**1. Neutral data format:** `SpendingContext` uses neutral field names (`delta_pct: 58`) rather than evaluative language (`overspent`). This prevents the LLM from inheriting alarm framing from the input.

**2. Coaching context is framed as encouragement:** the `coaching_phase` and `phase_target` fields in SpendingContext are written as forward-looking descriptions, not performance grades.

**3. System prompt constraints:**
- Persona: warm, curious, non-judgmental friend
- Hard rules: never use "budget", "exceeded", "alert", "warning", "you should"
- Always end with an optional question or gentle nudge — never a directive
- Few-shot examples anchored to the product voice in `design/product.md`

**4. Output safety check:** before sending, a lightweight Python scan checks for flagged phrases. If triggered, the agent re-prompts once with stricter instructions. If the second attempt also fails, it falls back to a minimal safe template message.

**5. Conversation continuity:** the last 10 turns are included in every prompt so the companion remembers context and doesn't repeat itself.

**6. Silence is respected:** if a coaching user hasn't engaged for two consecutive check-in periods, the companion's tone softens and cadence backs off by one level.

---

## Key Data Flows

### Onboarding

```
User opens app → registration screen
  → Creates account (email + password) → JWT issued
  → Selects finance profile and goal → stored in users table
  → Chooses account connection method:

  Path A (Plaid):
    → Plaid Link SDK opens natively in app
    → User authenticates with bank in Plaid's UI
    → Plaid returns public_token to app
    → App POSTs public_token to /api/plaid/exchange
    → API exchanges for access_token via Plaid
    → API encrypts access_token with KMS, stores in linked_accounts
    → API schedules EventBridge rules for this user

  Path B (Manual upload):
    → User exports statement from bank (PDF or CSV)
    → App requests presigned S3 URL from API
    → App uploads file directly to S3
    → S3 event triggers Statement Parser Lambda
    → Parser writes normalized records into the Financial Record Store
```

### Coaching check-in (bi-weekly)

```
EventBridge fires at user's check-in schedule
  → Invokes Companion Lambda with {user_id, trigger: "coaching_checkin"}
  → Lambda loads user context (profile, goal, coaching phase) via MCP
  → Queries transactions since last check-in via MCP
  → Python pre-processor → SpendingContext (includes coaching_phase + phase_target)
  → Recalls relevant prior context via MCP search_memory
  → Builds prompt (system + SpendingContext + coaching context + memory)
  → Calls LLM API
  → Output safety check
  → Stores the turn in memory_episodes
  → Sends push notification (APNs / FCM via SNS)
  → Updates last_checkin_at
```

### User Q&A (inbound chat)

```
User types message in the web chat (or the MCP connector)
  → Client POSTs to /api/chat {message_text}
  → API enqueues to SQS
  → Companion Lambda consumes SQS message
  → Queries transaction data via MCP + recalls context via MCP search_memory
  → Calls LLM API → response
  → Stores both turns in memory_episodes
  → Returns response via API (polled by client) OR push notification
```

### Monthly analysis

```
EventBridge fires on 1st of each month per user
  → Invokes Companion Lambda with {user_id, trigger: "monthly_analysis"}
  → Queries the full prior month of transactions from the store via MCP
  → Python analysis: anomaly detection, category deltas, new recurring charges
  → Builds SpendingContext for the full month
  → Calls LLM API with monthly analysis prompt → narrative text
  → Stores in monthly_analyses table
  → Sends push notification: "Your April summary is ready"
```

### Coaching subscription

```
User taps "Start Coaching" in app
  → App POSTs to /api/coaching/subscribe
  → API creates Stripe customer + subscription
  → Stripe processes payment → sends webhook to /api/stripe/webhook
  → API verifies Stripe-Signature, updates subscriptions table
  → API creates coaching_plans record (phase: foundation, phase_start_at: now)
  → API schedules per-user EventBridge rules for bi-weekly + weekly triggers
```

---

## AWS Services & Estimated Cost

| Service | Role | Early stage cost |
|---|---|---|
| Lambda | API + companion agent + statement parser | ~$0 (free tier) |
| API Gateway (HTTP API) | API routing | ~$1/month |
| RDS PostgreSQL t4g.micro | Primary database | ~$13/month |
| EventBridge Scheduler | Per-user check-in crons | ~$0 |
| SQS Standard | Async job queue | ~$0 |
| S3 | Statement uploads | ~$1/month |
| SNS Mobile Push | Push notifications (APNs / FCM) | ~$0 at early scale |
| AWS Secrets Manager | API key storage | ~$2/month |
| AWS KMS | Token encryption | ~$1/month |
| CloudWatch Logs | Monitoring + audit trail | ~$2/month |
| Stripe | Subscription billing | 0.5% + processing fees (on revenue) |

**Total infra: ~$20/month** at early stage. Scales linearly with users — main variable costs at scale are RDS (instance upgrade), LLM API calls, and SNS (negligible per notification).

---

## Key Design Decisions

**Why backend-first, with web before native mobile?**
The product's moat is the agent — the storage, search, memory, and companion logic — not a client UI. Building that behind an HTTP API and an MCP layer lets the same brain serve a web app and a Claude connector immediately, and a native app later. Native mobile (and its push notifications) is deferred until the companion experience is proven; see `design/phases.md`.

**Why in-app delivery instead of Signal / messaging platforms?**
External messaging platforms (Signal, Telegram) create a fragmented experience and significant ops overhead (running `signal-cli` as a sidecar, Telegram bot management, etc.). In-app push notifications give full control over the UX and avoid third-party platform dependencies at the cost of one additional integration (APNs + FCM via SNS).

**Why pre-process transactions before the LLM?**
Sending 30+ transactions to an LLM on every call would cost ~2,000 tokens. Pre-processing extracts the 3–5 meaningful observations in Python (free) and sends only those. The LLM's job is language, not arithmetic.

**Why store all finance records?**
Spending, income, and money movement are structured data, and the dashboard, chat drill-down, and semantic search all need queryable history — an in-memory-only approach cannot serve a 12-month cash-flow chart or an ad-hoc "show me everything like this" query. Records are persisted in the Financial Record Store and treated as sensitive: encrypted at rest, field-level minimized, retention-bounded, and user-deletable. The earlier "fetch-and-discard" design was dropped because it blocked the product.

**Why a separate Statement Parser Lambda?**
PDF/CSV processing is compute-heavy (especially PDF text extraction) and should not block the API response. The S3 → Lambda trigger pattern isolates file processing, gives it independent scaling, and means upload completion is never on the critical path for the user.

**Why Stripe for subscriptions?**
Stripe Billing handles the full subscription lifecycle (trials, renewals, failed payments, proration, cancellations) out of the box. The alternative — managing this state ourselves — is a significant surface area for bugs and edge cases.

**Why Lambda for the companion agent instead of a long-running worker?**
Check-ins and analyses are sparse events (once or twice a week per user). A long-running worker would idle 99% of the time. Lambda + EventBridge is zero-cost at idle, scales automatically, and each invocation is isolated — a failure for one user doesn't affect others.
