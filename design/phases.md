# PennyPath — Development Phases

Each phase has a clear scope boundary. The goal is to validate the core product value before adding infrastructure complexity.

---

## Phase 1 — Local Agent (current)

**Goal:** Validate the companion experience end-to-end with a single real user (the developer's own Plaid account). Iterate on the quality of spending insights, message tone, and habit coaching scenarios until the experience feels genuinely useful and human.

**In scope:**
- Plaid connection to one hardcoded bank account
- Transaction fetching and Python-based pre-processing (SpendingContext)
- LLM prompt design and companion message generation (Claude API)
- Conversation memory via a local JSON file
- Message delivery to console output (verify tone and content before wiring to a real chat channel)
- Optionally: wire to one messaging channel (Telegram bot is easiest) for real feel

**Out of scope:**
- Multi-user support, auth, or user management
- Web UI or dashboard
- Database (use local files)
- AWS or any cloud infrastructure
- Background scheduling (run manually via CLI)

**What success looks like:**  
You run `python agent/main.py` and get a check-in message that feels genuinely warm, contextually accurate, and useful. You'd want to receive it daily.

**Folder structure:**
```
agent/
  main.py           # CLI entrypoint — runs one check-in cycle
  plaid_client.py   # fetch transactions for a date window
  preprocessor.py   # transactions → SpendingContext JSON
  companion.py      # SpendingContext + history → LLM message
  messaging.py      # output channel (Phase 1: print to console)
  memory.py         # read/write conversation history (local JSON)
.env                # PLAID_CLIENT_ID, PLAID_SECRET, ANTHROPIC_API_KEY
requirements.txt
```

---

## Phase 2 — Multi-User (next)

**Goal:** Generalize Phase 1 to support multiple users. Add auth, a real database, and a web onboarding flow so other people can sign up and link their own accounts.

**Adds:**
- PostgreSQL database (local Docker for dev)
- FastAPI backend (`api/` folder alongside `agent/`)
- User registration, login, JWT auth
- Plaid Link flow (web-based account linking)
- Web onboarding UI (Next.js)
- Scheduled check-ins (cron job or simple scheduler, still local)
- Encrypted storage for Plaid tokens

**Still out of scope:**
- AWS / cloud deployment
- Horizontal scaling
- Production monitoring

**Migration from Phase 1:**  
`agent/` code stays largely intact. `memory.py` swaps file I/O for DB queries. `plaid_client.py` gains a `user_id` parameter. No rewrites, just generalization.

---

## Phase 3 — Distributed Architecture

**Goal:** Decouple the system into independently deployable services in preparation for cloud deployment. Introduce async job processing and a messaging adapter layer.

**Adds:**
- SQS / message queue for async companion jobs
- Abstract messaging adapter (`MessagingAdapter` interface + concrete implementations)
- Containerize all services (Docker Compose for local dev)
- Secrets management (move from `.env` to AWS Secrets Manager or local equivalent)
- Webhook ingestion for inbound messages
- Integration tests across services

**Migration from Phase 2:**  
API and agent decouple — agent no longer runs in-process with the API. Message delivery moves from direct function call to queue-based dispatch.

---

## Phase 4 — AWS Rollout

**Goal:** Deploy to production on AWS. Onboard real customers.

**Adds:**
- Lambda deployment (API via Mangum, companion agent as separate Lambda)
- RDS PostgreSQL (replace local Docker Postgres)
- EventBridge Scheduler (replace local cron)
- S3 + CloudFront (replace local Next.js dev server)
- AWS KMS for Plaid token encryption
- CloudWatch logging and alerting
- CI/CD pipeline (GitHub Actions)
- Custom domain, TLS

See `design/system.md` for the full AWS architecture.

---

## Working with Claude across phases

### How Claude knows which phase you're in

Claude reads `CLAUDE.md` automatically at the start of every session — that's where the current phase is declared. The most effective way to keep Claude focused is:

1. **CLAUDE.md** — always reflects the active phase (update it when you advance)
2. **Your opening message** — a one-line reminder: *"Phase 1, local agent only"* immediately scopes the session

### Effective conversation patterns

**Starting a focused session:**
> "Phase 1. I want to improve streak detection in preprocessor.py — right now it only looks at eating_out, I want it to work for any category."

**Preventing scope creep:**
> "Keep it Phase 1 — no DB, no auth, local files only."

**Asking for a Phase 1 → Phase 2 readiness review:**
> "Before we move to Phase 2, look at the agent/ folder and tell me what needs to change, what can be reused as-is, and what we should refactor first."

**Advancing a phase:**
1. Update the `## Current Phase` section in `CLAUDE.md`
2. Open your next session with: *"We're starting Phase 2 now. Phase 1 agent code is in agent/. Here's what Phase 2 adds..."*

### What to resist

- Don't let Claude add multi-user logic "just in case" during Phase 1 — it adds complexity you'll change anyway
- Don't wire up AWS during Phase 1 — a local script that works well is worth more than a deployed script that works poorly
- Don't move to Phase 2 until the companion experience genuinely feels good — that's the whole point of Phase 1
