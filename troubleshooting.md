# PennyPath — Troubleshooting

A practical guide for inspecting the service's intermediate state during local development. This file grows as we cover more areas — start here, add sections per surface.

## Where state lives

| Store | Path | What's in it |
|---|---|---|
| Transactions DB | `data/transactions.db` | `accounts`, `transactions`, `file_sources` (SQLite) |
| User config | `data/config.json` | Name, finance profile, goals, intentions |
| Conversation memory | `data/memory.json` | Chat history + session tracking |
| User wiki | `data/user_wiki.md` | LLM-distilled profile (updated after sessions) |
| Snapshots | `data/snapshots.json` | Pre-aggregated period summaries |
| Raw statements | `data/statements/*.pdf` / `*.csv` | Source files for ingestion |

---

## 1. Reading `data/transactions.db`

### 1.1 What's in it

Three tables:

- **`accounts`** — one row per linked account.
  `id` · `user_id` · `bank` · `name` · `mask` (last 4) · `type` (`checking`/`credit`/`savings`/`unknown`) · `source` (`plaid`/`statement`) · `created_at`
- **`transactions`** — one row per parsed transaction.
  `id` · `date` · `amount` (positive = money out, negative = money in) · `description` · `category` · `account_type` · `source_file` · `user_id` · `account_id` (FK → `accounts.id`) · `source` (`plaid`/`statement_pdf`/`statement_csv`) · `dedup_hash` · `flow_type` (`spending`/`transfer`/`interest`/`fee`/`refund`/`income`/`unknown`) · `ingested_at`
- **`file_sources`** — one row per ingested statement file. Keyed by `filepath` (`filename` alone is not unique — different banks can share a basename).
  `id` (PK, stable across re-ingests) · `filename` · `filepath` (UNIQUE) · `file_mtime` · `parse_method` · `tx_count` · `parse_error` · `recon_warning` · `parsed_at`

### 1.2 Quick view from the CLI

```bash
python -m src.cli accounts   # accounts + per-account transaction counts
python -m src.cli ingest     # (re)parse new/changed statements; prints parse errors + reconciliation warnings
```

### 1.3 Opening the DB directly

```bash
sqlite3 data/transactions.db
```

Make the output readable, then list tables / schema:

```
.headers on
.mode column
.tables
.schema transactions
.schema accounts
.schema file_sources
```

Exit with `.quit`.

### 1.4 Query recipes (paste straight into `sqlite3`)

**All accounts with row counts:**
```sql
SELECT a.bank, a.name, a.type, a.mask, a.source, COUNT(t.id) AS txns
FROM accounts a
LEFT JOIN transactions t ON t.account_id = a.id
GROUP BY a.id
ORDER BY a.type, a.mask;
```

**Most recent 20 transactions:**
```sql
SELECT date, amount, description, category, source_file
FROM transactions
ORDER BY date DESC, id
LIMIT 20;
```

**Everything for one account** (pass the bank/mask you care about):
```sql
SELECT t.date, t.amount, t.description, t.category
FROM transactions t
JOIN accounts a ON a.id = t.account_id
WHERE a.mask = '0418'
ORDER BY t.date DESC
LIMIT 50;
```

**Everything from one statement file** (use exactly what's in `source_file`):
```sql
SELECT date, amount, description, category
FROM transactions
WHERE source_file = 'eStmt_2025-02-23.pdf'
ORDER BY date, id;
```

**Spending by category, last 90 days, one account** (excludes payments, interest, fees, refunds):
```sql
SELECT category, COUNT(*) AS n, ROUND(SUM(amount), 2) AS total
FROM transactions
WHERE account_id = (SELECT id FROM accounts WHERE mask = '8373')
  AND date >= date('now', '-90 days')
  AND flow_type = 'spending'
GROUP BY category
ORDER BY total DESC;
```

**Per-bucket totals across the whole DB** — sanity check the classification:
```sql
SELECT flow_type, COUNT(*) AS n, ROUND(SUM(amount), 2) AS total
FROM transactions
GROUP BY flow_type
ORDER BY n DESC;
```

**Interest paid year-to-date, per account:**
```sql
SELECT a.bank, a.mask, ROUND(SUM(t.amount), 2) AS interest_paid
FROM transactions t JOIN accounts a ON a.id = t.account_id
WHERE t.flow_type = 'interest'
  AND substr(t.date, 1, 4) = strftime('%Y', 'now')
GROUP BY a.id;
```

**All credit-card payments** (money you sent to a card from your checking):
```sql
SELECT t.date, a.bank, a.mask, t.amount, t.description
FROM transactions t JOIN accounts a ON a.id = t.account_id
WHERE t.flow_type = 'transfer'
ORDER BY t.date DESC;
```

**Unclassified rows** — flow_type='unknown' usually means the extractor missed the classification; worth a look:
```sql
SELECT date, amount, description, source_file
FROM transactions
WHERE flow_type = 'unknown'
ORDER BY source_file, date;
```

**Find a merchant (case-insensitive substring):**
```sql
SELECT date, amount, description, source_file
FROM transactions
WHERE LOWER(description) LIKE '%netflix%'
ORDER BY date;
```

**Look for likely duplicates (same content hash across files):**
```sql
SELECT dedup_hash, COUNT(*) AS n, GROUP_CONCAT(source_file) AS files
FROM transactions
GROUP BY dedup_hash
HAVING n > 1
ORDER BY n DESC
LIMIT 20;
```
Dedup-counting is deferred to the query layer by design — these are *kept* in storage; this query surfaces what a future dedup pass would collapse.

**Hunt for internal-transfer legs** (same magnitude, opposite sign, near same date, different accounts):
```sql
SELECT a.date, a.account_id AS acct_a, b.account_id AS acct_b,
       a.amount, b.amount, a.description, b.description
FROM transactions a
JOIN transactions b
  ON ABS(a.amount + b.amount) < 0.01
 AND a.account_id <> b.account_id
 AND ABS(julianday(a.date) - julianday(b.date)) <= 3
WHERE a.amount > 0
ORDER BY a.date DESC
LIMIT 20;
```

### 1.5 Checking ingestion health

**Per-file ingestion state:**
```sql
SELECT filename, tx_count, parse_error IS NOT NULL AS errored,
       recon_warning IS NOT NULL AS has_warning, parsed_at
FROM file_sources
ORDER BY filename;
```

**Files with reconciliation warnings (extraction may have missed/added rows):**
```sql
SELECT filename, recon_warning
FROM file_sources
WHERE recon_warning IS NOT NULL;
```

**Files with hard parse errors (no transactions stored):**
```sql
SELECT filename, parse_error
FROM file_sources
WHERE parse_error IS NOT NULL;
```

### 1.6 Common workflows

**Re-parse one file** — the mtime guard skips unchanged files. Force a re-parse by clearing its `file_sources` row, then re-ingesting:
```bash
sqlite3 data/transactions.db "DELETE FROM file_sources WHERE filepath='data/statements/eStmt_2025-02-23.pdf'"
python -m src.cli ingest
```

(Alternative: `touch data/statements/eStmt_2025-02-23.pdf` to bump the mtime.)

**Re-parse everything from scratch** — wipes the DB; full re-extraction over every file (LLM calls, takes a few minutes):
```bash
rm data/transactions.db
python -m src.cli ingest
```

**Snapshot the DB before risky edits:**
```bash
cp data/transactions.db data/transactions.db.bak
```

### 1.7 Ad-hoc Python (when SQL is awkward)

For dict-style results or post-processing:

```bash
python -c "
import sqlite3
c = sqlite3.connect('data/transactions.db')
c.row_factory = sqlite3.Row
for r in c.execute('SELECT date, amount, description FROM transactions ORDER BY date DESC LIMIT 5'):
    print(dict(r))
"
```

Or load through the existing typed helper, which returns `Transaction` objects with all fields populated:

```bash
python -c "
from src.storage import TransactionStore
for t in TransactionStore.query_all()[:5]:
    print(t)
"
```

---

*More sections to come — memory / wiki inspection, LLM call tracing, ingestion-pipeline tracing.*
