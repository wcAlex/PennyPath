# PennyPath — Development Phases

Each phase delivers a complete product experience for one feature area, then hardens it before
moving on. Within each phase, work progresses through the steps below so each loop stays short
and testable.

## Steps within every phase

| Step | What happens | Infrastructure |
|---|---|---|
| **1. Local** | Build the feature as a CLI / local script; iterate fast on quality | Local files / SQLite, no cloud |
| **2. Code analysis** | Review error handling, edge cases, prompt quality, test coverage | Static review + unit tests |
| **3. Self-experiment** | Run it with your own real data for 1–2 weeks; iterate until it feels genuinely useful | Local + real data |
| **4. Tryout** | Deploy minimally; invite 3–10 people; gather real feedback | Light cloud or Docker |
| **5. Production ready** | Security hardening, monitoring, full cloud | AWS full stack |

---

## Product shape

PennyPath is **one product, not a tiered program**. The core experience is: ingest a user's
financial data, surface insight on an annotated dashboard, let the user customize that dashboard
through chat (pinning generated charts), and use the chat to discuss what to do next. There is no
separate coaching subscription — the companion *is* the product.

The feature surface grows over time: **spending analysis → payment / bill reminders → purchase
warnings → cost-reduction recommendations**. Earlier phases earn the trust and data access that
later phases depend on.

### Why statement upload first, Plaid later

Plaid production access carries a heavy compliance, review, and cost burden, and — more
importantly — asking a brand-new user to connect their bank on day one is the wrong trust ask.
We start with **manual statement upload (PDF / CSV)** so we can prove the core product without
that gate. Plaid arrives in Phase D, once we have customer traction and trust, and unlocks the
real-time features (reminders, warnings) that justify a paid tier.

---

## Phase A — Local Statement Companion

**Goal:** A user can upload a bank or credit-card statement and get the full companion
experience — annotated dashboard plus Q&A chat with custom chart generation — end to end on
their own machine.

**In scope:**
- **User profile + goal shaping** — the user picks a life-stage profile (early career,
  growing family, paying down debt, building wealth, or custom) and a starting intention. The
  companion then helps shape that into one or more **workable, multi-timeframe, category-scoped
  goals** grounded in the user's actual spending history — e.g. "~$40k/year on ski and travel"
  or "~$20k/year on clothes and gear" alongside monthly intentions for steadier categories. The
  companion proposes numbers from the user's own data; the user accepts, edits, or rejects.
  Goals are loose intentions (no hard targets) and are editable anytime. Progress against each
  intention is visible on the dashboard, and the companion can surface gentle observations
  ("you're at 80% of your ski intention with two months left"). Profile + goals shape the
  companion's tone, the dashboard's emphasis, and the chat. This is the user's initiative —
  everything else reacts to it.
- **Statement ingestion** (PDF / CSV) into a unified `transactions` store with magnitude-only
  amounts and a closed-enum `section_type`. See `design/storage.md` for the contract.
- **Internal-transfer pairing** so spending/income totals reflect real money in and out.
- **Four standard charts** — Spending (category donut + breakdown), Income (donut + 12-month
  history), Transactions (filterable list), Cash Flow (12-month income vs. spending).
- **LLM annotation layer** — each chart carries a short, warm annotation and, where useful, a
  gentle suggested action; cached and refreshable.
- **Q&A chat** over the user's real data, answered through typed query tools and hybrid
  semantic search.
- **Custom chart generation from chat** + **pin-to-dashboard** so the dashboard becomes
  personalized to what each user actually cares about.
- **User context / memory store** — working memory + episodic memory with an LLM-driven
  consolidation job that compacts old history while keeping recent detail.
- **Web UI** (local) for the dashboard and chat. The agent stays surface-agnostic.
- CLI commands to exercise ingest and every agent tool.

**Local stage:** SQLite (`src/storage.py`) with `sqlite-vec` or an embedding table for vectors.
Cloud Postgres + `pgvector` is Phase B.

**Out of scope:** Plaid, payment reminders, purchase warnings, recommendations, native mobile,
auth, multi-user, cloud.

**Success bar:** You upload a statement, see four annotated charts that correctly exclude
internal transfers, ask a follow-up in chat, pin a generated chart, and the dashboard remembers
it on the next run.

---

## Phase B — Cloud Deploy

**Goal:** Make the Phase A experience available to real users on the open web. This is the
first public launch, scoped to statement upload only.

**In scope:**
- PostgreSQL + `pgvector` (RDS) replacing local SQLite; Alembic migrations.
- FastAPI backend (`api/` alongside `src/`) + Mangum on Lambda + API Gateway.
- JWT auth (email + password registration).
- Web upload form for PDF / CSV statements; files in a private S3 bucket with presigned URLs.
- Real (but minimal) production posture: HTTPS, basic CloudWatch monitoring, error alerting,
  account deletion flow.
- Public invite/signup flow.

**Folder structure adds:**
```
api/
  main.py          # FastAPI app + Mangum adapter
  auth.py          # JWT registration + login
  upload.py        # statement upload + ingestion trigger
  chat.py          # conversation endpoints
  dashboard.py     # chart data + annotations
db/
  models.py        # SQLAlchemy models
  migrations/      # Alembic migrations
web/
  app/             # dashboard + chat web UI
src/               # unchanged — reused by the API
```

**Out of scope:** Plaid, mobile, advanced onboarding, payment reminders, recommendations.

**Success bar:** A user can register on the open web, upload a statement, see their annotated
dashboard, chat with the companion, and says "I'd actually use this."

---

## Phase C — Easy Onboarding (+ Mobile if traction)

**Goal:** Make the path from "I have a statement somewhere" to "I see my annotated dashboard"
frictionless. Statement upload is the friction point — solve it. Build a native mobile surface
only if usage and demand justify it.

**In scope:**
- **Per-bank statement helpers** — deep links and short instructions for the major U.S. banks
  on where to download a PDF/CSV statement.
- **Email-forwarding ingest** — a user-scoped address; forward your statement email, we parse
  and load.
- **Drag-and-drop multi-file upload** with a preview of detected accounts and transactions
  before commit, so the user can sanity-check before anything lands in their dashboard.
- **Friendlier first run** — profile and goal selection inline with the first upload, not as
  a separate wizard stage.
- **Mobile app (demand-gated)** — React Native; primary value is on-the-go photo capture of a
  statement (camera → OCR → ingest) and viewing the dashboard. Push notifications are deferred
  to Phase D, when there is real-time data worth pushing about.

**Folder structure adds (if mobile is built):**
```
mobile/
  app/             # screens (home, chat, settings, onboarding)
  components/
  lib/
    api.ts         # typed API client
    auth.ts       # JWT keychain helpers
```

**Out of scope:** Plaid, payment reminders, purchase warnings, recommendations.

**Success bar:** A new user gets from zero to an annotated dashboard in under five minutes
without instructions; if mobile shipped, they can do it from their phone.

---

## Phase D — Plaid + Proactive Account Features (paid tier)

**Goal:** Once we have traction and trust, integrate Plaid for real-time data and use that
signal to ship proactive features users will pay for.

**In scope:**

*Plaid + account analysis:*
- Plaid Link (read-only) — live balances and automatic transaction sync.
- Account analysis — balances across linked accounts, low-balance projections, recurring-charge
  inventory.

*Proactive features:*
- **Payment / bill reminders** — detect recurring bills and alert before due date (push on
  mobile, email/web otherwise).
- **Purchase warnings** — flag charges that are unusually large for a category, or that would
  push an account below a user-set threshold. Framed as gentle observations, not alarms.

*Paid tier:*
- Plaid-powered proactive features sit behind a subscription. The Phase A/B/C base experience
  (statement upload + dashboard + chat) remains free.
- Stripe integration; cancellation flow that downgrades cleanly back to the free base.

*Trust hardening required for Plaid prod access:*
- KMS encryption for Plaid access tokens.
- Secrets Manager for all third-party API keys.
- Lambda in VPC; RDS no public endpoint.
- Webhook signature verification (Plaid `Plaid-Verification`, Stripe `Stripe-Signature`).
- Audit log of account-linking and data-access events.
- Account deletion: wipes all user data, memory, and Plaid item links within 30 seconds.
- Privacy policy, terms of service, GDPR / CCPA data-rights flow.
- Independent security review pass before public Plaid launch.

**Out of scope:** Recommendation engine (Phase E).

**Success bar:** A subscribed user links a real account via Plaid, receives at least one
accurate bill reminder and one helpful purchase warning per month, and finds them useful
enough not to mute. An independent security review finds no critical or high issues.

---

## Phase E — Recommendation Engine

**Goal:** Move from "tell me what happened" to "here's what you could change." The companion
suggests concrete spending changes that reduce cost without hurting quality of life.

**In scope:**
- **Subscription audit** — duplicates, dormant subscriptions, plausible downgrades.
- **Category-level recommendations** grounded in the user's actual data ("you spent $X on
  ride-share this month; here's what a mixed transit/rideshare pattern would look like").
- **Quality-of-life modeling** — every recommendation explicitly preserves a user-declared set
  of things they value, so we never suggest cutting something they care about.
- **Optional "try this for two weeks" experiments** — companion-driven, with before/after
  observation, framed as gentle nudges, not prescriptions.

**Out of scope:** Hard budgets, score-based feedback, anything that violates the no-shame
product tone.

**Success bar:** Users accept and act on at least one recommendation per month on average,
and report that the recommendations feel respectful of their preferences and lifestyle.

---

## Working with Claude across phases

### How Claude knows which phase you're in

Update `CLAUDE.md` at the start of each phase:

```
## Current Phase: A — Local Statement Companion
In scope: local CLI + local web, real PDF/CSV statement data, dashboard + chat + custom
chart pinning. No Plaid, no cloud, no payment reminders.
```

Open each session with a one-line scope reminder:
> "Phase A. I want to improve the dashboard annotations — they feel generic right now."

### Advancing a phase

1. Update `## Current Phase` in `CLAUDE.md`.
2. Open the next session with: *"Moving to Phase B (Cloud Deploy). Here's what that adds…"*

### Preventing scope creep

> "Keep it Phase A — statement upload and local dashboard only, no Plaid or reminders yet."

### Readiness check before advancing

> "Before we move from A to B, review src/ and tell me what's solid and what still needs work
>  in the local companion."
