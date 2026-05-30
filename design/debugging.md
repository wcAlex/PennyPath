# Debugging

Hands-on instructions for poking at PennyPath's local state.

## Database

The local store lives at `data/transactions.db` (SQLite).

### 1. SQLite CLI (interactive)

```sh
sqlite3 data/transactions.db
```

Useful meta commands once inside:

```
.tables                          -- list tables
.schema transactions             -- show one table's schema
.headers on                      -- show column names in results
.mode column                     -- pretty columnar output
.mode box                        -- boxed table output
.width 12 10 30                  -- set column widths
.quit
```

Open read-only when you just want to look:

```sh
sqlite3 -readonly data/transactions.db
```

### 2. One-off query from the shell

```sh
sqlite3 -header -column data/transactions.db \
  "SELECT date, amount, description, section_type FROM transactions ORDER BY date DESC LIMIT 20;"
```

### 3. GUI

Install [DB Browser for SQLite](https://sqlitebrowser.org/):

```sh
brew install --cask db-browser-for-sqlite
```

Then File → Open → `data/transactions.db`. The *Browse Data* tab lets you click around without writing SQL.

## Starter queries

```sql
-- accounts overview
SELECT id, bank, name, type, mask FROM accounts;

-- recent transactions
SELECT date, account_type, section_type, amount, description
FROM transactions ORDER BY date DESC LIMIT 25;

-- signed flow per account per month (uses v_transactions_signed view)
SELECT account_id, substr(date,1,7) AS month, ROUND(SUM(account_flow),2) AS net
FROM v_transactions_signed GROUP BY account_id, month ORDER BY month DESC;

-- spending by category (credit purchases)
SELECT category, ROUND(SUM(amount),2) AS total, COUNT(*) AS n
FROM transactions
WHERE account_type='credit' AND section_type='purchase'
GROUP BY category ORDER BY total DESC;

-- ingestion log
SELECT filename, parse_method, tx_count, parse_error, recon_warning
FROM file_sources ORDER BY parsed_at DESC LIMIT 20;
```

## Schema reference

Inspect the live schema any time with:

```sh
sqlite3 data/transactions.db ".schema"
```

Tables: `accounts`, `transactions`, `file_sources`. View: `v_transactions_signed` (adds an `account_flow` column with the signed balance impact based on `account_type` × `section_type`).
