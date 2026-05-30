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

**Step 3 — Set a goal**
Choose from four goal types, or define a custom intention:
- Build an emergency fund
- Reduce discretionary spending (e.g., dining, subscriptions)
- Save for a specific purchase
- Get out of debt

Goals are framed as loose intentions — no hard targets or deadlines required. The companion adapts to where the user actually is, not where a plan says they should be.

**Step 4 — Connect your accounts**
Two options:
- **Plaid** — link bank accounts and credit cards directly; transactions sync automatically (read-only)
- **Manual upload** — export a PDF or CSV statement from your bank and upload it; PennyPath parses and normalizes the data

Users can mix both methods (e.g., Plaid for one bank, manual upload for another). Account settings are always accessible for adding, removing, or re-linking accounts.

---

## Features

### Feature 1: Personal Finance Visibility (Base Plan)

The foundational feature, included for all users. Gives users a clear, honest picture of their spending without judgment or pressure.

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

**(2) Consulting — Ask Anything**

An interactive chat interface where users can ask questions about their finances in plain language. The companion draws on the user's real transaction data to give specific, relevant answers:

> "How much did I spend on dining last month?"
> "What subscriptions am I paying for?"
> "How does this week compare to my usual?"

Beyond answering, the chat can generate a chart on the fly and pin it to the dashboard — this is how the dashboard becomes personalized (see the Personal Dashboard above).

**(3) Monthly Analysis**

A monthly narrative generated by the LLM based on the user's full statement data:
- Anomalies — unusual spikes, new recurring charges, merchants that appeared for the first time
- Optimization opportunities — duplicate subscriptions, category creep, charges that might be worth reconsidering
- Progress observations — how the month tracked against the user's stated goal, without grades

Framed as a story, not a report. Delivered in-app at the start of each new month.

---

### Feature 2: Coaching Plan (Optional, Paid)

A structured, time-limited coaching engagement for users who want active help changing their habits — not just visibility into them.

**Pricing:** $9.99/month, 7-day free trial. Designed as a 6-month program.

**Philosophy:** The goal is to help users graduate as quickly as possible — back to the base plan, with better habits and no ongoing coaching fees. Success is measured by how many users no longer need coaching, not by how long they stay subscribed.

**How it works:**

The companion and user co-create a coaching plan based on the user's goal, profile, and current spending patterns. The plan is structured in three progressive phases:

| Phase | Duration | Focus |
|---|---|---|
| Foundation | 7 days | Awareness only — observe spending without changing anything |
| Building | 21 days | One micro-habit introduced; light, optional accountability |
| Momentum | 3 months | Gradual reinforcement; celebrating consistency over perfection |

Each phase has a clear target and a clear end date. Completing a phase is a milestone worth acknowledging.

**Cadence:**
- Bi-weekly check-ins from the companion — short, warm, specific to recent activity
- Weekly progress summary against the current phase target
- Positive reinforcement for streaks and small wins; no shame or pressure for setbacks

---

## Privacy & Trust

Trust is the product's foundation. The decisions below are deliberate:

- **Plaid read-only** — PennyPath never initiates payments or transfers. It can only read transaction history.
- **Secure storage, minimized** — your finance records are stored so the dashboard, search, and history can work — encrypted at rest, kept no longer than needed, and limited to what the analysis requires.
- **Pause anytime** — users can mute or pause the companion at any time; the app respects it without asking why.
- **No data selling** — financial behavior data is never monetized through third-party sale or advertising targeting.
- **Deleted when you leave** — closing your account permanently deletes all of your personal data — finance records, linked accounts, the companion's memory, and conversation history. Users can also delete their data at any time from settings.
