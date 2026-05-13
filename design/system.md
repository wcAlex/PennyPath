# PennyPath — System Design

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  User                                                               │
│   │  browser               Signal / Telegram / WhatsApp            │
│   │                              ▲  │                              │
└───┼──────────────────────────────┼──┼──────────────────────────────┘
    │                              │  │
    ▼                              │  ▼
┌──────────────┐           ┌───────────────────┐
│  Web App     │           │  Messaging        │
│  (Next.js)   │           │  Adapter Layer    │
│  S3+CloudFront│           │  (swappable)      │
└──────┬───────┘           └────────┬──────────┘
       │ HTTPS                      │ webhook / send
       ▼                            ▼
┌──────────────────────────────────────────────────────┐
│  Backend API  (FastAPI / Lambda + API Gateway)       │
│  - Auth, user mgmt, onboarding                       │
│  - Plaid token exchange + webhook ingestion          │
│  - Report queries                                    │
│  - Messaging webhook ingestion → SQS                 │
└───────────┬──────────────────────┬───────────────────┘
            │                      │
            ▼                      ▼
┌───────────────────┐    ┌─────────────────────────────┐
│  Data Layer       │    │  Companion Agent             │
│                   │    │  (Python Lambda)             │
│  PostgreSQL (RDS) │◄───│  - Scheduled: EventBridge   │
│  AWS KMS          │    │  - Inbound reply: SQS        │
│  Secrets Manager  │    │  - Plaid fetch → pre-process │
└───────────────────┘    │  - LLM (Claude) → message   │
                         │  - Send via messaging adapter│
                         └──────────────┬──────────────┘
                                        │
                                        ▼
                               ┌─────────────────┐
                               │  Plaid API      │
                               │  Claude API     │
                               └─────────────────┘
```

---

## Components

### Web Frontend

**Next.js** — static export deployed to **S3 + CloudFront**.

Pages:
- Signup / login
- Onboarding wizard (link accounts → set intention → connect messaging)
- Settings (check-in frequency, messaging preferences, account management)
- Dashboard (spending reports, conversation history)

Auth state is held in an `httpOnly` cookie (JWT). The Plaid Link widget runs entirely client-side via `plaid-link` JS — the frontend never sees the Plaid `access_token`, only the short-lived `public_token` which it immediately exchanges via the backend API.

---

### Backend API

**Python FastAPI** deployed as **AWS Lambda** via **Mangum** (ASGI adapter) behind **API Gateway (HTTP API)**.

Responsibilities:
- User registration, login, JWT issuance and refresh
- Plaid `public_token` → `access_token` exchange; store encrypted token in DB
- Receive and verify Plaid webhooks (`TRANSACTIONS` events) → enqueue sync job to SQS
- Receive and verify messaging webhooks (inbound user messages) → enqueue to SQS
- Serve report data and conversation history to the web dashboard
- User settings and preference management

The API is stateless; all state lives in PostgreSQL or the job queue.

---

### Data Layer

**PostgreSQL on RDS** (`t4g.micro`). Key tables:

| Table | Purpose |
|---|---|
| `users` | Account credentials, preferences, check-in cadence |
| `linked_accounts` | Plaid item + encrypted `access_token` per user |
| `intentions` | User's loose financial intentions (freetext) |
| `spending_snapshots` | Pre-aggregated spending summaries per check-in period |
| `conversation_history` | Last 30 days of companion message turns per user |
| `messaging_identifiers` | User's messaging channel and identifier (encrypted) |

**What is not stored:** raw Plaid transactions. They are fetched from Plaid at check-in time, processed in-memory into a `SpendingContext`, and discarded. Only the synthesized snapshot is persisted.

Plaid `access_token` values are encrypted before INSERT using a **KMS data key** and decrypted only inside Lambda memory at call time. They are never logged.

Conversation history auto-purges at 30 days via a nightly Lambda. Users can trigger immediate deletion from the web dashboard.

---

### Companion Agent

A Python Lambda with two trigger paths:

**Scheduled check-in** (primary):
- **EventBridge Scheduler** fires a per-user cron at the user's preferred check-in time
- One Lambda invocation per user per check-in

**Inbound reply** (reactive):
- User replies to a companion message via their messaging app
- Messaging provider delivers webhook to the API → enqueued to **SQS**
- Companion Lambda consumes the SQS message and responds

**Execution steps (both paths):**

```
1. Decrypt Plaid access_token from DB
2. Fetch transactions from Plaid (window: since last check-in)
3. Pre-process → SpendingContext (Python, no LLM)
4. Load last 5 conversation turns from DB
5. Build prompt: system prompt + SpendingContext + conversation history
6. Call Claude API → assistant message text
7. (Safety check) scan output for flagged phrases → re-prompt once if triggered
8. Send message via Messaging Adapter
9. Persist outbound + inbound turns to conversation_history
10. Write spending_snapshot to DB
```

---

### Messaging Adapter Layer

An abstract interface that decouples the companion agent from any specific messaging platform:

```python
class MessagingAdapter:
    def send(self, user_id: str, text: str) -> None: ...
    def parse_inbound(self, payload: dict) -> InboundMessage: ...
```

Concrete implementations: `SignalAdapter`, `TelegramAdapter`, `WhatsAppAdapter`.  
Active adapter selected by env var `MESSAGING_ADAPTER`.

Inbound messages arrive at `/webhooks/message` on the API, are verified by the platform's signature header, and are enqueued to SQS. The webhook endpoint responds `200 OK` immediately — all processing is async.

---

## Token Efficiency

LLM cost is controlled by a strict separation: **code does analysis, LLM does language**.

### Pre-processing (Python, zero LLM cost)

Before any LLM call, raw Plaid transactions are processed in Python to produce a compact `SpendingContext`:

```json
{
  "period": "2024-04-15 → 2024-04-16",
  "highlights": [
    {"type": "category_delta", "category": "eating_out",
     "amount": 45, "baseline_4wk": 30, "delta_pct": 50},
    {"type": "streak", "label": "cooked at home", "days": 5},
    {"type": "large_charge", "merchant": "Amazon", "amount": 89}
  ],
  "total_spend": 134,
  "baseline_total": 95
}
```

This struct is < 200 tokens. The LLM never sees a raw transaction list.

### LLM usage

| Task | Model | Approx. input tokens | Approx. output tokens |
|---|---|---|---|
| Inbound intent classification | Claude Haiku | ~100 | ~20 |
| Outbound check-in generation | Claude Sonnet | ~400–500 | ~80–120 |

**Prompt caching:** the system prompt (persona, tone rules, few-shot examples) is identical across all users and all turns. Anthropic's prompt caching is used so this portion is charged at cache-read rates after the first call.

**Target per check-in:** < 500 input tokens + < 150 output tokens.  
At $3 / 1M input tokens (Sonnet cache miss), 1,000 daily check-ins costs < $2/day.

---

## Security & Trust

| Concern | Approach |
|---|---|
| Plaid access tokens | Encrypted with AWS KMS before DB write; decrypted only in Lambda memory |
| Raw transaction data | Never persisted; processed in-memory and discarded |
| Conversation history | Encrypted at rest in RDS; auto-purged at 30 days; user-deletable |
| API keys (Plaid, Anthropic, messaging) | Stored in AWS Secrets Manager; injected at Lambda cold start |
| User auth | Short-lived JWT (15 min) + httpOnly refresh cookie (30 days); invalidated on logout |
| Plaid webhooks | `Plaid-Verification` JWT header verified on every request |
| Messaging webhooks | Platform-specific HMAC signature verified before enqueue |
| Network | Lambda inside VPC; RDS has no public endpoint; all traffic HTTPS |
| Audit log | Account linking, data access, message dispatch events → CloudWatch (90 day retention) |

The user's messaging identifier (phone number or chat ID) is stored encrypted in the DB. It is never included in logs or error messages.

---

## Companion Agent — Gentleness by Design

Tone safety is enforced at multiple layers, not just in copy guidelines:

**1. Neutral data format:** `SpendingContext` uses neutral field names (`delta_pct: 50`) rather than evaluative language (`overspent`). This prevents the LLM from inheriting alarm framing from the input.

**2. System prompt constraints:**
- Persona: warm, curious, non-judgmental friend
- Hard rules: never use "budget", "exceeded", "alert", "warning", "you should"
- Always end with an optional question (never a directive)
- Three few-shot examples anchored to the product voice from `design/product.md`

**3. Output safety check:** before sending, a lightweight Python scan checks the message for flagged phrases. If triggered, the agent re-prompts once with a stricter instruction. If the second attempt also fails, it falls back to a minimal safe template message.

**4. Conversation continuity:** the last 5 turns are included in every prompt so the companion remembers recent context and doesn't repeat itself.

**5. Silence is respected:** if the user hasn't replied to the last 3 check-ins, the companion's tone softens further ("just checking in — no need to reply") and the check-in frequency temporarily backs off by one level.

---

## Key Data Flows

### Onboarding

```
User → clicks "Link Account" on web
  → Plaid Link JS widget opens (client-side)
  → User authenticates with their bank in Plaid's UI
  → Plaid returns public_token to frontend
  → Frontend POSTs public_token to /api/plaid/exchange
  → API exchanges for access_token via Plaid API
  → API encrypts access_token with KMS, stores in linked_accounts
  → API creates EventBridge schedule for this user's check-in cadence
```

### Scheduled check-in

```
EventBridge fires at user's check-in time
  → Invokes companion Lambda with user_id
  → Lambda decrypts access_token
  → Fetches transactions from Plaid (since last check-in timestamp)
  → Python pre-processor → SpendingContext
  → Loads last 5 turns from conversation_history
  → Builds prompt (system + context + history)
  → Calls Claude Sonnet API (cached system prompt)
  → Output safety check
  → Sends message via MessagingAdapter
  → Stores turns + snapshot in DB
  → Updates last_checkin_at timestamp
```

### Inbound user reply

```
User replies in Signal/Telegram/WhatsApp
  → Platform POSTs webhook to /webhooks/message
  → API verifies signature
  → Enqueues to SQS {user_id, message_text, timestamp}
  → Companion Lambda consumes SQS message
  → Classifies intent via Claude Haiku (< 20 tokens output)
  → If "pause": updates user preference, sends ack, exits
  → If "query" or "conversation": builds context, calls Sonnet, responds
  → Stores turns in DB
```

---

## AWS Services & Estimated Cost

| Service | Role | Early stage cost |
|---|---|---|
| Lambda | API + companion agent | ~$0 (free tier covers early scale) |
| API Gateway (HTTP API) | API routing | ~$1/month |
| RDS PostgreSQL t4g.micro | Primary database | ~$13/month |
| EventBridge Scheduler | Per-user check-in crons | ~$0 |
| SQS Standard | Async job queue | ~$0 |
| S3 + CloudFront | Frontend hosting + CDN | ~$1/month |
| AWS Secrets Manager | API key storage | ~$2/month (5 secrets) |
| AWS KMS | Token encryption | ~$1/month |
| CloudWatch Logs | Monitoring + audit trail | ~$2/month |

**Total infra: ~$20/month** at early stage. Scales linearly with users — the largest variable cost at scale is RDS (upgrade instance) and LLM API calls.

---

## Key Design Decisions

**Why no raw transaction storage?**  
Storing Plaid transactions creates a large liability: sensitive financial data at rest, growing indefinitely. Fetching on demand and processing in-memory eliminates this. The `spending_snapshot` (aggregated totals) is sufficient for reports and has no PII value.

**Why pre-process transactions before the LLM?**  
Sending 30 transactions to an LLM on every check-in would cost ~2,000 tokens per call. Pre-processing extracts the 3–5 meaningful observations in Python (free) and sends only those. The LLM's job is language, not arithmetic.

**Why an abstract messaging adapter?**  
Signal's programmatic API requires running `signal-cli` as a sidecar service — significant ops overhead for a pre-launch product. Abstracting the messaging layer lets us launch on Telegram (easy API, good developer experience), validate the product, and switch to Signal later without touching the companion agent logic.

**Why Lambda for the companion agent instead of a long-running worker?**  
Check-ins are sparse (once per user per day). A long-running worker would idle 99% of the time. Lambda + EventBridge is zero-cost at idle, scales automatically, and each invocation is isolated — a failure for one user doesn't affect others.
