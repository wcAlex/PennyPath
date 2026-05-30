import json
import os
from collections import defaultdict
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from openai import OpenAI

from src.models import Transaction

load_dotenv()

_PROMPT_PATH = Path(__file__).parent / "prompts" / "companion.txt"
_MONTHLY_PROMPT_PATH = Path(__file__).parent / "prompts" / "monthly_analysis.txt"
_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Generic warm fallback used when the LLM call or JSON parse fails twice.
_FALLBACK_ANNOTATION = {
    "annotation": "Here's how this period looks at a glance. Want me to dig deeper?",
    "suggestions": [],
}


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _load_monthly_prompt() -> str:
    return _MONTHLY_PROMPT_PATH.read_text(encoding="utf-8")


def _client() -> OpenAI:
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise ValueError("LLM_API_KEY environment variable is required")
    base_url = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    return OpenAI(api_key=api_key, base_url=base_url)


def _model() -> str:
    return os.getenv("LLM_MODEL", "deepseek-chat")


def _category_totals(transactions: List[Transaction]) -> dict:
    totals = defaultdict(float)
    for t in transactions:
        totals[t.category] += t.amount
    return dict(totals)


def _format_goals(prefs: dict) -> str:
    goals = prefs.get("goals") or []
    if not goals:
        return "The user hasn't shared any specific goals yet."
    return "The user's stated goals: " + "; ".join(str(g) for g in goals)


def _summarize_for_checkin(transactions: List[Transaction], prefs: dict) -> str:
    if not transactions:
        return (
            "There are no transactions to review for this period.\n"
            + _format_goals(prefs)
        )

    total_spend = sum(t.amount for t in transactions)
    totals = _category_totals(transactions)
    top_categories = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
    large = [t for t in transactions if t.amount > 100]

    lines = [f"Total spend this period: ${total_spend:.2f} across {len(transactions)} transactions."]
    lines.append("Top categories by spend:")
    for cat, amt in top_categories:
        lines.append(f"  - {cat}: ${amt:.2f}")
    if large:
        lines.append("Notable larger transactions (over $100):")
        for t in large:
            lines.append(f"  - {t.date} {t.description} ({t.category}): ${t.amount:.2f}")
    lines.append(_format_goals(prefs))
    return "\n".join(lines)


def _recent_transaction_context(transactions: List[Transaction]) -> str:
    recent = sorted(transactions, key=lambda t: t.date, reverse=True)[:30]
    if not recent:
        return "No recent transactions available."
    totals = _category_totals(recent)
    lines = [f"Recent transactions ({len(recent)} most recent), grouped by category:"]
    for cat, amt in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"  - {cat}: ${amt:.2f}")
    return "\n".join(lines)


def generate_checkin(transactions: List[Transaction], prefs: dict, wiki: str = "") -> str:
    client = _client()
    user_message = (
        "Here's a summary of the user's recent activity. Write a daily check-in.\n\n"
        + _summarize_for_checkin(transactions, prefs)
    )
    system_content = _load_system_prompt()
    if wiki:
        system_content = system_content + "\n\n## What I know about this user\n" + wiki
    response = client.chat.completions.create(
        model=_model(),
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content


def generate_monthly_analysis(transactions: List[Transaction], prefs: dict, wiki: str = "") -> str:
    """Generate a monthly narrative analysis. Uses a separate prompt from check-ins."""
    client = _client()

    total_spend = sum(t.amount for t in transactions)
    totals = _category_totals(transactions)
    categories_sorted = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    large = [t for t in transactions if t.amount > 100]

    lines = [
        f"Total spend this month: ${total_spend:.2f} across {len(transactions)} transactions.",
        "All categories by spend:",
    ]
    for cat, amt in categories_sorted:
        lines.append(f"  - {cat}: ${amt:.2f}")
    if large:
        lines.append("Large charges (over $100):")
        for t in large:
            lines.append(f"  - {t.date} {t.description} ({t.category}): ${t.amount:.2f}")
    lines.append(_format_goals(prefs))

    user_message = (
        "Here's a summary of the user's full month. Write a monthly narrative analysis.\n\n"
        + "\n".join(lines)
    )

    system_content = _load_monthly_prompt()
    if wiki:
        system_content = system_content + "\n\n## What I know about this user\n" + wiki

    response = client.chat.completions.create(
        model=_model(),
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content


def generate_dashboard_summary(transactions: List[Transaction], prefs: dict) -> dict:
    """Pre-process transactions for the dashboard. No LLM call."""
    totals = _category_totals(transactions)
    total_spend = sum(t.amount for t in transactions)
    top_categories = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
    large_charges = [t for t in transactions if t.amount > 100]

    return {
        "category_totals": totals,
        "total_spend": total_spend,
        "transaction_count": len(transactions),
        "top_categories": top_categories,
        "large_charges": large_charges,
    }


def answer_question(
    question: str,
    transactions: List[Transaction],
    history: List[dict],
    prefs: dict,
    image_b64: str = None,
    mime_type: str = "image/jpeg",
    wiki: str = "",
) -> str:
    client = _client()
    context = (
        _recent_transaction_context(transactions)
        + "\n"
        + _format_goals(prefs)
    )
    text_content = f"{question}\n\n[Context for you]\n{context}"

    if image_b64:
        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
            {"type": "text", "text": text_content},
        ]
    else:
        user_content = text_content

    system_content = _load_system_prompt()
    if wiki:
        system_content = system_content + "\n\n## What I know about this user\n" + wiki

    messages = [{"role": "system", "content": system_content}]
    # Strip metadata keys (timestamp, session_id) that the LLM API does not accept
    clean_history = [{"role": h["role"], "content": h["content"]} for h in history]
    messages.extend(clean_history)
    messages.append({"role": "user", "content": user_content})

    response = client.chat.completions.create(
        model=_model(),
        messages=messages,
    )
    return response.choices[0].message.content


# --- Dashboard insights (Phase 1B) -------------------------------------------


_CHART_PROMPTS = {
    "spending":     "dashboard_spending.txt",
    "income":       "dashboard_income.txt",
    "transactions": "dashboard_transactions.txt",
    "cashflow":     "dashboard_cashflow.txt",
}


def _load_prompt(filename: str) -> str:
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")


def _strip_code_fences(text: str) -> str:
    """Tolerate models that wrap JSON in ```json ... ``` despite instruction."""
    s = text.strip()
    if s.startswith("```"):
        # Remove leading fence (possibly ```json).
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _safe_json_loads(raw: str) -> dict | None:
    try:
        return json.loads(_strip_code_fences(raw))
    except (json.JSONDecodeError, TypeError):
        return None


def _format_goal_block(goal_key: str, goal_text: str) -> str:
    if not goal_key and not goal_text:
        return "User goal: not set yet — keep guidance neutral and exploratory."
    parts = []
    if goal_key:
        parts.append(f"goal_key={goal_key}")
    if goal_text:
        parts.append(f'goal_text="{goal_text}"')
    return "User goal: " + "; ".join(parts)


def _call_llm_json(
    system_prompt: str,
    user_message: str,
    *,
    retries: int = 1,
) -> dict | None:
    """Call the LLM in JSON mode (where supported), tolerate a parse failure once."""
    client = _client()

    last_error: Exception | None = None
    attempts = retries + 1
    for _ in range(attempts):
        try:
            kwargs = {
                "model": _model(),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            }
            # DeepSeek + OpenAI-compatible servers usually accept this; if a
            # server rejects it, retry without it on the next pass.
            try:
                response = client.chat.completions.create(
                    response_format={"type": "json_object"}, **kwargs
                )
            except Exception:
                response = client.chat.completions.create(**kwargs)
            content = response.choices[0].message.content or ""
            parsed = _safe_json_loads(content)
            if parsed is not None:
                return parsed
        except Exception as e:  # network errors, etc.
            last_error = e
    # Give up — caller decides on the fallback.
    if last_error is not None:
        # Keep a breadcrumb for debugging; never surface to the user.
        try:
            import sys as _sys
            print(f"[llm_orchestrator] LLM call failed: {last_error!r}", file=_sys.stderr)
        except Exception:
            pass
    return None


def generate_chart_annotation(
    chart_key: str,
    payload: dict,
    goal_key: str,
    goal_text: str,
    wiki_slice: str = "",
) -> dict:
    """Generate a warm 1–2 sentence annotation + up to 2 suggestions for one chart.

    Returns: {"annotation": str, "suggestions": [str, ...]}

    Falls back to a generic warm annotation on persistent parse / network failure.
    """
    prompt_file = _CHART_PROMPTS.get(chart_key)
    if not prompt_file:
        # Unknown chart_key — degrade gracefully.
        return dict(_FALLBACK_ANNOTATION)

    system_prompt = _load_prompt(prompt_file)
    goal_block = _format_goal_block(goal_key, goal_text)
    wiki_block = wiki_slice.strip() if wiki_slice else "(no wiki context available)"

    user_message = (
        f"Chart payload (JSON):\n{json.dumps(payload, sort_keys=True)}\n\n"
        f"{goal_block}\n\n"
        f"What I know about this user:\n{wiki_block}\n\n"
        "Respond with JSON only matching the schema in the system prompt."
    )

    parsed = _call_llm_json(system_prompt, user_message, retries=1)
    if not parsed or not isinstance(parsed, dict):
        return dict(_FALLBACK_ANNOTATION)

    annotation = str(parsed.get("annotation", "")).strip()
    suggestions = parsed.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []
    suggestions = [str(s).strip() for s in suggestions if str(s).strip()]
    if not annotation:
        return dict(_FALLBACK_ANNOTATION)
    return {"annotation": annotation, "suggestions": suggestions[:3]}


def generate_derived_budget(
    goal_key: str,
    goal_text: str,
    recent_category_avgs: dict,
    wiki_slice: str = "",
) -> list[dict]:
    """Generate text budget hints for the user's top categories.

    Returns: list of {"category": str, "hint_text": str}. Up to ~6 items.

    Falls back to an empty list on persistent parse / network failure — the
    caller can decide whether to keep the previous derived_budget.
    """
    system_prompt = _load_prompt("derived_budget.txt")
    goal_block = _format_goal_block(goal_key, goal_text)
    wiki_block = wiki_slice.strip() if wiki_slice else "(no wiki context available)"

    # Round averages for prompt cleanliness.
    avgs_clean = {
        str(k): round(float(v), 2)
        for k, v in (recent_category_avgs or {}).items()
        if v is not None
    }

    user_message = (
        f"Recent 3-month per-category averages (USD):\n"
        f"{json.dumps(avgs_clean, sort_keys=True)}\n\n"
        f"{goal_block}\n\n"
        f"What I know about this user:\n{wiki_block}\n\n"
        "Respond with JSON only matching the schema in the system prompt."
    )

    parsed = _call_llm_json(system_prompt, user_message, retries=1)
    if not parsed or not isinstance(parsed, dict):
        return []

    hints_raw = parsed.get("hints", [])
    if not isinstance(hints_raw, list):
        return []

    hints: list[dict] = []
    for entry in hints_raw:
        if not isinstance(entry, dict):
            continue
        cat = str(entry.get("category", "")).strip()
        text = str(entry.get("hint_text", "")).strip()
        if cat and text:
            hints.append({"category": cat, "hint_text": text})
    return hints[:6]
