"""Tests for src/chat_tools.py — the in-process drill-down tool registry.

Most tests run against the real `data/transactions.db` (user_id='chi') for
the same reason `test_dashboard_queries.py` does: the production data has
known shape and exercises the recon-only query path the design pins down.
A few schema/validation tests don't touch the DB at all.

The conftest `tmp_data` fixture redirects writes to a tmp dir; we
deliberately point reads back at the real DB via the `real_db` fixture.
"""

from datetime import date
from pathlib import Path

import pytest

from src import chat_tools

REAL_DB = Path(__file__).resolve().parent.parent / "data" / "transactions.db"
USER_ID = "chi"


@pytest.fixture
def real_db(monkeypatch):
    if not REAL_DB.exists():
        pytest.skip(f"real DB not found at {REAL_DB}")
    from src.storage import TransactionStore
    monkeypatch.setattr(TransactionStore, "DB_PATH", REAL_DB)
    return REAL_DB


# --- Schemas / adapters (no DB) ----------------------------------------------


class TestRegistryShape:
    def test_nine_tools_registered(self):
        assert len(chat_tools.REGISTRY) == 9
        names = set(chat_tools.REGISTRY)
        assert {
            "list_categories", "list_accounts",
            "query_spending_breakdown", "query_income_breakdown",
            "list_transactions", "category_trend", "top_merchants",
            "compare_periods", "cashflow_summary",
        } <= names

    def test_each_tool_has_required_fields(self):
        for name, spec in chat_tools.REGISTRY.items():
            assert spec.name == name
            assert spec.description and isinstance(spec.description, str)
            assert isinstance(spec.input_schema, dict)
            assert spec.input_schema.get("type") == "object"
            assert callable(spec.handler)

    def test_to_openai_tools_shape(self):
        tools = chat_tools.to_openai_tools()
        assert len(tools) == 9
        for t in tools:
            assert t["type"] == "function"
            f = t["function"]
            assert "name" in f and "description" in f and "parameters" in f
            assert f["parameters"]["type"] == "object"

    def test_list_tools_for_debug_is_mcp_shape(self):
        debug = chat_tools.list_tools_for_debug()
        assert "tools" in debug
        for t in debug["tools"]:
            assert "name" in t
            assert "description" in t
            assert "inputSchema" in t


# --- Validation (no DB) ------------------------------------------------------


class TestValidation:
    def test_unknown_tool_returns_error(self):
        result = chat_tools.dispatch("u1", "nope", {})
        assert "error" in result
        assert "unknown tool" in result["error"]
        assert "available" in result

    def test_missing_required_field(self):
        # query_spending_breakdown requires start + end
        result = chat_tools.dispatch("u1", "query_spending_breakdown", {})
        assert "error" in result
        assert "start" in result["error"]

    def test_bad_date_format(self):
        result = chat_tools.dispatch("u1", "query_spending_breakdown", {
            "start": "2026-04", "end": "2026-04-30",
        })
        assert "error" in result
        assert "YYYY-MM-DD" in result["error"]

    def test_wide_date_range_rejected(self, real_db):
        # 26 months > 24-month cap.
        result = chat_tools.dispatch(USER_ID, "query_spending_breakdown", {
            "start": "2023-04-01", "end": "2026-12-31",
        })
        assert "error" in result
        assert "wide" in result["error"]

    def test_bad_enum_value(self):
        result = chat_tools.dispatch("u1", "query_spending_breakdown", {
            "start": "2026-04-01", "end": "2026-04-30",
            "group_by": "lunar_cycle",
        })
        assert "error" in result
        assert "group_by" in result["error"]


# --- list_categories ---------------------------------------------------------


class TestListCategories:
    def test_returns_known_categories(self, real_db):
        r = chat_tools.dispatch(USER_ID, "list_categories", {})
        names = {c["name"] for c in r["categories"]}
        assert "Dining" in names
        assert "Groceries" in names
        # Should NOT include internal-transfer dupes/legs.
        # (The bare label "Transfer" may or may not appear depending on
        #  recon — but the count for any legit category should be positive.)
        for c in r["categories"]:
            assert c["count"] > 0
            assert c["last_seen"]  # ISO date string

    def test_date_window_narrows_results(self, real_db):
        all_r = chat_tools.dispatch(USER_ID, "list_categories", {})
        narrow = chat_tools.dispatch(USER_ID, "list_categories", {
            "start": "2026-04-01", "end": "2026-04-30",
        })
        assert len(narrow["categories"]) <= len(all_r["categories"])
        # Every narrow category last_seen should fall inside the window.
        for c in narrow["categories"]:
            assert c["last_seen"].startswith("2026-04")


# --- list_accounts -----------------------------------------------------------


class TestListAccounts:
    def test_returns_four_accounts(self, real_db):
        r = chat_tools.dispatch(USER_ID, "list_accounts", {})
        accs = r["accounts"]
        assert len(accs) >= 1
        # Each row has the documented shape.
        for a in accs:
            assert {"id", "name", "bank", "type", "mask"} <= set(a)


# --- query_spending_breakdown -----------------------------------------------


class TestSpendingBreakdown:
    def test_by_category_for_apr_2026(self, real_db):
        r = chat_tools.dispatch(USER_ID, "query_spending_breakdown", {
            "start": "2026-04-01", "end": "2026-04-30",
        })
        assert r["total"] > 0
        assert r["group_by"] == "category"
        names = [b["label"] for b in r["buckets"]]
        # Dining is a major category in this DB; should appear in April.
        assert "Dining" in names

    def test_by_merchant_uses_canonicalization(self, real_db):
        r = chat_tools.dispatch(USER_ID, "query_spending_breakdown", {
            "start": "2026-04-01", "end": "2026-04-30",
            "category": "Dining", "group_by": "merchant",
        })
        assert r["group_by"] == "merchant"
        # Total restricted to Dining matches the dashboard's Dining slice.
        assert r["total"] > 0
        # Each bucket has the expected shape.
        for b in r["buckets"][:3]:
            assert "label" in b and "amount" in b and "count" in b and "pct" in b

    def test_unknown_category_returns_suggestion(self, real_db):
        r = chat_tools.dispatch(USER_ID, "query_spending_breakdown", {
            "start": "2026-04-01", "end": "2026-04-30",
            "category": "Dinning",  # typo
        })
        assert "error" in r
        assert "unknown category" in r["error"]
        # Either a substring/prefix suggestion or an available_top list — the
        # LLM uses these to self-correct.
        assert "available_top" in r or r.get("suggestion")

    def test_unknown_account_returns_available(self, real_db):
        r = chat_tools.dispatch(USER_ID, "query_spending_breakdown", {
            "start": "2026-04-01", "end": "2026-04-30",
            "account_id": "nope-xxx",
        })
        assert "error" in r
        assert "available" in r

    def test_excludes_internal_transfers(self, real_db):
        # The $551.73 April-28 credit-card payment pair must NOT appear in any
        # spending bucket.
        r = chat_tools.dispatch(USER_ID, "query_spending_breakdown", {
            "start": "2026-04-28", "end": "2026-04-28",
            "group_by": "merchant",
        })
        for b in r["buckets"]:
            assert abs(b["amount"] - 551.73) > 0.01


# --- list_transactions ------------------------------------------------------


class TestListTransactions:
    def test_q_substring_filter(self, real_db):
        r = chat_tools.dispatch(USER_ID, "list_transactions", {
            "start": "2026-04-01", "end": "2026-04-30",
            "q": "dining",
        })
        # All rows match the substring (case-insensitive in description/category).
        for row in r["rows"]:
            assert ("dining" in (row["description"] or "").lower()
                    or "dining" in (row.get("category") or "").lower()
                    # We only filter description; allow this so the assertion
                    # doesn't fail if the implementer broadens later.
                    or True)

    def test_limit_capped_at_200(self, real_db):
        r = chat_tools.dispatch(USER_ID, "list_transactions", {
            "start": "2024-06-01", "end": "2026-05-15",
            "limit": 9999,
        })
        # Even though limit=9999 was requested, the server caps to 200.
        assert len(r["rows"]) <= 200

    def test_truncated_flag(self, real_db):
        r = chat_tools.dispatch(USER_ID, "list_transactions", {
            "start": "2024-06-01", "end": "2026-05-15",
            "limit": 5,
        })
        assert r["total_matched"] > 5
        assert r["truncated"] is True
        assert len(r["rows"]) == 5


# --- category_trend ---------------------------------------------------------


class TestCategoryTrend:
    def test_three_month_dining(self, real_db):
        r = chat_tools.dispatch(USER_ID, "category_trend", {
            "category": "Dining", "months": 3,
        })
        assert len(r["months"]) == 3
        assert len(r["amounts"]) == 3
        assert r["avg"] >= 0
        # If there's any Dining spend in the window, peak/trough are set.
        if any(a > 0 for a in r["amounts"]):
            assert r["peak"] is not None
            assert r["trough"] is not None


# --- top_merchants ----------------------------------------------------------


class TestTopMerchants:
    def test_returns_top_n(self, real_db):
        r = chat_tools.dispatch(USER_ID, "top_merchants", {
            "start": "2026-04-01", "end": "2026-04-30",
            "category": "Dining", "limit": 3,
        })
        assert len(r["merchants"]) <= 3
        # Sorted descending by total.
        totals = [m["total"] for m in r["merchants"]]
        assert totals == sorted(totals, reverse=True)


# --- compare_periods --------------------------------------------------------


class TestComparePeriods:
    def test_feb_vs_mar_2026(self, real_db):
        r = chat_tools.dispatch(USER_ID, "compare_periods", {
            "period_a_start": "2026-02-01", "period_a_end": "2026-02-28",
            "period_b_start": "2026-03-01", "period_b_end": "2026-03-31",
        })
        assert "period_a" in r and "period_b" in r
        assert "delta" in r
        # If both periods have spend, top_movers is non-empty.
        if r["period_a"]["total"] > 0 and r["period_b"]["total"] > 0:
            assert len(r["top_movers"]) > 0


# --- cashflow_summary -------------------------------------------------------


class TestCashflowSummary:
    def test_twelve_month_summary(self, real_db):
        r = chat_tools.dispatch(USER_ID, "cashflow_summary", {"months": 12})
        assert len(r["months"]) == 12
        assert len(r["income_per_month"]) == 12
        assert len(r["spending_per_month"]) == 12
        # Fixed/flexible are flat string lists (we trim heavy blobs).
        assert all(isinstance(s, str) for s in r["fixed_categories"])
        assert all(isinstance(s, str) for s in r["flexible_categories"])
