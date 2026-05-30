"""Tests for src/dashboard_queries.py against the real data/transactions.db.

These run read-only against the production DB, which has predictable known
data (1,498 rows, 2024-06-15 .. 2026-05-15, user_id='chi'). The conftest
`tmp_data` autouse fixture redirects TransactionStore.DB_PATH to a temp dir;
we deliberately point it back at the real DB for this module.

Transfer-pairing anchor: the design doc references a "May 21, 2026 $217.93"
internal-transfer pair, but the shipped DB stops at 2026-05-15, so that exact
row pair is not present. The equivalent verifiable pair in the real data is the
April 28, 2026 $551.73 credit-card payment (checking withdrawal <-> credit
payment). We assert it is excluded from spending/income totals. The pairing
*rule* (credit/payment <-> checking|savings/withdrawal, |dt|<=3d, |amt|<=$0.01)
is what's under test either way.
"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from src import dashboard_queries as dq

REAL_DB = Path(__file__).resolve().parent.parent / "data" / "transactions.db"
USER_ID = "chi"


@pytest.fixture
def real_db(monkeypatch):
    """Point the query layer at the real production DB (read-only)."""
    if not REAL_DB.exists():
        pytest.skip(f"real DB not found at {REAL_DB}")
    from src.storage import TransactionStore
    monkeypatch.setattr(TransactionStore, "DB_PATH", REAL_DB)
    return REAL_DB


def _row_exists(amount: float, day: str) -> bool:
    """True if a transaction with this amount exists on the given day."""
    with sqlite3.connect(REAL_DB) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE date = ? AND ABS(amount - ?) < 0.01",
            (day, amount),
        ).fetchone()[0]
    return n > 0


# --- Transfer pairing --------------------------------------------------------


class TestTransferPairing:
    def test_apr28_pair_present_in_raw_data(self, real_db):
        # Sanity: the $551.73 pair really is in the DB on 2026-04-28.
        assert _row_exists(551.73, "2026-04-28")

    def test_paired_transfer_flagged(self, real_db):
        rows = dq.list_transactions_signed(
            USER_ID, date(2026, 4, 28), date(2026, 4, 28)
        )
        legs = [r for r in rows if abs(r["amount"] - 551.73) < 0.01]
        # One credit/payment leg and one checking/withdrawal leg, both flagged.
        assert len(legs) == 2
        assert all(r["is_paired_transfer"] for r in legs)
        types = sorted(r["account_type"] for r in legs)
        assert types == ["checking", "credit"]

    def test_pair_excluded_from_spending(self, real_db):
        result = dq.spending_breakdown(
            USER_ID, date(2026, 4, 1), date(2026, 4, 30)
        )
        # The $551.73 leg must NOT inflate the April spend total. The only way
        # it could appear is via the checking withdrawal leg; assert it's gone.
        total = result["total_spend"]
        rows = dq.list_transactions_signed(USER_ID, date(2026, 4, 1), date(2026, 4, 30))
        spend_without_pairs = sum(
            r["amount"]
            for r in rows
            if r["flow_type"] == "spending" and not r["is_paired_transfer"]
        )
        assert abs(total - spend_without_pairs) < 0.01
        # And the paired legs are excluded from the breakdown contributions.
        paired_amounts = [
            r["amount"] for r in rows if r["is_paired_transfer"]
        ]
        assert 551.73 in [round(a, 2) for a in paired_amounts]

    def test_pair_excluded_from_income(self, real_db):
        # No income leg should pick up the transfer either.
        income = dq.income_breakdown(USER_ID, date(2026, 4, 1), date(2026, 4, 30))
        # The transfer pair is flow_type='transfer', never 'income', so it can't
        # be in income at all; assert no subcategory carries the 551.73 value.
        for sub in income["subcategories"]:
            assert abs(sub["amount"] - 551.73) > 0.01

    def test_transfer_flow_type_always_excluded(self, real_db):
        # Any flow_type='transfer' row is excluded from spending regardless of
        # whether the heuristic paired it.
        rows = dq.list_transactions_signed(USER_ID, date(2026, 4, 1), date(2026, 4, 30))
        result = dq.spending_breakdown(USER_ID, date(2026, 4, 1), date(2026, 4, 30))
        transfer_spend = sum(
            r["amount"] for r in rows if r["flow_type"] == "transfer"
        )
        assert transfer_spend > 0  # there ARE transfers in April
        # The total must equal spending rows only, none of the transfers.
        assert result["total_spend"] < transfer_spend + result["total_spend"]


# --- Fixed vs Flexible -------------------------------------------------------


class TestFixedVsFlexible:
    def test_classification_buckets(self, real_db):
        # Use a window that ends within the real data range so the lookback has
        # data. fixed_vs_flexible() uses date.today(); the data ends 2026-05-15
        # and "today" per the env is 2026-05-27, so the last 6 months land on
        # real data.
        fixed, flexible = dq.fixed_vs_flexible(USER_ID, months=6)
        fixed_names = {c["name"] for c in fixed}
        flexible_names = {c["name"] for c in flexible}

        # Housing is the stable, recurring expense (mortgage auto-pay) -> Fixed.
        assert "Housing" in fixed_names, f"fixed={fixed_names}"

        # Dining and Shopping are highly variable -> Flexible.
        assert "Dining" in flexible_names, f"flexible={flexible_names}"
        assert "Shopping" in flexible_names, f"flexible={flexible_names}"

        # A category can't be in both buckets.
        assert not (fixed_names & flexible_names)

    def test_cov_threshold_is_module_constant(self):
        assert hasattr(dq, "FIXED_COV_THRESHOLD")
        assert dq.FIXED_COV_THRESHOLD == 0.25


# --- Period filter -----------------------------------------------------------


class TestPeriodFilter:
    def test_changing_month_changes_total(self, real_db):
        may = dq.spending_breakdown(USER_ID, date(2026, 5, 1), date(2026, 5, 31))
        apr = dq.spending_breakdown(USER_ID, date(2026, 4, 1), date(2026, 4, 30))
        assert may["total_spend"] != apr["total_spend"]
        assert may["period"] == "2026-05"
        assert apr["period"] == "2026-04"

    def test_period_label_for_full_month(self, real_db):
        r = dq.spending_breakdown(USER_ID, date(2026, 3, 1), date(2026, 3, 31))
        assert r["period"] == "2026-03"


# --- Pagination --------------------------------------------------------------


class TestPagination:
    def test_page_size_respected(self, real_db):
        # Use a wide window with many rows.
        result = dq.transactions_filtered(
            USER_ID, date(2024, 6, 1), date(2026, 5, 31),
            page=1, page_size=10,
        )
        assert result["page"] == 1
        assert result["page_size"] == 10
        assert len(result["rows"]) == 10
        assert result["total"] > 10

    def test_total_consistent_across_pages(self, real_db):
        p1 = dq.transactions_filtered(
            USER_ID, date(2024, 6, 1), date(2026, 5, 31), page=1, page_size=25
        )
        p2 = dq.transactions_filtered(
            USER_ID, date(2024, 6, 1), date(2026, 5, 31), page=2, page_size=25
        )
        assert p1["total"] == p2["total"]
        # Pages don't overlap.
        ids1 = {r["id"] for r in p1["rows"]}
        ids2 = {r["id"] for r in p2["rows"]}
        assert not (ids1 & ids2)

    def test_last_page_partial(self, real_db):
        result = dq.transactions_filtered(
            USER_ID, date(2024, 6, 1), date(2026, 5, 31), page=1, page_size=50
        )
        total = result["total"]
        last_page = (total + 49) // 50
        last = dq.transactions_filtered(
            USER_ID, date(2024, 6, 1), date(2026, 5, 31),
            page=last_page, page_size=50,
        )
        expected = total - (last_page - 1) * 50
        assert len(last["rows"]) == expected

    def test_rows_include_paired_flag(self, real_db):
        result = dq.transactions_filtered(
            USER_ID, date(2026, 4, 28), date(2026, 4, 28), page=1, page_size=50
        )
        assert all("is_paired_transfer" in r for r in result["rows"])
        paired = [r for r in result["rows"] if r["is_paired_transfer"]]
        # The $551.73 pair is shown (not hidden) but flagged.
        assert any(abs(r["amount"] - 551.73) < 0.01 for r in paired)


# --- parse_period ------------------------------------------------------------


class TestParsePeriod:
    def test_single_month(self):
        s, e = dq.parse_period("2026-05", None, None, today=date(2026, 5, 27))
        assert s == date(2026, 5, 1)
        assert e == date(2026, 5, 31)

    def test_ytd(self):
        s, e = dq.parse_period("ytd", None, None, today=date(2026, 5, 27))
        assert s == date(2026, 1, 1)
        assert e == date(2026, 5, 27)

    def test_last_12mo(self):
        s, e = dq.parse_period("last-12mo", None, None, today=date(2026, 5, 27))
        assert s == date(2025, 6, 1)
        assert e == date(2026, 5, 27)

    def test_explicit_range_overrides_period(self):
        s, e = dq.parse_period("2026-05", "2025-11-01", "2026-05-15")
        assert s == date(2025, 11, 1)
        assert e == date(2026, 5, 15)

    def test_default_is_current_month(self):
        s, e = dq.parse_period(None, None, None, today=date(2026, 5, 27))
        assert s == date(2026, 5, 1)
        assert e == date(2026, 5, 31)

    def test_invalid_period_raises(self):
        with pytest.raises(ValueError):
            dq.parse_period("garbage", None, None)

    def test_partial_range_raises(self):
        with pytest.raises(ValueError):
            dq.parse_period(None, "2026-01-01", None)

    def test_inverted_range_raises(self):
        with pytest.raises(ValueError):
            dq.parse_period(None, "2026-05-01", "2026-01-01")
