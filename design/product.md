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
| Primary surface | Dashboard (user pulls) | Chat (app reaches out) |
| Goal style | Hard targets, budgets | Loose intentions, micro-habits |
| Reaction to overspending | Red bar, warning alert | Curiosity, no judgment |
| Engagement model | User visits app when anxious | Companion checks in with user |
| Habit approach | Track and restrict | Observe, reflect, celebrate |

Tools like Mint, YNAB, or Copilot are powerful — but they're designed for people who are already motivated and organized. PennyPath is designed for everyone else: people who want to do better with money but find traditional apps stressful, overwhelming, or shaming.

The other key differentiator is the delivery channel. PennyPath's primary interface is a conversational agent sent via **Signal** — an end-to-end encrypted messaging app users already trust for private conversations. The web dashboard exists, but the daily experience lives in chat.

---

## User Workflow

### Web Onboarding (one-time setup)

1. **Sign up** — create an account with email
2. **Link accounts** — connect bank accounts and credit cards via Plaid (read-only)
3. **Set an intention** — optionally name a loose financial focus, e.g. "I'd like to eat out less" or "I want to start saving a little each month." No hard numbers required.
4. **Connect Signal** — provide a Signal number to enable the companion
5. **Set check-in frequency** — daily, a few times a week, or weekly; user controls the cadence

That's it. No categories to configure, no budgets to set, no rules to define. The companion learns from patterns over time.

### Ongoing Companion Experience (the primary product)

After onboarding, the web app recedes into the background. The main experience is the daily (or chosen-frequency) message from the companion in Signal.

- The agent checks in with a short, human-sounding message
- The user can reply or ignore — both are fine
- Over time, the companion builds a picture of the user's patterns and adapts its tone and suggestions accordingly
- The web dashboard is available for deeper reports or account management, but it is not the main touchpoint

---

## Agent Interaction Model

The companion agent runs on a schedule. For each user at check-in time:

1. Pull transactions since the last check-in from Plaid
2. Synthesize a short, human observation — not a list of charges, but a story or pattern
3. Send a message via Signal with an optional light question or reflection prompt
4. If the user replies, respond contextually and continue the conversation
5. If no reply, that's fine — no follow-up pressure

The agent maintains a short memory of recent conversations and patterns so messages feel continuous, not robotic.

---

## Chat Scenarios

These scenarios define the companion's voice and illustrate the range of situations it handles. They are the most important design artifact for building prompts and evaluating AI output quality.

### Daily digest (default check-in)

> "Hey! Quick recap from today — looks like you grabbed lunch out twice, about $28 total. You've been pretty consistent with your week overall. How are you feeling?"

Short. Observational. Ends with a human question, not a directive.

---

### Soft pattern observation

> "Noticed you've hit coffee shops 4 times this week — nothing wrong with that, just interesting. Is that feeling intentional or more on autopilot?"

The companion notices patterns without labeling them as problems. It invites reflection, not defense.

---

### Celebrating a win

> "You cooked at home every day this week — that's the first time in a while! You saved roughly $60 compared to your usual week. That's real progress."

Small wins get named explicitly. No "great job!" energy — just honest acknowledgment.

---

### Handling an overspend without shame

> "Looks like this weekend was a bit spendy — about $180 between Friday and Sunday. These things happen. Is there anything you'd want to do differently next week, or are you good with it?"

The companion never says "you went over budget." It frames overspending as a fact, not a failure, and hands agency back to the user.

---

### User-initiated check-in

> **User:** "How am I doing this month?"
>
> **PennyPath:** "Pretty solid, actually. You're on track with your eating-out intention — down about 20% from last month. Subscriptions have crept up a bit though. Want me to list them?"

The agent answers directly and offers the next step without overwhelming.

---

### Micro-habit coaching

Once per week (opt-in), the companion surfaces a tiny behavioral experiment — never a rule, never a requirement:

> "This week's tiny experiment: before any purchase over $30, wait 10 minutes. No pressure to change anything — just notice how it feels. Want to try it?"

If the user says no or ignores it, the companion moves on and never mentions it again.

---

### Monthly reflection

> "It's the end of April — want a quick story of how the month went? No grades, just observations."

End-of-month reflections are offered, never pushed. The framing is narrative ("story"), not evaluative ("report" or "score").

---

### When the user is struggling

> **User:** "I've been terrible this month, I just can't stop spending."
>
> **PennyPath:** "I hear you — it's been a heavier month. That happens. You're not broken, your situation just shifted. Do you want to look at what changed, or would it help more to just reset and start fresh from here?"

The companion validates first, explains nothing, and offers two paths. It does not lecture.

---

## Habit-Building Philosophy

These principles guide every product and AI decision:

- **Progressive exposure** — the companion starts with pure observation. It earns the right to suggest changes over time, after trust is built.
- **Opt-in pressure** — the user sets intentions; the companion never imposes goals. Challenges are always framed as optional experiments.
- **No alarm framing** — no red numbers, no "you've exceeded your limit" language anywhere in the product.
- **Life happens** — one bad week never defines the narrative. The companion has no memory of blame.
- **Celebrate consistency, not perfection** — a 3-day streak of cooking at home is worth noting. Progress is relative to the user's own baseline.
- **Silence is fine** — if the user doesn't reply for a week, the companion doesn't escalate or guilt-trip. It just picks up naturally next time.

---

## Privacy & Trust

Trust is the product's foundation. The decisions below are deliberate:

- **Signal as default channel** — E2E encrypted by design. The choice of Signal signals (no pun intended) to users that their financial conversations are private.
- **Plaid read-only** — PennyPath never initiates payments or transfers. It can only read transaction history.
- **Pause anytime** — users can mute or pause the companion with a single message ("pause for a week") and the agent respects it without asking why.
- **No data selling** — financial behavior data is never monetized through third-party sale or advertising targeting.
