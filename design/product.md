# PennyPath — Product Document

## Vision

Most people don't fail at managing money because they're bad with numbers. They fail because the tools they use make them feel bad about themselves.

PennyPath is a personal finance companion — a buddy, not a budget enforcer. It works on the premise that small, consistent, judgment-free nudges build lasting financial habits better than aggressive goal-setting or score-based pressure ever could. We meet people where they are, not where a spreadsheet says they should be.

**Core idea:** Your money, at your own pace.

---

## How PennyPath is Different

| Dimension | Typical Finance Apps | PennyPath |
|---|---|---|
| Tone | Alerting, score-based | Warm, conversational |
| Primary surface | Dashboard (user pulls) | Annotated, personalized dashboard + a companion that reaches out |
| Goal style | Hard targets, budgets | Loose intentions, progressive habits |
| Reaction to overspending | Red bar, warning alert | Curiosity, no judgment |
| Engagement model | User visits app when anxious | App checks in with user |
| Habit approach | Track and restrict | Observe, reflect, celebrate |

Tools like Mint, YNAB, or Copilot are powerful — but they're designed for people who are already motivated and organized. PennyPath is designed for everyone else: people who want to do better with money but find traditional apps stressful, overwhelming, or shaming.

The daily experience isn't a dashboard you have to remember to open and decode — it's a dashboard the companion annotates and keeps current, paired with a companion that checks in with you and answers questions in plain language, all without pressure.

---

## User Workflow

### Onboarding (one-time setup)

**Step 1 — Create an account**
Register with email. No financial information is collected at this step.

**Step 2 — Select a finance profile**
Choose from four life-stage profiles, or describe your own situation:
- Early career — building savings habits from scratch
- Growing family — managing more complex, higher-stakes spending
- Paying down debt — focused on reducing balances
- Building wealth — optimizing and investing surplus

The profile primes the companion's tone and shapes what the dashboard highlights. Users can update it anytime.

**Step 3 — Pick a starting intention**
Choose a frame to start with, or describe your own:
- Build an emergency fund
- Reduce discretionary spending (e.g., dining, subscriptions)
- Save for a specific purchase
- Get out of debt

This is just the opening frame. Once a statement is in, the companion helps turn it into something concrete and workable — see **Goal Shaping** below. Goals stay loose intentions throughout — no hard targets, no deadlines, no shaming.

**Step 4 — Connect your accounts**

**Manual upload** is the primary path. Export a PDF or CSV statement from your bank and upload it; PennyPath parses and normalizes the data. Upload more statements at any time to keep the picture current.

**Direct bank linking via Plaid** is on the roadmap. We deliberately start with upload — connecting a bank is the wrong trust ask on day one. When Plaid arrives, it unlocks real-time features (bill reminders, purchase warnings) that statement upload alone can't support.

Account settings are always accessible for adding or removing accounts.

---

## Features

PennyPath is one product, not a tiered program. The companion is the product: it ingests the user's data, surfaces insight on an annotated dashboard, lets the user shape that dashboard through chat, and uses chat to discuss what to do next. Later features (bill reminders, purchase warnings, spending-reduction recommendations) extend this same companion — they do not replace it with something else.

### The Companion Experience

A clear, honest picture of the user's spending without judgment or pressure, plus a conversation about it.

**(1) Personal Dashboard**

A home screen generated from the user's actual data, shaped by their profile and goal. It has four parts:

*Standard charts.* Four base views, always available:
- **Spending** — total for the period and a category breakdown (donut + list)
- **Income** — total for the period, a source breakdown, and a 12-month history
- **Transactions** — a filterable list of individual records
- **Cash Flow** — income vs. spending across the last 12 months

Internal transfers (e.g. paying your own credit card) are excluded from spending and income totals, so the numbers reflect real money in and out. The dashboard is observational, not evaluative — no red numbers, no scores, no "shortfall" or "you overspent" framing.

*LLM annotation layer.* On top of each chart, the companion adds a short, warm annotation and, where useful, a gentle suggested action — turning a raw chart into something understandable and actionable ("your dining held steady this month — nice"; "two streaming subscriptions look similar, worth a glance?"). Annotations refresh when new data syncs, with a manual refresh always available.

*Chat-driven drill-down.* From any chart the user can ask follow-up questions in plain language ("why was March higher?", "show me just weekends"). The companion answers and can generate a new chart tailored to the question.

*Pinnable custom charts.* Any chart the companion generates in chat can be pinned to the dashboard, where it persists alongside the standard charts. Over time the dashboard becomes personalized to what each user actually cares about — that is the customized experience.

*Goal progress.* Each active intention (see Goal Shaping below) gets its own tile showing where the user stands — current spend against the intention, where they are within the timeframe. No red bars, no "you failed" framing — observations the user can act on.

**(2) Goal Shaping**

A single monthly target rarely matches real life — ski seasons, holiday travel, an annual gear refresh. Real spending is lumpy.

After the first upload, the companion uses the user's actual patterns to help shape richer, workable intentions. Goals can be:

- **Category-scoped** — "~$40k/year on ski and travel," "~$20k/year on clothes and gear"
- **Multi-timeframe** — annual ceilings for seasonal categories, monthly intentions for steady ones, one-off targets for purchases
- **Co-created** — the companion proposes numbers grounded in actual history ("you've averaged $X here over the last 12 months — does this feel right?"); the user accepts, edits, or rejects

The dashboard shows progress against each active intention. The companion can surface gentle observations ("you're at 80% of your ski intention with two months left — heads up") and call out the specific transactions driving the trend.

**Preventive suggestions** — a heads-up *before* a spend pushes the user past intention — are partial from statement data (the companion flags at the next sync) and become real-time once direct bank linking arrives (see Roadmap).

Editing a goal mid-year is fine. Life changes; the intention should follow.

**(3) Consulting — Ask Anything**

An interactive chat interface where users can ask questions about their finances in plain language. The companion draws on the user's real transaction data to give specific, relevant answers:

> "How much did I spend on dining last month?"
> "What subscriptions am I paying for?"
> "How does this week compare to my usual?"

Beyond answering, the chat can generate a chart on the fly and pin it to the dashboard — this is how the dashboard becomes personalized (see the Personal Dashboard above).

**(4) Monthly Analysis**

A monthly narrative generated by the LLM based on the user's full statement data:
- Anomalies — unusual spikes, new recurring charges, merchants that appeared for the first time
- Optimization opportunities — duplicate subscriptions, category creep, charges that might be worth reconsidering
- Progress observations — how the month tracked against the user's active intentions (monthly, seasonal, annual), without grades

Framed as a story, not a report. Delivered in-app at the start of each new month.

---

### Roadmap

The product grows along one arc: spending analysis → proactive account features → recommendations. Each stage extends the same companion. The first stage is free; later stages require direct bank linking and sit behind a subscription, because the value they add — and the infrastructure they need — is meaningfully larger.

**Bill & payment reminders.** Once a bank account is linked, the companion can recognize recurring bills and remind the user before they're due. Tone stays warm, never alarming.

**Purchase warnings.** Gentle, real-time observations when a charge is unusually large for a category, would push an account below a threshold the user set, or would take the user past an active intention (e.g. "this charge would put your ski spend over your annual intention"). Always framed as "thought you'd want to know," never as a block.

**Spending-reduction recommendations.** Concrete, data-grounded suggestions — duplicate subscriptions, category trims, downgrade paths — that explicitly preserve a user-declared set of things they value. The companion never recommends cutting something the user cares about.

See `design/phases.md` for the development sequence.

---

## Privacy & Trust

Trust is the product's foundation. The decisions below are deliberate:

- **Plaid read-only** — PennyPath never initiates payments or transfers. It can only read transaction history.
- **Secure storage, minimized** — your finance records are stored so the dashboard, search, and history can work — encrypted at rest, kept no longer than needed, and limited to what the analysis requires.
- **Pause anytime** — users can mute or pause the companion at any time; the app respects it without asking why.
- **No data selling** — financial behavior data is never monetized through third-party sale or advertising targeting.
- **Deleted when you leave** — closing your account permanently deletes all of your personal data — finance records, linked accounts, the companion's memory, and conversation history. Users can also delete their data at any time from settings.
