# Storage

How financial records enter the system, how they're laid out on disk, and what
the rest of the product can assume about them. Implemented in `src/storage.py`,
`src/statement_ingester.py`, `src/plaid_client.py`, `src/reconciler.py`.

Phase 1A (raw storage) + Phase 1B (reconciliation layer).

---

## Overview

### Tables & views at a glance

`data/transactions.db` (SQLite). Raw is immutable; the reconciliation layer is a
derived, rebuildable projection of it.

| object | kind | role |
|---|---|---|
| `accounts` | table | one row per real account (`md5(user_id\|type\|mask)`) |
| `transactions` | table | **raw, immutable** source-of-truth rows (magnitude amounts, source labels) |
| `file_sources` | table | per-user PDF/CSV ingestion bookkeeping (mtime guard, parse errors) |
| `chart_annotations` | table | per-user dashboard insight cache (Phase 1B) |
| `pinned_charts` | table | per-user pinned charts (Phase 1B slot; written in 1C) |
| `transactions_recon` | table | **derived**, 1:1 with raw — corrected flow_type, transfer/dup flags, signed amount |
| `v_transactions_signed` | view | raw + per-account balance sign (`account_flow`) |
| `v_transactions_recon` | view | raw + the recon overlay — **what every consumer reads** |

Every table holding user data carries `user_id`; every read filters on it (see
_Multi-tenancy_).

### Data process flow

```
            ┌─ statement PDF/CSV ─┐         ┌─ Plaid ─┐
            │  (LLM extraction +  │         │  /transactions │
            │   reconciliation)   │         │   get          │
            └─────────┬───────────┘         └────┬───────────┘
                      │  write magnitude rows         │
                      ▼                               ▼
              ┌───────────────────────────────────────────┐
              │  transactions  (RAW, immutable per row)    │
              │  amount ≥ 0 · section_type · flow_type(LLM)│
              └───────────────────────┬───────────────────┘
                                      │  rebuild_recon(user_id)
                                      │  (on ingest, CLI, or lazy freshness guard)
                                      ▼
              ┌───────────────────────────────────────────┐
              │  transactions_recon  (DERIVED, 1:1)        │
              │  flow_type_recon · is_internal_transfer    │
              │  transfer_group_id · is_duplicate · signed │
              └───────────────────────┬───────────────────┘
                                      │  v_transactions_recon (LEFT JOIN raw)
                                      ▼
        dashboard_queries · companion chat · monthly analysis  (all read the view)
```

1. **Ingest (raw).** Statement and Plaid paths write magnitude-only rows into
   `transactions` with the source's structural `section_type` and the LLM's
   best-guess `flow_type`/`category`. Nothing is paired, deduped, or signed here.
2. **Reconcile (derived).** `reconcile()` is a pure function of one user's raw
   rows → recon rows: pair internal transfers (credit-card payments + bank
   self-transfers), correct the paired legs' `flow_type`, flag cross-source
   duplicates, and compute the signed amount. `rebuild_recon(user_id)`
   materializes it — fired on ingest, on `python -m src.cli rebuild-recon`, or
   lazily on read when raw is newer than recon.
3. **Read.** All consumers query `v_transactions_recon` (raw columns + recon
   overlay), so spending/income totals, chat, and analysis agree and exclude
   internal transfers + duplicates consistently.

---

## Philosophy

- **Capture raw, defer interpretation.** Storage records what the source said.
  Decisions that depend on convention (sign, dedup, transfer pairing) happen
  at query / MCP time, not at write time.
- **Single source of truth.** One `transactions` table for every source —
  statement PDFs, statement CSVs, Plaid. The `source` column distinguishes.
- **Convention-free amounts.** `amount` is always a non-negative magnitude.
  Direction is derived from `section_type` (deterministic, in code, per row).
  This keeps the schema neutral and makes future convention changes a one-line
  edit in a view, not a data migration.
- **Refuse rather than silently corrupt.** A parse failure aborts the file and
  preserves the previously-ingested rows for that file. A reconciliation
  catastrophe refuses the file. Either way the user gets a loud signal in
  `file_sources.parse_error`, and the next `ingest` run retries automatically.
- **No cross-source side effects at write time.** Plaid sync and statement
  ingest write into the same tables independently. Dedup and transfer pairing
  are never done by mutating raw — they're materialized in the
  _Reconciliation layer_ (`transactions_recon`), a pure, rebuildable derivation.
- **Multi-tenant by row.** Every table that holds user data carries a `user_id`
  and every read filters on it; one shared SQLite file backs many tenants with
  no cross-user leakage. See _Multi-tenancy_ below for what's ready and what's
  deferred to 1D.

---

## Data schema

`data/transactions.db` (SQLite). Six tables + two views — see the _Overview_
table above. Raw tables (`accounts`, `transactions`, `file_sources`) and the
1B tables (`chart_annotations`, `pinned_charts`, `transactions_recon`).

### `accounts`

One row per `account_id = md5(user_id|account_type|mask)[:12]`. Deterministic
so re-ingestion converges on one row per real account regardless of source.
Upserted by both statement and Plaid paths.

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | deterministic md5 of (user_id, type, mask) |
| `user_id` | TEXT | slugified from `UserConfig.name` |
| `bank` | TEXT | institution (e.g. `"Bank of America"`) |
| `name` | TEXT | product (e.g. `"Adv Plus Banking"`) |
| `mask` | TEXT | last 4 digits |
| `type` | TEXT | `checking` / `credit` / `savings` / `unknown` |
| `source` | TEXT | `statement` / `plaid` (first-seen wins; later upserts overwrite) |
| `created_at` | TEXT | ISO timestamp |

### `transactions`

Source-of-truth row per money-movement activity. Same shape for every source.

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | `"{source_file}#{i}"` for PDF/CSV; Plaid `transaction_id` for Plaid |
| `date` | TEXT | ISO `YYYY-MM-DD`. Time of day not stored (no source provides it reliably) |
| `amount` | REAL ≥ 0 | **Magnitude only.** Sign / direction lives in `section_type` |
| `description` | TEXT | merchant or payee, as it appeared on the source |
| `category` | TEXT | soft enum; preferred-list snap with LLM-invented fallback |
| `account_type` | TEXT | `checking` / `credit` / `savings` / `unknown` |
| `source_file` | TEXT | basename of originating PDF/CSV (PDF path); empty for Plaid |
| `user_id` | TEXT | FK-ish to UserConfig name slug |
| `account_id` | TEXT | FK to `accounts.id` |
| `source` | TEXT | `statement_pdf` / `statement_csv` / `plaid` |
| `dedup_hash` | TEXT | md5 of (account_id, date, amount, description). Stored, never enforced — see _Deferred concerns_ |
| `flow_type` | TEXT | `spending` / `transfer` / `interest` / `fee` / `refund` / `income` / `unknown` |
| `notes` | TEXT | free-form source detail that doesn't fit other columns |
| `section_type` | TEXT | structural label from the source (closed enum below) |
| `ingested_at` | TEXT | ISO timestamp |

### `file_sources` (PDF/CSV only)

One row per ingested statement file. Plaid doesn't write here.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | stable across re-ingests |
| `user_id` | TEXT | tenant scope; backfilled from the transactions a file produced |
| `filename` | TEXT | basename |
| `filepath` | TEXT | path; uniqueness is `UNIQUE(user_id, filepath)` so two tenants can share a relative path |
| `file_mtime` | REAL | for the skip-if-clean-and-unchanged guard |
| `parse_method` | TEXT | `csv` / `llm` |
| `tx_count` | INTEGER | count of rows stored from this file |
| `parse_error` | TEXT NULL | non-null → file was refused; rows untouched |
| `recon_warning` | TEXT NULL | non-null but no parse_error → file saved with caveats |
| `parsed_at` | TEXT | ISO timestamp |

### `chart_annotations` (Phase 1B — dashboard insight cache)

Per-user cache of LLM-generated chart annotations. Regenerable, so a schema bump
just drops and recreates it.

| column | type | notes |
|---|---|---|
| `user_id` | TEXT | tenant scope (part of PK) |
| `chart_key` | TEXT | `spending` / `income` / `transactions` / `cashflow` |
| `period_key` | TEXT | e.g. `2026-05`, `last-12mo:2026-05` |
| `payload_hash` | TEXT | SHA1 of the aggregated payload; mismatch ⇒ regenerate |
| `annotation_text` | TEXT | the warm headline |
| `suggestions` | TEXT | JSON array of ≤2 gentle suggestions |
| `generated_at` | TEXT | ISO timestamp |
| PRIMARY KEY | | `(user_id, chart_key, period_key)` |

### `pinned_charts` (Phase 1B slot — written in 1C)

Per-user pinned custom charts. Empty in 1B; the read path returns this user's rows.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `user_id` | TEXT | tenant scope (indexed) |
| `name` | TEXT | chart title |
| `spec_json` | TEXT | chart definition |
| `pinned_at` | TEXT | ISO timestamp |

### View: `v_transactions_signed`

Derived column `account_flow` that adds a sign per row based on
`(account_type, section_type)`. Positive = this row raised the account's
balance number; negative = lowered it. The view is the per-account
balance-reconciliation projection — base table stays convention-free.

```sql
CREATE VIEW v_transactions_signed AS
SELECT t.*,
    CASE
        -- Credit cards: balance OWED rises with purchases/cash_advances/fees/interest,
        -- falls with payments/refunds.
        WHEN t.account_type = 'credit'
          AND t.section_type IN ('purchase','cash_advance','interest_charged','fee') THEN  t.amount
        WHEN t.account_type = 'credit'
          AND t.section_type IN ('payment','refund','interest_credited') THEN -t.amount
        -- Checking/savings: balance rises with deposits/interest_credited/refund,
        -- falls with withdrawals/checks/fees/interest_charged.
        WHEN t.account_type IN ('checking','savings')
          AND t.section_type IN ('deposit','interest_credited','refund') THEN  t.amount
        WHEN t.account_type IN ('checking','savings')
          AND t.section_type IN ('withdrawal','check','fee','interest_charged') THEN -t.amount
        ELSE 0
    END AS account_flow
FROM transactions t;
```

User-level queries that group by `flow_type` don't need this view — `amount`
is already the magnitude they want.

### `transactions_recon` (Phase 1B — reconciliation layer)

Materialized, **derived** projection of `transactions`: 1:1 by `id`, one row per
raw row, holding the reconciled interpretation. A pure, rebuildable function of
raw — never hand-edited (see _Reconciliation layer_ for the build contract).

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | same as `transactions.id` — the raw row this derives from |
| `user_id` | TEXT | tenant scope (indexed `idx_recon_user`); rebuild + reads filter on it |
| `flow_type_recon` | TEXT | corrected `flow_type` (e.g. a self-Zelle deposit fixed `income`→`transfer`) |
| `signed_amount` | REAL | per-account balance sign, same convention as `v_transactions_signed.account_flow` |
| `is_internal_transfer` | INTEGER | 1 if this row is one leg of a paired internal transfer |
| `transfer_group_id` | TEXT NULL | links the two legs of a pair (audit + display) |
| `is_duplicate` | INTEGER | 1 if collapsed by cross-source dedup (separate concern from transfers) |
| `reconciled_at` | TEXT | ISO timestamp of the rebuild that produced this row |

### View: `v_transactions_recon`

The projection every consumer (dashboard, chat, monthly analysis) reads. Raw
columns + the recon overlay; `LEFT JOIN` + `COALESCE` so a not-yet-reconciled
row still returns, falling back to raw `flow_type`.

```sql
CREATE VIEW v_transactions_recon AS
SELECT t.*,
    COALESCE(r.flow_type_recon, t.flow_type) AS flow_type_recon,
    COALESCE(r.signed_amount, 0)             AS signed_amount,
    COALESCE(r.is_internal_transfer, 0)      AS is_internal_transfer,
    r.transfer_group_id                      AS transfer_group_id,
    COALESCE(r.is_duplicate, 0)              AS is_duplicate
FROM transactions t
LEFT JOIN transactions_recon r ON r.id = t.id;
```

For the common "real spending/income" query, filter
`WHERE flow_type_recon = ? AND is_internal_transfer = 0 AND is_duplicate = 0`.

---

## Closed enums

### `section_type` (where the row appeared in the source)

`section_type` is per the **statement's account perspective**. The allowed
values partition by account type — the LLM (or Plaid mapper) must pick from
the right side. Cross-domain misuse (e.g. `payment` on a checking statement)
is auto-corrected by `_normalize_section_type(raw, account_type)` — see
_Defensive normalization_ below.

| value | account types | typical source heading |
|---|---|---|
| `deposit` | checking / savings | "Deposits and other additions", "ACH Credits" |
| `withdrawal` | checking / savings | "Withdrawals and other subtractions", "ACH Debits", "Online Payments" (incl. payments TO a CC) |
| `check` | checking | "Checks", "Checks paid" |
| `fee` | both | "Service fees", "FEES CHARGED" — bank-imposed only |
| `interest_charged` | both | "INTEREST CHARGED" (CC, owed) — also rare on checking |
| `interest_credited` | both | "Interest paid" (checking, deposited TO you) |
| `payment` | credit only | "PAYMENTS AND OTHER CREDITS" — payment received toward the card |
| `purchase` | credit only | "PURCHASE" / "PURCHASES" |
| `cash_advance` | credit only | "CASH ADVANCES" — withdrawing cash against the credit line. Increases balance owed; separate from purchases (different APR, no grace period). Includes Venmo / Visa Direct pushes. |
| `refund` | both | merchant refund / reversal — may appear under PAYMENTS section on CC |

**The same money movement gets different `section_type` on each side.** A
$10,000 "Payment To Chase Card" appears as `withdrawal` on the funding
checking statement and `payment` on the receiving CC statement. Both rows
are stored — pairing is deferred to query time (see _Deferred concerns_).

### `flow_type` (what kind of money movement)

`spending` / `transfer` / `interest` / `fee` / `refund` / `income`.
Semantic interpretation; the LLM (or Plaid PFC mapping) sets this. Different
from `section_type` — a checking `withdrawal` could be `spending` (Verizon
bill), `transfer` (paying down CC), or `fee` (NSF charge).

### `category` (soft enum)

Preferred 15-item list for `flow_type=spending`:

```
Dining, Groceries, Transportation, Travel, Entertainment, Shopping,
Utilities, Healthcare, Insurance, Housing, Personal Care, Education,
Subscriptions, Sports & Recreation, Childcare
```

LLM may invent a new category when none fit (escape hatch). Recurring
inventions get promoted into the preferred list (and the prompt) over time.

Non-spending flow types use natural labels: `Payment`, `Transfer`, `Interest`,
`Bank Fees`, `Refund`, `Salary`, `Investment Income`, etc.

### `notes` (free-form)

Sponge for source detail that doesn't fit other columns. Examples:

| Source artifact | `notes` value |
|---|---|
| Foreign-currency conversion | `NZD 6.61 @ 0.593040847` |
| Posting date distinct from transaction date | `posted 08/06` |
| Bank reference / transaction ID | `ref: 24710000120007316406` |
| Zelle / wire / ACH memo | `memo: rent July` |
| Check number | `check #1234` |
| Plaid pending flag | `pending` |
| Plaid non-USD currency | `currency: CAD` |
| Multiple artifacts | semicolon-joined: `NZD 6.61 @ 0.593; posted 08/06` |

Intentionally unstructured. When a pattern recurs often enough to be queried
directly, it gets promoted to a typed column.

---

## Source 1: Statement ingestion (PDF / CSV)

For each new or modified file in `data/statements/`:

```
1. Render PDF (pymupdf4llm)
2. Extract account identifier + summary from first 2 pages (LLM)
3. Multi-account guard (refuse if >1 account on those pages)
4. Derive last-4 mask deterministically; build account_id; upsert account
5. Extract all activities from the full document (LLM, chunked)
6. Reconcile parsed activities against summary (in-memory)
7. Commit decision: save clean / save-with-warning / refuse
```

CSV is a thin variant: no LLM, no reconciliation. The CSV path expects
`date,amount,description,category,account_type,account_mask,bank,flow_type`
columns and writes magnitude + a best-effort `section_type` derived from
`flow_type`. Most ingestion is PDF; CSV exists as a fallback when a PDF
is unparseable.

### Step 1 — Render

- Metadata pass: `pymupdf4llm.to_markdown(path, pages=[0, 1])`. Two pages
  because some banks push the account-summary box past the address block
  onto page 2.
- Activity pass: `pymupdf4llm.to_markdown(path)` for the full document.

### Step 2 — Account identifier + summary

One LLM call. Returns:

- `bank`, `account_name`, `account_type` (`checking` / `credit` / `savings` / `unknown`)
- `account_number` — the account number as written in the document, verbatim
  (e.g. `"XXXX XXXX XXXX 7370"` or `"123456789012"`). Last-4 is derived in
  code — `_last4()` takes the trailing 4 digits of the trailing numeric group.
- `account_number_count` — how many distinct account numbers appear (for the
  multi-account guard)
- `period_start`, `period_end`
- Summary totals — `previous_balance`, `new_balance`, `total_purchases`,
  `total_payments`, `total_interest`, `total_fees`, `total_cash_advances`.
  **In-memory only**, used for reconciliation in Step 6. The CC-only totals
  (`purchases`, `payments`, `cash_advances`) must be `null` on
  checking/savings statements — the prompt explicitly tells the LLM this,
  because it used to fill `total_payments` with the checking-account deposits
  total, which then wrecked reconciliation.

The prompt explicitly tells the LLM where to look (`"Account Number:"`,
`"Account ending in:"`, masked patterns) and what to ignore (page numbers,
customer IDs, phone numbers, statement reference codes). Last-4 ambiguity
("which 4 digits?") is removed entirely — code does the slice.

### Step 3 — Multi-account guard

If `account_number_count > 1`, refuse with
`parse_error = "multi-account statement not supported"`. Prevents silent
corruption from combined statements.

### Step 4 — Activity extraction

Full markdown split into ≤ 10 000-char chunks that never cut a table (see
`_chunk_text`). For each chunk, the LLM extracts every transaction. Each
returned row has:

| field | notes |
|---|---|
| `date` | `YYYY-MM-DD` |
| `raw_amount` | the amount **string, verbatim from the source** (e.g. `"6,391.79"`, `"-788.29"`, `"$1,234.56"`, `"(45.00)"`). The LLM never decides the sign. |
| `description` | merchant / payee, verbatim |
| `section_type` | closed enum from the list above |
| `flow_type` | closed enum (semantic) |
| `category` | preferred-list snap or invented |
| `notes` | free-form |

Code then:
1. Strips signs / commas / `$` / parens from `raw_amount` → `magnitude` (always positive).
2. Validates `section_type` against the closed enum (`""` if unrecognized — flagged in recon).
3. Builds the `Transaction` row.

**Sign / direction is never an LLM decision.** It's derived from
`section_type` at query time via the `v_transactions_signed` view, or in
code during reconciliation.

**Retry:** each chunk gets 3 LLM attempts, each failure logged. If a chunk
exhausts its retries, the entire file aborts with `parse_error`. Partial
extraction is never recorded.

**Why 10K chars per chunk:** `deepseek-chat` truncates large structured
outputs short of its 8K-token output cap in practice. 10K input chars yields
~40 rows × ~250 chars of JSON each = ~10K output chars / ~2.5K tokens —
comfortable margin even on FX-heavy statements where each transaction
carries an extra `notes` field for the conversion rate. Earlier attempts at
15K and 25K chunks intermittently truncated the JSON mid-string on big NZ /
Whistler trip statements, dropping a whole chunk.

**LLM transport hardening.** `_llm_json` wraps the OpenAI SDK call with an
explicit 120-second timeout and 3 retries on `APITimeoutError` /
`APIConnectionError`. Without this, a half-open TCP socket can hang the
ingest indefinitely (we lost ~10 hours to this once).

### Defensive layers around the LLM

Several deterministic safety nets correct for known LLM and source-format
quirks. The prompt is the primary defense; these catch what slips through.

**1. Whitespace-tolerant magnitude parser.** Chase prints checking-account
debits as `"- 10,000.00"` with a literal space between the minus sign and
the digits. `float("- 10000.00")` raises `ValueError`. `_parse_magnitude`
now strips all whitespace before `float()`, so `"- 10,000.00"` and
`"-10,000.00"` both yield `10000.0`. Previously, rows with this format
were silently dropped at the `if magnitude is None: continue` gate, which
broke reconciliation (deposits balanced, withdrawals didn't).

**2. Cross-domain `section_type` remap.** `_normalize_section_type(raw,
account_type)` corrects a CC-only label on a checking statement (and vice
versa) based on the obvious mapping. The LLM occasionally tags a
"Payment To Chase Card" row on a checking statement as `section_type=
'payment'` because the description literally says "Payment" — but
`payment` is CC-only in our schema. The remap turns it into `withdrawal`.

| LLM said | Account is | We store |
|---|---|---|
| `payment` | checking/savings | `withdrawal` |
| `purchase` | checking/savings | `withdrawal` |
| `deposit` | credit | `payment` |
| `withdrawal` | credit | `purchase` |
| `check` | credit | `purchase` |

**3. Summary-row denylist.** Chase CC statements include an "ACCOUNT
SUMMARY" table on page 1 that the LLM sometimes extracts as
transactions — `Previous Balance $10,573.74`, `Purchases +$16,498.95`,
`Cash Advances +$180.25`. These have dates and amounts but are roll-ups,
not events. `_is_summary_row(description)` filters any row whose
description (case-folded, trimmed) matches a known summary label:
`previous balance`, `new balance`, `total purchases`, `total payments`,
`fees charged`, `cash advances`, `purchases`, `payments`, etc. The
prompt also enumerates these as "do NOT extract", but the LLM ignores
the prompt ~5% of the time on tabular front-page summaries.

### Step 5 — Reconciliation

Compare parsed activities against the in-memory summary from Step 2. Buckets
keyed by `section_type` (all magnitudes positive, so sums are simple).

| Statement total | Compared against |
|---|---|
| `total_purchases` | `sum(amount where section_type='purchase')` |
| `total_payments` | `sum(amount where section_type IN ('payment','refund'))` |
| `total_interest` | `sum(amount where section_type='interest_charged')` (CC) or `'interest_credited'` (checking) |
| `total_fees` | `sum(amount where section_type='fee')` |
| `total_cash_advances` | `sum(amount where section_type='cash_advance')` (CC only) |
| `new_balance − previous_balance` | derived per-account balance flow (see view) |

For each comparison `gap = abs(parsed − expected)`. Two-tier tolerance:

| Gap | Action |
|---|---|
| ≤ $0.01 | Pass clean — no warning |
| ≤ $5.00 OR ≤ 5% of `abs(expected)` | Pass with `recon_warning` |
| > both | Fail — promotes whole file to `parse_error` |

A file passes overall only if **every** comparison passes. A single failed
comparison fails the whole file.

Comparisons whose expected value is `null` on the statement are skipped —
we can't reconcile what the source didn't report. Untagged-section rows
(LLM returned a `section_type` not in our enum) are a per-file warning.

### Step 6 — Commit

| Outcome | DB action |
|---|---|
| **Pass clean** | `replace_file_transactions(filename, activities)` → DELETE then INSERT. `file_sources`: `parse_error=null`, `recon_warning=null`. |
| **Pass with warning** | Same as Pass clean, plus `recon_warning="…"` describing which buckets disagreed. |
| **Refuse** | Transaction rows untouched. `file_sources`: `parse_error="…"`. |

The retry-on-error mtime guard ensures any file with `parse_error` is
re-tried on the next `ingest` run.

---

## Source 2: Plaid ingestion

Live `/transactions/get` calls produce per-account transactions. Implemented
in `src/plaid_client.py`. Same schema, same storage path
(`TransactionStore.upsert_transactions`), no reconciliation step (Plaid is
its own source of truth; there's no per-statement total to check against).

Pure transformation `_plaid_to_transaction(plaid_txn, account_map, user_id)`:

| Plaid field | Becomes |
|---|---|
| `transaction_id` | `id` |
| `date` | `date` (ISO) |
| `amount` (signed; +out / −in) | `abs(...)` → `amount` (magnitude only); sign drives `section_type` |
| `merchant_name` ?? `name` | `description` |
| `personal_finance_category.primary` | `flow_type` (`_plaid_flow_type`) and `category` (`_plaid_to_category`) |
| `personal_finance_category.detailed` | refines `category` (Groceries vs Dining, Salary vs generic Income) |
| `transaction_code` | informs `section_type`: `'check'` → `check`, `'cash'` on credit → `cash_advance` |
| `payment_meta.check_number` | `notes: "check #1234"` |
| `pending` | `notes: "pending"` |
| `iso_currency_code` (≠ USD) | `notes: "currency: CAD"` |
| `authorized_date` (≠ `date`) | `notes: "authorized 2026-03-10"` |

**Section_type derivation** (`_plaid_section_type`) uses Plaid's sign +
account_type + PFC:

```
credit + amount > 0   → purchase (or fee if BANK_FEES, interest_charged if INTEREST*,
                        cash_advance if transaction_code='cash')
credit + amount < 0   → payment  (if LOAN_PAYMENTS / CREDIT_CARD_PAYMENT) else refund
checking + amount > 0 → withdrawal (or fee, or check if transaction_code='check')
checking + amount < 0 → deposit  (or interest_credited if INTEREST*)
```

Plaid accounts upsert into the same `accounts` table as PDFs. Because
`account_id = md5(user_id|type|mask)[:12]`, a user who provides both Plaid
and a statement for the same physical account converges to one `accounts`
row — no manual reconciliation needed.

Plaid path is intentionally test-driven via synthetic responses — no live
API in the test suite (no token required to validate the transformation).

---

## Multi-tenancy

One SQLite file serves many users. Isolation is **row-level**: every table that
holds user data carries `user_id`, and every read filters on it. There is no
per-user file or per-user database — tenants are separated by the column, so a
query that forgets the filter is the only way to leak across users.

### Ready (storage layer is multi-tenant today)

| Table | Scope mechanism |
|---|---|
| `accounts` | `user_id` column; `id = md5(user_id\|type\|mask)` namespaces the PK per user |
| `transactions` | `user_id` column + `idx_tx_user`; every `dashboard_queries` read does `WHERE user_id = ?` |
| `file_sources` | `user_id` column; `UNIQUE(user_id, filepath)` |
| `chart_annotations` | `user_id` in the composite PK — two tenants never share an insight |
| `pinned_charts` | `user_id` column + `idx_pinned_user` |
| `v_transactions_signed` | carries `t.*` (incl. `user_id`); consumers filter |

**Invariant:** any new table holding user data must include `user_id`, and any
new query against one must filter on it. The reconciliation layer
(`transactions_recon`) follows this — it carries `user_id`, and
`rebuild_recon(user_id)` rebuilds **only** the given user's rows, never a global
sweep (pairing is within-user anyway).

### Deferred to Phase 1D (auth / multi-user)

These are **not** storage-schema gaps — they're the identity and app-state layer:

- **Per-request identity.** `_resolve_user_id()` currently reads the single local
  `config.json`, so every request resolves to the same tenant. Real isolation
  needs auth/session (JWT) to carry the user per request. The data is already
  scoped; the request layer just can't yet tell two users apart.
- **File-based app state is single-user.** `config.json`, `memory.json`
  (conversation), `user_wiki.md`, and `snapshots.json` live at one fixed path in
  `DATA_DIR`. They must become per-user (e.g. `data/users/<id>/…`) or move into
  user-keyed tables before real multi-user.

---

## Reconciliation layer: `transactions_recon`

> **Status: built.** Implemented in `src/reconciler.py`; the table + the
> `v_transactions_recon` view are created in `TransactionStore.init_db`, and
> `dashboard_queries` reads the view.

Raw `transactions` stays **immutable** — it preserves exactly what the source
said and what the LLM first guessed (`flow_type`, `category`, `section_type`).
A separate, **materialized** `transactions_recon` table holds the *reconciled*
interpretation that every consumer — dashboard, companion chat, monthly
analysis — reads through one view. This is what makes the deferred concerns
below (transfer pairing, dedup, flow-type fixes) resolve **once, consistently**,
instead of each surface re-deriving them (or, worse, only some of them doing it).

**Invariant — recon is a pure, deterministic function of raw.** Never
hand-edited; always rebuildable. You can drop the whole table and recompute it
from `transactions` at any time. That single rule is what keeps it safe.

**Shape — 1:1 with raw, flags not deletion.** Every raw row gets one recon row
keyed by the same `id`, plus derived columns. Rows are never physically merged
or dropped — consumers filter on the flags. Keeps it reversible and auditable.
The column schema lives under _Data schema → `transactions_recon`_ above.

**Derivation (inside `reconcile()`):**
1. **transfer pairing** — two greedy 1:1 rules, each within `|Δamount| ≤ $0.01`
   and dates within ±3 days, scoped to one user:
   - *Rule A (credit-card payment):* a credit `payment` leg ↔ a checking/savings
     `withdrawal` leg.
   - *Rule B (bank self-transfer):* a checking/savings `deposit` ↔ a
     checking/savings `withdrawal` when **both** memos contain a transfer keyword
     (`zelle`, `transfer`, `xfer`, `wire`, `ach trnsfr`). The keyword guard is
     what keeps a real paycheck of the same amount from being mistaken for a
     transfer.
   Both matched legs get `is_internal_transfer=1` and a shared `transfer_group_id`.
2. **flow-type correction** — any paired leg's `flow_type_recon` becomes
   `transfer`, regardless of what the source/LLM first called it. This is what
   fixes the asymmetry where the outgoing leg was already `transfer` but the
   incoming self-Zelle deposit had leaked into `income`. Unpaired rows keep their
   raw `flow_type`.
3. **dedup** — conservative and cross-source only: rows sharing a `dedup_hash`
   across **different** `source` values collapse onto the most authoritative
   (statement over Plaid); the rest get `is_duplicate=1`. Same-source repeats are
   left alone.
4. **sign** — `signed_amount` from `(account_type, section_type)`, same logic as
   `v_transactions_signed`.

**Rebuild model — `rebuild_recon(user_id)`:** a full, deterministic rebuild of
**one user's** rows (`DELETE … WHERE user_id=?` then recompute). Per-user because
pairing is within-user anyway and it bounds the blast radius. Triggers:
- at the end of every ingest (statement / Plaid) for the ingested user;
- manual CLI `python -m src.cli rebuild-recon [--user <id>]` (no `--user` loops
  over `SELECT DISTINCT user_id` for a one-time backfill);
- a **lazy freshness guard** on read — if raw's `max(ingested_at)` for the user
  is newer than recon's, rebuild before serving.

It is **not** a scheduled job: recon is a pure function of raw, so it only needs
to run when raw changes. A timer would just recompute identical output.

**View — `v_transactions_recon`:** joins `transactions ⨝ transactions_recon` on
`id`, exposing raw columns + recon flags + `signed_amount`. **All consumers read
this view, not the raw table.** A convenience query (or second view) pre-excludes
`is_internal_transfer` and `is_duplicate` for the common "real spending/income"
case.

**Supersedes:** the query-time transfer pairing currently in
`dashboard_queries.py` moves into `reconcile()`; the dashboard then reads the
recon view. Same logic, computed once and shared across all surfaces.

---

## Deferred concerns

Things we explicitly do NOT resolve at **raw** storage time, by design. Where a
concern is materialized later, it's resolved in the _Reconciliation layer_ above
— never by mutating raw rows.

### Sign convention

Storage is convention-free. `amount` is a magnitude; `section_type` carries
direction. Whatever signed view you want — "money out from user's net worth,"
"per-account balance flow," "as displayed on the source" — is derivable from
these two columns at query time. The `v_transactions_signed` view provides
per-account balance flow out of the box; other conventions are similar
2-line CASE expressions over `section_type`.

Why: lock-in. A signed `amount` column would bake one convention into every
row and require a data migration to change. The magnitude + categorical
approach changes conventions with a one-line edit in a view.

### Cross-source duplication

If a user has both Plaid and a statement covering the same period, both
ingest, and the same transaction lands twice in the table (once with
`source='plaid'`, once with `source='statement_pdf'`). Storage does NOT
collapse them.

Why: a content match isn't the same as a duplicate. Plaid sometimes shows
authorization holds; statements show the final posted amount; they can
disagree by a few cents and dates can shift by a day. Collapsing at write
time forces a wrong policy on every consumer.

The `dedup_hash` column (`md5(account_id|date|amount|description)`) is
stored as a signal — queries can `GROUP BY dedup_hash` to surface candidates
— but no automatic collapse happens at the raw layer. A user-level "trust
Plaid for the last 30 days, trust statements before that" rule lives in the
_Reconciliation layer_ (`is_duplicate`), not in raw storage.

### Inter-account transfer pairing

A credit-card payment generates two rows: one withdrawal on the checking
account and one payment on the CC account. They're real and both belong in
the table. Storage does NOT pair them.

Why: pairing depends on lookup windows (same day? ± 3 days?), amount
tolerance, and which accounts the user considers "internal." That's policy,
and it must not mutate the raw legs — both rows stay in `transactions` exactly
as ingested.

Resolution: materialized in the _Reconciliation layer_ above
(`is_internal_transfer` + `transfer_group_id`), using `abs(amount_a -
amount_b) ≤ 0.01` AND `account_a != account_b` AND `date` within window.
Consumers exclude paired legs from spending/income aggregates by filtering the
recon view. A semantic test like "did your net worth change?" doesn't even
need the pairing — it just subtracts `sum(income)` from `sum(spending)`.

### Category synonym normalization

The LLM may produce `"Dining"` one run and `"Restaurants"` the next on
similar rows. Storage keeps both verbatim — no canonicalization at write
time. Periodic review of category distribution suggests promotions to the
preferred-list prompt; once promoted, future ingests snap consistently. The
existing rows aren't rewritten; query-layer normalization can collapse them.

### Account-balance recomputation

We don't store running balance or per-period balance snapshots. The
`v_transactions_signed` view derives balance flow from rows; a query like
"my Chase checking balance over time" is `SUM(account_flow) OVER (ORDER BY date)`
plus the earliest known prev_balance. Snapshots are a query-time concern.

---

## Idempotency & retry

- **mtime guard.** A file is skipped if its mtime is unchanged AND the prior
  parse was clean. Files with `parse_error` are always re-tried.
- **Empty-result contract.** `replace_file_transactions(name, [])` legitimately
  means "this statement has no activity" and clears prior rows. Callers must
  only invoke it after a *successful* parse — parse failures must short-circuit
  before reaching this call (already gated on `error is None`).
- **Account upsert is idempotent.** Re-ingesting overwrites bank/name on the
  same `account_id`, so prompt-quality improvements propagate without
  re-keying.
- **Plaid upserts on `transaction_id`.** Re-syncing the same window
  overwrites by primary key — no duplicates.

---

## Tests

Layered. `tests/test_*.py`.

### Unit tests (fast, no LLM, no Plaid network)

- Date / amount / magnitude normalization edge cases.
- Last-4 derivation from masked patterns: `"XXXX XXXX XXXX 7370"`,
  `"Account ending in 0418"`, `"123456789012"`, etc.
- Reconciliation arithmetic across all tier boundaries (clean / warn / fail),
  per-account balance flow for credit and checking.
- `flow_type` and `section_type` normalization.
- mtime guard: skips clean files, retries failed files, processes new files.
- `replace_file_transactions([])` clears prior rows; scoped to `source_file`.
- Plaid transformation: synthetic Plaid responses → assert magnitude / section_type
  / flow_type / category / notes match expectations across CC and checking cases.
- Plaid → storage round-trip: build a Plaid-style `Transaction`, upsert,
  read back, confirm all Option-5 fields survive.

### Integration tests (slow, real LLM, opt-in)

Gated on `RUN_INTEGRATION=1`. One test per statement type. Each test
ingests the fixture in isolation (tmp DB) and asserts:

- account mask / bank / type
- ≥ 1 transaction extracted, no `flow_type=unknown`, all `section_type` populated
- all `amount >= 0` (Option 5 contract)
- no `parse_error`; `recon_warning` empty or within tier-1

Fixture files (in `data/statements/`, gitignored):

| File | Bank | Type | Mask |
|---|---|---|---|
| `20250124-statements-7370-.pdf` | Chase | Checking | 7370 |
| `20260404-statements-0418-.pdf` | Chase | Credit | 0418 |
| `eStmt_2026-03-23.pdf` | Bank of America | Credit | 8373 |
| `eStmt_2026-03-12.pdf` | Bank of America | Checking | 0790 |

Tests print rendered text and parsed activity summaries to stdout so the
user can eyeball numbers. Run with `-s` to see the output.

---

## Known limitations

- **One account per PDF.** Combined statements are refused. Multi-account
  PDFs would need per-section parsing in Step 4.
- **No OCR.** Image-only / scanned PDFs produce empty text from pymupdf4llm
  and are refused with `parse_error`.
- **Statements without summary totals** can't be reconciled. They pass with
  no warning even if extraction missed rows (rare on monthly statements;
  common on interim / on-demand exports).
- **LLM nondeterminism on classification.** Reconciliation catches gross
  errors (a missing transaction, a payment classified as a purchase). It
  doesn't catch a row whose `category` swings between `"Dining"` and
  `"Restaurants"` across runs — see _Deferred concerns_.
- **Currency.** All `amount` values assumed USD. Foreign-currency rows are
  stored at the USD-equivalent the source shows; the original currency /
  amount / FX rate is captured in `notes` for audit.
- **Plaid sandbox vs. production.** `PlaidClient` is hard-coded to
  production today. A future flag would let testers run against sandbox.

---

## Implementation status

As of 2026-05-27, on `main`:

- [x] Schema: `accounts`, `transactions` (magnitude + `section_type` + `notes`),
      `file_sources`, `v_transactions_signed` view (with `cash_advance` in the
      CC up-set)
- [x] Statement ingestion: render → metadata extract → multi-account guard →
      last-4 derivation → chunked activity extraction (10K chunks, 3-attempt
      retry that propagates on failure) → defensive normalization (whitespace
      magnitude, cross-domain `section_type` remap, summary-row denylist) →
      two-tier reconciliation by `section_type` (including `total_cash_advances`)
      → commit / refuse
- [x] LLM transport hardening: 120s timeout + 3-attempt retry on
      `APITimeoutError` / `APIConnectionError` inside `_llm_json`
- [x] CSV ingestion: magnitude + best-effort section_type from flow_type
- [x] Plaid ingestion: pure transformation, magnitude + section_type derived
      from sign + PFC (incl. `transaction_code='cash'` → `cash_advance` on
      credit), category mapped to preferred list, notes capture check_number
      / currency / pending / authorized_date
- [x] mtime guard with retry-on-error (retries `parse_error` files;
      `recon_warning` files are not auto-retried)
- [x] Integration tests for 4 fixture statements
- [x] Unit tests: 85 ingestion/storage + 31 Plaid = 116 total

Current ingest health on real data:

- 58 statement PDFs across 4 accounts (2 checking, 2 credit)
- 0 refusals, 2 recon warnings (both well under the $5 / 5% tolerance)
- 1,498 transactions

Not yet built (out of Phase 1A scope unless explicitly pulled in):

- [ ] Cross-source dedup / collapse views — deferred to query / MCP layer
- [ ] Inter-account transfer pairing — deferred to query / MCP layer
- [ ] Promotion workflow for invented categories → preferred list
- [ ] Plaid sandbox toggle
- [ ] Retry `recon_warning` files on prompt / parser improvements (today the
      mtime guard only retries `parse_error` files)
