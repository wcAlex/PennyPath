"""Cached LLM annotations for dashboard charts (Phase 1B).

The cache key is (chart_key, period_key). The payload_hash protects against
stale annotations after new data ingestion — if the freshly aggregated payload
differs from what the LLM last saw, we regenerate automatically. A `force`
flag also lets the user explicitly request a refresh.

See design/ui_dashboard.md §5.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Optional

from src import llm_orchestrator
from src.storage import (
    BudgetHint,
    ChartAnnotationStore,
    UserConfig,
    UserConfigStore,
)


def _payload_hash(payload: dict) -> str:
    """SHA1 over a canonicalized JSON dump of the payload."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _wiki_slice_from(wiki_text: str) -> str:
    """Pull the 'Goal' + 'Active Concerns' sections out of the wiki.

    The wiki is short Markdown the wiki_updater maintains; if those sections
    aren't present, fall back to the first ~500 chars so the LLM has *some*
    context. Cheap and good enough for Phase 1B.
    """
    if not wiki_text:
        return ""
    text = wiki_text.strip()
    interesting = []
    for marker in ("## Goal", "## Active Concerns", "## Goals", "## Concerns"):
        idx = text.find(marker)
        if idx == -1:
            continue
        # Take the section up to the next top-level heading.
        section = text[idx:]
        next_heading = section.find("\n## ", 1)
        if next_heading != -1:
            section = section[:next_heading]
        interesting.append(section.strip())
    if interesting:
        return "\n\n".join(interesting)
    return text[:500]


def get_or_generate_annotation(
    user_id: str,
    chart_key: str,
    period_key: str,
    payload: dict,
    user_config: Optional[UserConfig] = None,
    wiki_text: str = "",
    force: bool = False,
) -> dict:
    """Return a cached or freshly generated annotation for one (user, chart, period).

    The cache is per-user so two tenants never read each other's insights.

    Returns:
        {
            "annotation": str,
            "suggestions": [str, ...],
            "generated_at": ISO timestamp,
            "cached": bool,
        }
    """
    hash_now = _payload_hash(payload)
    cached = ChartAnnotationStore.get(user_id, chart_key, period_key)

    if cached and not force and cached.get("payload_hash") == hash_now:
        return {
            "annotation": cached["annotation_text"],
            "suggestions": cached["suggestions"],
            "generated_at": cached["generated_at"],
            "cached": True,
        }

    cfg = user_config if user_config is not None else UserConfigStore.load()
    goal_key = getattr(cfg, "goal_key", "") or ""
    goal_text = getattr(cfg, "goal_text", "") or ""
    wiki_slice = _wiki_slice_from(wiki_text)

    result = llm_orchestrator.generate_chart_annotation(
        chart_key=chart_key,
        payload=payload,
        goal_key=goal_key,
        goal_text=goal_text,
        wiki_slice=wiki_slice,
    )

    annotation_text = result.get("annotation", "")
    suggestions = result.get("suggestions", []) or []

    ChartAnnotationStore.upsert(
        user_id=user_id,
        chart_key=chart_key,
        period_key=period_key,
        payload_hash=hash_now,
        annotation_text=annotation_text,
        suggestions=suggestions,
    )

    fresh = ChartAnnotationStore.get(user_id, chart_key, period_key) or {}
    return {
        "annotation": annotation_text,
        "suggestions": suggestions,
        "generated_at": fresh.get("generated_at") or datetime.now().isoformat(),
        "cached": False,
    }


def get_or_generate_derived_budget(
    user_config: Optional[UserConfig] = None,
    recent_category_avgs: Optional[dict] = None,
    wiki_text: str = "",
    force: bool = False,
) -> list[dict]:
    """Return the user's derived_budget list, regenerating if stale or forced.

    "Stale" = the user has no derived_budget yet OR has no generated_at timestamp.
    The caller can pass force=True to always regenerate. The resulting list is
    persisted back to UserConfig.derived_budget.
    """
    cfg = user_config if user_config is not None else UserConfigStore.load()

    needs_regen = force or not cfg.derived_budget or not cfg.derived_budget_generated_at
    if not needs_regen:
        return [
            {"category": b.category, "hint_text": b.hint_text}
            for b in cfg.derived_budget
        ]

    goal_key = getattr(cfg, "goal_key", "") or ""
    goal_text = getattr(cfg, "goal_text", "") or ""
    wiki_slice = _wiki_slice_from(wiki_text)

    hints = llm_orchestrator.generate_derived_budget(
        goal_key=goal_key,
        goal_text=goal_text,
        recent_category_avgs=recent_category_avgs or {},
        wiki_slice=wiki_slice,
    )

    if hints:
        cfg.derived_budget = [
            BudgetHint(category=h["category"], hint_text=h["hint_text"])
            for h in hints
        ]
        cfg.derived_budget_generated_at = datetime.now().isoformat()
        UserConfigStore.save(cfg)
    # If the LLM call failed entirely, leave the existing budget alone so the
    # user doesn't see a sudden blank surface.
    return [
        {"category": b.category, "hint_text": b.hint_text}
        for b in cfg.derived_budget
    ]


__all__ = [
    "get_or_generate_annotation",
    "get_or_generate_derived_budget",
]
