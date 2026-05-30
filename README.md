# PennyPath

A personal finance companion that checks in with you daily in a warm, non-judgmental way — not a budgeting app, a buddy.

Phase 1 is a local agent you run manually against your own accounts to iterate on the companion experience before adding multi-user support or cloud infrastructure.

---

## Setup

**Requirements:** Python 3.11+

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root (copy from `.env.example` if it exists):

```bash
# LLM provider (DeepSeek by default — cheap and good enough for Phase 1)
LLM_API_KEY=your-deepseek-api-key
LLM_BASE_URL=https://api.deepseek.com   # default, can omit
LLM_MODEL=deepseek-chat                 # default, can omit

# Plaid (only needed if you're using --plaid mode)
PLAID_CLIENT_ID=your-client-id
PLAID_SECRET=your-secret
PLAID_ACCESS_TOKEN=your-access-token
```

Get a DeepSeek API key at [platform.deepseek.com](https://platform.deepseek.com). The cost per check-in is fractions of a cent.

---

## Transaction sources

The agent supports two ways to load transactions — you can use either or both:

### Option A: Local bank statements (easiest to start)

Drop files into `data/statements/`. Supported formats:

**CSV** — most reliable. Each file must have these columns:

```
date,amount,description,category,account_type
2024-04-15,28.50,Chipotle,dining,credit
2024-04-14,12.00,Spotify,subscriptions,credit
2024-04-13,1850.00,Rent,housing,checking
```

- `account_type` must be `checking` or `credit`
- `date` accepts most common formats (`2024-04-15`, `04/15/2024`, `Apr 15, 2024`, etc.)
- `amount` is positive for spending, negative for income/refunds
- `category` is freeform — use whatever labels make sense to you

Export a CSV from your bank's website, add the required columns, and drop it in `data/statements/`.

**PDF** — the agent sends the PDF text to the LLM for extraction. Works on most structured bank statements. If a PDF can't be parsed, the dashboard will show a warning with CSV export instructions for your specific bank.

How to export a CSV from common banks:
- **Chase** → Accounts → Download → CSV → select date range
- **Citi** → View Statements → Download Activity CSV
- **Discover** → Manage → Download All Transactions → CSV
- **BofA** → Download → Microsoft Excel Format (CSV)

Transactions are deduplicated by `(date, amount, description)` across all files and cached in `data/transactions.db`. Re-ingestion only re-parses files that have changed on disk.

### Option B: Plaid (live account data)

Plaid gives the agent read-only access to your real checking and credit card transactions. You need three env vars: `PLAID_CLIENT_ID`, `PLAID_SECRET`, and `PLAID_ACCESS_TOKEN`.

To get a `PLAID_ACCESS_TOKEN` for your real accounts:
1. Sign up at [plaid.com/developers](https://plaid.com/developers/) — the development tier is free for personal use
2. Use [Plaid Quickstart](https://github.com/plaid/quickstart) to link your account and get an access token
3. Copy the token into your `.env`

The client currently connects to Plaid Sandbox. To use real accounts, change `plaid.Environment.Sandbox` to `plaid.Environment.Development` in `src/plaid_client.py`.

---

## User preferences

Complete onboarding at `http://127.0.0.1:8080` when you first run the web interface, or edit `data/config.json` directly:

```json
{
  "name": "Alex",
  "finance_profile": "early_career",
  "goal_type": "emergency_fund",
  "goal_label": "Emergency fund",
  "goal_monthly_target": 500,
  "intentions": ["spend less on eating out", "track subscriptions"],
  "onboarding_complete": true
}
```

Finance profiles: `early_career`, `growing_family`, `paying_debt`, `building_wealth`

Goal types: `emergency_fund`, `reduce_spending`, `save_purchase`, `get_out_of_debt`

This file is gitignored — your preferences never leave your machine.

---

## Web chat interface (recommended)

A browser-based interface with chat, dashboard, and onboarding wizard.

```bash
python -m src.web_chat
```

Then open **http://127.0.0.1:8080** in your browser.

**First run:** you'll be taken through a 4-step onboarding wizard (name → finance profile → goal → connect accounts). After completing it, you land on the dashboard.

**Chat** (`/chat`):
- Type a question and press Enter (Shift+Enter for a newline)
- Click 📎 to attach an image — the LLM will reason about it alongside your transaction data
- The companion's response renders as formatted markdown: tables, bold, lists, code blocks
- **New Chat** clears the conversation and triggers a wiki update (the companion learns from your session)

**Dashboard** (`/dashboard`):
- Spending by category (bar chart) and monthly trend (line chart)
- Goal progress
- Monthly narrative analysis on demand
- **Statement Issues** warning card — shown if any PDFs could not be parsed, with per-bank CSV export instructions

**Note on image input:** image analysis requires a vision-capable model. Set `LLM_MODEL` in your `.env` to a vision model (e.g. `deepseek-vl2` for DeepSeek, or a Claude 3 model with `LLM_BASE_URL=https://api.anthropic.com/v1`). Text-only questions work with any model.

To use a different port: `WEB_CHAT_PORT=9000 python -m src.web_chat`

---

## How the companion remembers you

After each conversation session, the companion updates `data/user_wiki.md` — a short markdown document that captures what it has learned about you: your active concerns, spending patterns, preferences, and resolved issues.

This wiki is injected into every LLM call, so the companion always has context about you without you having to repeat yourself. You can read and edit it directly at any time.

A new session starts automatically after 30 minutes of inactivity, or when you click **New Chat**. Either event triggers a wiki update.

---

## CLI commands

All commands are run from the project root with the virtualenv active.

### Daily check-in (one-shot)

```bash
# From local bank statements
python -m src.cli checkin --local

# From Plaid
python -m src.cli checkin
```

### Interactive chat

```bash
# From local bank statements
python -m src.cli chat --local

# From Plaid
python -m src.cli chat
```

Type `quit` or `exit` (or Ctrl+C) to leave. Conversation history is saved to `data/memory.json` and picked up next time.

#### Sample conversation

```
> how am I doing this month?
Pretty solid, actually. You're down about 20% on dining compared to last month —
looks like the cooking-at-home streak is holding up. Subscriptions crept up a bit
though. Want me to list them?

> yeah show me the subscriptions
Looks like Spotify ($9.99), Netflix ($15.49), and a $12 charge from Adobe that
showed up last week. The Adobe one is newer — is that intentional?

> that was a one-time trial, it shouldn't recur
Good to know — worth keeping an eye on it next month to make sure it doesn't.

> this weekend I went a bit overboard on going out
These things happen. Looks like about $180 between Friday and Sunday — restaurants
and a couple of rideshares. Is there anything you'd want to do differently next
week, or are you good with it?

> I'm good, just wanted to flag it
Totally fair. One week doesn't define anything. I'll just keep watching the trend.
```

#### Things to ask the companion

**Spending overview**
- `how am I doing this month?`
- `give me a summary of this week`
- `what did I spend on dining?`
- `what are my biggest expenses lately?`

**Digging into a category**
- `how much did I spend on subscriptions?`
- `how does my grocery spending compare to last month?`
- `show me any charges over $100`

**Setting and tracking goals**
- `set goal emergency fund 500`
- `my goal is to save $300 a month`
- `show goals`
- `how am I tracking against my savings goal?`

**Reflection and habits**
- `I've been eating out a lot this week — is that unusual for me?`
- `what's one thing I could do differently this month?`
- `I want to spend less on coffee — any patterns you notice?`

**When things go sideways**
- `I've been terrible with money this month`
- `I had a really expensive week, feeling bad about it`
- `can we just reset and start fresh from here?`

---

## Data files

All data lives in `data/` and is gitignored.

| File | Purpose |
|---|---|
| `data/config.json` | Your profile, goal, and preferences |
| `data/transactions.db` | SQLite — all parsed transactions + parse state per file |
| `data/user_wiki.md` | Companion's living memory — updated each session |
| `data/memory.json` | Conversation history (last 50 turns) |
| `data/snapshots.json` | Pre-computed monthly spending aggregates |
| `data/statements/` | Drop your bank statement CSV/PDF files here |

---

## Project structure

```
src/
  cli.py                # CLI entrypoint — all commands live here
  companion.py          # chat session logic, intent detection, session tracking
  llm_orchestrator.py   # builds prompts and calls the LLM
  statement_ingester.py # parse CSV/PDF statements; write to SQLite
  plaid_client.py       # fetch transactions from Plaid
  models.py             # Transaction dataclass
  storage.py            # file/DB stores: UserConfig, Transactions, Wiki, Conversation, Snapshots
  wiki_updater.py       # LLM call to update user_wiki.md at session end
  prefs.py              # legacy prefs loader
  prompts/
    companion.txt        # system prompt — edit this to tune the companion's voice
    monthly_analysis.txt # prompt for monthly narrative
    wiki_update.txt      # prompt for wiki maintenance
  templates/
    chat.html
    dashboard.html
    onboarding.html
data/
  statements/           # drop bank statement CSVs/PDFs here (gitignored)
  config.json           # your profile (gitignored, created on onboarding)
  transactions.db       # SQLite transaction store (gitignored, created automatically)
  user_wiki.md          # companion memory (gitignored, created on first session)
  memory.json           # conversation history (gitignored, created automatically)
design/
  product.md            # product vision and feature details
  system.md             # full system architecture
  phases.md             # phase breakdown
  context_system.md     # information architecture design
```

---

## Tuning the companion voice

The companion's personality is defined in `src/prompts/companion.txt`. If a check-in message doesn't feel right, edit that file and re-run — no code changes needed.

Things worth experimenting with:
- How it frames spending deltas (curiosity vs. concern)
- How explicitly it references your goals
- The length and tone of the closing nudge
- How it handles weeks with no notable activity

The wiki update behavior is controlled by `src/prompts/wiki_update.txt`.
