# PennyPath — Information Architecture

**Decisions:** LLM-based PDF extraction · SQLite transaction store · Living user wiki

---

## What Is Broken and Why It Matters

### 1. PDF Parsing Fails Silently

`statement_ingester._parse_pdf()` applies a single regex expecting: date · amount · description · category · account_type on one space-delimited line. Real bank PDFs (Chase, Citi, Discover, BofA) don't produce that layout — they use multi-column table rendering with page headers, running balances, and bank-specific date columns.

**Effect:** 25 PDF files in `data/statements/` contribute exactly zero transactions. The dashboard, check-ins, and all LLM responses are based on no data, silently.

### 2. Full Re-parse on Every Request

`ingest_statements()` is called from three places in `web_chat.py` (dashboard, monthly analysis, every chat message). Each call reads and parses every file from disk. With 25 PDFs that take 2–3 seconds each when fixed, this is 75 seconds of I/O per dashboard load. Nothing is persisted; deduplication only works within a single call.

**Effect:** Unusable latency when PDF parsing is fixed. Inconsistent deduplication. Dashboard can't display data efficiently.

### 3. Conversation Has No Memory Structure

`ConversationStore` stores a flat list of `{role, content}` with no timestamps, no session boundaries, no topic tagging. The hard cap is 50 turns. A concern raised in January is gone by March unless the user brings it up again within the active window.

**Effect:** The companion treats every session like a first meeting. Cannot track resolved concerns or observe patterns over time.

### 4. Agent Never Evolves

User config (`data/config.json`) has static fields set at onboarding. There is no mechanism for the companion to accumulate observations about the user — spending habits, stated vs. actual behavior, changing priorities, things the user no longer cares about.

**Effect:** Every check-in is generic. The companion cannot say "I noticed your dining spending dropped — last month you mentioned wanting to cut back; looks like it worked."

---

## Chosen Architecture

### Transaction Layer: SQLite

`data/transactions.db` — stdlib `sqlite3`, no server, one file. Two tables:

**`transactions`**

```sql
CREATE TABLE transactions (
    id           TEXT PRIMARY KEY,    -- 12-char MD5(date+amount+description)
    date         TEXT NOT NULL,       -- YYYY-MM-DD
    amount       REAL NOT NULL,       -- positive = spend, negative = income/refund
    description  TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT '',
    account_type TEXT NOT NULL,       -- 'checking' | 'credit'
    source_file  TEXT NOT NULL,       -- basename of originating file
    ingested_at  TEXT NOT NULL        -- ISO 8601 timestamp
);

CREATE INDEX idx_tx_date ON transactions(date);
CREATE INDEX idx_tx_category ON transactions(category);
```

**`file_sources`** (tracks parse state per file — replaces mtime cache)

```sql
CREATE TABLE file_sources (
    filename      TEXT PRIMARY KEY,   -- basename
    filepath      TEXT NOT NULL,
    file_mtime    REAL NOT NULL,      -- os.path.getmtime()
    parse_method  TEXT NOT NULL,      -- 'csv' | 'llm' | 'pdfplumber'
    tx_count      INTEGER NOT NULL,
    parse_error   TEXT,               -- NULL if successful
    parsed_at     TEXT NOT NULL
);
```

**Dashboard queries (examples):**
```sql
-- Category totals for current month
SELECT category, SUM(amount) AS total
FROM transactions
WHERE date BETWEEN '2026-05-01' AND '2026-05-31'
GROUP BY category ORDER BY total DESC;

-- All transactions over $100
SELECT * FROM transactions WHERE amount > 100 ORDER BY date DESC;

-- Month-over-month trend (last 6 months)
SELECT substr(date,1,7) AS month, SUM(amount) AS total
FROM transactions
GROUP BY month ORDER BY month DESC LIMIT 6;
```

**Migration to Postgres (Phase 1.4):** Schema is identical. Replace `sqlite3` connection with `psycopg2`. No changes to query logic.

---

### PDF Parsing: LLM Extraction

**Flow for a PDF file:**

```
_parse_pdf_v2(path)
    │
    ├── pdfminer.extract_text(path) → raw_text
    │
    ├── Split into chunks ≤ 3000 chars (to stay within token budget)
    │
    ├── For each chunk:
    │   └── LLM call with extraction prompt (see below)
    │       ├── Response is valid JSON array? → parse rows
    │       └── Invalid JSON? → retry once, then skip chunk
    │
    ├── Validate each row: date parseable? amount numeric? description non-empty?
    │
    ├── If total extracted < 2 rows:
    │   └── Return parse_error: "Could not extract transactions from PDF.
    │         Export a CSV from your bank instead:
    │         Chase → Accounts → Download → CSV
    │         Citi → View Statements → Download CSV
    │         Discover → Manage → Download All Transactions → CSV
    │         BofA → Download → Microsoft Excel Format (CSV)"
    │
    └── Return List[Transaction]
```

**LLM extraction prompt (injected per chunk):**

```
Extract every transaction from this bank statement text.
Return ONLY a valid JSON array — no explanation, no markdown, no other text.

Each element must have exactly these fields:
  date: "YYYY-MM-DD"
  amount: 12.34   (positive for charges/debits, negative for income/refunds)
  description: "Merchant name or payee"
  category: "best guess category, or empty string"

If you cannot find transactions in this text, return an empty array: []

Statement text:
{chunk}
```

**Why this works:** The LLM handles Chase's "[Date] [Description] [Amount]" layout, Citi's split debit/credit columns, Discover's transaction-date vs. post-date columns, and narrative summaries in bank letters — without any bank-specific code.

**Cost:** DeepSeek at ~$0.14/1M input tokens. A 20-page PDF is ~15,000 tokens. Cost per statement: ~$0.002.

---

### Living User Wiki: Karpathy Pattern

`data/user_wiki.md` — a ~500 token markdown document maintained by the LLM. Always loaded as part of the system context before every LLM call. Updated at session end with one additional LLM call.

**Wiki structure (enforced by the update prompt):**

```markdown
## Identity
[Name]. Finance profile: [type]. Using PennyPath since [YYYY-MM].

## Goal
[Goal label]. Monthly target: $[N].
Signal: [on track | behind | ahead] — [one-sentence evidence].

## Active Concerns
- [Concern 1]: [one-sentence description with context]
- [Concern 2]: [one-sentence description with context]
- [Concern 3 max]: [one-sentence description with context]

## Observed Patterns
- [Pattern 1: specific recurring behavior with evidence]
- [Pattern 2]
- [Pattern 3]
- [Pattern 4 max]

## Preferences
- [Communication preference or sensitivity]
- [Another preference]
- [One more max]

## Resolved
- [YYYY-MM] [Topic]: [one-line resolution]
- [YYYY-MM] [Topic]: [one-line resolution]
```

**Example wiki:**

```markdown
## Identity
Chi. Finance profile: paying down debt. Using PennyPath since 2026-01.

## Goal
Get out of debt. Monthly target: $4000 toward debt reduction.
Signal: on track — total spend has been under $3200/month for the last 2 months.

## Active Concerns
- Dining spending: was $140-160/month Jan-Feb; Chi wants to reduce it. Dropped to ~$90 in April.
- Subscription audit: asked about subscriptions in April, found 2 unused. Cancellation unconfirmed.

## Observed Patterns
- Groceries consistent at $250/month (Trader Joe's + Whole Foods split)
- Large one-time charges cluster at month start (rent, utilities)
- Dining is evening-heavy (Uber Eats), not lunch spend
- Most engaged with companion Monday mornings

## Preferences
- Dislikes being asked about dollar amounts in goals repeatedly
- Responds well to "I noticed" framing vs. "you should"

## Resolved
- 2026-02: Adobe charge — identified $12/month charge, user confirmed cancelled
- 2026-03: Overdraft anxiety — resolved after setting up automatic transfer buffer
```

---

## Data Flow Diagrams

### Transaction Ingestion

```
data/statements/
  ├── chase-may.pdf
  ├── citi-may.pdf
  └── discover-may.csv
         │
         ▼
  ingest_statements()
         │
         ├── Open data/transactions.db
         │
         ├── For each file in statements/:
         │   │
         │   ├── Query file_sources WHERE filename = ?
         │   │   └── mtime matches stored? → SKIP (already parsed)
         │   │
         │   └── mtime changed or not found → parse:
         │       │
         │       ├── .csv → _parse_csv() [existing, works]
         │       │
         │       └── .pdf → _parse_pdf_v2()
         │               │
         │               ├── extract_text (pdfminer)
         │               ├── chunk text (≤ 3000 chars)
         │               ├── LLM call per chunk → JSON rows
         │               ├── validate rows
         │               └── if < 2 rows → log parse_error
         │
         ├── INSERT OR IGNORE INTO transactions (dedup via PK)
         │
         ├── UPDATE file_sources (mtime, method, count, error)
         │
         └── SELECT * FROM transactions ORDER BY date DESC
             → Return List[Transaction]
```

### LLM Context Assembly

```
User sends message
       │
       ▼
Companion.chat()
       │
       ├── Load data/user_wiki.md          [~500 tokens, Tier 1, always]
       │
       ├── Load data/config.json           [~60 tokens, Tier 1, always]
       │
       ├── Load ConversationStore          [last 15 turns, ~600 tokens]
       │
       ├── Detect intent
       │   ├── checkin / overview
       │   │   └── SQL: top 5 categories + transactions > $100  [~250 tokens]
       │   │
       │   ├── monthly_analysis
       │   │   └── SQL: all categories + large charges + 6-month trend  [~350 tokens]
       │   │
       │   └── question (default)
       │       └── SQL: 30 most recent, grouped by category  [~200 tokens]
       │
       ├── Assemble messages:
       │   [{"role": "system", "content": companion_prompt + wiki + config}]
       │   [history turns]
       │   [{"role": "user", "content": question + "\n\n[Context]\n" + tx_context}]
       │
       ├── LLM call → response
       │
       ├── Append turns with timestamp + session_id to memory.json
       │
       └── If session conditions met → _update_wiki() [async, post-response]
```

### Agent Evolution Loop

```
Session starts
    │
    ├── Companion.__init__()
    │   ├── wiki = read data/user_wiki.md (or "" if not exists)
    │   └── history = ConversationStore.load(max_turns=15)
    │
    [multiple chat() calls — wiki injected in every LLM call]
    │
    Session close condition:
    ├── clear_memory() called (New Chat button)
    ├── OR new Companion() instance AND ≥ 2 turns in prior session
    │
    ▼
_update_wiki(session_turns, current_wiki)
    │
    ├── Build update prompt:
    │   "Here is the user's current profile:
    │    {current_wiki}
    │
    │    Here is what happened in today's conversation:
    │    {session_turns formatted as dialogue}
    │
    │    Update the profile. Rules:
    │    - Output ONLY the markdown document, no other text
    │    - Keep under 600 tokens total
    │    - Max 3 Active Concerns, 4 Patterns, 3 Preferences
    │    - Move concerns not mentioned in this session and last raised
    │      > 3 sessions ago to Resolved (with date + one-line resolution)
    │    - If stated goal conflicts with observed behavior, note BOTH
    │      in Patterns — do not silently overwrite either
    │    - Keep all section headers exactly as shown"
    │
    ├── LLM call → updated markdown
    │
    ├── Validate: all 6 section headers present? token count < 700?
    │   └── Validation fails → keep existing wiki, log warning
    │
    ├── Write data/user_wiki.md
    │
    └── Archive session turns to data/sessions/YYYY-MM-DD-{session_id}.jsonl
```

---

## Context Token Budget

| Component | Tokens | Source | When |
|---|---|---|---|
| System prompt (`companion.txt`) | ~350 | file | Always |
| User wiki | ~500 | `data/user_wiki.md` | Always |
| User config (name, goal) | ~60 | `data/config.json` | Always |
| Transaction context | ~200–350 | SQLite query (intent-based) | Always |
| Conversation history | ~600 | `data/memory.json` (15 turns) | Always |
| User message | ~50–100 | current input | Always |
| **Total** | **~1760–1960** | | |

DeepSeek context window: 32k tokens. Cost per call at ~2000 tokens input + ~150 tokens output: ~$0.00033. A user sending 10 messages/day costs $0.10/month in LLM fees.

Monthly analysis uses the richer system prompt (`monthly_analysis.txt`, ~450 tokens) with full month data (~400 tokens), bringing that call to ~2200 tokens — still well within budget.

---

## Schema: New and Changed Files

### `data/transactions.db` (new)

Two tables as defined above. Created on first `ingest_statements()` call.

### `data/user_wiki.md` (new)

Created on first session with the user's name + config as the initial `Identity` section. All other sections start empty and are populated by the wiki updater.

### `data/memory.json` (non-breaking extension)

Add `timestamp` (ISO 8601) and `session_id` (string) to each turn. Existing readers ignore unknown keys.

```json
{
  "history": [
    {
      "role": "user",
      "content": "How am I doing this month?",
      "timestamp": "2026-05-19T09:15:22",
      "session_id": "s_20260519_091500"
    }
  ]
}
```

### `data/sessions/` (new directory)

Per-session archives. Written at session close.

```
data/sessions/
  2026-05-19-s_abc123.jsonl    ← recent sessions
  2026-05-12-s_def456.jsonl
  archive/                     ← sessions > 90 days, gzipped
    2026-02-01-s_ghi789.jsonl.gz
```

### `data/config.json`, `data/snapshots.json`, `data/user_prefs.json` — unchanged

---

## Source Code Changes

### `src/models.py`

Add `source_file: str = ""` field to `Transaction` (optional, backward-compatible).

### `src/statement_ingester.py`

- Replace `_parse_pdf()` with `_parse_pdf_v2()` using LLM extraction
- Add `_chunk_text(text, max_chars) -> List[str]`
- Add `_extract_via_llm(chunk) -> List[dict]` (one LLM call, returns raw dicts)
- `ingest_statements()` now reads/writes SQLite instead of returning ephemeral list
- Keep returning `List[Transaction]` for backward compatibility with all call sites

### `src/storage.py`

- Add `TransactionStore` class wrapping `sqlite3`
  - `TransactionStore.init_db()` — creates tables if not exist
  - `TransactionStore.upsert(transactions, source_file, parse_method)` — INSERT OR IGNORE
  - `TransactionStore.query(start_date, end_date, category, min_amount, limit) -> List[Transaction]`
  - `TransactionStore.get_file_source(filename) -> Optional[dict]`
  - `TransactionStore.upsert_file_source(filename, filepath, mtime, method, count, error)`
- Add `WikiStore` class
  - `WikiStore.load() -> str` — reads `data/user_wiki.md`, returns "" if not exists
  - `WikiStore.save(content: str) -> None`
  - `WikiStore.exists() -> bool`
- Extend `ConversationStore.append()` to add `timestamp` and `session_id`

### `src/wiki_updater.py` (new file)

- `update_wiki(session_turns: List[dict], current_wiki: str) -> str`
  - Builds the wiki update prompt
  - Calls LLM
  - Validates response (section headers + token count)
  - Returns updated wiki content (or current_wiki if validation fails)
- `should_update_wiki(session_turns: List[dict]) -> bool`
  - Returns True if ≥ 2 assistant turns in session (avoid updating for single pings)

### `src/companion.py`

- `__init__`: load wiki via `WikiStore.load()`; set `self.session_id`; track `self.session_turns`
- `chat()`: inject `wiki` into every context build; append turns to `self.session_turns`
- `clear_memory()`: trigger `_finalize_session()` before clearing
- Add `_finalize_session()`: calls `update_wiki()` if `should_update_wiki()`, archives session turns, clears session state

### `src/llm_orchestrator.py`

- `answer_question()`: add `wiki: str = ""` parameter; prepend wiki to system content
- `generate_checkin()`: add `wiki: str = ""` parameter; include wiki in messages
- `generate_monthly_analysis()`: same

### `src/prompts/wiki_update.txt` (new)

The wiki update system prompt (the "Rules" block defined in the evolution loop above).

---

## File Structure After Implementation

```
data/
  config.json            unchanged — user profile from onboarding
  user_prefs.json        unchanged — legacy format
  snapshots.json         unchanged — period aggregates

  transactions.db        NEW — SQLite (transactions + file_sources tables)
  user_wiki.md           NEW — always-loaded companion context

  memory.json            EXTENDED — adds timestamp + session_id per turn
  sessions/              NEW — per-session turn archives
    2026-05-19-s_abc.jsonl
    archive/

  statements/            unchanged — raw source files
    chase-may.pdf
    discover-may.csv

src/
  models.py              add source_file field
  statement_ingester.py  replace _parse_pdf, ingest writes to SQLite
  storage.py             add TransactionStore, WikiStore; extend ConversationStore
  wiki_updater.py        NEW — wiki maintenance LLM call
  companion.py           add session tracking + _finalize_session()
  llm_orchestrator.py    add wiki param to all context-building functions
  prompts/
    companion.txt        unchanged
    monthly_analysis.txt unchanged
    wiki_update.txt      NEW — wiki update system prompt
```

---

## Implementation Sequence

Each step is independently shippable — the app is more functional after each one.

**Step 1: SQLite transaction store**
Create `TransactionStore` in `storage.py`. Migrate `ingest_statements()` to write to DB and query from DB. Update all call sites in `web_chat.py`. Run existing tests. The PDF bug is still present but now the fix is just changing one function.

**Step 2: PDF parser v2**
Replace `_parse_pdf()` with `_parse_pdf_v2()` using LLM extraction. Add CSV fallback instructions. Test with a real PDF from `data/statements/`. Verify rows appear in `transactions.db`.

**Step 3: Conversation timestamps + session IDs**
Non-breaking: add `timestamp` and `session_id` to `ConversationStore.append()`. Add `sessions/` directory. No behavior change — just metadata enrichment.

**Step 4: Living wiki**
Add `WikiStore`, `wiki_updater.py`, `wiki_update.txt`. Wire `_finalize_session()` into `Companion.clear_memory()` and the web POST /chat path. Add wiki to all LLM call context. Test: have a conversation, hit New Chat, verify `data/user_wiki.md` is written.

---

## Open Questions Before Implementation

These are decisions that affect implementation details but not the overall design:

1. **Wiki update timing in the web path:** The web server creates a new `Companion()` on every POST /chat request. Session end is ambiguous. Options:
   - Update wiki when `clear_memory()` is called (New Chat button) — explicit, user-controlled
   - Update wiki when a new session is detected (check `session_id` in memory.json vs. current) — automatic
   - Both (automatic + explicit)

2. **Initial wiki bootstrap:** When `user_wiki.md` doesn't exist yet, do we:
   - Create it immediately from `config.json` data (name, goal) with empty sections — clean start
   - Wait for first session to end, then create from that session's turns — richer initial content

3. **PDF extraction error visibility:** When a PDF fails LLM extraction, do we:
   - Surface the error in the chat response ("I couldn't read chase-may.pdf — here's how to export a CSV")
   - Log silently and continue with partial data
   - Show an indicator in the dashboard ("3 files could not be parsed")

4. **`source_file` field in the wiki's Observed Patterns:** Should the wiki note which transactions triggered a pattern observation, or keep it high-level? (Affects auditability vs. wiki bloat.)
