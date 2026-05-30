# UI Dashboard — Phase 1B Design

**Status:** Draft for review. No code yet.
**Scope:** Phase 1B per `design/phases.md` + clarifications from the 2026-05-27 design conversation.
**Owners:** Chi (design + implementation).

## 1. Goal & scope

Phase 1B ships the **annotated personal dashboard** — the visibility surface that sits on top of
the Phase 1A data layer. From `design/phases.md`:

- Four standard charts: **Spending**, **Income**, **Transactions**, **Cash Flow**.
- Internal transfers excluded from spending and income totals.
- An **LLM annotation layer** — each chart carries a short, warm annotation and, where useful, a
  gentle suggested action. Annotations are cached and refreshable.
- A **pinned-chart rendering slot** for charts pinned from chat (Phase 1C will populate it).
- A **web surface** to host it.

Phase 1B additions agreed in design review:

- A **side chat panel** on the dashboard, **collapsed by default**, with a floating bubble
  as the persistent entry point that reminds users they can drill down. When opened, the
  drawer takes a meaningful right column. Backed by the existing `/chat` endpoint — no new
  chat backend in 1B.
- A **user menu in the top-right** of the header (avatar / hamburger) that opens a settings
  surface covering: profile basics, **goal**, linked accounts, bank statement uploads, Plaid
  permissions, and other account controls. The goal is **not** shown in the default dashboard
  chrome — it lives behind this menu.
- A **single user-goal** (e.g. "stay ahead of my bills", "pay off my credit", "build good
  credit", or custom text) as the anchor for LLM-generated budget guidance. **No per-category
  numeric budgets** — the user has explicitly opted out of that mental overhead. The LLM
  produces a *rough, text-based* budget; the user can read and tweak it inside the user menu.
- **Period controls** on every chart (single month, year, or custom range), BoA-style inline
  dropdowns.

### Out of scope for 1B

- Per-category numeric budgets / "Set a budget" UI per category.
- **Payment due-date reminders.** No due-date data exists today. Deferred until Plaid is wired
  (which surfaces statement metadata including due dates).
- Drill-down chat ("why was March higher?") and chat-driven custom chart generation → **1C**.
- Auth, multi-user, cloud Postgres → **1D**.
- Native mobile → **Phase 4**.

---

## 2. Page layout

**Default state (chat drawer collapsed):**
```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  PennyPath        Dashboard                                                  [ 👤 ]  │ ← header (52px)
├──────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                      │
│  [ Spending | Income | Transactions | Cash Flow | Pinned ]                           │
│                                                                                      │
│  Spending for [All Categories ▾] in [May 2026 ▾] in [All Accounts ▾]                 │
│                                                                                      │
│  ┌─────────────────────────────────────┐  ┌─────────────────────────────────────┐    │
│  │                                     │  │  Insight ✦                          │    │
│  │           Donut chart               │  │  Dining was about $260 above your   │    │
│  │      ($ total in center)            │  │  usual this month — the main lift   │    │
│  │                                     │  │  vs. April.                         │    │
│  └─────────────────────────────────────┘  │                                     │    │
│                                           │  Try this:                          │    │
│  ┌─────────────────────────────────────┐  │  • Pick one dining night → home    │    │
│  │  Categories (sorted)                │  │    meal — keeps the habit.          │    │
│  │  Dining     $1,240   ⌀ $980         │  │  • Pause subs you haven't opened.   │    │
│  │  Groceries    $560   ⌀ $620         │  │                                     │    │
│  │  …                                  │  └─────────────────────────────────────┘    │
│  │  [ Refresh insights ↻ ]             │                                             │
│  └─────────────────────────────────────┘                                             │
│                                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────┘
                                                                                ┌─────┐
                                          floating chat bubble lives here   →   │  💬 │
                                                                                └─────┘
```

**Opened state (drawer expanded):** the main column shrinks; the drawer (~380px) slides in from
the right with the chat conversation. The floating bubble morphs into the drawer's collapse
control.

- **Header.** Reuses the existing dark bar styling. Left: logo. Center: just "Dashboard" (single
  page in 1B; no nav clutter). Right: a **user menu icon** (avatar / hamburger) that opens the
  settings surface — profile, goal, linked accounts, bank statement uploads, Plaid permissions.
  The user's goal is **not** surfaced in the default chrome; it lives behind this menu.
- **Tab bar.** Five tabs: the four standard charts plus **Pinned** (empty in 1B; reserved slot).
- **Period selector row.** Inline dropdowns *next to the title* so users see what they're
  looking at without hunting for filters. Mirrors the BoA pattern in the screenshots.
- **Chart card + insight card** sit side-by-side on desktop. Below the chart, the breakdown
  list / table is full-width.
- **Chat drawer.** **Collapsed by default.** A floating bubble bottom-right is the persistent
  entry point and the nudge that the companion is available to drill down. When opened, the
  drawer occupies a ~380px right column and the main content reflows.
- **Mobile (<768px).** Tabs become a horizontal scroll. Insight card stacks below the chart
  card. Chat drawer always opens as a full-screen overlay.

---

## 3. The four chart tabs

Each tab section below specifies: **data shape**, **visual**, **LLM insight slot**, **period
default**, **exclusions**.

### 3.1 Spending

**Visual.**
- Doughnut chart of category totals for the selected period. Total spend ($) in the center.
- A sorted category list under the donut: `category · $ spent · ⌀ $ your avg/mo` where the
  average is a **rolling 6-month per-category mean** (derived from data, no user input).
- Indigo palette to match the existing `#4f46e5` tone; "other" tail collapsed when there are
  more than 8 categories (preserve existing behavior).

**Data shape (from `GET /dashboard/spending`).**
```json
{
  "period": "2026-05",
  "total_spend": 10288.90,
  "categories": [
    { "name": "Dining",     "amount": 1240.55, "avg_6mo": 980.10 },
    { "name": "Groceries",  "amount":  560.30, "avg_6mo": 620.40 }
  ],
  "previous_period_total": 9220.10
}
```

**LLM insight slot.** One warm 1–2 sentence annotation + up to 2 gentle suggestions, both
anchored to the user's goal. Example output:

> **Insight.** Dining was your top category this month — about $260 above your usual. That's the
> main lift in your overall spend vs. April.
>
> **Try this.**
> - Pick one dining night and turn it into a home cook — keeps the habit but rests the wallet.
> - Pause a streaming sub or two you haven't opened lately (Subscriptions also crept up $35).

**Period default.** Current month.

**Exclusions.**
- Paired internal transfers (see §8).
- Rows with `flow_type IN ('transfer','income','refund','interest','fee')` — those have their
  own homes in Cash Flow / Income / Transactions.
- "Spending" = sum of `amount` for rows where `flow_type='spending'`, on either credit
  (`section_type='purchase'`) or checking/savings (`section_type='withdrawal'` net of paired
  transfers).

### 3.2 Income

**Visual.**
- Doughnut of income subcategories for the selected period (Paychecks / Investment Income /
  Interest / Other). Total income in the center.
- Below the donut: 12-month **bar chart of income history** with a light "average monthly
  income" band overlay.
- Teal palette (`#2dd4bf`-ish) to pair with the indigo Spending tab — visually echoes BoA.

**Data shape (from `GET /dashboard/income`).**
```json
{
  "period": "2026-05",
  "total_income": 8875.75,
  "subcategories": [
    { "name": "Paychecks/Salary", "amount": 8875.75, "pct": 100.0 }
  ],
  "monthly_history": [
    { "month": "2025-06", "total": 11781.42 },
    { "month": "2025-07", "total":  4200.00 }
  ],
  "avg_monthly": 27975.18
}
```

**LLM insight slot.** Example output for a month where income dipped:

> **Insight.** May income came in lower than the past few months — only the Salesforce paycheck
> landed in this window. Nothing to worry about if your other deposits are paid out later.

**Period default.** Current month for the donut; trailing 12 months for the bar chart (always).

**Exclusions.**
- Rows with `flow_type='income'`. Refunds and interest credits are reported in Transactions and
  Cash Flow respectively, not here.

### 3.3 Transactions

**Visual.**
- Filter bar at the top: date range pickers, category multi-select, account dropdown, min/max
  amount, free-text search on description.
- A table: **Date · Merchant · Account · Category · $ Amount** (signed via the existing
  `v_transactions_signed` view).
- Pagination at 50 rows per page (revisit during implementation).
- Above the table: a small "**Worth a look**" insight card from the LLM.

**Data shape (from `GET /dashboard/transactions`).**
```json
{
  "page": 1,
  "page_size": 50,
  "total": 137,
  "rows": [
    {
      "date": "2026-05-21",
      "merchant": "LATE FEE FOR PAYMENT DUE",
      "account_label": "Customized Cash Rewards Visa Signature - 8373",
      "category": "Finance: Service Charges/Fees",
      "amount_signed": -25.00
    }
  ]
}
```

**LLM insight slot — "Worth a look".** The LLM is given a small JSON of the period's notable
rows (interest charges, fees, top 5 largest non-recurring purchases, biggest merchant
concentrations) plus the user's goal. It returns 2–3 short callouts:

> **Worth a look.**
> - A late fee of $25 hit your credit card on May 21 — knocking these out helps the "pay off my
>   credit" goal.
> - $540.48 to PC BELLEVUE is the largest one-off this month; worth tagging if it's a refundable
>   work expense.

**Period default.** Current month range. Filters live in URL query string.

**Exclusions.**
- Paired internal transfers are still *shown* in the table (the user wants to see them) but
  rendered with a muted style and a "↻ transfer" tag. They never appear in the "Worth a look"
  callouts.

### 3.4 Cash Flow

**Visual.**
- **Grouped bar chart**: income (teal) vs. spending (indigo) per month for the trailing 12
  months. A light band shows the user's monthly average. Optional dotted line for "budget"
  once an overall budget exists (see §6).
- **Summary row** below the chart (BoA-style):
  `Income $X − Spending $Y = Net/Shortfall $Z · Avg spending $A/mo · Avg net $B/mo`. Shortfall
  rendered in `#ef4444`.
- **Fixed vs. Flexible breakdown table** under the summary, one row per category, columns =
  the last few months + `Monthly Average` + `12-Month Total`. Categories are grouped under
  **Fixed Expenses** and **Flexible Expenses** headers.

**Fixed vs. Flexible classification heuristic.** A category is classified **Fixed** if both:
- It appears in **at least 4 of the last 6 months**, and
- The **coefficient of variation** (stddev / mean) of its monthly totals over the last 6 months
  is **≤ 0.25**.

Everything else is **Flexible**. The threshold is intentionally tunable — start at 0.25, watch
real data for a few weeks, adjust. Note in code as a single constant.

**Data shape (from `GET /dashboard/cashflow`).**
```json
{
  "months": ["2025-06", "2025-07", "...", "2026-05"],
  "income_per_month":   [11781.42, 4200.00, "...", 8875.75],
  "spending_per_month": [17089.71, 26801.86, "...", 10288.90],
  "avg_spending": 29037.89,
  "avg_net": -1062.71,
  "fixed_categories":   [{ "name": "Home & Utilities", "monthly": { "2026-05": 3281.94, "...": 0 }, "avg": 3128.74 }],
  "flexible_categories":[{ "name": "Dining",           "monthly": { "2026-05": 1240.55, "...": 0 }, "avg":  980.10 }]
}
```

**LLM insight slot.** Anchored to the goal. Example for a user whose goal is "pay off my credit":

> **Insight.** You're running about $1,063/mo behind across the last year. Your Dining and
> Shopping totals together are roughly that gap — softening either by ~20% would tip you back to
> neutral and let you put more toward the card balance.

**Period default.** Trailing 12 months. Period selector still exposed so power users can pick a
specific year or window.

**Exclusions.**
- Paired internal transfers (both legs).
- `flow_type IN ('transfer')` rows that the ingester already marked.

### 3.5 Pinned (placeholder for 1C)

Empty tab in 1B. Renders any rows in the `pinned_charts` table (none today). Wireframe shows
"Pinned charts appear here once you ask the companion to save one — coming soon." This makes 1C
a write-only addition.

---

## 4. Period selector

Inline dropdowns next to the tab title, mirroring the BoA screenshots. Same component shared
across all four chart tabs.

**Options.**
- Each of the last 24 months (e.g. "May 2026", "Apr 2026" …).
- "Year to date".
- "Last 12 months".
- "Custom range…" → opens a two-date picker.

**State.** The selection is held in the **URL query string** (e.g. `?period=2026-05` or
`?start=2025-11-01&end=2026-05-15`) so a view is shareable and survives reload. Defaults:

| Tab          | Default period               |
| ------------ | ---------------------------- |
| Spending     | Current month                |
| Income       | Current month (donut)        |
| Transactions | Current month range          |
| Cash Flow    | Last 12 months               |

Account filter is a separate dropdown ("All Accounts" / specific account); same URL-state rule.

---

## 5. LLM annotation layer

Every chart in §3 has an insight slot. The annotation is **generated lazily** on first view of
a `(chart_key, period_key)` pair, **cached** afterward, and **manually refreshable**.

### Cache contract

New SQLite table `chart_annotations`:

| column            | type      | notes                                                         |
| ----------------- | --------- | ------------------------------------------------------------- |
| `chart_key`       | TEXT      | One of: `spending`, `income`, `transactions`, `cashflow`      |
| `period_key`      | TEXT      | E.g. `2026-05`, `range:2025-11..2026-05`, `last-12mo:2026-05` |
| `payload_hash`    | TEXT      | SHA1 over the aggregated JSON we passed to the LLM            |
| `annotation_text` | TEXT      | The warm 1–2 sentence headline                                |
| `suggestions`     | TEXT JSON | Array of up to 2 gentle suggestions                           |
| `generated_at`    | TEXT      | ISO timestamp                                                 |
| PRIMARY KEY       |           | `(chart_key, period_key)`                                     |

On read: if `payload_hash` of the freshly aggregated data matches the cached row's hash, return
the cached annotation. Otherwise regenerate. This handles the "new ingestion came in"
invalidation automatically — we don't need an external trigger.

A "**Refresh insights ↻**" button on each chart card forces regeneration regardless of hash.

### Prompt contract

The new module `src/dashboard_insights.py` owns the prompt assembly. Inputs:

- The chart's aggregated payload (the same JSON the frontend uses to render).
- The user's `goal_key` + `goal_text` from `UserConfig`.
- A 1–2 sentence relevant slice of `user_wiki.md` (use the **Goal** and **Active Concerns**
  sections from the wiki).
- The chart's `chart_key` (used to pick the right prompt template).

Output JSON schema (validated; if parse fails, retry once then fall back to a generic warm
annotation):

```json
{
  "annotation": "string, 1-2 short sentences, warm tone",
  "suggestions": ["string", "string"]
}
```

A separate prompt template lives in `src/prompts/dashboard_<chart_key>.txt` for each chart,
inheriting the tone rules from `src/prompts/companion.txt`. The LLM call goes through
`llm_orchestrator.generate_chart_annotation(...)` so the existing DeepSeek client is reused.

### Tone

Reaffirms the product voice (see `CLAUDE.md`):

- Warm and non-judgmental — **never** "you overspent", **always** "this came in a little higher
  than usual".
- Suggestions are gentle and optional — phrased as "try this" or "want me to look closer?",
  never as instructions.
- Short. One sentence per suggestion, two max.

---

## 6. User goal + LLM-derived budget

### Data model

Extend `UserConfig` (in `src/storage.py`'s `UserConfigStore`):

```python
@dataclass
class UserConfig:
    # ... existing fields ...
    goal_key: str          # "stay_ahead_bills" | "pay_off_credit" | "build_credit" | "custom"
    goal_text: str         # free text, shown to LLM
    derived_budget: list[BudgetHint]   # see below
    derived_budget_generated_at: str | None
```

```python
@dataclass
class BudgetHint:
    category: str          # e.g. "Dining"
    hint_text: str         # e.g. "Try to keep around $350-400/mo; you've averaged $420 lately"
```

`derived_budget` is **text, not numbers** — by design. We don't validate or enforce them; they
are guidance the LLM has surfaced based on the user's recent spending and stated goal.

### Generation flow

The derived budget is generated by an LLM call:

- **When triggered.** On profile-page click of "Refresh budget guidance", and automatically the
  first time the user sets a goal. *Not* on every dashboard load.
- **Inputs.** `goal_text`, the last 3-month per-category averages, the user's wiki Goal/Active
  Concerns sections.
- **Output.** A list of `{category, hint_text}` for the user's top ~6 categories. The LLM
  decides which categories matter; we don't ask it to cover everything.

### User menu + settings surface

The dashboard's only entry into settings is the **user menu icon** in the top-right of the
header (avatar / hamburger). Clicking it opens a dropdown or slide-in panel with these
sections — each linking to a settings sub-route under `/settings`:

| Section            | Route                | Purpose                                              |
| ------------------ | -------------------- | ---------------------------------------------------- |
| Profile            | `/settings/profile`  | Name, basic identity                                 |
| Goal               | `/settings/goal`     | Chip selector + free-text editor + derived budget    |
| Linked accounts    | `/settings/accounts` | View / unlink current accounts                       |
| Bank statements    | `/settings/uploads`  | Upload / list / remove PDF / CSV statements          |
| Plaid permissions  | `/settings/plaid`    | Connect / disconnect Plaid; manage scopes            |
| Sign out           | (action)             | (placeholder in 1B — no auth yet)                    |

**1B implementation scope.** The menu shell + Profile + Goal sub-routes are in scope. The
other entries appear in the menu as **disabled / "coming soon"** placeholders so the IA is
visible to the user but we don't build them yet. The Plaid and uploads work was already
deferred to Tryout (1D) per `design/phases.md` — the menu just reserves their home.

**Goal sub-route** content:

```
Settings → Goal

What matters to you right now?
[ Stay ahead of my bills ]  [ Pay off my credit ]  [ Build good credit ]  [ Custom… ]

Your words:
┌──────────────────────────────────────────────────────────┐
│ I'd like to clear my $4k card balance over the next 6    │
│ months without giving up everything.                     │
└──────────────────────────────────────────────────────────┘
                                          [ Save goal ]

──────────────────────────────────────────────────────────────

Budget guidance (auto-generated from your goal)        [ Refresh ↻ ]

┌─────────────────────────────┐  ┌─────────────────────────────┐
│ Dining                      │  │ Subscriptions               │
│ Keep around $350–400/mo;    │  │ You've got 8 active subs.   │
│ averaged $420 lately.       │  │ Pause 1–2 you don't use.    │
└─────────────────────────────┘  └─────────────────────────────┘
```

Each guidance card is plain text the user can edit inline. Derived from `derived_budget` (see
data model above).

### Goal is not in the default chrome

There is **no goal pill in the header**, no goal banner on the dashboard. The goal is a
private setting the user revisits when they choose to; it influences the LLM annotations and
budget guidance silently. This is intentional — the product is a non-judgmental companion,
not a scoreboard.

---

## 7. Side chat panel

A collapsible drawer rendered on the dashboard (and reusable on other pages later). **Collapsed
by default;** the floating bubble is the persistent entry point and the visible nudge that the
companion is there to drill down on any number on screen. The drawer
**reuses the existing `/chat` POST endpoint and `Companion.chat()`** — no new backend.

### Open state

```
─ Resume previous conversation   Clear ─
┌──────────────────────────────────────┐
│ Companion: Hey — I noticed you're    │
│ ahead on groceries this month. Nice. │
│                                      │
│ You: What did I spend on dining?     │
│                                      │
│ Companion: About $1,240 in May …     │
└──────────────────────────────────────┘
┌──────────────────────────────────────┐
│ Type a message…                  [↵] │
└──────────────────────────────────────┘
```

- **Resume previous conversation** — lays groundwork for the user's future ambition of letting
  users select prior conversations. In 1B it just expands a small menu listing recent sessions
  (we already have `session_id` on memory rows; this is a thin read). Picking one loads its
  history into the drawer. Defaults to the current session.
- **Clear** — invokes the existing `DELETE /memory` endpoint.

### Collapsed state (default)

The drawer is hidden; a floating bubble (~52px circle, chat icon, indigo) sits bottom-right of
the viewport. Click reopens the drawer. The bubble has a tiny dot indicator slot for an
"unread companion message" cue — not wired in 1B, but the markup is there. Subtle hover label
("Ask your companion") to reinforce the affordance.

### Persistence

Open/closed state is stored in `localStorage` so the user's preference survives navigation.
The default on first visit is **collapsed**.

### What 1B does **not** add

- Drill-down "explain this chart" buttons or per-chart "ask why" hooks — that's 1C.
- Pinning generated charts back to the dashboard — also 1C.
- Image upload in the drawer (it already exists on the standalone `/chat` page; mirroring it in
  the drawer is fine if cheap, but not required).

---

## 8. Internal transfer pairing (Phase 1A → 1B carry-over)

Phase 1A deliberately deferred transfer pairing to the query layer (see `design/storage.md`).
1B implements it.

### The rule

Two rows are a paired internal transfer if **all** are true:

1. One row has `account_type='credit'` AND `section_type='payment'`.
2. The other row has `account_type IN ('checking','savings')` AND `section_type='withdrawal'`.
3. Same `user_id`.
4. `|amount_a − amount_b| ≤ $0.01`.
5. Dates are within a **±3-day window**.

Both rows are logically flagged `is_paired_transfer=true` at query time and **excluded** from
Spending, Income, and Cash Flow totals.

Additionally, any row already marked `flow_type='transfer'` by the ingester is excluded from
those totals regardless of pairing.

### Where it lives

`src/dashboard_queries.py` (new file). The storage schema does **not** change — the pairing
happens at read time. Helpers:

```python
def list_transactions_signed(user_id, start, end, account_id=None) -> list[Row]:
    """Reads from v_transactions_signed and attaches is_paired_transfer."""

def spending_total(user_id, start, end, account_id=None) -> Decimal: ...
def income_total(user_id, start, end, account_id=None) -> Decimal: ...
def category_breakdown(user_id, start, end, kind='spending', account_id=None) -> dict: ...
def monthly_series(user_id, months=12, kind='spending', account_id=None) -> list[Point]: ...
def fixed_vs_flexible(user_id, months=6) -> tuple[list[Cat], list[Cat]]: ...
```

### Verification anchor

There is a concrete known pair in `data/transactions.db`:

- `2026-05-21 · MOBILE BANKING PAYMENT TO CRD 8373 · −$217.93` (checking, `withdrawal`).
- `2026-05-21 · PAYMENT FROM CHK 0790 CON · $217.93` (credit, `payment`).

After implementation, these two rows must not appear in Spending or Income totals.

---

## 9. Backend surface

New / changed FastAPI endpoints on `src/web_chat.py`:

| Method | Path                                       | Returns                                            |
| ------ | ------------------------------------------ | -------------------------------------------------- |
| GET    | `/dashboard`                               | HTML — the new tabbed dashboard                    |
| GET    | `/dashboard/spending`                      | JSON — see §3.1                                    |
| GET    | `/dashboard/income`                        | JSON — see §3.2                                    |
| GET    | `/dashboard/transactions`                  | JSON — see §3.3 (paginated)                        |
| GET    | `/dashboard/cashflow`                      | JSON — see §3.4                                    |
| GET    | `/dashboard/insights/{chart_key}`          | JSON — cached annotation (`?refresh=1` forces)     |
| POST   | `/dashboard/insights/{chart_key}/refresh`  | JSON — same payload, forces regeneration           |
| GET    | `/dashboard/pinned`                        | JSON — `{ "charts": [] }` in 1B                    |
| GET    | `/settings`                                | HTML — user menu landing (links to sub-routes)     |
| GET    | `/settings/profile`                        | HTML — profile basics                              |
| GET    | `/settings/goal`                           | HTML — goal chips + derived-budget cards           |
| POST   | `/settings/goal`                           | JSON — persists `goal_key` + `goal_text`           |
| POST   | `/settings/goal/budget/refresh`            | JSON — regenerates `derived_budget`                |

The existing `/dashboard/data` route becomes a thin compatibility shim that delegates to the
new ones, or is removed once the new frontend is in place (recommend remove — there are no
external consumers).

Query-string conventions (consistent across all dashboard JSON endpoints):

- `period=YYYY-MM` — single month.
- `period=ytd` — year to date.
- `period=last-12mo` — trailing 12 months.
- `start=YYYY-MM-DD&end=YYYY-MM-DD` — explicit range; overrides `period`.
- `account=<account_id>` or omitted for all.
- `category=A,B,C` — multi-select where applicable.

---

## 10. Files to add or modify

| Path                                       | Action  | Purpose                                              |
| ------------------------------------------ | ------- | ---------------------------------------------------- |
| `src/templates/dashboard.html`             | Rewrite | Tabbed layout, period selector, user menu, chat drawer markup |
| `src/templates/settings_goal.html`         | New     | Goal chips + free text + derived-budget cards        |
| `src/templates/settings_profile.html`      | New     | Basic profile placeholder                            |
| `src/templates/_chat_drawer.html`          | New     | Shared chat drawer partial (collapsed by default)    |
| `src/templates/_user_menu.html`            | New     | Top-right user menu dropdown partial                 |
| `src/web_chat.py`                          | Extend  | New endpoints, settings pages, drawer mount          |
| `src/dashboard_queries.py`                 | New     | Aggregations, transfer pairing, fixed/flex classifier|
| `src/dashboard_insights.py`                | New     | Cached LLM annotations + prompt assembly             |
| `src/llm_orchestrator.py`                  | Extend  | `generate_chart_annotation(...)`, `generate_derived_budget(...)` |
| `src/storage.py`                           | Extend  | `chart_annotations`, `pinned_charts` tables; extend `UserConfig` |
| `src/prompts/dashboard_spending.txt`       | New     | Tone-locked annotation prompt                        |
| `src/prompts/dashboard_income.txt`         | New     | ″                                                    |
| `src/prompts/dashboard_transactions.txt`   | New     | ″                                                    |
| `src/prompts/dashboard_cashflow.txt`       | New     | ″                                                    |
| `src/prompts/derived_budget.txt`           | New     | Budget guidance prompt                               |
| `tests/test_dashboard_queries.py`          | New     | Transfer pairing + period filters against real DB    |
| `tests/test_dashboard_insights.py`         | New     | Cache hit/miss + payload hash invalidation           |

---

## 11. Visual tone

- Reuse the existing CSS palette in `src/templates/dashboard.html`:
  - Background `#f0f2f5`, cards white with `0 1px 4px rgba(0,0,0,0.08)` shadow.
  - Primary indigo `#4f46e5` (Spending donut, links, primary button).
  - Pair with a teal `#2dd4bf` for Income; matches BoA.
  - Neutral grays: `#1a1a2e` (text), `#6b7280` (muted), `#e5e7eb` (lines).
  - Error / shortfall red `#ef4444`.
- Don't redesign from scratch — the existing CSS is already on-brand.
- Charts: keep Chart.js. Doughnut for Spending/Income totals, grouped bar for Cash Flow, simple
  bar for Income history, plain HTML table for Transactions + Fixed/Flexible breakdown.

---

## 12. Verification plan

End-to-end, before merging 1B:

1. `python -m src.web_chat`; open `http://localhost:8000/dashboard`.
2. Tab through **Spending → Income → Transactions → Cash Flow → Pinned**; confirm each renders
   without console errors and shows real numbers.
3. Change the period dropdown (e.g. May 2026 → Apr 2026 → Last 12 months); confirm totals
   change and URL updates.
4. **Transfer-pairing check.** Pick the known May 21 pair (`MOBILE BANKING PAYMENT TO CRD 8373`
   ↔ `PAYMENT FROM CHK 0790 CON`, both $217.93). Confirm:
   - It is shown muted with a "↻ transfer" tag in the Transactions table.
   - It is **not** in the Spending donut total.
   - It is **not** in the Income donut total.
   - It is **not** counted toward Cash Flow income or spending for May 2026.
5. Click **Refresh insights ↻** on the Spending card; confirm a new annotation appears and the
   row in `chart_annotations` for `(spending, 2026-05)` has a newer `generated_at`.
6. Click the user menu icon top-right; open **Goal**; change the chip from "Pay off my credit"
   to "Build good credit"; click **Refresh budget guidance**; confirm new `derived_budget`
   cards reflect the new goal. Return to the dashboard and confirm the next Spending insight
   refresh references the new goal.
7. Confirm the chat drawer is **collapsed on first load**; the floating bubble sits bottom-right.
   Click the bubble; send "what did I spend on dining?"; confirm the existing `/chat` plumbing
   still responds. Collapse again with the drawer's close control and reload — should stay
   collapsed (localStorage).
8. `pytest tests/test_dashboard_queries.py tests/test_dashboard_insights.py`.

Success bar (from `design/phases.md`): "The dashboard renders the four standard charts from real
data, each carries a warm and accurate annotation, and totals correctly exclude internal
transfers." Items 2 + 4 + 5 cover this directly.

---

## 13. Open items / decisions deferred to implementation

These are intentionally left for the implementer to settle when they have the code in hand:

- **Transactions pagination size.** Start at 50/page; revisit once you've used the page on real
  data for a week.
- **Period-state location.** Recommend URL query string (sharable, survives reload). Reconsider
  only if URL ends up cluttered.
- **Fixed/Flex CoV threshold.** Start at 0.25; surface the parameter as a module-level constant
  so it's a one-line tweak.
- **Insight regeneration cost.** DeepSeek is cheap, but if a power user mashes "Refresh
  insights" we shouldn't make 4 calls in 4 seconds. Add a light per-(chart, period) cooldown
  (e.g. 10 seconds) — minor.
- **Resume-previous-conversation UX.** 1B does the minimum: list recent sessions, click to load.
  Anything more (search, naming) waits for 1C feedback.
- **Whether `/dashboard/data` survives.** Recommend deleting it once the new endpoints are
  wired — no external consumer.
