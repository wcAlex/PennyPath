"""Tests for src/dashboard_insights.py — the cached LLM annotation layer.

These use the conftest `tmp_data` autouse fixture, so ChartAnnotationStore
writes into a throwaway DB. We mock llm_orchestrator.generate_chart_annotation
to avoid real network calls and to count invocations.
"""

from datetime import date  # noqa: F401  (kept handy for future cases)

import pytest

from src import dashboard_insights
from src.storage import ChartAnnotationStore, UserConfig


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the LLM call with a counting stub."""
    calls = {"count": 0}

    def _fake(chart_key, payload, goal_key, goal_text, wiki_slice=""):
        calls["count"] += 1
        return {
            "annotation": f"warm annotation #{calls['count']}",
            "suggestions": ["try this", "and this"],
        }

    monkeypatch.setattr(
        dashboard_insights.llm_orchestrator,
        "generate_chart_annotation",
        _fake,
    )
    return calls


SPENDING_PAYLOAD = {
    "period": "2026-05",
    "total_spend": 1000.0,
    "categories": [{"name": "Dining", "amount": 400.0, "avg_6mo": 350.0}],
    "previous_period_total": 900.0,
}


class TestAnnotationCache:
    def test_cache_miss_calls_llm_and_upserts(self, fake_llm):
        result = dashboard_insights.get_or_generate_annotation(
            user_id="u1",
            chart_key="spending",
            period_key="2026-05",
            payload=SPENDING_PAYLOAD,
            user_config=UserConfig(name="Test"),
            wiki_text="",
        )
        assert fake_llm["count"] == 1
        assert result["cached"] is False
        assert result["annotation"] == "warm annotation #1"
        assert result["suggestions"] == ["try this", "and this"]
        assert result["generated_at"]

        # It was persisted under this user.
        row = ChartAnnotationStore.get("u1", "spending", "2026-05")
        assert row is not None
        assert row["annotation_text"] == "warm annotation #1"

    def test_cache_hit_skips_llm(self, fake_llm):
        # First call populates the cache.
        dashboard_insights.get_or_generate_annotation(
            user_id="u1",
            chart_key="spending",
            period_key="2026-05",
            payload=SPENDING_PAYLOAD,
            user_config=UserConfig(name="Test"),
        )
        assert fake_llm["count"] == 1

        # Same payload -> same hash -> served from cache, no new LLM call.
        result = dashboard_insights.get_or_generate_annotation(
            user_id="u1",
            chart_key="spending",
            period_key="2026-05",
            payload=SPENDING_PAYLOAD,
            user_config=UserConfig(name="Test"),
        )
        assert fake_llm["count"] == 1
        assert result["cached"] is True
        assert result["annotation"] == "warm annotation #1"

    def test_different_payload_hash_regenerates(self, fake_llm):
        dashboard_insights.get_or_generate_annotation(
            user_id="u1",
            chart_key="spending",
            period_key="2026-05",
            payload=SPENDING_PAYLOAD,
            user_config=UserConfig(name="Test"),
        )
        assert fake_llm["count"] == 1

        # New data arrived: same (chart, period) but a different payload.
        changed = {**SPENDING_PAYLOAD, "total_spend": 2000.0}
        result = dashboard_insights.get_or_generate_annotation(
            user_id="u1",
            chart_key="spending",
            period_key="2026-05",
            payload=changed,
            user_config=UserConfig(name="Test"),
        )
        assert fake_llm["count"] == 2
        assert result["cached"] is False
        assert result["annotation"] == "warm annotation #2"

    def test_force_always_calls_llm(self, fake_llm):
        dashboard_insights.get_or_generate_annotation(
            user_id="u1",
            chart_key="spending",
            period_key="2026-05",
            payload=SPENDING_PAYLOAD,
            user_config=UserConfig(name="Test"),
        )
        assert fake_llm["count"] == 1

        # force=True regenerates even though the payload hash is identical.
        result = dashboard_insights.get_or_generate_annotation(
            user_id="u1",
            chart_key="spending",
            period_key="2026-05",
            payload=SPENDING_PAYLOAD,
            user_config=UserConfig(name="Test"),
            force=True,
        )
        assert fake_llm["count"] == 2
        assert result["cached"] is False


class TestMultiUserIsolation:
    def test_two_users_do_not_share_annotation_cache(self, fake_llm):
        # User u1 generates an annotation for (spending, 2026-05).
        r1 = dashboard_insights.get_or_generate_annotation(
            user_id="u1",
            chart_key="spending",
            period_key="2026-05",
            payload=SPENDING_PAYLOAD,
            user_config=UserConfig(name="One"),
        )
        assert fake_llm["count"] == 1
        assert r1["cached"] is False

        # User u2 hits the SAME (chart, period) — must NOT read u1's cache.
        r2 = dashboard_insights.get_or_generate_annotation(
            user_id="u2",
            chart_key="spending",
            period_key="2026-05",
            payload=SPENDING_PAYLOAD,
            user_config=UserConfig(name="Two"),
        )
        assert fake_llm["count"] == 2  # a fresh generation, not a cross-tenant hit
        assert r2["cached"] is False

        # Each user's row is independent.
        assert ChartAnnotationStore.get("u1", "spending", "2026-05")["annotation_text"] == "warm annotation #1"
        assert ChartAnnotationStore.get("u2", "spending", "2026-05")["annotation_text"] == "warm annotation #2"

        # And u1's cache still serves on a repeat — untouched by u2.
        r1_again = dashboard_insights.get_or_generate_annotation(
            user_id="u1",
            chart_key="spending",
            period_key="2026-05",
            payload=SPENDING_PAYLOAD,
            user_config=UserConfig(name="One"),
        )
        assert fake_llm["count"] == 2
        assert r1_again["cached"] is True
        assert r1_again["annotation"] == "warm annotation #1"


class TestPayloadHash:
    def test_hash_is_stable_and_order_independent(self):
        a = {"x": 1, "y": [1, 2], "z": "t"}
        b = {"z": "t", "y": [1, 2], "x": 1}
        assert dashboard_insights._payload_hash(a) == dashboard_insights._payload_hash(b)

    def test_hash_changes_with_content(self):
        a = {"total": 1000.0}
        b = {"total": 2000.0}
        assert dashboard_insights._payload_hash(a) != dashboard_insights._payload_hash(b)


class TestDerivedBudget:
    @pytest.fixture
    def fake_budget_llm(self, monkeypatch):
        calls = {"count": 0}

        def _fake(goal_key, goal_text, recent_category_avgs, wiki_slice=""):
            calls["count"] += 1
            return [
                {"category": "Dining", "hint_text": "Keep around $350/mo."},
                {"category": "Shopping", "hint_text": "You've averaged a bit more lately."},
            ]

        monkeypatch.setattr(
            dashboard_insights.llm_orchestrator,
            "generate_derived_budget",
            _fake,
        )
        return calls

    def test_generates_and_persists_when_empty(self, fake_budget_llm):
        from src.storage import UserConfigStore
        cfg = UserConfig(name="Test", goal_key="pay_off_credit", goal_text="clear my card")
        UserConfigStore.save(cfg)

        hints = dashboard_insights.get_or_generate_derived_budget(
            user_config=cfg,
            recent_category_avgs={"Dining": 420.0, "Shopping": 600.0},
        )
        assert fake_budget_llm["count"] == 1
        assert len(hints) == 2
        assert hints[0]["category"] == "Dining"

        # Persisted to config.
        reloaded = UserConfigStore.load()
        assert len(reloaded.derived_budget) == 2
        assert reloaded.derived_budget_generated_at is not None

    def test_force_regenerates(self, fake_budget_llm):
        from src.storage import BudgetHint, UserConfigStore
        cfg = UserConfig(
            name="Test",
            goal_key="pay_off_credit",
            derived_budget=[BudgetHint(category="Old", hint_text="old hint")],
            derived_budget_generated_at="2026-01-01T00:00:00",
        )
        UserConfigStore.save(cfg)

        # Without force, the existing (non-empty) budget is returned untouched.
        hints = dashboard_insights.get_or_generate_derived_budget(
            user_config=UserConfigStore.load(),
            recent_category_avgs={"Dining": 420.0},
        )
        assert fake_budget_llm["count"] == 0
        assert hints[0]["category"] == "Old"

        # With force, the LLM is called and the budget replaced.
        hints = dashboard_insights.get_or_generate_derived_budget(
            user_config=UserConfigStore.load(),
            recent_category_avgs={"Dining": 420.0},
            force=True,
        )
        assert fake_budget_llm["count"] == 1
        assert hints[0]["category"] == "Dining"
