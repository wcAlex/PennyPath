"""Tests for the user-overlay storage layer (Phase 1C).

Covers schema shape, OverrideStore manual writes, AuditStore append-and-list,
and the view's effective category / exclusion behavior. Rule storage and
materialization land in tests/test_rule_matching.py.

All tests run against a tmp DB via the conftest `tmp_data` fixture.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from src.storage import (
    AuditStore,
    AUDIT_CLEAR_OVERRIDE,
    AUDIT_SET_OVERRIDE,
    OverrideStore,
    SOURCE_KIND_USER_MANUAL,
    TransactionStore,
)


# --- Helpers -----------------------------------------------------------------


def _seed_raw_row(
    user_id: str = "u1",
    tx_id: str = "tx-1",
    *,
    category: str = "Dining",
    flow_type: str = "spending",
    description: str = "PACIFIC TABLE #4421 NY",
    amount: float = 42.0,
    date: str = "2026-04-15",
    account_type: str = "credit",
    section_type: str = "purchase",
    account_id: str = "acc-1",
) -> None:
    TransactionStore.init_db()
    now = datetime.now().isoformat()
    with sqlite3.connect(TransactionStore.DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO transactions "
            "(id, date, amount, description, category, account_type, "
            "source_file, user_id, account_id, source, dedup_hash, flow_type, "
            "notes, section_type, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, 'test', '', ?, '', ?, ?)",
            (tx_id, date, amount, description, category, account_type,
             user_id, account_id, flow_type, section_type, now),
        )
        conn.commit()


def _effective_row(user_id: str, tx_id: str) -> dict | None:
    TransactionStore.init_db()
    with sqlite3.connect(TransactionStore.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM v_transactions_effective "
            "WHERE user_id = ? AND id = ?",
            (user_id, tx_id),
        ).fetchone()
    return dict(row) if row else None


# --- Schema ------------------------------------------------------------------


class TestSchema:
    def test_init_db_creates_all_tables(self):
        TransactionStore.init_db()
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table', 'view')"
                ).fetchall()
            }
        assert "transaction_overrides" in tables
        assert "category_rules" in tables
        assert "override_audit" in tables
        assert "v_transactions_effective" in tables

    def test_old_recon_view_is_dropped(self):
        TransactionStore.init_db()
        with sqlite3.connect(TransactionStore.DB_PATH) as conn:
            views = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'view'"
                ).fetchall()
            }
        assert "v_transactions_recon" not in views


# --- OverrideStore (manual) --------------------------------------------------


class TestManualOverride:
    def test_set_category_only(self):
        _seed_raw_row()
        OverrideStore.set_override("u1", "tx-1", category="Kids Education")
        row = _effective_row("u1", "tx-1")
        assert row["category"] == "Kids Education"
        # flow_type_recon falls back through COALESCE to raw because override
        # didn't set it.
        assert row["flow_type_recon"] == "spending"
        assert row["override_source"] == SOURCE_KIND_USER_MANUAL

    def test_set_flow_type_and_exclude(self):
        _seed_raw_row()
        OverrideStore.set_override(
            "u1", "tx-1",
            flow_type="transfer",
            is_excluded=1,
            note="duplicate from spouse's account",
        )
        row = _effective_row("u1", "tx-1")
        assert row["flow_type_recon"] == "transfer"
        assert row["is_user_excluded"] == 1
        assert row["override_note"] == "duplicate from spouse's account"

    def test_get_returns_current_override(self):
        _seed_raw_row()
        OverrideStore.set_override("u1", "tx-1", category="Travel")
        got = OverrideStore.get("u1", "tx-1")
        assert got is not None
        assert got["category"] == "Travel"
        assert got["source_kind"] == SOURCE_KIND_USER_MANUAL
        assert got["source_rule_id"] is None

    def test_get_returns_none_when_absent(self):
        _seed_raw_row()
        assert OverrideStore.get("u1", "tx-1") is None

    def test_repeat_set_updates_in_place(self):
        _seed_raw_row()
        OverrideStore.set_override("u1", "tx-1", category="Travel")
        first = OverrideStore.get("u1", "tx-1")
        OverrideStore.set_override("u1", "tx-1", category="Kids Education")
        second = OverrideStore.get("u1", "tx-1")
        assert second["category"] == "Kids Education"
        # created_at preserved across updates.
        assert second["created_at"] == first["created_at"]
        # updated_at advances (or at least doesn't go backward).
        assert second["updated_at"] >= first["updated_at"]

    def test_set_without_any_field_raises(self):
        _seed_raw_row()
        with pytest.raises(ValueError):
            OverrideStore.set_override("u1", "tx-1")

    def test_clear_removes_row(self):
        _seed_raw_row()
        OverrideStore.set_override("u1", "tx-1", category="Travel")
        removed = OverrideStore.clear_override("u1", "tx-1")
        assert removed is not None
        assert removed["category"] == "Travel"
        # Effective view falls back to raw.
        row = _effective_row("u1", "tx-1")
        assert row["category"] == "Dining"
        assert row["override_source"] is None

    def test_clear_when_no_override_returns_none(self):
        _seed_raw_row()
        assert OverrideStore.clear_override("u1", "tx-1") is None

    def test_list_overrides_ordered_by_updated_at_desc(self):
        _seed_raw_row(tx_id="tx-1")
        _seed_raw_row(tx_id="tx-2")
        _seed_raw_row(tx_id="tx-3")
        OverrideStore.set_override("u1", "tx-1", category="Travel")
        OverrideStore.set_override("u1", "tx-2", category="Travel")
        OverrideStore.set_override("u1", "tx-3", category="Travel")
        # Touch tx-1 last so it floats to the top.
        OverrideStore.set_override("u1", "tx-1", category="Education")
        rows = OverrideStore.list_overrides("u1")
        assert [r["transaction_id"] for r in rows[:1]] == ["tx-1"]

    def test_list_filters_by_transaction_id(self):
        _seed_raw_row(tx_id="tx-1")
        _seed_raw_row(tx_id="tx-2")
        OverrideStore.set_override("u1", "tx-1", category="Travel")
        OverrideStore.set_override("u1", "tx-2", category="Travel")
        rows = OverrideStore.list_overrides("u1", transaction_id="tx-2")
        assert len(rows) == 1
        assert rows[0]["transaction_id"] == "tx-2"


# --- AuditStore --------------------------------------------------------------


class TestAudit:
    def test_set_override_writes_audit_row(self):
        _seed_raw_row()
        OverrideStore.set_override(
            "u1", "tx-1",
            category="Kids Education",
            chat_session_id="s_test",
            chat_message_id="m_42",
        )
        events = AuditStore.list_events("u1")
        assert len(events) == 1
        ev = events[0]
        assert ev["action"] == AUDIT_SET_OVERRIDE
        assert ev["transaction_id"] == "tx-1"
        assert ev["before"] is None
        assert ev["after"]["category"] == "Kids Education"
        assert ev["chat_session_id"] == "s_test"
        assert ev["chat_message_id"] == "m_42"

    def test_repeat_set_writes_two_audit_rows_with_before(self):
        _seed_raw_row()
        OverrideStore.set_override("u1", "tx-1", category="Travel")
        OverrideStore.set_override("u1", "tx-1", category="Kids Education")
        events = AuditStore.list_events("u1")
        assert len(events) == 2
        # Most recent first; the second set has before.category == 'Travel'
        assert events[0]["after"]["category"] == "Kids Education"
        assert events[0]["before"]["category"] == "Travel"

    def test_clear_writes_audit_row_with_before(self):
        _seed_raw_row()
        OverrideStore.set_override("u1", "tx-1", category="Travel")
        OverrideStore.clear_override("u1", "tx-1")
        events = AuditStore.list_events("u1")
        # set + clear = 2 events.
        assert len(events) == 2
        assert events[0]["action"] == AUDIT_CLEAR_OVERRIDE
        assert events[0]["before"]["category"] == "Travel"
        assert events[0]["after"] is None

    def test_filter_by_transaction_id(self):
        _seed_raw_row(tx_id="tx-1")
        _seed_raw_row(tx_id="tx-2")
        OverrideStore.set_override("u1", "tx-1", category="Travel")
        OverrideStore.set_override("u1", "tx-2", category="Education")
        only_two = AuditStore.list_events("u1", transaction_id="tx-2")
        assert len(only_two) == 1
        assert only_two[0]["transaction_id"] == "tx-2"

    def test_since_filter(self):
        _seed_raw_row()
        OverrideStore.set_override("u1", "tx-1", category="Travel")
        future = "2099-01-01T00:00:00"
        assert AuditStore.list_events("u1", since=future) == []


# --- Reader behavior: dashboard sees the override --------------------------


class TestReaderRespectsOverlay:
    """Spending donuts and category trends pick up overrides automatically
    because every reader goes through v_transactions_effective."""

    def test_category_override_moves_amount_in_breakdown(self):
        from datetime import date as _date
        from src.dashboard_queries import spending_breakdown

        _seed_raw_row(tx_id="tx-1", category="Dining", amount=42.0)
        _seed_raw_row(tx_id="tx-2", category="Dining", amount=58.0)

        # Move tx-1 to Kids Education.
        OverrideStore.set_override("u1", "tx-1", category="Kids Education")

        sb = spending_breakdown("u1", _date(2026, 4, 1), _date(2026, 4, 30))
        cat_amounts = {c["name"]: c["amount"] for c in sb["categories"]}
        assert cat_amounts.get("Dining") == 58.0
        assert cat_amounts.get("Kids Education") == 42.0
        # Total spending unchanged — the override moves money sideways.
        assert sb["total_spend"] == 100.0

    def test_is_excluded_drops_amount_from_spending(self):
        from datetime import date as _date
        from src.dashboard_queries import spending_breakdown

        _seed_raw_row(tx_id="tx-1", category="Dining", amount=42.0)
        _seed_raw_row(tx_id="tx-2", category="Dining", amount=58.0)

        # Exclude tx-1 entirely.
        OverrideStore.set_override("u1", "tx-1", is_excluded=1)

        sb = spending_breakdown("u1", _date(2026, 4, 1), _date(2026, 4, 30))
        cat_amounts = {c["name"]: c["amount"] for c in sb["categories"]}
        assert cat_amounts.get("Dining") == 58.0
        assert sb["total_spend"] == 58.0

    def test_list_transactions_signed_carries_override_flags(self):
        from datetime import date as _date
        from src.dashboard_queries import list_transactions_signed

        _seed_raw_row(tx_id="tx-1", category="Dining")
        OverrideStore.set_override("u1", "tx-1", category="Kids Education")

        rows = list_transactions_signed("u1", _date(2026, 4, 1), _date(2026, 4, 30))
        assert len(rows) == 1
        r = rows[0]
        assert r["category"] == "Kids Education"
        assert r["override_source"] == SOURCE_KIND_USER_MANUAL
        assert r["is_user_excluded"] is False
