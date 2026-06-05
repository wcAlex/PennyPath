# User overrides & category rules

How the user corrects mis-categorized transactions without ever mutating raw
storage. A chat-driven overlay that sits between system reconciliation and the
single read projection every consumer (dashboard, chat tools, monthly analysis)
already goes through.

Phase 1C addition. Builds on the storage contract in `design/storage.md` and
the chat agent in `design/chat_agent.md`.

---

## Goal & scope

The LLM-driven category assignment at ingest time is wrong some of the time.
"PACIFIC TABLE NY" lands as `Dining` but is actually the user's kids' tutor.
The user must be able to fix it from chat — single row, several rows, or "any
row matching this merchant, now and forever" — without us ever rewriting the
raw `transactions` row. Fixes are reversible, auditable, and survive
re-ingestion.

**In scope:**

- A per-transaction override layer that overrides one or more of `category`,
  `flow_type`, `is_excluded` while raw stays immutable.
- A rule layer for "this merchant is always X" — applies to all past matches
  and is re-applied to new rows as they ingest.
- An append-only audit trail for every override and rule mutation, with the
  chat session that produced it. Lets the user inspect "why is this row tagged
  X?" and undo if a rule goes wrong.
- Chat tools so the agent can preview, write, list, and unwind overrides /
  rules in plain conversation. No separate page UI in this phase.
- A single read view (`v_transactions_effective`) that every existing reader
  picks up — dashboard and chat agree by construction.

**Out of scope (this phase):**

- A standalone "Overrides & Rules" page in the web UI. Chat is the surface.
- Editing `description`, `notes`, `amount`, `date` — the source is the source;
  we don't rewrite money facts.
- Overriding the `is_internal_transfer` / `is_duplicate` flags (those are
  recon-owned; user-side equivalent is `is_excluded`).
- Cross-user / shared rules. Rules are strictly per-user.
- A regex-based `match_type`. Start with `description_exact`,
  `description_substring`, and `merchant_canonical`. Regex can come later.

---

## End-to-end flow

```
                                                      ┌─ user chat: ─────────────┐
                                                      │  "PACIFIC TABLE is        │
                                                      │  Kids Education, not      │
                                                      │  Dining"                  │
                                                      └────────────┬──────────────┘
                                                                   │
                                                                   ▼
                                           ChatAgent.run() ── new tools ──▶ OverrideStore / RuleStore
                                                                   │             │
                                                                   │             ▼
                                                                   │      transaction_overrides
                                                                   │      category_rules
                                                                   │      override_audit
                                                                   │             │
                                                                   ▼             │
                                                       reply (+ confirmation)    │
                                                                                 │
   ┌─ raw (immutable) ──────────────┐                                            │
   │  transactions                  │                                            │
   └──────────────┬─────────────────┘                                            │
                  │  reconcile()                                                 │
                  ▼                                                              │
   ┌─ system recon (pure fn of raw)─┐                                            │
   │  transactions_recon            │                                            │
   └──────────────┬─────────────────┘                                            │
                  │              ┌─ user overlay (chat-driven) ──────────────────┘
                  │              │  transaction_overrides   ◀── what the view reads
                  │              │  category_rules          ◀── source-of-truth patterns
                  │              │  override_audit          ◀── append-only history
                  │              │
                  ▼              ▼
   ┌─ read projection ─────────────────────────────────────────────────────┐
   │  v_transactions_effective    (raw ⨝ recon ⨝ overrides)                 │
   └──────────────┬─────────────────────────────────────────────────────────┘
                  │
       ┌──────────┴──────────┐
       ▼                     ▼
   dashboard            chat tools
   (donut, list,        (9 existing +
   cashflow, trend)     7 new override
                         /rule tools)
```

Precedence at read time, per column:

```
explicit override (source_kind='user_manual')
    > rule-materialized override (source_kind='rule')
    > system recon (flow_type_recon)
    > raw (category, flow_type)
```

`user_manual` and `rule` both live in the same `transaction_overrides` table
and share its PK. An explicit upsert just replaces a rule-materialized row —
there is never more than one override per (user_id, transaction_id), so the
view stays a single `LEFT JOIN`.

---

## Layering — where the overlay sits

| Layer | Owner | Mutability | Source |
|---|---|---|---|
| `transactions` (raw) | ingest | immutable | what Plaid / PDF / CSV said |
| `accounts` (raw) | ingest | immutable per row | derived per-source identifier |
| `transactions_recon` (system) | `src/reconciler.py` | rebuildable; pure fn of raw | transfer pairing, dedup, sign |
| **`transaction_overrides` (user)** | **chat agent** | **upsert / delete; survives ingest** | **explicit clicks + materialized rule applications** |
| **`category_rules` (user)** | **chat agent** | **CRUD** | **patterns the user wants applied "always"** |
| **`override_audit` (user)** | **all override / rule writers** | **append-only** | **every mutation, with provenance** |
| `v_transactions_effective` (view) | none — derived | — | raw ⨝ recon ⨝ overrides |

Boldface rows are new.

The user overlay is **orthogonal to the reconciler**. `reconcile()` keeps its
"pure function of raw" contract; rebuilding recon does not touch overrides.
Re-ingest re-runs recon and (separately) re-applies active rules to any new
rows — `user_manual` overrides survive both untouched.

---

## Data schema

`data/transactions.db` (SQLite). Three new tables + one renamed view.

### `transaction_overrides`

One row per (user, transaction). Single source for both manual overrides and
rule-materialized overrides.

| column | type | notes |
|---|---|---|
| `user_id` | TEXT | tenant scope; part of PK |
| `transaction_id` | TEXT | FK to `transactions.id`; part of PK |
| `category` | TEXT NULL | NULL = don't override category |
| `flow_type` | TEXT NULL | NULL = don't override flow_type (closed enum below) |
| `is_excluded` | INTEGER NULL | 1 = drop from spending/income totals; NULL = no opinion |
| `source_kind` | TEXT | `'user_manual'` \| `'rule'` |
| `source_rule_id` | INTEGER NULL | FK to `category_rules.id` when `source_kind='rule'`; NULL otherwise |
| `note` | TEXT | optional user-visible reason (`"this is for the kids' tutor"`) |
| `created_at` | TEXT | ISO timestamp |
| `updated_at` | TEXT | ISO timestamp |
| PRIMARY KEY | | `(user_id, transaction_id)` |

`source_kind='user_manual'` always wins on upsert. A rule's materializer
explicitly **skips** rows that already have a `user_manual` override, so a
manual correction is never silently undone by a rule re-run.

Why all three override columns NULLable: a common case is "change just the
category, leave flow_type alone". The view's per-column `COALESCE` honors
that.

### `category_rules`

Patterns the user wants applied to all matching transactions, past and future.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `user_id` | TEXT | tenant scope (indexed) |
| `match_type` | TEXT | closed enum: `description_exact` \| `description_substring` \| `merchant_canonical` |
| `match_value` | TEXT | the string to match (case-insensitive) |
| `target_category` | TEXT NULL | what to set on matches (NULL = don't override) |
| `target_flow_type` | TEXT NULL | |
| `target_is_excluded` | INTEGER NULL | |
| `priority` | INTEGER | higher wins on multi-rule overlap; default `100` |
| `active` | INTEGER | 1 = applied; 0 = paused. Pausing keeps the audit chain intact |
| `note` | TEXT | optional user-visible reason |
| `created_at` | TEXT | |
| `updated_at` | TEXT | |

At least one of `target_category` / `target_flow_type` / `target_is_excluded`
must be non-NULL — a rule that sets nothing is rejected at write time.

### `override_audit`

Append-only. Every override and rule mutation appends one row; rule
materializations append one row per affected transaction. Lets the user
inspect history and undo.

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `user_id` | TEXT | tenant scope (indexed) |
| `at` | TEXT | ISO timestamp |
| `action` | TEXT | closed enum below |
| `transaction_id` | TEXT NULL | for per-tx actions and rule materializations |
| `rule_id` | INTEGER NULL | for rule actions or rule-driven materializations |
| `before_json` | TEXT | snapshot of the affected override row before the action (`"null"` if none existed) |
| `after_json` | TEXT | snapshot after the action |
| `chat_session_id` | TEXT NULL | provenance — which chat session triggered it |
| `chat_message_id` | TEXT NULL | optional reference to the specific user message |
| `note` | TEXT | optional |

The audit insert runs in the same SQLite transaction as the override / rule
mutation, so the log can never drift from the data.

### View — `v_transactions_effective`

Replaces `v_transactions_recon`. Same recon overlay + new override columns.
All readers switch their `FROM` clause; they keep working unchanged otherwise.

```sql
DROP VIEW IF EXISTS v_transactions_effective;
CREATE VIEW v_transactions_effective AS
SELECT t.id,
       t.date,
       t.amount,
       t.description,
       COALESCE(o.category,  t.category)                     AS category,
       t.account_type,
       t.account_id,
       t.user_id,
       t.section_type,
       COALESCE(o.flow_type, r.flow_type_recon, t.flow_type) AS flow_type_recon,
       COALESCE(r.signed_amount, 0)                          AS signed_amount,
       COALESCE(r.is_internal_transfer, 0)                   AS is_internal_transfer,
       r.transfer_group_id,
       COALESCE(r.is_duplicate, 0)                           AS is_duplicate,
       COALESCE(o.is_excluded, 0)                            AS is_user_excluded,
       o.source_kind                                         AS override_source,
       o.source_rule_id                                      AS override_rule_id,
       o.note                                                AS override_note
FROM transactions t
LEFT JOIN transactions_recon    r ON r.id = t.id
LEFT JOIN transaction_overrides o ON o.user_id = t.user_id AND o.transaction_id = t.id;
```

The view stays a 2-LEFT-JOIN read — overrides participate via a single join
because rule applications are pre-materialized into the same overrides table.

`v_transactions_recon` is dropped. Two callers in code change their `FROM`
clause (see _Reader changes_); no other behavior shifts because the recon
columns are still there and `COALESCE` defaults match the dropped view's
defaults.

---

## Closed enums

### `match_type`

| value | matches against | when to use |
|---|---|---|
| `description_exact` | raw `description` literal (case-insensitive equality) | rare; "exact string this card prints" |
| `description_substring` | raw `description` (case-insensitive contains) | free-text patterns the user types ("rent", "venmo to mom") |
| `merchant_canonical` | `_canonical_merchant(description)` equality (case-insensitive) | the common case — "this merchant, however the bank writes it" |

Why three: bank descriptions are noisy and inconsistent across statements
and Plaid. `TST*PACIFIC TABLE #4421 NY`, `Pacific Table 4421 New York NY`,
and `PACIFIC TABLE BROOKLYN NY 12345` are the same merchant. The existing
`_canonical_merchant` helper in `src/dashboard_queries.py:762` already strips
processor prefixes (`tst*`, `sq*`, `paypal*`, `venmo*`, `amzn mktp*`),
trailing `#1234 …`, trailing state codes, and trailing zips. All three
strings collapse to `Pacific Table`.

If the agent picked `description_substring` with `match_value` copied from a
raw row, the rule would silently miss rows from a different statement /
store #. `merchant_canonical` makes the rule robust by design.

The agent's default when the user points at a row and says "always
categorize this place as …" is `merchant_canonical` with `match_value =
_canonical_merchant(row.description)`. `description_substring` stays
available when the user explicitly types a non-merchant pattern.

### `source_kind`

| value | meaning |
|---|---|
| `user_manual` | the user explicitly set this row's override; wins over rule |
| `rule` | this row's override was materialized from a rule (`source_rule_id` set) |

### `flow_type` (override target)

Same closed enum as `transactions.flow_type`: `spending` / `transfer` /
`interest` / `fee` / `refund` / `income`. The override layer never invents
new flow types; it just lets the user pick a different one for a row.

### Audit `action`

| value | written by |
|---|---|
| `set_override` | manual override upsert |
| `clear_override` | manual override delete |
| `create_rule` | rule create (including materialization batch) |
| `edit_rule` | rule update (target_*, match_*, priority, active) |
| `delete_rule` | rule delete (with rule_unmaterialize entries for each affected tx) |
| `pause_rule` | `active 1 → 0` |
| `resume_rule` | `active 0 → 1` |
| `rule_materialize` | system applied a rule to a tx (auto-batch row) |
| `rule_unmaterialize` | system removed a rule-sourced override (delete-rule or rule edit no longer matches) |

---

## Precedence & tie-break

The view's `COALESCE` ordering is the precedence:

```
COALESCE(o.category,  t.category)
COALESCE(o.flow_type, r.flow_type_recon, t.flow_type)
COALESCE(o.is_excluded, 0)
```

Inside `transaction_overrides`, the PK enforces one row per
(user_id, transaction_id), so we never need to break ties in SQL. The
**writer** breaks ties:

1. **Manual upsert** replaces any existing row at that PK regardless of its
   prior `source_kind`. Audit captures the before/after.
2. **Rule materializer** for a given rule walks matching rows and, for each:
   - If no override row exists → INSERT with `source_kind='rule'`.
   - If `source_kind='rule'` exists for *this* rule → idempotent UPDATE (no
     audit row if columns unchanged).
   - If `source_kind='rule'` exists for a *different* rule with lower
     `priority` → UPDATE to this rule (audit row written; the old rule is no
     longer materialized on this tx).
   - If `source_kind='rule'` exists for a *different* rule with equal or
     higher `priority` → skip (the higher-priority rule wins).
   - If `source_kind='user_manual'` exists → **always skip**. A manual
     decision is sacred.

This keeps the read path one join while preserving manual-wins, rule
priority, and idempotent re-materialization.

---

## Materialization triggers

Rules don't match at read time. They're pre-applied into
`transaction_overrides` at three points so the view stays a single join:

1. **Rule create / edit.** The write API walks current matching rows and
   upserts overrides. For an edit, rows that no longer match the new pattern
   are unmaterialized (their `source_kind='rule', source_rule_id=N` overrides
   deleted, with `rule_unmaterialize` audit rows).
2. **Rule delete.** `DELETE FROM transaction_overrides WHERE source_rule_id =
   N AND user_id = ?` plus one `rule_unmaterialize` audit row per affected
   tx. `user_manual` rows are untouched.
3. **After each ingest.** `apply_rules_to_new(user_id, since_ingested_at)`
   runs after the per-user recon rebuild. It scans only rows newer than
   `since_ingested_at` against the user's `active` rules. Cheap because the
   scan window is the just-ingested set, not the whole history.

For #3, the trigger lives at the end of `ingest()` in
`src/statement_ingester.py` and `src/plaid_client.py` — same call site as
the existing `rebuild_recon(user_id)`. Rules apply after recon so a paired
transfer leg's `flow_type_recon = 'transfer'` is set before any rule that
might target it (the rule's `target_flow_type` would override that recon
flow only if the user explicitly wanted it; the common case is rules touch
`target_category` and leave flow alone).

Rules are **not** applied lazily on read. The freshness guard pattern
(`ensure_recon_fresh`) doesn't extend here because rule application is a
write, not a derivation — and we want the audit row written at materialize
time, not at read time.

---

## Chat flow — every edit comes from chat

The agent's job around overrides is to **discover scope, confirm with the
user, then write**. The discover step is what makes rules feel safe.

### Single-row pattern

```
User (looking at a row): "this one isn't Dining, it's Kids Education"

Agent: → set_override(transaction_id='abc123', category='Kids Education')
       → audit row {action: 'set_override', before: null, after: {...}}
       "Done. This 2026-04-15 PACIFIC TABLE row is now Kids Education.
        (Reply with 'show my rules' or 'what changed?' anytime to review.)"
```

### Pattern correction — agent prompts for scope

```
User: "Pacific Table is actually Kids Education, not Dining"

Agent: → preview_rule_matches(match_type='merchant_canonical',
                              match_value='Pacific Table')
       "I found 12 transactions matching the merchant 'Pacific Table',
        going back to 2025-08. They're all currently Dining. How should I
        handle this?

          1. Just the one I was looking at on 2026-04-15.
          2. All 12 past matches.
          3. All 12 past matches AND set a rule so future ones are
             auto-fixed."

User: "option 3"

Agent: → create_category_rule(match_type='merchant_canonical',
                              match_value='Pacific Table',
                              target_category='Kids Education',
                              apply_to_past=true)
       (rule INSERT + 12 override INSERTs + 1 create_rule audit +
        12 rule_materialize audits, all in one DB transaction)
       "Done — Pacific Table will be Kids Education from now on.
        I've also fixed the 12 past matches.
        (Reply with 'show my rules' or 'what changed?' anytime to review.)"
```

If the user is unambiguous up front ("fix all past and future Pacific Table
→ Kids Education"), the agent skips the menu and just confirms the count
before writing:

```
Agent: → preview_rule_matches(...)
       "Found 12 past matches. Setting the rule and fixing all of them — confirm?"
User: "yes"
Agent: → create_category_rule(...)
```

### Undo

```
User: "actually undo that — Pacific Table is fine as Dining"

Agent: → list_category_rules() to find the rule_id
       → delete_category_rule(rule_id=N)
       (rule DELETE + 12 override DELETEs + 1 delete_rule audit +
        12 rule_unmaterialize audits)
       "Removed the rule and restored all 12 rows. Their categories are
        back to whatever they were originally tagged as."
```

### Inspection

```
User: "what rules do I have?"

Agent: → list_category_rules(active_only=true)
       "You have 3 active rules:"
       [ table block:
         Rule | Match (canonical) | Sets             | Affects | Created
         #4   | Pacific Table     | Kids Education   | 12 rows | 2026-05-30
         #2   | Venmo To Mom      | Transfer (excl.) |  8 rows | 2026-04-12
         #1   | Verizon           | Utilities        | 14 rows | 2026-03-01 ]
       "Want to remove or edit any?"
```

```
User: "why is this row Kids Education?" (clicked from a transactions list)

Agent: → list_override_history(transaction_id='abc123', limit=5)
       "On 2026-05-30 you set a rule that matches 'Pacific Table' →
        Kids Education. Before that, the row was tagged Dining (the
        category your statement gave it)."
```

```
User: "what did I change in the last week?"

Agent: → list_override_history(since='2026-05-23', limit=20)
       "Here's the activity:"
       [ table block:
         When        | What            | Row / Rule       | Before → After
         2026-05-30  | created rule    | Pacific Table    | Dining → Kids Education
         2026-05-30  | rule applied    | 12 rows          | (auto)
         2026-05-28  | manual override | 2026-04-15 Uber  | Transportation → Travel ]
```

Discoverability without UI: the confirmation reply on any write ends with
one line — *"(Reply with 'show my rules' or 'what changed?' anytime to
review.)"* Only on writes, not on every reply.

A small `✎` badge on overridden rows in the existing dashboard transactions
list, click → opens the chat drawer pre-filled with "Tell me about this
row", is a cheap follow-up but not required for v1.

---

## Chat tool surface

Seven new tools join the existing 9-tool registry (`src/chat_tools.py`). Same
`ToolSpec` contract, same `to_openai_tools()` adapter, same internal-only
constraint — no external client speaks to the registry.

| tool | shape | purpose |
|---|---|---|
| `preview_rule_matches` | `(match_type, match_value, target_category?, target_flow_type?, target_is_excluded?)` → `{matches: [{id, date, description, category, flow_type}], total_matched, sample_before_after}` | dry run — what would this rule change? agent shows the count and a small sample before asking confirmation |
| `set_override` | `(transaction_id, category?, flow_type?, is_excluded?, note?)` → `{ok, before, after}` | single explicit override (or update / clear individual fields) |
| `set_overrides_bulk` | `(transaction_ids[], category?, flow_type?, is_excluded?, note?)` → `{ok, count, before_sample, after_sample}` | "fix these N rows" without making a rule |
| `clear_override` | `(transaction_id)` → `{ok, before}` | undo a manual override |
| `create_category_rule` | `(match_type, match_value, target_category?, target_flow_type?, target_is_excluded?, note?, priority?, apply_to_past=true)` → `{rule_id, materialized_count}` | rule + (optional) materialize past matches |
| `list_category_rules` | `(active_only=true)` → `{rules: [{id, match_type, match_value, target_*, priority, active, affects_count, created_at, note}]}` | what's set |
| `delete_category_rule` | `(rule_id)` → `{ok, unmaterialized_count}` | rule cleanup; unwinds materialized overrides; `user_manual` rows untouched |
| `list_overrides` | `(transaction_id?, since?, limit=20)` → `{overrides: [{transaction_id, date, description, category_before, category_after, flow_before, flow_after, is_excluded, source_kind, source_rule_id, note, updated_at}]}` | "what's been overridden?" — shows the effective state |
| `list_override_history` | `(transaction_id?, rule_id?, since?, limit=20)` → `{events: [{at, action, transaction_id, rule_id, before, after, chat_session_id, note}]}` | append-only audit — answers "what changed?" and "why is this row X?" |

Note: that's nine new tools (the four mutation tools plus five list / preview
tools). Group them in the registry's prompt description by purpose so the LLM
can find them: *Mutation*, *Rules*, *Inspection*.

Validation lives in `src/chat_tools.py` next to the existing helpers:

- `match_type` must be in the closed enum.
- `match_value` non-empty, trimmed.
- At least one `target_*` non-NULL on rule create.
- `transaction_id` must exist for the user (or skip with structured error and
  suggested neighbors).
- `category` validated against `list_categories` (reuse the existing
  `_check_category` helper); unknown categories return `{error, suggestion}`
  for self-correction.

System-prompt rule to encode in `src/prompts/chat_drill_down.txt`:

> When the user wants to fix a category, never write immediately if the
> change could affect more than one row. Call `preview_rule_matches` first,
> announce the scope ("found 12 matches"), and offer the three options
> (single / all past / all past + future). Only the single-row case skips
> the preview.

---

## Audit trail — what's logged and how it's used

Every mutation writes one or more rows into `override_audit` in the same DB
transaction as the data write. The chat agent supplies
`chat_session_id` + `chat_message_id` (from the existing
`ConversationStore.get_current_session_id()`); the writer threads them
through.

| Mutation | Audit rows written |
|---|---|
| `set_override` upsert | 1 `set_override` |
| `clear_override` | 1 `clear_override` |
| `set_overrides_bulk` | N `set_override` (one per affected tx) |
| `create_category_rule` (with `apply_to_past=true`) | 1 `create_rule` + N `rule_materialize` |
| `edit_rule` that broadens matches | 1 `edit_rule` + (new-matches) `rule_materialize` + (no-longer-matching) `rule_unmaterialize` |
| `pause_rule` | 1 `pause_rule` + N `rule_unmaterialize` |
| `resume_rule` | 1 `resume_rule` + N `rule_materialize` |
| `delete_category_rule` | 1 `delete_rule` + N `rule_unmaterialize` |
| Ingest post-hook rule application | N `rule_materialize` (no top-level row — the rule already has its `create_rule` entry) |

**Three uses, all chat-driven:**

1. **Inspection** — `list_override_history` answers the user's questions.
2. **Provenance on a single row** — the agent walks the audit by
   `transaction_id` to explain "this row is Kids Education because of rule
   #4, set on 2026-05-30".
3. **Forensic undo** — if a rule turns out wrong but the user has since made
   manual edits we don't want to lose, the agent can offer "undo just the
   rule, keep manual overrides" by replaying `before_json` only on rows
   whose audit chain shows pure `rule_materialize` history and no
   subsequent `set_override`.

The audit table is never read in the hot path. It's append-only and grows
linearly with user activity — no GC needed at this phase.

---

## Reader changes

The renaming is the only required code change in readers.

**`src/dashboard_queries.py`**

- `_fetch_recon_rows`'s SQL string: `FROM v_transactions_recon` →
  `FROM v_transactions_effective`. Select-list grows by `is_user_excluded`,
  `override_source`, `override_rule_id`, `override_note`.
- `list_transactions_signed` adds `is_user_excluded` to the per-row dict it
  emits, so consumers can show the `✎` badge.
- `_excluded_from_spending` and `_excluded_from_income` add
  `or row["is_user_excluded"]` to their predicate. Spending/income totals
  immediately respect user exclusions.

**`src/chat_tools.py`**

- `_known_categories` and any direct `FROM v_transactions_recon` SQL switch
  to `v_transactions_effective`.
- Tool handlers that filter by `category` now see the overridden category
  for free (the COALESCE happens in the view).
- Add `or r["is_user_excluded"]` everywhere the handlers currently check
  `r["is_internal_transfer"] or r["is_duplicate"]`.

That's it for reads. No public API change in `dashboard_queries`; chat tools
gain the new ones from the previous section.

---

## Reconciler unchanged

`src/reconciler.py` keeps its current contract:

- **Pure function of raw.** Override / rule tables are never read.
- `rebuild_recon(user_id)` and `ensure_recon_fresh(user_id)` keep their
  signatures. Re-ingest re-runs recon and the rule re-apply step runs *after*
  recon completes, against the user's just-ingested rows.

This is the load-bearing invariant. If recon ever reads overrides, the
"recon is a pure function of raw" property breaks and so do its rebuild
semantics. Keep them orthogonal.

---

## Files to add or modify

| Path | Change | Why |
|---|---|---|
| `src/storage.py` | extend `TransactionStore.init_db` to create `transaction_overrides`, `category_rules`, `override_audit`, and replace `v_transactions_recon` with `v_transactions_effective`. Add `OverrideStore`, `RuleStore`, `AuditStore` classes (or one consolidated `OverrideStore` if cleaner) | schema + write API |
| `src/overrides.py` | new — pure-Python helpers: rule matching (`match_rule(row, rule) -> bool`), materializer (`materialize_rule(user_id, rule_id)`), unmaterializer, audit row helpers. Keeps SQL out of the agent | reusable from CLI, ingest hook, and chat tools |
| `src/chat_tools.py` | add 9 new tools (4 mutation + 5 inspection per the table above), schemas + handlers + descriptions. Switch existing handlers to `v_transactions_effective` and the `is_user_excluded` predicate | chat surface |
| `src/dashboard_queries.py` | switch `FROM v_transactions_recon` to `FROM v_transactions_effective`; extend `list_transactions_signed` and the two exclusion predicates | reader change |
| `src/statement_ingester.py` | call `apply_rules_to_new(user_id, since_ingested_at)` at the end of each ingest, after `rebuild_recon(user_id)` | rule application on new rows |
| `src/plaid_client.py` | same call site addition | same |
| `src/prompts/chat_drill_down.txt` | add the "preview before any multi-row write" rule, plus the 3-option scope prompt and the discoverability footer | agent behavior |
| `src/templates/_chat_drawer.html` | optional in v1 — render a `✎` badge when `override_source` is set on a row in a `table` block (chat-tool result), clicking it pre-fills "Tell me about this row" | minor UX nudge |
| `src/cli.py` | optional — `python -m src.cli rebuild-overrides [--user <id>] [--rule <id>]` for a one-shot re-materialize after regex / canonicalizer changes | maintenance |
| `tests/test_overrides.py` | new — schema + view shape, precedence, manual-wins, rule materialize / unmaterialize, idempotent re-materialize, audit shape | core correctness |
| `tests/test_rule_matching.py` | new — `description_exact` / `_substring` / `merchant_canonical` behaviors on the noisy-description cases above | matcher correctness |
| `tests/test_chat_tools.py` | extend — preview, single set, bulk, rule create / list / delete, override history reads | tool surface |
| `tests/test_dashboard_queries.py` | extend — `is_user_excluded` participates in spending / income totals; overridden `category` flows through donuts and lists | reader correctness |

---

## Phased implementation plan

A path that keeps the system green at every step.

**1. Schema + view rename.** `init_db` creates the three new tables and
replaces the view. Existing readers in `dashboard_queries.py` and
`chat_tools.py` switch their `FROM` clause. No new behavior; existing tests
pass with one `s/v_transactions_recon/v_transactions_effective/g`.

**2. Write API for manual overrides.** `OverrideStore.set / clear` + audit
write. Unit tests cover precedence (override wins over recon wins over raw)
and audit row shape.

**3. Reader exclusion check.** Add `or is_user_excluded` to the two
predicates. Manual exclusions immediately move money out of the donut. Test
that spending totals respect it.

**4. Rule storage + matching.** `RuleStore.create / list / delete`, the
pure `match_rule(row, rule)` predicate, and `materialize_rule` /
`unmaterialize_rule` writers. Unit tests for each `match_type` against the
noisy description cases. `merchant_canonical` reuses
`_canonical_merchant` directly.

**5. Manual-wins skip.** Materializer never overwrites `user_manual` rows.
Add the explicit test.

**6. Ingest hook.** `apply_rules_to_new(user_id, since_ingested_at)` called
from both ingest paths after `rebuild_recon`. Integration test: ingest a
fixture, see a matching rule auto-apply.

**7. Chat tools.** Add the 9 tools. Wire validation through
`_check_category` etc. Extend the system prompt with the
preview-then-confirm rule and the 3-option scope prompt.

**8. Inspection tools + audit.** `list_overrides`,
`list_override_history`, `list_category_rules`. The agent's "what rules do
I have / what changed?" replies start working.

**9. Optional UX nudge.** `✎` badge in the chat drawer's table blocks.

Each step has a small test surface and leaves the system shippable.

---

## Open items / known limitations

- **Canonicalizer drift.** If `_canonical_merchant` is later improved, an
  old `merchant_canonical` rule's stored `match_value` ("Pacific Table") may
  stop matching rows whose canonical form has shifted ("Pacific Table NY",
  say). Mitigation: bump a `canonicalizer_version` field on the rule or
  surface a CLI rebuild (`rebuild-overrides`) the user runs after a regex
  change. v1 doesn't auto-version; we accept the manual fix.
- **Rule conflicts.** Two rules can target the same merchant with different
  categories. `priority` breaks ties at materialize time. The agent should
  refuse to create a new rule that's strictly dominated by an existing one,
  or prompt the user to pick the priority — defer the exact UX to
  implementation.
- **Dangling overrides.** A future re-ingest could delete a raw `transactions`
  row whose `id` is referenced by an override. The view's `LEFT JOIN` from
  `transactions` means the orphan override simply disappears from the
  effective view, but the row lingers in the table. Periodic cleanup via
  `DELETE FROM transaction_overrides WHERE NOT EXISTS (SELECT 1 FROM
  transactions ...)` is a one-line maintenance task; not urgent.
- **History size.** `override_audit` grows linearly. At Phase 1C scale
  (one user, hundreds of overrides/month), this is negligible. If we ever
  hit a five-figure month on one user, a coarse "summarize old months"
  job (`action='audit_compact'` rolling up older entries) is the next
  evolution.
- **No "approve before apply" preview UI.** The agent confirms in prose,
  but there's no visual diff. If a rule materializes 200 rows that's a
  large change presented as a number. If users start asking for a
  before/after summary table, that's a small inspection-tool extension —
  not a v1 blocker.
- **Per-user, not shared.** No multi-user rule sharing (e.g. household).
  Out of scope until 1D auth lands.
