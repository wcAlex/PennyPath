# Chat Drill-Down Agent — Phase 1C

This is the design for the Phase 1C chat experience: a companion that answers
finance questions grounded in the user's real transactions, drilling down from
the dashboard charts in plain language. It builds directly on the Phase 1B data
layer (`v_transactions_recon` + `src/dashboard_queries.py`) and the existing
chat drawer (`src/templates/_chat_drawer.html` + `POST /chat`).

Review and edit before any code is written.

---

## 1. Goal & scope

Phase 1C of `design/phases.md` calls for Q&A over real data, drill-down from
any chart, custom chart generation, pin-from-chat, and conversation memory wired
through the User Context store. This document covers the **agent logic and tool
surface** that powers all of that. Specifically:

**In scope**
- An **in-process tool registry** living in the backend alongside the rest of
  the app. Tools are Python callables with a `name`, a one-line description
  the LLM reads, and a JSON Schema for their inputs. The agent calls them via
  direct Python dispatch — no protocol, no separate process, no network hop.
  The tool surface is **internal to PennyPath**: only the agent loop inside
  this backend ever invokes it. No external client (Claude Desktop, ChatGPT,
  third-party MCP clients) connects to the user's data. This is a deliberate
  security boundary — the user's financial data stays inside one process
  PennyPath controls end-to-end.
- A **bounded agent loop** that calls the LLM with these tools via OpenAI
  function-calling, executes the tool, feeds the result back, and either calls
  another tool or replies. Hard cap at 5 tool-calls per user turn, 20-second
  total wall-clock budget.
- **Chart-context passthrough** from the drawer: each `/chat` POST carries
  `{chart_key, period, selected_category, selected_account_id, summary_numbers}`
  so "break that down" doesn't need to re-ask what "that" is. The LLM treats
  context as the *default* scope, free to override when the question asks for
  something broader.
- A **hybrid clarification policy**: if chart-context resolves the ambiguity,
  the agent runs the query and announces the assumed scope in the first line of
  the reply. If essential scope is missing and context can't fill it, the agent
  asks one focused follow-up before querying.
- A **structured response shape** `{text, blocks: [...]}` where `blocks` can
  hold a small `table` or `chart_spec` rendered inline in the drawer. This sets
  up "pin from chat" — a pinned-chart payload is just a `chart_spec` block the
  user chose to save.
- **Consolidated data only.** Every tool reads the reconciled view
  `v_transactions_recon` (and `accounts` for account metadata) — never the raw
  `transactions` table. This is the same source the dashboard uses, so chat
  numbers match the dashboard numbers exactly: internal transfers are paired
  and excluded, duplicates from cross-source ingestion are dropped, and
  `flow_type_recon` is the corrected classification. Going through the recon
  layer is a non-negotiable rule of the tool surface, called out again in §3.

**Out of scope (this doc)**
- **Exposing the tool surface to external clients** (Claude Desktop, ChatGPT,
  third-party MCP clients). The tool registry is internal-only by design. If
  this changes in a future phase, it requires a separate security review —
  read-only DB role, per-user auth, audit log — not just a transport wrapper.
  Until that review happens, no MCP server, no external HTTP tool routes.
- The actual **"pin from chat" UI** (button, persistence wiring). The
  `chart_spec` data shape is designed for it, but the button lives in a
  follow-up after this lands.
- New analytics that aren't already in `dashboard_queries.py`. The tool surface
  exposes what's there plus three thin helpers (`category_trend`,
  `top_merchants`, `compare_periods`); we do not grow the analytics layer.
- Replacing the goal/intent routing in `Companion`. Check-in, monthly-analysis,
  and set-goal stay as-is. Only the `question` intent routes through the new
  agent.
- Coaching content, push notifications, native mobile (Phase 2+).

---

## 2. End-to-end flow

```
 ┌───────────────────────┐
 │ Chat drawer           │
 │  - reads dashboard    │
 │    tab/period/cat     │
 │  - sends message      │
 └───────────┬───────────┘
             │  POST /chat
             │  { message, chart_context }
             ▼
 ┌───────────────────────┐
 │ web_chat.py           │
 │  /chat route          │
 └───────────┬───────────┘
             │  Companion.chat(...)  (question intent)
             ▼
 ┌───────────────────────┐         ┌────────────────────────┐
 │ ChatAgent.run         │◀───────▶│ ConversationStore      │
 │  user_id, message,    │         │ (history persistence)  │
 │  history, ctx         │         └────────────────────────┘
 └───────────┬───────────┘
             │
             │  loop ≤ 5 iters
             ▼
 ┌────────────────────────────────────────────┐
 │ LLM (DeepSeek via OpenAI SDK)              │
 │  - sees tools=to_openai_tools(REGISTRY)    │
 │  - chooses: tool_call OR final reply       │
 └───────────┬────────────────────────────────┘
             │  tool_call(name, args)
             ▼
 ┌────────────────────────────────────────────┐
 │ ToolRegistry (chat_tools.py)               │
 │  - validates args against inputSchema      │
 │  - dispatches handler(user_id, args)       │
 │  - handler reads v_transactions_recon via  │
 │    dashboard_queries.py                    │
 └───────────┬────────────────────────────────┘
             │  tool_result(json)
             ▼
        (back to LLM)
             │
             ▼  (final reply)
 ┌───────────────────────┐
 │ ChatReply             │
 │  { text, blocks?:[…] }│
 └───────────┬───────────┘
             │  JSON
             ▼
 ┌───────────────────────┐
 │ Drawer renders text   │
 │ + inline table/chart  │
 └───────────────────────┘
```

The dashed line back from `ConversationStore` is real: history is loaded at the
start of the turn and the user/assistant messages (plus tool-call/result pairs)
are appended after the reply is produced. The same `ConversationStore` the
existing `Companion` uses today — no parallel history store.

---

## 3. Tool surface

Each tool is described below as: `name`, `description` (one sentence the LLM
reads), `inputSchema` (JSON Schema, draft 2020-12) for argument validation,
and `returns` (the JSON shape the agent receives back). All tools take
`user_id` from the server-side session — the LLM never sees or sets it.
Numbers in `returns` are JSON floats (not `Decimal`); dates are ISO strings.

JSON Schema is used because it's the cleanest way to (a) validate the LLM's
tool arguments and (b) hand the same schema to OpenAI's function-calling API
through a one-line adapter (`to_openai_tools()`). It is not used as a protocol
hook for external clients.

### Data source: consolidated, not raw

Every handler reads the **reconciled view `v_transactions_recon`** (and
`accounts` for account metadata). No handler queries the raw `transactions`
table directly. This guarantees:

- Internal transfers (credit-card payments ↔ checking withdrawals; Zelle
  self-transfers) are paired and **excluded** from spending/income totals.
- Duplicates from overlapping ingestion sources (Plaid + PDF) are dropped.
- The corrected `flow_type_recon` is used in place of the raw LLM-guessed
  `flow_type`, so a Zelle-to-self does not surface as "income".
- Chat numbers match dashboard numbers exactly — both layers read the same
  view through the same `user_id` filter.

If a tool needs a column that only exists on the raw table, add the column to
`transactions_recon` and the view rather than letting the handler bypass the
recon layer. The reconciler (`src/reconciler.py`) is the single seam where raw
becomes consolidated; the chat agent stays on the consolidated side of that
seam.

Handler implementations wrap existing helpers in `src/dashboard_queries.py`. The
table below maps tools to the wrapped function so the implementer knows the
seam.

| Tool                        | Wraps                                                       |
| --------------------------- | ----------------------------------------------------------- |
| `list_categories`           | New small `SELECT category, COUNT(*) … GROUP BY category`   |
| `list_accounts`             | Reads `accounts` table directly                             |
| `query_spending_breakdown`  | `spending_breakdown` (`src/dashboard_queries.py:260`)       |
| `query_income_breakdown`    | `income_breakdown` (`src/dashboard_queries.py:325`)         |
| `list_transactions`         | `transactions_filtered` (`src/dashboard_queries.py:385`)    |
| `category_trend`            | New helper, reads `v_transactions_recon`                    |
| `top_merchants`             | New helper, groups by canonicalized description             |
| `compare_periods`           | New helper, reuses `spending_breakdown` twice               |
| `cashflow_summary`          | `cashflow_series` (`src/dashboard_queries.py:570`)          |

### Shared validation rules

- **Dates** are `YYYY-MM-DD`. The validator rejects `YYYY-MM` and tells the
  LLM "use start/end dates"; this gives a single rule across tools.
- **`category`** matches case-insensitively against the strings
  `list_categories` would return. An unknown category becomes a structured
  `{error: "unknown category 'X'", suggestion: ["closest match",…]}`
  — the LLM can self-correct on the next iteration.
- **`account_id`** matches against `list_accounts`. Same `{error, suggestion}`
  shape on miss.
- `limit` is capped at 200 server-side regardless of what the LLM asks.
- Date ranges spanning more than 24 months return `{error: "range too wide,
  use ≤24 months"}` to keep tool results bounded.

### Tool definitions

#### `list_categories`

```yaml
description: |
  List spending/income categories that appear in the user's data, with row
  counts. Useful when the user names a category and you want to confirm the
  exact label before calling other tools.
inputSchema:
  type: object
  properties:
    start: { type: string, format: date, description: "optional; restrict to date range" }
    end:   { type: string, format: date }
  required: []
returns:
  categories: [{ name: string, count: integer, last_seen: string }]
```

Example call: `list_categories(start="2026-01-01", end="2026-05-31")`.

#### `list_accounts`

```yaml
description: |
  List the user's linked or uploaded accounts (id, friendly name, bank, type,
  last-4 mask). Use when the user says "my Chase account" and you need to pick
  the right account_id, or when you must ask the user which account they mean.
inputSchema:
  type: object
  properties: {}
returns:
  accounts: [{ id: string, name: string, bank: string, type: string, mask: string }]
```

#### `query_spending_breakdown`

```yaml
description: |
  Total spending in a date range, broken down by the requested dimension.
  Excludes internal transfers and duplicates.
inputSchema:
  type: object
  properties:
    start:      { type: string, format: date }
    end:        { type: string, format: date }
    category:   { type: string, description: "optional; filter to one category" }
    account_id: { type: string }
    group_by:   { enum: [category, merchant, week, month], default: category }
  required: [start, end]
returns:
  period: { start: string, end: string }
  total:  number
  buckets: [{ label: string, amount: number, count: integer, pct: number }]
```

The default `group_by=category` reuses `spending_breakdown` directly. The
other three (`merchant`, `week`, `month`) iterate raw rows from the shared
`_fetch_recon_rows` helper inside `chat_tools.py` — no need to grow
`dashboard_queries.py` if it adds friction there.

#### `query_income_breakdown`

```yaml
description: |
  Total income in a date range, broken down by subcategory or month. Excludes
  internal transfers.
inputSchema:
  type: object
  properties:
    start:      { type: string, format: date }
    end:        { type: string, format: date }
    account_id: { type: string }
    group_by:   { enum: [subcategory, month], default: subcategory }
  required: [start, end]
returns:
  period: { start: string, end: string }
  total:  number
  buckets: [{ label: string, amount: number, count: integer, pct: number }]
```

#### `list_transactions`

```yaml
description: |
  Return individual transactions matching the filters. Use when the user wants
  to see actual line items, not aggregates.
inputSchema:
  type: object
  properties:
    start:       { type: string, format: date }
    end:         { type: string, format: date }
    category:    { type: string }
    account_id:  { type: string }
    q:           { type: string, description: "substring match on description (case-insensitive)" }
    min_amount:  { type: number }
    max_amount:  { type: number }
    limit:       { type: integer, default: 50, minimum: 1, maximum: 200 }
  required: [start, end]
returns:
  rows: [{ date: string, description: string, amount: number, category: string,
           account_id: string, flow_type: string, is_internal_transfer: boolean }]
  total_matched: integer
  truncated: boolean
```

#### `category_trend`

```yaml
description: |
  Monthly totals for a category over the last N months. Use for trend / "is
  this growing" questions.
inputSchema:
  type: object
  properties:
    category:    { type: string }
    months:      { type: integer, default: 12, minimum: 1, maximum: 24 }
    account_id:  { type: string }
    flow:        { enum: [spending, income], default: spending }
  required: [category]
returns:
  months: [string]               # ["2025-06", "2025-07", …]
  amounts: [number]              # aligned with months
  avg: number
  peak: { month: string, amount: number }
  trough: { month: string, amount: number }
```

#### `top_merchants`

```yaml
description: |
  Top N merchants by total spend in a date range, optionally filtered to a
  category or account. Merchant labels are canonicalized (trailing reference
  numbers and locations stripped) so the same store doesn't show up as several
  rows.
inputSchema:
  type: object
  properties:
    start:       { type: string, format: date }
    end:         { type: string, format: date }
    category:    { type: string }
    account_id:  { type: string }
    limit:       { type: integer, default: 10, minimum: 1, maximum: 50 }
  required: [start, end]
returns:
  merchants: [{ name: string, total: number, visits: integer, last_seen: string }]
```

#### `compare_periods`

```yaml
description: |
  Compare totals between two date ranges, optionally filtered to a category or
  account. Returns the delta and the categories or merchants that moved most.
inputSchema:
  type: object
  properties:
    period_a_start: { type: string, format: date }
    period_a_end:   { type: string, format: date }
    period_b_start: { type: string, format: date }
    period_b_end:   { type: string, format: date }
    category:       { type: string }
    account_id:     { type: string }
    mover_dim:      { enum: [category, merchant], default: category }
  required: [period_a_start, period_a_end, period_b_start, period_b_end]
returns:
  period_a: { start: string, end: string, total: number }
  period_b: { start: string, end: string, total: number }
  delta: number
  delta_pct: number
  top_movers: [{ label: string, a: number, b: number, delta: number }]
```

#### `cashflow_summary`

```yaml
description: |
  Income vs. spending per month over the last N months, with average and net.
inputSchema:
  type: object
  properties:
    months:     { type: integer, default: 12, minimum: 1, maximum: 24 }
    account_id: { type: string }
  required: []
returns:
  months: [string]
  income_per_month: [number]
  spending_per_month: [number]
  avg_income: number
  avg_spending: number
  avg_net: number
  fixed_categories: [string]
  flexible_categories: [string]
```

### Why this list, not more

Nine tools cover every drill-down example we've discussed (breakdown by
category/merchant/week, transaction lookup, trend, top-N merchants,
period-over-period, cash-flow summary). Resist adding more until a real
question can't be expressed as a composition of these — most "why" / "show me"
questions are answered by two of these tools called in sequence.

---

## 4. Tool registry

A plain Python dict, in the same process as the agent.

```python
# src/chat_tools.py

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict      # JSON Schema, draft 2020-12
    handler: Callable[[str, dict], dict]   # (user_id, args) -> result

REGISTRY: dict[str, ToolSpec] = {
    "list_categories": ToolSpec(...),
    "list_accounts":   ToolSpec(...),
    # ...
}

def to_openai_tools(registry=REGISTRY) -> list[dict]:
    """Adapter for OpenAI function-calling."""
    return [
        {
          "type": "function",
          "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
          },
        }
        for spec in registry.values()
    ]

def dispatch(user_id: str, name: str, args: dict) -> dict:
    spec = REGISTRY.get(name)
    if not spec:
        return {"error": f"unknown tool '{name}'"}
    # validate args against spec.input_schema; on failure return {"error": ...}
    try:
        return spec.handler(user_id, args)
    except ToolError as e:
        return {"error": str(e), **e.payload}
```

The agent loop calls `dispatch(user_id, name, args)` directly. There is no
network hop, no serialization round-trip, no transport layer. The OpenAI
function-calling adapter is the only consumer of the schemas besides the
in-process validator.

This registry is **not** designed to be lifted into an external MCP server. If
a future phase ever wants to expose finance tools to a third-party AI client,
that's a different design problem — it needs per-user auth, a read-only DB
role, an audit trail, and an explicit consent surface for the user. Don't
shoehorn it into this registry; build it as a separate, deliberately-narrowed
surface at that time.

---

## 5. Agent loop

```python
# src/chat_agent.py

MAX_ITERS = 5
WALL_BUDGET_SECONDS = 20.0

@dataclass
class ChatReply:
    text: str
    blocks: list[dict] = field(default_factory=list)

class ChatAgent:
    def run(
        self,
        user_id: str,
        user_message: str,
        history: list[dict],          # prior assistant/user turns from ConversationStore
        chart_context: dict | None,
    ) -> ChatReply:
        messages = self._build_messages(history, chart_context, user_message)
        start = time.monotonic()

        for _ in range(MAX_ITERS):
            if time.monotonic() - start > WALL_BUDGET_SECONDS:
                return ChatReply(text=_FALLBACK_BUSY)

            resp = llm.chat.completions.create(
                model=_model(),
                messages=messages,
                tools=to_openai_tools(),
                tool_choice="auto",
            )
            choice = resp.choices[0].message

            if choice.tool_calls:
                messages.append(choice.model_dump())   # assistant turn w/ tool_calls
                for call in choice.tool_calls:
                    args = json.loads(call.function.arguments or "{}")
                    result = chat_tools.dispatch(user_id, call.function.name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result),
                    })
                continue

            return self._parse_reply(choice.content or "")

        return ChatReply(text=_FALLBACK_GAVE_UP)
```

Key details:

- **Per-tool timeout**: the dispatch wrapper raises `ToolError("timeout")` if a
  single handler exceeds 5 seconds. The error goes back to the LLM as a tool
  result; the loop keeps going.
- **Wall-clock budget**: 20 seconds across the whole turn. If we exceed it
  between iterations, return a warm fallback ("Give me a sec — try that again")
  instead of timing out the HTTP request.
- **Reply parsing**: the LLM is instructed to return JSON `{text, blocks}` when
  it emits a block, otherwise plain text. `_parse_reply` tries `json.loads`
  on stripped content; on failure, the whole content is treated as `text` with
  no blocks. The LLM is told this fallback exists so it doesn't have to
  produce valid JSON for trivial replies.
- **History persistence**: the *user* and *final assistant* messages are
  appended to `ConversationStore`. Tool-call / tool-result pairs are *not*
  persisted across turns — they balloon history fast and the next turn's LLM
  doesn't need them. (If a future turn asks "what did you find last time?", the
  text reply already summarizes it.)

---

## 6. System prompt + clarification policy

The system prompt lives in `src/prompts/chat_drill_down.txt`. We don't fix the
exact wording here — that's an implementation detail tuned against real
conversations — but the prompt **must** encode the rules below. They are
load-bearing for the agent's behavior; changes here are design changes.

### Tone

- Warm, non-judgmental, conversational. Same voice as the rest of the
  Companion (`design/product.md`). "You spent a bit more on dining this month
  — that's okay, here's what I saw" rather than "Your dining spend exceeded
  baseline."
- Short replies. 1–3 sentences of text on either side of a block. Money
  numbers are stated neutrally; no shaming language ("blew", "wasted",
  "overspent"), no pure red on a finance surface.
- The agent never lectures. If it notices something worth flagging, it offers
  one optional next step ("want me to pull the actual receipts?") rather than
  a list of corrections.

### Tool-use defaults

- **Tool-use is the default for any data question.** Do not state numbers from
  memory. Do not quote a number from `chart_context.summary_numbers` as a
  fresh answer — those numbers are background only. If the user's question
  asks "why" or "show me" or "break down", a tool gets called.
- **`chart_context` as default scope.** If the user uses anaphora ("that",
  "those", "it") or asks "break that down", the agent assumes the
  `chart_context` period + category + account. It runs the query without
  asking.
- **Scope override is OK.** If the user explicitly broadens ("past 3 months",
  "across accounts", "all year") or narrows ("just weekends"), the agent
  overrides `chart_context` and uses the new scope.

### Hybrid clarification by confidence

The agent picks one of two paths per turn:

- **High confidence** — `chart_context` resolves period + scope, OR the
  question is fully explicit. Run the query. Open the reply with one short
  line stating the assumed scope in plain English: "Looking at Dining for May
  2026 —". This is the *announcement*; it tells the user what scope you used
  so they can correct cheaply on the next turn.
- **Low confidence** — the question is missing essential scope AND
  `chart_context` can't fill it (e.g. user opened chat from the chat tab and
  asks "how much have I spent on travel?"). Ask **one** focused follow-up.
  Suggest 1–2 likely options ("Just this month, or the year so far?"). Do not
  ask a multi-part question. Do not call a tool yet.

### Empty-result handling

If a tool returns zero rows, the agent explains why in plain language (wrong
category name? wrong period? no data ingested for that account?) and offers
one constructive next step ("want me to look across all your accounts?" or
"that period might be before your earliest statement — should I check?").
Don't return "no results" alone.

### Block emission

- Emit a `table` block when listing transactions or merchants (>= 3 rows).
- Emit a `chart_spec` block when answering a "trend" or "compare" question
  with a clear time axis or A/B axis.
- Otherwise text-only.
- **Cap to one block per reply.** If both a table and a chart would help, pick
  one — preferring the chart for trends and the table for line-item drill-down.
- When emitting a `table` block, the text introduces it ("here are your top
  spots:") and stops. Don't restate the table contents in prose.

### Worked examples

Four end-to-end traces. The implementer should validate the agent's behavior
against these as smoke tests.

**Example 1: "Break down my dining"** with `chart_context = {chart_key:
spending, period: 2026-05-01..2026-05-31, selected_category: Dining,
summary_numbers: {total_spend: 1711.42, …}}`

```
user: break down my dining
→ LLM tool_call: query_spending_breakdown(
    start=2026-05-01, end=2026-05-31, category=Dining, group_by=merchant)
← {total: 1711.42, buckets: [
    {label: "Don Angie",   amount: 412.00, count: 2, pct: 24.1},
    {label: "Levain",      amount: 187.50, count: 5, pct: 11.0},
    …
   ]}
→ LLM final reply:
{
  "text": "Looking at Dining for May 2026 — most of it was a few bigger nights out, with the regulars filling in. Here are the top spots:",
  "blocks": [{
    "type": "table",
    "title": "Top dining merchants — May 2026",
    "columns": ["Merchant", "Spent", "Visits"],
    "rows": [["Don Angie", "$412.00", 2], …]
  }]
}
```

**Example 2: "Show me dining trend past 3 months"** with `chart_context` on
Spending/May/Dining (period gets overridden).

```
user: show me dining trend past 3 months
→ LLM tool_call: category_trend(category=Dining, months=3)
← {months: ["2026-03","2026-04","2026-05"], amounts: [1342.10, 1610.55, 1711.42], avg: 1554.69, peak: …}
→ LLM final reply:
{
  "text": "Looking at Dining for March–May 2026 — it's been ticking up each month, about $1,555 on average. Here's the trend:",
  "blocks": [{
    "type": "chart_spec",
    "title": "Dining — last 3 months",
    "chart_type": "bar",
    "labels": ["Mar 2026", "Apr 2026", "May 2026"],
    "series": [{"name": "Dining", "data": [1342.10, 1610.55, 1711.42]}]
  }]
}
```

**Example 3: "How am I doing on travel?"** with `chart_context = null` (chat
opened from the chat tab, no dashboard underneath).

```
user: how am I doing on travel?
→ LLM (no tool call — low confidence):
{
  "text": "Happy to dig in — do you want to look at just this month, or the year so far?"
}
```

**Example 4: "Why was March higher?"** with `chart_context = {chart_key:
cashflow, period: 2026-01..2026-05}`.

```
user: why was march higher?
→ LLM tool_call: compare_periods(
    period_a_start=2026-02-01, period_a_end=2026-02-28,
    period_b_start=2026-03-01, period_b_end=2026-03-31, mover_dim=category)
← {period_a: {total: 3120.40}, period_b: {total: 4080.10}, delta: 959.70, delta_pct: 30.8,
   top_movers: [
     {label: "Travel",   a: 0, b: 612, delta: 612},
     {label: "Shopping", a: 280, b: 540, delta: 260},
     …
   ]}
→ LLM final reply:
{
  "text": "March was about $960 higher than February — mostly a Travel bump and a bit more Shopping. Top movers below:",
  "blocks": [{
    "type": "table",
    "title": "What moved most — Feb vs. Mar 2026",
    "columns": ["Category", "Feb", "Mar", "Δ"],
    "rows": [["Travel", "$0", "$612", "+$612"], …]
  }]
}
```

---

## 7. Chart-context contract

The drawer assembles `chart_context` from whatever the user is currently
viewing and sends it with each `/chat` POST.

```json
{
  "message": "break down my dining",
  "chart_context": {
    "chart_key": "spending",
    "period": {
      "start": "2026-05-01",
      "end":   "2026-05-31",
      "label": "May 2026"
    },
    "selected_category":   "Dining",
    "selected_account_id": null,
    "summary_numbers": {
      "total_spend": 1711.42,
      "top_5": [
        {"name": "Dining",       "amount": 1711.42},
        {"name": "Groceries",    "amount":  548.10},
        {"name": "Subscriptions","amount":  219.99},
        {"name": "Transportation","amount":  187.30},
        {"name": "Shopping",     "amount":  140.00}
      ]
    }
  }
}
```

Notes:

- `chart_key ∈ {spending, income, transactions, cashflow}` or `null`.
- `period` is always a `{start, end, label}` triple — the LLM gets exact ISO
  dates *and* the human label so its announcement reads naturally.
- `selected_category` is whatever the user has filtered to (e.g. clicked a
  slice). `selected_account_id` likewise.
- `summary_numbers` is **context only**. Document this explicitly in the
  system prompt: the LLM may *quote* a number from `summary_numbers` ("you
  mentioned $1,711"), but any **new** number in the reply must come from a
  tool call. This is the guardrail against the LLM cosplaying a query.
- When opened from the chat tab (no dashboard underneath), `chart_context` is
  `null`. The LLM falls back to the low-confidence path.
- On Transactions tab, `summary_numbers` is omitted (the donut/top-5 shape
  doesn't fit a list). The other three fields still come through.

The drawer can derive these from a small `window.dashboardState` object that
the dashboard template maintains on tab/period/filter change. If
`dashboardState` doesn't exist yet, this design adds it.

---

## 8. Response shape

```json
{
  "text": "Looking at Dining for May 2026 — most of it was a few big nights out:",
  "blocks": [
    {
      "type": "table",
      "title": "Top dining merchants — May 2026",
      "columns": ["Merchant", "Spent", "Visits"],
      "rows": [
        ["Don Angie", "$412.00", 2],
        ["Levain",    "$187.50", 5]
      ]
    }
  ]
}
```

Block types:

- **`table`** — `{title, columns: [string], rows: [[cell, …]]}`. Cells are
  pre-formatted strings (the agent formats money as `$X.XX`). Rendered as a
  compact HTML table beneath the text bubble.
- **`chart_spec`** — `{title, chart_type: "bar"|"line"|"doughnut",
  labels: [string], series: [{name, data: [number]}]}`. Rendered via Chart.js
  (already loaded on the dashboard). Width fits the drawer (≤340px), height
  ~180px.

Plain-text replies omit `blocks` entirely (no empty array). The full-page
`chat.html` renderer ignores `blocks` for now — drill-down is a
dashboard-drawer feature in this phase. We add the same renderer to
`chat.html` only if user testing shows people want to drill down from the
chat tab too.

Backward compat: any older client that ignores extra JSON fields keeps
working. The `text` field is unchanged in shape from today's `/chat` response.

---

## 9. Backend surface

**Changed:**

- `POST /chat` — body extended with optional `chart_context` (JSON string in
  the form payload, parsed server-side). Response becomes `{text, blocks?}`.

**New:**

- `GET /chat/tools` — local debug endpoint that returns the in-process
  registry as JSON `{tools: [{name, description, inputSchema}]}`. Useful for
  inspecting what tools the agent sees during development; **not** an
  externally-consumed surface. Only bound to `127.0.0.1` (localhost) and
  excluded from any production deployment that exposes the backend beyond
  the same host. If a Tryout deploy is reachable from outside `localhost`,
  this route is removed or hidden behind dev-only auth.

No routes for individual tool *calls*. Tools are dispatched in-process from
the agent loop; there is no HTTP path that lets a caller (internal or
external) invoke a tool directly. This keeps the only way to query user data
through the agent — which means the system prompt, clarification policy, and
recon-only data source all apply uniformly.

---

## 10. Files to add or modify

| Path                                       | Change | Purpose                                                                                                |
| ------------------------------------------ | ------ | ------------------------------------------------------------------------------------------------------ |
| `src/chat_tools.py`                        | new    | `ToolSpec` dataclass, `REGISTRY`, 9 handlers, `to_openai_tools()` adapter, `dispatch()`, input validator |
| `src/chat_agent.py`                        | new    | `ChatAgent.run(user_id, message, history, chart_context)` implementing the bounded loop                |
| `src/prompts/chat_drill_down.txt`          | new    | System prompt encoding tone + tool-use defaults + clarification rules                                  |
| `src/llm_orchestrator.py`                  | edit   | Optional thin `chat_with_tools(...)` helper if it cleans up the loop                                   |
| `src/companion.py`                         | edit   | Route the `question` intent through `ChatAgent.run()` instead of `answer_question`. Other intents unchanged. |
| `src/web_chat.py`                          | edit   | `/chat` accepts `chart_context`, returns `{text, blocks?}`. Add `GET /chat/tools`.                     |
| `src/dashboard_queries.py`                 | edit   | Add `category_trend`, `top_merchants`, `compare_periods` helpers. `spending_breakdown` stays as-is; `group_by` lives in `chat_tools.py`. |
| `src/templates/_chat_drawer.html`          | edit   | Read `window.dashboardState`, POST `chart_context`, render `blocks[]` as table or Chart.js canvas      |
| `src/templates/dashboard.html`             | edit   | Maintain `window.dashboardState` on tab/period/filter change                                           |
| `src/templates/chat.html`                  | edit   | Render `text`; ignore `blocks` (note as follow-up)                                                     |
| `tests/test_chat_tools.py`                 | new    | Per-tool unit tests against `data/transactions.db` or seeded SQLite. Empty result, category miss, account filter, date-range edges. |
| `tests/test_chat_agent.py`                 | new    | Mock LLM client. Assert: (a) tool_call → handler → result → next iter; (b) loop terminates at MAX_ITERS; (c) chart_context lands in system message; (d) low-confidence path asks a question without a tool call. |

---

## 11. Tone & UX details (drawer)

- First line of any drill-down reply states the scope: "Looking at Dining for
  May 2026 —". Drop this only when the reply is a clarification question.
- Body is 1–3 short sentences before/after the block.
- Numbers: `$` prefix, two decimals, thousands separator above $1,000
  (`$1,711.42`), no separator below ($412.00).
- Inline chart colors reuse `CATEGORY_PALETTE` + `donutColor(i)` already
  defined in `dashboard.html` (the golden-angle generator). Move the constants
  into a small shared `<script>` partial — `src/templates/_chart_palette.html`
  — included by both `dashboard.html` and `_chat_drawer.html`. Avoids
  duplication and keeps drift impossible.
- Table styling: thin gray header, no zebra striping, right-align numbers.
  Match the compact density of the existing breakdown tables on the dashboard.
- The drawer collapses to a floating bubble as today; nothing here changes
  that.

---

## 12. Verification plan

End-to-end smoke from a fresh checkout with the live SQLite DB:

1. `WEB_CHAT_PORT=8080 python -m src.web_chat`
2. Open `http://localhost:8080/dashboard`, go to **Spending → May 2026**.
3. Open the drawer, send **"break down my dining"**. Confirm:
   - Network: request body includes `chart_context.chart_key = "spending"` and
     `selected_category = "Dining"`.
   - Response: includes a `table` block titled "Top dining merchants — May
     2026" with ≥3 rows.
   - Text opens with "Looking at Dining for May 2026 —".
4. Send **"show me dining trend past 3 months"**. Confirm:
   - Response includes a `chart_spec` block with `chart_type = "bar"` and
     three labels spanning March–May 2026 (the override worked).
   - Drawer renders a small Chart.js bar chart.
5. Open `/chat` (full-page tab, no dashboard underneath). Send **"how am I
   doing on travel?"**. Confirm:
   - No tool call in the server logs.
   - Response is a single short question ("Just this month, or the year so
     far?").
6. `curl http://localhost:8080/chat/tools | jq '.tools[].name'` lists 9 tools.
7. Tail server logs while sending each message above and confirm:
   - High-confidence cases call ≤2 tools.
   - Low-confidence cases call zero tools.
   - No turn exceeds MAX_ITERS or the wall budget.
8. `pytest tests/test_chat_tools.py tests/test_chat_agent.py` is green.

---

## 13. Open items (deferred to implementation)

- **`summary_numbers` on Transactions tab.** It probably shouldn't be sent —
  the donut/top-5 shape doesn't fit a transaction list. Decide once the
  drawer wiring is in.
- **"Use full year" shortcut on clarification.** When the LLM asks a focused
  follow-up, surface 1–2 chip buttons under the reply ("This month", "Year to
  date") that auto-fill the next message. Small UX nicety; not required for
  the design to land.
- **Tool-call streaming vs. all-at-once.** All-at-once is fine for v1. If the
  drawer feels slow on the trend example, add streaming as a follow-up.
- **Merchant-canonicalization regex.** Lives in `chat_tools.py` for now.
  If `transactions_filtered` ever needs the same logic, lift it into
  `dashboard_queries.py` then.
- **Pin-from-chat button.** The `chart_spec` block already carries enough
  data to persist; designing the button is its own follow-up.
